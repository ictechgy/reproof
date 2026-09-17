"""Bounded disposal of staged iOS native-operation files.

This module is deliberately narrower than device recovery.  A live native
owner and the callback token issued for its cleanup phase are the authority to
enter this function.  The durable records below only pin inode identities and
the disposal state; they never turn a copied JSON record into native authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
import weakref
from copy import deepcopy

from . import contracts
from .execution.wire import MAX_TRANSFER_BYTES, canonical
from .ios_mobile_callbacks import IOSNativeCallbackToken
from .ios_mobile_native import IOSMobileNativeOwner
from .ios_mobile_operation import IOSMobileOperationError
from .repair_android_operation import (
    _identity_info,
    _open_regular_at,
    _replace_at,
    _read_json_at,
    _same_identity,
    _valid_identity,
)


FINALIZATION_DIRECTORY = "native-finalization"
_RECORD_LIMIT = 512 * 1024
_MAX_GENERATED_ENTRIES = 100_000
_MAX_GENERATED_BYTES = 512 * 1024 * 1024
_ROLE_CHILDREN = frozenset(("input.ipa", "App.app", "transfer"))
_TRANSFER_CHILDREN = frozenset(("source.ipa", "app"))
_PHASE_NAMES = frozenset(("install", "cleanup", "replay-001", "replay-002", "replay-003"))
_COMMAND_NAME = re.compile(
    r"command-(?:install-candidate|restore-original|xctest-(?:candidate|original)-00[1-3])-work\Z"
)
_PRESERVED_WORK_FILES = frozenset(
    ("intent.json", "state.json", "native.json", "session.xctestrun", "stage.json", "helper-control")
)
_GENERATED_WORK_FILES = frozenset(
    ("result.json", "identity.json", "result.xcresult", ".native-session.xctestrun", "home")
)
_JOURNAL_FILES = frozenset(("intent.json", "state.json", "native.json", "ack.json"))
_FINALIZATION_TEMP = re.compile(r"\.native-record-[0-9a-f]{32}\Z")
_LIVE_FINALIZATIONS = weakref.WeakKeyDictionary()


class _RecoveryOwner:
    """Narrow adapter for file disposal; it never implements device dispatch."""

    def __init__(self, files):
        self._files = files
        self.operations = files._operations
        self._directory = files.directory
        self.binding_digest = files.binding_digest
        context = type("_RecoveryContext", (), {})()
        context.operation_id = files.operation_id
        context.request_digest = files.request_digest
        context.digest = files.context_digest
        operation = type("_RecoveryOperation", (), {})()
        operation.context = context
        self.operation = operation
        self._recovery_finalization = True

    def prepared_app_digest(self, role):
        self._files.require()
        _require(role in self.operations._roles,
                 "ios_native_finalization_binding")
        return self._files.native["preparedApps"][role]


class _RecoverySession:
    def __init__(self, files, cancellation, deadline):
        self.files = files
        self.cancellation = cancellation
        self.deadline = deadline

    def bounds(self):
        _require(time.monotonic() < self.deadline,
                 "ios_native_finalization_deadline")
        _require(not self.cancellation.is_set(),
                 "ios_native_finalization_cancelled")
        self.files.require()


def _prepared_app_digest(owner, role):
    if type(owner) is _RecoveryOwner:
        return owner.prepared_app_digest(role)
    from .ios_mobile_native import prepared_app
    prepared_app(owner, role)
    return json.loads(owner._record)["preparedApps"][role]


def _recorded_prepared_app_digest(owner, role):
    if type(owner) is _RecoveryOwner:
        owner._files.require()
        return owner._files.native["preparedApps"][role]
    return json.loads(owner._record)["preparedApps"][role]


class IOSNativeFinalizationError(IOSMobileOperationError):
    """Rejected, tampered, expired, or incomplete native-file disposal."""

    def __init__(self, code="ios_native_finalization_unavailable"):
        self.code = code
        super().__init__(code)


def _fail(code="ios_native_finalization_unavailable"):
    raise IOSNativeFinalizationError(code) from None


def _require(value, code="ios_native_finalization_unavailable"):
    if not value:
        _fail(code)


def _safe_identity(value, *, directory=False):
    """Validate a recorded identity, allowing normal read-only output modes."""
    if type(value) is not dict or set(value) != {"device", "inode", "mode", "uid", "links"}:
        return False
    if not all(type(item) is int for item in value.values()):
        return False
    if value["device"] < 0 or value["inode"] <= 0 or value["uid"] != os.getuid():
        return False
    if value["links"] < 1:
        return False
    mode = value["mode"]
    if directory:
        return mode & 0o022 == 0 and not mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
    return mode & 0o022 == 0 and not mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)


def _same_safe_identity(info, expected, *, directory=False):
    if not _safe_identity(expected, directory=directory):
        return False
    actual = _identity_info(info)
    if directory:
        actual.pop("links")
        expected = dict(expected)
        expected.pop("links")
    return actual == expected


def _same_recorded_identity(left, right, *, directory=False):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if directory:
        left = {key: value for key, value in left.items() if key != "links"}
        right = {key: value for key, value in right.items() if key != "links"}
    return left == right


def _record_write_new(parent, name, value):
    """Publish one of this module's records without widening Android names."""
    _require(name in {"intent.json", "state.json"})
    body = canonical(value)
    _require(len(body) <= _RECORD_LIMIT, "ios_native_finalization_record")
    temporary = ".native-record-" + secrets.token_hex(16)
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        offset = 0
        while offset < len(body):
            count = os.write(descriptor, body[offset:])
            _require(count > 0, "ios_native_finalization_record")
            offset += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.fsync(parent)
    except IOSNativeFinalizationError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError):
        _fail("ios_native_finalization_record")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
    os.fsync(parent)


