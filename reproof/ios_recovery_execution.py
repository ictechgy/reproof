"""Fixed original-app sanitation under one live native iOS recovery lease.

Recovery dispatch capabilities in this module are process-local.  The JSON
journal records their complete immutable fields before dispatch, but reopening
that record never recreates a capability.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import io
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
import uuid
import zipfile

from . import contracts
from .execution.wire import MAX_TRANSFER_BYTES
from .ios_artifact_transfer import (
    MAX_APP_ENTRIES,
    MAX_EXPANDED_APP_BYTES,
    _create_relative_file,
    _ensure_relative_directory,
    _validated_ipa_members,
    parse_ios_artifact,
)
from .ios_mobile_helper import IOSHelperChannel, command_payload
from .ios_mobile_inputs import IOSMobileInputsConfig
from .ios_mobile_native import IOSMobileNativeOwner
from .ios_mobile_operation import IOSMobileOperationError
from .ios_mobile_xctest import IOSXCTestRunner
from .ios_native_recovery import (
    IOSNativeRecoveryContext,
    IOSNativeRecoveryError,
    _validate_command,
    require_native_recovery,
)
from .ios_signing_operation import _write_bytes
from .live.authority import (
    HELPER_VERSION,
    MAX_NS,
    NATIVE_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    QUALIFIED_NATIVE_CLOCKS,
    NativeGrant,
    NativeHandshake,
)
from .repair_android_operation import (
    _identity_info,
    _open_child_directory,
    _open_regular_at,
    _read_fd,
    _read_json_at,
    _replace_at,
    _same_identity,
    _valid_identity,
    _write_new_at,
)


_ROLES = ("original", "helper-host", "helper-runner")
_SLOTS = ("restore-original", "start-original", "cleanup-original")
_SEQUENCES = {name: index + 1 for index, name in enumerate(_SLOTS)}
_ATTEMPT_NAMES = tuple(f"attempt-{number:03d}" for number in range(1, 4))
_RECOVERY_NAME = "native-recovery"
_ARCHIVES_NAME = "archives"
_MAX_RECORD_BYTES = 2 * 1024 * 1024


def _fail(code="ios_recovery_execution_unavailable"):
    raise IOSNativeRecoveryError(code)


def _require(value, code="ios_recovery_execution_unavailable"):
    if not value:
        _fail(code)


def _canonical_result(value):
    if callable(getattr(value, "public", None)):
        value = value.public()
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, UnicodeError):
        _fail("ios_recovery_execution_result")
    _require(len(encoded.encode("utf-8")) <= _MAX_RECORD_BYTES,
             "ios_recovery_execution_result")
    return decoded


class _InspectionBounds:
    def bounds(self):
        return None


class _ExecutionBounds:
    def __init__(self, execution):
        self.execution = execution

    def bounds(self):
        self.execution._context.require()


def _expected_app_tree(recovery_fd, root_intent, root_state, role, operation_root):
    from .ios_mobile_recovery import _expected_tree

    archives = archive = None
    try:
        archives = _open_child_directory(
            recovery_fd, _ARCHIVES_NAME,
            expected=root_intent["archivesDirectoryIdentity"],
        )
        row = root_state["archives"][role]
        archive = _open_regular_at(
            archives, role + ".ipa", expected=row["identity"]
        )
        body = _read_fd(archive, MAX_TRANSFER_BYTES)
        _require(len(body) == row["bytes"]
                 and hashlib.sha256(body).hexdigest() == row["sha256"],
                 "ios_recovery_execution_materials")
        files, directories = _expected_tree(
            archive,
            Path(operation_root) / _RECOVERY_NAME / _ARCHIVES_NAME / (role + ".ipa"),
        )
        return files, directories, body
    finally:
        if archive is not None:
            os.close(archive)
        if archives is not None:
            os.close(archives)


def _subset_files(directory, *, prefix="", depth=0, budget=None):
    _require(depth <= 512, "ios_recovery_execution_materials")
    if budget is None:
        budget = {"entries": 0, "bytes": 0, "files": {}}
    for name in os.listdir(directory):
        relative = name if not prefix else prefix + "/" + name
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        budget["entries"] += 1
        _require(budget["entries"] <= MAX_APP_ENTRIES and info.st_uid == os.getuid(),
                 "ios_recovery_execution_materials")
        if stat.S_ISDIR(info.st_mode):
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
            try:
                _require(_same_identity(os.fstat(child), _identity_info(info), directory=True),
                         "ios_recovery_execution_materials")
                _subset_files(child, prefix=relative, depth=depth + 1, budget=budget)
            finally:
                os.close(child)
        else:
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                     and stat.S_IMODE(info.st_mode) in (0o600, 0o700),
                     "ios_recovery_execution_materials")
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            try:
                actual = os.fstat(descriptor)
                _require((actual.st_dev, actual.st_ino, actual.st_mode, actual.st_uid,
                          actual.st_nlink, actual.st_size)
                         == (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                             info.st_nlink, info.st_size),
                         "ios_recovery_execution_materials")
                body = _read_fd(descriptor, info.st_size, allow_empty=True)
            finally:
                os.close(descriptor)
            budget["bytes"] += len(body)
            _require(budget["bytes"] <= MAX_EXPANDED_APP_BYTES,
                     "ios_recovery_execution_materials")
            budget["files"][relative] = (
                len(body), hashlib.sha256(body).hexdigest(),
                bool(stat.S_IMODE(info.st_mode) & 0o111),
            )
    return budget["files"]


def _validate_subset_contents(app_fd, archive_body):
    observed = _subset_files(app_fd)
    with zipfile.ZipFile(io.BytesIO(archive_body), "r", allowZip64=True) as archive:
        entries, app_name = _validated_ipa_members(
            archive.infolist(), max_bytes=MAX_EXPANDED_APP_BYTES,
            max_entries=MAX_APP_ENTRIES,
        )
        prefix = "Payload/" + app_name + "/"
        expected = {
            name[len(prefix):]: entry for entry, name, directory in entries
            if not directory and name.startswith(prefix)
        }
        for relative, (size, digest, executable) in observed.items():
            entry = expected.get(relative)
            expected_mode = (entry.external_attr >> 16) & 0xFFFF if entry is not None else 0
            _require(entry is not None and size <= entry.file_size
                     and executable == bool(expected_mode & 0o111),
                     "ios_recovery_execution_materials")
            checksum = hashlib.sha256()
            remaining = size
            with archive.open(entry, "r") as source:
                while remaining:
                    block = source.read(min(1024 * 1024, remaining))
                    _require(block, "ios_recovery_execution_materials")
                    checksum.update(block)
                    remaining -= len(block)
            _require(checksum.hexdigest() == digest,
                     "ios_recovery_execution_materials")


def _validate_app_subset(app_fd, expected, bounds, *, remove=False):
    from .ios_mobile_recovery import _tree

    files, directories, archive_body = expected
    _tree(app_fd, files, directories, bounds, remove=False)
    _validate_subset_contents(app_fd, archive_body)
    if remove:
        _tree(app_fd, files, directories, bounds, remove=True)


def _extract_registered_app(body, app_fd):
    """Extract one already verified registered IPA into a recorded empty app inode."""
    with zipfile.ZipFile(io.BytesIO(body), "r", allowZip64=True) as archive:
        entries, app_name = _validated_ipa_members(
            archive.infolist(), max_bytes=MAX_EXPANDED_APP_BYTES,
            max_entries=MAX_APP_ENTRIES,
        )
        prefix = "Payload/" + app_name + "/"
        for _entry, name, directory in entries:
            if not directory or not name.startswith(prefix) or name == prefix[:-1]:
                continue
            relative = name[len(prefix):]
            selected = _ensure_relative_directory(app_fd, relative)
            try:
                os.fchmod(selected, 0o700)
                os.fsync(selected)
            finally:
                os.close(selected)
        copied = 0
        for entry, name, directory in entries:
            if directory or not name.startswith(prefix):
                continue
            relative = name[len(prefix):]
            descriptor = _create_relative_file(app_fd, relative)
            try:
                mode = (entry.external_attr >> 16) & 0xFFFF
                os.fchmod(descriptor, 0o700 if mode & 0o111 else 0o600)
                written = 0
                with archive.open(entry, "r") as source:
                    while True:
                        block = source.read(min(1024 * 1024, entry.file_size - written + 1))
                        if not block:
                            break
                        written += len(block)
                        copied += len(block)
                        _require(written <= entry.file_size
                                 and copied <= MAX_EXPANDED_APP_BYTES,
                                 "ios_recovery_execution_materials")
                        offset = 0
                        while offset < len(block):
                            count = os.write(descriptor, block[offset:])
                            _require(count > 0, "ios_recovery_execution_materials")
                            offset += count
                _require(written == entry.file_size,
                         "ios_recovery_execution_materials")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    os.fsync(app_fd)


def _regular_identity(parent, name, expected, *, maximum, allow_empty=False):
    descriptor = _open_regular_at(parent, name, expected=expected)
    try:
        before = os.fstat(descriptor)
        _require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.getuid()
            and before.st_nlink == 1
            and not before.st_mode & 0o022
            and (0 <= before.st_size <= maximum if allow_empty
                 else 0 < before.st_size <= maximum),
            "ios_recovery_execution_storage",
        )
        body = _read_fd(descriptor, maximum, allow_empty=allow_empty)
        after = os.fstat(descriptor)
        _require(
            _same_identity(after, expected)
            and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "ios_recovery_execution_storage",
        )
        return body
    finally:
        os.close(descriptor)


def _open_recorded_app(parent, expected=None):
    descriptor = None
    try:
        descriptor = os.open(
            "App.app",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
        info = os.fstat(descriptor)
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) == 0o700
                 and (expected is None or _same_identity(info, expected, directory=True)),
                 "ios_recovery_execution_materials")
        result, descriptor = descriptor, None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _root_intent(context, recovery_identity, archives_identity, expected):
    return {
        "schemaVersion": 1,
        "kind": "ios-native-recovery-materials-v1",
        "operationId": context.operation_id,
        "requestDigest": context.request_digest,
        "contextDigest": context.context_digest,
        "configurationDigest": context.configuration_digest,
        "scopeDigest": context.scope_digest,
        "nativeBindingDigest": context.binding_digest,
        "operationDirectoryIdentity": context.intent["directoryIdentity"],
        "recoveryDirectoryIdentity": recovery_identity,
        "archivesDirectoryIdentity": archives_identity,
        "archives": expected,
    }


def _root_state(context, archive_rows, intent, intent_identity):
    return {
        "schemaVersion": 1,
        "kind": "ios-native-recovery-materials-state-v1",
        "operationId": context.operation_id,
        "nativeBindingDigest": context.binding_digest,
        "intentDigest": contracts.digest(intent),
        "intentIdentity": intent_identity,
        "archiveState": "preparing",
        "archiveDisposalDigest": None,
        "archives": archive_rows,
    }


def _archive_disposal_digest(root_state):
    return contracts.digest({
        "schemaVersion": 1,
        "kind": "ios-native-recovery-archive-disposal-v1",
        "operationId": root_state["operationId"],
        "nativeBindingDigest": root_state["nativeBindingDigest"],
        "archives": root_state["archives"],
    })


def _binding_matches(record, intent, native):
    return (
        type(record) is dict
        and record.get("operationId") == intent["operationId"]
        and record.get("requestDigest", intent["requestDigest"]) == intent["requestDigest"]
        and record.get("contextDigest", intent["contextDigest"]) == intent["contextDigest"]
        and record.get("configurationDigest", intent["configurationDigest"])
        == intent["configurationDigest"]
        and record.get("scopeDigest", intent["context"]["scope_digest"])
        == intent["context"]["scope_digest"]
        and record.get("nativeBindingDigest") == native["bindingDigest"]
    )


def _validate_archive_rows(recovery_fd, root_intent, root_state):
    _require(
        type(root_state) is dict
        and set(root_state) == {
            "schemaVersion", "kind", "operationId", "nativeBindingDigest",
            "intentDigest", "intentIdentity", "archiveState", "archiveDisposalDigest",
            "archives",
        }
        and root_state["schemaVersion"] == 1
        and root_state["kind"] == "ios-native-recovery-materials-state-v1"
        and root_state["operationId"] == root_intent["operationId"]
        and root_state["nativeBindingDigest"] == root_intent["nativeBindingDigest"]
        and root_state["intentDigest"] == contracts.digest(root_intent)
        and _valid_identity(root_state["intentIdentity"])
        and root_state["archiveState"] in {"preparing", "ready", "discarding", "discarded"}
        and ((root_state["archiveDisposalDigest"] is None)
             == (root_state["archiveState"] != "discarded"))
        and type(root_state["archives"]) is dict
        and set(root_state["archives"]) == set(_ROLES),
        "ios_recovery_execution_journal",
    )
    if root_state["archiveState"] == "discarded":
        _require(
            root_state["archiveDisposalDigest"] == _archive_disposal_digest(root_state),
            "ios_recovery_execution_journal",
        )
    _regular_identity(
        recovery_fd, "intent.json", root_state["intentIdentity"],
        maximum=_MAX_RECORD_BYTES,
    )
    archives = _open_child_directory(
        recovery_fd, _ARCHIVES_NAME, expected=root_intent["archivesDirectoryIdentity"]
    )
    try:
        names = set(os.listdir(archives))
        expected_names = {role + ".ipa" for role in _ROLES}
        _require(
            (root_state["archiveState"] == "preparing" and names <= expected_names)
            or (root_state["archiveState"] == "ready" and names == expected_names)
            or (root_state["archiveState"] == "discarding" and names <= expected_names)
            or (root_state["archiveState"] == "discarded" and not names),
            "ios_recovery_execution_journal",
        )
        for role in _ROLES:
            expected = root_intent["archives"][role]
            row = root_state["archives"][role]
            _require(type(expected) is dict
                     and set(expected) == {"sha256", "bytes", "bundleId"}
                     and type(row) is dict
                     and row.get("sha256") == expected["sha256"]
                     and row.get("bytes") == expected["bytes"],
                     "ios_recovery_execution_journal")
            filename = role + ".ipa"
            if root_state["archiveState"] == "preparing":
                _require(
                    set(row) == {"sha256", "bytes", "identity", "state"}
                    and row["state"] in {"pending", "creating", "writing", "ready"}
                    and ((row["identity"] is None)
                         == (row["state"] in {"pending", "creating"})),
                    "ios_recovery_execution_journal",
                )
                if row["state"] == "pending":
                    _require(filename not in names, "ios_recovery_execution_journal")
                elif row["state"] == "creating":
                    _require(filename not in names or row["identity"] is None,
                             "ios_recovery_execution_journal")
                else:
                    _require(filename in names and _valid_identity(row["identity"]),
                             "ios_recovery_execution_journal")
            else:
                _require(set(row) == {"sha256", "bytes", "identity"}
                         and _valid_identity(row["identity"]),
                         "ios_recovery_execution_journal")
            if filename in names:
                if row.get("identity") is None:
                    descriptor = _open_regular_at(archives, filename)
                    try:
                        info = os.fstat(descriptor)
                        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                                 and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
                                 and info.st_size <= expected["bytes"],
                                 "ios_recovery_execution_journal")
                    finally:
                        os.close(descriptor)
                    continue
                body = _regular_identity(
                    archives, filename, row["identity"], maximum=MAX_TRANSFER_BYTES,
                    allow_empty=(root_state["archiveState"] == "preparing"
                                 and row["state"] == "writing"),
                )
                if root_state["archiveState"] == "preparing" and row["state"] == "writing":
                    _require(len(body) <= expected["bytes"],
                             "ios_recovery_execution_journal")
                else:
                    _require(len(body) == expected["bytes"]
                             and hashlib.sha256(body).hexdigest() == expected["sha256"],
                             "ios_recovery_execution_journal")
    finally:
        os.close(archives)


def _validate_slot(name, row, attempt_intent):
    if row is None:
        return
    keys = {
        "protocolVersion", "operationId", "operationFingerprint", "payloadDigest",
        "projectId", "sessionId", "controllerId", "sequence",
        "ownershipGeneration", "hostIncarnation", "helperIncarnation",
        "providerIncarnation", "deadlineNs", "deviceFingerprint",
        "grantFingerprint", "state", "resultDigest",
    }
    _require(
        type(row) is dict
        and set(row) == keys
        and row["protocolVersion"] == PROTOCOL_VERSION
        and row["sequence"] == _SEQUENCES[name]
        and row["ownershipGeneration"] == attempt_intent["priorOwnershipGeneration"]
        and row["hostIncarnation"] == attempt_intent["hostIncarnation"]
        and row["helperIncarnation"] == attempt_intent["helperIncarnation"]
        and row["projectId"] == attempt_intent["projectId"]
        and row["sessionId"] == attempt_intent["sessionId"]
        and row["controllerId"] == attempt_intent["controllerId"]
        and row["deviceFingerprint"] == attempt_intent["scopeDigest"]
        and row["grantFingerprint"] == attempt_intent["grantFingerprint"]
        and type(row["deadlineNs"]) is int
        and 0 < row["deadlineNs"] <= attempt_intent["grantDeadlineNs"]
        and row["state"] in {"issued", "settled", "failed"}
        and ((row["resultDigest"] is None) == (row["state"] == "issued")),
        "ios_recovery_execution_journal",
    )
    for key in (
        "operationId", "projectId", "sessionId", "controllerId",
        "hostIncarnation", "helperIncarnation", "providerIncarnation",
    ):
        contracts.validate_id(row[key])
    for key in ("operationFingerprint", "payloadDigest", "deviceFingerprint",
                "grantFingerprint"):
        contracts.validate_digest(row[key])
    if row["resultDigest"] is not None:
        contracts.validate_digest(row["resultDigest"])
    fingerprint_fields = {
        "protocolVersion": row["protocolVersion"],
        "deviceFingerprint": row["deviceFingerprint"],
        "operationId": row["operationId"],
        "payloadDigest": row["payloadDigest"],
        "projectId": row["projectId"],
        "sessionId": row["sessionId"],
        "controllerId": row["controllerId"],
        "ownershipGeneration": row["ownershipGeneration"],
        "hostIncarnation": row["hostIncarnation"],
        "helperIncarnation": row["helperIncarnation"],
        "deadlineNs": row["deadlineNs"],
        "sequence": row["sequence"],
        "providerIncarnation": row["providerIncarnation"],
        "grantFingerprint": row["grantFingerprint"],
        "nativeBindingDigest": attempt_intent["nativeBindingDigest"],
        "contextDigest": attempt_intent["contextDigest"],
        "slot": name,
    }
    _require(row["operationFingerprint"] == contracts.digest(fingerprint_fields),
             "ios_recovery_execution_journal")


def _validate_attempt(recovery_fd, name, intent, native, root_intent, root_state,
                      operation_root):
    attempt_fd = _open_child_directory(recovery_fd, name)
    try:
        names = set(os.listdir(attempt_fd))
        allowed = {"intent.json", "state.json", *_ROLES,
                   "command-restore-original-work", "command-xctest-original-001-work"}
        _require(names <= allowed and ("state.json" not in names or "intent.json" in names),
                 "ios_recovery_execution_journal")
        if "intent.json" not in names:
            _require(not names, "ios_recovery_execution_journal")
            return None, None
        attempt_intent = _read_json_at(attempt_fd, "intent.json", _MAX_RECORD_BYTES)
        intent_keys = {
            "schemaVersion", "kind", "attempt", "operationId", "requestDigest",
            "contextDigest", "configurationDigest", "scopeDigest",
            "nativeBindingDigest", "recoveryDirectoryIdentity", "attemptDirectoryIdentity",
            "priorOwnershipGeneration", "hostIncarnation", "helperIncarnation",
            "projectId", "sessionId", "controllerId", "grantId",
            "grantFingerprint", "grantDeadlineNs", "roles",
        }
        _require(
            type(attempt_intent) is dict
            and set(attempt_intent) == intent_keys
            and attempt_intent["schemaVersion"] == 1
            and attempt_intent["kind"] == "ios-native-recovery-attempt-v1"
            and attempt_intent["attempt"] == int(name[-3:])
            and _binding_matches(attempt_intent, intent, native)
            and _same_identity(
                os.fstat(recovery_fd), attempt_intent["recoveryDirectoryIdentity"],
                directory=True,
            )
            and _same_identity(
                os.fstat(attempt_fd), attempt_intent["attemptDirectoryIdentity"], directory=True
            )
            and attempt_intent["priorOwnershipGeneration"] == native["ownershipGeneration"]
            and type(attempt_intent["grantDeadlineNs"]) is int
            and type(attempt_intent["roles"]) is dict
            and set(attempt_intent["roles"]) == set(_ROLES),
            "ios_recovery_execution_journal",
        )
        for key in ("hostIncarnation", "helperIncarnation", "projectId", "sessionId",
                    "controllerId", "grantId"):
            contracts.validate_id(attempt_intent[key])
        contracts.validate_digest(attempt_intent["grantFingerprint"])
        if "state.json" not in names:
            _require(names == {"intent.json"}, "ios_recovery_execution_journal")
            return attempt_intent, None
        state = _read_json_at(attempt_fd, "state.json", _MAX_RECORD_BYTES)
        state_keys = {
            "schemaVersion", "kind", "attempt", "operationId", "nativeBindingDigest",
            "materialState", "roles", "slots", "outcome", "observationDigest",
            "materialDisposalDigest", "intentDigest", "intentIdentity",
        }
        _require(
            type(state) is dict
            and set(state) == state_keys
            and state["schemaVersion"] == 1
            and state["kind"] == "ios-native-recovery-attempt-state-v1"
            and state["attempt"] == attempt_intent["attempt"]
            and state["operationId"] == intent["operationId"]
            and state["nativeBindingDigest"] == native["bindingDigest"]
            and state["intentDigest"] == contracts.digest(attempt_intent)
            and _valid_identity(state["intentIdentity"])
            and state["materialState"] in {"preparing", "ready", "retiring", "retired"}
            and type(state["roles"]) is dict
            and set(state["roles"]) == set(_ROLES)
            and type(state["slots"]) is dict
            and set(state["slots"]) == set(_SLOTS)
            and state["outcome"] in {"pending", "succeeded", "failed"}
            and (state["observationDigest"] is None or state["outcome"] == "succeeded"),
            "ios_recovery_execution_journal",
        )
        _regular_identity(
            attempt_fd, "intent.json", state["intentIdentity"],
            maximum=_MAX_RECORD_BYTES,
        )
        if state["observationDigest"] is not None:
            contracts.validate_digest(state["observationDigest"])
        _require(
            (state["materialDisposalDigest"] is None)
            == (state["materialState"] != "retired"),
            "ios_recovery_execution_journal",
        )
        if state["materialDisposalDigest"] is not None:
            contracts.validate_digest(state["materialDisposalDigest"])
        for slot in _SLOTS:
            _validate_slot(slot, state["slots"][slot], attempt_intent)
        for role in _ROLES:
            expected = attempt_intent["roles"][role]
            row = state["roles"][role]
            _require(
                type(expected) is dict
                and set(expected) == {"bundleId", "archiveSha256", "appDigest"}
                and type(row) is dict
                and row.get("state") in {
                    "pending", "creating", "preparing", "app-creating", "extracting",
                    "ready", "retiring", "retired"
                },
                "ios_recovery_execution_journal",
            )
            if row["state"] == "pending":
                _require(set(row) == {"state"} and role not in names,
                         "ios_recovery_execution_journal")
            elif row["state"] == "creating":
                _require(set(row) == {"state", "directoryIdentity"}
                         and row["directoryIdentity"] is None,
                         "ios_recovery_execution_journal")
                if role in names:
                    role_fd = _open_child_directory(attempt_fd, role)
                    try:
                        _require(not os.listdir(role_fd), "ios_recovery_execution_journal")
                    finally:
                        os.close(role_fd)
            elif row["state"] == "preparing":
                _require(set(row) == {"state", "directoryIdentity"}
                         and _valid_identity(row["directoryIdentity"], directory=True)
                         and role in names,
                         "ios_recovery_execution_journal")
                role_fd = _open_child_directory(
                    attempt_fd, role, expected=row["directoryIdentity"]
                )
                try:
                    _require(not os.listdir(role_fd), "ios_recovery_execution_journal")
                finally:
                    os.close(role_fd)
            elif row["state"] == "app-creating":
                _require(set(row) == {"state", "directoryIdentity", "appDirectoryIdentity"}
                         and _valid_identity(row["directoryIdentity"], directory=True)
                         and row["appDirectoryIdentity"] is None and role in names,
                         "ios_recovery_execution_journal")
                role_fd = _open_child_directory(
                    attempt_fd, role, expected=row["directoryIdentity"]
                )
                try:
                    children = set(os.listdir(role_fd))
                    _require(children <= {"App.app"}, "ios_recovery_execution_journal")
                    if "App.app" in children:
                        app_fd = _open_recorded_app(role_fd)
                        try:
                            _require(not os.listdir(app_fd), "ios_recovery_execution_journal")
                        finally:
                            os.close(app_fd)
                finally:
                    os.close(role_fd)
            elif row["state"] == "extracting":
                _require(set(row) == {"state", "directoryIdentity", "appDirectoryIdentity",
                                      "appDigest"}
                         and _valid_identity(row["directoryIdentity"], directory=True)
                         and _valid_identity(row["appDirectoryIdentity"], directory=True)
                         and row["appDigest"] == expected["appDigest"] and role in names,
                         "ios_recovery_execution_journal")
                role_fd = _open_child_directory(
                    attempt_fd, role, expected=row["directoryIdentity"]
                )
                try:
                    _require(set(os.listdir(role_fd)) == {"App.app"},
                             "ios_recovery_execution_journal")
                    app_fd = _open_recorded_app(role_fd, row["appDirectoryIdentity"])
                    try:
                        expected_tree = _expected_app_tree(
                            recovery_fd, root_intent, root_state, role, operation_root
                        )
                        _validate_app_subset(app_fd, expected_tree, _InspectionBounds())
                    finally:
                        os.close(app_fd)
                finally:
                    os.close(role_fd)
            elif row["state"] == "retired":
                _require(
                    set(row) == {"state", "directoryIdentity", "appDirectoryIdentity", "appDigest"}
                    and (row["directoryIdentity"] is None
                         or _valid_identity(row["directoryIdentity"], directory=True))
                    and (row["appDirectoryIdentity"] is None
                         or _valid_identity(row["appDirectoryIdentity"], directory=True))
                    and row["appDigest"] == expected["appDigest"]
                    and role not in names,
                    "ios_recovery_execution_journal",
                )
            elif row["state"] == "retiring":
                _require(
                    set(row) == {"state", "directoryIdentity", "appDirectoryIdentity", "appDigest"}
                    and _valid_identity(row["directoryIdentity"], directory=True)
                    and (row["appDirectoryIdentity"] is None
                         or _valid_identity(row["appDirectoryIdentity"], directory=True))
                    and row["appDigest"] == expected["appDigest"],
                    "ios_recovery_execution_journal",
                )
                if role in names:
                    role_fd = _open_child_directory(
                        attempt_fd, role, expected=row["directoryIdentity"]
                    )
                    try:
                        children = set(os.listdir(role_fd))
                        _require(children <= {"App.app"}
                                 and ("App.app" not in children
                                      or row["appDirectoryIdentity"] is not None),
                                 "ios_recovery_execution_journal")
                        if "App.app" in children:
                            info = os.stat("App.app", dir_fd=role_fd, follow_symlinks=False)
                            _require(_same_identity(info, row["appDirectoryIdentity"], directory=True),
                                     "ios_recovery_execution_journal")
                            app_fd = _open_recorded_app(
                                role_fd, row["appDirectoryIdentity"]
                            )
                            try:
                                expected_tree = _expected_app_tree(
                                    recovery_fd, root_intent, root_state, role, operation_root
                                )
                                _validate_app_subset(
                                    app_fd, expected_tree, _InspectionBounds()
                                )
                            finally:
                                os.close(app_fd)
                    finally:
                        os.close(role_fd)
            else:
                _require(
                    set(row) == {"state", "directoryIdentity", "appDirectoryIdentity", "appDigest"}
                    and _valid_identity(row["directoryIdentity"], directory=True)
                    and _valid_identity(row["appDirectoryIdentity"], directory=True)
                    and row["appDigest"] == expected["appDigest"]
                    and role in names,
                    "ios_recovery_execution_journal",
                )
                role_fd = _open_child_directory(
                    attempt_fd, role, expected=row["directoryIdentity"]
                )
                try:
                    _require(set(os.listdir(role_fd)) == {"App.app"},
                             "ios_recovery_execution_journal")
                    app_info = os.stat("App.app", dir_fd=role_fd, follow_symlinks=False)
                    _require(_same_identity(app_info, row["appDirectoryIdentity"], directory=True),
                             "ios_recovery_execution_journal")
                    app = parse_ios_artifact(
                        Path(operation_root) / _RECOVERY_NAME / name / role / "App.app"
                    )
                    _require(
                        app.app_digest == expected["appDigest"]
                        and app.manifest["applicationId"] == expected["bundleId"],
                        "ios_recovery_execution_journal",
                    )
                finally:
                    os.close(role_fd)
        if state["materialState"] == "ready":
            _require(all(row["state"] == "ready" for row in state["roles"].values()),
                     "ios_recovery_execution_journal")
        if state["materialState"] == "preparing":
            _require(not (names & {"command-restore-original-work",
                                   "command-xctest-original-001-work"}),
                     "ios_recovery_execution_journal")
        if state["materialState"] == "retired":
            _require(all(row["state"] == "retired" for row in state["roles"].values()),
                     "ios_recovery_execution_journal")
        for command in names - {"intent.json", "state.json", *_ROLES}:
            command_fd = _open_child_directory(attempt_fd, command)
            try:
                _validate_command((command, command_fd), native, intent)
            finally:
                os.close(command_fd)
        return attempt_intent, state
    finally:
        os.close(attempt_fd)


def _validate_recovery_journal(operation_fd, intent, state, native, operation_root):
    """Validate all persisted recovery material and attempts without issuing authority."""
    recovery_fd = _open_child_directory(operation_fd, _RECOVERY_NAME)
    try:
        names = set(os.listdir(recovery_fd))
        _require(
            names <= {"intent.json", "state.json", _ARCHIVES_NAME, *_ATTEMPT_NAMES}
            and ("state.json" not in names or "intent.json" in names),
            "ios_recovery_execution_journal",
        )
        if "intent.json" not in names:
            _require(names <= {_ARCHIVES_NAME}, "ios_recovery_execution_journal")
            if _ARCHIVES_NAME in names:
                archives = _open_child_directory(recovery_fd, _ARCHIVES_NAME)
                try:
                    _require(not os.listdir(archives), "ios_recovery_execution_journal")
                finally:
                    os.close(archives)
            return ()
        root_intent = _read_json_at(recovery_fd, "intent.json", _MAX_RECORD_BYTES)
        keys = {
            "schemaVersion", "kind", "operationId", "requestDigest", "contextDigest",
            "configurationDigest", "scopeDigest", "nativeBindingDigest",
            "operationDirectoryIdentity", "recoveryDirectoryIdentity",
            "archivesDirectoryIdentity", "archives",
        }
        _require(
            type(root_intent) is dict
            and set(root_intent) == keys
            and root_intent["schemaVersion"] == 1
            and root_intent["kind"] == "ios-native-recovery-materials-v1"
            and _binding_matches(root_intent, intent, native)
            and root_intent["operationDirectoryIdentity"] == intent["directoryIdentity"]
            and _same_identity(
                os.fstat(recovery_fd), root_intent["recoveryDirectoryIdentity"], directory=True
            )
            and type(root_intent["archives"]) is dict
            and set(root_intent["archives"]) == set(_ROLES),
            "ios_recovery_execution_journal",
        )
        _require(_ARCHIVES_NAME in names, "ios_recovery_execution_journal")
        if "state.json" not in names:
            _require(names == {"intent.json", _ARCHIVES_NAME},
                     "ios_recovery_execution_journal")
            archives = _open_child_directory(
                recovery_fd, _ARCHIVES_NAME,
                expected=root_intent["archivesDirectoryIdentity"],
            )
            try:
                _require(not os.listdir(archives), "ios_recovery_execution_journal")
            finally:
                os.close(archives)
            return ()
        root_state = _read_json_at(recovery_fd, "state.json", _MAX_RECORD_BYTES)
        _validate_archive_rows(recovery_fd, root_intent, root_state)
        attempts = sorted(names & set(_ATTEMPT_NAMES))
        _require(attempts == list(_ATTEMPT_NAMES[:len(attempts)]),
                 "ios_recovery_execution_journal")
        attempts = tuple(
            _validate_attempt(
                recovery_fd, name, intent, native, root_intent, root_state,
                operation_root,
            )
            for name in attempts
        )
        if root_state["archiveState"] == "preparing":
            _require(not attempts, "ios_recovery_execution_journal")
        if root_state["archiveState"] in {"discarding", "discarded"}:
            _require(
                attempts
                and all(item[1]["materialState"] == "retired" for item in attempts),
                "ios_recovery_execution_journal",
            )
        return attempts
    except IOSNativeRecoveryError:
        raise
    except Exception:
        _fail("ios_recovery_execution_journal")
    finally:
        os.close(recovery_fd)


@dataclass(frozen=True, slots=True)
class IOSRecoveryDispatch:
    protocol_version: int
    operation_id: str
    operation_fingerprint: str
    payload_digest: str
    project_id: str
    session_id: str
    controller_id: str
    sequence: int
    ownership_generation: int
    host_incarnation: str
    helper_incarnation: str
    provider_incarnation: str
    deadline_ns: int
    _device_fingerprint: str = field(repr=False, compare=False)
    _slot: str = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True, repr=False)
class IOSRecoveryExecutionObservation:
    context_digest: str
    native_binding_digest: str
    attempt: int
    restore_observation: object = field(repr=False, compare=False)
    installed_identity: object = field(repr=False, compare=False)
    launch_observation: object = field(repr=False, compare=False)
    cleanup_observation: object = field(repr=False, compare=False)
    handshake: NativeHandshake = field(repr=False, compare=False)
    shutdown_observation: object = field(repr=False, compare=False)
    cleanup_evidence_digest: str
    cleanup_receipt_digest: str
    evidence_digest: str
    _execution: object = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)

    def __repr__(self):
        return "<IOSRecoveryExecutionObservation>"

    def public(self):
        disposal = self._execution._observation_material_disposal(self)
        return {
            "schemaVersion": 1,
            "kind": "ios-native-recovery-execution-observation-v1",
            "contextDigest": self.context_digest,
            "nativeBindingDigest": self.native_binding_digest,
            "attempt": self.attempt,
            "evidenceDigest": self.evidence_digest,
            "cleanupEvidenceDigest": self.cleanup_evidence_digest,
            "cleanupReceiptDigest": self.cleanup_receipt_digest,
            "materialsDisposed": disposal is not None,
            "originalSanitationConfirmed": True,
            "ownershipReleased": False,
            "deviceReconciled": False,
            "deviceCleanupConfirmed": False,
            "executionAuthority": "none",
        } | ({"materialDisposalDigest": disposal} if disposal is not None else {})


class _RecoveredContext:
    __slots__ = ("_execution",)

    def __init__(self, execution):
        self._execution = execution

    @property
    def operation_id(self):
        self._execution._require_active()
        return self._execution._context.operation_id

    @property
    def digest(self):
        self._execution._require_active()
        return self._execution._context.context_digest


class _RecoveredOperation:
    __slots__ = ("context",)

    def __init__(self, execution):
        self.context = _RecoveredContext(execution)


class IOSRecoveryExecution:
    """One bounded, non-resumable authority set for a fixed recovery attempt."""

    def __init__(self, context, config):
        _require(type(context) is IOSNativeRecoveryContext,
                 "ios_recovery_execution_context")
        _require(type(config) is IOSMobileInputsConfig,
                 "ios_recovery_execution_configuration")
        self._context = context
        self._config = config
        self._operations = context._operations
        self._issuer = object()
        self._owner = None
        self._active = False
        self._entered = False
        self._cleanup_unknown = False
        self._recovery_fd = None
        self._attempt_fd = None
        self._attempt = None
        self._attempt_name = None
        self._attempt_root = None
        self._attempt_intent = None
        self._attempt_state = None
        self._baseline_bodies = None
        self._issued = {}
        self._issued_by_slot = {}
        self._handshakes = {}
        self._observations = {}
        self._observation_evidence = {}
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._thread = threading.get_ident()
        self._thread_object = threading.current_thread()
        self._helper_incarnation = "ios_recovery_helper_" + uuid.uuid4().hex[:24]

    def __repr__(self):
        return "<IOSRecoveryExecution>"

    def _same_thread(self):
        return (
            self._pid == os.getpid()
            and self._thread == threading.get_ident()
            and self._thread_object is threading.current_thread()
        )

    def _require_active(self):
        _require(self._active and self._same_thread() and not self._cleanup_unknown,
                 "ios_recovery_execution_inactive")
        require_native_recovery(self._context)
        return self

    @property
    def command_directory(self):
        self._require_active()
        _require(type(self._attempt_fd) is int, "ios_recovery_execution_storage")
        return self._attempt_fd

    @property
    def command_root(self):
        self._require_active()
        return self._attempt_root

    @property
    def helper_incarnation(self):
        self._require_active()
        return self._helper_incarnation

    @property
    def native_deadline_ns(self):
        self._require_active()
        return self._context.grant_deadline_ns

    def _baseline_snapshot(self):
        require_native_recovery(self._context)
        _require(
            self._config.definition == self._operations.definition,
            "ios_recovery_execution_configuration",
        )
        baselines = self._config.read_baselines()
        _require(
            baselines.digest == self._operations.definition.baseline_digest
            and {name for name, _ in baselines.entries}
            == {role + ".ipa" for role in _ROLES},
            "ios_recovery_execution_configuration",
        )
        bodies = dict(baselines.entries)
        for role in _ROLES:
            record = self._context.intent["roles"][role]
            body = bodies[role + ".ipa"]
            _require(
                len(body) == record["bytes"]
                and hashlib.sha256(body).hexdigest() == record["sha256"],
                "ios_recovery_execution_configuration",
            )
        return {role: bodies[role + ".ipa"] for role in _ROLES}

    def _expected_archives(self):
        bundles = self._operations._roles
        return {
            role: {
                "sha256": hashlib.sha256(self._baseline_bodies[role]).hexdigest(),
                "bytes": len(self._baseline_bodies[role]),
                "bundleId": bundles[role],
            }
            for role in _ROLES
        }

    @staticmethod
    def _initial_archive_rows(expected):
        return {
            role: {
                "sha256": expected[role]["sha256"],
                "bytes": expected[role]["bytes"],
                "identity": None,
                "state": "pending",
            }
            for role in _ROLES
        }

    def _resume_archives(self, recovery_fd, root_intent, state):
        _require(state["archiveState"] == "preparing",
                 "ios_recovery_execution_materials")
        archives = _open_child_directory(
            recovery_fd, _ARCHIVES_NAME,
            expected=root_intent["archivesDirectoryIdentity"],
        )
        try:
            for role in _ROLES:
                self._context.require()
                filename = role + ".ipa"
                body = self._baseline_bodies[role]
                row = state["archives"][role]
                if row["state"] == "pending":
                    _require(filename not in os.listdir(archives),
                             "ios_recovery_execution_materials")
                    row["state"] = "creating"
                    _replace_at(recovery_fd, "state.json", state)
                if row["state"] == "creating":
                    descriptor = None
                    try:
                        try:
                            descriptor = os.open(
                                filename,
                                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600,
                                dir_fd=archives,
                            )
                            os.fsync(archives)
                        except FileExistsError:
                            descriptor = _open_regular_at(archives, filename, writable=True)
                        info = os.fstat(descriptor)
                        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                                 and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
                                 and info.st_size <= len(body),
                                 "ios_recovery_execution_materials")
                        existing = _read_fd(descriptor, len(body), allow_empty=True)
                        _require(body.startswith(existing),
                                 "ios_recovery_execution_materials")
                        row["identity"] = _identity_info(info)
                        row["state"] = "writing"
                        _replace_at(recovery_fd, "state.json", state)
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)
                if row["state"] == "writing":
                    descriptor = _open_regular_at(
                        archives, filename, expected=row["identity"], writable=True
                    )
                    try:
                        existing = _read_fd(descriptor, len(body), allow_empty=True)
                        _require(body.startswith(existing),
                                 "ios_recovery_execution_materials")
                        if existing != body:
                            _write_bytes(descriptor, body)
                        current = _read_fd(descriptor, len(body))
                        _require(current == body
                                 and hashlib.sha256(current).hexdigest() == row["sha256"],
                                 "ios_recovery_execution_materials")
                    finally:
                        os.close(descriptor)
                    row["state"] = "ready"
                    _replace_at(recovery_fd, "state.json", state)
                _require(row["state"] == "ready", "ios_recovery_execution_materials")
                current = _regular_identity(
                    archives, filename, row["identity"], maximum=MAX_TRANSFER_BYTES
                )
                _require(current == body and hashlib.sha256(current).hexdigest() == row["sha256"],
                         "ios_recovery_execution_materials")
            os.fsync(archives)
        finally:
            os.close(archives)
        state["archives"] = {
            role: {key: state["archives"][role][key]
                   for key in ("sha256", "bytes", "identity")}
            for role in _ROLES
        }
        state["archiveState"] = "ready"
        _replace_at(recovery_fd, "state.json", state)

    def _prepare_recovery_root(self):
        operation_fd = self._context.directory
        operation_root = self._operations._operation_root(self._context.operation_id)
        expected = self._expected_archives()
        if _RECOVERY_NAME not in os.listdir(operation_fd):
            os.mkdir(_RECOVERY_NAME, mode=0o700, dir_fd=operation_fd)
            os.fsync(operation_fd)
        recovery_fd = _open_child_directory(operation_fd, _RECOVERY_NAME)
        try:
            names = set(os.listdir(recovery_fd))
            _require(names <= {"intent.json", "state.json", _ARCHIVES_NAME, *_ATTEMPT_NAMES},
                     "ios_recovery_execution_journal")
            if _ARCHIVES_NAME not in names:
                _require(not names, "ios_recovery_execution_journal")
                os.mkdir(_ARCHIVES_NAME, mode=0o700, dir_fd=recovery_fd)
                os.fsync(recovery_fd)
                names.add(_ARCHIVES_NAME)
            archives = _open_child_directory(recovery_fd, _ARCHIVES_NAME)
            try:
                archives_identity = _identity_info(os.fstat(archives))
                if "intent.json" not in names:
                    _require(not os.listdir(archives) and "state.json" not in names
                             and not (set(_ATTEMPT_NAMES) & names),
                             "ios_recovery_execution_journal")
                    root_intent = _root_intent(
                        self._context, _identity_info(os.fstat(recovery_fd)),
                        archives_identity, expected,
                    )
                    _write_new_at(recovery_fd, "intent.json", root_intent)
                    names.add("intent.json")
                else:
                    root_intent = _read_json_at(recovery_fd, "intent.json", _MAX_RECORD_BYTES)
                    _require(root_intent["archives"] == expected
                             and _same_identity(os.fstat(archives),
                                                root_intent["archivesDirectoryIdentity"],
                                                directory=True),
                             "ios_recovery_execution_configuration")
                if "state.json" not in names:
                    _require(not os.listdir(archives) and not (set(_ATTEMPT_NAMES) & names),
                             "ios_recovery_execution_journal")
                    state = _root_state(
                        self._context, self._initial_archive_rows(expected), root_intent,
                        _identity_info(os.stat(
                            "intent.json", dir_fd=recovery_fd, follow_symlinks=False
                        )),
                    )
                    _write_new_at(recovery_fd, "state.json", state)
                else:
                    state = _read_json_at(recovery_fd, "state.json", _MAX_RECORD_BYTES)
            finally:
                os.close(archives)
            if state["archiveState"] == "preparing":
                self._resume_archives(recovery_fd, root_intent, state)
            else:
                _require(state["archiveState"] == "ready",
                         "ios_recovery_execution_materials")
        finally:
            os.close(recovery_fd)
        rows = _validate_recovery_journal(
            operation_fd, self._context.intent,
            self._operations._records(self._context.operation_id, operation_fd)[1],
            self._context.native,
            operation_root,
        )
        recovery_fd = _open_child_directory(operation_fd, _RECOVERY_NAME)
        self._recovery_fd = recovery_fd
        root_intent = _read_json_at(recovery_fd, "intent.json", _MAX_RECORD_BYTES)
        _require(root_intent["archives"] == expected,
                 "ios_recovery_execution_configuration")
        return rows

    def _read_attempt(self, name):
        fd = _open_child_directory(self._recovery_fd, name)
        try:
            return (
                _read_json_at(fd, "intent.json", _MAX_RECORD_BYTES),
                _read_json_at(fd, "state.json", _MAX_RECORD_BYTES),
            )
        finally:
            os.close(fd)

    def _retire_attempt_materials(self, name):
        attempt_fd = _open_child_directory(self._recovery_fd, name)
        try:
            intent = _read_json_at(attempt_fd, "intent.json", _MAX_RECORD_BYTES)
            if "state.json" not in os.listdir(attempt_fd):
                state = self._initial_attempt_state(
                    intent,
                    _identity_info(os.stat(
                        "intent.json", dir_fd=attempt_fd, follow_symlinks=False
                    )),
                )
                _write_new_at(attempt_fd, "state.json", state)
            else:
                state = _read_json_at(attempt_fd, "state.json", _MAX_RECORD_BYTES)
            if state["materialState"] == "retired":
                return
            _require(state["materialState"] in {"preparing", "ready", "retiring"},
                     "ios_recovery_execution_materials")
            if state["materialState"] != "retiring":
                state["materialState"] = "retiring"
                _replace_at(attempt_fd, "state.json", state)
            for role in _ROLES:
                row = state["roles"][role]
                if row["state"] == "retired":
                    _require(role not in os.listdir(attempt_fd),
                             "ios_recovery_execution_materials")
                    continue
                if row["state"] == "pending":
                    _require(role not in os.listdir(attempt_fd),
                             "ios_recovery_execution_materials")
                    state["roles"][role] = {
                        "state": "retired", "directoryIdentity": None,
                        "appDirectoryIdentity": None,
                        "appDigest": intent["roles"][role]["appDigest"],
                    }
                    _replace_at(attempt_fd, "state.json", state)
                    continue
                if row["state"] == "creating":
                    if role not in os.listdir(attempt_fd):
                        state["roles"][role] = {
                            "state": "retired", "directoryIdentity": None,
                            "appDirectoryIdentity": None,
                            "appDigest": intent["roles"][role]["appDigest"],
                        }
                        _replace_at(attempt_fd, "state.json", state)
                        continue
                    role_fd = _open_child_directory(attempt_fd, role)
                    try:
                        _require(not os.listdir(role_fd),
                                 "ios_recovery_execution_materials")
                        directory_identity = _identity_info(os.fstat(role_fd))
                    finally:
                        os.close(role_fd)
                    state["roles"][role] = {
                        "state": "retiring", "directoryIdentity": directory_identity,
                        "appDirectoryIdentity": None,
                        "appDigest": intent["roles"][role]["appDigest"],
                    }
                    _replace_at(attempt_fd, "state.json", state)
                    row = state["roles"][role]
                if row["state"] == "preparing":
                    role_fd = _open_child_directory(
                        attempt_fd, role, expected=row["directoryIdentity"]
                    )
                    try:
                        _require(not os.listdir(role_fd),
                                 "ios_recovery_execution_materials")
                    finally:
                        os.close(role_fd)
                    state["roles"][role] = {
                        "state": "retiring",
                        "directoryIdentity": row["directoryIdentity"],
                        "appDirectoryIdentity": None,
                        "appDigest": intent["roles"][role]["appDigest"],
                    }
                    _replace_at(attempt_fd, "state.json", state)
                    row = state["roles"][role]
                if row["state"] == "app-creating":
                    role_fd = _open_child_directory(
                        attempt_fd, role, expected=row["directoryIdentity"]
                    )
                    app_identity = None
                    app_fd = None
                    try:
                        children = set(os.listdir(role_fd))
                        _require(children <= {"App.app"},
                                 "ios_recovery_execution_materials")
                        if "App.app" in children:
                            app_fd = _open_recorded_app(role_fd)
                            _require(not os.listdir(app_fd),
                                     "ios_recovery_execution_materials")
                            app_identity = _identity_info(os.fstat(app_fd))
                    finally:
                        if app_fd is not None:
                            os.close(app_fd)
                        os.close(role_fd)
                    state["roles"][role] = {
                        "state": "retiring",
                        "directoryIdentity": row["directoryIdentity"],
                        "appDirectoryIdentity": app_identity,
                        "appDigest": intent["roles"][role]["appDigest"],
                    }
                    _replace_at(attempt_fd, "state.json", state)
                    row = state["roles"][role]
                if row["state"] == "extracting":
                    state["roles"][role] = dict(row, state="retiring")
                    _replace_at(attempt_fd, "state.json", state)
                    row = state["roles"][role]
                if row["state"] == "ready":
                    role_fd = _open_child_directory(
                        attempt_fd, role, expected=row["directoryIdentity"]
                    )
                    try:
                        _require(set(os.listdir(role_fd)) == {"App.app"},
                                 "ios_recovery_execution_materials")
                        app = parse_ios_artifact(
                            self._operations._operation_root(self._context.operation_id)
                            / _RECOVERY_NAME / name / role / "App.app"
                        )
                        _require(app.app_digest == intent["roles"][role]["appDigest"]
                                 == row["appDigest"],
                                 "ios_recovery_execution_materials")
                    finally:
                        os.close(role_fd)
                    row["state"] = "retiring"
                    _replace_at(attempt_fd, "state.json", state)
                row = state["roles"][role]
                _require(row["state"] == "retiring",
                         "ios_recovery_execution_materials")
                if role in os.listdir(attempt_fd):
                    role_fd = _open_child_directory(
                        attempt_fd, role, expected=row["directoryIdentity"]
                    )
                    try:
                        children = set(os.listdir(role_fd))
                        _require(children <= {"App.app"}
                                 and ("App.app" not in children
                                      or row["appDirectoryIdentity"] is not None),
                                 "ios_recovery_execution_materials")
                        if "App.app" in children:
                            current = os.stat(
                                "App.app", dir_fd=role_fd, follow_symlinks=False
                            )
                            _require(_same_identity(
                                current, row["appDirectoryIdentity"], directory=True
                            ), "ios_recovery_execution_materials")
                            app_fd = _open_recorded_app(
                                role_fd, row["appDirectoryIdentity"]
                            )
                            try:
                                root_intent = _read_json_at(
                                    self._recovery_fd, "intent.json", _MAX_RECORD_BYTES
                                )
                                root_state = _read_json_at(
                                    self._recovery_fd, "state.json", _MAX_RECORD_BYTES
                                )
                                expected_tree = _expected_app_tree(
                                    self._recovery_fd, root_intent, root_state, role,
                                    self._operations._operation_root(self._context.operation_id),
                                )
                                _validate_app_subset(
                                    app_fd, expected_tree, _ExecutionBounds(self)
                                )
                                _validate_app_subset(
                                    app_fd, expected_tree, _ExecutionBounds(self), remove=True
                                )
                                _require(not os.listdir(app_fd),
                                         "ios_recovery_execution_materials")
                            finally:
                                os.close(app_fd)
                            current = os.stat(
                                "App.app", dir_fd=role_fd, follow_symlinks=False
                            )
                            _require(_same_identity(
                                current, row["appDirectoryIdentity"], directory=True
                            ), "ios_recovery_execution_materials")
                            os.rmdir("App.app", dir_fd=role_fd)
                            os.fsync(role_fd)
                        _require(not os.listdir(role_fd),
                                 "ios_recovery_execution_materials")
                    finally:
                        os.close(role_fd)
                    current = os.stat(role, dir_fd=attempt_fd, follow_symlinks=False)
                    _require(_same_identity(current, row["directoryIdentity"], directory=True),
                             "ios_recovery_execution_materials")
                    os.rmdir(role, dir_fd=attempt_fd)
                    os.fsync(attempt_fd)
                row["state"] = "retired"
                _replace_at(attempt_fd, "state.json", state)
            state["materialState"] = "retired"
            state["materialDisposalDigest"] = contracts.digest({
                "schemaVersion": 1,
                "kind": "ios-native-recovery-material-disposal-v1",
                "operationId": self._context.operation_id,
                "nativeBindingDigest": self._context.binding_digest,
                "attempt": intent["attempt"],
                "roles": state["roles"],
            })
            _replace_at(attempt_fd, "state.json", state)
            if name == self._attempt_name:
                self._attempt_state = state
        finally:
            os.close(attempt_fd)

    def _attempt_roles(self):
        return {
            role: {
                "bundleId": self._operations._roles[role],
                "archiveSha256": self._context.intent["roles"][role]["sha256"],
                "appDigest": self._context.native["preparedApps"][role],
            }
            for role in _ROLES
        }

    def _initial_attempt_state(self, intent, intent_identity):
        return {
            "schemaVersion": 1,
            "kind": "ios-native-recovery-attempt-state-v1",
            "attempt": intent["attempt"],
            "operationId": self._context.operation_id,
            "nativeBindingDigest": self._context.binding_digest,
            "materialState": "preparing",
            "roles": {role: {"state": "pending"} for role in _ROLES},
            "slots": {slot: None for slot in _SLOTS},
            "outcome": "pending",
            "observationDigest": None,
            "materialDisposalDigest": None,
            "intentDigest": contracts.digest(intent),
            "intentIdentity": intent_identity,
        }

    def _new_attempt_intent(self, number, attempt_fd):
        parent = self._context._parent_grant
        _require(parent.project_id == self._config.registration.project["id"],
                 "ios_recovery_execution_grant")
        session_id = "iosrec_" + contracts.digest({
            "context": self._context.context_digest, "attempt": number,
            "grant": parent.grant_fingerprint,
        })[:32]
        return {
            "schemaVersion": 1,
            "kind": "ios-native-recovery-attempt-v1",
            "attempt": number,
            "operationId": self._context.operation_id,
            "requestDigest": self._context.request_digest,
            "contextDigest": self._context.context_digest,
            "configurationDigest": self._context.configuration_digest,
            "scopeDigest": self._context.scope_digest,
            "nativeBindingDigest": self._context.binding_digest,
            "recoveryDirectoryIdentity": _identity_info(os.fstat(self._recovery_fd)),
            "attemptDirectoryIdentity": _identity_info(os.fstat(attempt_fd)),
            "priorOwnershipGeneration": self._context.prior_generation,
            "hostIncarnation": self._context._device._authority.host_incarnation,
            "helperIncarnation": self._helper_incarnation,
            "projectId": parent.project_id,
            "sessionId": session_id,
            "controllerId": parent.controller_id,
            "grantId": parent.grant_id,
            "grantFingerprint": parent.grant_fingerprint,
            "grantDeadlineNs": self._context.grant_deadline_ns,
            "roles": self._attempt_roles(),
        }

    def _prepare_attempt(self, prior):
        reusable_empty = None
        for number, (intent, _state) in enumerate(prior, 1):
            name = f"attempt-{number:03d}"
            if intent is None:
                _require(number == len(prior), "ios_recovery_execution_journal")
                reusable_empty = number
                continue
            self._retire_attempt_materials(name)
        number = reusable_empty if reusable_empty is not None else len(prior) + 1
        _require(1 <= number <= 3, "ios_recovery_execution_attempt_limit")
        name = f"attempt-{number:03d}"
        if name not in os.listdir(self._recovery_fd):
            os.mkdir(name, mode=0o700, dir_fd=self._recovery_fd)
            os.fsync(self._recovery_fd)
        attempt_fd = _open_child_directory(self._recovery_fd, name)
        root = self._operations._operation_root(self._context.operation_id) / _RECOVERY_NAME / name
        _require(not os.listdir(attempt_fd), "ios_recovery_execution_journal")
        intent = self._new_attempt_intent(number, attempt_fd)
        _write_new_at(attempt_fd, "intent.json", intent)
        state = self._initial_attempt_state(
            intent,
            _identity_info(os.stat(
                "intent.json", dir_fd=attempt_fd, follow_symlinks=False
            )),
        )
        _write_new_at(attempt_fd, "state.json", state)
        root_state = _read_json_at(self._recovery_fd, "state.json", _MAX_RECORD_BYTES)
        for role in _ROLES:
            self._context.require()
            row = state["roles"][role]
            if row["state"] == "pending":
                row = {"state": "creating", "directoryIdentity": None}
                state["roles"][role] = row
                _replace_at(attempt_fd, "state.json", state)
            if row["state"] == "creating":
                if role not in os.listdir(attempt_fd):
                    os.mkdir(role, mode=0o700, dir_fd=attempt_fd)
                    os.fsync(attempt_fd)
                role_fd = _open_child_directory(attempt_fd, role)
                try:
                    _require(not os.listdir(role_fd),
                             "ios_recovery_execution_materials")
                    row = {
                        "state": "preparing",
                        "directoryIdentity": _identity_info(os.fstat(role_fd)),
                    }
                finally:
                    os.close(role_fd)
                state["roles"][role] = row
                _replace_at(attempt_fd, "state.json", state)
            if row["state"] == "preparing":
                role_fd = _open_child_directory(
                    attempt_fd, role, expected=row["directoryIdentity"]
                )
                try:
                    _require(not os.listdir(role_fd),
                             "ios_recovery_execution_materials")
                finally:
                    os.close(role_fd)
                row = {
                    "state": "app-creating",
                    "directoryIdentity": row["directoryIdentity"],
                    "appDirectoryIdentity": None,
                }
                state["roles"][role] = row
                _replace_at(attempt_fd, "state.json", state)
            if row["state"] == "app-creating":
                role_fd = _open_child_directory(
                    attempt_fd, role, expected=row["directoryIdentity"]
                )
                app_fd = None
                try:
                    if "App.app" not in os.listdir(role_fd):
                        os.mkdir("App.app", mode=0o700, dir_fd=role_fd)
                        os.fsync(role_fd)
                    _require(set(os.listdir(role_fd)) == {"App.app"},
                             "ios_recovery_execution_materials")
                    app_fd = _open_recorded_app(role_fd)
                    _require(not os.listdir(app_fd),
                             "ios_recovery_execution_materials")
                    app_identity = _identity_info(os.fstat(app_fd))
                finally:
                    if app_fd is not None:
                        os.close(app_fd)
                    os.close(role_fd)
                row = {
                    "state": "extracting",
                    "directoryIdentity": row["directoryIdentity"],
                    "appDirectoryIdentity": app_identity,
                    "appDigest": intent["roles"][role]["appDigest"],
                }
                state["roles"][role] = row
                _replace_at(attempt_fd, "state.json", state)
            _require(row["state"] == "extracting",
                     "ios_recovery_execution_materials")
            role_fd = _open_child_directory(
                attempt_fd, role, expected=row["directoryIdentity"]
            )
            app_fd = None
            try:
                _require(set(os.listdir(role_fd)) == {"App.app"},
                         "ios_recovery_execution_materials")
                app_fd = _open_recorded_app(role_fd, row["appDirectoryIdentity"])
                expected_tree = _expected_app_tree(
                    self._recovery_fd,
                    _read_json_at(self._recovery_fd, "intent.json", _MAX_RECORD_BYTES),
                    root_state, role,
                    self._operations._operation_root(self._context.operation_id),
                )
                _validate_app_subset(app_fd, expected_tree, _ExecutionBounds(self))
                if os.listdir(app_fd):
                    _validate_app_subset(
                        app_fd, expected_tree, _ExecutionBounds(self), remove=True
                    )
                    _require(not os.listdir(app_fd),
                             "ios_recovery_execution_materials")
                _extract_registered_app(self._baseline_bodies[role], app_fd)
                prepared = parse_ios_artifact(root / role / "App.app")
                _require(prepared.app_digest == row["appDigest"],
                         "ios_recovery_execution_materials")
                state["roles"][role] = {
                    "state": "ready",
                    "directoryIdentity": row["directoryIdentity"],
                    "appDirectoryIdentity": row["appDirectoryIdentity"],
                    "appDigest": prepared.app_digest,
                }
                _replace_at(attempt_fd, "state.json", state)
            finally:
                if app_fd is not None:
                    os.close(app_fd)
                os.close(role_fd)
        state["materialState"] = "ready"
        _replace_at(attempt_fd, "state.json", state)
        self._attempt_fd = attempt_fd
        self._attempt = number
        self._attempt_name = name
        self._attempt_root = root
        self._attempt_intent = intent
        self._attempt_state = state

    def __enter__(self):
        _require(not self._entered and not self._active and self._same_thread(),
                 "ios_recovery_execution_inactive")
        self._entered = True
        try:
            self._baseline_bodies = self._baseline_snapshot()
            # The exact recovery lease, operation definition, and every external
            # baseline byte were checked before these tool verifications.
            self._config.validate()
            _require(
                self._config.xctest is not None and self._config.sanitation is not None,
                "ios_recovery_execution_configuration",
            )
            prior = self._prepare_recovery_root()
            self._prepare_attempt(prior)
            operation = _RecoveredOperation(self)
            lease = self._context.device_lease
            owner = IOSMobileNativeOwner(
                self._operations,
                operation,
                self._context._device,
                self._context.directory,
                self._context.producer,
                (lease.descriptor, lease.directory_descriptor, lease.lock_name),
                self._context.native,
            )
            self._owner = owner
            owner._recovery_context = self
            self._active = True
            with self._operations._changed:
                require_native_recovery(self._context)
                _require(not self._operations._native_owners,
                         "ios_recovery_execution_busy")
                self._operations._native_owners[id(owner)] = owner
            self.require_owner(owner)
            return owner
        except BaseException:
            self._active = False
            if self._attempt_fd is not None:
                os.close(self._attempt_fd)
                self._attempt_fd = None
            if self._recovery_fd is not None:
                os.close(self._recovery_fd)
                self._recovery_fd = None
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        cleanup_error = None
        if self._owner is not None and self._active:
            deadline = min(self._context.deadline_monotonic, time.monotonic() + 5)
            with self._operations._changed:
                clients = tuple(
                    client for client in self._operations._native_clients
                    if getattr(client, "native_owner", None) is self._owner
                )
            for client in clients:
                try:
                    stopped = client.close(deadline_monotonic=deadline)
                    if not stopped:
                        cleanup_error = IOSNativeRecoveryError(
                            "ios_recovery_execution_process_unknown"
                        )
                except Exception:
                    cleanup_error = IOSNativeRecoveryError(
                        "ios_recovery_execution_process_unknown"
                    )
            with self._operations._changed:
                retained = tuple(
                    client for client in self._operations._native_clients
                    if getattr(client, "native_owner", None) is self._owner
                )
                exports = tuple(
                    item for item in self._operations._native_exports.values()
                    if getattr(item, "_owner", None) is self._owner
                )
            if retained or exports or any(
                getattr(client, "active_processes", 1) != 0 for client in clients
            ):
                cleanup_error = IOSNativeRecoveryError(
                    "ios_recovery_execution_process_unknown"
                )
            if cleanup_error is None:
                try:
                    self._retire_attempt_materials(self._attempt_name)
                except Exception:
                    cleanup_error = IOSNativeRecoveryError(
                        "ios_recovery_execution_materials_unknown"
                    )
            if cleanup_error is None:
                with self._operations._changed:
                    self._owner._active = False
                    self._operations._native_owners.pop(id(self._owner), None)
                    self._operations._changed.notify_all()
                self._owner._recovery_context = None
                self._active = False
            else:
                # Preserve every unknown client/export and its exact owner in
                # the shared registry.  A later context cannot treat it as gone.
                self._cleanup_unknown = True
        if not self._cleanup_unknown:
            if self._attempt_fd is not None:
                os.close(self._attempt_fd)
                self._attempt_fd = None
            if self._recovery_fd is not None:
                os.close(self._recovery_fd)
                self._recovery_fd = None
        if cleanup_error is not None:
            raise cleanup_error
        return False

    def require_owner(self, owner):
        self._require_active()
        with self._operations._changed:
            _require(
                type(owner) is IOSMobileNativeOwner
                and owner is self._owner
                and owner._active
                and owner._recovery_context is self
                and owner.operations is self._operations
                and owner.device is self._context._device
                and owner._directory == self._context.directory
                and owner._producer == self._context.producer
                and self._operations._native_owners.get(id(owner)) is owner
                and json.loads(owner._record) == self._context.native,
                "ios_recovery_execution_owner",
            )
        return owner

    def prepared_app(self, owner, role):
        self.require_owner(owner)
        _require(type(role) is str and role in _ROLES,
                 "ios_recovery_execution_role")
        row = self._attempt_state["roles"][role]
        expected = self._attempt_intent["roles"][role]
        _require(self._attempt_state["materialState"] == "ready" and row["state"] == "ready",
                 "ios_recovery_execution_materials")
        role_fd = _open_child_directory(
            self._attempt_fd, role, expected=row["directoryIdentity"]
        )
        try:
            _require(set(os.listdir(role_fd)) == {"App.app"},
                     "ios_recovery_execution_materials")
            info = os.stat("App.app", dir_fd=role_fd, follow_symlinks=False)
            _require(_same_identity(info, row["appDirectoryIdentity"], directory=True),
                     "ios_recovery_execution_materials")
            app = parse_ios_artifact(self._attempt_root / role / "App.app")
            _require(
                app.app_digest == row["appDigest"] == expected["appDigest"]
                and app.manifest["applicationId"] == expected["bundleId"],
                "ios_recovery_execution_materials",
            )
            return app
        finally:
            os.close(role_fd)

    def _now(self):
        self._require_active()
        try:
            return self._context._device._authority._require_parent_grant(
                self._context._parent_grant
            )
        except Exception:
            _fail("ios_recovery_execution_grant")

    def _token_row(self, token, state="issued", result_digest=None):
        return {
            "protocolVersion": token.protocol_version,
            "operationId": token.operation_id,
            "operationFingerprint": token.operation_fingerprint,
            "payloadDigest": token.payload_digest,
            "projectId": token.project_id,
            "sessionId": token.session_id,
            "controllerId": token.controller_id,
            "sequence": token.sequence,
            "ownershipGeneration": token.ownership_generation,
            "hostIncarnation": token.host_incarnation,
            "helperIncarnation": token.helper_incarnation,
            "providerIncarnation": token.provider_incarnation,
            "deadlineNs": token.deadline_ns,
            "deviceFingerprint": token._device_fingerprint,
            "grantFingerprint": self._attempt_intent["grantFingerprint"],
            "state": state,
            "resultDigest": result_digest,
        }

    def _issue(self, slot, payload, provider_incarnation, cancellation, deadline_monotonic):
        with self._lock:
            self._bounds(cancellation, deadline_monotonic)
            _require(slot in _SLOTS and self._attempt_state["slots"][slot] is None,
                     "ios_recovery_execution_dispatch")
            contracts.validate_id(provider_incarnation)
            payload = _canonical_result(payload)
            if slot == "restore-original":
                _require(payload.get("command") == "restore-original"
                         and payload.get("role") == "original",
                         "ios_recovery_execution_dispatch")
            elif slot == "start-original":
                _require(payload.get("kind") == "ios-fixed-xctest-launch-v1"
                         and payload.get("role") == "original"
                         and payload.get("iteration") == 1,
                         "ios_recovery_execution_dispatch")
            else:
                _require(payload == command_payload("cleanup", {}),
                         "ios_recovery_execution_dispatch")
            now = self._now()
            deadline_ns = min(
                self.native_deadline_ns,
                now + int(max(0, deadline_monotonic - time.monotonic()) * 1_000_000_000),
            )
            _require(now < deadline_ns, "ios_recovery_execution_dispatch")
            operation_id = "iosrec_" + contracts.digest({
                "context": self._context.context_digest,
                "attempt": self._attempt,
                "slot": slot,
            })[:32]
            fields = {
                "protocolVersion": PROTOCOL_VERSION,
                "deviceFingerprint": self._context.scope_digest,
                "operationId": operation_id,
                "payloadDigest": contracts.digest(payload),
                "projectId": self._attempt_intent["projectId"],
                "sessionId": self._attempt_intent["sessionId"],
                "controllerId": self._attempt_intent["controllerId"],
                "ownershipGeneration": self._context.prior_generation,
                "hostIncarnation": self._attempt_intent["hostIncarnation"],
                "helperIncarnation": self._helper_incarnation,
                "deadlineNs": deadline_ns,
                "sequence": _SEQUENCES[slot],
                "providerIncarnation": provider_incarnation,
                "grantFingerprint": self._attempt_intent["grantFingerprint"],
                "nativeBindingDigest": self._context.binding_digest,
                "contextDigest": self._context.context_digest,
                "slot": slot,
            }
            token = IOSRecoveryDispatch(
                PROTOCOL_VERSION,
                operation_id,
                contracts.digest(fields),
                fields["payloadDigest"],
                fields["projectId"],
                fields["sessionId"],
                fields["controllerId"],
                fields["sequence"],
                fields["ownershipGeneration"],
                fields["hostIncarnation"],
                fields["helperIncarnation"],
                provider_incarnation,
                deadline_ns,
                self._context.scope_digest,
                slot,
                self._issuer,
            )
            self._attempt_state["slots"][slot] = self._token_row(token)
            # Publish before the exact process-local object becomes dispatchable.
            _replace_at(self._attempt_fd, "state.json", self._attempt_state)
            self._issued[id(token)] = token
            self._issued_by_slot[slot] = token
            return token

    def require_dispatch(self, owner, permit, payload_digest):
        with self._lock:
            self.require_owner(owner)
            _require(
                type(permit) is IOSRecoveryDispatch
                and permit._issuer is self._issuer
                and self._issued.get(id(permit)) is permit
                and self._issued_by_slot.get(permit._slot) is permit
                and type(payload_digest) is str
                and payload_digest == permit.payload_digest
                and permit.payload_digest == self._attempt_state["slots"][permit._slot]["payloadDigest"]
                and self._attempt_state["slots"][permit._slot]["state"] == "issued"
                and permit.ownership_generation == self._context.prior_generation
                and permit.host_incarnation == self._context._device._authority.host_incarnation
                and permit.helper_incarnation == self._helper_incarnation
                and permit.project_id == self._context._parent_grant.project_id
                and permit.controller_id == self._context._parent_grant.controller_id
                and permit.deadline_ns <= self._context.grant_deadline_ns,
                "ios_recovery_execution_dispatch",
            )
            expected_row = self._token_row(
                permit,
                state=self._attempt_state["slots"][permit._slot]["state"],
                result_digest=self._attempt_state["slots"][permit._slot]["resultDigest"],
            )
            _require(
                expected_row == self._attempt_state["slots"][permit._slot],
                "ios_recovery_execution_dispatch",
            )
            now = self._now()
            _require(now < permit.deadline_ns, "ios_recovery_execution_dispatch")
            return now

    def record_result(self, token, result):
        with self._lock:
            self.require_dispatch(self._owner, token, token.payload_digest)
            value = _canonical_result(result)
            digest = contracts.digest(value)
            self._attempt_state["slots"][token._slot] = self._token_row(
                token, state="settled", result_digest=digest
            )
            _replace_at(self._attempt_fd, "state.json", self._attempt_state)
            return digest

    def _record_failure(self):
        digest = contracts.digest({
            "kind": "ios-native-recovery-attempt-failure",
            "attempt": self._attempt,
            "deviceCleanupConfirmed": False,
        })
        with self._lock:
            for slot, token in self._issued_by_slot.items():
                if self._attempt_state["slots"][slot]["state"] == "issued":
                    self._attempt_state["slots"][slot] = self._token_row(
                        token, state="failed", result_digest=digest
                    )
            self._attempt_state["outcome"] = "failed"
            _replace_at(self._attempt_fd, "state.json", self._attempt_state)

    def bind_native_handshake(self, owner, permit, **fields):
        with self._lock:
            host_received = self.require_dispatch(owner, permit, permit.payload_digest)
            expected = {
                "protocol_version", "helper_version", "helper_incarnation",
                "provider_incarnation", "native_incarnation", "native_clock_id",
                "native_time_ms",
            }
            _require(set(fields) == expected, "ios_recovery_execution_handshake")
            _require(
                type(fields["protocol_version"]) is int
                and type(fields["helper_version"]) is int
                and fields["protocol_version"] == NATIVE_PROTOCOL_VERSION
                and fields["helper_version"] == HELPER_VERSION
                and fields["helper_incarnation"] == permit.helper_incarnation
                and fields["provider_incarnation"] == permit.provider_incarnation
                and type(fields["native_time_ms"]) is int
                and 0 <= fields["native_time_ms"] <= MAX_NS // 1_000_000,
                "ios_recovery_execution_handshake",
            )
            for key in ("native_incarnation", "native_clock_id"):
                contracts.validate_id(fields[key])
            qualification = QUALIFIED_NATIVE_CLOCKS.get(
                (self._context._device.device_kind, fields["native_clock_id"])
            )
            _require(qualification is not None, "ios_recovery_execution_handshake")
            self.require_dispatch(owner, permit, permit.payload_digest)
            handshake = NativeHandshake(
                fields["protocol_version"], fields["helper_version"],
                fields["helper_incarnation"], fields["provider_incarnation"],
                fields["native_incarnation"], fields["native_clock_id"],
                fields["native_time_ms"], host_received,
                qualification["maxRateErrorPpm"], qualification["mappingUncertaintyMs"],
                self._context.scope_digest, self._issuer,
            )
            self._handshakes[id(handshake)] = handshake
            return handshake

    def native_grant(self, owner, permit, handshake):
        with self._lock:
            self.require_dispatch(owner, permit, permit.payload_digest)
            _require(
                type(handshake) is NativeHandshake
                and handshake._issuer is self._issuer
                and self._handshakes.get(id(handshake)) is handshake
                and handshake._device_fingerprint == self._context.scope_digest
                and handshake.protocol_version == NATIVE_PROTOCOL_VERSION
                and handshake.helper_version == HELPER_VERSION
                and handshake.helper_incarnation == permit.helper_incarnation
                and handshake.provider_incarnation == permit.provider_incarnation,
                "ios_recovery_execution_handshake",
            )
            remaining_ms = max(0, permit.deadline_ns - handshake.host_received_ns) // 1_000_000
            native_duration = (
                remaining_ms * (1_000_000 - handshake.max_rate_error_ppm)
            ) // 1_000_000
            _require(native_duration > handshake.mapping_uncertainty_ms,
                     "ios_recovery_execution_handshake")
            native_deadline = (
                handshake.native_time_ms + native_duration
                - handshake.mapping_uncertainty_ms
            )
            _require(type(native_deadline) is int and 0 <= native_deadline <= MAX_NS // 1_000_000,
                     "ios_recovery_execution_handshake")
            return NativeGrant(
                NATIVE_PROTOCOL_VERSION, permit.operation_id,
                permit.operation_fingerprint, permit.payload_digest,
                permit.project_id, permit.session_id, permit.controller_id,
                permit.sequence, permit.ownership_generation, permit.host_incarnation,
                permit.helper_incarnation, permit.provider_incarnation,
                handshake.native_incarnation, handshake.native_clock_id,
                native_deadline, self._issuer,
            )

    def _bounds(self, cancellation, deadline_monotonic):
        self._require_active()
        _require(
            callable(getattr(cancellation, "is_set", None))
            and type(deadline_monotonic) in (int, float)
            and not isinstance(deadline_monotonic, bool)
            and math.isfinite(deadline_monotonic)
            and time.monotonic() < deadline_monotonic
            and not cancellation.is_set(),
            "ios_recovery_execution_bounds",
        )
        self._now()

    def run_original_sanitation(self, *, cancellation, deadline_monotonic):
        """Restore, launch, sanitize, and collect the fixed original app."""
        self._bounds(cancellation, deadline_monotonic)
        installer = runner = reader = None
        try:
            installer = self._config.query.open_installer(native_owner=self._owner)
            restore_payload = installer.payload("restore-original")
            restore = self._issue(
                "restore-original", restore_payload, "ios_recovery_installer",
                cancellation, deadline_monotonic,
            )
            install_observation = installer.run(
                "restore-original", permit=restore, cancellation=cancellation,
                deadline_monotonic=deadline_monotonic,
            )
            installed_identity = installer.observe_installed(
                install_observation, cancellation=cancellation,
                deadline_monotonic=deadline_monotonic,
            )
            self.record_result(restore, {
                "install": install_observation.public(),
                "identity": installed_identity.public(),
            })
            _require(installer.close(deadline_monotonic=deadline_monotonic),
                     "ios_recovery_execution_process_unknown")
            installer = None

            runner = IOSXCTestRunner(self._config.xctest, self._config.query, self._owner)
            profile = self._config.original_profile
            launch = runner.prepare(
                role="original", iteration=1,
                application_id=self._config.application_id,
                profile_digest=profile.digest,
                actions=tuple(profile.data["capabilities"]["actions"]),
                cancellation=cancellation, deadline_monotonic=deadline_monotonic,
            )
            startup = self._issue(
                "start-original", launch.payload,
                launch.payload["providerIncarnation"],
                cancellation, deadline_monotonic,
            )
            session = runner.start(
                launch, permit=startup, cancellation=cancellation,
                deadline_monotonic=deadline_monotonic,
            )
            helper = IOSHelperChannel(session)
            handshake = helper.handshake(
                startup, cancellation=cancellation,
                deadline_monotonic=deadline_monotonic,
            )
            activation = helper.activate(
                startup, installed_identity, cancellation=cancellation,
                deadline_monotonic=deadline_monotonic,
            )
            reader = self._config.query.open_runtime_reader(native_owner=self._owner)
            launch_observation = reader.read(
                launch, cancellation=cancellation,
                deadline_monotonic=deadline_monotonic,
            )
            self.record_result(startup, {
                "activation": activation,
                "runtime": launch_observation.public(),
                "handshakeDigest": contracts.digest({
                    "protocolVersion": handshake.protocol_version,
                    "helperIncarnation": handshake.helper_incarnation,
                    "providerIncarnation": handshake.provider_incarnation,
                    "nativeIncarnation": handshake.native_incarnation,
                    "nativeClockId": handshake.native_clock_id,
                    "nativeTimeMs": handshake.native_time_ms,
                }),
            })

            cleanup_payload = command_payload("cleanup", {})
            cleanup = self._issue(
                "cleanup-original", cleanup_payload,
                launch.payload["providerIncarnation"],
                cancellation, deadline_monotonic,
            )
            cleanup_command = helper.command(
                "cleanup", {}, cleanup, cancellation=cancellation,
                deadline_monotonic=deadline_monotonic,
            )
            cleanup_observation = reader.read(
                launch, stage="cleanup", cancellation=cancellation,
                deadline_monotonic=deadline_monotonic,
            )
            _require(reader.close(deadline_monotonic=deadline_monotonic),
                     "ios_recovery_execution_process_unknown")
            reader = None
            shutdown = helper.shutdown(
                cleanup, cancellation=cancellation,
                deadline_monotonic=deadline_monotonic,
            )
            _require(
                shutdown.get("ok") is True
                and shutdown["target"]["terminationConfirmed"]
                and shutdown["helper"]["terminationConfirmed"]
                and shutdown["host"]["terminated"],
                "ios_recovery_execution_process_unknown",
            )
            self.record_result(cleanup, {
                "command": cleanup_command,
                "sanitation": cleanup_observation.public(),
                "shutdown": shutdown,
            })
            _require(runner.close(deadline_monotonic=deadline_monotonic),
                     "ios_recovery_execution_process_unknown")
            runner = None
            evidence = {
                "schemaVersion": 1,
                "kind": "ios-native-recovery-execution-evidence-v1",
                "contextDigest": self._context.context_digest,
                "nativeBindingDigest": self._context.binding_digest,
                "attempt": self._attempt,
                "restore": install_observation.public(),
                "identity": installed_identity.public(),
                "launch": launch_observation.public(),
                "cleanup": cleanup_observation.public(),
                "shutdown": shutdown,
                "deviceCleanupConfirmed": False,
                "ownershipReleased": False,
                "deviceReconciled": False,
            }
            evidence_digest = contracts.digest(evidence)
            cleanup_receipt_digest = contracts.digest(
                cleanup_observation.sanitation.public()
            )
            observation = IOSRecoveryExecutionObservation(
                self._context.context_digest,
                self._context.binding_digest,
                self._attempt,
                install_observation,
                installed_identity,
                launch_observation,
                cleanup_observation,
                handshake,
                deepcopy(shutdown),
                cleanup_observation.evidence_digest,
                cleanup_receipt_digest,
                evidence_digest,
                self,
                self._issuer,
            )
            self._observations[id(observation)] = observation
            self._observation_evidence[id(observation)] = deepcopy(evidence)
            self._attempt_state["outcome"] = "succeeded"
            self._attempt_state["observationDigest"] = evidence_digest
            _replace_at(self._attempt_fd, "state.json", self._attempt_state)
            return observation
        except BaseException:
            try:
                self._record_failure()
            except Exception:
                pass
            raise
        finally:
            if reader is not None:
                _require(reader.close(deadline_monotonic=deadline_monotonic),
                         "ios_recovery_execution_process_unknown")
            if installer is not None:
                _require(installer.close(deadline_monotonic=deadline_monotonic),
                         "ios_recovery_execution_process_unknown")
            if runner is not None:
                _require(runner.close(deadline_monotonic=deadline_monotonic),
                         "ios_recovery_execution_process_unknown")

    def inspect_attempts(self):
        _require(self._entered and self._same_thread() and not self._cleanup_unknown,
                 "ios_recovery_execution_inactive")
        if self._active:
            self._require_active()
        else:
            require_native_recovery(self._context)
        rows = _validate_recovery_journal(
            self._context.directory,
            self._context.intent,
            self._operations._records(
                self._context.operation_id, self._context.directory
            )[1],
            self._context.native,
            self._operations._operation_root(self._context.operation_id),
        )
        return tuple({
            "attempt": intent["attempt"],
            "hostIncarnation": intent["hostIncarnation"],
            "helperIncarnation": intent["helperIncarnation"],
            "grantFingerprint": intent["grantFingerprint"],
            "materialState": state["materialState"],
            "outcome": state["outcome"],
            "observationDigest": state["observationDigest"],
            "materialDisposalDigest": state["materialDisposalDigest"],
            "slots": deepcopy(state["slots"]),
        } for intent, state in rows)

    def _observation_material_disposal(self, observation):
        self.require_observation(observation, require_materials_disposed=False)
        if self._active:
            state = self._attempt_state
        else:
            recovery_fd = attempt_fd = None
            try:
                recovery_fd = _open_child_directory(self._context.directory, _RECOVERY_NAME)
                attempt_fd = _open_child_directory(
                    recovery_fd, f"attempt-{observation.attempt:03d}"
                )
                state = _read_json_at(attempt_fd, "state.json", _MAX_RECORD_BYTES)
            finally:
                if attempt_fd is not None:
                    os.close(attempt_fd)
                if recovery_fd is not None:
                    os.close(recovery_fd)
        return state["materialDisposalDigest"]

    def require_observation(self, observation, *, require_materials_disposed=True):
        _require(type(require_materials_disposed) is bool,
                 "ios_recovery_execution_observation")
        _require(
            self._entered and self._same_thread() and not self._cleanup_unknown,
            "ios_recovery_execution_observation",
        )
        require_native_recovery(self._context)
        _require(
            type(observation) is IOSRecoveryExecutionObservation
            and observation._issuer is self._issuer
            and observation._execution is self
            and self._observations.get(id(observation)) is observation
            and observation.context_digest == self._context.context_digest
            and observation.native_binding_digest == self._context.binding_digest
            and observation.attempt == self._attempt
            and observation.cleanup_evidence_digest
            == observation.cleanup_observation.evidence_digest
            and observation.cleanup_receipt_digest
            == contracts.digest(observation.cleanup_observation.sanitation.public())
            and observation.evidence_digest
            == contracts.digest(self._observation_evidence[id(observation)]),
            "ios_recovery_execution_observation",
        )
        disposal = None
        if self._active:
            disposal = self._attempt_state["materialDisposalDigest"]
        else:
            recovery_fd = attempt_fd = None
            try:
                recovery_fd = _open_child_directory(self._context.directory, _RECOVERY_NAME)
                attempt_fd = _open_child_directory(
                    recovery_fd, f"attempt-{observation.attempt:03d}"
                )
                state = _read_json_at(attempt_fd, "state.json", _MAX_RECORD_BYTES)
                disposal = state["materialDisposalDigest"]
                _require(
                    state["outcome"] == "succeeded"
                    and state["observationDigest"] == observation.evidence_digest,
                    "ios_recovery_execution_observation",
                )
            finally:
                if attempt_fd is not None:
                    os.close(attempt_fd)
                if recovery_fd is not None:
                    os.close(recovery_fd)
        if require_materials_disposed:
            _require(disposal is not None, "ios_recovery_execution_materials")
        if disposal is not None:
            contracts.validate_digest(disposal)
        return observation


__all__ = [
    "IOSRecoveryDispatch",
    "IOSRecoveryExecution",
    "IOSRecoveryExecutionObservation",
]
