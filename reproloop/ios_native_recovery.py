"""Restart recovery for an interrupted native iOS mobile operation.

The object returned by :func:`native_recovery` is deliberately process local.
It reopens the operation's original private records and the canonical
``RunStore`` lock, then borrows the quarantined device lock through
``DeviceAuthority``.  It does not issue a device command, prove sanitation,
release a reservation, or turn a JSON record into a capability.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import fcntl
import math
import os
import re
import stat
import threading
import time

from . import contracts
from .execution.journal import RunDenied, RunStore
from .execution.wire import canonical, decode_json
from .ios_mobile_native import _binding_record
from .ios_mobile_operation import IOSMobileOperationError, IOSMobileOperationStore
from .live.authority import DeviceAuthority, HostAuthority
from .repair_android_operation import (
    _identity_info,
    _open_child_directory,
    _open_regular_at,
    _read_fd,
    _read_json_at,
    _same_identity,
    _valid_identity,
    _walk_directory,
)


_RECORD_BYTES = 512 * 1024
_MAX_GENERATED_ENTRIES = 100_000
_MAX_GENERATED_BYTES = 512 * 1024 * 1024
_COMMAND = re.compile(
    r"command-(?:install-candidate|restore-original|xctest-(?:candidate|original)-00[1-3])-work\Z"
)
_PHASE = re.compile(r"replay-00[1-3]\Z")
_PHASE_NAMES = frozenset(("install", "cleanup"))
_ROLE_CHILDREN = frozenset(("input.ipa", "App.app", "transfer"))
_TRANSFER_CHILDREN = frozenset(("source.ipa", "app"))
_COMMAND_PRESERVED = frozenset(
    ("intent.json", "state.json", "native.json", "session.xctestrun", "stage.json", "helper-control")
)
_COMMAND_GENERATED = frozenset(
    ("result.json", "identity.json", "result.xcresult", ".native-session.xctestrun", "home")
)
_HELPER_RECORDS = frozenset(("intent.json", "state.json", "ack.json"))
_FINALIZATION_RECORDS = frozenset(("intent.json", "state.json"))
_FINALIZATION_TEMP = re.compile(r"\.native-record-[0-9a-f]{32}\Z")


class IOSNativeRecoveryError(IOSMobileOperationError):
    """A native recovery binding or bounded journal was rejected."""

    def __init__(self, code="ios_native_recovery_unavailable"):
        self.code = code
        super().__init__(code)


class _ReadOnlyPublic(dict):
    """A JSON-serializable immutable view of the recovery inspection."""

    __slots__ = ()

    def _readonly(self, *args, **kwargs):
        raise TypeError("read-only recovery inspection")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _readonly

    def __ior__(self, other):
        self._readonly(other)


def _fail(code="ios_native_recovery_unavailable"):
    raise IOSNativeRecoveryError(code)


def _require_recovery(value, code="ios_native_recovery_unavailable"):
    if not value:
        _fail(code)


def _is_directory(info):
    return stat.S_ISDIR(info.st_mode)


def _is_regular(info):
    return stat.S_ISREG(info.st_mode)


def _validate_private_directory(info):
    _require_recovery(
        _is_directory(info)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        "ios_native_recovery_storage",
    )


def _validate_private_file(info, *, maximum=_RECORD_BYTES):
    _require_recovery(
        _is_regular(info)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) in (0o600, 0o700)
        and info.st_size <= maximum,
        "ios_native_recovery_storage",
    )


def _validate_safe_generated_directory(info):
    _require_recovery(
        _is_directory(info)
        and info.st_uid == os.getuid()
        and not stat.S_IMODE(info.st_mode) & 0o022
        and not info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX),
        "ios_native_recovery_journal",
    )


def _validate_safe_generated_file(info, *, maximum=_MAX_GENERATED_BYTES):
    _require_recovery(
        _is_regular(info)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and not stat.S_IMODE(info.st_mode) & 0o022
        and not info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        and info.st_size <= maximum,
        "ios_native_recovery_journal",
    )


def _valid_safe_identity(value, *, directory=False):
    if type(value) is not dict or set(value) != {"device", "inode", "mode", "uid", "links"}:
        return False
    if not all(type(item) is int for item in value.values()):
        return False
    return (
        value["device"] >= 0
        and value["inode"] > 0
        and value["uid"] == os.getuid()
        and value["links"] >= 1
        and not value["mode"] & 0o022
        and not value["mode"] & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
    )


def _stat(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        _fail("ios_native_recovery_storage")


def _open_directory(parent, name, expected=None, *, safe=False):
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
        info = os.fstat(descriptor)
        if safe:
            _validate_safe_generated_directory(info)
        else:
            _validate_private_directory(info)
        if expected is not None:
            _require_recovery(
                _same_identity(info, expected, directory=True),
                "ios_native_recovery_binding",
            )
        return descriptor
    except IOSNativeRecoveryError:
        raise
    except (OSError, TypeError, ValueError):
        _fail("ios_native_recovery_storage")


def _record(parent, name):
    info = _stat(parent, name)
    _require_recovery(info is not None, "ios_native_recovery_journal")
    _validate_private_file(info)
    try:
        return _read_json_at(parent, name, maximum=_RECORD_BYTES)
    except Exception:
        _fail("ios_native_recovery_journal")


def _finalization_record(parent, name):
    info = _stat(parent, name)
    _require_recovery(info is not None and _is_regular(info)
                      and info.st_uid == os.getuid()
                      and stat.S_IMODE(info.st_mode) == 0o600
                      and info.st_nlink in (1, 2)
                      and info.st_size <= _RECORD_BYTES,
                      "ios_native_recovery_journal")
    descriptor = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        _require_recovery(_same_identity(os.fstat(descriptor), _identity_info(info)),
                          "ios_native_recovery_journal")
        body = _read_fd(descriptor, _RECORD_BYTES)
        value = decode_json(body)
        _require_recovery(canonical(value) == body, "ios_native_recovery_journal")
        return value
    except IOSNativeRecoveryError:
        raise
    except Exception:
        _fail("ios_native_recovery_journal")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_record_file(parent, name, *, maximum=_RECORD_BYTES):
    info = _stat(parent, name)
    _require_recovery(info is not None, "ios_native_recovery_journal")
    _validate_private_file(info, maximum=maximum)
    return _identity_info(info)


def _validate_generated_tree(directory, *, depth=0, budget=None):
    """Validate generated command output without treating it as authority."""

    if budget is None:
        budget = {"entries": 0, "bytes": 0}
    _require_recovery(depth <= 128, "ios_native_recovery_journal")
    try:
        names = os.listdir(directory)
    except OSError:
        _fail("ios_native_recovery_journal")
    for name in names:
        _require_recovery(
            type(name) is str and name not in ("", ".", "..") and "/" not in name,
            "ios_native_recovery_journal",
        )
        budget["entries"] += 1
        _require_recovery(
            budget["entries"] <= _MAX_GENERATED_ENTRIES,
            "ios_native_recovery_journal",
        )
        info = _stat(directory, name)
        _require_recovery(info is not None and info.st_uid == os.getuid(), "ios_native_recovery_journal")
        if _is_directory(info):
            _validate_safe_generated_directory(info)
            child = _open_directory(directory, name, _identity_info(info), safe=True)
            try:
                _validate_generated_tree(child, depth=depth + 1, budget=budget)
            finally:
                os.close(child)
        else:
            _validate_safe_generated_file(info)
            budget["bytes"] += info.st_size
            _require_recovery(
                budget["bytes"] <= _MAX_GENERATED_BYTES,
                "ios_native_recovery_journal",
            )


def _validate_helper_control(directory):
    """Inspect helper-control's private, bounded journal names and records."""

    entries = 0
    names = set(os.listdir(directory))
    _require_recovery(
        names <= _HELPER_RECORDS or all(name.startswith("op-") for name in names - _HELPER_RECORDS),
        "ios_native_recovery_journal",
    )
    for name in sorted(names):
        entries += 1
        _require_recovery(entries <= 1024, "ios_native_recovery_journal")
        info = _stat(directory, name)
        _require_recovery(info is not None, "ios_native_recovery_journal")
        if name in _HELPER_RECORDS:
            _validate_private_file(info)
            _record(directory, name)
            continue
        _require_recovery(
            _is_directory(info)
            and re.fullmatch(r"op-[a-z][a-z0-9_-]{0,126}\Z", name) is not None,
            "ios_native_recovery_journal",
        )
        _validate_private_directory(info)
        operation = _open_directory(directory, name, _identity_info(info))
        try:
            nested = set(os.listdir(operation))
            _require_recovery(nested <= _HELPER_RECORDS, "ios_native_recovery_journal")
            for child in sorted(nested):
                _validate_record_file(operation, child)
                _record(operation, child)
        finally:
            os.close(operation)


