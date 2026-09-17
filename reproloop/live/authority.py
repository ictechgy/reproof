"""Single durable host authority and G1b provider/native dispatch capabilities."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid

from reproloop.core import ContractError
from reproloop.storage import Lease
from .clock_sync import ClockMapping, ClockSynchronizer
from .state_store import FORMAT_VERSION, StateStore


PROTOCOL_VERSION = 1
NATIVE_PROTOCOL_VERSION = 2
CODE_VERSION = 2
HELPER_VERSION = 2
MAX_CACHE_ENTRIES = 4_096
MAX_SEQUENCE = 2 ** 31 - 1
MAX_NS = 2 ** 63 - 1
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DEVICE_KINDS = frozenset(("android", "ios-physical", "ios-simulator"))
COMPATIBILITY_TABLE = {
    "legacy-offline-v1": {"code": 1, "store": 1, "helper": 1, "protocol": 1},
    "shared-v2": {"code": CODE_VERSION, "store": FORMAT_VERSION,
                  "helper": HELPER_VERSION, "protocol": NATIVE_PROTOCOL_VERSION},
}
QUALIFIED_NATIVE_CLOCKS = {
    ("android", "android-elapsed-realtime"): {
        "maxRateErrorPpm": 1_000, "mappingUncertaintyMs": 1,
    },
    ("ios-physical", "ios-mach-continuous"): {
        "maxRateErrorPpm": 1_000, "mappingUncertaintyMs": 1,
    },
    # Simulator and host both use the same boot-scoped mach_continuous_time
    # domain, with only millisecond wire quantization.
    ("ios-simulator", "ios-mach-continuous"): {
        "maxRateErrorPpm": 0, "mappingUncertaintyMs": 1,
    },
}


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _identifier(value, field_name):
    _require(type(value) is str and _ID.fullmatch(value) is not None,
             f"Invalid {field_name}")
    return value


def _digest(value, field_name):
    _require(type(value) is str and _DIGEST.fullmatch(value) is not None,
             f"Invalid {field_name}")
    return value


def _integer(value, field_name, low=0, high=MAX_NS):
    _require(type(value) is int and low <= value <= high, f"Invalid {field_name}")
    return value


def _canonical_digest(value):
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ContractError("Invalid canonical authority data") from None
    return hashlib.sha256(encoded).hexdigest()


def _bounded_alias(value):
    if value is None:
        return
    try:
        valid = type(value) is str and 0 < len(value.encode("utf-8")) <= 80
    except UnicodeError:
        valid = False
    _require(valid and not any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ), "Invalid device display alias")


def _canonical_device(device_kind, physical_id):
    _require(device_kind in _DEVICE_KINDS, "Unsupported device kind")
    try:
        encoded_length = len(physical_id.encode("utf-8")) if type(physical_id) is str else 0
    except UnicodeError:
        encoded_length = 0
    _require(
        type(physical_id) is str
        and 0 < encoded_length <= 256
        and not any(ord(character) < 33 or ord(character) == 127 for character in physical_id),
        "Invalid physical device identity",
    )
    if device_kind == "android":
        lease_identity = physical_id
    elif device_kind == "ios-physical":
        lease_identity = "ios-device:" + physical_id
    else:
        lease_identity = "ios-simulator:" + physical_id
    fingerprint = hashlib.sha256(lease_identity.encode("utf-8")).hexdigest()
    return lease_identity, fingerprint


def canonical_device_fingerprint(device_kind, physical_id):
    """Public opaque identity shared by physical leases and enrolled inventory."""
    return _canonical_device(device_kind, physical_id)[1]


@dataclass(frozen=True, slots=True)
class ParentGrant:
    grant_id: str
    project_id: str
    controller_id: str
    renewal_sequence: int
    coordinator_clock_id: str
    coordinator_deadline_ns: int
    local_deadline_ns: int
    local_deadline_latest_ns: int
    mapping_id: str
    host_clock_id: str
    host_boot_digest: str
    grant_fingerprint: str
    _mapping: ClockMapping = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class OperationAdmission:
    protocol_version: int
    operation_id: str
    operation_fingerprint: str
    payload_digest: str
    project_id: str
    session_id: str
    controller_id: str
    sequence: int
    generation: int
    host_incarnation: str
    helper_incarnation: str
    deadline_ns: int
    status: str
    result_digest: str | None
    is_new: bool
    _device_fingerprint: str = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DispatchPermit:
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
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class NativeHandshake:
    protocol_version: int
    helper_version: int
    helper_incarnation: str
    provider_incarnation: str
    native_incarnation: str
    native_clock_id: str
    native_time_ms: int
    host_received_ns: int
    max_rate_error_ppm: int
    mapping_uncertainty_ms: int
    _device_fingerprint: str = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class NativeGrant:
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
    native_incarnation: str
    native_clock_id: str
    native_deadline_ms: int
    _issuer: object = field(repr=False, compare=False)

    def wire(self):
        return {
            "protocolVersion": self.protocol_version,
            "operationId": self.operation_id,
            "operationFingerprint": self.operation_fingerprint,
            "payloadDigest": self.payload_digest,
            "projectId": self.project_id,
            "sessionId": self.session_id,
            "controllerId": self.controller_id,
            "sequence": self.sequence,
            "ownershipGeneration": self.ownership_generation,
            "hostIncarnation": self.host_incarnation,
            "helperIncarnation": self.helper_incarnation,
            "providerIncarnation": self.provider_incarnation,
            "nativeIncarnation": self.native_incarnation,
            "nativeClockId": self.native_clock_id,
            "nativeDeadlineMs": self.native_deadline_ms,
        }


@dataclass(frozen=True, slots=True)
class ProviderResult:
    receipt_id: str
    status: str
    result_digest: str

    def __post_init__(self):
        _identifier(self.receipt_id, "provider receipt identity")
        _require(self.status in ("succeeded", "rejected", "unknown"),
                 "Invalid provider result status")
        _digest(self.result_digest, "provider result digest")


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    device_fingerprint: str
    prior_generation: int
    prior_host_incarnation: str
    prior_helper_incarnation: str
    quarantine_reason: str
    operations: tuple[tuple[str, str], ...]
    _issuer: object = field(repr=False, compare=False)


@dataclass(slots=True)
class NativeRecoveryLease:
    device_fingerprint: str
    prior_generation: int
    prior_host_incarnation: str = field(repr=False)
    prior_helper_incarnation: str = field(repr=False)
    descriptor: int = field(repr=False)
    directory_descriptor: int = field(repr=False)
    lock_name: str = field(repr=False)
    _snapshot: RecoverySnapshot = field(repr=False)
    _grant: ParentGrant = field(repr=False)
    _pid: int = field(repr=False)
    _thread: int = field(repr=False)
    _active: bool = field(default=True, repr=False)


@dataclass(frozen=True, slots=True)
class TrustedOperationDisposition:
    device_fingerprint: str
    prior_generation: int
    operation_id: str
    terminal_status: str
    result_digest: str
    evidence_digest: str
    disposition_fingerprint: str
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class TrustedReconciliation:
    reconciliation_id: str
    reconciliation_fingerprint: str
    device_fingerprint: str
    prior_generation: int
    prior_host_incarnation: str
    prior_helper_incarnation: str
    prior_helper_exit_digest: str
    pointer_cleanup_digest: str
    fresh_helper_incarnation: str
    fresh_handshake_digest: str
    dispositions: tuple[TrustedOperationDisposition, ...]
    _issuer: object = field(repr=False, compare=False)


class _BorrowedLease:
    """No-op context over the authority-owned canonical legacy lock."""

    def __init__(self, device):
        self._device = device

    def __enter__(self):
        self._device._require_open()
        return self

    def __exit__(self, *_):
        # The authority remains the sole owner.  Legacy provider cleanup must
        # not release the physical lock behind it.
        return False


class HostAuthority:
    """Trusted process-local composition root backed by one durable journal."""

    def __init__(
        self,
        state_path=None,
        *,
        clock=None,
        lease_directory=None,
        operation_cache_size=256,
        max_clock_uncertainty_ns=250_000_000,
    ):
        if state_path is None:
            state_path = (
                Path(tempfile.gettempdir())
                / f"reproloop-authority-{os.getuid()}"
                / "authority.sqlite3"
            )
        self.clock_sync = ClockSynchronizer(
            clock, max_mapping_uncertainty_ns=max_clock_uncertainty_ns
        )
        self.max_clock_uncertainty_ns = _integer(
            max_clock_uncertainty_ns, "maximum clock uncertainty", 0, MAX_NS
        )
        self.lease_directory = lease_directory
        self.operation_cache_size = _integer(
            operation_cache_size, "operation cache size", 1, MAX_CACHE_ENTRIES
        )
        self.host_incarnation = "host_" + uuid.uuid4().hex
        self._issuer = object()
        self._handles = set()
        self._lock = threading.RLock()
        self._closed = False
        self.store = StateStore(state_path)
        self.authority_root_digest = hashlib.sha256(
            b"reproloop-authority-root\0" + str(self.store.path).encode("utf-8")
        ).hexdigest()

    def _require_open(self):
        _require(not self._closed, "Host authority is closed")

    def inventory_ownership(self, *, device_kind, physical_id):
        """Attest the durable G1 journal to an authenticated coordinator.

        The returned digest binds a receipt; authenticated host transport supplies
        its provenance. This read cannot release, reconcile, or claim a device.
        """
        with self._lock:
            self._require_open()
            fingerprint = canonical_device_fingerprint(device_kind, physical_id)
            row = self.store.device(fingerprint)
            value = {
                "physicalDigest": fingerprint,
                "authorityRootDigest": self.authority_root_digest,
                "generation": 0 if row is None else row["generation"],
                "status": "unused" if row is None else row["status"],
                "hostIncarnation": self.host_incarnation if row is None else row["host_incarnation"],
                "helperIncarnation": None if row is None else row["helper_incarnation"],
            }
            value["releaseReceiptDigest"] = (
                _canonical_digest(value) if value["status"] == "released" else None)
            return value

    def _require_parent_grant(self, grant):
        fields = self._parent_grant_fields(grant)
        _require(type(grant) is ParentGrant and grant._issuer is self._issuer
                 and grant.grant_fingerprint == _canonical_digest(fields),
                 "Trusted parent grant required")
        sample = self.clock_sync.require_mapping(grant._mapping)
        interval = grant._mapping.translate(grant.coordinator_deadline_ns)
        _require(
            grant.host_clock_id == sample.clock_id
            and grant.host_boot_digest == sample.boot_digest
            and grant.mapping_id == grant._mapping.mapping_id
            and grant.local_deadline_ns == interval.earliest_ns
            and grant.local_deadline_latest_ns == interval.latest_ns,
            "Clock mapping is incompatible",
        )
        now = sample.nanoseconds + sample.uncertainty_ns
        _require(now < grant.local_deadline_ns, "Operation authority expired")
        return now

    @staticmethod
    def _parent_grant_fields(grant):
        return {
            "grantId": getattr(grant, "grant_id", None),
            "projectId": getattr(grant, "project_id", None),
            "controllerId": getattr(grant, "controller_id", None),
            "renewalSequence": getattr(grant, "renewal_sequence", None),
            "coordinatorClockId": getattr(grant, "coordinator_clock_id", None),
            "coordinatorDeadlineNs": getattr(grant, "coordinator_deadline_ns", None),
            "localDeadlineNs": getattr(grant, "local_deadline_ns", None),
            "localDeadlineLatestNs": getattr(grant, "local_deadline_latest_ns", None),
            "mappingId": getattr(grant, "mapping_id", None),
            "hostClockId": getattr(grant, "host_clock_id", None),
            "hostBootDigest": getattr(grant, "host_boot_digest", None),
        }

    def issue_parent_grant(
        self,
        mapping,
        *,
        grant_id,
        project_id,
        controller_id,
        renewal_sequence,
        coordinator_deadline_ns,
    ):
        self._require_open()
        _identifier(grant_id, "parent grant identity")
        _identifier(project_id, "project identity")
        _identifier(controller_id, "controller identity")
        sequence = _integer(renewal_sequence, "renewal sequence", 1, MAX_SEQUENCE)
        deadline = _integer(coordinator_deadline_ns, "coordinator deadline")
        _require(type(mapping) is ClockMapping, "Trusted clock mapping required")
        sample = self.clock_sync.require_mapping(mapping)
        _require(deadline >= mapping.coordinator_receive_ns,
                 "Coordinator deadline predates its clock exchange")
        interval = mapping.translate(deadline)
        _require(
            interval.uncertainty_ns <= self.max_clock_uncertainty_ns,
            "Clock mapping uncertainty is excessive",
        )
        now = sample.nanoseconds + sample.uncertainty_ns
        _require(now < interval.earliest_ns, "Operation authority expired")
        fields = {
            "grantId": grant_id,
            "projectId": project_id,
            "controllerId": controller_id,
            "renewalSequence": sequence,
            "coordinatorClockId": mapping.coordinator_clock_id,
            "coordinatorDeadlineNs": deadline,
            "localDeadlineNs": interval.earliest_ns,
            "localDeadlineLatestNs": interval.latest_ns,
            "mappingId": mapping.mapping_id,
            "hostClockId": mapping.host_clock_id,
            "hostBootDigest": mapping.host_boot_digest,
        }
        return ParentGrant(
            grant_id=grant_id,
            project_id=project_id,
            controller_id=controller_id,
            renewal_sequence=sequence,
            coordinator_clock_id=mapping.coordinator_clock_id,
            coordinator_deadline_ns=deadline,
            local_deadline_ns=interval.earliest_ns,
            local_deadline_latest_ns=interval.latest_ns,
            mapping_id=mapping.mapping_id,
            host_clock_id=mapping.host_clock_id,
            host_boot_digest=mapping.host_boot_digest,
            grant_fingerprint=_canonical_digest(fields),
            _mapping=mapping,
            _issuer=self._issuer,
        )

    @staticmethod
    def _grant_values(grant):
        return {
            "grant_id": grant.grant_id,
            "project_id": grant.project_id,
            "controller_id": grant.controller_id,
            "renewal_sequence": grant.renewal_sequence,
            "local_deadline_ns": grant.local_deadline_ns,
            "mapping_id": grant.mapping_id,
        }

    def claim_device(
        self,
        *,
        device_kind,
        physical_id,
        helper_incarnation,
        parent_grant,
        display_alias=None,
    ):
        # Register the handle atomically with respect to host shutdown. No
        # provider callback occurs while this short authority lock is held.
        with self._lock:
            self._require_open()
            _identifier(helper_incarnation, "helper incarnation")
            _bounded_alias(display_alias)
            now = self._require_parent_grant(parent_grant)
            lease_identity, device_fingerprint = _canonical_device(device_kind, physical_id)
            lease = Lease(lease_identity, self.lease_directory,
                          authority_root=self.authority_root_digest)
            try:
                lease.__enter__()
            except ContractError:
                raise
            except OSError:
                raise ContractError("Canonical device lease unavailable") from None
            try:
                # Publish the fail-closed migration gate before durable
                # ownership can change.  If any later write fails, legacy v1
                # remains blocked after the kernel lock is released.
                lease.mark_authority("rollback-blocked")
                row = self.store.claim_device(
                    device_fingerprint=device_fingerprint,
                    device_kind=device_kind,
                    host_incarnation=self.host_incarnation,
                    helper_incarnation=helper_incarnation,
                    grant=self._grant_values(parent_grant),
                    now_ns=now,
                )
                lease.mark_authority(
                    "shared" if row["status"] == "owned" else "rollback-blocked"
                )
                active_grant = parent_grant if row["status"] == "owned" else None
                handle = DeviceAuthority(
                    authority=self,
                    lease=lease,
                    device_kind=device_kind,
                    device_fingerprint=device_fingerprint,
                    generation=row["generation"],
                    helper_incarnation=row["helper_incarnation"],
                    parent_grant=active_grant,
                )
                self._handles.add(handle)
                return handle
            except BaseException:
                lease.__exit__(None, None, None)
                raise
    def record_operation_disposition(
        self,
        snapshot,
        *,
        operation_id,
        terminal_status,
        result_digest,
        evidence_digest,
    ):
        """Record trusted reconciliation evidence, separately from provider results.

        ``recovered`` confirms a restored state while the original dispatched
        effect remains unknown. Its result digest describes that restoration;
        it never overwrites the original operation's result or its receipts.
        """
        self._require_open()
        _require(
            type(snapshot) is RecoverySnapshot and snapshot._issuer is self._issuer,
            "Trusted recovery snapshot required",
        )
        _identifier(operation_id, "operation identity")
        _require(terminal_status in ("not-dispatched", "succeeded", "rejected", "recovered"),
                 "Invalid operation disposition")
        _digest(result_digest, "reconciled result digest")
        _digest(evidence_digest, "operation disposition evidence digest")
        states = dict(snapshot.operations)
        _require(operation_id in states, "Unknown recovery operation")
        if states[operation_id] == "queued":
            _require(terminal_status == "not-dispatched",
                     "Queued operation disposition is invalid")
        else:
            _require(terminal_status in ("succeeded", "rejected", "recovered"),
                     "Uncertain operation needs a terminal disposition")
        fields = {
            "deviceFingerprint": snapshot.device_fingerprint,
            "priorGeneration": snapshot.prior_generation,
            "operationId": operation_id,
            "terminalStatus": terminal_status,
            "resultDigest": result_digest,
            "evidenceDigest": evidence_digest,
        }
        return TrustedOperationDisposition(
            snapshot.device_fingerprint,
            snapshot.prior_generation,
            operation_id,
            terminal_status,
            result_digest,
            evidence_digest,
            _canonical_digest(fields),
            self._issuer,
        )

    def record_reconciliation(
        self,
        snapshot,
        *,
        dispositions,
        prior_helper_exit_digest,
        pointer_cleanup_digest,
        fresh_helper_incarnation,
        fresh_handshake_digest,
    ):
        self._require_open()
        _require(
            type(snapshot) is RecoverySnapshot and snapshot._issuer is self._issuer,
            "Trusted recovery snapshot required",
        )
        _require(type(dispositions) in (list, tuple),
                 "Trusted operation dispositions required")
        values = tuple(dispositions)
        _require(
            all(
                type(item) is TrustedOperationDisposition
                and item._issuer is self._issuer
                and item.device_fingerprint == snapshot.device_fingerprint
                and item.prior_generation == snapshot.prior_generation
                for item in values
            ),
            "Trusted operation dispositions required",
        )
        for item in values:
            disposition_fields = {
                "deviceFingerprint": item.device_fingerprint,
                "priorGeneration": item.prior_generation,
                "operationId": item.operation_id,
                "terminalStatus": item.terminal_status,
                "resultDigest": item.result_digest,
                "evidenceDigest": item.evidence_digest,
            }
            _require(
                item.disposition_fingerprint == _canonical_digest(disposition_fields),
                "Trusted operation dispositions required",
            )
        _require(
            len(values) == len({item.operation_id for item in values}),
            "Duplicate operation disposition",
        )
        _digest(prior_helper_exit_digest, "prior helper exit evidence digest")
        _digest(pointer_cleanup_digest, "pointer cleanup evidence digest")
        _identifier(fresh_helper_incarnation, "fresh helper incarnation")
        _require(fresh_helper_incarnation != snapshot.prior_helper_incarnation,
                 "Fresh helper handshake required")
        _digest(fresh_handshake_digest, "fresh helper handshake evidence digest")
        reconciliation_id = "reconciliation_" + uuid.uuid4().hex
        fields = {
            "reconciliationId": reconciliation_id,
            "deviceFingerprint": snapshot.device_fingerprint,
            "priorGeneration": snapshot.prior_generation,
            "priorHostIncarnation": snapshot.prior_host_incarnation,
            "priorHelperIncarnation": snapshot.prior_helper_incarnation,
            "priorHelperExitDigest": prior_helper_exit_digest,
            "pointerCleanupDigest": pointer_cleanup_digest,
            "freshHelperIncarnation": fresh_helper_incarnation,
            "freshHandshakeDigest": fresh_handshake_digest,
            "dispositions": [item.disposition_fingerprint for item in values],
        }
        return TrustedReconciliation(
            reconciliation_id,
            _canonical_digest(fields),
            snapshot.device_fingerprint,
            snapshot.prior_generation,
            snapshot.prior_host_incarnation,
            snapshot.prior_helper_incarnation,
            prior_helper_exit_digest,
            pointer_cleanup_digest,
            fresh_helper_incarnation,
            fresh_handshake_digest,
            values,
            self._issuer,
        )

    def record_provider_result(
        self,
        *,
        operation_id,
        generation,
        host_incarnation,
        provider_incarnation,
        result,
    ):
        """Persist an asynchronous or late provider result.

        This is a trusted adapter API, not an RPC.  Exact generation, host and
        provider bindings decide whether the receipt is current or historical.
        """
        self._require_open()
        _identifier(operation_id, "operation identity")
        generation = _integer(generation, "ownership generation", 1, MAX_SEQUENCE)
        _identifier(host_incarnation, "host incarnation")
        _identifier(provider_incarnation, "provider incarnation")
        _require(type(result) is ProviderResult, "Bounded provider result required")
        sample = self.clock_sync.sample()
        fields = {
            "receiptId": result.receipt_id,
            "operationId": operation_id,
            "generation": generation,
            "hostIncarnation": host_incarnation,
            "providerIncarnation": provider_incarnation,
            "status": result.status,
            "resultDigest": result.result_digest,
        }
        return self.store.acknowledge_result(
            receipt_id=result.receipt_id,
            receipt_fingerprint=_canonical_digest(fields),
            operation_id=operation_id,
            generation=generation,
            host_incarnation=host_incarnation,
            provider_incarnation=provider_incarnation,
            status=result.status,
            result_digest=result.result_digest,
            observed_ns=sample.nanoseconds + sample.uncertainty_ns,
        )

    def record_legacy_adoption(self, *, adoption_id, artifact_digest, semantics):
        self._require_open()
        _identifier(adoption_id, "legacy adoption identity")
        _digest(artifact_digest, "legacy artifact digest")
        _require(semantics in ("lock-only", "unverified-history"),
                 "Invalid legacy adoption semantics")
        return self.store.record_legacy_adoption(
            adoption_id=adoption_id,
            artifact_digest=artifact_digest,
            semantics=semantics,
        )

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles = list(self._handles)
        failed = False
        for handle in handles:
            try:
                handle.close()
            except Exception:
                failed = True
        self.store.close()
        _require(not failed, "Authority cleanup is uncertain")


def issue_local_parent_grant(authority, *, lifetime_ns, grant_id=None):
    """Issue one fixed-deadline grant for the trusted offline local service.

    This is intentionally not a renewal loop. A service must be restarted or
    supplied a newer trusted coordinator grant after the fixed ceiling.
    """
    _require(type(authority) is HostAuthority, "Trusted host authority required")
    lifetime = _integer(lifetime_ns, "local grant lifetime", 1, 7_200_000_000_000)
    received = authority.clock_sync.sample()
    sent = authority.clock_sync.sample()
    mapping = authority.clock_sync.record_exchange(
        coordinator_clock_id="local-coordinator",
        coordinator_send_ns=received.nanoseconds,
        host_received=received,
        host_sent=sent,
        coordinator_receive_ns=sent.nanoseconds,
        max_drift_ppm=0,
    )
    return authority.issue_parent_grant(
        mapping,
        grant_id=grant_id or ("grant_" + uuid.uuid4().hex),
        project_id="legacy-local",
        controller_id="local-service",
        renewal_sequence=1,
        coordinator_deadline_ns=sent.nanoseconds + lifetime,
    )


class DeviceAuthority:
    """The only live owner of one canonical device lock and generation."""

    def __init__(
        self,
        *,
        authority,
        lease,
        device_kind,
        device_fingerprint,
        generation,
        helper_incarnation,
        parent_grant,
    ):
        self._authority = authority
        self._lease = lease
        self.device_kind = device_kind
        self._device_fingerprint = device_fingerprint
        self.generation = generation
        self.helper_incarnation = helper_incarnation
        self._parent_grant = parent_grant
        self._issuer = object()
        self._operation_cache = OrderedDict()
        self._lock = threading.RLock()
        self._closed = False
        self._dispatch_revoked = False
        self._native_helper_attached = False
        self._native_cleanup_confirmed = False
        self._released = False
        self._native_recovery_borrows = {}

    def _require_open(self):
        _require(not self._closed and self._lease.file is not None,
                 "Device authority is closed")
        self._authority._require_open()

    @property
    def status(self):
        self._require_open()
        row = self._authority.store.device(self._device_fingerprint)
        _require(row is not None, "Device authority is unavailable")
        return row["status"]

    @property
    def requires_reconciliation(self):
        return self.status == "quarantined"

    @property
    def parent_deadline_ns(self):
        self._require_open()
        row = self._authority.store.device(self._device_fingerprint)
        _require(row is not None, "Device authority is unavailable")
        return row["parent_deadline_ns"]

    @property
    def cached_operation_count(self):
        return len(self._operation_cache)

    def _now(self):
        self._require_open()
        _require(self._parent_grant is not None, "Device requires reconciliation")
        try:
            sample = self._authority.clock_sync.require_mapping(
                self._parent_grant._mapping
            )
        except ContractError:
            now_ns = None
            try:
                sample = self._authority.clock_sync.sample()
                now_ns = sample.nanoseconds + sample.uncertainty_ns
            except ContractError:
                pass
            self._authority.store.quarantine_clock(
                device_fingerprint=self._device_fingerprint,
                generation=self.generation,
                host_incarnation=self._authority.host_incarnation,
                now_ns=now_ns,
            )
            raise ContractError("Clock mapping is incompatible") from None
        return sample.nanoseconds + sample.uncertainty_ns

    def _remember(self, admission):
        self._operation_cache[admission.operation_id] = admission
        self._operation_cache.move_to_end(admission.operation_id)
        while len(self._operation_cache) > self._authority.operation_cache_size:
            self._operation_cache.popitem(last=False)

    def _admission_from_row(self, row, *, is_new):
        return OperationAdmission(
            protocol_version=row["protocol_version"],
            operation_id=row["operation_id"],
            operation_fingerprint=row["operation_fingerprint"],
            payload_digest=row["payload_digest"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            controller_id=row["controller_id"],
            sequence=row["sequence"],
            generation=row["generation"],
            host_incarnation=row["host_incarnation"],
            helper_incarnation=row["helper_incarnation"],
            deadline_ns=row["deadline_ns"],
            status=row["status"],
            result_digest=row["result_digest"],
            is_new=is_new,
            _device_fingerprint=self._device_fingerprint,
            _issuer=self._issuer,
        )

    def borrowed_lease(self):
        self._require_open()
        return _BorrowedLease(self)

    @contextmanager
    def borrow_native_lease(self):
        """Retain an existing native lock; this grants no new device effect."""
        with ExitStack() as stack:
            with self._lock:
                self._require_open()
                row = self._authority.store.device(self._device_fingerprint)
                _require(row is not None and row['status'] in {'owned', 'quarantined'}
                    and row['generation'] == self.generation
                    and row['host_incarnation'] == self._authority.host_incarnation
                    and row['helper_incarnation'] == self.helper_incarnation,
                    'Original device ownership required')
                borrowed = stack.enter_context(self._lease.borrow_descriptor())
            yield borrowed

    def _require_recovery_snapshot(self, snapshot, parent_grant):
        self._require_open()
        self._authority._require_parent_grant(parent_grant)
        _require(type(snapshot) is RecoverySnapshot
            and snapshot._issuer is self._authority._issuer
            and snapshot.device_fingerprint == self._device_fingerprint
            and snapshot.prior_generation == self.generation
            and self.requires_reconciliation
            and snapshot == self.recovery_snapshot(), 'Current device recovery snapshot required')

    @contextmanager
    def borrow_native_recovery_lease(self, snapshot, *, parent_grant):
        """Retain a quarantined lease under a fresh grant, without device effects."""
        borrowed = None
        with ExitStack() as stack:
            try:
                with self._lock:
                    self._require_recovery_snapshot(snapshot, parent_grant)
                    _require(not self._native_recovery_borrows, 'Device recovery owner is already active')
                    descriptor, directory, name = stack.enter_context(self._lease.borrow_descriptor())
                    self._authority.store.fence_native_recovery(
                        device_fingerprint=self._device_fingerprint, generation=snapshot.prior_generation,
                        host_incarnation=snapshot.prior_host_incarnation,
                        helper_incarnation=snapshot.prior_helper_incarnation,
                        now_ns=self._authority._require_parent_grant(parent_grant))
                    self._dispatch_revoked = True
                    snapshot = self.recovery_snapshot()
                    borrowed = NativeRecoveryLease(self._device_fingerprint, self.generation,
                        snapshot.prior_host_incarnation, snapshot.prior_helper_incarnation,
                        descriptor, directory, name, snapshot, parent_grant,
                        os.getpid(), threading.get_ident())
                    self._native_recovery_borrows[id(borrowed)] = borrowed
                    self.require_native_recovery_lease(borrowed)
                yield borrowed
            finally:
                if borrowed is not None:
                    with self._lock:
                        borrowed._active = False
                        self._native_recovery_borrows.pop(id(borrowed), None)

    def require_native_recovery_lease(self, borrowed):
        with self._lock:
            _require(type(borrowed) is NativeRecoveryLease
                and self._native_recovery_borrows.get(id(borrowed)) is borrowed and borrowed._active
                and borrowed._pid == os.getpid() and borrowed._thread == threading.get_ident(),
                'Live device recovery lease required')
            self._require_recovery_snapshot(borrowed._snapshot, borrowed._grant)
            original = os.fstat(self._lease.file.fileno())
            opened = os.fstat(borrowed.descriptor)
            _require((original.st_dev, original.st_ino) == (opened.st_dev, opened.st_ino)
                and borrowed.device_fingerprint == self._device_fingerprint
                and borrowed.prior_generation == self.generation
                and borrowed.prior_host_incarnation == borrowed._snapshot.prior_host_incarnation
                and borrowed.prior_helper_incarnation == borrowed._snapshot.prior_helper_incarnation,
                'Original device recovery lease required')
            return borrowed

    def check_ownership(self):
        """Check the existing grant before a reserved device starts a provider."""
        with self._lock:
            self._require_open()
            now = self._authority._require_parent_grant(self._parent_grant)
            row = self._authority.store.device(self._device_fingerprint)
            _require(row is not None and row["status"] == "owned"
                     and row["generation"] == self.generation
                     and row["host_incarnation"] == self._authority.host_incarnation
                     and row["helper_incarnation"] == self.helper_incarnation
                     and now < row["parent_deadline_ns"],
                     "Device ownership is unavailable")

    def check_observation_authority(self):
        """Check passive capture while an already permitted effect is in flight.

        Dispatch deliberately marks ownership unconfirmed until its receipt.
        Observing that operation must remain possible; this check never creates
        a dispatch permit or resolves an unconfirmed outcome.
        """
        with self._lock:
            self._require_open()
            _require(not self._dispatch_revoked, "Observation authority was revoked")
            now = self._authority._require_parent_grant(self._parent_grant)
            row = self._authority.store.device(self._device_fingerprint)
            _require(row is not None
                     and (row["status"] == "owned" or
                          (row["status"] == "quarantined" and
                           row["quarantine_reason"] == "provider-outcome-unconfirmed"))
                     and row["generation"] == self.generation
                     and row["host_incarnation"] == self._authority.host_incarnation
                     and row["helper_incarnation"] == self.helper_incarnation
                     and now < row["parent_deadline_ns"],
                     "Observation authority is unavailable")

    def renew(self, parent_grant):
        with self._lock:
            self._require_open()
            _require(type(parent_grant) is ParentGrant
                     and parent_grant._issuer is self._authority._issuer,
                     "Trusted parent grant required")
            self._now()
            now = self._authority._require_parent_grant(parent_grant)
            row = self._authority.store.renew_device(
                device_fingerprint=self._device_fingerprint,
                generation=self.generation,
                host_incarnation=self._authority.host_incarnation,
                grant=self._authority._grant_values(parent_grant),
                now_ns=now,
            )
            self._parent_grant = parent_grant
            return row["parent_deadline_ns"]

    def admit_operation(
        self,
        *,
        operation_id,
        payload_digest,
        session_id,
        sequence,
        deadline_ns=None,
    ):
        with self._lock:
            self._require_open()
            _identifier(operation_id, "operation identity")
            _digest(payload_digest, "operation payload digest")
            _identifier(session_id, "session identity")
            sequence = _integer(sequence, "operation sequence", 1, MAX_SEQUENCE)
            parent = self._parent_grant
            _require(parent is not None, "Device requires reconciliation")
            existing = self._authority.store.operation(operation_id)
            if existing is not None:
                _require(
                    existing["device_fingerprint"] == self._device_fingerprint
                    and existing["protocol_version"] == PROTOCOL_VERSION
                    and existing["generation"] == self.generation
                    and existing["project_id"] == parent.project_id
                    and existing["session_id"] == session_id
                    and existing["controller_id"] == parent.controller_id
                    and existing["sequence"] == sequence
                    and existing["payload_digest"] == payload_digest
                    and existing["host_incarnation"] == self._authority.host_incarnation
                    and existing["helper_incarnation"] == self.helper_incarnation,
                    "Operation identity conflict",
                )
                admission = self._admission_from_row(existing, is_new=False)
                self._remember(admission)
                return admission
            _require(self.status == "owned", "Device requires reconciliation")
            now = self._now()
            requested_deadline = (
                parent.local_deadline_ns
                if deadline_ns is None
                else _integer(deadline_ns, "operation deadline")
            )
            local_deadline = min(requested_deadline, parent.local_deadline_ns)
            _require(now < local_deadline, "Operation authority expired")
            fields = {
                "protocolVersion": PROTOCOL_VERSION,
                "deviceFingerprint": self._device_fingerprint,
                "operationId": operation_id,
                "payloadDigest": payload_digest,
                "projectId": parent.project_id,
                "sessionId": session_id,
                "controllerId": parent.controller_id,
                "ownershipGeneration": self.generation,
                "hostIncarnation": self._authority.host_incarnation,
                "helperIncarnation": self.helper_incarnation,
                "deadlineNs": local_deadline,
                "sequence": sequence,
            }
            fingerprint = _canonical_digest(fields)
            row = self._authority.store.admit_operation({
                "operation_id": operation_id,
                "operation_fingerprint": fingerprint,
                "protocol_version": PROTOCOL_VERSION,
                "payload_digest": payload_digest,
                "project_id": parent.project_id,
                "session_id": session_id,
                "controller_id": parent.controller_id,
                "sequence": sequence,
                "generation": self.generation,
                "host_incarnation": self._authority.host_incarnation,
                "helper_incarnation": self.helper_incarnation,
                "deadline_ns": local_deadline,
                "admitted_ns": now,
                "device_fingerprint": self._device_fingerprint,
            })
            admission = self._admission_from_row(row, is_new=row["is_new"])
            self._remember(admission)
            return admission

    def prepare_dispatch(self, admission, *, provider_incarnation):
        with self._lock:
            self._require_open()
            _require(
                type(admission) is OperationAdmission
                and admission._issuer is self._issuer
                and admission._device_fingerprint == self._device_fingerprint
                and admission.generation == self.generation
                and admission.host_incarnation == self._authority.host_incarnation,
                "Trusted operation admission required",
            )
            _identifier(provider_incarnation, "provider incarnation")
            now = self._now()
            parent_deadline = self._parent_grant.local_deadline_ns
            if now >= admission.deadline_ns or now >= parent_deadline:
                self._authority.store.expire_queued_operation(
                    device_fingerprint=self._device_fingerprint,
                    generation=self.generation,
                    host_incarnation=self._authority.host_incarnation,
                    operation_id=admission.operation_id,
                    now_ns=now,
                )
                raise ContractError("Operation authority expired")
            row = self._authority.store.prepare_dispatch(
                operation_id=admission.operation_id,
                operation_fingerprint=admission.operation_fingerprint,
                device_fingerprint=self._device_fingerprint,
                generation=self.generation,
                host_incarnation=self._authority.host_incarnation,
                provider_incarnation=provider_incarnation,
                now_ns=now,
            )
            return DispatchPermit(
                protocol_version=PROTOCOL_VERSION,
                operation_id=row["operation_id"],
                operation_fingerprint=row["operation_fingerprint"],
                payload_digest=row["payload_digest"],
                project_id=row["project_id"],
                session_id=row["session_id"],
                controller_id=row["controller_id"],
                sequence=row["sequence"],
                ownership_generation=row["generation"],
                host_incarnation=row["host_incarnation"],
                helper_incarnation=self.helper_incarnation,
                provider_incarnation=provider_incarnation,
                deadline_ns=min(row["deadline_ns"], parent_deadline),
                _device_fingerprint=self._device_fingerprint,
                _issuer=self._issuer,
            )

    def check_dispatch_permit(self, permit):
        """Recheck a prepared operation immediately before a host/native effect."""
        with self._lock:
            self._require_open()
            _require(not self._dispatch_revoked, "Dispatch authority was revoked")
            _require(
                type(permit) is DispatchPermit
                and permit._issuer is self._issuer
                and permit._device_fingerprint == self._device_fingerprint
                and permit.ownership_generation == self.generation
                and permit.host_incarnation == self._authority.host_incarnation
                and permit.helper_incarnation == self.helper_incarnation,
                "Trusted dispatch permit required",
            )
            now = self._now()
            row = self._authority.store.operation(permit.operation_id)
            _require(
                row is not None
                and row["operation_fingerprint"] == permit.operation_fingerprint
                and row["status"] == "uncertain"
                and row["provider_incarnation"] == permit.provider_incarnation,
                "Dispatch permit is not current",
            )
            _require(now < permit.deadline_ns
                     and now < self._parent_grant.local_deadline_ns,
                     "Operation authority expired")
            return now

    def revoke_dispatches(self):
        """Fence all issued permits after a host session loses safe ownership."""
        with self._lock:
            self._require_open()
            self._dispatch_revoked = True
            now_ns = None
            try:
                sample = self._authority.clock_sync.sample()
                now_ns = sample.nanoseconds + sample.uncertainty_ns
            except ContractError:
                # Revocation remains durable even when the clock that caused
                # the failure cannot supply a new journal timestamp.
                pass
            self._authority.store.revoke_dispatches(
                device_fingerprint=self._device_fingerprint,
                generation=self.generation,
                host_incarnation=self._authority.host_incarnation,
                now_ns=now_ns,
            )

    def bind_native_handshake(
        self,
        permit,
        *,
        protocol_version,
        helper_version,
        helper_incarnation,
        provider_incarnation,
        native_incarnation,
        native_clock_id,
        native_time_ms,
    ):
        """Create a process-local capability from a checked helper status reply."""
        host_received = self.check_dispatch_permit(permit)
        _require(type(protocol_version) is int
                 and type(helper_version) is int
                 and protocol_version == NATIVE_PROTOCOL_VERSION
                 and helper_version == HELPER_VERSION,
                 "Native helper protocol is incompatible")
        _require(helper_incarnation == permit.helper_incarnation
                 and provider_incarnation == permit.provider_incarnation,
                 "Native helper incarnation is incompatible")
        _identifier(native_incarnation, "native incarnation")
        _identifier(native_clock_id, "native clock identity")
        native_time_ms = _integer(native_time_ms, "native clock value", 0, MAX_NS // 1_000_000)
        qualification = QUALIFIED_NATIVE_CLOCKS.get((self.device_kind, native_clock_id))
        _require(qualification is not None, "Native clock is not qualified")
        with self._lock:
            self.check_dispatch_permit(permit)
            self._native_helper_attached = True
            self._native_cleanup_confirmed = False
        return NativeHandshake(
            protocol_version, helper_version, helper_incarnation,
            provider_incarnation, native_incarnation, native_clock_id,
            native_time_ms, host_received,
            qualification["maxRateErrorPpm"], qualification["mappingUncertaintyMs"],
            self._device_fingerprint, self._issuer,
        )

    def native_grant(self, permit, handshake):
        """Translate a host permit to an equal-or-earlier native absolute deadline."""
        self.check_dispatch_permit(permit)
        _require(
            type(handshake) is NativeHandshake
            and handshake._issuer is self._issuer
            and handshake._device_fingerprint == self._device_fingerprint
            and handshake.protocol_version == NATIVE_PROTOCOL_VERSION
            and handshake.helper_version == HELPER_VERSION
            and handshake.helper_incarnation == permit.helper_incarnation
            and handshake.provider_incarnation == permit.provider_incarnation,
            "Trusted native handshake required",
        )
        remaining_ms = max(0, permit.deadline_ns - handshake.host_received_ns) // 1_000_000
        # Convert duration using the slowest qualified native rate, then
        # subtract the bounded wire/quantization uncertainty.  The native
        # deadline therefore cannot outlive the host permit even though the
        # absolute clock epochs differ.
        native_duration = (
            remaining_ms * (1_000_000 - handshake.max_rate_error_ppm)
        ) // 1_000_000
        _require(native_duration > handshake.mapping_uncertainty_ms,
                 "Native clock mapping is too uncertain")
        native_deadline = (
            handshake.native_time_ms + native_duration
            - handshake.mapping_uncertainty_ms
        )
        _integer(native_deadline, "native deadline", 0, MAX_NS // 1_000_000)
        return NativeGrant(
            NATIVE_PROTOCOL_VERSION, permit.operation_id,
            permit.operation_fingerprint, permit.payload_digest,
            permit.project_id, permit.session_id, permit.controller_id,
            permit.sequence, permit.ownership_generation, permit.host_incarnation,
            permit.helper_incarnation, permit.provider_incarnation,
            handshake.native_incarnation, handshake.native_clock_id,
            native_deadline, self._issuer,
        )

    def confirm_operation(self, permit, result):
        self._require_open()
        _require(
            type(permit) is DispatchPermit
            and permit._issuer is self._issuer
            and permit._device_fingerprint == self._device_fingerprint,
            "Trusted dispatch permit required",
        )
        _require(type(result) is ProviderResult, "Bounded provider result required")
        self._now()
        receipt = self._authority.record_provider_result(
            operation_id=permit.operation_id,
            generation=permit.ownership_generation,
            host_incarnation=permit.host_incarnation,
            provider_incarnation=permit.provider_incarnation,
            result=result,
        )
        return ProviderResult(receipt["receipt_id"], receipt["status"], receipt["result_digest"])

    def confirm_native_cleanup(self, permit):
        """Bind a successful helper/pointer cleanup before releasing ownership."""
        with self._lock:
            self._require_open()
            _require(not self._dispatch_revoked, "Revoked native ownership requires reconciliation")
            _require(
                type(permit) is DispatchPermit
                and permit._issuer is self._issuer
                and permit._device_fingerprint == self._device_fingerprint
                and permit.ownership_generation == self.generation
                and permit.host_incarnation == self._authority.host_incarnation
                and permit.helper_incarnation == self.helper_incarnation
                and permit.payload_digest == _canonical_digest({
                    "kind": "cleanup", "payload": {"scope": "native-helper-and-pointers"}
                }),
                "Trusted native cleanup permit required",
            )
            operation = self._authority.store.operation(permit.operation_id)
            _require(
                operation is not None
                and operation["operation_fingerprint"] == permit.operation_fingerprint
                and operation["provider_incarnation"] == permit.provider_incarnation
                and operation["status"] == "succeeded"
                and self.status in {"owned", "expired"},
                "Native cleanup is not confirmed",
            )
            self._native_cleanup_confirmed = True

    def dispatch_operation(self, admission, *, provider_incarnation, callback):
        _require(callable(callback), "Provider callback is required")
        permit = self.prepare_dispatch(
            admission, provider_incarnation=provider_incarnation
        )
        # No handle mutex or SQLite transaction is held across provider code.
        try:
            result = callback(permit)
        except Exception:
            raise ContractError("Provider outcome is uncertain") from None
        _require(type(result) is ProviderResult, "Provider outcome is uncertain")
        return self.confirm_operation(permit, result)

    def recovery_snapshot(self):
        with self._lock:
            self._require_open()
            device, operations = self._authority.store.recovery_state(
                self._device_fingerprint
            )
            return RecoverySnapshot(
                device_fingerprint=self._device_fingerprint,
                prior_generation=device["generation"],
                prior_host_incarnation=device["host_incarnation"],
                prior_helper_incarnation=device["helper_incarnation"],
                quarantine_reason=device["quarantine_reason"],
                operations=tuple(
                    (operation["operation_id"], operation["status"])
                    for operation in operations
                ),
                _issuer=self._authority._issuer,
            )

    def reconcile(self, reconciliation, *, parent_grant, cleanup_pending=False):
        with self._lock:
            self._require_open()
            _require(type(cleanup_pending) is bool, "Invalid reconciliation cleanup state")
            _require(not self._native_recovery_borrows, 'Collect the native recovery owner before reconciliation')
            _require(
                type(reconciliation) is TrustedReconciliation
                and reconciliation._issuer is self._authority._issuer
                and reconciliation.device_fingerprint == self._device_fingerprint
                and reconciliation.prior_generation == self.generation,
                "Trusted reconciliation required",
            )
            for item in reconciliation.dispositions:
                disposition_fields = {
                    "deviceFingerprint": item.device_fingerprint,
                    "priorGeneration": item.prior_generation,
                    "operationId": item.operation_id,
                    "terminalStatus": item.terminal_status,
                    "resultDigest": item.result_digest,
                    "evidenceDigest": item.evidence_digest,
                }
                _require(
                    type(item) is TrustedOperationDisposition
                    and item._issuer is self._authority._issuer
                    and item.device_fingerprint == self._device_fingerprint
                    and item.prior_generation == self.generation
                    and item.disposition_fingerprint
                    == _canonical_digest(disposition_fields),
                    "Trusted reconciliation required",
                )
            reconciliation_fields = {
                "reconciliationId": reconciliation.reconciliation_id,
                "deviceFingerprint": reconciliation.device_fingerprint,
                "priorGeneration": reconciliation.prior_generation,
                "priorHostIncarnation": reconciliation.prior_host_incarnation,
                "priorHelperIncarnation": reconciliation.prior_helper_incarnation,
                "priorHelperExitDigest": reconciliation.prior_helper_exit_digest,
                "pointerCleanupDigest": reconciliation.pointer_cleanup_digest,
                "freshHelperIncarnation": reconciliation.fresh_helper_incarnation,
                "freshHandshakeDigest": reconciliation.fresh_handshake_digest,
                "dispositions": [
                    item.disposition_fingerprint for item in reconciliation.dispositions
                ],
            }
            _require(
                reconciliation.reconciliation_fingerprint
                == _canonical_digest(reconciliation_fields),
                "Trusted reconciliation required",
            )
            now = self._authority._require_parent_grant(parent_grant)
            disposition_values = {
                item.operation_id: {
                    "terminal_status": item.terminal_status,
                    "result_digest": item.result_digest,
                    "evidence_digest": item.evidence_digest,
                    "disposition_fingerprint": item.disposition_fingerprint,
                }
                for item in reconciliation.dispositions
            }
            row = self._authority.store.reconcile_device(
                reconciliation={
                    "reconciliation_id": reconciliation.reconciliation_id,
                    "reconciliation_fingerprint": reconciliation.reconciliation_fingerprint,
                    "device_fingerprint": reconciliation.device_fingerprint,
                    "prior_generation": reconciliation.prior_generation,
                    "prior_host_incarnation": reconciliation.prior_host_incarnation,
                    "prior_helper_incarnation": reconciliation.prior_helper_incarnation,
                    "prior_helper_exit_digest": reconciliation.prior_helper_exit_digest,
                    "pointer_cleanup_digest": reconciliation.pointer_cleanup_digest,
                    "fresh_handshake_digest": reconciliation.fresh_handshake_digest,
                },
                dispositions=disposition_values,
                host_incarnation=self._authority.host_incarnation,
                fresh_helper_incarnation=reconciliation.fresh_helper_incarnation,
                grant=self._authority._grant_values(parent_grant),
                now_ns=now,
                cleanup_pending=cleanup_pending,
            )
            self.generation = row["generation"]
            self.helper_incarnation = row["helper_incarnation"]
            self._parent_grant = parent_grant
            self._operation_cache.clear()
            self._dispatch_revoked = cleanup_pending
            self._native_helper_attached = False
            self._native_cleanup_confirmed = False
            self._lease.mark_authority("shared")
            return self

    def close(self):
        with self._lock:
            if self._closed:
                return self._released
            released = False
            now_ns = None
            try:
                sample = self._authority.clock_sync.sample()
                now_ns = sample.nanoseconds + sample.uncertainty_ns
            except ContractError:
                pass
            try:
                if self._native_helper_attached and not self._native_cleanup_confirmed:
                    self._dispatch_revoked = True
                    self._authority.store.revoke_dispatches(
                        device_fingerprint=self._device_fingerprint,
                        generation=self.generation,
                        host_incarnation=self._authority.host_incarnation,
                        now_ns=now_ns,
                    )
                elif now_ns is not None:
                    self._authority.store.release_device(
                        device_fingerprint=self._device_fingerprint,
                        generation=self.generation,
                        host_incarnation=self._authority.host_incarnation,
                        now_ns=now_ns,
                    )
                row = self._authority.store.device(self._device_fingerprint)
                released = row is not None and row["status"] == "released"
            except ContractError:
                # Failing to journal a clean release must not clear durable
                # ownership.  Releasing the kernel lock then forces recovery.
                pass
            try:
                self._lease.mark_authority(
                    "legacy-allowed" if released else "rollback-blocked"
                )
            except ContractError:
                released = False
            self._lease.__exit__(None, None, None)
            self._closed = True
            self._released = released
        with self._authority._lock:
            self._authority._handles.discard(self)
        return released


__all__ = [
    "DeviceAuthority",
    "DispatchPermit",
    "NativeGrant",
    "NativeHandshake",
    "HostAuthority",
    "OperationAdmission",
    "ParentGrant",
    "PROTOCOL_VERSION",
    "NATIVE_PROTOCOL_VERSION",
    "CODE_VERSION",
    "HELPER_VERSION",
    "COMPATIBILITY_TABLE",
    "issue_local_parent_grant",
    "ProviderResult",
    "RecoverySnapshot",
    "TrustedOperationDisposition",
    "TrustedReconciliation",
]