def _record_replace(parent, name, value):
    _require(name in {"intent.json", "state.json"})
    body = canonical(value)
    _require(len(body) <= _RECORD_LIMIT, "ios_native_finalization_record")
    temporary = ".native-record-" + secrets.token_hex(16)
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        offset = 0
        while offset < len(body):
            count = os.write(descriptor, body[offset:])
            _require(count > 0, "ios_native_finalization_record")
            offset += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except IOSNativeFinalizationError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError):
        _fail("ios_native_finalization_record")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _stat_at(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        _fail("ios_native_finalization_storage")


def _node(parent, name, *, directory, expected=None, safe=False):
    info = _stat_at(parent, name)
    if info is None:
        return None
    if directory:
        _require(stat.S_ISDIR(info.st_mode), "ios_native_finalization_storage")
    else:
        _require(stat.S_ISREG(info.st_mode), "ios_native_finalization_storage")
        _require(info.st_nlink == 1, "ios_native_finalization_hardlink")
    _require(info.st_uid == os.getuid(), "ios_native_finalization_storage")
    if safe:
        _require(_safe_identity(_identity_info(info), directory=directory),
                 "ios_native_finalization_storage")
    elif directory:
        _require(stat.S_IMODE(info.st_mode) == 0o700, "ios_native_finalization_storage")
    else:
        _require(stat.S_IMODE(info.st_mode) in (0o600, 0o700),
                 "ios_native_finalization_storage")
    if expected is not None:
        if safe:
            _require(_same_safe_identity(info, expected, directory=directory),
                     "ios_native_finalization_binding")
        else:
            _require(_same_identity(info, expected, directory=directory),
                     "ios_native_finalization_binding")
    return info


def _open_directory(parent, name, expected, *, safe=False):
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
        info = os.fstat(descriptor)
        if safe:
            _require(_same_safe_identity(info, expected, directory=True),
                     "ios_native_finalization_binding")
        else:
            _require(_same_identity(info, expected, directory=True),
                     "ios_native_finalization_binding")
        return descriptor
    except IOSNativeFinalizationError:
        raise
    except (OSError, TypeError, ValueError):
        _fail("ios_native_finalization_storage")


def _hash_file(descriptor, session, maximum):
    before = os.fstat(descriptor)
    _require(
        stat.S_ISREG(before.st_mode)
        and before.st_uid == os.getuid()
        and before.st_nlink == 1
        and 0 < before.st_size <= maximum,
        "ios_native_finalization_archive",
    )
    digest = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        session.bounds()
        block = os.pread(descriptor, min(1024 * 1024, before.st_size - offset), offset)
        _require(block, "ios_native_finalization_archive")
        digest.update(block)
        offset += len(block)
    after = os.fstat(descriptor)
    _require(
        offset == before.st_size
        and (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        == (before.st_size, before.st_mtime_ns, before.st_ctime_ns),
        "ios_native_finalization_archive",
    )
    return digest.hexdigest(), offset


def _unlink(parent, name, expected, session, *, safe=False):
    session.bounds()
    info = _stat_at(parent, name)
    if info is None:
        return False
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1,
             "ios_native_finalization_storage")
    if safe:
        _require(_same_safe_identity(info, expected), "ios_native_finalization_binding")
    else:
        _require(_same_identity(info, expected), "ios_native_finalization_binding")
    os.unlink(name, dir_fd=parent)
    os.fsync(parent)
    return True


def _remove_tree(directory, session, *, expected_files=None, expected_dirs=None,
                 prefix="", depth=0, generated=False, remove=True, budget=None):
    """Validate and optionally remove one bounded private tree.

    Missing expected nodes are allowed because a deadline may have interrupted
    a previous attempt.  Any present node must remain a regular owned node in
    the archive-derived tree (or in a known generated-results tree).
    """
    _require(depth <= 512, "ios_native_finalization_tree")
    if budget is None:
        budget = {"entries": 0, "bytes": 0}
    for name in os.listdir(directory):
        session.bounds()
        budget["entries"] += 1
        _require(budget["entries"] <= _MAX_GENERATED_ENTRIES, "ios_native_finalization_tree")
        relative = name if not prefix else prefix + "/" + name
        info = _stat_at(directory, name)
        _require(info is not None and info.st_uid == os.getuid(), "ios_native_finalization_storage")
        if stat.S_ISDIR(info.st_mode):
            if generated:
                _require(_safe_identity(_identity_info(info), directory=True),
                         "ios_native_finalization_storage")
            else:
                _require(relative in expected_dirs and stat.S_IMODE(info.st_mode) == 0o700,
                         "ios_native_finalization_tree")
            child = None
            try:
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
                actual = os.fstat(child)
                if generated:
                    _require(_same_safe_identity(actual, _identity_info(info), directory=True),
                             "ios_native_finalization_binding")
                else:
                    _require(_same_identity(actual, _identity_info(info), directory=True),
                             "ios_native_finalization_binding")
                _remove_tree(
                    child,
                    session,
                    expected_files=expected_files,
                    expected_dirs=expected_dirs,
                    prefix=relative,
                    depth=depth + 1,
                    generated=generated,
                    remove=remove,
                    budget=budget,
                )
            finally:
                if child is not None:
                    os.close(child)
            if generated:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                _require(_same_safe_identity(current, _identity_info(info), directory=True),
                         "ios_native_finalization_binding")
            else:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                _require(_same_identity(current, _identity_info(info), directory=True),
                         "ios_native_finalization_binding")
            if remove:
                session.bounds()
                os.rmdir(name, dir_fd=directory)
                os.fsync(directory)
        else:
            if generated:
                _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                         and _safe_identity(_identity_info(info)),
                         "ios_native_finalization_storage")
            else:
                _require(
                    relative in expected_files
                    and stat.S_ISREG(info.st_mode)
                    and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) in (0o600, 0o700)
                    and info.st_size == expected_files[relative],
                    "ios_native_finalization_tree",
                )
            budget["bytes"] += info.st_size
            _require(budget["bytes"] <= _MAX_GENERATED_BYTES, "ios_native_finalization_tree")
            if generated:
                _require(_safe_identity(_identity_info(info)), "ios_native_finalization_storage")
            _require(
                _same_safe_identity(info, _identity_info(info)) if generated else True,
                "ios_native_finalization_binding",
            )
            current = _stat_at(directory, name)
            _require(current is not None and _same_safe_identity(current, _identity_info(info)),
                     "ios_native_finalization_binding")
            if remove:
                session.bounds()
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)


def _expected_tree(archive_fd, archive_path):
    from .ios_mobile_recovery import _expected_tree as preparation_expected_tree

    try:
        return preparation_expected_tree(archive_fd, archive_path)
    except (IOSMobileOperationError, OSError, RuntimeError, TypeError, ValueError, KeyError,
            contracts.ContractError):
        _fail("ios_native_finalization_archive")


def _recorded_node(parent, name, expected, *, directory, safe=False):
    current = _node(parent, name, directory=directory, expected=expected, safe=safe)
    if expected is None:
        _require(current is None, "ios_native_finalization_binding")
    return current


def _validate_preserved_journal_tree(directory, session, *, depth=0):
    _require(depth <= 16, "ios_native_finalization_journal")
    names = set(os.listdir(directory))
    _require(names <= _JOURNAL_FILES, "ios_native_finalization_journal")
    for name in names:
        info = _stat_at(directory, name)
        _require(info is not None and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                 and _safe_identity(_identity_info(info)), "ios_native_finalization_journal")
        _require(info.st_size <= _RECORD_LIMIT, "ios_native_finalization_journal")
        session.bounds()


def _command_record(parent, name, session):
    info = _node(parent, name, directory=True, safe=True)
    _require(info is not None, "ios_native_finalization_command")
    work = _open_directory(parent, name, _identity_info(info), safe=True)
    try:
        names = set(os.listdir(work))
        _require(names <= _PRESERVED_WORK_FILES | _GENERATED_WORK_FILES,
                 "ios_native_finalization_command")
        generated = {}
        for item in sorted(_GENERATED_WORK_FILES):
            child = _stat_at(work, item)
            if child is None:
                generated[item] = None
                continue
            if item == "home" or item == "result.xcresult":
                _require(stat.S_ISDIR(child.st_mode), "ios_native_finalization_command")
                _require(_safe_identity(_identity_info(child), directory=True),
                         "ios_native_finalization_command")
                child_fd = os.open(item, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=work)
                try:
                    _remove_tree(child_fd, session, generated=True, remove=False)
                finally:
                    os.close(child_fd)
                # _remove_tree above only validates.  It intentionally does
                # not mutate during journal creation, so the output remains.
            else:
                _require(stat.S_ISREG(child.st_mode) and child.st_nlink == 1
                         and _safe_identity(_identity_info(child)),
                         "ios_native_finalization_command")
                _require(child.st_size <= _MAX_GENERATED_BYTES,
                         "ios_native_finalization_command")
            generated[item] = _identity_info(child)
        preserved = {}
        for item in sorted(names - set(_GENERATED_WORK_FILES) - {"helper-control"}):
            child = _stat_at(work, item)
            _require(child is not None and stat.S_ISREG(child.st_mode) and child.st_nlink == 1
                     and _safe_identity(_identity_info(child)), "ios_native_finalization_journal")
            _require(child.st_size <= _RECORD_LIMIT, "ios_native_finalization_journal")
            preserved[item] = _identity_info(child)
        if "helper-control" in names:
            child = os.open("helper-control", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=work)
            try:
                helper_identity = _identity_info(os.fstat(child))
                _require(_safe_identity(helper_identity, directory=True),
                         "ios_native_finalization_journal")
                _validate_helper_control(child, session)
                preserved["helper-control"] = helper_identity
            finally:
                os.close(child)
        return {"directoryIdentity": _identity_info(info), "generated": generated,
                "preserved": preserved}
    finally:
        os.close(work)