def _validate_journal_tree(directory, *, allowed, generated=False):
    names = set(os.listdir(directory))
    _require_recovery(names <= set(allowed), "ios_native_recovery_journal")
    for name in sorted(names):
        info = _stat(directory, name)
        _require_recovery(info is not None, "ios_native_recovery_journal")
        if name == "helper-control":
            _require_recovery(_is_directory(info), "ios_native_recovery_journal")
            _validate_private_directory(info)
            child = _open_directory(directory, name, _identity_info(info), safe=True)
            try:
                _validate_helper_control(child)
            finally:
                os.close(child)
        elif name in ("home", "result.xcresult") and generated:
            _require_recovery(_is_directory(info), "ios_native_recovery_journal")
            _validate_safe_generated_directory(info)
            child = _open_directory(directory, name, _identity_info(info), safe=True)
            try:
                _validate_generated_tree(child)
            finally:
                os.close(child)
        else:
            if generated and name in _COMMAND_GENERATED:
                _validate_safe_generated_file(info)
            else:
                _validate_record_file(directory, name)


def _validate_phases(directory, binding, intent, state):
    phase = _stat(directory, "phases")
    if phase is None:
        return None
    _require_recovery(_is_directory(phase), "ios_native_recovery_journal")
    _validate_private_directory(phase)
    phases = _open_directory(directory, "phases", _identity_info(phase))
    rows = {}
    try:
        names = set(os.listdir(phases))
        _require_recovery(len(names) <= 5, "ios_native_recovery_journal")
        for name in sorted(names):
            _require_recovery(name in _PHASE_NAMES or _PHASE.fullmatch(name), "ios_native_recovery_journal")
            info = _stat(phases, name)
            _require_recovery(info is not None and _is_directory(info), "ios_native_recovery_journal")
            _validate_private_directory(info)
            phase_fd = _open_directory(phases, name, _identity_info(info))
            try:
                _require_recovery(set(os.listdir(phase_fd)) == {"intent.json", "state.json"},
                                  "ios_native_recovery_journal")
                phase_intent = _record(phase_fd, "intent.json")
                phase_state = _record(phase_fd, "state.json")
                _validate_phase_record(phase_intent, binding, intent, state)
                _validate_phase_record(phase_state, binding, intent, state)
                immutable = set(phase_intent) - {"attempt", "state", "outcomeDigest"}
                _require_recovery(
                    all(phase_intent[name] == phase_state[name] for name in immutable)
                    and phase_intent["state"] == "running"
                    and phase_state["attempt"] >= phase_intent["attempt"]
                    and phase_state["state"] in {"running", "completed", "failed"},
                    "ios_native_recovery_journal",
                )
                rows[name] = _identity_info(info)
            finally:
                os.close(phase_fd)
    finally:
        os.close(phases)
    return {"directoryIdentity": _identity_info(phase), "entries": rows}


