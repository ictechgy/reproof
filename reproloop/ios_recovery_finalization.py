"""Reconcile measured iOS recovery and release only after bounded file cleanup."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import fcntl
import math
import os
import threading
import time

from . import contracts
from .execution.journal import RunDenied, RunStore
from .execution.wire import decode_json
from .ios_fixture_recovery import recover_ios_fixtures, require_ios_fixture_recovery
from .ios_mobile_finalization import (
    discard_recovery_native_staged,
    verify_recovery_native_staged_disposed,
)
from .ios_mobile_inputs import IOSMobileInputsConfig
from .ios_mobile_native import _binding_record
from .ios_mobile_operation import IOSMobileOperationError, IOSMobileOperationStore
from .ios_native_recovery import (
    IOSNativeRecoveryFinalizationFiles,
    capture_native_recovery_finalization,
    native_recovery,
    require_native_recovery_finalization,
)
from .ios_recovery_execution import IOSRecoveryExecution
from .ios_recovery_helper import (
    recover_prior_helpers,
    require_ios_recovery_helper_retirement,
)
from .live.authority import DeviceAuthority, HostAuthority
from .repair_android_operation import (
    _identity_info,
    _open_child_directory,
    _open_regular_at,
    _read_fd,
    _read_json_at,
    _replace_at,
    _same_identity,
    _walk_directory,
    _write_new_at,
)


RECORD = "finalization.json"
_RECORD_LIMIT = 128 * 1024
_STATES = frozenset(("prepared", "reconciled", "sanitized", "completed"))


class IOSRecoveryFinalizationError(IOSMobileOperationError):
    def __init__(self, code="ios_recovery_finalization_unavailable"):
        self.code = code
        super().__init__(code)


def _fail(code="ios_recovery_finalization_unavailable"):
    raise IOSRecoveryFinalizationError(code) from None


def _require(value, code="ios_recovery_finalization_unavailable"):
    if not value:
        _fail(code)


@dataclass(frozen=True, slots=True)
class IOSRecoveryFinalizationObservation:
    operation_id: str
    context_digest: str
    evidence_digest: str
    reservation_released: bool
    ownership_released: bool


@dataclass(slots=True, repr=False)
class IOSRecoveryCleanupCapability:
    operation_id: str
    request_digest: str
    context_digest: str
    scope_digest: str
    evidence_digest: str
    expected_reserved_bytes: int
    _session: object = field(repr=False)
    _issuer: object = field(repr=False)
    _active: bool = field(default=True, repr=False)
    _consumed: bool = field(default=False, repr=False)

    def __repr__(self):
        return "<IOSRecoveryCleanupCapability>"


def _record_fields(record):
    return {
        "schemaVersion", "kind", "operationId", "requestDigest", "contextDigest",
        "configurationDigest", "nativeBindingDigest", "deviceFingerprint",
        "priorGeneration", "priorHostIncarnation", "priorHelperIncarnation",
        "generation", "hostIncarnation", "helperIncarnation",
        "reconciliationId", "reconciliationFingerprint",
        "priorHelperExitDigest", "pointerCleanupDigest", "freshHandshakeDigest",
        "fixtureRecoveryDigest", "originalSanitationDigest",
        "originalCleanupReceiptDigest", "attemptMaterialDisposalDigest",
        "observationDigest", "stagedDisposalDigest", "state", "historyDigest",
    }


def validate_ios_recovery_finalization_record(directory, intent, native):
    """Validate durable correlation data without issuing cleanup authority."""

    if RECORD not in os.listdir(directory):
        return None
    try:
        record = _read_json_at(directory, RECORD, _RECORD_LIMIT)
        _require(type(record) is dict and set(record) == _record_fields(record)
                 and record["schemaVersion"] == 1
                 and record["kind"] == "ios-native-recovery-finalization-v1"
                 and all(record[key] == intent[key] for key in
                         ("operationId", "requestDigest", "contextDigest",
                          "configurationDigest"))
                 and record["nativeBindingDigest"] == native["bindingDigest"]
                 and record["deviceFingerprint"] == intent["context"]["scope_digest"]
                 and record["priorGeneration"] == native["ownershipGeneration"]
                 and record["priorHostIncarnation"] == native["hostIncarnation"]
                 and record["priorHelperIncarnation"] == native["helperIncarnation"]
                 and record["generation"] == record["priorGeneration"] + 1
                 and record["helperIncarnation"] != record["priorHelperIncarnation"]
                 and record["state"] in _STATES,
                 "ios_recovery_finalization_record")
        for key in ("operationId", "hostIncarnation", "helperIncarnation",
                    "priorHostIncarnation", "priorHelperIncarnation",
                    "reconciliationId"):
            contracts.validate_id(record[key])
        for key in (
            "requestDigest", "contextDigest", "configurationDigest",
            "nativeBindingDigest", "deviceFingerprint", "reconciliationFingerprint",
            "priorHelperExitDigest", "pointerCleanupDigest", "freshHandshakeDigest",
            "fixtureRecoveryDigest", "originalSanitationDigest",
            "originalCleanupReceiptDigest", "attemptMaterialDisposalDigest",
            "observationDigest", "historyDigest",
        ):
            contracts.validate_digest(record[key])
        _require(record["stagedDisposalDigest"] is None
                 if record["state"] in {"prepared", "reconciled"}
                 else True, "ios_recovery_finalization_record")
        if record["stagedDisposalDigest"] is not None:
            contracts.validate_digest(record["stagedDisposalDigest"])
        if record["state"] in {"sanitized", "completed"}:
            _require(record["stagedDisposalDigest"] is not None,
                     "ios_recovery_finalization_record")
        return record
    except IOSRecoveryFinalizationError:
        raise
    except (contracts.ContractError, OSError, RuntimeError, TypeError, ValueError, KeyError):
        _fail("ios_recovery_finalization_record")


def _reconciliation(authority, record):
    if record is None:
        return None
    row = authority.store.reconciliation(record["reconciliationId"])
    if row is None:
        _require(record["state"] == "prepared",
                 "ios_recovery_reconciliation_missing")
        return None
    bindings = {
        "reconciliation_id": "reconciliationId",
        "reconciliation_fingerprint": "reconciliationFingerprint",
        "device_fingerprint": "deviceFingerprint",
        "prior_generation": "priorGeneration",
        "prior_host_incarnation": "priorHostIncarnation",
        "prior_helper_incarnation": "priorHelperIncarnation",
        "fresh_helper_incarnation": "helperIncarnation",
        "prior_helper_exit_digest": "priorHelperExitDigest",
        "pointer_cleanup_digest": "pointerCleanupDigest",
        "fresh_handshake_digest": "freshHandshakeDigest",
    }
    _require(all(row[key] == record[value] for key, value in bindings.items()),
             "ios_recovery_reconciliation_binding")
    return row


def _released(row, record):
    return row is not None and (
        row["generation"] > record["generation"]
        or (row["generation"] == record["generation"]
            and row["status"] == "released"
            and row["host_incarnation"] == record["hostIncarnation"]
            and row["helper_incarnation"] == record["helperIncarnation"])
    )


class _Session:
    def __init__(self, operations, files, record, cancellation, deadline):
        self.operations = operations
        self.files = files
        self.record = record
        self.cancellation = cancellation
        self.deadline = deadline
        self._issuer = object()
        self.active = True
        self.run_directory_identity = None
        parent = directory = None
        try:
            parent = _walk_directory(operations.run_store.root / "runs")
            directory = _open_child_directory(parent, files.operation_id)
            self.run_directory_identity = _identity_info(os.fstat(directory))
        finally:
            if directory is not None:
                os.close(directory)
            if parent is not None:
                os.close(parent)

    def check(self):
        _require(self.active and time.monotonic() < self.deadline
                 and not self.cancellation.is_set(),
                 "ios_recovery_finalization_bounds")
        require_native_recovery_finalization(self.files)
        current = validate_ios_recovery_finalization_record(
            self.files.directory, self.files.intent, self.files.native
        )
        _require(current == self.record,
                 "ios_recovery_finalization_binding")
        if self.record["state"] != "prepared":
            _require(_reconciliation(self.files._device._authority, self.record) is not None,
                     "ios_recovery_reconciliation_missing")
        return self

    def save(self, state, *, staged_disposal_digest=None, checked=True):
        if checked:
            self.check()
        _require(state in _STATES, "ios_recovery_finalization_record")
        if staged_disposal_digest is not None:
            contracts.validate_digest(staged_disposal_digest)
            self.record["stagedDisposalDigest"] = staged_disposal_digest
        self.record["state"] = state
        _replace_at(self.files.directory, RECORD, self.record)

    def remove_run_hold(self):
        self.check()
        parent = directory = descriptor = None
        try:
            parent = _walk_directory(self.operations.run_store.root / "runs")
            directory = _open_child_directory(parent, self.files.operation_id,
                                               expected=self.run_directory_identity)
            names = set(os.listdir(directory))
            _require(names <= {"intent.json"},
                     "ios_recovery_run_hold_unknown")
            if "intent.json" in names:
                descriptor = _open_regular_at(directory, "intent.json")
                identity = _identity_info(os.fstat(descriptor))
                _require(decode_json(_read_fd(descriptor, 4096)) == {
                    "kind": "ios-mobile-preparation-hold",
                    "contextDigest": self.files.context_digest,
                }, "ios_recovery_run_hold_unknown")
                _require(_same_identity(
                    os.stat("intent.json", dir_fd=directory, follow_symlinks=False),
                    identity,
                ), "ios_recovery_run_hold_unknown")
                os.unlink("intent.json", dir_fd=directory)
                os.fsync(directory)
            _require(not os.listdir(directory), "ios_recovery_run_hold_unknown")
        finally:
            for selected in (descriptor, directory, parent):
                if selected is not None:
                    os.close(selected)


def require_ios_recovery_cleanup(authority, capability, run_store):
    try:
        _require(type(authority) is IOSMobileOperationStore
                 and type(run_store) is RunStore
                 and authority.run_store is run_store
                 and type(capability) is IOSRecoveryCleanupCapability,
                 "ios_recovery_cleanup_capability")
        exports = getattr(authority, "_ios_recovery_cleanup_exports", {})
        _require(exports.get(id(capability)) is capability
                 and capability._active and not capability._consumed,
                 "ios_recovery_cleanup_capability")
        session = capability._session
        _require(type(session) is _Session and session.operations is authority
                 and capability._issuer is session._issuer,
                 "ios_recovery_cleanup_capability")
        session.check()
        record = session.record
        _require(record["state"] == "sanitized"
                 and capability.operation_id == record["operationId"]
                 and capability.request_digest == record["requestDigest"]
                 and capability.context_digest == record["contextDigest"]
                 and capability.scope_digest == record["deviceFingerprint"]
                 and capability.evidence_digest == contracts.digest(record)
                 and capability.expected_reserved_bytes == session.files.intent["reservedBytes"],
                 "ios_recovery_cleanup_capability")
        _require(verify_recovery_native_staged_disposed(
            session.files, record["stagedDisposalDigest"],
            cancellation=session.cancellation, deadline_monotonic=session.deadline,
        ) == record["stagedDisposalDigest"], "ios_recovery_cleanup_capability")
        return session
    except IOSRecoveryFinalizationError:
        raise
    except (RunDenied, OSError, RuntimeError, TypeError, ValueError, KeyError):
        _fail("ios_recovery_cleanup_capability")


def _register_cleanup(session):
    operations = session.operations
    capability = IOSRecoveryCleanupCapability(
        session.files.operation_id, session.files.request_digest,
        session.files.context_digest, session.files.scope_digest,
        contracts.digest(session.record), session.files.intent["reservedBytes"],
        session, session._issuer,
    )
    with operations._changed:
        exports = getattr(operations, "_ios_recovery_cleanup_exports", None)
        if exports is None:
            exports = {}
            operations._ios_recovery_cleanup_exports = exports
        _require(not exports, "ios_recovery_cleanup_capability")
        exports[id(capability)] = capability
    return capability


def _unregister_cleanup(operations, capability):
    if capability is None:
        return
    with operations._changed:
        capability._active = False
        getattr(operations, "_ios_recovery_cleanup_exports", {}).pop(id(capability), None)
        operations._changed.notify_all()


def _handshake_digest(handshake):
    return contracts.digest({
        "protocolVersion": handshake.protocol_version,
        "helperVersion": handshake.helper_version,
        "helperIncarnation": handshake.helper_incarnation,
        "providerIncarnation": handshake.provider_incarnation,
        "nativeIncarnation": handshake.native_incarnation,
        "nativeClockId": handshake.native_clock_id,
        "nativeTimeMs": handshake.native_time_ms,
    })


def _make_record(context, snapshot, authority, reconciliation, retirement,
                 fixture, observation, execution, previous):
    execution.require_observation(observation, require_materials_disposed=True)
    retirement = require_ios_recovery_helper_retirement(retirement, context)
    fixture = require_ios_fixture_recovery(fixture, context)
    material_digest = observation.public()["materialDisposalDigest"]
    aggregate = contracts.digest({
        "schemaVersion": 1,
        "kind": "ios-native-recovery-observations-v1",
        "contextDigest": context.context_digest,
        "retirementDigest": retirement.evidence_digest,
        "fixtureRecoveryDigest": fixture.evidence_digest,
        "originalSanitationDigest": observation.evidence_digest,
        "originalCleanupReceiptDigest": observation.cleanup_receipt_digest,
        "attemptMaterialDisposalDigest": material_digest,
    })
    return {
        "schemaVersion": 1,
        "kind": "ios-native-recovery-finalization-v1",
        "operationId": context.operation_id,
        "requestDigest": context.request_digest,
        "contextDigest": context.context_digest,
        "configurationDigest": context.configuration_digest,
        "nativeBindingDigest": context.binding_digest,
        "deviceFingerprint": context.scope_digest,
        "priorGeneration": snapshot.prior_generation,
        "priorHostIncarnation": snapshot.prior_host_incarnation,
        "priorHelperIncarnation": snapshot.prior_helper_incarnation,
        "generation": snapshot.prior_generation + 1,
        "hostIncarnation": authority.host_incarnation,
        "helperIncarnation": observation.handshake.helper_incarnation,
        "reconciliationId": reconciliation.reconciliation_id,
        "reconciliationFingerprint": reconciliation.reconciliation_fingerprint,
        "priorHelperExitDigest": retirement.evidence_digest,
        "pointerCleanupDigest": observation.cleanup_evidence_digest,
        "freshHandshakeDigest": _handshake_digest(observation.handshake),
        "fixtureRecoveryDigest": fixture.evidence_digest,
        "originalSanitationDigest": observation.evidence_digest,
        "originalCleanupReceiptDigest": observation.cleanup_receipt_digest,
        "attemptMaterialDisposalDigest": material_digest,
        "observationDigest": aggregate,
        "stagedDisposalDigest": None,
        "state": "prepared",
        "historyDigest": "0" * 64 if previous is None else contracts.digest(previous),
    }


def _open_record(operations, operation_id, request_digest):
    with operations._directory(operation_id) as directory:
        intent, state = operations._records(operation_id, directory)
        _require(intent["requestDigest"] == request_digest,
                 "ios_recovery_finalization_binding")
        native = _binding_record(directory, intent, state)
        _require(native is not None, "ios_recovery_finalization_binding")
        return validate_ios_recovery_finalization_record(directory, intent, native)


def _assert_terminal_disposal(operations, operation_id, request_digest, record):
    """Read-only proof that a completed authority row still has no staged payload."""

    from .ios_mobile_finalization import _GENERATED_WORK_FILES
    from .ios_native_recovery import _validate_operation_journals
    from .ios_recovery_execution import _archive_disposal_digest

    with operations._directory(operation_id) as directory:
        intent, state = operations._records(operation_id, directory)
        native = _binding_record(directory, intent, state)
        _require(intent["requestDigest"] == request_digest and native is not None,
                 "ios_recovery_finalization_binding")
        _validate_operation_journals(operations, directory, intent, state, native)
        final = _open_child_directory(directory, "native-finalization")
        try:
            final_intent = _read_json_at(final, "intent.json")
            final_state = _read_json_at(final, "state.json")
            _require(final_state["state"] == "discarded",
                     "ios_recovery_cleanup_incomplete")
            for role in operations._roles:
                role_fd = _open_child_directory(
                    directory, role, expected=final_intent["roles"][role]["directoryIdentity"]
                )
                transfer = None
                try:
                    _require(set(os.listdir(role_fd)) == {"transfer"},
                             "ios_recovery_cleanup_incomplete")
                    transfer = _open_child_directory(
                        role_fd, "transfer",
                        expected=final_intent["roles"][role]["transferIdentity"],
                    )
                    _require(not os.listdir(transfer), "ios_recovery_cleanup_incomplete")
                finally:
                    if transfer is not None:
                        os.close(transfer)
                    os.close(role_fd)
            for name, row in final_intent["commands"].items():
                command = _open_child_directory(directory, name,
                                                expected=row["directoryIdentity"])
                try:
                    _require(not (set(os.listdir(command)) & _GENERATED_WORK_FILES),
                             "ios_recovery_cleanup_incomplete")
                finally:
                    os.close(command)
            native_evidence = final_state["evidenceDigest"]
        finally:
            os.close(final)
        recovery = _open_child_directory(directory, "native-recovery")
        try:
            root_state = _read_json_at(recovery, "state.json")
            _require(root_state["archiveState"] == "discarded"
                     and root_state["archiveDisposalDigest"]
                     == _archive_disposal_digest(root_state),
                     "ios_recovery_cleanup_incomplete")
            archives = _open_child_directory(recovery, "archives")
            try:
                _require(not os.listdir(archives), "ios_recovery_cleanup_incomplete")
            finally:
                os.close(archives)
            for name in (item for item in os.listdir(recovery)
                         if item.startswith("attempt-")):
                attempt = _open_child_directory(recovery, name)
                try:
                    attempt_state = _read_json_at(attempt, "state.json")
                    _require(attempt_state["materialState"] == "retired",
                             "ios_recovery_cleanup_incomplete")
                    for command_name in (item for item in os.listdir(attempt)
                                         if item.startswith("command-")):
                        command = _open_child_directory(attempt, command_name)
                        try:
                            _require(not (set(os.listdir(command)) & _GENERATED_WORK_FILES),
                                     "ios_recovery_cleanup_incomplete")
                        finally:
                            os.close(command)
                finally:
                    os.close(attempt)
            recovery_evidence = root_state["archiveDisposalDigest"]
        finally:
            os.close(recovery)
        actual = contracts.digest({
            "schemaVersion": 1,
            "kind": "ios-recovery-staged-disposal-v1",
            "operationId": operation_id,
            "contextDigest": intent["contextDigest"],
            "nativeEvidenceDigest": native_evidence,
            "recoveryEvidenceDigest": recovery_evidence,
        })
        _require(actual == record["stagedDisposalDigest"],
                 "ios_recovery_cleanup_incomplete")


@contextmanager
def _resume_files(operations, operation_id, request_digest, *, device,
                  parent_grant, cancellation, deadline_monotonic):
    files = None
    with ExitStack() as stack:
        stack.enter_context(operations.run_store.repair_scope_lease(
            "mobile-device", operations.definition.scope_digest
        ))
        run_root = _walk_directory(operations.run_store.root)
        stack.callback(os.close, run_root)
        vm_lock = _open_regular_at(run_root, ".vm-lock", writable=True)
        stack.callback(os.close, vm_lock)
        try:
            fcntl.flock(vm_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("ios_recovery_finalization_busy")
        operation_parent = _walk_directory(operations.operations)
        stack.callback(os.close, operation_parent)
        directory = _open_child_directory(operation_parent, operation_id)
        stack.callback(os.close, directory)
        intent, state = operations._records(operation_id, directory)
        native = _binding_record(directory, intent, state)
        _require(intent["requestDigest"] == request_digest and native is not None,
                 "ios_recovery_finalization_binding")
        record = validate_ios_recovery_finalization_record(directory, intent, native)
        _require(record is not None and _reconciliation(device._authority, record) is not None,
                 "ios_recovery_reconciliation_missing")
        producer = _open_regular_at(directory, "producer.lock",
                                    expected=intent["producerIdentity"], writable=True)
        stack.callback(os.close, producer)
        try:
            fcntl.flock(producer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("ios_recovery_finalization_busy")
        device_descriptor, device_directory, device_name = stack.enter_context(
            device._lease.borrow_descriptor()
        )
        files = IOSNativeRecoveryFinalizationFiles(
            operation_id, request_digest, intent["contextDigest"],
            operations.definition.scope_digest, intent["configurationDigest"],
            native["bindingDigest"], native["ownershipGeneration"],
            directory, producer, device_descriptor, device_directory, device_name,
            intent, native, operations, device, parent_grant, cancellation,
            deadline_monotonic, os.getpid(), threading.get_ident(),
            threading.current_thread(),
        )
        files.reconciled_host_incarnation = record["hostIncarnation"]
        files.reconciled_helper_incarnation = record["helperIncarnation"]
        with operations._changed:
            exports = operations._native_recovery_finalization_exports
            _require(not exports, "ios_recovery_finalization_busy")
            exports[id(files)] = files
        try:
            require_native_recovery_finalization(files)
            yield files, record
        finally:
            with operations._changed:
                files._active = False
                operations._native_recovery_finalization_exports.pop(id(files), None)
                operations._changed.notify_all()


def _finish_session(session, device, parent_grant):
    capability = None
    operations = session.operations
    authority = device._authority
    try:
        if session.record["state"] == "prepared":
            session.save("reconciled")
        if session.record["state"] == "reconciled":
            disposal = discard_recovery_native_staged(
                session.files, cancellation=session.cancellation,
                deadline_monotonic=session.deadline,
            )
            session.save("sanitized", staged_disposal_digest=disposal)
        run = operations.run_store.status(session.files.operation_id)
        if run["state"] in {"admitted", "quarantined"}:
            capability = _register_cleanup(session)
            operations.run_store.finish_ios_native_recovery(
                capability, authority=operations
            )
        else:
            _require(run["state"] in {"failed", "cancelled"}
                     and run["reservedBytes"] == 0,
                     "ios_recovery_cleanup_binding")
        _require(verify_recovery_native_staged_disposed(
            session.files, session.record["stagedDisposalDigest"],
            cancellation=session.cancellation, deadline_monotonic=session.deadline,
        ) == session.record["stagedDisposalDigest"],
                 "ios_recovery_cleanup_incomplete")
        session.check()
        with device._lock:
            now = authority._require_parent_grant(parent_grant)
            session.check()
            authority.store.finish_reconciliation_cleanup(
                reconciliation_id=session.record["reconciliationId"],
                reconciliation_fingerprint=session.record["reconciliationFingerprint"],
                device_fingerprint=session.record["deviceFingerprint"],
                generation=session.record["generation"],
                host_incarnation=session.record["hostIncarnation"],
                helper_incarnation=session.record["helperIncarnation"],
                now_ns=now,
            )
            _require(device.close(), "ios_recovery_release_unconfirmed")
            # Close the exact reconciled handle before the fallible private
            # completion write.  The outer duplicated descriptors retain the
            # lock until this function exits, so no new owner can race the
            # record update and a failed write cannot leak the live handle.
            session.save("completed", checked=False)
        return IOSRecoveryFinalizationObservation(
            session.files.operation_id, session.files.context_digest,
            contracts.digest(session.record), True, True,
        )
    finally:
        _unregister_cleanup(operations, capability)


def finalize_recovery(operations, operation_id, request_digest, *, config, device,
                      parent_grant, cancellation, deadline_monotonic):
    """Run fixed iOS recovery and retain quarantine until every cleanup commits."""

    session = None
    try:
        _require(type(operations) is IOSMobileOperationStore
                 and type(config) is IOSMobileInputsConfig
                 and config.definition == operations.definition
                 and type(device) is DeviceAuthority
                 and type(device._authority) is HostAuthority
                 and device.device_kind == "ios-physical"
                 and device._device_fingerprint == operations.definition.scope_digest
                 and callable(getattr(cancellation, "is_set", None))
                 and type(deadline_monotonic) in (int, float)
                 and not isinstance(deadline_monotonic, bool)
                 and math.isfinite(deadline_monotonic)
                 and time.monotonic() < deadline_monotonic
                 and not cancellation.is_set(),
                 "ios_recovery_finalization_binding")
        authority = device._authority
        previous = _open_record(operations, operation_id, request_digest)
        if previous is not None:
            reconciliation = _reconciliation(authority, previous)
            row = authority.store.device(operations.definition.scope_digest)
            run = operations.run_store.status(operation_id)
            if reconciliation is not None and run["state"] in {"failed", "cancelled"} \
                    and run["reservedBytes"] == 0 and _released(row, previous):
                _assert_terminal_disposal(
                    operations, operation_id, request_digest, previous
                )
                if (not device._closed
                        and row["generation"] == previous["generation"]
                        and device.generation == previous["generation"]
                        and row["host_incarnation"] == previous["hostIncarnation"]
                        and row["helper_incarnation"] == previous["helperIncarnation"]):
                    _require(device.close(), "ios_recovery_release_unconfirmed")
                return IOSRecoveryFinalizationObservation(
                    operation_id, previous["contextDigest"], contracts.digest(previous),
                    True, True,
                )
            if reconciliation is not None:
                with _resume_files(
                    operations, operation_id, request_digest, device=device,
                    parent_grant=parent_grant, cancellation=cancellation,
                    deadline_monotonic=deadline_monotonic,
                ) as (files, record):
                    session = _Session(operations, files, record, cancellation,
                                       deadline_monotonic)
                    return _finish_session(session, device, parent_grant)

        with ExitStack() as retained:
            snapshot = device.recovery_snapshot()
            device._require_recovery_snapshot(snapshot, parent_grant)
            with native_recovery(
                operations, operation_id, request_digest, device=device,
                snapshot=snapshot, parent_grant=parent_grant,
                cancellation=cancellation, deadline_monotonic=deadline_monotonic,
            ) as context:
                execution = IOSRecoveryExecution(context, config)
                with execution:
                    retirement = recover_prior_helpers(
                        context, config, cancellation=cancellation,
                        deadline_monotonic=deadline_monotonic,
                    )
                    observation = execution.run_original_sanitation(
                        cancellation=cancellation,
                        deadline_monotonic=deadline_monotonic,
                    )
                execution.require_observation(
                    observation, require_materials_disposed=True
                )
                fixture = recover_ios_fixtures(
                    context, config, cancellation=cancellation,
                    deadline_monotonic=deadline_monotonic,
                )
                retirement = require_ios_recovery_helper_retirement(retirement, context)
                fixture = require_ios_fixture_recovery(fixture, context)
                handshake_digest = _handshake_digest(observation.handshake)
                evidence = contracts.digest({
                    "retirement": retirement.evidence_digest,
                    "fixture": fixture.evidence_digest,
                    "sanitation": observation.evidence_digest,
                    "cleanup": observation.cleanup_receipt_digest,
                })
                dispositions = [authority.record_operation_disposition(
                    snapshot,
                    operation_id=identity,
                    terminal_status="recovered" if status == "uncertain" else "not-dispatched",
                    result_digest=evidence,
                    evidence_digest=evidence,
                ) for identity, status in snapshot.operations]
                proof = authority.record_reconciliation(
                    snapshot, dispositions=dispositions,
                    prior_helper_exit_digest=retirement.evidence_digest,
                    pointer_cleanup_digest=observation.cleanup_evidence_digest,
                    fresh_helper_incarnation=observation.handshake.helper_incarnation,
                    fresh_handshake_digest=handshake_digest,
                )
                files = retained.enter_context(
                    capture_native_recovery_finalization(context)
                )
                record = _make_record(
                    context, snapshot, authority, proof, retirement,
                    fixture, observation, execution, previous,
                )
                files.reconciled_host_incarnation = record["hostIncarnation"]
                files.reconciled_helper_incarnation = record["helperIncarnation"]
                writer = (_replace_at if RECORD in os.listdir(context.directory)
                          else _write_new_at)
                writer(context.directory, RECORD, record)
                validate_ios_recovery_finalization_record(
                    context.directory, context.intent, context.native
                )
            require_native_recovery_finalization(files)
            device.reconcile(proof, parent_grant=parent_grant, cleanup_pending=True)
            session = _Session(operations, files, record, cancellation,
                               deadline_monotonic)
            return _finish_session(session, device, parent_grant)
    except IOSRecoveryFinalizationError:
        raise
    except IOSMobileOperationError as error:
        _fail(getattr(error, "code", "ios_recovery_finalization_unavailable"))
    except (RunDenied, contracts.ContractError, OSError,
            RuntimeError, TypeError, ValueError, KeyError, AttributeError):
        _fail()
    finally:
        if session is not None:
            session.active = False


__all__ = [
    "IOSRecoveryCleanupCapability", "IOSRecoveryFinalizationError",
    "IOSRecoveryFinalizationObservation", "RECORD", "finalize_recovery",
    "require_ios_recovery_cleanup", "validate_ios_recovery_finalization_record",
]