def _validate_helper_control(directory, session, *, depth=0):
    _require(depth <= 8, "ios_native_finalization_journal")
    for name in os.listdir(directory):
        session.bounds()
        info = _stat_at(directory, name)
        _require(info is not None and info.st_uid == os.getuid(),
                 "ios_native_finalization_journal")
        if stat.S_ISDIR(info.st_mode):
            _require(name.startswith("op-") and len(name) <= 127
                     and _safe_identity(_identity_info(info), directory=True),
                     "ios_native_finalization_journal")
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            try:
                _validate_preserved_journal_tree(child, session, depth=depth + 1)
            finally:
                os.close(child)
        else:
            _require(name in _JOURNAL_FILES and stat.S_ISREG(info.st_mode)
                     and info.st_nlink == 1 and info.st_size <= _RECORD_LIMIT
                     and _safe_identity(_identity_info(info)),
                     "ios_native_finalization_journal")


def _phase_record(parent, session):
    phase = _stat_at(parent, "phases")
    if phase is None:
        return None
    _require(stat.S_ISDIR(phase.st_mode) and stat.S_IMODE(phase.st_mode) == 0o700,
             "ios_native_finalization_journal")
    directory = os.open("phases", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent)
    try:
        rows = {}
        for name in os.listdir(directory):
            session.bounds()
            _require(name in _PHASE_NAMES, "ios_native_finalization_journal")
            child = _stat_at(directory, name)
            _require(child is not None and stat.S_ISDIR(child.st_mode)
                     and stat.S_IMODE(child.st_mode) == 0o700,
                     "ios_native_finalization_journal")
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=directory)
            try:
                _validate_preserved_journal_tree(child_fd, session)
            finally:
                os.close(child_fd)
            rows[name] = {"directoryIdentity": _identity_info(child)}
        return {"directoryIdentity": _identity_info(phase), "entries": rows}
    finally:
        os.close(directory)


def _validate_phase_record(parent, expected, session):
    phase = _stat_at(parent, "phases")
    if expected is None:
        _require(phase is None, "ios_native_finalization_journal")
        return
    _require(phase is not None and _same_identity(phase, expected["directoryIdentity"], directory=True),
             "ios_native_finalization_journal")
    directory = os.open("phases", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent)
    try:
        _require(set(os.listdir(directory)) == set(expected["entries"]),
                 "ios_native_finalization_journal")
        for name, row in expected["entries"].items():
            session.bounds()
            child = _stat_at(directory, name)
            _require(child is not None
                     and _same_identity(child, row["directoryIdentity"], directory=True),
                     "ios_native_finalization_journal")
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=directory)
            try:
                _validate_preserved_journal_tree(child_fd, session)
            finally:
                os.close(child_fd)
    finally:
        os.close(directory)


class _Session:
    def __init__(self, owner, token, cancellation, deadline):
        self.owner = owner
        self.token = token
        self.cancellation = cancellation
        self.deadline = deadline
        self.operation_id = owner.operation.context.operation_id
        self.root_identity = None

    def bounds(self):
        _require(time.monotonic() < self.deadline, "ios_native_finalization_deadline")
        _require(not self.cancellation.is_set(), "ios_native_finalization_cancelled")
        try:
            self.token.require()
        except IOSNativeFinalizationError:
            raise
        except BaseException:
            _fail("ios_native_finalization_capability")
        owner = self.owner
        store = owner.operations
        with store._changed:
            _require(not store._closed and store._native_owners.get(id(owner)) is owner,
                     "ios_native_finalization_capability")
            coordinator = self.token._coordinator
            _require(coordinator._active_token is self.token,
                     "ios_native_finalization_capability")
            _require(coordinator._resources_idle_locked(), "ios_native_finalization_busy")
            _require(not owner._command_lock.locked(), "ios_native_finalization_busy")
            for client in tuple(store._native_clients):
                if getattr(client, "native_owner", None) is not owner:
                    continue
                try:
                    active = client.active_processes
                except BaseException:
                    _fail("ios_native_finalization_busy")
                _require(type(active) is int and active == 0,
                         "ios_native_finalization_busy")
            _require(not any(getattr(item, "_owner", None) is owner
                             and getattr(item, "_active", False)
                             for item in tuple(store._native_exports.values())),
                     "ios_native_finalization_busy")
        owner._check()


def _validate_token(native_owner, cleanup_token):
    _require(type(native_owner) is IOSMobileNativeOwner,
             "ios_native_finalization_capability")
    _require(type(cleanup_token) is IOSNativeCallbackToken,
             "ios_native_finalization_capability")
    coordinator = cleanup_token._coordinator
    _require(coordinator.owner is native_owner and coordinator.operation is native_owner.operation,
             "ios_native_finalization_capability")
    _require(cleanup_token._operation is native_owner.operation
             and cleanup_token._phase == "cleanup"
             and coordinator._active_token is cleanup_token
             and cleanup_token._issuer is coordinator._token_issuer
             and cleanup_token._outcome is None,
             "ios_native_finalization_capability")
    cleanup_token.require()