def _validate_phase_record(record, binding, intent, state):
    _require_recovery(type(record) is dict, "ios_native_recovery_journal")
    required = {
        "schemaVersion", "operationId", "requestDigest", "contextDigest",
        "configurationDigest", "scopeDigest", "nativeBindingDigest",
        "ownershipGeneration", "hostIncarnation", "helperIncarnation",
        "authorityRootDigest", "phase", "iteration", "sequence", "attempt",
        "state", "outcomeDigest",
    }
    _require_recovery(set(record) == required, "ios_native_recovery_journal")
    _require_recovery(
        record["schemaVersion"] == 1
        and record["operationId"] == intent["operationId"]
        and record["requestDigest"] == intent["requestDigest"]
        and record["contextDigest"] == intent["contextDigest"]
        and record["configurationDigest"] == intent["configurationDigest"]
        and record["scopeDigest"] == intent["context"]["scope_digest"]
        and record["nativeBindingDigest"] == binding["bindingDigest"]
        and record["ownershipGeneration"] == binding["ownershipGeneration"]
        and record["hostIncarnation"] == binding["hostIncarnation"]
        and record["helperIncarnation"] == binding["helperIncarnation"]
        and record["authorityRootDigest"] == binding["authorityRootDigest"],
        "ios_native_recovery_journal",
    )
    _require_recovery(record["phase"] in {"install", "replay", "cleanup"}, "ios_native_recovery_journal")
    _require_recovery(type(record["iteration"]) is int and 0 <= record["iteration"] <= 3,
                      "ios_native_recovery_journal")
    _require_recovery(type(record["sequence"]) is int and 0 < record["sequence"] <= 16,
                      "ios_native_recovery_journal")
    _require_recovery(type(record["attempt"]) is int and 0 <= record["attempt"] < 2 ** 31,
                      "ios_native_recovery_journal")
    _require_recovery(record["state"] in {"running", "completed", "failed"}, "ios_native_recovery_journal")
    _require_recovery(record["outcomeDigest"] is None or _valid_digest(record["outcomeDigest"]),
                      "ios_native_recovery_journal")


def _valid_digest(value):
    try:
        contracts.validate_digest(value)
    except Exception:
        return False
    return True


def _validate_embedded_binding(value, binding, *, depth=0):
    """Check native binding digests inside bounded command journal objects."""

    _require_recovery(depth <= 32, "ios_native_recovery_journal")
    if type(value) is dict:
        for key, item in value.items():
            if key == "nativeBindingDigest":
                _require_recovery(item == binding["bindingDigest"], "ios_native_recovery_journal")
            elif key == "authorityRootDigest":
                _require_recovery(item == binding["authorityRootDigest"], "ios_native_recovery_journal")
            _validate_embedded_binding(item, binding, depth=depth + 1)
    elif type(value) is list:
        for item in value:
            _validate_embedded_binding(item, binding, depth=depth + 1)


def _validate_command(directory, binding, intent):
    _require_recovery(_COMMAND.fullmatch(directory[0]) is not None, "ios_native_recovery_journal")
    command_fd = directory[1]
    _validate_journal_tree(
        command_fd,
        allowed=_COMMAND_PRESERVED | _COMMAND_GENERATED,
        generated=True,
    )
    names = set(os.listdir(command_fd))
    for name in {"intent.json", "state.json", "native.json"} & names:
        value = _record(command_fd, name)
        _require_recovery(type(value) is dict, "ios_native_recovery_journal")
        _validate_embedded_binding(value, binding)
        if name == "native.json":
            _require_recovery(
                value.get("nativeBindingDigest") == binding["bindingDigest"]
                or value.get("nativeBindingDigest") == binding.get("bindingDigest"),
                "ios_native_recovery_journal",
            )
        else:
            _require_recovery(
                value.get("operationId") in (None, intent["operationId"]),
                "ios_native_recovery_journal",
            )


