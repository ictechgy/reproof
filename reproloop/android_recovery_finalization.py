"""Reconcile measured Android restoration and finish cleanup under original locks."""
from contextlib import ExitStack
from dataclasses import dataclass, field
import fcntl
import os
import threading

from . import contracts
from .android_native_calls import validate_workspace
from .android_native_process import _bounds
from .android_recovery import validate_record
from .android_recovery_helper import recover_android_helper
from .execution.journal import RunDenied
from .live.authority import DeviceAuthority
from .repair_android_operation import (
    AndroidOperationError, _APK_NAMES, _file_digest, _identity_info, _open_child_directory,
    _open_regular_at, _read_json_at, _replace_at, _require, _same_identity, _write_new_at,
)
from .storage import MAX_APK


@dataclass(frozen=True, slots=True)
class AndroidFinalizationObservation:
    operation_id: str
    context_digest: str
    evidence_digest: str
    reservation_released: bool
    ownership_released: bool


@dataclass(slots=True)
class AndroidCleanupCapability:
    operation_id: str
    request_digest: str
    context_digest: str
    scope_digest: str
    evidence_digest: str
    _session: object = field(repr=False)
    _active: bool = field(default=True, repr=False)
    _consumed: bool = field(default=False, repr=False)


def validate_finalization(directory, intent, native):
    if "finalization.json" not in os.listdir(directory):
        return None
    value = _read_json_at(directory, "finalization.json", 16*1024)
    fields = {"schemaVersion", "operationId", "requestDigest", "contextDigest", "configurationDigest",
        "bindingDigest", "deviceFingerprint", "priorGeneration", "priorHostIncarnation",
        "priorHelperIncarnation", "generation", "hostIncarnation", "helperIncarnation",
        "reconciliationId", "reconciliationFingerprint", "priorHelperExitDigest", "pointerCleanupDigest",
        "freshHandshakeDigest", "observationDigest", "state", "historyDigest"}
    _require(type(value) is dict and set(value) == fields and native is not None
        and type(value["schemaVersion"]) is int and value["schemaVersion"] == 1
        and all(value[key] == intent[key] for key in
                ("operationId", "requestDigest", "contextDigest", "configurationDigest"))
        and value["bindingDigest"] == native["bindingDigest"]
        and value["deviceFingerprint"] == intent["scopeDigest"]
        and type(value["priorGeneration"]) is int
        and value["priorGeneration"] == native["ownershipGeneration"]
        and value["priorHostIncarnation"] == native["hostIncarnation"]
        and value["priorHelperIncarnation"] == native["helperIncarnation"]
        and type(value["generation"]) is int and value["generation"] == value["priorGeneration"]+1
        and value["helperIncarnation"] != value["priorHelperIncarnation"]
        and value["observationDigest"] == value["freshHandshakeDigest"]
        and value["state"] in {"prepared", "reconciled", "sanitized", "completed"},
        "android_recovery_finalization_record")
    try:
        for key in ("hostIncarnation", "helperIncarnation", "reconciliationId"):
            contracts.validate_id(value[key])
        for key in ("reconciliationFingerprint", "priorHelperExitDigest", "pointerCleanupDigest",
                    "freshHandshakeDigest", "observationDigest", "historyDigest"):
            contracts.validate_digest(value[key])
    except contracts.ContractError:
        raise AndroidOperationError("android_recovery_finalization_record") from None
    return value


def _reconciliation(authority, record):
    if record is None:
        return None
    row = authority.store.reconciliation(record["reconciliationId"])
    if row is None:
        _require(record["state"] == "prepared", "android_recovery_reconciliation_missing")
        return None
    bindings = {"reconciliation_id": "reconciliationId", "reconciliation_fingerprint": "reconciliationFingerprint",
        "device_fingerprint": "deviceFingerprint", "prior_generation": "priorGeneration",
        "prior_host_incarnation": "priorHostIncarnation", "prior_helper_incarnation": "priorHelperIncarnation",
        "fresh_helper_incarnation": "helperIncarnation", "prior_helper_exit_digest": "priorHelperExitDigest",
        "pointer_cleanup_digest": "pointerCleanupDigest", "fresh_handshake_digest": "freshHandshakeDigest"}
    _require(all(row[key] == record[value] for key, value in bindings.items()),
             "android_recovery_reconciliation_binding")
    return row