def _role_initial_record(owner, role, session):
    root = _open_directory(owner._directory, role,
                           owner.operations._records(owner.operation.context.operation_id,
                                                    owner._directory)[0]["roles"][role]["directoryIdentity"])
    transfer = archive = None
    try:
        names = set(os.listdir(root))
        _require(names <= _ROLE_CHILDREN, "ios_native_finalization_unknown")
        transfer_info = _node(root, "transfer", directory=True)
        _require(transfer_info is not None, "ios_native_finalization_storage")
        transfer = _open_directory(root, "transfer", _identity_info(transfer_info))
        transfer_names = set(os.listdir(transfer))
        _require(transfer_names <= _TRANSFER_CHILDREN, "ios_native_finalization_unknown")
        archive_info = _node(root, "input.ipa", directory=False)
        app_info = _node(root, "App.app", directory=True)
        source_info = _node(transfer, "source.ipa", directory=False)
        transfer_app_info = _node(transfer, "app", directory=True)
        _require(archive_info is not None and app_info is not None and source_info is not None,
                 "ios_native_finalization_storage")
        archive = _open_regular_at(root, "input.ipa",
                                   expected=_identity_info(archive_info))
        archive_digest, archive_bytes = _hash_file(archive, session, MAX_TRANSFER_BYTES)
        files, directories = _expected_tree(
            archive,
            owner.operations._operation_root(owner.operation.context.operation_id) / role / "input.ipa",
        )
        app = os.open("App.app", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            _remove_tree(app, session, expected_files=files, expected_dirs=directories,
                          generated=False, remove=False)
        finally:
            os.close(app)
        app_digest = _prepared_app_digest(owner, role)
        if transfer_app_info is not None:
            transferred = os.open("app", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=transfer)
            try:
                _remove_tree(transferred, session, expected_files=files, expected_dirs=directories,
                             generated=False, remove=False)
            finally:
                os.close(transferred)
        source = _open_regular_at(transfer, "source.ipa", expected=_identity_info(source_info))
        try:
            source_digest, source_bytes = _hash_file(source, session, MAX_TRANSFER_BYTES)
        finally:
            os.close(source)
        _require((source_digest, source_bytes) == (archive_digest, archive_bytes),
                 "ios_native_finalization_archive")
        return {
            "directoryIdentity": _identity_info(os.fstat(root)),
            "transferIdentity": _identity_info(os.fstat(transfer)),
            "archiveIdentity": _identity_info(archive_info),
            "appIdentity": _identity_info(app_info),
            "transferAppIdentity": None if transfer_app_info is None else _identity_info(transfer_app_info),
            "snapshotIdentity": _identity_info(source_info),
            "archiveDigest": archive_digest,
            "archiveBytes": archive_bytes,
            "appDigest": app_digest,
        }
    finally:
        if archive is not None:
            os.close(archive)
        if transfer is not None:
            os.close(transfer)
        os.close(root)


def _role_record(owner, role, session, record):
    root = _open_directory(owner._directory, role, record["directoryIdentity"])
    transfer = None
    try:
        names = set(os.listdir(root))
        _require(names <= _ROLE_CHILDREN, "ios_native_finalization_unknown")
        _recorded_node(root, "input.ipa", record["archiveIdentity"], directory=False)
        _recorded_node(root, "App.app", record["appIdentity"], directory=True)
        transfer_info = _recorded_node(root, "transfer", record["transferIdentity"], directory=True)
        _require(transfer_info is not None, "ios_native_finalization_storage")
        transfer = _open_directory(root, "transfer", record["transferIdentity"])
        transfer_names = set(os.listdir(transfer))
        _require(transfer_names <= _TRANSFER_CHILDREN, "ios_native_finalization_unknown")
        _recorded_node(transfer, "source.ipa", record["snapshotIdentity"], directory=False)
        _recorded_node(transfer, "app", record["transferAppIdentity"], directory=True)
        return root, transfer
    except BaseException:
        if transfer is not None:
            os.close(transfer)
        os.close(root)
        raise


def _dispose_role(owner, role, record, session):
    root, transfer = _role_record(owner, role, session, record)
    archive = None
    try:
        archive_info = _stat_at(root, "input.ipa")
        app_info = _stat_at(root, "App.app")
        transfer_app_info = _stat_at(transfer, "app")
        source_info = _stat_at(transfer, "source.ipa")
        if app_info is not None or transfer_app_info is not None:
            _require(archive_info is not None, "ios_native_finalization_archive")
            archive = _open_regular_at(root, "input.ipa", expected=record["archiveIdentity"])
            digest, size = _hash_file(archive, session, MAX_TRANSFER_BYTES)
            _require((digest, size) == (record["archiveDigest"], record["archiveBytes"]),
                     "ios_native_finalization_archive")
            files, directories = _expected_tree(
                archive,
                owner.operations._operation_root(owner.operation.context.operation_id) / role / "input.ipa",
            )
            if app_info is not None:
                app = os.open("App.app", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=root)
                try:
                    _remove_tree(app, session, expected_files=files, expected_dirs=directories,
                                  generated=False)
                finally:
                    os.close(app)
                session.bounds()
                current = _stat_at(root, "App.app")
                _require(current is not None and _same_identity(current, record["appIdentity"], directory=True),
                         "ios_native_finalization_binding")
                os.rmdir("App.app", dir_fd=root)
                os.fsync(root)
            if transfer_app_info is not None:
                transferred = os.open("app", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=transfer)
                try:
                    _remove_tree(transferred, session, expected_files=files, expected_dirs=directories,
                                 generated=False)
                finally:
                    os.close(transferred)
                session.bounds()
                current = _stat_at(transfer, "app")
                _require(current is not None and _same_identity(
                    current, record["transferAppIdentity"], directory=True),
                    "ios_native_finalization_binding")
                os.rmdir("app", dir_fd=transfer)
                os.fsync(transfer)
        _require(_stat_at(root, "App.app") is None and _stat_at(transfer, "app") is None,
                 "ios_native_finalization_tree")
        if source_info is not None:
            source = _open_regular_at(transfer, "source.ipa", expected=record["snapshotIdentity"])
            try:
                digest, size = _hash_file(source, session, MAX_TRANSFER_BYTES)
            finally:
                os.close(source)
            _require((digest, size) == (record["archiveDigest"], record["archiveBytes"]),
                     "ios_native_finalization_archive")
            _unlink(transfer, "source.ipa", record["snapshotIdentity"], session)
        _require(not os.listdir(transfer), "ios_native_finalization_unknown")
        if archive_info is not None:
            _require(_stat_at(root, "App.app") is None and not os.listdir(transfer),
                     "ios_native_finalization_tree")
            archive = archive or _open_regular_at(root, "input.ipa", expected=record["archiveIdentity"])
            try:
                digest, size = _hash_file(archive, session, MAX_TRANSFER_BYTES)
            finally:
                if archive is not None:
                    os.close(archive)
                    archive = None
            _require((digest, size) == (record["archiveDigest"], record["archiveBytes"]),
                     "ios_native_finalization_archive")
            _unlink(root, "input.ipa", record["archiveIdentity"], session)
    finally:
        if archive is not None:
            os.close(archive)
        os.close(transfer)
        os.close(root)


def _dispose_commands(owner, records, session, *, remove=True):
    for name in sorted(records):
        session.bounds()
        row = records[name]
        work = _open_directory(owner._directory, name, row["directoryIdentity"], safe=True)
        try:
            names = set(os.listdir(work))
            _require(names <= _PRESERVED_WORK_FILES | _GENERATED_WORK_FILES,
                     "ios_native_finalization_unknown")
            for item in sorted(_GENERATED_WORK_FILES):
                expected = row["generated"][item]
                info = _stat_at(work, item)
                if info is None:
                    continue
                _require(expected is not None, "ios_native_finalization_binding")
                is_directory = item in {"home", "result.xcresult"}
                if is_directory:
                    _require(stat.S_ISDIR(info.st_mode), "ios_native_finalization_command")
                    _require(_same_safe_identity(info, expected, directory=True),
                             "ios_native_finalization_binding")
                    child = os.open(item, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=work)
                    try:
                        _remove_tree(child, session, generated=True, remove=remove)
                    finally:
                        os.close(child)
                    session.bounds()
                    _require(_same_safe_identity(os.stat(item, dir_fd=work, follow_symlinks=False),
                                                 expected, directory=True),
                             "ios_native_finalization_binding")
                    if remove:
                        os.rmdir(item, dir_fd=work)
                        os.fsync(work)
                else:
                    if remove:
                        _unlink(work, item, expected, session, safe=True)
            for item, expected in row["preserved"].items():
                info = _stat_at(work, item)
                _require(info is not None, "ios_native_finalization_journal")
                if item == "helper-control":
                    _require(stat.S_ISDIR(info.st_mode)
                             and _same_safe_identity(info, expected, directory=True),
                             "ios_native_finalization_journal")
                    child = os.open(item, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=work)
                    try:
                        _validate_helper_control(child, session)
                    finally:
                        os.close(child)
                else:
                    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                             and _same_safe_identity(info, expected),
                             "ios_native_finalization_journal")
            expected_names = set(row["preserved"])
            if not remove:
                for item, expected in row["generated"].items():
                    if expected is not None and _stat_at(work, item) is not None:
                        expected_names.add(item)
            _require(set(os.listdir(work)) == expected_names,
                     "ios_native_finalization_unknown")
        finally:
            os.close(work)