def _validate_finalization(directory, binding, intent):
    final = _stat(directory, "native-finalization")
    if final is None:
        return None
    _require_recovery(_is_directory(final), "ios_native_recovery_journal")
    _validate_private_directory(final)
    final_fd = _open_directory(directory, "native-finalization", _identity_info(final))
    try:
        names = set(os.listdir(final_fd))
        published = names & _FINALIZATION_RECORDS
        temporary = names - published
        _require_recovery(
            published in (set(), {"intent.json"}, set(_FINALIZATION_RECORDS))
            and len(temporary) <= 1
            and all(_FINALIZATION_TEMP.fullmatch(name) for name in temporary),
            "ios_native_recovery_journal",
        )
        for name in temporary:
            info = _stat(final_fd, name)
            _require_recovery(
                info is not None and _is_regular(info) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink in (1, 2) and info.st_size <= _RECORD_BYTES,
                "ios_native_recovery_journal",
            )
            if info.st_nlink == 2:
                linked = [item for item in published if _same_identity(
                    os.stat(item, dir_fd=final_fd, follow_symlinks=False),
                    _identity_info(info),
                )]
                _require_recovery(len(linked) == 1, "ios_native_recovery_journal")
        if "intent.json" not in published:
            return {"state": "uncommitted", "directoryIdentity": _identity_info(final)}
        final_intent = _finalization_record(final_fd, "intent.json")
        _require_recovery(type(final_intent) is dict,
                          "ios_native_recovery_journal")
        required = {
            "schemaVersion", "kind", "operationId", "requestDigest", "contextDigest",
            "configurationDigest", "scopeDigest", "nativeBindingDigest",
            "operationDirectoryIdentity", "finalizationDirectoryIdentity", "roles",
            "commands", "phases",
        }
        _require_recovery(set(final_intent) == required, "ios_native_recovery_journal")
        _require_recovery(
            final_intent["schemaVersion"] == 1
            and final_intent["kind"] == "ios-native-finalization-v1"
            and final_intent["operationId"] == intent["operationId"]
            and final_intent["requestDigest"] == intent["requestDigest"]
            and final_intent["contextDigest"] == intent["contextDigest"]
            and final_intent["configurationDigest"] == intent["configurationDigest"]
            and final_intent["scopeDigest"] == intent["context"]["scope_digest"]
            and final_intent["nativeBindingDigest"] == binding["bindingDigest"]
            and _valid_identity(final_intent["operationDirectoryIdentity"], directory=True)
            and _valid_identity(final_intent["finalizationDirectoryIdentity"], directory=True)
            and _same_identity(os.stat(".", dir_fd=directory, follow_symlinks=False),
                               final_intent["operationDirectoryIdentity"], directory=True)
            and _same_identity(os.fstat(final_fd), final_intent["finalizationDirectoryIdentity"], directory=True),
            "ios_native_recovery_journal",
        )
        _require_recovery(type(final_intent["roles"]) is dict, "ios_native_recovery_journal")
        _require_recovery(type(final_intent["commands"]) is dict, "ios_native_recovery_journal")
        _require_recovery(final_intent["phases"] is None or type(final_intent["phases"]) is dict,
                          "ios_native_recovery_journal")
        _require_recovery(set(final_intent["roles"]) == set(intent["roles"]),
                          "ios_native_recovery_journal")
        _require_recovery(len(final_intent["commands"]) <= 8, "ios_native_recovery_journal")
        for name, row in final_intent["commands"].items():
            _require_recovery(
                _COMMAND.fullmatch(name) is not None
                and type(row) is dict
                and set(row) == {"directoryIdentity", "generated", "preserved"}
                and _valid_safe_identity(row["directoryIdentity"], directory=True)
                and type(row["generated"]) is dict
                and set(row["generated"]) == set(_COMMAND_GENERATED)
                and type(row["preserved"]) is dict
                and set(row["preserved"]) <= set(_COMMAND_PRESERVED),
                "ios_native_recovery_journal",
            )
            for item, value in row["generated"].items():
                _require_recovery(
                    value is None or _valid_safe_identity(value, directory=item in {"home", "result.xcresult"}),
                    "ios_native_recovery_journal",
                )
            for item, value in row["preserved"].items():
                _require_recovery(_valid_safe_identity(value, directory=item == "helper-control"),
                                  "ios_native_recovery_journal")
        if final_intent["phases"] is not None:
            phase = final_intent["phases"]
            _require_recovery(
                set(phase) == {"directoryIdentity", "entries"}
                and _valid_safe_identity(phase["directoryIdentity"], directory=True)
                and type(phase["entries"]) is dict,
                "ios_native_recovery_journal",
            )
            _require_recovery(len(phase["entries"]) <= 5, "ios_native_recovery_journal")
            for name, row in phase["entries"].items():
                _require_recovery(
                    (name in _PHASE_NAMES or _PHASE.fullmatch(name))
                    and type(row) is dict
                    and set(row) == {"directoryIdentity"}
                    and _valid_safe_identity(row["directoryIdentity"], directory=True),
                    "ios_native_recovery_journal",
                )
        if "state.json" not in published:
            return {"state": "intent-published", "directoryIdentity": _identity_info(final)}
        final_state = _finalization_record(final_fd, "state.json")
        _require_recovery(type(final_state) is dict, "ios_native_recovery_journal")
        state_keys = {
            "schemaVersion", "kind", "operationId", "requestDigest", "contextDigest",
            "nativeBindingDigest", "intentDigest", "state",
        }
        _require_recovery(
            set(final_state) in (state_keys, state_keys | {"evidenceDigest"})
            and final_state["schemaVersion"] == 1
            and final_state["kind"] == "ios-native-finalization-state"
            and final_state["operationId"] == intent["operationId"]
            and final_state["requestDigest"] == intent["requestDigest"]
            and final_state["contextDigest"] == intent["contextDigest"]
            and final_state["nativeBindingDigest"] == binding["bindingDigest"]
            and final_state["intentDigest"] == contracts.digest(final_intent)
            and final_state["state"] in {"discarding", "discarded"},
            "ios_native_recovery_journal",
        )
        if "evidenceDigest" in final_state:
            _require_recovery(_valid_digest(final_state["evidenceDigest"]), "ios_native_recovery_journal")
        # The finalizer has its own exact recorded identities.  Validate their
        # scalar shape, but do not require large role/app nodes to still exist:
        # an interrupted finalizer may have already disposed some of them.
        for role, row in final_intent["roles"].items():
            _require_recovery(type(role) is str and type(row) is dict, "ios_native_recovery_journal")
            _require_recovery(
                set(row)
                == {
                    "directoryIdentity", "transferIdentity", "archiveIdentity", "appIdentity",
                    "transferAppIdentity", "snapshotIdentity", "archiveDigest", "archiveBytes", "appDigest",
                },
                "ios_native_recovery_journal",
            )
            for key, value in row.items():
                if key.endswith("Identity"):
                    if key in {"directoryIdentity", "transferIdentity", "archiveIdentity"}:
                        _require_recovery(
                            _valid_identity(value, directory=key in {"directoryIdentity", "transferIdentity"}),
                            "ios_native_recovery_journal",
                        )
                    elif value is not None:
                        _require_recovery(
                            _valid_safe_identity(value, directory=True),
                            "ios_native_recovery_journal",
                        )
            _require_recovery(_valid_digest(row["archiveDigest"]) and _valid_digest(row["appDigest"]),
                              "ios_native_recovery_journal")
            _require_recovery(type(row["archiveBytes"]) is int and 0 < row["archiveBytes"] <= 64 * 1024 * 1024,
                              "ios_native_recovery_journal")
        return {"state": final_state["state"], "directoryIdentity": _identity_info(final)}
    finally:
        os.close(final_fd)


def _validate_roles(operations, directory, intent, state):
    for role, row in intent["roles"].items():
        role_fd = _open_directory(directory, role, row["directoryIdentity"])
        try:
            names = set(os.listdir(role_fd))
            _require_recovery(names <= _ROLE_CHILDREN, "ios_native_recovery_journal")
            _require_recovery("transfer" in names, "ios_native_recovery_journal")
            if "input.ipa" in names:
                _validate_record_file(role_fd, "input.ipa", maximum=64 * 1024 * 1024)
                _require_recovery(
                    _same_identity(os.stat("input.ipa", dir_fd=role_fd, follow_symlinks=False), row["archiveIdentity"]),
                    "ios_native_recovery_binding",
                )
            if "App.app" in names:
                app = _stat(role_fd, "App.app")
                _require_recovery(app is not None and _is_directory(app), "ios_native_recovery_storage")
                _validate_private_directory(app)
            transfer = _stat(role_fd, "transfer")
            _require_recovery(transfer is not None and _is_directory(transfer), "ios_native_recovery_storage")
            _require_recovery(
                _same_identity(transfer, row["transferIdentity"], directory=True),
                "ios_native_recovery_binding",
            )
            transfer_fd = _open_directory(role_fd, "transfer", row["transferIdentity"])
            try:
                transfer_names = set(os.listdir(transfer_fd))
                _require_recovery(transfer_names <= _TRANSFER_CHILDREN, "ios_native_recovery_journal")
                for name in sorted(transfer_names):
                    info = _stat(transfer_fd, name)
                    if name == "source.ipa":
                        _validate_private_file(info, maximum=64 * 1024 * 1024)
                    else:
                        _require_recovery(_is_directory(info), "ios_native_recovery_storage")
                        _validate_private_directory(info)
            finally:
                os.close(transfer_fd)
        finally:
            os.close(role_fd)


