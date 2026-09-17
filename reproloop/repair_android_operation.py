"""Durable private ownership for protected Android mobile operations.

This journal stages inert APK bytes and records callback/native intent.  It
does not stop a device, sanitize fixtures, qualify a result, or authorize
RunStore reservation release.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time

from . import contracts
from .execution.artifacts import ArtifactError, BlobSet, open_directory
from .execution.journal import RunDenied, RunStore, TERMINAL
from .execution.wire import ProtocolError, canonical, decode_json
from .repair_android import AndroidMobileAdapterConfig, NATIVE_TOOL_OWNERSHIP_VERSION
from .repair_mobile import MobileContext
from .storage import MAX_APK


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z][a-z0-9_-]{0,127}\Z")
_APK_NAMES = ("candidate.apk", "original.apk", "helper.apk")
_MAX_METADATA_BYTES = 512 * 1024
_MAX_REPLAYS = 128
_RECORD_TEMP = re.compile(r"\.record-[0-9a-f]{32}\Z")
_METADATA_NAMES = frozenset(('intent.json', 'state.json', 'stage.json', 'native.json',
    'discard.json', 'recovery.json', 'finalization.json', 'recovery-materials.json',
    'install.json', 'cleanup.json'))


class AndroidOperationError(RuntimeError):
    def __init__(self, code="android_operation_unavailable"):
        self.code = code
        super().__init__(code)


def _require(condition, code="android_operation_unavailable"):
    if not condition:
        raise AndroidOperationError(code)


def _identity_info(info):
    return {"device": info.st_dev, "inode": info.st_ino,
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
            "links": info.st_nlink}


def _valid_identity(value, *, directory=False):
    return (type(value) is dict
            and set(value) == {"device", "inode", "mode", "uid", "links"}
            and all(type(item) is int for item in value.values())
            and value["device"] >= 0 and value["inode"] > 0
            and value["uid"] == os.getuid() and value["links"] >= 1
            and value["mode"] == (0o700 if directory else 0o600))


def _same_identity(info, expected, *, directory=False):
    if not _valid_identity(expected, directory=directory):
        return False
    actual = _identity_info(info)
    if directory:
        actual.pop("links"); expected = dict(expected); expected.pop("links")
    return actual == expected


def _walk_directory(path, *, create=False):
    path = Path(path)
    _require(path.is_absolute(), "android_operation_storage")
    descriptor = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open("/", flags)
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        info = os.fstat(descriptor)
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                 and not (stat.S_IMODE(info.st_mode) & 0o077),
                 "android_operation_storage")
        return descriptor
    except (OSError, AndroidOperationError):
        if descriptor is not None:
            os.close(descriptor)
        raise AndroidOperationError("android_operation_storage") from None


def _private_directory(path, *, create):
    descriptor = _walk_directory(path, create=create)
    os.close(descriptor)
    return Path(path)


def _walk_source_directory(path):
    path = Path(path)
    _require(path.is_absolute(), "android_operation_source")
    descriptor = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open("/", flags)
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                 and not (mode & 0o022), "android_operation_source")
        return descriptor
    except (OSError, AndroidOperationError):
        if descriptor is not None:
            os.close(descriptor)
        raise AndroidOperationError("android_operation_source") from None


def _open_child_directory(parent, name, *, expected=None):
    descriptor = None
    try:
        _require(type(name) is str and _ID.fullmatch(name),
                 "android_operation_storage")
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(descriptor)
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                 and not (stat.S_IMODE(info.st_mode) & 0o077)
                 and (expected is None
                      or _same_identity(info, expected, directory=True)),
                 "android_operation_storage")
        return descriptor
    except (OSError, AndroidOperationError):
        if descriptor is not None:
            os.close(descriptor)
        raise AndroidOperationError("android_operation_storage") from None


def _metadata_name(name):
    return name in _METADATA_NAMES or re.fullmatch(r'replay-[0-9]{3}\.json', name) is not None


def _same_file(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _pending_metadata_link(parent, name, info):
    if info.st_nlink != 2 or not _metadata_name(name):
        return False
    aliases = [other for other in os.listdir(parent) if _RECORD_TEMP.fullmatch(other)
               and _same_file(os.stat(other, dir_fd=parent, follow_symlinks=False), info)]
    return len(aliases) == 1


def _open_regular_at(parent, name, *, expected=None, writable=False, pending_metadata=False):
    descriptor = None
    try:
        _require(type(name) is str and name not in {"", ".", ".."}
                 and "/" not in name,
                 "android_operation_storage")
        descriptor = os.open(
            name, (os.O_RDWR if writable else os.O_RDONLY)
            | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                 and (info.st_nlink == 1 or pending_metadata and not writable
                      and expected is None and _pending_metadata_link(parent, name, info))
                 and stat.S_IMODE(info.st_mode) == 0o600
                 and (expected is None or _same_identity(info, expected)),
                 "android_operation_storage")
        return descriptor
    except (OSError, AndroidOperationError):
        if descriptor is not None:
            os.close(descriptor)
        raise AndroidOperationError("android_operation_storage") from None


def _read_fd(descriptor, maximum, *, allow_empty=False):
    before = os.fstat(descriptor)
    _require((allow_empty or before.st_size > 0)
             and 0 <= before.st_size <= maximum,
             "android_operation_record")
    os.lseek(descriptor, 0, os.SEEK_SET)
    body = bytearray()
    while len(body) <= maximum:
        chunk = os.read(descriptor, min(1024 * 1024,
                                        maximum + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
    after = os.fstat(descriptor)
    _require(len(body) == before.st_size <= maximum
             and (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
             == (before.st_size, before.st_mtime_ns, before.st_ctime_ns),
             "android_operation_record")
    return bytes(body)


def _read_json_at(parent, name, maximum=_MAX_METADATA_BYTES):
    descriptor = _open_regular_at(parent, name, pending_metadata=True)
    try:
        body = _read_fd(descriptor, maximum)
        value = decode_json(body)
        _require(canonical(value) == body, "android_operation_record")
        return value
    except (ValueError, TypeError, ProtocolError, AndroidOperationError):
        raise AndroidOperationError("android_operation_record") from None
    finally:
        os.close(descriptor)


def _write_new_at(parent, name, value):
    body = canonical(value)
    _require(len(body) <= _MAX_METADATA_BYTES, "android_operation_record")
    _require(_metadata_name(name), "android_operation_record")
    temporary = ".record-" + os.urandom(16).hex()
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        offset = 0
        while offset < len(body):
            count = os.write(descriptor, body[offset:])
            _require(count > 0, "android_operation_record")
            offset += count
        os.fsync(descriptor)
        os.close(descriptor); descriptor = None
        # linkat publishes a complete record without ever replacing an existing
        # target. A crash after publication leaves only this known second link.
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.fsync(parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
    os.fsync(parent)


def _replace_at(parent, name, value):
    body = canonical(value)
    _require(len(body) <= _MAX_METADATA_BYTES, "android_operation_record")
    temporary = ".record-" + os.urandom(16).hex()
    descriptor = None
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        offset = 0
        while offset < len(body):
            count = os.write(descriptor, body[offset:])
            _require(count > 0, "android_operation_record")
            offset += count
        os.fsync(descriptor); os.close(descriptor); descriptor = None
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except (OSError, AndroidOperationError):
        raise AndroidOperationError("android_operation_record") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _file_digest(descriptor, maximum):
    body = _read_fd(descriptor, maximum)
    return hashlib.sha256(body).hexdigest(), len(body)


def _retire_record_temps(parent):
    """Discard only bounded writer scratch after original producer acquisition."""
    names = os.listdir(parent)
    selected = [name for name in names if _RECORD_TEMP.fullmatch(name)]
    _require(len(selected) <= 16, 'android_metadata_scratch_limit')
    opened = []
    total = 0
    try:
        for name in selected:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            opened.append((name, descriptor))
            info = os.fstat(descriptor)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink in (1, 2),
                'android_metadata_scratch_invalid')
            total += info.st_size
            _require(total <= _MAX_METADATA_BYTES, 'android_metadata_scratch_limit')
            if info.st_nlink == 2:
                targets = [target for target in names if _metadata_name(target)
                    and _same_file(os.stat(target, dir_fd=parent, follow_symlinks=False), info)]
                _require(len(targets) == 1, 'android_metadata_scratch_invalid')
        for name, descriptor in opened:
            _require(_same_identity(os.stat(name, dir_fd=parent, follow_symlinks=False),
                                   _identity_info(os.fstat(descriptor))), 'android_metadata_scratch_invalid')
            os.unlink(name, dir_fd=parent)
        if selected:
            os.fsync(parent)
    finally:
        for _, descriptor in opened:
            os.close(descriptor)


def _validate_context(context):
    _require(type(context) is MobileContext
             and type(context.nonce) is str
             and 1 <= len(context.nonce) <= 128,
             "android_operation_context")
    try:
        contracts.validate_id(context.operation_id)
        contracts.validate_id(context.application_id)
        for name in ("request_digest", "repair_plan_digest", "project_digest",
                     "source_digest", "artifact_digest", "scope_digest",
                     "runtime_policy_digest"):
            contracts.validate_digest(getattr(context, name))
    except contracts.ContractError:
        raise AndroidOperationError("android_operation_context") from None


def _context_record(context):
    return {name: getattr(context, name) for name in (
        "operation_id", "request_digest", "repair_plan_digest",
        "project_digest", "application_id", "source_digest",
        "artifact_digest", "scope_digest", "runtime_policy_digest")}


def _fixture_digest(preparations):
    values = []
    for item in preparations:
        plan = item.plan
        value = {name: getattr(plan, name) for name in (
            "project_id", "project_revision", "project_digest",
            "application_id", "fixture_id", "check_recipe_ids",
            "cleanup_recipe_id", "equivalence_digest")}
        value['payloadDigest'] = contracts.digest(item.payload)
        values.append(value)
    return contracts.digest(values)


@dataclass(slots=True)
class AndroidOperation:
    store: "AndroidOperationStore" = field(repr=False)
    run: object = field(repr=False)
    operation_id: str
    request_digest: str
    context_digest: str
    reserved_bytes: int
    staging_root: Path = field(repr=False)
    _issuer: object = field(repr=False)
    _pid: int = field(repr=False)
    _active: bool = field(default=True, repr=False)

    @property
    def candidate_path(self):
        return self.staging_root / "candidate.apk"

    @property
    def original_path(self):
        return self.staging_root / "original.apk"

    @property
    def helper_path(self):
        return self.staging_root / "helper.apk"


@dataclass(slots=True)
class AndroidNativeBinding:
    operation_id: str
    context_digest: str
    binding_digest: str
    ownership_generation: int
    host_incarnation: str = field(repr=False)
    helper_incarnation: str = field(repr=False)
    provider_incarnation: str = field(repr=False)
    _issuer: object = field(repr=False)
    _pid: int = field(repr=False)
    _active: bool = field(default=True, repr=False)


@dataclass(slots=True)
class AndroidPhaseCapability:
    operation_id: str
    context_digest: str
    phase: str
    replay_number: int | None
    native_binding_digest: str | None
    _issuer: object = field(repr=False)
    _pid: int = field(repr=False)
    _thread: int = field(repr=False)
    _producer_fd: int = field(repr=False)
    _active: bool = field(default=True, repr=False)
    _completed: bool = field(default=False, repr=False)


@dataclass(frozen=True, slots=True)
class AndroidRecoveryInspection:
    operation_id: str
    request_digest: str
    context_digest: str
    configuration_digest: str
    state: str
    phase_digests: tuple[str, ...]


@dataclass(slots=True)
class AndroidNativeDescriptors:
    operation_id: str
    context_digest: str
    request_digest: str
    scope_digest: str
    configuration_digest: str
    binding_digest: str
    ownership_generation: int
    host_incarnation: str = field(repr=False)
    helper_incarnation: str = field(repr=False)
    producer_fd: int = field(repr=False)
    operation_directory_fd: int = field(repr=False)
    device_fd: int = field(repr=False)
    device_directory_fd: int = field(repr=False)
    device_lock_name: str = field(repr=False)
    _operation: object = field(repr=False)
    _context: object = field(repr=False)
    _binding: object = field(repr=False)
    _phase: object = field(repr=False)
    _device: object = field(repr=False)
    _pid: int = field(repr=False)
    _thread: int = field(repr=False)
    _active: bool = field(default=True, repr=False)


@dataclass(slots=True)
class _AndroidRecoveryFiles:
    inspection: AndroidRecoveryInspection
    intent: dict = field(repr=False)
    native: dict | None = field(repr=False)
    producer_fd: int = field(repr=False)
    operation_fd: int = field(repr=False)


@dataclass(slots=True)
class AndroidRecoveryDescriptors:
    operation_id: str
    request_digest: str
    context_digest: str
    scope_digest: str
    configuration_digest: str
    binding_digest: str
    prior_generation: int
    prior_host_incarnation: str = field(repr=False)
    prior_helper_incarnation: str = field(repr=False)
    producer_fd: int = field(repr=False)
    operation_directory_fd: int = field(repr=False)
    device_fd: int = field(repr=False)
    device_directory_fd: int = field(repr=False)
    device_lock_name: str = field(repr=False)
    _files: _AndroidRecoveryFiles = field(repr=False)
    _device: object = field(repr=False)
    _lease: object = field(repr=False)
    _pid: int = field(repr=False)
    _thread: int = field(repr=False)
    _active: bool = field(default=True, repr=False)


class AndroidOperationStore:
    def __init__(self, run_store, config, private_root, *, create=True):
        _require(type(run_store) is RunStore
                 and type(config) is AndroidMobileAdapterConfig
                 and type(create) is bool,
                 "android_operation_configuration")
        try:
            config.validate()
        except Exception:
            raise AndroidOperationError(
                "android_operation_configuration") from None
        self.run_store = run_store
        self.config = config
        self.root = _private_directory(Path(private_root), create=create)
        self.operations = _private_directory(
            self.root / "operations", create=create)
        self._control_path = self.root / "control.lock"
        if create:
            try:
                descriptor = os.open(
                    self._control_path, os.O_RDWR | os.O_CREAT
                    | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                descriptor = os.open(
                    self._control_path, os.O_RDWR | os.O_NOFOLLOW)
            os.close(descriptor)
        else:
            descriptor = os.open(
                self._control_path, os.O_RDWR | os.O_NOFOLLOW
                | os.O_NONBLOCK)
            os.close(descriptor)
        self._issuer = object()
        self._mutex = threading.RLock()
        self._condition = threading.Condition(self._mutex)
        self._closed = False
        self._admissions = set()
        self._active = {}
        self._native = {}
        self._phases = {}
        self._callbacks = set()
        self._native_exports = {}
        self._native_controls = {}
        self._native_dispatchers = {}
        self._native_dispatch_threads = {}
        self._recovery_exports = {}
        self._recovery_dispatches = {}
        self._cleanup_exports = {}
        self._configuration = self._configuration_record()
        self.configuration_digest = contracts.digest(self._configuration)

    def _configuration_record(self):
        profile = self.config.original_profile.data
        registration = self.config.registration
        value = {
            "schemaVersion": 1,
            "scopeDigest": self.config.scope_digest,
            "projectDigest": registration.project_digest,
            "registrationDigest": contracts.digest({
                "project": registration.project,
                "collectionPolicy": registration.collection_policy}),
            "profileDigest": self.config.original_profile.digest,
            "applicationId": self.config.application_id,
            "package": self.config.package,
            "deviceId": self.config.device_id,
            "owner": self.config.owner,
            "runtimePolicyDigest": self.config.runtime_policy_digest,
            "originalDigest": profile["artifact"]["sha256"],
            "originalBytes": profile["artifact"]["bytes"],
            "helperDigest": self.config.helper_digest,
            "fixturePlansDigest": _fixture_digest(self.config.preparations),
            "toolsDigest": contracts.digest({
                "adb": self.config.tools.adb_digest,
                "packageInspector": self.config.tools.package_inspector_digest}),
            "runStoreRootDigest": contracts.digest(str(self.run_store.root)),
            "environmentDigest": self.run_store.environment_digest,
        }
        if self.config.adb_endpoint is not None:
            value['adbEndpointDigest']=self.config.adb_endpoint.definition_digest
        if self.config.native_guardian is not None:
            from .live.issue_sessions import FIXTURE_RESERVATION_VERSION
            value['nativeGuardianDigest']=self.config.native_guardian.definition_digest
            value['nativeToolOwnershipVersion']=NATIVE_TOOL_OWNERSHIP_VERSION
            value['fixtureReservationVersion']=FIXTURE_RESERVATION_VERSION
        if self.config.tools.inspector_support:
            value['packageInspectorSupportDigest']=self.config.tools.inspector_support_digest
        return value

    @contextmanager
    def _control(self):
        with self._mutex:
            descriptor = os.open(
                self._control_path, os.O_RDWR | os.O_NOFOLLOW
                | os.O_NONBLOCK)
            try:
                info = os.fstat(descriptor)
                _require(stat.S_ISREG(info.st_mode)
                         and info.st_uid == os.getuid()
                         and info.st_nlink == 1
                         and stat.S_IMODE(info.st_mode) == 0o600,
                         "android_operation_storage")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _root(self, operation_id):
        _require(type(operation_id) is str and _ID.fullmatch(operation_id),
                 "android_operation_context")
        return self.operations / operation_id

    def _intent(self, operation_id, operation_fd=None):
        if operation_fd is None:
            parent = _walk_directory(self._root(operation_id))
            try:
                value = _read_json_at(parent, "intent.json")
            finally:
                os.close(parent)
        else:
            value = _read_json_at(operation_fd, "intent.json")
        expected = {"schemaVersion", "operationId", "requestDigest",
                    "contextDigest", "context", "scopeDigest",
                    "configurationDigest", "configuration",
                    "runStoreRootDigest", "reservedBytes", "rootIdentity",
                    "stagingIdentity", "producerIdentity", "files"}
        if type(value) is dict and 'nativeCalls' in value:
            expected.add('nativeCalls')
        _require(type(value) is dict and set(value) == expected
                 and value["schemaVersion"] == 1
                 and value["operationId"] == operation_id
                 and all(type(value[name]) is str and _DIGEST.fullmatch(value[name])
                         for name in ("requestDigest", "contextDigest",
                                     "scopeDigest", "configurationDigest",
                                     "runStoreRootDigest"))
                 and type(value["context"]) is dict
                 and type(value["configuration"]) is dict
                 and type(value["reservedBytes"]) is int
                 and 1 <= value["reservedBytes"] <= 512 * 1024 ** 3
                 and _valid_identity(value["rootIdentity"], directory=True)
                 and _valid_identity(value["stagingIdentity"], directory=True)
                 and _valid_identity(value["producerIdentity"])
                 and type(value["files"]) is dict
                 and set(value["files"]) == set(_APK_NAMES),
                 "android_operation_record")
        context = value["context"]
        try:
            configuration_digest = contracts.digest(value["configuration"])
        except (contracts.ContractError, TypeError, ValueError):
            raise AndroidOperationError("android_operation_record") from None
        _require(set(context) == {
            "operation_id", "request_digest", "repair_plan_digest",
            "project_digest", "application_id", "source_digest",
            "artifact_digest", "scope_digest", "runtime_policy_digest"}
            and context["operation_id"] == operation_id
            and context["request_digest"] == value["requestDigest"]
            and context["scope_digest"] == value["scopeDigest"]
            and type(context["application_id"]) is str
            and _ID.fullmatch(context["application_id"])
            and all(type(context[name]) is str and _DIGEST.fullmatch(context[name])
                    for name in ("request_digest", "repair_plan_digest",
                                 "project_digest", "source_digest",
                                 "artifact_digest", "scope_digest",
                                 "runtime_policy_digest"))
            and configuration_digest == value["configurationDigest"],
            "android_operation_record")
        for item in value["files"].values():
            _require(type(item) is dict and set(item) == {
                "digest", "bytes", "identity"}
                and type(item["digest"]) is str and _DIGEST.fullmatch(item["digest"])
                and type(item["bytes"]) is int and 0 < item["bytes"] <= MAX_APK
                and _valid_identity(item["identity"]),
                "android_operation_record")
        from .android_native_calls import reservation
        native_bytes = reservation(value)
        _require(value["files"]["candidate.apk"]["digest"]
                 == context["artifact_digest"]
                 and value["files"]["original.apk"]["digest"]
                 == value["configuration"].get("originalDigest")
                 and value["files"]["original.apk"]["bytes"]
                 == value["configuration"].get("originalBytes")
                 and value["files"]["helper.apk"]["digest"]
                 == value["configuration"].get("helperDigest")
                 and value["reservedBytes"] == _MAX_METADATA_BYTES + native_bytes + sum(
                     item["bytes"] for item in value["files"].values()),
                 "android_operation_record")
        return value

    def _state(self, operation_id, operation_fd=None):
        if operation_fd is None:
            parent = _walk_directory(self._root(operation_id))
            try:
                value = _read_json_at(parent, "state.json")
            finally:
                os.close(parent)
        else:
            value = _read_json_at(operation_fd, "state.json")
        _require(type(value) is dict and set(value) == {
            "schemaVersion", "operationId", "requestDigest", "stage",
            "nativeBindingDigest", "phases"}
            and value["schemaVersion"] == 1
            and value["operationId"] == operation_id
            and type(value["requestDigest"]) is str
            and _DIGEST.fullmatch(value["requestDigest"])
            and value["stage"] in {
                "empty", "prepared", "discarding", "discarded"}
            and (value["nativeBindingDigest"] is None
                 or (type(value["nativeBindingDigest"]) is str
                     and _DIGEST.fullmatch(value["nativeBindingDigest"])))
            and type(value["phases"]) is dict
            and len(value["phases"]) <= _MAX_REPLAYS + 2,
            "android_operation_record")
        for key, item in value["phases"].items():
            _require(type(key) is str
                     and (key in {"install", "cleanup"}
                          or re.fullmatch(r"replay-[0-9]{3}", key))
                     and type(item) is dict and set(item) == {
                         "state", "recordDigest"}
                     and item["state"] in {"prepared", "returned", "completed"}
                     and type(item["recordDigest"]) is str
                     and _DIGEST.fullmatch(item["recordDigest"]),
                     "android_operation_record")
        return value

    def _replace_state(self, operation_id, state):
        directory = _walk_directory(self._root(operation_id))
        try:
            _replace_at(directory, "state.json", state)
        finally:
            os.close(directory)

    @staticmethod
    def _source(path, maximum):
        descriptor = parent = None
        try:
            path = Path(path)
            _require(path.is_absolute() and path.name not in {"", ".", ".."},
                     "android_operation_source")
            parent = _walk_source_directory(path.parent)
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent)
            info = os.fstat(descriptor)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                     and info.st_uid == os.getuid()
                     and bool(stat.S_IMODE(info.st_mode) & 0o400)
                     and not (stat.S_IMODE(info.st_mode) & 0o022)
                     and 0 < info.st_size <= maximum,
                     "android_operation_source")
            digest, size = _file_digest(descriptor, maximum)
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor, digest, size
        except (OSError, AndroidOperationError):
            if descriptor is not None:
                os.close(descriptor)
            raise AndroidOperationError("android_operation_source") from None
        finally:
            if parent is not None:
                os.close(parent)

    def _write_staged(self, operation_root, name, source, intent):
        operation_fd = _walk_directory(operation_root)
        staging_fd = None
        destination = None
        try:
            staging_fd = _open_child_directory(
                operation_fd, "staging",
                expected=intent["stagingIdentity"])
            destination = _open_regular_at(
                staging_fd, name, expected=intent["files"][name]["identity"],
                writable=True)
            _require(os.fstat(destination).st_size == 0,
                     "android_operation_stage")
            if type(source) is bytes:
                chunks = (source,)
            else:
                os.lseek(source, 0, os.SEEK_SET)
                chunks = iter(lambda: os.read(source, 1024 * 1024), b"")
            total = 0; digest = hashlib.sha256()
            for chunk in chunks:
                total += len(chunk)
                _require(total <= intent["files"][name]["bytes"],
                         "android_operation_stage")
                offset = 0
                while offset < len(chunk):
                    count = os.write(destination, chunk[offset:])
                    _require(count > 0, "android_operation_stage")
                    digest.update(chunk[offset:offset + count])
                    offset += count
            os.fsync(destination)
            _require(total == intent["files"][name]["bytes"]
                     and digest.hexdigest() == intent["files"][name]["digest"],
                     "android_operation_stage")
            os.fsync(staging_fd)
        finally:
            if destination is not None:
                os.close(destination)
            if staging_fd is not None:
                os.close(staging_fd)
            os.close(operation_fd)

    @contextmanager
    def _admission(self):
        current = threading.current_thread()
        with self._control():
            _require(not self._closed, "android_operation_closed")
            self._admissions.add(current)
        try:
            yield
        finally:
            with self._condition:
                self._admissions.discard(current)
                self._condition.notify_all()

    @contextmanager
    def admit(self, context, artifacts):
        with self._admission():
            with self._admit(context, artifacts) as operation:
                yield operation

    @contextmanager
    def _admit(self, context, artifacts):
        _validate_context(context)
        _require(type(artifacts) is BlobSet and len(artifacts.entries) == 1
                 and artifacts.entries[0][0] == "candidate.apk",
                 "android_operation_artifact")
        candidate = artifacts.entries[0][1]
        _require(0 < len(candidate) <= MAX_APK
                 and hashlib.sha256(candidate).hexdigest()
                 == context.artifact_digest,
                 "android_operation_artifact")
        try:
            self.config.validate(); self.config.tools.verify()
            _require(self._configuration_record() == self._configuration,
                     'android_operation_configuration')
        except Exception:
            raise AndroidOperationError(
                "android_operation_configuration") from None
        _require(context.project_digest == self.config.registration.project_digest
                 and context.application_id == self.config.application_id
                 and context.scope_digest == self.config.scope_digest
                 and context.runtime_policy_digest
                 == self.config.runtime_policy_digest,
                 "android_operation_binding")
        original_fd = helper_fd = None
        try:
            original_fd, original_digest, original_bytes = self._source(
                self.config.original_apk, MAX_APK)
            helper_fd, helper_digest, helper_bytes = self._source(
                self.config.helper_apk, MAX_APK)
            profile_artifact = self.config.original_profile.data["artifact"]
            _require((original_digest, original_bytes) == (
                profile_artifact["sha256"], profile_artifact["bytes"])
                and helper_digest == self.config.helper_digest,
                "android_operation_binding")
            file_records = {
                "candidate.apk": {"digest": context.artifact_digest,
                                  "bytes": len(candidate)},
                "original.apk": {"digest": original_digest,
                                 "bytes": original_bytes},
                "helper.apk": {"digest": helper_digest,
                               "bytes": helper_bytes},
            }
            from .android_native_calls import RESERVED_BYTES, initialize
            reserved = sum(item["bytes"] for item in file_records.values()) \
                + _MAX_METADATA_BYTES + (RESERVED_BYTES if self.config.adb_endpoint is not None else 0)
            operation_id = context.operation_id
            with self.run_store.repair_scope_lease(
                    "mobile-device", self.config.scope_digest):
                self.run_store.require_available()
                with self._control():
                    _require(not self._closed
                             and operation_id not in self._active
                             and not self._root(operation_id).exists(),
                             "android_operation_admission")
                    operation_root = self._root(operation_id)
                    operation_root.mkdir(mode=0o700)
                    (operation_root / "staging").mkdir(mode=0o700)
                    (operation_root / "phases").mkdir(mode=0o700)
                    for name in ("producer.lock", *_APK_NAMES):
                        parent = (operation_root if name == "producer.lock"
                                  else operation_root / "staging")
                        descriptor = os.open(
                            parent / name, os.O_RDWR | os.O_CREAT | os.O_EXCL
                            | os.O_NOFOLLOW, 0o600)
                        os.fsync(descriptor); os.close(descriptor)
                    for directory in (operation_root / "staging",
                                      operation_root / "phases",
                                      operation_root):
                        descriptor = os.open(
                            directory, os.O_RDONLY | os.O_DIRECTORY
                            | os.O_NOFOLLOW)
                        os.fsync(descriptor); os.close(descriptor)
                    for name in _APK_NAMES:
                        file_records[name]["identity"] = _identity_info(
                            (operation_root / "staging" / name).lstat())
                    intent = {
                        "schemaVersion": 1, "operationId": operation_id,
                        "requestDigest": context.request_digest,
                        "contextDigest": context.digest,
                        "context": _context_record(context),
                        "scopeDigest": self.config.scope_digest,
                        "configurationDigest": self.configuration_digest,
                        "configuration": self._configuration,
                        "runStoreRootDigest": contracts.digest(
                            str(self.run_store.root)),
                        "reservedBytes": reserved,
                        "rootIdentity": _identity_info(operation_root.lstat()),
                        "stagingIdentity": _identity_info(
                            (operation_root / "staging").lstat()),
                        "producerIdentity": _identity_info(
                            (operation_root / "producer.lock").lstat()),
                        "files": file_records,
                    }
                    operation_fd = _walk_directory(operation_root)
                    try:
                        if self.config.adb_endpoint is not None:
                            intent['nativeCalls'] = initialize(operation_fd, context)
                        _write_new_at(operation_fd, "intent.json", intent)
                        _write_new_at(operation_fd, "state.json", {
                            "schemaVersion": 1, "operationId": operation_id,
                            "requestDigest": context.request_digest,
                            "stage": "empty", "nativeBindingDigest": None,
                            "phases": {}})
                    finally:
                        os.close(operation_fd)
                with self.run_store.admit(
                        operation_id, context.request_digest,
                        disk_bytes=reserved) as run:
                    self._write_staged(
                        operation_root, "candidate.apk", candidate, intent)
                    self._write_staged(
                        operation_root, "original.apk", original_fd, intent)
                    self._write_staged(
                        operation_root, "helper.apk", helper_fd, intent)
                    os.close(original_fd); original_fd = None
                    os.close(helper_fd); helper_fd = None
                    operation_fd = _walk_directory(operation_root)
                    try:
                        _write_new_at(operation_fd, "stage.json", {
                            "schemaVersion": 1,
                            "operationId": operation_id,
                            "contextDigest": context.digest,
                            "files": intent["files"],
                            "stageDigest": contracts.digest(intent["files"]),
                        })
                        state = self._state(operation_id, operation_fd)
                        state["stage"] = "prepared"
                        _replace_at(operation_fd, "state.json", state)
                    finally:
                        os.close(operation_fd)
                    operation = AndroidOperation(
                        self, run, operation_id, context.request_digest,
                        context.digest, reserved, operation_root / "staging",
                        self._issuer, os.getpid())
                    with self._control():
                        _require(not self._closed,
                                 "android_operation_admission")
                        self._active[operation_id] = operation
                    try:
                        yield operation
                    finally:
                        operation._active = False
                        with self._control():
                            native = self._native.pop(operation_id, None)
                            if native is not None:
                                native._active = False
                            if self._active.get(operation_id) is operation:
                                self._active.pop(operation_id, None)
        finally:
            for descriptor in (original_fd, helper_fd):
                if descriptor is not None:
                    os.close(descriptor)

    def require_operation(self, operation, context=None):
        with self._mutex:
            live = self._active.get(getattr(operation, "operation_id", None))
        _require(type(operation) is AndroidOperation
                 and operation.store is self
                 and operation._issuer is self._issuer
                 and operation._pid == os.getpid()
                 and operation._active and live is operation
                 and not self._closed
                 and self._configuration_record() == self._configuration,
                 "android_operation_capability")
        if context is not None:
            _validate_context(context)
            _require(context.digest == operation.context_digest,
                     "android_operation_binding")
        intent = self._intent(operation.operation_id)
        state = self._state(operation.operation_id)
        _require(intent["requestDigest"] == operation.request_digest
                 and intent["contextDigest"] == operation.context_digest
                 and intent["configurationDigest"]
                 == self.configuration_digest
                 and intent["configuration"] == self._configuration
                 and intent["scopeDigest"] == self.config.scope_digest
                 and intent["runStoreRootDigest"]
                 == contracts.digest(str(self.run_store.root))
                 and intent["reservedBytes"] == operation.reserved_bytes
                 and state["requestDigest"] == operation.request_digest
                 and state["stage"] == "prepared"
                 and (context is None
                      or (intent["context"] == _context_record(context)
                          and intent["contextDigest"] == context.digest)),
                 "android_operation_binding")
        row = self.run_store.status(operation.operation_id)
        _require(row["requestDigest"] == operation.request_digest
                 and row["state"] == "admitted",
                 "android_operation_binding")
        return operation

    def _producer_probe(self, intent):
        operation_fd = _walk_directory(self._root(intent["operationId"]))
        try:
            return _open_regular_at(
                operation_fd, "producer.lock",
                expected=intent["producerIdentity"], writable=True)
        finally:
            os.close(operation_fd)

    def _producer(self, intent, *, nonblocking=True):
        descriptor = self._producer_probe(intent)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX
                        | (fcntl.LOCK_NB if nonblocking else 0))
            return descriptor
        except OSError:
            os.close(descriptor)
            raise AndroidOperationError("android_operation_producer_live") from None

    def bind_native(self, operation, context, *, ownership_generation,
                    host_incarnation, helper_incarnation,
                    provider_incarnation):
        self.require_operation(operation, context)
        _require(type(ownership_generation) is int
                 and 1 <= ownership_generation < 2 ** 53,
                 "android_operation_native_binding")
        try:
            for value in (host_incarnation, helper_incarnation,
                          provider_incarnation):
                contracts.validate_id(value)
        except contracts.ContractError:
            raise AndroidOperationError(
                "android_operation_native_binding") from None
        intent = self._intent(operation.operation_id)
        producer = self._producer(intent)
        try:
            self.require_operation(operation, context)
            state = self._state(operation.operation_id)
            _require(state["stage"] == "prepared"
                     and state["nativeBindingDigest"] is None
                     and operation.operation_id not in self._native,
                     "android_operation_native_binding")
            operation_fd = _walk_directory(self._root(operation.operation_id))
            try:
                _require(self._stage_status(
                    operation_fd, intent, state, full=True) == "prepared",
                    "android_operation_stage")
            finally:
                os.close(operation_fd)
            value = {
                "schemaVersion": 1,
                "operationId": operation.operation_id,
                "requestDigest": operation.request_digest,
                "contextDigest": context.digest,
                "scopeDigest": self.config.scope_digest,
                "configurationDigest": self.configuration_digest,
                "ownershipGeneration": ownership_generation,
                "hostIncarnation": host_incarnation,
                "helperIncarnation": helper_incarnation,
                "providerIncarnation": provider_incarnation,
            }
            binding_digest = contracts.digest(value)
            value["bindingDigest"] = binding_digest
            operation_fd = _walk_directory(self._root(operation.operation_id))
            try:
                _write_new_at(operation_fd, "native.json", value)
                state["nativeBindingDigest"] = binding_digest
                _replace_at(operation_fd, "state.json", state)
            finally:
                os.close(operation_fd)
            binding = AndroidNativeBinding(
                operation.operation_id, context.digest, binding_digest,
                ownership_generation, host_incarnation,
                helper_incarnation, provider_incarnation,
                self._issuer, os.getpid())
            with self._mutex:
                self._native[operation.operation_id] = binding
            return binding
        finally:
            fcntl.flock(producer, fcntl.LOCK_UN); os.close(producer)

    def _require_native(self, operation, context, binding):
        self.require_operation(operation, context)
        with self._mutex:
            live = self._native.get(operation.operation_id)
        _require(type(binding) is AndroidNativeBinding
                 and binding._issuer is self._issuer
                 and binding._pid == os.getpid()
                 and binding._active and live is binding
                 and binding.context_digest == context.digest,
                 "android_operation_native_binding")
        state = self._state(operation.operation_id)
        _require(state["nativeBindingDigest"] == binding.binding_digest,
                 "android_operation_native_binding")
        return binding

    @staticmethod
    def _phase_key(phase, replay_number):
        _require(phase in {"install", "replay", "cleanup"},
                 "android_operation_phase")
        if phase == "replay":
            _require(type(replay_number) is int
                     and 1 <= replay_number <= _MAX_REPLAYS,
                     "android_operation_phase")
            return f"replay-{replay_number:03d}"
        _require(replay_number is None, "android_operation_phase")
        return phase

    @contextmanager
    def phase(self, operation, context, native_binding, phase,
              replay_number=None):
        key = self._phase_key(phase, replay_number)
        callback = threading.current_thread()
        with self._control():
            _require(not self._closed, "android_operation_closed")
            self._callbacks.add(callback)
        producer = None; capability = None
        try:
            if native_binding is None:
                _require(phase == 'cleanup', 'android_operation_native_binding')
                self.require_operation(operation, context)
            else:
                self._require_native(operation, context, native_binding)
            intent = self._intent(operation.operation_id)
            producer = self._producer(intent)
            if native_binding is None:
                self.require_operation(operation, context)
            else:
                self._require_native(operation, context, native_binding)
            state = self._state(operation.operation_id)
            operation_fd = _walk_directory(self._root(operation.operation_id))
            try:
                _require(self._stage_status(
                    operation_fd, intent, state, full=True) == "prepared",
                    "android_operation_stage")
                native_record = self._validate_native(operation_fd, intent, state)
                if native_binding is None:
                    _require(native_record is None and not state['phases'],
                             'android_operation_native_binding')
            finally:
                os.close(operation_fd)
            phases = state["phases"]
            _require(key not in phases and "cleanup" not in phases,
                     "android_operation_phase")
            if phase == "install":
                _require(not phases, "android_operation_phase")
            elif phase == "replay":
                _require(phases.get("install", {}).get("state") == "completed"
                         and all(item.get("state") == "completed"
                                 for name, item in phases.items()
                                 if name.startswith("replay-"))
                         and replay_number == 1 + len([
                             item for item in phases if item.startswith("replay-")]),
                         "android_operation_phase")
            # Cleanup can follow cancellation before dispatch or a bound
            # native owner whose install phase could not start.
            record = {
                "schemaVersion": 1, "operationId": operation.operation_id,
                "requestDigest": operation.request_digest,
                "contextDigest": context.digest,
                "phase": phase, "replayNumber": replay_number,
                "nativeBindingDigest": (None if native_binding is None
                                        else native_binding.binding_digest),
                "configurationDigest": self.configuration_digest,
                "state": "prepared", "resultDigest": None,
            }
            phases_fd = _walk_directory(
                self._root(operation.operation_id) / "phases")
            try:
                _write_new_at(phases_fd, key + ".json", record)
            finally:
                os.close(phases_fd)
            phases[key] = {"state": "prepared",
                           "recordDigest": contracts.digest(record)}
            self._replace_state(operation.operation_id, state)
            capability = AndroidPhaseCapability(
                operation.operation_id, context.digest, phase,
                replay_number, record['nativeBindingDigest'],
                self._issuer, os.getpid(), threading.get_ident(), producer)
            with self._mutex:
                _require(operation.operation_id not in self._phases,
                         "android_operation_phase")
                self._phases[operation.operation_id] = capability
            try:
                yield capability
            finally:
                if not capability._completed:
                    record["state"] = "returned"
                    phases_fd = _walk_directory(
                        self._root(operation.operation_id) / "phases")
                    try:
                        _replace_at(phases_fd, key + ".json", record)
                    finally:
                        os.close(phases_fd)
                    state = self._state(operation.operation_id)
                    state["phases"][key] = {
                        "state": "returned",
                        "recordDigest": contracts.digest(record)}
                    self._replace_state(operation.operation_id, state)
        finally:
            if capability is not None:
                capability._active = False
                with self._mutex:
                    if self._phases.get(operation.operation_id) is capability:
                        self._phases.pop(operation.operation_id, None)
            if producer is not None:
                # Native children may share this exact open file description.
                # Closing the parent copy must not unlock their retained copy.
                os.close(producer)
            with self._mutex:
                self._callbacks.discard(callback)
                self._condition.notify_all()

    def require_phase(self, capability):
        return self._require_phase(capability, owner_thread=True)

    def _require_phase(self, capability, *, owner_thread):
        with self._mutex:
            live = self._phases.get(getattr(capability, "operation_id", None))
        _require(type(capability) is AndroidPhaseCapability
                 and live is capability
                 and capability._issuer is self._issuer
                 and capability._pid == os.getpid()
                 and (not owner_thread or capability._thread == threading.get_ident())
                 and capability._active and not capability._completed,
                 "android_operation_capability")
        intent = self._intent(capability.operation_id)
        actual = os.fstat(capability._producer_fd)
        _require(_same_identity(actual, intent["producerIdentity"]),
                 "android_operation_capability")
        probe = self._producer_probe(intent)
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(probe, fcntl.LOCK_UN)
                raise AndroidOperationError(
                    "android_operation_capability")
        finally:
            os.close(probe)
        return capability

    @contextmanager
    def borrow_native_descriptors(self, operation, context, binding, phase, device):
        """Export original held descriptors only within the exact live phase."""
        from .live.authority import DeviceAuthority
        self._require_native(operation, context, binding)
        self.require_phase(phase)
        _require(phase.operation_id == operation.operation_id
            and phase.native_binding_digest == binding.binding_digest
            and type(device) is DeviceAuthority and device.device_kind == 'android'
            and device._device_fingerprint == self.config.scope_digest
            and device.generation == binding.ownership_generation
            and device._authority.host_incarnation == binding.host_incarnation
            and device.helper_incarnation == binding.helper_incarnation,
            'android_operation_native_binding')
        borrowed = None
        with ExitStack() as stack:
            try:
                device_fd, device_directory, name = stack.enter_context(device.borrow_native_lease())
                directory = _walk_directory(self._root(operation.operation_id))
                stack.callback(os.close, directory)
                intent = self._intent(operation.operation_id, directory)
                _require(_same_identity(os.fstat(directory), intent['rootIdentity'], directory=True),
                         'android_operation_native_binding')
                producer = os.dup(phase._producer_fd); stack.callback(os.close, producer)
                borrowed = AndroidNativeDescriptors(operation.operation_id, context.digest, operation.request_digest,
                    self.config.scope_digest, self.configuration_digest, binding.binding_digest,
                    binding.ownership_generation, binding.host_incarnation, binding.helper_incarnation,
                    producer, directory, device_fd, device_directory, name, operation, context, binding,
                    phase, device, os.getpid(), threading.get_ident())
                with self._mutex:
                    _require(not self._closed, 'android_operation_closed')
                    self._native_exports[id(borrowed)] = borrowed
                self.require_native_descriptors(borrowed)
                yield borrowed
            finally:
                if borrowed is not None:
                    borrowed._active = False
                    with self._mutex:self._native_exports.pop(id(borrowed), None)

    def require_native_descriptors(self, borrowed):
        from .android_recovery import AndroidRecoveryDispatch, require_dispatch
        if type(borrowed) is AndroidRecoveryDispatch:
            return require_dispatch(self,borrowed)
        with self._mutex:
            forwarded = self._native_dispatch_threads.get((id(borrowed), threading.get_ident()), 0) > 0
            _require(type(borrowed) is AndroidNativeDescriptors
                and self._native_exports.get(id(borrowed)) is borrowed and borrowed._active
                and borrowed._pid == os.getpid() and (borrowed._thread == threading.get_ident() or forwarded)
                and not self._closed, 'android_operation_native_binding')
        self._require_native(borrowed._operation, borrowed._context, borrowed._binding)
        self._require_phase(borrowed._phase, owner_thread=not forwarded)
        device = borrowed._device; device._require_open()
        _require(device.generation == borrowed.ownership_generation
            and device._authority.host_incarnation == borrowed.host_incarnation
            and device.helper_incarnation == borrowed.helper_incarnation,
            'android_operation_native_binding')
        for descriptor, original in ((borrowed.producer_fd, borrowed._phase._producer_fd),
                                     (borrowed.device_fd, device._lease.file.fileno())):
            opened, expected = os.fstat(descriptor), os.fstat(original)
            _require((opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino),
                     'android_operation_native_binding')
        return borrowed

    def complete_phase(self, capability, result_digest):
        self.require_phase(capability)
        with self._mutex:
            _require(not any(call._descriptors._phase is capability for call in self._native_controls.values()),
                     'android_native_owner_uncollected')
            _require(not any(self._native_exports.get(key[0]) is not None
                and self._native_exports[key[0]]._phase is capability
                for key in self._native_dispatch_threads), 'android_native_owner_uncollected')
        _require(type(result_digest) is str and _DIGEST.fullmatch(result_digest),
                 "android_operation_phase")
        key = self._phase_key(capability.phase, capability.replay_number)
        phases_fd = _walk_directory(
            self._root(capability.operation_id) / "phases")
        try:
            record = _read_json_at(phases_fd, key + ".json")
            _require(record["state"] == "prepared"
                     and record["nativeBindingDigest"]
                     == capability.native_binding_digest,
                     "android_operation_phase")
            record["state"] = "completed"
            record["resultDigest"] = result_digest
            _replace_at(phases_fd, key + ".json", record)
        finally:
            os.close(phases_fd)
        state = self._state(capability.operation_id)
        state["phases"][key] = {
            "state": "completed", "recordDigest": contracts.digest(record)}
        self._replace_state(capability.operation_id, state)
        capability._completed = True
        return result_digest

    @staticmethod
    def _remove_staged(staging_fd, name):
        os.unlink(name, dir_fd=staging_fd)

    def discard_staged(self, operation, cleanup_phase_capability):
        self.require_operation(operation)
        capability = self.require_phase(cleanup_phase_capability)
        _require(capability.operation_id == operation.operation_id
                 and capability.context_digest == operation.context_digest
                 and capability.phase == "cleanup"
                 and capability.replay_number is None,
                 "android_operation_capability")
        operation_fd = _walk_directory(self._root(operation.operation_id))
        staging_fd = None
        try:
            intent = self._intent(operation.operation_id, operation_fd)
            state = self._state(operation.operation_id, operation_fd)
            from .android_native_calls import validate_workspace
            _require(validate_workspace(operation_fd, intent) == 'idle', 'android_native_call_unresolved')
            _require(self._stage_status(
                operation_fd, intent, state, full=True) == "prepared",
                "android_operation_stage")
            staging_fd = _open_child_directory(
                operation_fd, "staging",
                expected=intent["stagingIdentity"])
            discard = {
                "schemaVersion": 1,
                "operationId": operation.operation_id,
                "contextDigest": operation.context_digest,
                "configurationDigest": self.configuration_digest,
                "files": intent["files"],
                "state": "discarding",
            }
            _write_new_at(operation_fd, "discard.json", discard)
            state["stage"] = "discarding"
            _replace_at(operation_fd, "state.json", state)
            # Every file was validated before the durable discarding intent.
            # Recheck the exact opened inode immediately before each unlinkat.
            for name in _APK_NAMES:
                descriptor = _open_regular_at(
                    staging_fd, name,
                    expected=intent["files"][name]["identity"])
                try:
                    digest, size = _file_digest(descriptor, MAX_APK)
                    _require((digest, size) == (
                        intent["files"][name]["digest"],
                        intent["files"][name]["bytes"]),
                        "android_operation_stage")
                finally:
                    os.close(descriptor)
                self._remove_staged(staging_fd, name)
            os.fsync(staging_fd)
            discard["state"] = "discarded"
            _replace_at(operation_fd, "discard.json", discard)
            state["stage"] = "discarded"
            _replace_at(operation_fd, "state.json", state)
            return contracts.digest(discard)
        finally:
            if staging_fd is not None:
                os.close(staging_fd)
            os.close(operation_fd)

    def _stage_status(self, operation_fd, intent, state, *, full):
        staging_fd = _open_child_directory(
            operation_fd, "staging", expected=intent["stagingIdentity"])
        try:
            names = set(os.listdir(staging_fd))
            stage_state = state["stage"]
            from .android_recovery_materials import validate_materials
            materials = validate_materials(operation_fd, intent, state, full=full)
            recreated = set(materials['files']) if materials is not None else set()
            discard = None
            if "discard.json" in os.listdir(operation_fd):
                discard = _read_json_at(operation_fd, "discard.json")
                _require(type(discard) is dict and set(discard) == {
                    "schemaVersion", "operationId", "contextDigest",
                    "configurationDigest", "files", "state"}
                    and discard["schemaVersion"] == 1
                    and discard["operationId"] == intent["operationId"]
                    and discard["contextDigest"] == intent["contextDigest"]
                    and discard["configurationDigest"]
                    == intent["configurationDigest"]
                    and discard["files"] == intent["files"]
                    and discard["state"] in {"discarding", "discarded"},
                    "android_operation_stage")
            if stage_state in {"empty", "prepared"} and discard is None:
                _require(names == set(_APK_NAMES),
                         "android_operation_stage")
            else:
                _require(discard is not None and names <= set(_APK_NAMES),
                         "android_operation_stage")
            for name in names - recreated:
                descriptor = _open_regular_at(
                    staging_fd, name,
                    expected=intent["files"][name]["identity"])
                try:
                    info = os.fstat(descriptor)
                    expected = intent["files"][name]
                    _require(0 <= info.st_size <= expected["bytes"],
                             "android_operation_stage")
                    if (stage_state != "empty" or discard is not None
                            or full and info.st_size == expected["bytes"]):
                        digest, size = _file_digest(descriptor, MAX_APK)
                        _require((digest, size) == (
                            expected["digest"], expected["bytes"]),
                            "android_operation_stage")
                    if stage_state != "empty" or discard is not None:
                        _require(info.st_size == expected["bytes"],
                                 "android_operation_stage")
                finally:
                    os.close(descriptor)
            if stage_state != "empty":
                stage = _read_json_at(operation_fd, "stage.json")
                _require(type(stage) is dict and set(stage) == {
                    "schemaVersion", "operationId", "contextDigest",
                    "files", "stageDigest"}
                    and stage["schemaVersion"] == 1
                    and stage["operationId"] == intent["operationId"]
                    and stage["contextDigest"] == intent["contextDigest"]
                    and stage["files"] == intent["files"]
                    and stage["stageDigest"]
                    == contracts.digest(intent["files"]),
                    "android_operation_stage")
            if materials is not None and materials['state'] in ('preparing', 'prepared'):
                return 'recovery-copy-incomplete' if materials['state']=='preparing' else 'recovery-apks-prepared'
            if discard is None:
                return "prepared" if stage_state == "prepared" else "staging-incomplete"
            if stage_state == "prepared":
                return "discard-intent-uncommitted"
            if stage_state == "discarding":
                return ("discard-state-uncommitted"
                        if discard["state"] == "discarded"
                        else "staged-discard-incomplete")
            _require(stage_state == "discarded"
                     and discard["state"] == "discarded" and not names,
                     "android_operation_stage")
            return "staged-discarded"
        finally:
            os.close(staging_fd)

    def status(self, operation_id):
        root = self._root(operation_id)
        if not root.exists():
            return {"schemaVersion": 1, "operationId": operation_id,
                    "state": "no-intent"}
        operation_fd = None; producer = None
        try:
            operation_fd = _walk_directory(root)
            intent = self._intent(operation_id, operation_fd)
            state = self._state(operation_id, operation_fd)
            try:
                row = self.run_store.status(operation_id)
            except RunDenied:
                return {"schemaVersion": 1, "operationId": operation_id,
                        "state": "intent-orphan"}
            _require(intent['configurationDigest'] == self.configuration_digest
                     and intent['configuration'] == self._configuration
                     and self._configuration_record() == self._configuration
                     and intent['scopeDigest'] == self.config.scope_digest
                     and intent['runStoreRootDigest'] == contracts.digest(str(self.run_store.root))
                     and intent['requestDigest'] == state['requestDigest'] == row['requestDigest']
                     and (row['state'] in TERMINAL or row['reservedBytes'] == intent['reservedBytes'])
                     and _same_identity(os.fstat(operation_fd), intent['rootIdentity'], directory=True),
                     'android_operation_binding')
            producer = _open_regular_at(
                operation_fd, "producer.lock",
                expected=intent["producerIdentity"], writable=True)
            try:
                fcntl.flock(producer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                selected = "producer-live"
            else:
                stage_status = self._stage_status(
                    operation_fd, intent, state, full=False)
                native = self._validate_native(operation_fd, intent, state)
                from .android_recovery import validate_record
                recovery = validate_record(operation_fd,intent,native)
                from .android_recovery_finalization import validate_finalization
                finalization = validate_finalization(operation_fd, intent, native)
                _, phase_state = self._phase_records(operation_fd, intent, state)
                from .android_native_calls import validate_workspace
                native_state = validate_workspace(operation_fd, intent)
                selected = (native_state if native_state != 'idle' else
                            'finalization-'+finalization['state'] if finalization is not None else
                            'recovery-'+recovery['state'] if recovery is not None else
                            "terminal" if row["state"] in TERMINAL else phase_state
                            if phase_state is not None
                            else "native-recovery-required"
                            if stage_status == "prepared" else stage_status)
            return {"schemaVersion": 1, "operationId": operation_id,
                    "requestDigest": intent["requestDigest"],
                    "contextDigest": intent["contextDigest"],
                    "configurationDigest": intent["configurationDigest"],
                    "state": selected}
        except (OSError, AndroidOperationError):
            return {"schemaVersion": 1, "operationId": operation_id,
                    "state": "record-invalid"}
        finally:
            if producer is not None:
                os.close(producer)
            if operation_fd is not None:
                os.close(operation_fd)

    def _validate_native(self, operation_fd, intent, state):
        digest = state["nativeBindingDigest"]
        has_native = "native.json" in os.listdir(operation_fd)
        if digest is None and not has_native:
            return None
        _require(has_native and (digest is not None or not state["phases"]),
                 "android_operation_native_binding")
        value = _read_json_at(operation_fd, "native.json")
        _require(type(value) is dict and set(value) == {
            "schemaVersion", "operationId", "requestDigest", "contextDigest",
            "scopeDigest", "configurationDigest", "ownershipGeneration",
            "hostIncarnation", "helperIncarnation", "providerIncarnation",
            "bindingDigest"}
            and value["schemaVersion"] == 1
            and value["operationId"] == intent["operationId"]
            and value["requestDigest"] == intent["requestDigest"]
            and value["contextDigest"] == intent["contextDigest"]
            and value["scopeDigest"] == intent["scopeDigest"]
            and value["configurationDigest"] == intent["configurationDigest"]
            and type(value["ownershipGeneration"]) is int
            and 1 <= value["ownershipGeneration"] < 2 ** 53,
            "android_operation_native_binding")
        measured = dict(value); measured.pop("bindingDigest")
        measured_digest = contracts.digest(measured)
        _require(measured_digest == value["bindingDigest"]
                 and (digest is None or digest == measured_digest),
                 "android_operation_native_binding")
        try:
            for name in ("hostIncarnation", "helperIncarnation",
                         "providerIncarnation"):
                contracts.validate_id(value[name])
        except contracts.ContractError:
            raise AndroidOperationError(
                "android_operation_native_binding") from None
        return value

    def _phase_records(self, operation_fd, intent, state):
        phases_fd = _open_child_directory(operation_fd, "phases")
        digests = []
        try:
            actual_names = set(os.listdir(phases_fd))
            expected_names = {key + ".json" for key in state["phases"]}
            _require(expected_names <= actual_names
                     and len(actual_names - expected_names) <= 1
                     and len(actual_names) <= _MAX_REPLAYS + 2,
                     "android_operation_phase")
            intermediate = None
            for name in sorted(actual_names):
                _require(name.endswith(".json"), "android_operation_phase")
                key = name[:-5]
                summary = state["phases"].get(key)
                record = _read_json_at(phases_fd, name)
                _require(type(record) is dict and set(record) == {
                    "schemaVersion", "operationId", "requestDigest",
                    "contextDigest", "phase", "replayNumber",
                    "nativeBindingDigest", "configurationDigest", "state",
                    "resultDigest"}
                    and record["schemaVersion"] == 1
                    and record["operationId"] == intent["operationId"]
                    and record["requestDigest"] == intent["requestDigest"]
                    and record["contextDigest"] == intent["contextDigest"]
                    and record["configurationDigest"]
                    == intent["configurationDigest"]
                    and record["nativeBindingDigest"]
                    == state["nativeBindingDigest"]
                    and ((record["state"] == "completed"
                          and type(record["resultDigest"]) is str
                          and _DIGEST.fullmatch(record["resultDigest"]))
                         or (record["state"] != "completed"
                             and record["resultDigest"] is None)),
                    "android_operation_phase")
                _require(self._phase_key(
                    record["phase"], record["replayNumber"]) == key,
                    "android_operation_phase")
                record_digest = contracts.digest(record)
                if summary is None:
                    _require(record["state"] == "prepared"
                             and intermediate is None,
                             "android_operation_phase")
                    intermediate = "phase-intent-uncommitted"
                elif (summary["state"] == record["state"]
                      and summary["recordDigest"] == record_digest):
                    pass
                else:
                    prior = dict(record)
                    prior["state"] = "prepared"
                    prior["resultDigest"] = None
                    _require(summary["state"] == "prepared"
                             and summary["recordDigest"]
                             == contracts.digest(prior)
                             and record["state"] in {"returned", "completed"}
                             and intermediate is None,
                             "android_operation_phase")
                    intermediate = (
                        "phase-completion-uncommitted"
                        if record["state"] == "completed"
                        else "phase-return-uncommitted")
                digests.append(record_digest)
            return tuple(sorted(digests)), intermediate
        finally:
            os.close(phases_fd)

    @contextmanager
    def _recovery_scope(self):
        try:
            with self.run_store.repair_scope_lease(
                    "mobile-device", self.config.scope_digest):
                yield
        except RunDenied:
            raise AndroidOperationError(
                "android_operation_recovery_unavailable") from None

    @contextmanager
    def recovery(self, operation_id, request_digest):
        with self._admission(), self._recovery_files(operation_id, request_digest) as files:
            yield files.inspection

    @contextmanager
    def _recovery_files(self, operation_id, request_digest, *, allow_finished=False, retire_metadata=False):
        operation_root = self._root(operation_id)
        lock_fds = []; directory_fds = []
        with self._recovery_scope():
            try:
                run_root = _walk_directory(self.run_store.root)
                directory_fds.append(run_root)
                vm_lock = _open_regular_at(run_root, ".vm-lock", writable=True)
                lock_fds.append(vm_lock)
                fcntl.flock(vm_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                private = _walk_directory(self.root)
                directory_fds.append(private)
                operations = _open_child_directory(private, "operations")
                directory_fds.append(operations)
                operation_fd = _open_child_directory(operations, operation_id)
                directory_fds.append(operation_fd)
                intent = self._intent(operation_id, operation_fd)
                state = self._state(operation_id, operation_fd)
                _require(intent["requestDigest"] == request_digest
                         and state['requestDigest'] == request_digest
                         and intent["scopeDigest"] == self.config.scope_digest
                         and intent["configurationDigest"]
                         == self.configuration_digest
                         and intent["configuration"] == self._configuration
                         and self._configuration_record() == self._configuration
                         and intent["runStoreRootDigest"]
                         == contracts.digest(str(self.run_store.root))
                         and _same_identity(
                             os.fstat(operation_fd), intent["rootIdentity"],
                             directory=True),
                         "android_operation_binding")
                row = self.run_store.status(operation_id)
                unfinished = (row["reservedBytes"] == intent["reservedBytes"]
                              and row["state"] in {"admitted", "quarantined"})
                finished = (allow_finished and row["reservedBytes"] == 0
                            and row["state"] in {"failed", "cancelled"})
                _require(row["requestDigest"] == request_digest and (unfinished or finished),
                         "android_operation_binding")
                producer = _open_regular_at(
                    operation_fd, "producer.lock",
                    expected=intent["producerIdentity"], writable=True)
                lock_fds.append(producer)
                fcntl.flock(producer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if retire_metadata:
                    _retire_record_temps(operation_fd)
                    phases = _open_child_directory(operation_fd, 'phases')
                    try:
                        _retire_record_temps(phases)
                    finally:
                        os.close(phases)
                    if 'nativeCalls' in intent:
                        native_directory = _open_child_directory(operation_fd, 'native-calls',
                            expected=intent['nativeCalls']['identity'])
                        try:
                            _retire_record_temps(native_directory)
                        finally:
                            os.close(native_directory)
                operation_names = set(os.listdir(operation_fd))
                expected = {"intent.json", "state.json", "producer.lock",
                            "staging", "phases"}
                if 'nativeCalls' in intent:
                    expected.add('native-calls')
                if state["stage"] != "empty" or "stage.json" in operation_names:
                    expected.add("stage.json")
                if "native.json" in operation_names:
                    expected.add("native.json")
                if "discard.json" in operation_names:
                    expected.add("discard.json")
                if 'recovery.json' in operation_names:
                    expected.add('recovery.json')
                if 'finalization.json' in operation_names:
                    expected.add('finalization.json')
                if 'recovery-materials.json' in operation_names:
                    expected.add('recovery-materials.json')
                _require(operation_names == expected,
                         "android_operation_record")
                stage_status = self._stage_status(
                    operation_fd, intent, state, full=True)
                native = self._validate_native(operation_fd, intent, state)
                from .android_recovery import validate_record
                recovery = validate_record(operation_fd,intent,native)
                from .android_recovery_finalization import validate_finalization
                finalization = validate_finalization(operation_fd, intent, native)
                _require(not finished or finalization is not None,
                         'android_recovery_finalization_record')
                phase_digests, phase_state = self._phase_records(
                    operation_fd, intent, state)
                from .android_native_calls import validate_workspace
                native_state = validate_workspace(operation_fd, intent)
                inspection_state = (native_state if native_state != 'idle' else
                    'finalization-'+finalization['state'] if finalization is not None else
                    'recovery-'+recovery['state'] if recovery is not None else phase_state or
                    ("native-recovery-required"
                     if stage_status == "prepared" else stage_status))
                inspection = AndroidRecoveryInspection(
                    operation_id, request_digest, intent["contextDigest"],
                    intent["configurationDigest"],
                    inspection_state, phase_digests)
                yield _AndroidRecoveryFiles(inspection, intent, native, producer, operation_fd)
            except (OSError, ArtifactError, RunDenied):
                raise AndroidOperationError(
                    "android_operation_recovery_unavailable") from None
            finally:
                for descriptor in reversed(lock_fds):
                    try:
                        # A recovery child may retain the same open file
                        # description after the parent's recovery scope exits.
                        os.close(descriptor)
                    except OSError:
                        pass
                for descriptor in reversed(directory_fds):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    @contextmanager
    def native_recovery(self, operation_id, request_digest, *, device, snapshot, parent_grant):
        """Retain the original recovery locks; this authorizes no device effect."""
        with self._admission(), self._recovery_files(operation_id, request_digest) as files:
            with self._borrow_recovery_files(files, device=device, snapshot=snapshot,
                                             parent_grant=parent_grant) as borrowed:
                yield borrowed

    @contextmanager
    def _borrow_recovery_files(self, files, *, device, snapshot, parent_grant):
        from .live.authority import DeviceAuthority
        _require(type(device) is DeviceAuthority and device.device_kind == 'android'
            and device._device_fingerprint == self.config.scope_digest,
            'android_recovery_binding')
        borrowed = None
        operation_id, request_digest = files.inspection.operation_id, files.inspection.request_digest
        with ExitStack() as stack:
            try:
                _require(files.native is not None, 'android_recovery_binding')
                lease = stack.enter_context(device.borrow_native_recovery_lease(snapshot, parent_grant=parent_grant))
                _require(parent_grant.project_id == self.config.registration.project['id']
                    and files.native['ownershipGeneration'] == lease.prior_generation
                    and files.native['hostIncarnation'] == lease.prior_host_incarnation
                    and files.native['helperIncarnation'] == lease.prior_helper_incarnation,
                    'android_recovery_binding')
                producer = os.dup(files.producer_fd)
                stack.callback(os.close, producer)
                directory = os.dup(files.operation_fd)
                stack.callback(os.close, directory)
                borrowed = AndroidRecoveryDescriptors(operation_id, request_digest,
                    files.intent['contextDigest'], self.config.scope_digest, self.configuration_digest,
                    files.native['bindingDigest'], lease.prior_generation, lease.prior_host_incarnation,
                    lease.prior_helper_incarnation, producer, directory, lease.descriptor,
                    lease.directory_descriptor, lease.lock_name, files, device, lease,
                    os.getpid(), threading.get_ident())
                with self._mutex:
                    _require(not self._closed, 'android_operation_closed')
                    self._recovery_exports[id(borrowed)] = borrowed
                self.require_recovery_descriptors(borrowed)
                yield borrowed
            except (contracts.ContractError, OSError):
                raise AndroidOperationError('android_recovery_binding') from None
            finally:
                if borrowed is not None:
                    with self._mutex:
                        borrowed._active = False
                        self._recovery_exports.pop(id(borrowed), None)

    def require_recovery_descriptors(self, borrowed):
        with self._mutex:
            _require(type(borrowed) is AndroidRecoveryDescriptors
                and self._recovery_exports.get(id(borrowed)) is borrowed and borrowed._active
                and borrowed._pid == os.getpid() and borrowed._thread == threading.get_ident()
                and not self._closed, 'android_recovery_capability')
        try:
            lease = borrowed._device.require_native_recovery_lease(borrowed._lease)
            files = borrowed._files
            intent = self._intent(borrowed.operation_id, borrowed.operation_directory_fd)
            _require(intent == files.intent and self._configuration_record() == self._configuration
                and borrowed.request_digest == intent['requestDigest']
                and borrowed.context_digest == intent['contextDigest']
                and borrowed.scope_digest == intent['scopeDigest'] == self.config.scope_digest
                and borrowed.configuration_digest == intent['configurationDigest'] == self.configuration_digest
                and borrowed.binding_digest == files.native['bindingDigest']
                and borrowed.prior_generation == lease.prior_generation
                and borrowed.prior_host_incarnation == lease.prior_host_incarnation
                and borrowed.prior_helper_incarnation == lease.prior_helper_incarnation
                and _same_identity(os.fstat(borrowed.operation_directory_fd), intent['rootIdentity'], directory=True)
                and _same_identity(os.fstat(borrowed.producer_fd), intent['producerIdentity']),
                'android_recovery_binding')
            for current, original in ((borrowed.device_fd, lease.descriptor),
                                      (borrowed.device_directory_fd, lease.directory_descriptor)):
                actual, expected = os.fstat(current), os.fstat(original)
                _require((actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino),
                         'android_recovery_binding')
            row = self.run_store.status(borrowed.operation_id)
            _require(row['requestDigest'] == borrowed.request_digest
                and row['state'] in {'admitted', 'quarantined'}
                and row['reservedBytes'] == intent['reservedBytes'], 'android_recovery_binding')
            return borrowed
        except (contracts.ContractError, OSError, RunDenied):
            raise AndroidOperationError('android_recovery_binding') from None

    def finalize_recovery(self, operation_id, request_digest, *, device, parent_grant,
                          cancellation, deadline_monotonic):
        from .android_recovery_finalization import finalize_recovery
        return finalize_recovery(self, operation_id, request_digest, device=device,
            parent_grant=parent_grant, cancellation=cancellation,
            deadline_monotonic=deadline_monotonic)

    def require_cleanup(self, capability, run_store):
        from .android_recovery_finalization import require_cleanup
        return require_cleanup(self, capability, run_store)

    def close(self, *, deadline_monotonic):
        _require(type(deadline_monotonic) in (int, float)
                 and math.isfinite(deadline_monotonic),
                 "android_operation_configuration")
        self._closed = True
        with self._condition:
            while (self._admissions or self._active or self._phases
                   or self._callbacks or self._native_controls or self._native_dispatch_threads):
                if self._native_controls:
                    from .android_native_process import _collected
                    for control in tuple(self._native_controls):
                        control.close_liveness()
                        if _collected(control.process):
                            self._native_controls.pop(control, None)
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    return False
                if self._admissions or self._active or self._phases or self._callbacks or self._native_controls or self._native_dispatch_threads:
                    self._condition.wait(min(remaining, .05) if self._native_controls or self._native_dispatch_threads else remaining)
            return True


__all__ = [
    "AndroidNativeBinding", "AndroidOperation", "AndroidOperationError",
    "AndroidOperationStore", "AndroidPhaseCapability",
    "AndroidRecoveryInspection",
]