def _initial_intent(owner, session, *, finalization_directory_present=False):
    operation_root = owner._directory
    root_info = os.fstat(operation_root)
    _require(_valid_identity(_identity_info(root_info), directory=True),
             "ios_native_finalization_binding")
    names = set(os.listdir(operation_root))
    roles = tuple(owner.operations._roles)
    allowed = {"intent.json", "state.json", "producer.lock", "native.json", "phases"}
    if finalization_directory_present:
        allowed.add(FINALIZATION_DIRECTORY)
    if type(owner) is _RecoveryOwner:
        allowed.update({"native-recovery", "finalization.json"})
    allowed.update(roles)
    command_names = set()
    for name in names:
        if name in allowed:
            continue
        if _COMMAND_NAME.fullmatch(name):
            command_names.add(name)
            allowed.add(name)
            continue
        _fail("ios_native_finalization_unknown")
    role_records = {}
    for role in roles:
        session.bounds()
        role_records[role] = _role_initial_record(owner, role, session)
    phase = _phase_record(operation_root, session)
    commands = {}
    for name in sorted(command_names):
        commands[name] = _command_record(operation_root, name, session)
    return {
        "schemaVersion": 1,
        "kind": "ios-native-finalization-v1",
        "operationId": owner.operation.context.operation_id,
        "requestDigest": owner.operation.context.request_digest,
        "contextDigest": owner.operation.context.digest,
        "configurationDigest": owner.operations.configuration_digest,
        "scopeDigest": owner.operations.definition.scope_digest,
        "nativeBindingDigest": owner.binding_digest,
        "operationDirectoryIdentity": _identity_info(root_info),
        "roles": role_records,
        "commands": commands,
        "phases": phase,
    }


def _preflight_roles(owner, intent, session):
    """Revalidate every role before a resumed attempt mutates one of them."""
    for role, record in intent["roles"].items():
        session.bounds()
        root, transfer = _role_record(owner, role, session, record)
        archive = None
        try:
            archive_info = _stat_at(root, "input.ipa")
            app_info = _stat_at(root, "App.app")
            transfer_app_info = _stat_at(transfer, "app")
            source_info = _stat_at(transfer, "source.ipa")
            if archive_info is None:
                _require(app_info is None and transfer_app_info is None and source_info is None,
                         "ios_native_finalization_archive")
                continue
            archive = _open_regular_at(root, "input.ipa", expected=record["archiveIdentity"])
            digest, size = _hash_file(archive, session, MAX_TRANSFER_BYTES)
            _require((digest, size) == (record["archiveDigest"], record["archiveBytes"]),
                     "ios_native_finalization_archive")
            if app_info is not None or transfer_app_info is not None:
                files, directories = _expected_tree(
                    archive,
                    owner.operations._operation_root(owner.operation.context.operation_id)
                    / role / "input.ipa",
                )
                if app_info is not None:
                    app = os.open("App.app", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=root)
                    try:
                        _remove_tree(app, session, expected_files=files,
                                      expected_dirs=directories, remove=False)
                    finally:
                        os.close(app)
                if transfer_app_info is not None:
                    transferred = os.open("app", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                         dir_fd=transfer)
                    try:
                        _remove_tree(transferred, session, expected_files=files,
                                     expected_dirs=directories, remove=False)
                    finally:
                        os.close(transferred)
            if source_info is not None:
                source = _open_regular_at(transfer, "source.ipa",
                                          expected=record["snapshotIdentity"])
                try:
                    source_digest, source_bytes = _hash_file(source, session, MAX_TRANSFER_BYTES)
                finally:
                    os.close(source)
                _require((source_digest, source_bytes) == (digest, size),
                         "ios_native_finalization_archive")
        finally:
            if archive is not None:
                os.close(archive)
            os.close(transfer)
            os.close(root)


def _validate_intent(owner, session, intent, finalization_identity):
    roles = tuple(owner.operations._roles)
    required = {
        "schemaVersion", "kind", "operationId", "requestDigest", "contextDigest",
        "configurationDigest", "scopeDigest", "nativeBindingDigest",
        "operationDirectoryIdentity", "finalizationDirectoryIdentity", "roles", "commands", "phases",
    }
    _require(type(intent) is dict and set(intent) == required
             and intent["schemaVersion"] == 1
             and intent["kind"] == "ios-native-finalization-v1"
             and intent["operationId"] == owner.operation.context.operation_id
             and intent["requestDigest"] == owner.operation.context.request_digest
             and intent["contextDigest"] == owner.operation.context.digest
             and intent["configurationDigest"] == owner.operations.configuration_digest
             and intent["scopeDigest"] == owner.operations.definition.scope_digest
             and intent["nativeBindingDigest"] == owner.binding_digest
             and set(intent["roles"]) == set(roles)
             and type(intent["commands"]) is dict
             and (intent["phases"] is None or type(intent["phases"]) is dict),
             "ios_native_finalization_binding")
    for key in ("requestDigest", "contextDigest", "configurationDigest", "scopeDigest",
                "nativeBindingDigest"):
        contracts.validate_digest(intent[key])
    _require(_valid_identity(intent["operationDirectoryIdentity"], directory=True),
             "ios_native_finalization_binding")
    _require(_valid_identity(intent["finalizationDirectoryIdentity"], directory=True),
             "ios_native_finalization_binding")
    _require(_same_identity(os.fstat(owner._directory), intent["operationDirectoryIdentity"], directory=True),
             "ios_native_finalization_binding")
    live_intent = _LIVE_FINALIZATIONS.get(owner)
    _require(type(live_intent) is dict and live_intent == intent,
             "ios_native_finalization_capability")
    _require(_same_safe_identity(os.fstat(finalization_identity[0]), intent["finalizationDirectoryIdentity"],
                                 directory=True), "ios_native_finalization_binding")
    operation_intent, _operation_state = owner.operations._records(
        owner.operation.context.operation_id, owner._directory
    )
    for role in roles:
        row = intent["roles"][role]
        source = operation_intent["roles"][role]
        _require(type(row) is dict and set(row) == {
            "directoryIdentity", "transferIdentity", "archiveIdentity", "appIdentity",
            "transferAppIdentity", "snapshotIdentity", "archiveDigest", "archiveBytes", "appDigest",
        }, "ios_native_finalization_binding")
        _require(
            _same_recorded_identity(row["directoryIdentity"], source["directoryIdentity"], directory=True)
            and _same_recorded_identity(row["transferIdentity"], source["transferIdentity"], directory=True)
            and row["archiveIdentity"] == source["archiveIdentity"]
            and row["archiveDigest"] == source["sha256"]
            and row["archiveBytes"] == source["bytes"],
            "ios_native_finalization_binding",
        )
        _require(row["appDigest"] == _recorded_prepared_app_digest(owner, role),
                 "ios_native_finalization_binding")
        for key in ("directoryIdentity", "transferIdentity", "archiveIdentity", "appIdentity", "snapshotIdentity"):
            _require(_valid_identity(row[key], directory=key in {"directoryIdentity", "transferIdentity", "appIdentity"}),
                     "ios_native_finalization_binding")
        if row["transferAppIdentity"] is not None:
            _require(_safe_identity(row["transferAppIdentity"], directory=True),
                     "ios_native_finalization_binding")
        contracts.validate_digest(row["archiveDigest"])
        contracts.validate_digest(row["appDigest"])
        _require(type(row["archiveBytes"]) is int and 0 < row["archiveBytes"] <= MAX_TRANSFER_BYTES,
                 "ios_native_finalization_binding")
    for name, row in intent["commands"].items():
        _require(_COMMAND_NAME.fullmatch(name) is not None and type(row) is dict
                 and set(row) == {"directoryIdentity", "generated", "preserved"},
                 "ios_native_finalization_binding")
        _require(_safe_identity(row["directoryIdentity"], directory=True),
                 "ios_native_finalization_binding")
        _require(set(row["generated"]) == set(_GENERATED_WORK_FILES)
                 and type(row["preserved"]) is dict
                 and set(row["preserved"]) <= _PRESERVED_WORK_FILES,
                 "ios_native_finalization_binding")
        for item, identity in row["generated"].items():
            if identity is not None:
                _require(_safe_identity(identity, directory=item in {"home", "result.xcresult"}),
                         "ios_native_finalization_binding")
                if item not in {"home", "result.xcresult"}:
                    _require(identity["links"] == 1, "ios_native_finalization_hardlink")
        for item, identity in row["preserved"].items():
            _require(_safe_identity(identity, directory=item == "helper-control"),
                     "ios_native_finalization_binding")
            if item != "helper-control":
                _require(identity["links"] == 1, "ios_native_finalization_hardlink")
    if intent["phases"] is not None:
        phase = intent["phases"]
        _require(type(phase) is dict and set(phase) == {"directoryIdentity", "entries"}
                 and _valid_identity(phase["directoryIdentity"], directory=True)
                 and type(phase["entries"]) is dict,
                 "ios_native_finalization_journal")
        for name, row in phase["entries"].items():
            _require(name in _PHASE_NAMES and type(row) is dict
                     and set(row) == {"directoryIdentity"}
                     and _valid_identity(row["directoryIdentity"], directory=True),
                     "ios_native_finalization_journal")