def _validate_operation_journals(operations, directory, intent, state, native):
    names = set(os.listdir(directory))
    allowed = {
        "intent.json", "state.json", "producer.lock", "native.json", "phases",
        "native-finalization", "native-recovery", "finalization.json",
        *operations._roles,
    }
    commands = set()
    for name in names - allowed:
        _require_recovery(_COMMAND.fullmatch(name) is not None, "ios_native_recovery_journal")
        commands.add(name)
    _require_recovery(len(commands) <= 8, "ios_native_recovery_journal")
    for role in operations._roles:
        _require_recovery(role in names, "ios_native_recovery_storage")
    _validate_roles(operations, directory, intent, state)
    _validate_phases(directory, native, intent, state)
    for name in sorted(commands):
        command_identity = _identity_info(
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        )
        command = _open_directory(directory, name, command_identity)
        try:
            _validate_command((name, command), native, intent)
        finally:
            os.close(command)
    if "native-recovery" in names:
        from .ios_recovery_execution import _validate_recovery_journal
        _validate_recovery_journal(
            directory, intent, state, native,
            operations._operation_root(intent["operationId"]),
        )
    if "finalization.json" in names:
        from .ios_recovery_finalization import validate_ios_recovery_finalization_record
        validate_ios_recovery_finalization_record(directory, intent, native)
    return _validate_finalization(directory, native, intent)


def _validate_context_inputs(operations, operation_id, request_digest, cancellation, deadline_monotonic):
    _require_recovery(type(operations) is IOSMobileOperationStore, "ios_native_recovery_binding")
    _require_recovery(type(operations.run_store) is RunStore, "ios_native_recovery_binding")
    _require_recovery(callable(getattr(cancellation, "is_set", None)), "ios_native_recovery_bounds")
    _require_recovery(
        type(deadline_monotonic) in (int, float)
        and not isinstance(deadline_monotonic, bool)
        and math.isfinite(deadline_monotonic)
        and time.monotonic() < deadline_monotonic
        and not cancellation.is_set(),
        "ios_native_recovery_bounds",
    )
    try:
        contracts.validate_id(operation_id)
        contracts.validate_digest(request_digest)
    except Exception:
        _fail("ios_native_recovery_binding")


def _require_device(operations, device):
    _require_recovery(
        type(device) is DeviceAuthority
        and type(device._authority) is HostAuthority
        and device.device_kind == "ios-physical"
        and device._device_fingerprint == operations.definition.scope_digest
        and device in device._authority._handles,
        "ios_native_recovery_device",
    )


def _same_open_lock(descriptor):
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        _fail("ios_native_recovery_descriptor")


@dataclass(slots=True, repr=False)
class IOSNativeRecoveryContext:
    """An exact, process-local recovery owner for one native iOS operation."""

    operation_id: str
    request_digest: str
    context_digest: str
    scope_digest: str
    configuration_digest: str
    binding_digest: str
    prior_generation: int
    directory: int = field(repr=False, compare=False)
    producer: int = field(repr=False, compare=False)
    device_lease: object = field(repr=False, compare=False)
    intent: dict = field(repr=False, compare=False)
    native: dict = field(repr=False, compare=False)
    _operations: object = field(repr=False, compare=False)
    _device: object = field(repr=False, compare=False)
    _parent_grant: object = field(repr=False, compare=False)
    _cancellation: object = field(repr=False, compare=False)
    _deadline_monotonic: float = field(repr=False, compare=False)
    _grant_deadline_ns: int = field(repr=False, compare=False)
    _pid: int = field(repr=False, compare=False)
    _thread: int = field(repr=False, compare=False)
    _thread_object: object = field(repr=False, compare=False)
    _original_directory: int = field(repr=False, compare=False)
    _original_producer: int = field(repr=False, compare=False)
    _original_lease: object = field(repr=False, compare=False)
    _original_parent_grant: object = field(repr=False, compare=False)
    _original_cancellation: object = field(repr=False, compare=False)
    _original_deadline_monotonic: float = field(repr=False, compare=False)
    _intent_snapshot: dict = field(repr=False, compare=False)
    _native_snapshot: dict = field(repr=False, compare=False)
    _public: object = field(repr=False, compare=False)
    _active: bool = field(default=True, repr=False, compare=False)

    def __repr__(self):
        return "<IOSNativeRecoveryContext>"

    def __copy__(self):
        _fail("ios_native_recovery_descriptor")

    def __deepcopy__(self, memo):
        _fail("ios_native_recovery_descriptor")

    @property
    def public(self):
        self.require()
        return self._public

    @property
    def deadline_monotonic(self):
        self.require()
        return self._deadline_monotonic

    @property
    def grant_deadline_ns(self):
        self.require()
        return self._grant_deadline_ns

    def _bounds(self):
        _require_recovery(
            self._active
            and self._pid == os.getpid()
            and self._thread == threading.get_ident()
            and self._thread_object is threading.current_thread()
            and not self._operations._closed
            and self._parent_grant is self._original_parent_grant
            and self._cancellation is self._original_cancellation
            and self._deadline_monotonic == self._original_deadline_monotonic
            and time.monotonic() < self._deadline_monotonic
            and not self._cancellation.is_set(),
            "ios_native_recovery_bounds",
        )

    def require(self):
        _require_native_recovery(self)
        return self

    def close(self):
        operations = self._operations
        with operations._changed:
            self._active = False
            exports = getattr(operations, "_native_recovery_exports", None)
            if exports is not None:
                exports.pop(id(self), None)
            operations._changed.notify_all()


@dataclass(slots=True, repr=False)
class IOSNativeRecoveryFinalizationFiles:
    """Original lock-bearing files retained after the native borrow is collected."""

    operation_id: str
    request_digest: str
    context_digest: str
    scope_digest: str
    configuration_digest: str
    binding_digest: str
    prior_generation: int
    directory: int = field(repr=False)
    producer: int = field(repr=False)
    device_descriptor: int = field(repr=False)
    device_directory: int = field(repr=False)
    device_lock_name: str = field(repr=False)
    intent: dict = field(repr=False)
    native: dict = field(repr=False)
    _operations: object = field(repr=False)
    _device: object = field(repr=False)
    _parent_grant: object = field(repr=False)
    _cancellation: object = field(repr=False)
    _deadline_monotonic: float = field(repr=False)
    _pid: int = field(repr=False)
    _thread: int = field(repr=False)
    _thread_object: object = field(repr=False)
    reconciled_host_incarnation: str | None = field(default=None, repr=False)
    reconciled_helper_incarnation: str | None = field(default=None, repr=False)
    _active: bool = field(default=True, repr=False)

    def __repr__(self):
        return "<IOSNativeRecoveryFinalizationFiles>"

    def __copy__(self):
        _fail("ios_native_recovery_finalization_descriptor")

    def __deepcopy__(self, memo):
        _fail("ios_native_recovery_finalization_descriptor")

    def require(self):
        return require_native_recovery_finalization(self)


