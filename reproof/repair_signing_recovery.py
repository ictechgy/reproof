"""Durable Android signing intent, phase ownership, and recovery authority.

This module never resumes an interrupted signature and never converts JSON,
PIDs, or a native exit-intent into cleanup authority.  Recovery holds the
canonical signing scope, RunStore lock, producer lock, and every surviving
phase lock while issuing one process-local capability.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import tempfile
import threading
import time

from . import contracts
from .execution.artifacts import BlobSet
from .execution.journal import RunDenied, RunStore, TERMINAL
from .execution.wire import MAX_TRANSFER_BYTES, canonical, decode_json
from .repair_android_signing import (
    APK_PATH, AndroidSigningIdentity, _OpenedMaterial,
)
from .repair_signing import (
    SignatureObservation, SigningContext, SigningFailureObservation,
    SigningObservation,
)


MAX_RECORD_BYTES = 64 * 1024
MAX_OWNER_OUTPUT_BYTES = 64 * 1024
MIN_OPERATION_BYTES = 2 * MAX_TRANSFER_BYTES + 512 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_PHASES = ("sign", "inspect")
_OWNER_CLASS = "io.reproloop.signing.SigningOwner"


class SigningRecoveryError(RuntimeError):
    def __init__(self, code="signing_recovery_unavailable"):
        self.code = code
        super().__init__(code)


class _OwnedProcess:
    """Synchronize the one liveness writer shared by execute() and close()."""

    __slots__ = ("process", "_writer", "_lock")

    def __init__(self, process, writer):
        self.process = process
        self._writer = writer
        self._lock = threading.Lock()

    def close_liveness(self):
        with self._lock:
            writer = self._writer
            self._writer = None
        if writer is not None:
            try:
                os.close(writer)
            except OSError:
                pass

    def acknowledge(self):
        with self._lock:
            writer = self._writer
            if writer is None:
                return False
            self._writer = None
            try:
                os.write(writer, b"\x01")
                return True
            except OSError:
                return False
            finally:
                try:
                    os.close(writer)
                except OSError:
                    pass


def _require(condition, code="signing_recovery_unavailable"):
    if not condition:
        raise SigningRecoveryError(code)


def _sha_file(path, maximum=64 * 1024 * 1024):
    path = Path(path)
    descriptor = None
    try:
        before = path.lstat()
        _require(stat.S_ISREG(before.st_mode) and not path.is_symlink()
                 and 0 < before.st_size <= maximum,
                 "signing_recovery_tool_invalid")
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        _require((opened.st_dev, opened.st_ino, opened.st_size)
                 == (before.st_dev, before.st_ino, before.st_size),
                 "signing_recovery_tool_invalid")
        digest = hashlib.sha256()
        total = 0
        while total < opened.st_size:
            chunk = os.read(descriptor, min(1024 * 1024,
                                            opened.st_size - total))
            _require(bool(chunk), "signing_recovery_tool_invalid")
            total += len(chunk); digest.update(chunk)
        after = os.fstat(descriptor)
        _require((after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                 == (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns),
                 "signing_recovery_tool_invalid")
        return digest.hexdigest()
    except SigningRecoveryError:
        raise
    except OSError:
        raise SigningRecoveryError("signing_recovery_tool_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class SigningOwnerTools:
    java: Path
    java_digest: str
    owner_jar: Path
    owner_jar_digest: str
    jni_library: Path
    jni_library_digest: str
    apksigner_jar: Path
    apksigner_jar_digest: str

    def __post_init__(self):
        fields = ("java", "owner_jar", "jni_library", "apksigner_jar")
        for name in fields:
            selected = Path(getattr(self, name))
            _require(selected.is_absolute(), "signing_recovery_tool_invalid")
            try:
                selected = selected.resolve(strict=True)
            except OSError:
                raise SigningRecoveryError(
                    "signing_recovery_tool_invalid") from None
            object.__setattr__(self, name, selected)
        for name in ("java_digest", "owner_jar_digest",
                     "jni_library_digest", "apksigner_jar_digest"):
            _require(type(getattr(self, name)) is str
                     and _DIGEST.fullmatch(getattr(self, name)) is not None,
                     "signing_recovery_tool_invalid")
        self.verify()
        _require(self.jni_library.name == "libreproloop_signing_owner_fd.dylib",
                 "signing_recovery_tool_invalid")

    def verify(self):
        for path, expected, executable in (
            (self.java, self.java_digest, True),
            (self.owner_jar, self.owner_jar_digest, False),
            (self.jni_library, self.jni_library_digest, False),
            (self.apksigner_jar, self.apksigner_jar_digest, False),
        ):
            try:
                info = path.lstat()
            except OSError:
                raise SigningRecoveryError(
                    "signing_recovery_tool_invalid") from None
            _require(stat.S_ISREG(info.st_mode) and not path.is_symlink()
                     and (not executable or info.st_mode & 0o111)
                     and _sha_file(path) == expected,
                     "signing_recovery_tool_invalid")

    @property
    def definition_digest(self):
        return contracts.digest({
            "schemaVersion": 1,
            "java": self.java_digest,
            "ownerJar": self.owner_jar_digest,
            "jniLibrary": self.jni_library_digest,
            "apksignerJar": self.apksigner_jar_digest,
            "ownerClass": _OWNER_CLASS,
            "javaFlags": ["--add-opens=java.base/java.io=ALL-UNNAMED",
                          "-Xmx256m"],
        })

    @property
    def command(self):
        self.verify()
        return (
            str(self.java), "--add-opens=java.base/java.io=ALL-UNNAMED",
            "-Xmx256m", "-Djava.library.path=" + str(self.jni_library.parent),
            "-cp", str(self.owner_jar) + os.pathsep + str(self.apksigner_jar),
            _OWNER_CLASS,
        )


def _stat_identity(info):
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


def _walk_directory(path, *, create=False, expected=None):
    path = Path(path)
    _require(path.is_absolute(), "signing_recovery_storage_invalid")
    descriptor = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | os.O_NOFOLLOW
        descriptor = os.open("/", flags)
        for component in path.parts[1:]:
            try:
                selected = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                selected = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = selected
            info = os.fstat(descriptor)
            _require(stat.S_ISDIR(info.st_mode),
                     "signing_recovery_storage_invalid")
        info = os.fstat(descriptor)
    except (OSError, SigningRecoveryError):
        if descriptor is not None:
            os.close(descriptor)
        raise SigningRecoveryError("signing_recovery_storage_invalid") from None
    actual = _stat_identity(info)
    if not (info.st_uid == os.getuid() and info.st_mode & 0o077 == 0
            and (expected is None or (_valid_identity(expected, directory=True)
                 and {key: value for key, value in actual.items()
                      if key != "links"}
                 == {key: value for key, value in expected.items()
                     if key != "links"}))):
        os.close(descriptor)
        raise SigningRecoveryError("signing_recovery_storage_invalid")
    return descriptor


def _private_directory(path, *, create=True):
    path = Path(path)
    descriptor = _walk_directory(path, create=create)
    os.close(descriptor)
    return path


def _open_child_directory(parent, name, *, expected=None):
    _require(type(name) is str and name not in {"", ".", ".."}
             and "/" not in name,
             "signing_recovery_storage_invalid")
    descriptor = None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK
            | os.O_NOFOLLOW, dir_fd=parent)
        info = os.fstat(descriptor)
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                 and info.st_mode & 0o077 == 0
                 and (expected is None
                      or (_valid_identity(expected, directory=True)
                      and {key: value for key, value in _stat_identity(info).items()
                          if key != "links"}
                      == {key: value for key, value in expected.items()
                          if key != "links"})),
                 "signing_recovery_storage_invalid")
        return descriptor
    except (OSError, SigningRecoveryError):
        if descriptor is not None:
            os.close(descriptor)
        raise SigningRecoveryError("signing_recovery_storage_invalid") from None


def _open_owned_regular(parent, name, *, expected=None, writable=False,
                        zero=None):
    _require(type(name) is str and name not in {"", ".", ".."}
             and "/" not in name,
             "signing_recovery_record_invalid")
    descriptor = None
    try:
        flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NONBLOCK \
            | os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=parent)
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                 and info.st_nlink == 1
                 and stat.S_IMODE(info.st_mode) == 0o600
                 and (expected is None or (_valid_identity(expected)
                      and _stat_identity(info) == expected))
                 and (zero is None or (info.st_size == 0) is zero),
                 "signing_recovery_record_invalid")
        return descriptor
    except (OSError, SigningRecoveryError):
        if descriptor is not None:
            os.close(descriptor)
        raise SigningRecoveryError("signing_recovery_record_invalid") from None


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _identity(path):
    info = Path(path).lstat()
    return {"device": info.st_dev, "inode": info.st_ino,
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
            "links": info.st_nlink}


def _same_identity(path, expected, *, directory=False, zero=None):
    try:
        info = Path(path).lstat()
    except OSError:
        return False
    actual = {"device": info.st_dev, "inode": info.st_ino,
              "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
              "links": info.st_nlink}
    compared = dict(expected)
    if directory:
        actual.pop("links", None); compared.pop("links", None)
    if ((directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and not stat.S_ISREG(info.st_mode))
            or Path(path).is_symlink()
            or actual != compared):
        return False
    return zero is None or (info.st_size == 0) is zero


def _write_new(path, value):
    raw = canonical(value)
    _require(len(raw) <= MAX_RECORD_BYTES, "signing_recovery_record_invalid")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            _require(written > 0, "signing_recovery_record_invalid")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_directory(Path(path).parent)


def _replace(path, value):
    raw = canonical(value)
    _require(len(raw) <= MAX_RECORD_BYTES, "signing_recovery_record_invalid")
    temporary = Path(path).parent / (".state-" + os.urandom(16).hex())
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            _require(written > 0, "signing_recovery_record_invalid")
            offset += written
        os.fsync(descriptor); os.close(descriptor); descriptor = None
        os.replace(temporary, path)
        _sync_directory(Path(path).parent)
    except OSError:
        raise SigningRecoveryError("signing_recovery_record_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _replace_at(parent, name, value):
    raw = canonical(value)
    _require(len(raw) <= MAX_RECORD_BYTES, "signing_recovery_record_invalid")
    temporary = ".state-" + os.urandom(16).hex()
    descriptor = None
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            _require(written > 0, "signing_recovery_record_invalid")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor); descriptor = None
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except (OSError, SigningRecoveryError):
        raise SigningRecoveryError("signing_recovery_record_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _read_at(parent, name, maximum=MAX_RECORD_BYTES):
    descriptor = None
    try:
        descriptor = _open_owned_regular(parent, name)
        opened = os.fstat(descriptor)
        _require(0 < opened.st_size <= maximum,
                 "signing_recovery_record_invalid")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        _require(len(raw) == opened.st_size <= maximum
                 and (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                 == (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns),
                 "signing_recovery_record_invalid")
        return decode_json(bytes(raw))
    except (OSError, ValueError, SigningRecoveryError):
        raise SigningRecoveryError("signing_recovery_record_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read(path, maximum=MAX_RECORD_BYTES):
    parent = _walk_directory(Path(path).parent)
    try:
        return _read_at(parent, Path(path).name, maximum)
    finally:
        os.close(parent)


def _read_blob(path, maximum):
    parent = _walk_directory(Path(path).parent)
    descriptor = None
    try:
        descriptor = _open_owned_regular(parent, Path(path).name)
        opened = os.fstat(descriptor)
        _require(0 < opened.st_size <= maximum,
                 "signing_recovery_owner_output_invalid")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024,
                                            maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        _require(len(raw) == opened.st_size <= maximum
                 and (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                 == (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns),
                 "signing_recovery_owner_output_invalid")
        return bytes(raw)
    except (OSError, SigningRecoveryError):
        raise SigningRecoveryError(
            "signing_recovery_owner_output_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _sha_at(parent, name, maximum):
    descriptor = None
    try:
        descriptor = _open_owned_regular(parent, name)
        opened = os.fstat(descriptor)
        _require(0 < opened.st_size <= maximum,
                 "signing_recovery_record_invalid")
        digest = hashlib.sha256()
        total = 0
        while total <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        _require(total == opened.st_size <= maximum
                 and (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                 == (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns),
                 "signing_recovery_record_invalid")
        return digest.hexdigest()
    except (OSError, SigningRecoveryError):
        raise SigningRecoveryError("signing_recovery_record_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _context_common(context):
    return {
        "operationId": context.operation_id,
        "repairPlanDigest": context.repair_plan_digest,
        "projectDigest": context.project_digest,
        "applicationId": context.application_id,
        "sourceDigest": context.source_digest,
        "unsignedArtifactDigest": context.unsigned_artifact_digest,
        "signingPolicyDigest": context.signing_policy_digest,
    }


def _validate_context(context):
    _require(type(context) is SigningContext and type(context.nonce) is str
             and 1 <= len(context.nonce) <= 128,
             "signing_recovery_context_invalid")
    try:
        contracts.validate_id(context.operation_id)
        for value in _context_common(context).values():
            if value != context.application_id and value != context.operation_id:
                contracts.validate_digest(value)
        contracts.validate_id(context.application_id)
        if context.signed_artifact_digest is not None:
            contracts.validate_digest(context.signed_artifact_digest)
    except contracts.ContractError:
        raise SigningRecoveryError("signing_recovery_context_invalid") from None


@dataclass(slots=True)
class SigningOperation:
    store: "SigningOperationStore" = field(repr=False)
    run: object = field(repr=False)
    operation_id: str
    request_digest: str
    initial_context_digest: str
    _issuer: object = field(repr=False)
    _pid: int = field(repr=False)
    _thread: int = field(repr=False)
    _active: bool = field(default=True, repr=False)


@dataclass(slots=True)
class SigningCleanupCapability:
    operation_id: str
    request_digest: str
    context_digest: str
    scope_digest: str
    evidence_digest: str
    _issuer: object = field(repr=False)
    _pid: int = field(repr=False)
    _thread: int = field(repr=False)
    _lock_fds: tuple[int, ...] = field(repr=False)
    _lock_checks: tuple[tuple[int, str, dict], ...] = field(repr=False)
    _active: bool = field(default=True, repr=False)
    _consumed: bool = field(default=False, repr=False)


class SigningOperationStore:
    def __init__(self, run_store, scope_digest, tools, identity, private_root, *, create=True):
        _require(type(run_store) is RunStore and type(tools) is SigningOwnerTools
                 and type(identity) is AndroidSigningIdentity and type(create) is bool,
                 "signing_recovery_configuration_invalid")
        try:
            contracts.validate_digest(scope_digest)
        except contracts.ContractError:
            raise SigningRecoveryError(
                "signing_recovery_configuration_invalid") from None
        self.run_store = run_store
        self.scope_digest = scope_digest
        self.tools = tools
        self.identity = identity
        self.root = _private_directory(Path(private_root), create=create)
        self.operations = _private_directory(self.root / "operations", create=create)
        self._control_path = self.root / "control.lock"
        if create and not self._control_path.exists():
            descriptor = os.open(self._control_path, os.O_RDWR | os.O_CREAT
                                 | os.O_NOFOLLOW, 0o600)
            os.close(descriptor); _sync_directory(self.root)
        control = self._control_path.lstat()
        _require(stat.S_ISREG(control.st_mode) and not self._control_path.is_symlink()
                 and control.st_uid == os.getuid() and control.st_nlink == 1
                 and stat.S_IMODE(control.st_mode) == 0o600,
                 "signing_recovery_storage_invalid")
        self._issuer = object()
        self._mutex = threading.RLock()
        self._closed = False
        self._active = {}
        self._processes = {}
        self._callbacks = set()
        self._recoveries = {}

    @property
    def definition_digest(self):
        return contracts.digest({
            "schemaVersion": 1,
            "scopeDigest": self.scope_digest,
            "toolsDigest": self.tools.definition_digest,
            "identity": {
                "referenceId": self.identity.reference_id,
                "applicationId": self.identity.application_id,
                "packageName": self.identity.package_name,
                "certificateSha256": self.identity.certificate_sha256,
                "signingConfigurationDigest":
                    self.identity.signing_configuration_digest,
            },
            "runStoreRootDigest": contracts.digest(str(self.run_store.root)),
        })

    @property
    def active_processes(self):
        """Count live or not-yet-returned native dispatch ownership."""
        with self._mutex:
            return len(self._processes) + len(self._callbacks)

    @contextmanager
    def _control(self):
        with self._mutex:
            descriptor = os.open(self._control_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN); os.close(descriptor)

    def _operation_root(self, operation_id):
        _require(type(operation_id) is str and _ID.fullmatch(operation_id),
                 "signing_recovery_operation_invalid")
        return self.operations / operation_id

    def _intent(self, operation_id, directory_fd=None):
        value = (_read(self._operation_root(operation_id) / "intent.json")
                 if directory_fd is None
                 else _read_at(directory_fd, "intent.json"))
        _require(type(value) is dict and set(value) == {
            "schemaVersion", "operationId", "requestDigest",
            "initialContextDigest", "scopeDigest", "definitionDigest",
            "toolsDigest", "identityDigest", "runStoreRootDigest",
            "diskBytes", "context", "rootIdentity", "producerIdentity"}
            and value["schemaVersion"] == 1
            and value["operationId"] == operation_id
            and all(type(value[key]) is str and _DIGEST.fullmatch(value[key])
                    for key in ("requestDigest", "initialContextDigest",
                                "scopeDigest", "definitionDigest", "toolsDigest",
                                "identityDigest", "runStoreRootDigest"))
            and type(value["diskBytes"]) is int
            and MIN_OPERATION_BYTES <= value["diskBytes"] <= 512 * 1024 ** 3
            and type(value["context"]) is dict
            and set(value["context"]) == {
                "operationId", "repairPlanDigest", "projectDigest",
                "applicationId", "sourceDigest", "unsignedArtifactDigest",
                "signingPolicyDigest"}
            and _valid_identity(value["rootIdentity"], directory=True)
            and _valid_identity(value["producerIdentity"]),
            "signing_recovery_record_invalid")
        return value

    def _state(self, operation_id, directory_fd=None):
        value = (_read(self._operation_root(operation_id) / "state.json")
                 if directory_fd is None
                 else _read_at(directory_fd, "state.json"))
        _require(type(value) is dict and set(value) == {
            "schemaVersion", "operationId", "requestDigest", "phases", "recovery"}
            and value["schemaVersion"] == 1
            and value["operationId"] == operation_id
            and type(value["requestDigest"]) is str
            and _DIGEST.fullmatch(value["requestDigest"]) is not None
            and type(value["phases"]) is dict
            and set(value["phases"]) <= set(_PHASES)
            and value["recovery"] in {None, "sanitized"},
            "signing_recovery_record_invalid")
        expected = {"state", "contextDigest", "inputDigest", "inputBytes",
                    "outputDigest", "outputBytes", "definitionDigest",
                    "phaseDigest"}
        _require(all(type(item) is dict and set(item) == expected
                     for item in value["phases"].values()),
                 "signing_recovery_record_invalid")
        for item in value["phases"].values():
            _require(item["state"] in {"prepared", "exit-collected", "cleaned"}
                     and all(type(item[key]) is str and _DIGEST.fullmatch(item[key])
                             for key in ("contextDigest", "inputDigest",
                                         "definitionDigest", "phaseDigest"))
                     and type(item["inputBytes"]) is int and item["inputBytes"] > 0
                     and ((item["outputDigest"] is None and item["outputBytes"] is None)
                          or (type(item["outputDigest"]) is str
                              and _DIGEST.fullmatch(item["outputDigest"])
                              and type(item["outputBytes"]) is int
                              and item["outputBytes"] > 0)),
                     "signing_recovery_record_invalid")
        return value

    def _replace_state(self, operation_id, value, directory_fd=None):
        if directory_fd is None:
            _replace(self._operation_root(operation_id) / "state.json", value)
        else:
            _replace_at(directory_fd, "state.json", value)

    @contextmanager
    def admit(self, initial_context, request_digest, disk_bytes):
        _validate_context(initial_context)
        _require(initial_context.signed_artifact_digest is None
                 and type(request_digest) is str and _DIGEST.fullmatch(request_digest)
                 and type(disk_bytes) is int
                 and MIN_OPERATION_BYTES <= disk_bytes <= 512 * 1024 ** 3,
                 "signing_recovery_admission_invalid")
        operation_id = initial_context.operation_id
        with self.run_store.repair_scope_lease("signing", self.scope_digest):
            self.run_store.require_available()
            with self._control():
                _require(not self._closed and operation_id not in self._active,
                         "signing_recovery_admission_invalid")
                root = self._operation_root(operation_id)
                try:
                    root.mkdir(mode=0o700)
                    producer = root / "producer.lock"
                    descriptor = os.open(producer, os.O_RDWR | os.O_CREAT
                                         | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    os.close(descriptor)
                    intent = {
                        "schemaVersion": 1,
                        "operationId": operation_id,
                        "requestDigest": request_digest,
                        "initialContextDigest": initial_context.digest,
                        "scopeDigest": self.scope_digest,
                        "definitionDigest": self.definition_digest,
                        "toolsDigest": self.tools.definition_digest,
                        "identityDigest": contracts.digest({
                            "referenceId": self.identity.reference_id,
                            "certificateSha256": self.identity.certificate_sha256,
                            "configurationDigest": self.identity.signing_configuration_digest,
                        }),
                        "runStoreRootDigest": contracts.digest(str(self.run_store.root)),
                        "diskBytes": disk_bytes,
                        "context": _context_common(initial_context),
                        "rootIdentity": _identity(root),
                        "producerIdentity": _identity(producer),
                    }
                    _write_new(root / "intent.json", intent)
                    _write_new(root / "state.json", {
                        "schemaVersion": 1, "operationId": operation_id,
                        "requestDigest": request_digest, "phases": {},
                        "recovery": None})
                except Exception:
                    raise SigningRecoveryError(
                        "signing_recovery_admission_invalid") from None
            with self.run_store.admit(operation_id, request_digest,
                                      disk_bytes=disk_bytes) as run:
                operation = SigningOperation(
                    self, run, operation_id, request_digest,
                    initial_context.digest, self._issuer, os.getpid(),
                    threading.get_ident())
                with self._control():
                    self._active[operation_id] = operation
                try:
                    yield operation
                finally:
                    operation._active = False
                    with self._control():
                        self._active.pop(operation_id, None)

    def _require_operation(self, operation, context, mode, body):
        _validate_context(context)
        with self._control():
            live = self._active.get(getattr(operation, "operation_id", None))
        _require(type(operation) is SigningOperation
                 and operation.store is self and operation._issuer is self._issuer
                 and operation._active and operation._pid == os.getpid()
                 and live is operation
                 and mode in _PHASES and not self._closed,
                 "signing_recovery_operation_invalid")
        intent = self._intent(operation.operation_id)
        state = self._state(operation.operation_id)
        _require(intent["requestDigest"] == operation.request_digest
                 and intent["initialContextDigest"] == operation.initial_context_digest
                 and intent["definitionDigest"] == self.definition_digest
                 and intent["toolsDigest"] == self.tools.definition_digest
                 and intent["context"] == _context_common(context)
                 and state["requestDigest"] == operation.request_digest
                 and operation.run.store is self.run_store
                 and operation.run.operation_id == operation.operation_id
                 and operation.run.request_digest == operation.request_digest,
                 "signing_recovery_binding_invalid")
        row = self.run_store.status(operation.operation_id)
        _require(row["requestDigest"] == operation.request_digest
                 and row["state"] == "admitted",
                 "signing_recovery_binding_invalid")
        _require(mode not in state["phases"]
                 and (mode == "sign" or state["phases"].get("sign", {}).get("state") == "cleaned")
                 and type(body) is bytes and 0 < len(body) <= MAX_TRANSFER_BYTES,
                 "signing_recovery_phase_invalid")
        measured = hashlib.sha256(body).hexdigest()
        if mode == "sign":
            _require(context.signed_artifact_digest is None
                     and context.unsigned_artifact_digest == measured,
                     "signing_recovery_binding_invalid")
        else:
            sign = state["phases"]["sign"]
            _require(context.signed_artifact_digest == measured
                     and sign["outputDigest"] == measured
                     and sign["outputBytes"] == len(body),
                     "signing_recovery_binding_invalid")
        return intent, state, measured

    @staticmethod
    def _apk(artifacts):
        _require(type(artifacts) is BlobSet and len(artifacts.entries) == 1
                 and artifacts.entries[0][0] == APK_PATH,
                 "signing_recovery_artifact_invalid")
        return artifacts.entries[0][1]

    def _owner_config(self, phase, mode, context, input_path, output_path,
                      input_digest, material):
        values = {
            "schemaVersion": "1", "mode": mode,
            "operationId": context.operation_id,
            "requestDigest": phase["requestDigest"],
            "contextDigest": context.digest,
            "scopeDigest": self.scope_digest,
            "ownerDefinitionDigest": self.tools.definition_digest,
            "inputPath": str(input_path), "outputPath": str(output_path),
            "inputDigest": input_digest,
            "maxBytes": str(phase["maxBytes"]),
            "packageName": self.identity.package_name,
            "certificateSha256": self.identity.certificate_sha256,
            "schemes": ",".join(self.identity.signature_schemes),
            "usesPermissions": ",".join(self.identity.permissions),
            "declaredPermissions": "",
            "lockPath": str(input_path.parent / "owner.lock"),
            "startPath": str(input_path.parent / "start.json"),
            "terminationPath": str(input_path.parent / "termination.json"),
            "workPath": str(input_path.parent),
            "keyAlias": material.key_alias if material is not None else "",
        }
        return "".join(f"{key}={values[key]}\n"
                       for key in sorted(values)).encode("ascii")

    def _prepare_phase(self, operation, context, mode, body, state, digest):
        root = self._operation_root(operation.operation_id)
        phase_root = root / mode
        phase_root.mkdir(mode=0o700)
        paths = {name: phase_root / name for name in
                 ("owner.lock", "start.json", "termination.json", APK_PATH)}
        for name in ("owner.lock", "start.json", "termination.json"):
            descriptor = os.open(paths[name], os.O_RDWR | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
        descriptor = os.open(paths[APK_PATH], os.O_WRONLY | os.O_CREAT
                             | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            offset = 0
            while offset < len(body):
                count = os.write(descriptor, body[offset:offset + 1024 * 1024])
                _require(count > 0, "signing_recovery_storage_invalid")
                offset += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _sync_directory(phase_root)
        phase = {
            "schemaVersion": 1, "mode": mode,
            "operationId": operation.operation_id,
            "requestDigest": operation.request_digest,
            "contextDigest": context.digest,
            "definitionDigest": self.tools.definition_digest,
            "inputDigest": digest, "inputBytes": len(body),
            "maxBytes": MAX_TRANSFER_BYTES,
            "allowedFiles": sorted({"phase.json", "owner.lock", "start.json",
                                     "termination.json", APK_PATH, "signed.apk"}),
            "directoryIdentity": _identity(phase_root),
            "fileIdentities": {name: _identity(path)
                               for name, path in paths.items()},
            "outputExpectedAbsent": True,
        }
        _write_new(phase_root / "phase.json", phase)
        state["phases"][mode] = {
            "state": "prepared", "contextDigest": context.digest,
            "inputDigest": digest, "inputBytes": len(body),
            "outputDigest": None, "outputBytes": None,
            "definitionDigest": self.tools.definition_digest,
            "phaseDigest": _sha_file(phase_root / "phase.json",
                                     MAX_RECORD_BYTES),
        }
        self._replace_state(operation.operation_id, state)
        return phase_root, phase

    @staticmethod
    def _read_owner_record(path):
        value = _read(path, 4096)
        return SigningOperationStore._validate_owner_record_shape(value)

    @staticmethod
    def _read_owner_record_at(parent, name):
        value = _read_at(parent, name, 4096)
        return SigningOperationStore._validate_owner_record_shape(value)

    @staticmethod
    def _validate_owner_record_shape(value):
        _require(type(value) is dict and set(value) == {
            "contextDigest", "operationId", "ownerDefinitionDigest",
            "ownerPid", "ownerStartedAtMs", "recordMeaning",
            "requestDigest", "schemaVersion", "scopeDigest", "stage", "state"},
            "signing_recovery_owner_record_invalid")
        return value

    def _validate_owner_binding(self, value, operation_id, request_digest,
                                context_digest, states, meaning):
        _require(value["schemaVersion"] == 1
                 and value["operationId"] == operation_id
                 and value["requestDigest"] == request_digest
                 and value["contextDigest"] == context_digest
                 and value["scopeDigest"] == self.scope_digest
                 and value["ownerDefinitionDigest"] == self.tools.definition_digest
                 and value["recordMeaning"] == meaning
                 and value["state"] in states,
                 "signing_recovery_owner_record_invalid")

    def _validate_owner_record(self, value, context, request_digest, state):
        self._validate_owner_binding(
            value, context.operation_id, request_digest, context.digest,
            {state}, "owner-start" if state == "started" else "exit-intent")

    def _collect(self, process, owned_process, cancellation, deadline):
        stdout = bytearray(); stderr = bytearray(); line = None
        interrupted = False
        streams = {process.stdout.fileno(): (process.stdout, stdout),
                   process.stderr.fileno(): (process.stderr, stderr)}
        while process.poll() is None and line is None:
            if (self._closed or cancellation.is_set()
                    or time.monotonic() >= deadline):
                interrupted = True
                owned_process.close_liveness()
                break
            readable, _, _ = select.select(list(streams), [], [],
                                           min(.05, max(0, deadline-time.monotonic())))
            for descriptor in readable:
                chunk = os.read(descriptor, 8192)
                if not chunk:
                    streams.pop(descriptor, None)
                    continue
                target = streams[descriptor][1]
                target.extend(chunk)
                if len(target) > MAX_OWNER_OUTPUT_BYTES:
                    interrupted = True
                    owned_process.close_liveness()
                    break
            if b"\n" in stdout:
                first, remainder = bytes(stdout).split(b"\n", 1)
                _require(not remainder, "signing_recovery_owner_output_invalid")
                line = first
        if line is not None:
            if not owned_process.acknowledge():
                interrupted = True
        if self._closed:
            interrupted = True
        wait_deadline = time.monotonic() + 3
        while process.poll() is None and time.monotonic() < wait_deadline:
            readable, _, _ = select.select(list(streams), [], [], .05)
            for descriptor in readable:
                chunk = os.read(descriptor, 8192)
                if not chunk:
                    streams.pop(descriptor, None)
                    continue
                streams[descriptor][1].extend(chunk)
                if len(streams[descriptor][1]) > MAX_OWNER_OUTPUT_BYTES:
                    interrupted = True
        owned_process.close_liveness()
        _require(process.poll() is not None,
                 "signing_recovery_owner_live")
        if line is not None:
            _require(bytes(stdout) == line + b"\n",
                     "signing_recovery_owner_output_invalid")
        for stream in (process.stdout, process.stderr):
            stream.close()
        return process.returncode, line, bytes(stderr), interrupted

    @staticmethod
    def _validate_phase_cleanup(phase_root, phase):
        _require(_same_identity(phase_root, phase["directoryIdentity"],
                                directory=True),
                 "signing_recovery_cleanup_unknown")
        children = list(phase_root.iterdir())
        _require({item.name for item in children} <= set(phase["allowedFiles"]),
                 "signing_recovery_cleanup_unknown")
        for name, expected in phase["fileIdentities"].items():
            _require(_same_identity(phase_root / name, expected),
                     "signing_recovery_cleanup_unknown")
        for item in children:
            info = item.lstat()
            _require(stat.S_ISREG(info.st_mode) and not item.is_symlink()
                     and info.st_uid == os.getuid() and info.st_nlink == 1
                     and stat.S_IMODE(info.st_mode) == 0o600
                     and info.st_size <= MAX_TRANSFER_BYTES,
                     "signing_recovery_cleanup_unknown")
        _require((phase_root / "owner.lock").stat().st_size == 0,
                 "signing_recovery_cleanup_unknown")
        return children

    @staticmethod
    def _validate_phase_cleanup_fd(phase_fd, phase):
        names = os.listdir(phase_fd)
        _require(set(names) <= set(phase["allowedFiles"]),
                 "signing_recovery_cleanup_unknown")
        for name in names:
            info = os.stat(name, dir_fd=phase_fd, follow_symlinks=False)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                     and info.st_nlink == 1
                     and stat.S_IMODE(info.st_mode) == 0o600
                     and info.st_size <= MAX_TRANSFER_BYTES,
                     "signing_recovery_cleanup_unknown")
        for name, expected in phase["fileIdentities"].items():
            try:
                info = os.stat(name, dir_fd=phase_fd, follow_symlinks=False)
            except OSError:
                raise SigningRecoveryError(
                    "signing_recovery_cleanup_unknown") from None
            _require(_valid_identity(expected)
                     and _stat_identity(info) == expected,
                     "signing_recovery_cleanup_unknown")
        owner = os.stat(
            "owner.lock", dir_fd=phase_fd, follow_symlinks=False)
        _require(owner.st_size == 0,
                 "signing_recovery_cleanup_unknown")
        return names

    def _cleanup_phase_fd(self, operation_fd, mode, phase_fd, phase):
        names = self._validate_phase_cleanup_fd(phase_fd, phase)
        current = os.stat(mode, dir_fd=operation_fd, follow_symlinks=False)
        _require(stat.S_ISDIR(current.st_mode)
                 and {key: value for key, value in _stat_identity(current).items()
                      if key != "links"}
                 == {key: value for key, value in
                     phase["directoryIdentity"].items() if key != "links"},
                 "signing_recovery_cleanup_unknown")
        for name in names:
            os.unlink(name, dir_fd=phase_fd)
        os.fsync(phase_fd)
        os.rmdir(mode, dir_fd=operation_fd)
        os.fsync(operation_fd)

    def _cleanup_phase(self, phase_root, phase):
        children = self._validate_phase_cleanup(phase_root, phase)
        for item in children:
            item.unlink()
        _sync_directory(phase_root); phase_root.rmdir()
        _sync_directory(phase_root.parent)

    def execute(self, operation, context, artifacts, *, mode, material=None,
                cancellation, deadline):
        _require(type(deadline) in (int, float)
                 and callable(getattr(cancellation, "is_set", None))
                 and ((mode == "sign" and type(material) is _OpenedMaterial)
                      or (mode == "inspect" and material is None)),
                 "signing_recovery_phase_invalid")
        callback = threading.current_thread()
        try:
            with self._control():
                _require(not self._closed,
                         "signing_recovery_operation_invalid")
                self._callbacks.add(callback)
        except Exception:
            if material is not None:
                material.close()
            raise
        producer_fd = phase_fd = live_read = live_write = None
        start_fd = termination_fd = directory_fd = password_reader = None
        config_file = process = owned_process = None
        phase_root = phase = lock = None
        try:
            body = self._apk(artifacts)
            self._require_operation(operation, context, mode, body)
            root = self._operation_root(operation.operation_id)
            producer_fd = os.open(
                root / "producer.lock", os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(producer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SigningRecoveryError(
                    "signing_recovery_producer_live") from None
            # A competing callback can complete between the optimistic check
            # above and this lock acquisition.  Reload all durable state only
            # after this callback owns the producer lock.
            intent, state, measured = self._require_operation(
                operation, context, mode, body)
            phase_root, phase = self._prepare_phase(
                operation, context, mode, body, state, measured)
            phase_fd = os.open(phase_root / "owner.lock", os.O_RDWR | os.O_NOFOLLOW)
            fcntl.flock(phase_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            config = self._owner_config(
                phase, mode, context, phase_root / APK_PATH,
                phase_root / "signed.apk", measured, material)
            config_file = tempfile.TemporaryFile(dir=phase_root)
            config_file.write(config); config_file.flush(); os.fsync(config_file.fileno())
            config_file.seek(0)
            live_read, live_write = os.pipe()
            start_fd = os.open(phase_root / "start.json", os.O_RDWR | os.O_NOFOLLOW)
            termination_fd = os.open(phase_root / "termination.json", os.O_RDWR | os.O_NOFOLLOW)
            directory_fd = os.open(phase_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            inherited = [config_file.fileno(), phase_fd, live_read, start_fd,
                         termination_fd, directory_fd]
            arguments = list(self.tools.command)
            if mode == "sign":
                password_reader = self._password_pipe(material.password_input)
                inherited.extend([material.descriptor, password_reader])
            else:
                inherited.extend([0, 0])
            passed = tuple(item for item in inherited if item >= 3)
            # This gate makes the final closed/deadline/cancellation check,
            # spawn, and owner registration indivisible to close().  close()
            # seals dispatch before waiting on this mutex and returns False if
            # the Popen boundary does not return within its caller's deadline.
            with self._control():
                _require(not self._closed,
                         "signing_recovery_operation_invalid")
                _require(not cancellation.is_set()
                         and time.monotonic() < deadline,
                         "signing_recovery_phase_invalid")
                process = subprocess.Popen(
                    [*arguments, *(str(item) for item in inherited)],
                    cwd=phase_root, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    close_fds=True, pass_fds=passed,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C",
                         "LC_ALL": "C"})
                owned_process = _OwnedProcess(process, live_write)
                live_write = None
                self._processes[(operation.operation_id, mode)] = owned_process
            # The JVM now owns this same locked open-file description.
            os.close(phase_fd); phase_fd = None
            for descriptor in (live_read, start_fd, termination_fd, directory_fd):
                os.close(descriptor)
            live_read = start_fd = termination_fd = directory_fd = None
            if password_reader is not None:
                os.close(password_reader); password_reader = None
            config_file.close(); config_file = None
            code, raw, error, interrupted = self._collect(
                process, owned_process, cancellation, deadline)
            with self._control():
                if self._processes.get((operation.operation_id, mode)) \
                        is owned_process:
                    self._processes.pop((operation.operation_id, mode), None)
            _require(len(error) <= MAX_OWNER_OUTPUT_BYTES,
                     "signing_recovery_owner_output_invalid")
            lock = os.open(phase_root / "owner.lock", os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(lock)
                raise SigningRecoveryError("signing_recovery_owner_live") from None
            try:
                start = self._read_owner_record(phase_root / "start.json")
                self._validate_owner_record(start, context, operation.request_digest,
                                            "started")
                termination = self._read_owner_record(
                    phase_root / "termination.json")
                if interrupted:
                    self._validate_owner_binding(
                        termination, context.operation_id,
                        operation.request_digest, context.digest,
                        {"parent-loss-exit-intent", "ack-timeout-exit-intent",
                         "failed", "succeeded"}, "exit-intent")
                else:
                    self._validate_owner_record(
                        termination, context, operation.request_digest,
                        "succeeded" if code == 0 else "failed")
                if interrupted or code != 0 or raw is None:
                    state = self._state(operation.operation_id)
                    state["phases"][mode]["state"] = "exit-collected"
                    self._replace_state(operation.operation_id, state)
                    self._cleanup_phase(phase_root, phase)
                    state = self._state(operation.operation_id)
                    state["phases"][mode]["state"] = "cleaned"
                    self._replace_state(operation.operation_id, state)
                    failure = "cancelled" if (
                        self._closed or cancellation.is_set()) else (
                        "signing_timeout" if interrupted else
                        "signature_invalid" if mode == "inspect" else "signing_failed")
                    evidence = contracts.digest({"phase": mode,
                        "contextDigest": context.digest, "code": failure,
                        "definitionDigest": self.tools.definition_digest})
                    return SigningFailureObservation(
                        context.digest, failure, evidence, True, True)
                result = decode_json(raw)
                _require(type(result) is dict and result.get("status") == "succeeded"
                         and result.get("operationId") == context.operation_id
                         and result.get("requestDigest") == operation.request_digest
                         and result.get("contextDigest") == context.digest
                         and result.get("scopeDigest") == self.scope_digest
                         and result.get("inputDigest") == measured
                         and result.get("packageName") == self.identity.package_name
                         and result.get("certificateSha256") == self.identity.certificate_sha256
                         and result.get("schemes") == list(self.identity.signature_schemes)
                         and result.get("usesPermissions") == list(self.identity.permissions)
                         and result.get("declaredPermissions") == [],
                         "signing_recovery_owner_output_invalid")
                output_path = phase_root / ("signed.apk" if mode == "sign" else APK_PATH)
                output = _read_blob(output_path, MAX_TRANSFER_BYTES)
                output_digest = hashlib.sha256(output).hexdigest()
                _require(result["outputDigest"] == output_digest
                         and 0 < len(output) <= MAX_TRANSFER_BYTES,
                         "signing_recovery_owner_output_invalid")
                state = self._state(operation.operation_id)
                state["phases"][mode].update(
                    state="exit-collected", outputDigest=output_digest,
                    outputBytes=len(output))
                self._replace_state(operation.operation_id, state)
                self._cleanup_phase(phase_root, phase)
                state = self._state(operation.operation_id)
                state["phases"][mode]["state"] = "cleaned"
                self._replace_state(operation.operation_id, state)
                evidence = contracts.digest({"phase": mode,
                    "contextDigest": context.digest, "inputDigest": measured,
                    "outputDigest": output_digest,
                    "definitionDigest": self.tools.definition_digest})
                if mode == "sign":
                    return SigningObservation(
                        context.digest, BlobSet(((APK_PATH, output),)),
                        evidence, True, True)
                return SignatureObservation(context.digest, True, evidence, True, True)
            finally:
                if lock is not None:
                    fcntl.flock(lock, fcntl.LOCK_UN); os.close(lock)
                    lock = None
        finally:
            if material is not None:
                material.close()
            if owned_process is not None:
                owned_process.close_liveness()
            if config_file is not None:
                config_file.close()
            if phase_fd is not None:
                # No JVM inherited the phase lock if it remains here.
                try:
                    fcntl.flock(phase_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            for descriptor in (phase_fd, live_read, live_write, start_fd,
                               termination_fd, directory_fd, password_reader):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if (process is not None and process.poll() is not None
                    and owned_process is not None):
                with self._control():
                    if self._processes.get((operation.operation_id, mode)) \
                            is owned_process:
                        self._processes.pop((operation.operation_id, mode), None)
            if producer_fd is not None:
                try:
                    fcntl.flock(producer_fd, fcntl.LOCK_UN)
                finally:
                    os.close(producer_fd)
            with self._mutex:
                self._callbacks.discard(callback)

    @staticmethod
    def _password_pipe(password):
        reader, writer = os.pipe()
        try:
            raw = bytes(password)
            offset = 0
            while offset < len(raw):
                count = os.write(writer, raw[offset:])
                _require(count > 0, "signing_recovery_material_invalid")
                offset += count
        finally:
            os.close(writer)
        return reader

    def status(self, operation_id):
        self._operation_root(operation_id)
        descriptors = []
        try:
            parent = _walk_directory(self.operations)
            descriptors.append(parent)
            try:
                os.stat(operation_id, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return {"schemaVersion": 1, "operationId": operation_id,
                        "state": "no-intent"}
            root = _open_child_directory(parent, operation_id)
            descriptors.append(root)
            intent = self._intent(operation_id, root)
            state = self._state(operation_id, root)
            row = self.run_store.status(operation_id)
            _require(intent["definitionDigest"] == self.definition_digest
                     and intent["scopeDigest"] == self.scope_digest
                     and intent["requestDigest"] == row["requestDigest"]
                     == state["requestDigest"], "signing_recovery_binding_invalid")
            producer = _open_owned_regular(root, "producer.lock", writable=True,
                                            expected=intent["producerIdentity"], zero=True)
            descriptors.append(producer)
            live = False
            try:
                fcntl.flock(producer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                live = True
            selected = ("producer-live" if live else "terminal" if row["state"] in TERMINAL
                        else "recovery-sanitized" if state.get("recovery") == "sanitized"
                        else "recovery-required")
            return {"schemaVersion": 1, "operationId": operation_id,
                    "requestDigest": intent["requestDigest"],
                    "contextDigest": intent["initialContextDigest"],
                    "definitionDigest": intent["definitionDigest"],
                    "state": selected}
        except (SigningRecoveryError, RunDenied, OSError):
            return {"schemaVersion": 1, "operationId": operation_id,
                    "state": "intent-orphan"}
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @contextmanager
    def recovery(self, operation_id, request_digest):
        root = self._operation_root(operation_id)
        lock_fds = []
        lock_checks = []
        directory_fds = []
        capability = None
        with self.run_store.repair_scope_lease("signing", self.scope_digest):
            run_root_fd = _walk_directory(self.run_store.root)
            directory_fds.append(run_root_fd)
            vm_fd = _open_owned_regular(
                run_root_fd, ".vm-lock", writable=True)
            try:
                fcntl.flock(vm_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_fds.append(vm_fd)
                lock_checks.append(
                    (run_root_fd, ".vm-lock",
                     _stat_identity(os.fstat(vm_fd))))
                private_fd = _walk_directory(self.root)
                directory_fds.append(private_fd)
                operations_fd = _open_child_directory(
                    private_fd, "operations")
                directory_fds.append(operations_fd)
                operation_fd = _open_child_directory(
                    operations_fd, operation_id)
                directory_fds.append(operation_fd)
                with self._control():
                    intent = self._intent(operation_id, operation_fd)
                    state = self._state(operation_id, operation_fd)
                    _require(intent["operationId"] == operation_id
                             and intent["requestDigest"] == request_digest
                             and intent["scopeDigest"] == self.scope_digest
                             and intent["definitionDigest"] == self.definition_digest
                             and intent["toolsDigest"] == self.tools.definition_digest
                             and intent["runStoreRootDigest"]
                             == contracts.digest(str(self.run_store.root)),
                             "signing_recovery_binding_invalid")
                    row = self.run_store.status(operation_id)
                    _require(row["requestDigest"] == request_digest
                             and row["state"] in {"admitted", "quarantined"},
                             "signing_recovery_binding_invalid")
                    actual_root = _stat_identity(os.fstat(operation_fd))
                    _require({key: value for key, value in actual_root.items()
                              if key != "links"}
                             == {key: value for key, value in
                                 intent["rootIdentity"].items()
                                 if key != "links"},
                             "signing_recovery_inode_changed")
                producer = _open_owned_regular(
                    operation_fd, "producer.lock",
                    expected=intent["producerIdentity"], writable=True,
                    zero=True)
                fcntl.flock(producer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_fds.append(producer)
                lock_checks.append(
                    (operation_fd, "producer.lock",
                     intent["producerIdentity"]))
                operation_names = set(os.listdir(operation_fd))
                expected_top = {"intent.json", "state.json", "producer.lock"}
                expected_top.update(mode for mode in state["phases"]
                                    if mode in operation_names)
                _require(operation_names == expected_top,
                         "signing_recovery_cleanup_unknown")
                phases_to_clean = []
                absent_phases = []
                for mode, phase_state in state["phases"].items():
                    _require(mode in _PHASES
                             and phase_state["definitionDigest"]
                             == self.tools.definition_digest,
                             "signing_recovery_record_invalid")
                    if mode not in operation_names:
                        _require(phase_state["state"] in {"cleaned", "exit-collected"},
                                 "signing_recovery_cleanup_unknown")
                        absent_phases.append(phase_state)
                        continue
                    phase_fd = _open_child_directory(operation_fd, mode)
                    directory_fds.append(phase_fd)
                    phase = _read_at(phase_fd, "phase.json")
                    _require(type(phase) is dict and set(phase) == {
                        "schemaVersion", "mode", "operationId", "requestDigest",
                        "contextDigest", "definitionDigest", "inputDigest",
                        "inputBytes", "maxBytes", "allowedFiles",
                        "directoryIdentity", "fileIdentities",
                        "outputExpectedAbsent"}
                             and phase["schemaVersion"] == 1
                             and phase["mode"] == mode
                             and phase["definitionDigest"] == self.tools.definition_digest
                             and phase["operationId"] == operation_id
                             and phase["requestDigest"] == request_digest
                             and phase["contextDigest"] == phase_state["contextDigest"]
                             and phase["inputDigest"] == phase_state["inputDigest"]
                             and phase["inputBytes"] == phase_state["inputBytes"]
                             and phase["maxBytes"] == MAX_TRANSFER_BYTES
                             and phase["outputExpectedAbsent"] is True
                             and phase["allowedFiles"] == sorted({
                                 "phase.json", "owner.lock", "start.json",
                                 "termination.json", APK_PATH, "signed.apk"})
                             and _valid_identity(
                                 phase["directoryIdentity"], directory=True)
                             and type(phase["fileIdentities"]) is dict
                             and set(phase["fileIdentities"]) == {
                                 "owner.lock", "start.json", "termination.json",
                                 APK_PATH}
                             and all(_valid_identity(value) for value in
                                     phase["fileIdentities"].values())
                             and _sha_at(phase_fd, "phase.json",
                                         MAX_RECORD_BYTES)
                             == phase_state["phaseDigest"]
                             and {key: value for key, value in
                                  _stat_identity(os.fstat(phase_fd)).items()
                                  if key != "links"}
                             == {key: value for key, value in
                                 phase["directoryIdentity"].items()
                                 if key != "links"},
                             "signing_recovery_binding_invalid")
                    phase_lock = _open_owned_regular(
                        phase_fd, "owner.lock",
                        expected=phase["fileIdentities"]["owner.lock"],
                        writable=True, zero=True)
                    fcntl.flock(phase_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_fds.append(phase_lock)
                    lock_checks.append((
                        phase_fd, "owner.lock",
                        phase["fileIdentities"]["owner.lock"]))
                    start_info = os.stat(
                        "start.json", dir_fd=phase_fd,
                        follow_symlinks=False)
                    termination_info = os.stat(
                        "termination.json", dir_fd=phase_fd,
                        follow_symlinks=False)
                    if start_info.st_size:
                        start = self._read_owner_record_at(
                            phase_fd, "start.json")
                        self._validate_owner_binding(
                            start, operation_id, request_digest,
                            phase["contextDigest"], {"started"}, "owner-start")
                    if termination_info.st_size:
                        termination = self._read_owner_record_at(
                            phase_fd, "termination.json")
                        self._validate_owner_binding(
                            termination, operation_id, request_digest,
                            phase["contextDigest"], {
                                "succeeded", "failed",
                                "parent-loss-exit-intent",
                                "ack-timeout-exit-intent"}, "exit-intent")
                    self._validate_phase_cleanup_fd(phase_fd, phase)
                    phases_to_clean.append(
                        (mode, phase_fd, phase, phase_state))
                # No durable state or private file is changed until every
                # recorded phase lock and exact cleanup set has been proven.
                for phase_state in absent_phases:
                    phase_state["state"] = "cleaned"
                for mode, phase_fd, phase, phase_state in phases_to_clean:
                    phase_state["state"] = "exit-collected"
                    self._replace_state(operation_id, state, operation_fd)
                    self._cleanup_phase_fd(
                        operation_fd, mode, phase_fd, phase)
                    phase_state["state"] = "cleaned"
                    self._replace_state(operation_id, state, operation_fd)
                state["recovery"] = "sanitized"
                self._replace_state(operation_id, state, operation_fd)
                evidence = contracts.digest({
                    "operationId": operation_id,
                    "requestDigest": request_digest,
                    "contextDigest": intent["initialContextDigest"],
                    "scopeDigest": self.scope_digest,
                    "definitionDigest": self.definition_digest,
                    "state": "sanitized-under-locks",
                })
                capability = SigningCleanupCapability(
                    operation_id, request_digest, intent["initialContextDigest"],
                    self.scope_digest, evidence, self._issuer, os.getpid(),
                    threading.get_ident(), tuple(lock_fds), tuple(lock_checks))
                with self._mutex:
                    _require(operation_id not in self._recoveries,
                             "signing_recovery_capability_invalid")
                    self._recoveries[operation_id] = capability
                try:
                    yield capability
                finally:
                    with self._mutex:
                        if self._recoveries.get(operation_id) is capability:
                            self._recoveries.pop(operation_id, None)
            except (OSError, BlockingIOError, RunDenied):
                raise SigningRecoveryError("signing_recovery_owner_live") from None
            finally:
                if capability is not None:
                    capability._active = False
                for descriptor in reversed(lock_fds):
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                        os.close(descriptor)
                    except OSError:
                        pass
                for descriptor in reversed(directory_fds):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                if vm_fd not in lock_fds:
                    os.close(vm_fd)

    @staticmethod
    def _locks_still_held(capability):
        if len(capability._lock_fds) != len(capability._lock_checks):
            return False
        probes = []
        try:
            for lock_fd, (parent_fd, name, expected) in zip(
                    capability._lock_fds, capability._lock_checks):
                actual = _stat_identity(os.fstat(lock_fd))
                if ({key: value for key, value in actual.items()
                     if key != "links"}
                        != {key: value for key, value in expected.items()
                            if key != "links"}):
                    return False
                # Exact phase cleanup unlinks owner.lock while retaining this
                # still-open locked inode through capability consumption.
                if actual["links"] == 0:
                    if name != "owner.lock" or expected["links"] != 1:
                        return False
                    continue
                if actual["links"] != expected["links"]:
                    return False
                probe = _open_owned_regular(
                    parent_fd, name, expected=expected, writable=True)
                probes.append(probe)
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                fcntl.flock(probe, fcntl.LOCK_UN)
                return False
            return True
        except (OSError, SigningRecoveryError):
            return False
        finally:
            for probe in probes:
                try:
                    os.close(probe)
                except OSError:
                    pass

    def require_cleanup(self, capability, run_store):
        _require(type(capability) is SigningCleanupCapability,
                 "signing_recovery_capability_invalid")
        try:
            row = self.run_store.status(capability.operation_id)
            operation_fd = next(
                parent for parent, name, _ in capability._lock_checks
                if name == "producer.lock")
        except (AttributeError, RunDenied, StopIteration):
            raise SigningRecoveryError(
                "signing_recovery_capability_invalid") from None
        with self._mutex:
            live = self._recoveries.get(capability.operation_id)
        _require(live is capability
                 and capability._issuer is self._issuer
                 and capability._active and not capability._consumed
                 and capability._pid == os.getpid()
                 and capability._thread == threading.get_ident()
                 and run_store is self.run_store
                 and capability.scope_digest == self.scope_digest
                 and row["requestDigest"] == capability.request_digest
                 and row["state"] in {"admitted", "quarantined"}
                 and self._locks_still_held(capability)
                 and self._state(
                     capability.operation_id,
                     operation_fd).get("recovery") == "sanitized",
                 "signing_recovery_capability_invalid")
        capability._consumed = True
        return capability

    def close(self, *, deadline_monotonic=None):
        deadline = (time.monotonic() + 5 if deadline_monotonic is None
                    else deadline_monotonic)
        _require(type(deadline) in (int, float),
                 "signing_recovery_configuration_invalid")
        # Seal future dispatch before waiting for a callback that may currently
        # be inside Popen while holding the registration mutex.
        self._closed = True
        if not self._mutex.acquire(
                timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            processes = tuple(self._processes.values())
            callbacks = tuple(self._callbacks)
        finally:
            self._mutex.release()
        for owned in processes:
            owned.close_liveness()
        for owned in processes:
            remaining = max(0, deadline - time.monotonic())
            if not remaining:
                break
            try:
                owned.process.wait(timeout=remaining)
            except (OSError, subprocess.TimeoutExpired):
                pass
        current = threading.current_thread()
        for callback in callbacks:
            if callback is current or not callback.is_alive():
                continue
            callback.join(max(0, deadline - time.monotonic()))
        if not self._mutex.acquire(
                timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            return not self._processes and not self._callbacks
        finally:
            self._mutex.release()


__all__ = [
    "SigningCleanupCapability", "SigningOperation", "SigningOperationStore",
    "SigningOwnerTools", "SigningRecoveryError",
]