def _released(row, record):
    # Pending cleanup cannot admit work, be normally reconciled or be closed
    # as released. A later generation therefore proves this one was released.
    return row is not None and (row["generation"] > record["generation"] or (
        row["generation"] == record["generation"] and row["status"] == "released"
        and row["host_incarnation"] == record["hostIncarnation"]
        and row["helper_incarnation"] == record["helperIncarnation"]))


def _require_sanitized(operations, files):
    state = operations._state(files.inspection.operation_id, files.operation_fd)
    _require(operations._stage_status(files.operation_fd, files.intent, state, full=True) == "staged-discarded"
        and validate_workspace(files.operation_fd, files.intent) == "idle", "android_recovery_cleanup_unknown")


class _Session:
    def __init__(self, operations, files, device, grant, device_lease, cancellation, deadline):
        self.operations, self.files, self.device, self.grant = operations, files, device, grant
        self.device_lease = device_lease
        self.device_identity = _identity_info(os.fstat(device_lease[0]))
        self.cancellation, self.deadline = cancellation, deadline
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.record = None
        self.active = True

    def check(self):
        _bounds(self.cancellation, self.deadline)
        _require(self.active and self.pid == os.getpid() and self.thread == threading.get_ident(),
                 "android_recovery_cleanup_capability")
        operations, files = self.operations, self.files
        with operations._mutex:
            _require(not operations._closed and not operations._native_controls
                and not operations._native_dispatch_threads and not operations._recovery_dispatches,
                "android_recovery_host_busy")
        self.device._require_open()
        self.device._authority._require_parent_grant(self.grant)
        _require(self.grant.project_id == operations.config.registration.project["id"]
            and operations._configuration_record() == operations._configuration
            and operations._intent(files.inspection.operation_id, files.operation_fd) == files.intent
            and _same_identity(os.fstat(files.operation_fd), files.intent["rootIdentity"], directory=True),
            "android_recovery_cleanup_binding")
        for descriptor, directory, name, identity in (
            (files.producer_fd, files.operation_fd, "producer.lock", files.intent["producerIdentity"]),
            (self.device_lease[0], self.device_lease[1], self.device_lease[2], self.device_identity),
        ):
            _require(_same_identity(os.fstat(descriptor), identity), "android_recovery_cleanup_binding")
            probe = _open_regular_at(directory, name, expected=identity, writable=True)
            try:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AndroidOperationError("android_recovery_lock_not_held")
            finally:
                os.close(probe)
        if self.record is not None:
            row = self.device._authority.store.device(files.intent["scopeDigest"])
            _require(row is not None and row["generation"] == self.record["generation"]
                and row["host_incarnation"] == self.record["hostIncarnation"]
                and row["helper_incarnation"] == self.record["helperIncarnation"]
                and row["status"] == "quarantined" and row["quarantine_reason"] == "recovery-cleanup-pending",
                "android_recovery_cleanup_binding")

    def save(self, state):
        self.record["state"] = state
        _replace_at(self.files.operation_fd, "finalization.json", self.record)

    def discard(self):
        self.check()
        operations, files = self.operations, self.files
        state = operations._state(files.inspection.operation_id, files.operation_fd)
        status = operations._stage_status(files.operation_fd, files.intent, state, full=True)
        _require(validate_workspace(files.operation_fd, files.intent) == "idle", "android_native_call_unresolved")
        if status == "staged-discarded":
            return
        _require(status in {"prepared", "discard-intent-uncommitted", "staged-discard-incomplete",
                            "discard-state-uncommitted", "recovery-apks-prepared"}, "android_recovery_staging")
        from .android_recovery_materials import validate_materials, RECORD
        materials = validate_materials(files.operation_fd, files.intent, state)
        if status == "prepared":
            discard = {"schemaVersion": 1, "operationId": files.inspection.operation_id,
                "contextDigest": files.intent["contextDigest"], "configurationDigest": files.intent["configurationDigest"],
                "files": files.intent["files"], "state": "discarding"}
            _write_new_at(files.operation_fd, "discard.json", discard)
        else:
            discard = _read_json_at(files.operation_fd, "discard.json")
        state["stage"] = "discarding"
        _replace_at(files.operation_fd, "state.json", state)
        if materials is not None and materials['state'] == 'prepared':
            materials['state'] = 'discarding'
            _replace_at(files.operation_fd, RECORD, materials)
        staging = _open_child_directory(files.operation_fd, "staging", expected=files.intent["stagingIdentity"])
        try:
            for name in _APK_NAMES:
                self.check()
                if name not in os.listdir(staging):
                    continue
                expected = dict(files.intent["files"][name])
                if materials is not None and name in materials['files']:
                    expected['identity'] = materials['files'][name]
                descriptor = _open_regular_at(staging, name, expected=expected["identity"])
                try:
                    _require(_file_digest(descriptor, MAX_APK) == (expected["digest"], expected["bytes"]),
                             "android_recovery_staging")
                finally:
                    os.close(descriptor)
                operations._remove_staged(staging, name)
                os.fsync(staging)
            _require(not os.listdir(staging), "android_recovery_staging")
        finally:
            os.close(staging)
        if materials is not None:
            materials['state'] = 'discarded'
            _replace_at(files.operation_fd, RECORD, materials)
        discard["state"] = "discarded"
        _replace_at(files.operation_fd, "discard.json", discard)
        state["stage"] = "discarded"
        _replace_at(files.operation_fd, "state.json", state)