def _held_named_lock(descriptor, directory, name, expected):
    _require_recovery(_same_identity(os.fstat(descriptor), expected),
                      "ios_native_recovery_finalization_binding")
    probe = _open_regular_at(directory, name, expected=expected, writable=True)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            _fail("ios_native_recovery_finalization_lock")
        # This is a duplicate of the already locked open file description.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)


def require_native_recovery_finalization(files):
    """Revalidate a process-local outer cleanup owner without native authority."""

    try:
        _require_recovery(type(files) is IOSNativeRecoveryFinalizationFiles,
                          "ios_native_recovery_finalization_descriptor")
        operations = files._operations
        _require_recovery(
            files._active
            and files._pid == os.getpid()
            and files._thread == threading.get_ident()
            and files._thread_object is threading.current_thread()
            and time.monotonic() < files._deadline_monotonic
            and not files._cancellation.is_set()
            and not operations._closed,
            "ios_native_recovery_finalization_bounds",
        )
        with operations._changed:
            exports = getattr(operations, "_native_recovery_finalization_exports", {})
            _require_recovery(exports.get(id(files)) is files,
                              "ios_native_recovery_finalization_descriptor")
            _require_recovery(not operations._active and not operations._callbacks
                              and not operations._native_owners
                              and not operations._native_exports
                              and not operations._native_clients
                              and not operations._recoveries,
                              "ios_native_recovery_finalization_busy")
        _require_recovery(
            _same_identity(os.fstat(files.directory), files.intent["directoryIdentity"], directory=True)
            and _same_identity(os.fstat(files.producer), files.intent["producerIdentity"]),
            "ios_native_recovery_finalization_binding",
        )
        current_intent, current_state = operations._records(files.operation_id, files.directory)
        current_native = _binding_record(files.directory, current_intent, current_state)
        _require_recovery(
            current_intent == files.intent
            and current_native == files.native
            and files.request_digest == files.intent["requestDigest"]
            and files.context_digest == files.intent["contextDigest"]
            and files.configuration_digest == files.intent["configurationDigest"]
            and files.scope_digest == files.intent["context"]["scope_digest"]
            and files.binding_digest == files.native["bindingDigest"],
            "ios_native_recovery_finalization_binding",
        )
        with operations._directory(files.operation_id) as reopened:
            _require_recovery(_same_identity(os.fstat(reopened), files.intent["directoryIdentity"], directory=True),
                              "ios_native_recovery_finalization_binding")
            _held_named_lock(files.producer, reopened, "producer.lock", files.intent["producerIdentity"])
        _require_recovery(
            _same_identity(os.fstat(files.device_descriptor), files.native["deviceLeaseIdentity"])
            and _same_identity(os.fstat(files.device_directory), files.native["deviceDirectoryIdentity"], directory=True),
            "ios_native_recovery_finalization_binding",
        )
        _held_named_lock(files.device_descriptor, files.device_directory,
                         files.device_lock_name, files.native["deviceLeaseIdentity"])
        device = files._device
        _require_recovery(type(device) is DeviceAuthority and device in device._authority._handles
                          and device._device_fingerprint == files.scope_digest,
                          "ios_native_recovery_finalization_device")
        row = device._authority.store.device(files.scope_digest)
        _require_recovery(row is not None and (
            (row["generation"] == files.prior_generation
             and device.generation == files.prior_generation
             and row["status"] == "quarantined")
            or
            (row["generation"] == files.prior_generation + 1
             and device.generation == files.prior_generation + 1
             and device.helper_incarnation == row["helper_incarnation"]
             and row["status"] == "quarantined"
             and row["quarantine_reason"] == "recovery-cleanup-pending"
             and files.reconciled_host_incarnation is not None
             and files.reconciled_helper_incarnation is not None
             and row["host_incarnation"] == files.reconciled_host_incarnation
             and row["helper_incarnation"] == files.reconciled_helper_incarnation)
        ), "ios_native_recovery_finalization_device")
        run = operations.run_store.status(files.operation_id)
        _require_recovery(run["requestDigest"] == files.request_digest
                          and run["state"] in {"admitted", "quarantined", "failed", "cancelled"}
                          and ((run["reservedBytes"] == files.intent["reservedBytes"])
                               if run["state"] in {"admitted", "quarantined"}
                               else run["reservedBytes"] == 0),
                          "ios_native_recovery_finalization_binding")
        return files
    except IOSNativeRecoveryError:
        raise
    except (RunDenied, OSError, RuntimeError, TypeError, ValueError, KeyError):
        _fail("ios_native_recovery_finalization_unavailable")


@contextmanager
def capture_native_recovery_finalization(context):
    """Retain only outer file locks while the native recovery borrow is collected."""

    files = None
    with ExitStack() as stack:
        require_native_recovery(context)
        operations = context._operations
        directory = os.dup(context.directory)
        stack.callback(os.close, directory)
        producer = os.dup(context.producer)
        stack.callback(os.close, producer)
        device_descriptor, device_directory, device_name = stack.enter_context(
            context._device._lease.borrow_descriptor()
        )
        files = IOSNativeRecoveryFinalizationFiles(
            context.operation_id, context.request_digest, context.context_digest,
            context.scope_digest, context.configuration_digest, context.binding_digest,
            context.prior_generation, directory, producer, device_descriptor,
            device_directory, device_name, deepcopy(context.intent), deepcopy(context.native),
            operations, context._device, context._parent_grant, context._cancellation,
            context._deadline_monotonic, os.getpid(), threading.get_ident(),
            threading.current_thread(),
        )
        with operations._changed:
            exports = getattr(operations, "_native_recovery_finalization_exports", None)
            if exports is None:
                exports = {}
                operations._native_recovery_finalization_exports = exports
            _require_recovery(not exports, "ios_native_recovery_finalization_busy")
            exports[id(files)] = files
        try:
            require_native_recovery_finalization(files)
            yield files
        finally:
            with operations._changed:
                files._active = False
                operations._native_recovery_finalization_exports.pop(id(files), None)
                operations._changed.notify_all()