def _state(owner, intent, value, *, evidence_digest=None):
    result = {
        "schemaVersion": 1,
        "kind": "ios-native-finalization-state",
        "operationId": owner.operation.context.operation_id,
        "requestDigest": owner.operation.context.request_digest,
        "contextDigest": owner.operation.context.digest,
        "nativeBindingDigest": owner.binding_digest,
        "intentDigest": contracts.digest(intent),
        "state": value,
    }
    if evidence_digest is not None:
        result["evidenceDigest"] = evidence_digest
    return result


def _validate_state(owner, intent, state):
    _require(type(state) is dict and set(state) in ({
        "schemaVersion", "kind", "operationId", "requestDigest", "contextDigest",
        "nativeBindingDigest", "intentDigest", "state",
    }, {
        "schemaVersion", "kind", "operationId", "requestDigest", "contextDigest",
        "nativeBindingDigest", "intentDigest", "state", "evidenceDigest",
    }), "ios_native_finalization_record")
    expected = _state(owner, intent, state["state"],
                      evidence_digest=state.get("evidenceDigest"))
    _require(state == expected and state["state"] in {"discarding", "discarded"},
             "ios_native_finalization_record")
    if state["state"] == "discarded":
        contracts.validate_digest(state.get("evidenceDigest"))


def _evidence_digest(intent):
    return contracts.digest({
        "schemaVersion": 1,
        "kind": "ios-native-finalization-evidence-v1",
        "operationId": intent["operationId"],
        "requestDigest": intent["requestDigest"],
        "contextDigest": intent["contextDigest"],
        "nativeBindingDigest": intent["nativeBindingDigest"],
        "intentDigest": contracts.digest(intent),
        "state": "discarded",
    })


def _assert_empty_large_data(owner, intent, session):
    root_names = set(os.listdir(owner._directory))
    allowed = {"intent.json", "state.json", "producer.lock", "native.json", "phases",
               FINALIZATION_DIRECTORY, *owner.operations._roles, *intent["commands"]}
    if type(owner) is _RecoveryOwner:
        allowed.update({"native-recovery", "finalization.json"})
    _require(root_names <= allowed, "ios_native_finalization_unknown")
    _validate_phase_record(owner._directory, intent["phases"], session)
    for role, row in intent["roles"].items():
        root = _open_directory(owner._directory, role, row["directoryIdentity"])
        transfer = None
        try:
            _require(set(os.listdir(root)) == {"transfer"}
                     and _stat_at(root, "input.ipa") is None
                     and _stat_at(root, "App.app") is None,
                     "ios_native_finalization_incomplete")
            transfer = _open_directory(root, "transfer", row["transferIdentity"])
            _require(not os.listdir(transfer), "ios_native_finalization_incomplete")
        finally:
            if transfer is not None:
                os.close(transfer)
            os.close(root)
    _dispose_commands(owner, intent["commands"], session, remove=False)


def discard_native_staged(native_owner, cleanup_token, *, cancellation, deadline_monotonic):
    """Dispose iOS staged files under one live cleanup callback capability.

    The function only consumes private staged data.  Device sanitation,
    restore observations, leases, and RunStore reservation accounting remain
    with the fixed native adapter and its exact owner capability.
    """
    final_fd = None
    try:
        _require(callable(getattr(cancellation, "is_set", None)),
                 "ios_native_finalization_bounds")
        _require(type(deadline_monotonic) in (int, float)
                 and not isinstance(deadline_monotonic, bool)
                 and math.isfinite(deadline_monotonic)
                 and time.monotonic() < deadline_monotonic,
                 "ios_native_finalization_deadline")
        if type(native_owner) is _RecoveryOwner:
            _require(type(cleanup_token) is _RecoverySession
                     and cleanup_token.files is native_owner._files,
                     "ios_native_finalization_capability")
            session = cleanup_token
        else:
            _validate_token(native_owner, cleanup_token)
            session = _Session(native_owner, cleanup_token, cancellation, deadline_monotonic)
        session.bounds()
        operation_root = native_owner._directory
        final = _stat_at(operation_root, FINALIZATION_DIRECTORY)
        intent = None
        if final is None:
            intent = _initial_intent(native_owner, session)
            session.bounds()
            try:
                os.mkdir(FINALIZATION_DIRECTORY, mode=0o700, dir_fd=operation_root)
                os.fsync(operation_root)
            except FileExistsError:
                final = _stat_at(operation_root, FINALIZATION_DIRECTORY)
            if final is None:
                final = _stat_at(operation_root, FINALIZATION_DIRECTORY)
            _require(final is not None and stat.S_ISDIR(final.st_mode)
                     and stat.S_IMODE(final.st_mode) == 0o700,
                     "ios_native_finalization_storage")
            final_fd = os.open(FINALIZATION_DIRECTORY,
                               os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                               dir_fd=operation_root)
            final_identity = _identity_info(os.fstat(final_fd))
            intent["finalizationDirectoryIdentity"] = final_identity
            # Publish the full immutable intent in the same-owner process-local
            # registry before either durable record.  A failure after mkdir,
            # link, or fsync can then resume only through this exact owner;
            # copied JSON never acquires disposal authority.
            _LIVE_FINALIZATIONS[native_owner] = deepcopy(intent)
            _record_write_new(final_fd, "intent.json", intent)
            _record_write_new(final_fd, "state.json", _state(native_owner, intent, "discarding"))
        else:
            _require(stat.S_ISDIR(final.st_mode) and stat.S_IMODE(final.st_mode) == 0o700,
                     "ios_native_finalization_storage")
            final_fd = os.open(FINALIZATION_DIRECTORY,
                               os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                               dir_fd=operation_root)
            names = set(os.listdir(final_fd))
            _require(names <= {"intent.json", "state.json"}
                     and not ("state.json" in names and "intent.json" not in names),
                     "ios_native_finalization_record")
            live_intent = _LIVE_FINALIZATIONS.get(native_owner)
            if "intent.json" not in names:
                if live_intent is None and not names:
                    # An fsync failure immediately after mkdir can leave this
                    # exact empty private directory before process-local
                    # publication.  The still-live cleanup owner may rebuild
                    # the full intent from freshly preflighted original files.
                    intent = _initial_intent(
                        native_owner, session, finalization_directory_present=True
                    )
                    intent["finalizationDirectoryIdentity"] = _identity_info(
                        os.fstat(final_fd)
                    )
                    _LIVE_FINALIZATIONS[native_owner] = deepcopy(intent)
                else:
                    _require(type(live_intent) is dict,
                             "ios_native_finalization_capability")
                    intent = deepcopy(live_intent)
                _require(
                    intent.get("finalizationDirectoryIdentity")
                    == _identity_info(os.fstat(final_fd)),
                    "ios_native_finalization_binding",
                )
                _record_write_new(final_fd, "intent.json", intent)
                names.add("intent.json")
            else:
                intent = _read_json_at(final_fd, "intent.json")
                _require(type(live_intent) is dict and live_intent == intent,
                         "ios_native_finalization_capability")
            if "state.json" not in names:
                _record_write_new(
                    final_fd, "state.json", _state(native_owner, intent, "discarding")
                )
            state = _read_json_at(final_fd, "state.json")
            _validate_intent(native_owner, session, intent, (final_fd, _identity_info(os.fstat(final_fd))))
            _validate_state(native_owner, intent, state)
        _require(intent is not None)
        if "finalizationDirectoryIdentity" not in intent:
            # Only an in-process record writer can make this state; a missing
            # immutable identity is treated as an unknown journal.
            _fail("ios_native_finalization_record")
        _validate_intent(native_owner, session, intent, (final_fd, _identity_info(os.fstat(final_fd))))
        state = _read_json_at(final_fd, "state.json")
        _validate_state(native_owner, intent, state)
        _validate_phase_record(operation_root, intent["phases"], session)
        if state["state"] == "discarded":
            _assert_empty_large_data(native_owner, intent, session)
            session.bounds()
            return state["evidenceDigest"]
        _record_replace(final_fd, "state.json", _state(native_owner, intent, "discarding"))
        # Validate the operation root before the first unlink.  This keeps an
        # injected name, link, or replacement from causing partial disposal.
        session.bounds()
        root_names = set(os.listdir(operation_root))
        allowed = {"intent.json", "state.json", "producer.lock", "native.json", "phases",
                   FINALIZATION_DIRECTORY, *native_owner.operations._roles, *intent["commands"]}
        if type(native_owner) is _RecoveryOwner:
            allowed.update({"native-recovery", "finalization.json"})
        _require(root_names <= allowed, "ios_native_finalization_unknown")
        _preflight_roles(native_owner, intent, session)
        _dispose_commands(native_owner, intent["commands"], session, remove=False)
        for role in tuple(native_owner.operations._roles):
            if role == "original":
                continue
            session.bounds()
            _dispose_role(native_owner, role, intent["roles"][role], session)
        _dispose_commands(native_owner, intent["commands"], session, remove=True)
        # Original is always the final archive-bearing role operation.
        if "original" in intent["roles"]:
            _dispose_role(native_owner, "original", intent["roles"]["original"], session)
        _assert_empty_large_data(native_owner, intent, session)
        evidence = _evidence_digest(intent)
        session.bounds()
        _record_replace(final_fd, "state.json", _state(native_owner, intent, "discarded",
                                                        evidence_digest=evidence))
        return evidence
    except IOSNativeFinalizationError:
        raise
    except (contracts.ContractError, OSError, RuntimeError, TypeError, ValueError, KeyError):
        raise IOSNativeFinalizationError() from None
    finally:
        if final_fd is not None:
            os.close(final_fd)