def require_cleanup(operations, capability, run_store):
    with operations._mutex:
        _require(type(capability) is AndroidCleanupCapability
            and operations._cleanup_exports.get(id(capability)) is capability
            and capability._active and not capability._consumed
            and run_store is operations.run_store, "android_recovery_cleanup_capability")
        session = capability._session
        _require(type(session) is _Session and session.operations is operations,
                 "android_recovery_cleanup_capability")
        session.check()
        record = validate_finalization(session.files.operation_fd, session.files.intent, session.files.native)
        row = run_store.status(capability.operation_id)
        _require(record == session.record and record["state"] == "sanitized"
            and capability.operation_id == record["operationId"]
            and capability.request_digest == record["requestDigest"]
            and capability.context_digest == record["contextDigest"]
            and capability.scope_digest == record["deviceFingerprint"]
            and capability.evidence_digest == contracts.digest(record)
            and row["requestDigest"] == capability.request_digest
            and row["state"] in {"admitted", "quarantined"}
            and row["reservedBytes"] == session.files.intent["reservedBytes"],
            "android_recovery_cleanup_capability")
        _require_sanitized(operations, session.files)
        capability._consumed = True
        return capability


def finalize_recovery(operations, operation_id, request_digest, *, device, parent_grant,
                      cancellation, deadline_monotonic):
    _require(type(device) is DeviceAuthority and device.device_kind == "android"
        and device._device_fingerprint == operations.config.scope_digest, "android_recovery_binding")
    session = capability = None
    try:
        with operations._admission(), operations._recovery_files(
                operation_id, request_digest, allow_finished=True, retire_metadata=True) as files, ExitStack() as stack:
            authority = device._authority
            record = validate_finalization(files.operation_fd, files.intent, files.native)
            reconciliation = _reconciliation(authority, record)
            run = operations.run_store.status(operation_id)
            if reconciliation is not None and run["state"] in {"failed", "cancelled"} and run["reservedBytes"] == 0:
                _require_sanitized(operations, files)
                if _released(authority.store.device(operations.config.scope_digest), record):
                    record["state"] = "completed"
                    _replace_at(files.operation_fd, "finalization.json", record)
                    if not device._closed and device.generation == record["generation"]:
                        device.close()
                    return AndroidFinalizationObservation(operation_id, files.intent["contextDigest"],
                        contracts.digest(record), True, True)
            with device._lock:
                snapshot = device.recovery_snapshot()
                device._require_recovery_snapshot(snapshot, parent_grant)
                # This extra duplicate outlives native-recovery exports and any
                # concurrent handle close; the original lock is never reacquired.
                device_lease = stack.enter_context(device._lease.borrow_descriptor())
            session = _Session(operations, files, device, parent_grant, device_lease,
                               cancellation, deadline_monotonic)
            session.check()
            if reconciliation is None:
                _require(parent_grant.grant_id != authority.store.device(operations.config.scope_digest)["grant_id"],
                         "android_recovery_fresh_grant_required")
                with operations._borrow_recovery_files(files, device=device, snapshot=snapshot,
                                                       parent_grant=parent_grant) as recovery:
                    helper = recover_android_helper(operations, recovery, cancellation=cancellation,
                                                    deadline_monotonic=deadline_monotonic)
                    _require(helper.fresh_helper_verified and helper.helper_collected,
                             "android_recovery_helper_unconfirmed")
                    observed = validate_record(files.operation_fd, files.intent, files.native)
                    dispositions = [authority.record_operation_disposition(snapshot,
                        operation_id=identity, terminal_status="recovered" if status == "uncertain" else "not-dispatched",
                        result_digest=helper.evidence_digest, evidence_digest=contracts.digest(observed))
                        for identity, status in snapshot.operations]
                    proof = authority.record_reconciliation(snapshot, dispositions=dispositions,
                        prior_helper_exit_digest=observed["steps"]["confirm-cleared"]["evidenceDigest"],
                        pointer_cleanup_digest=observed["helperRecovery"]["statusDigest"],
                        fresh_helper_incarnation=helper.helper_incarnation, fresh_handshake_digest=helper.evidence_digest)
                    record = {"schemaVersion": 1, "operationId": operation_id, "requestDigest": request_digest,
                        "contextDigest": files.intent["contextDigest"], "configurationDigest": files.intent["configurationDigest"],
                        "bindingDigest": files.native["bindingDigest"], "deviceFingerprint": operations.config.scope_digest,
                        "priorGeneration": snapshot.prior_generation, "priorHostIncarnation": snapshot.prior_host_incarnation,
                        "priorHelperIncarnation": snapshot.prior_helper_incarnation, "generation": snapshot.prior_generation+1,
                        "hostIncarnation": authority.host_incarnation, "helperIncarnation": helper.helper_incarnation,
                        "reconciliationId": proof.reconciliation_id, "reconciliationFingerprint": proof.reconciliation_fingerprint,
                        "priorHelperExitDigest": proof.prior_helper_exit_digest, "pointerCleanupDigest": proof.pointer_cleanup_digest,
                        "freshHandshakeDigest": proof.fresh_handshake_digest, "observationDigest": helper.evidence_digest,
                        "state": "prepared", "historyDigest": "0"*64 if record is None else contracts.digest(record)}
                    writer = _write_new_at if "finalization.json" not in os.listdir(files.operation_fd) else _replace_at
                    writer(files.operation_fd, "finalization.json", record)
                session.check()
                device.reconcile(proof, parent_grant=parent_grant, cleanup_pending=True)
            session.record = record
            session.check()
            session.save("reconciled")
            session.discard()
            session.check()
            session.save("sanitized")
            run = operations.run_store.status(operation_id)
            if run["state"] in {"admitted", "quarantined"}:
                capability = AndroidCleanupCapability(operation_id, request_digest, files.intent["contextDigest"],
                    operations.config.scope_digest, contracts.digest(session.record), session)
                with operations._mutex:
                    operations._cleanup_exports[id(capability)] = capability
                operations.run_store.finish_mobile_recovery(capability, authority=operations)
            else:
                _require(run["state"] in {"failed", "cancelled"} and run["reservedBytes"] == 0,
                         "android_recovery_cleanup_binding")
            session.check()
            with device._lock:
                now = authority._require_parent_grant(parent_grant)
                session.check()
                authority.store.finish_reconciliation_cleanup(
                    reconciliation_id=record["reconciliationId"], reconciliation_fingerprint=record["reconciliationFingerprint"],
                    device_fingerprint=record["deviceFingerprint"], generation=record["generation"],
                    host_incarnation=record["hostIncarnation"], helper_incarnation=record["helperIncarnation"], now_ns=now)
                session.save("completed")
                _require(device.close(), "android_recovery_release_unconfirmed")
            return AndroidFinalizationObservation(operation_id, files.intent["contextDigest"],
                                                   contracts.digest(session.record), True, True)
    except (OSError, RunDenied, contracts.ContractError):
        raise AndroidOperationError("android_recovery_finalization_unavailable") from None
    finally:
        if session is not None:
            session.active = False
        if capability is not None:
            with operations._mutex:
                capability._active = False
                operations._cleanup_exports.pop(id(capability), None)