def _require_native_recovery(context):
    _require_recovery(type(context) is IOSNativeRecoveryContext, "ios_native_recovery_descriptor")
    context._bounds()
    operations = context._operations
    with operations._changed:
        exports = getattr(operations, "_native_recovery_exports", {})
        _require_recovery(exports.get(id(context)) is context, "ios_native_recovery_descriptor")
        owners = tuple(operations._native_owners.values())
        if owners:
            from .ios_recovery_execution import IOSRecoveryExecution
            _require_recovery(len(owners) == 1, "ios_native_recovery_busy")
            owner = owners[0]
            execution = getattr(owner, "_recovery_context", None)
            _require_recovery(
                type(execution) is IOSRecoveryExecution
                and execution._context is context
                and execution._owner is owner
                and execution._active
                and operations._native_owners.get(id(owner)) is owner
                and all(getattr(item, "_owner", None) is owner
                        for item in operations._native_exports.values())
                and all(getattr(client, "native_owner", None) is owner
                        for client in operations._native_clients),
                "ios_native_recovery_busy",
            )
        else:
            _require_recovery(
                not operations._native_exports and not operations._native_clients,
                "ios_native_recovery_busy",
            )
        _require_recovery(
            not operations._active
            and not operations._callbacks
            and not operations._recoveries,
            "ios_native_recovery_busy",
        )
        _require_recovery(
            context.directory == context._original_directory
            and context.producer == context._original_producer
            and context.device_lease is context._original_lease
            and context.intent == context._intent_snapshot
            and context.native == context._native_snapshot,
            "ios_native_recovery_descriptor",
        )
    _require_recovery(
        _same_identity(os.fstat(context.directory), context.intent["directoryIdentity"], directory=True)
        and _same_identity(os.fstat(context.producer), context.intent["producerIdentity"]),
        "ios_native_recovery_binding",
    )
    try:
        current_intent, current_state = operations._records(
            context.operation_id, context.directory
        )
        current_native = _binding_record(context.directory, current_intent, current_state)
    except Exception:
        _fail("ios_native_recovery_binding")
    _require_recovery(
        current_intent == context.intent
        and current_native == context.native
        and current_state.get("nativeBindingDigest") == context.binding_digest,
        "ios_native_recovery_binding",
    )
    _same_open_lock(context.producer)
    try:
        context._device.require_native_recovery_lease(context.device_lease)
    except Exception:
        _fail("ios_native_recovery_device")
    lease = context.device_lease
    _require_recovery(
        _same_identity(os.fstat(lease.descriptor), context.native["deviceLeaseIdentity"])
        and _same_identity(
            os.fstat(lease.directory_descriptor),
            context.native["deviceDirectoryIdentity"],
            directory=True,
        ),
        "ios_native_recovery_binding",
    )
    try:
        fcntl.flock(lease.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        _fail("ios_native_recovery_descriptor")
    row = operations.run_store.status(context.operation_id)
    _require_recovery(
        row["requestDigest"] == context.request_digest
        and row["state"] in {"admitted", "quarantined"}
        and row["reservedBytes"] == context.intent["reservedBytes"],
        "ios_native_recovery_binding",
    )
    return context


def require_native_recovery(context):
    """Revalidate the exact process-local context before a fixed dispatcher."""

    try:
        return _require_native_recovery(context)
    except IOSNativeRecoveryError:
        raise
    except (RunDenied, OSError, RuntimeError, TypeError, ValueError, KeyError):
        _fail("ios_native_recovery_unavailable")


@contextmanager
def native_recovery(
    operations,
    operation_id,
    request_digest,
    *,
    device,
    snapshot,
    parent_grant,
    cancellation,
    deadline_monotonic,
):
    """Reopen one native-bound iOS operation under the live recovery lease."""

    context = None
    try:
        _validate_context_inputs(
            operations, operation_id, request_digest, cancellation, deadline_monotonic
        )
        _require_device(operations, device)
        with ExitStack() as stack:
            try:
                stack.enter_context(
                    operations.run_store.repair_scope_lease(
                        "mobile-device", operations.definition.scope_digest
                    )
                )
                run_root = _walk_directory(operations.run_store.root)
                stack.callback(os.close, run_root)
                _require_recovery(
                    contracts.digest(str(operations.run_store.root))
                    == operations._configuration["runStoreRootDigest"],
                    "ios_native_recovery_binding",
                )
                vm_lock = _open_regular_at(run_root, ".vm-lock", writable=True)
                stack.callback(os.close, vm_lock)
                try:
                    fcntl.flock(vm_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    _fail("ios_native_recovery_busy")

                private = _walk_directory(operations.root)
                stack.callback(os.close, private)
                _require_recovery(
                    _same_identity(os.fstat(private), operations._root_identity, directory=True),
                    "ios_native_recovery_binding",
                )
                root_names = set(os.listdir(private))
                _require_recovery(
                    root_names <= {"control.lock", "intent.json", "operations"},
                    "ios_native_recovery_journal",
                )
                _require_recovery(
                    _read_json_at(private, "intent.json") == operations._configuration,
                    "ios_native_recovery_binding",
                )
                operation_parent = _open_child_directory(
                    private, "operations", expected=operations._operations_identity
                )
                stack.callback(os.close, operation_parent)
                directory = _open_child_directory(operation_parent, operation_id)
                stack.callback(os.close, directory)
                intent, state = operations._records(operation_id, directory)
                _require_recovery(
                    intent["requestDigest"] == request_digest
                    and intent["configurationDigest"] == operations.configuration_digest
                    and _same_identity(os.fstat(directory), intent["directoryIdentity"], directory=True),
                    "ios_native_recovery_binding",
                )
                native = _binding_record(directory, intent, state)
                _require_recovery(native is not None and native["schemaVersion"] == 2,
                                  "ios_native_recovery_binding")
                _require_recovery(
                    state.get("nativeBindingDigest") == native["bindingDigest"]
                    and all(item["state"] == "prepared" for item in state["roles"].values()),
                    "ios_native_recovery_binding",
                )

                row = operations.run_store.status(operation_id)
                _require_recovery(
                    row["requestDigest"] == request_digest
                    and row["state"] in {"admitted", "quarantined"}
                    and row["reservedBytes"] == intent["reservedBytes"],
                    "ios_native_recovery_binding",
                )
                runs = _open_directory(run_root, "runs")
                stack.callback(os.close, runs)
                run = _open_directory(runs, operation_id)
                stack.callback(os.close, run)
                _require_recovery(set(os.listdir(run)) == {"intent.json"},
                                  "ios_native_recovery_binding")
                run_intent = _record(run, "intent.json")
                _require_recovery(
                    run_intent == {"kind": "ios-mobile-preparation-hold", "contextDigest": intent["contextDigest"]},
                    "ios_native_recovery_binding",
                )
                _validate_operation_journals(operations, directory, intent, state, native)

                producer = _open_regular_at(
                    directory, "producer.lock", expected=intent["producerIdentity"], writable=True
                )
                stack.callback(os.close, producer)
                try:
                    fcntl.flock(producer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    _fail("ios_native_recovery_busy")

                with operations._changed:
                    _require_recovery(
                        not operations._closed
                        and not operations._active
                        and not operations._callbacks
                        and not operations._native_owners
                        and not operations._native_exports
                        and not operations._native_clients
                        and not operations._recoveries,
                        "ios_native_recovery_busy",
                    )
                    _require_recovery(
                        not getattr(operations, "_native_recovery_finalization_exports", {}),
                        "ios_native_recovery_busy",
                    )
                    exports = getattr(operations, "_native_recovery_exports", None)
                    if exports is None:
                        exports = {}
                        operations._native_recovery_exports = exports

                # Reject an operation/root/device mismatch before borrowing
                # the quarantine lease.  ``borrow_native_recovery_lease``
                # fences the durable provider journal as part of its atomic
                # hand-off, so a forged native record must never reach it.
                _require_recovery(
                    native["authorityRootDigest"] == device._authority.authority_root_digest
                    and native["scopeDigest"] == operations.definition.scope_digest
                    and native["ownershipGeneration"] == snapshot.prior_generation
                    and native["hostIncarnation"] == snapshot.prior_host_incarnation
                    and native["helperIncarnation"] == snapshot.prior_helper_incarnation,
                    "ios_native_recovery_binding",
                )
                try:
                    device._require_recovery_snapshot(snapshot, parent_grant)
                    lease_file = os.fstat(device._lease.file.fileno())
                    lease_directory = os.open(
                        device._lease.directory,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    )
                    try:
                        _validate_private_directory(os.fstat(lease_directory))
                        _require_recovery(
                            _same_identity(lease_file, native["deviceLeaseIdentity"])
                            and _same_identity(
                                os.fstat(lease_directory),
                                native["deviceDirectoryIdentity"],
                                directory=True,
                            ),
                            "ios_native_recovery_binding",
                        )
                    finally:
                        os.close(lease_directory)
                except IOSNativeRecoveryError:
                    raise
                except (OSError, RuntimeError, TypeError, ValueError, KeyError):
                    _fail("ios_native_recovery_binding")

                lease = stack.enter_context(
                    device.borrow_native_recovery_lease(snapshot, parent_grant=parent_grant)
                )
                _require_recovery(
                    native["authorityRootDigest"] == device._authority.authority_root_digest
                    and native["scopeDigest"] == operations.definition.scope_digest
                    and native["ownershipGeneration"] == lease.prior_generation == snapshot.prior_generation
                    and native["hostIncarnation"] == lease.prior_host_incarnation == snapshot.prior_host_incarnation
                    and native["helperIncarnation"]
                    == lease.prior_helper_incarnation
                    == snapshot.prior_helper_incarnation
                    and _same_identity(os.fstat(lease.descriptor), native["deviceLeaseIdentity"])
                    and _same_identity(
                        os.fstat(lease.directory_descriptor),
                        native["deviceDirectoryIdentity"],
                        directory=True,
                    ),
                    "ios_native_recovery_binding",
                )
                _require_recovery(
                    lease._grant is parent_grant
                    and lease._snapshot.device_fingerprint == operations.definition.scope_digest,
                    "ios_native_recovery_device",
                )
                public = _ReadOnlyPublic(
                    {
                        "schemaVersion": 1,
                        "kind": "ios-native-recovery-v1",
                        "operationId": operation_id,
                        "requestDigest": request_digest,
                        "contextDigest": intent["contextDigest"],
                        "scopeDigest": operations.definition.scope_digest,
                        "configurationDigest": intent["configurationDigest"],
                        "nativeBindingDigest": native["bindingDigest"],
                        "priorOwnershipGeneration": native["ownershipGeneration"],
                        "state": "native-recovery-required",
                        "deviceCleanupConfirmed": False,
                        "executionAuthority": "none",
                    }
                )
                context = IOSNativeRecoveryContext(
                    operation_id,
                    request_digest,
                    intent["contextDigest"],
                    operations.definition.scope_digest,
                    intent["configurationDigest"],
                    native["bindingDigest"],
                    native["ownershipGeneration"],
                    directory,
                    producer,
                    lease,
                    intent,
                    native,
                    operations,
                    device,
                    parent_grant,
                    cancellation,
                    deadline_monotonic,
                    parent_grant.local_deadline_ns,
                    os.getpid(),
                    threading.get_ident(),
                    threading.current_thread(),
                    directory,
                    producer,
                    lease,
                    parent_grant,
                    cancellation,
                    deadline_monotonic,
                    deepcopy(intent),
                    deepcopy(native),
                    public,
                )
                with operations._changed:
                    _require_recovery(not operations._closed, "ios_native_recovery_busy")
                    operations._native_recovery_exports[id(context)] = context
                require_native_recovery(context)
                yield context
            except IOSNativeRecoveryError:
                raise
            except (IOSMobileOperationError, RunDenied, OSError, RuntimeError, TypeError, ValueError, KeyError):
                _fail()
    finally:
        if context is not None:
            context.close()


__all__ = [
    "IOSNativeRecoveryContext",
    "IOSNativeRecoveryError",
    "IOSNativeRecoveryFinalizationFiles",
    "capture_native_recovery_finalization",
    "native_recovery",
    "require_native_recovery",
    "require_native_recovery_finalization",
]