def _dispose_recovery_command(command, session):
    names = set(os.listdir(command))
    _require(names <= _PRESERVED_WORK_FILES | _GENERATED_WORK_FILES,
             "ios_native_finalization_unknown")
    for item in sorted(names & _GENERATED_WORK_FILES):
        session.bounds()
        info = _stat_at(command, item)
        _require(info is not None, "ios_native_finalization_command")
        if stat.S_ISDIR(info.st_mode):
            _require(item in {"home", "result.xcresult"}
                     and _safe_identity(_identity_info(info), directory=True),
                     "ios_native_finalization_command")
            child = os.open(item, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=command)
            try:
                _remove_tree(child, session, generated=True, remove=True)
            finally:
                os.close(child)
            session.bounds()
            current = os.stat(item, dir_fd=command, follow_symlinks=False)
            _require(_same_safe_identity(current, _identity_info(info), directory=True),
                     "ios_native_finalization_binding")
            os.rmdir(item, dir_fd=command)
            os.fsync(command)
        else:
            _require(item not in {"home", "result.xcresult"}
                     and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                     and info.st_size <= _MAX_GENERATED_BYTES
                     and _safe_identity(_identity_info(info)),
                     "ios_native_finalization_command")
            _unlink(command, item, _identity_info(info), session, safe=True)
    for item in set(os.listdir(command)):
        info = _stat_at(command, item)
        _require(item in _PRESERVED_WORK_FILES and info is not None,
                 "ios_native_finalization_journal")
        if item == "helper-control":
            _require(stat.S_ISDIR(info.st_mode)
                     and _safe_identity(_identity_info(info), directory=True),
                     "ios_native_finalization_journal")
            child = os.open(item, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=command)
            try:
                _validate_helper_control(child, session)
            finally:
                os.close(child)
        else:
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                     and info.st_size <= _RECORD_LIMIT
                     and _safe_identity(_identity_info(info)),
                     "ios_native_finalization_journal")


def _dispose_recovery_materials(owner, session):
    from .ios_recovery_execution import (
        _archive_disposal_digest,
        _validate_recovery_journal,
    )

    operation_root = owner.operations._operation_root(owner.operation.context.operation_id)
    intent, state = owner.operations._records(owner.operation.context.operation_id,
                                               owner._directory)
    _validate_recovery_journal(owner._directory, intent, state, owner._files.native,
                               operation_root)
    recovery = _open_directory(
        owner._directory, "native-recovery",
        _identity_info(os.stat("native-recovery", dir_fd=owner._directory,
                               follow_symlinks=False)),
    )
    archives = None
    try:
        root_intent = _read_json_at(recovery, "intent.json")
        root_state = _read_json_at(recovery, "state.json")
        attempts = sorted(name for name in os.listdir(recovery)
                          if re.fullmatch(r"attempt-00[1-3]", name))
        _require(attempts, "ios_native_finalization_recovery")
        for name in attempts:
            attempt = _open_directory(
                recovery, name,
                _identity_info(os.stat(name, dir_fd=recovery, follow_symlinks=False)),
                safe=True,
            )
            try:
                attempt_state = _read_json_at(attempt, "state.json")
                _require(attempt_state.get("materialState") == "retired"
                         and attempt_state.get("materialDisposalDigest") is not None,
                         "ios_native_finalization_recovery")
                contracts.validate_digest(attempt_state["materialDisposalDigest"])
                for command_name in sorted(
                    item for item in os.listdir(attempt) if item.startswith("command-")
                ):
                    command = _open_directory(
                        attempt, command_name,
                        _identity_info(os.stat(command_name, dir_fd=attempt,
                                               follow_symlinks=False)),
                        safe=True,
                    )
                    try:
                        _dispose_recovery_command(command, session)
                    finally:
                        os.close(command)
            finally:
                os.close(attempt)
        archives = _open_directory(recovery, "archives",
                                   root_intent["archivesDirectoryIdentity"])
        if root_state["archiveState"] != "discarded":
            if root_state["archiveState"] == "ready":
                root_state["archiveState"] = "discarding"
                root_state["archiveDisposalDigest"] = None
                _replace_at(recovery, "state.json", root_state)
            _require(root_state["archiveState"] == "discarding",
                     "ios_native_finalization_recovery")
            for role, row in root_state["archives"].items():
                name = role + ".ipa"
                info = _stat_at(archives, name)
                if info is None:
                    continue
                descriptor = _open_regular_at(archives, name, expected=row["identity"])
                try:
                    digest, size = _hash_file(descriptor, session, MAX_TRANSFER_BYTES)
                finally:
                    os.close(descriptor)
                _require((digest, size) == (row["sha256"], row["bytes"]),
                         "ios_native_finalization_archive")
                _unlink(archives, name, row["identity"], session)
            _require(not os.listdir(archives), "ios_native_finalization_recovery")
            root_state["archiveState"] = "discarded"
            root_state["archiveDisposalDigest"] = _archive_disposal_digest(root_state)
            _replace_at(recovery, "state.json", root_state)
        _validate_recovery_journal(owner._directory, intent, state, owner._files.native,
                                   operation_root)
        return root_state["archiveDisposalDigest"]
    finally:
        if archives is not None:
            os.close(archives)
        os.close(recovery)


def _verify_recovery_materials(owner, session):
    from .ios_recovery_execution import _validate_recovery_journal

    operation_root = owner.operations._operation_root(owner.operation.context.operation_id)
    intent, state = owner.operations._records(owner.operation.context.operation_id,
                                               owner._directory)
    _validate_recovery_journal(owner._directory, intent, state, owner._files.native,
                               operation_root)
    recovery = _open_directory(
        owner._directory, "native-recovery",
        _identity_info(os.stat("native-recovery", dir_fd=owner._directory,
                               follow_symlinks=False)),
    )
    try:
        root_state = _read_json_at(recovery, "state.json")
        _require(root_state["archiveState"] == "discarded",
                 "ios_native_finalization_incomplete")
        archives = _open_directory(
            recovery, "archives",
            _read_json_at(recovery, "intent.json")["archivesDirectoryIdentity"],
        )
        try:
            _require(not os.listdir(archives), "ios_native_finalization_incomplete")
        finally:
            os.close(archives)
        for name in sorted(item for item in os.listdir(recovery)
                           if re.fullmatch(r"attempt-00[1-3]", item)):
            attempt = _open_directory(
                recovery, name,
                _identity_info(os.stat(name, dir_fd=recovery, follow_symlinks=False)),
                safe=True,
            )
            try:
                _require(_read_json_at(attempt, "state.json")["materialState"] == "retired",
                         "ios_native_finalization_incomplete")
                for command_name in (item for item in os.listdir(attempt)
                                     if item.startswith("command-")):
                    command = _open_directory(
                        attempt, command_name,
                        _identity_info(os.stat(command_name, dir_fd=attempt,
                                               follow_symlinks=False)), safe=True,
                    )
                    try:
                        _require(not (set(os.listdir(command)) & _GENERATED_WORK_FILES),
                                 "ios_native_finalization_incomplete")
                        _dispose_recovery_command(command, session)
                    finally:
                        os.close(command)
            finally:
                os.close(attempt)
        return root_state["archiveDisposalDigest"]
    finally:
        os.close(recovery)


def discard_recovery_native_staged(files, *, cancellation, deadline_monotonic):
    """Dispose original and recovery copies under the exact outer recovery owner."""

    from .ios_native_recovery import (
        IOSNativeRecoveryFinalizationFiles,
        require_native_recovery_finalization,
    )

    try:
        _require(type(files) is IOSNativeRecoveryFinalizationFiles,
                 "ios_native_finalization_capability")
        require_native_recovery_finalization(files)
        owner = _RecoveryOwner(files)
        session = _RecoverySession(files, cancellation, deadline_monotonic)
        final = _stat_at(files.directory, FINALIZATION_DIRECTORY)
        if final is not None:
            final_fd = _open_directory(files.directory, FINALIZATION_DIRECTORY,
                                       _identity_info(final))
            try:
                names = set(os.listdir(final_fd))
                temporary = names - {"intent.json", "state.json"}
                _require(len(temporary) <= 1
                         and all(_FINALIZATION_TEMP.fullmatch(name) for name in temporary),
                         "ios_native_finalization_record")
                for name in temporary:
                    info = _stat_at(final_fd, name)
                    _require(info is not None and stat.S_ISREG(info.st_mode)
                             and info.st_uid == os.getuid()
                             and stat.S_IMODE(info.st_mode) == 0o600
                             and info.st_nlink in (1, 2)
                             and info.st_size <= _RECORD_LIMIT,
                             "ios_native_finalization_record")
                    if info.st_nlink == 2:
                        linked = [item for item in {"intent.json", "state.json"} & names
                                  if _same_identity(
                                      os.stat(item, dir_fd=final_fd, follow_symlinks=False),
                                      _identity_info(info),
                                  )]
                        _require(len(linked) == 1,
                                 "ios_native_finalization_record")
                    os.unlink(name, dir_fd=final_fd)
                    os.fsync(final_fd)
                names -= temporary
                if "intent.json" in names:
                    _LIVE_FINALIZATIONS[owner] = _read_json_at(final_fd, "intent.json")
                elif not names:
                    intent = _initial_intent(
                        owner, session, finalization_directory_present=True
                    )
                    intent["finalizationDirectoryIdentity"] = _identity_info(os.fstat(final_fd))
                    _LIVE_FINALIZATIONS[owner] = intent
            finally:
                os.close(final_fd)
        native_evidence = discard_native_staged(
            owner, session, cancellation=cancellation,
            deadline_monotonic=deadline_monotonic,
        )
        recovery_evidence = _dispose_recovery_materials(owner, session)
        return contracts.digest({
            "schemaVersion": 1,
            "kind": "ios-recovery-staged-disposal-v1",
            "operationId": files.operation_id,
            "contextDigest": files.context_digest,
            "nativeEvidenceDigest": native_evidence,
            "recoveryEvidenceDigest": recovery_evidence,
        })
    except IOSNativeFinalizationError:
        raise
    except (contracts.ContractError, OSError, RuntimeError, TypeError, ValueError, KeyError):
        raise IOSNativeFinalizationError("ios_native_recovery_finalization_unavailable") from None


def verify_recovery_native_staged_disposed(files, expected_digest, *, cancellation,
                                           deadline_monotonic):
    """Freshly prove every recorded large native/recovery file remains absent."""

    from .ios_native_recovery import (
        IOSNativeRecoveryFinalizationFiles,
        require_native_recovery_finalization,
    )

    try:
        contracts.validate_digest(expected_digest)
        _require(type(files) is IOSNativeRecoveryFinalizationFiles,
                 "ios_native_finalization_capability")
        require_native_recovery_finalization(files)
        owner = _RecoveryOwner(files)
        session = _RecoverySession(files, cancellation, deadline_monotonic)
        final = _open_directory(
            files.directory, FINALIZATION_DIRECTORY,
            _identity_info(os.stat(FINALIZATION_DIRECTORY, dir_fd=files.directory,
                                   follow_symlinks=False)),
        )
        try:
            intent = _read_json_at(final, "intent.json")
            _LIVE_FINALIZATIONS[owner] = intent
            _validate_intent(owner, session, intent,
                             (final, _identity_info(os.fstat(final))))
            state = _read_json_at(final, "state.json")
            _validate_state(owner, intent, state)
            _require(state["state"] == "discarded",
                     "ios_native_finalization_incomplete")
            _assert_empty_large_data(owner, intent, session)
            native_evidence = state["evidenceDigest"]
        finally:
            os.close(final)
        recovery_evidence = _verify_recovery_materials(owner, session)
        actual = contracts.digest({
            "schemaVersion": 1,
            "kind": "ios-recovery-staged-disposal-v1",
            "operationId": files.operation_id,
            "contextDigest": files.context_digest,
            "nativeEvidenceDigest": native_evidence,
            "recoveryEvidenceDigest": recovery_evidence,
        })
        _require(actual == expected_digest, "ios_native_finalization_incomplete")
        return actual
    except IOSNativeFinalizationError:
        raise
    except (contracts.ContractError, OSError, RuntimeError, TypeError, ValueError, KeyError):
        raise IOSNativeFinalizationError("ios_native_recovery_finalization_unavailable") from None


__all__ = [
    "FINALIZATION_DIRECTORY", "IOSNativeFinalizationError",
    "discard_native_staged", "discard_recovery_native_staged",
    "verify_recovery_native_staged_disposed",
]
