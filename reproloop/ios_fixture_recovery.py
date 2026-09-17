"""Finalize fixture reservations for one live native iOS recovery lease."""
from dataclasses import dataclass, field
import json
import os
import threading
import time
import weakref

from . import contracts
from .ios_mobile_inputs import IOSMobileInputsConfig
from .ios_native_recovery import IOSNativeRecoveryContext
from .live.issue_sessions import (
    FIXTURE_RESERVATION_VERSION,
    IssueSessionError,
    fixture_reservation_id,
    _operation,
)


class IOSFixtureRecoveryError(RuntimeError):
    def __init__(self, code="ios_fixture_recovery_unavailable"):
        self.code = code
        super().__init__(code)


def _require(value, code="ios_fixture_recovery_unavailable"):
    if not value:
        raise IOSFixtureRecoveryError(code)


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class IOSFixtureRecoveryObservation:
    context_digest: str
    evidence_digest: str
    allocations: int
    completed: int
    selection_digest: str
    results_digest: str
    _context: object = field(repr=False, compare=False)
    _document: str = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


_ISSUER = object()
_LOCK = threading.RLock()
_LIVE = weakref.WeakValueDictionary()


def _bounds(context, cancellation, deadline):
    _require(callable(getattr(cancellation, "is_set", None)))
    _require(type(deadline) in (int, float) and time.monotonic() < deadline)
    _require(not cancellation.is_set())
    context.require()


def _phase_records(context):
    try:
        phases = os.open("phases", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         dir_fd=context.directory)
    except FileNotFoundError:
        return ()
    try:
        names = sorted(name for name in os.listdir(phases) if name.startswith("replay-"))
        _require(len(names) <= 3, "ios_fixture_recovery_journal")
        result = []
        for name in names:
            _require(len(name) == 10 and name[7:].isdigit(), "ios_fixture_recovery_journal")
            number = int(name[7:])
            _require(1 <= number <= 3, "ios_fixture_recovery_journal")
            directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=phases)
            try:
                import json
                from .repair_android_operation import _read_json_at
                intent = _read_json_at(directory, "intent.json")
                state = _read_json_at(directory, "state.json")
            finally:
                os.close(directory)
            _require(type(intent) is dict and type(state) is dict
                     and intent.get("phase") == "replay"
                     and intent.get("iteration") == number
                     and state.get("phase") == "replay"
                     and state.get("iteration") == number
                     and intent.get("state") == "running"
                     and state.get("state") in {"running", "completed", "failed"},
                     "ios_fixture_recovery_journal")
            result.append((number, state["state"]))
        _require([number for number, _ in result] == list(range(1, len(result) + 1)),
                 "ios_fixture_recovery_journal")
        return tuple(result)
    finally:
        os.close(phases)


def _issue_binding(issue, issue_id, config, build, plans):
    _require(type(issue) is dict and issue.get("issueId") == issue_id,
             "ios_fixture_recovery_issue")
    _require(issue.get("projectDigest") == config.registration.project_digest
             and issue.get("deviceId") == config.device_id
             and issue.get("applicationId") == config.application_id
             and issue.get("buildId") == build
             and issue.get("state") in {"complete", "failed", "quarantined"},
             "ios_fixture_recovery_issue")
    _require(type(issue.get("fixtures")) is list and len(issue["fixtures"]) <= len(plans),
             "ios_fixture_recovery_issue")
    planned = issue.get("fixtureReservations")
    if planned is not None:
        _require(issue.get("fixtureReservationVersion") == FIXTURE_RESERVATION_VERSION
                 and type(planned) is list and len(planned) == len(plans),
                 "ios_fixture_recovery_issue")
        _require(planned == [
            {"fixtureId": name, "allocationId": fixture_reservation_id(issue_id, name)}
            for name in plans
        ], "ios_fixture_recovery_issue")
    return planned is not None


def _lookup_issue(service, issue_id):
    """Distinguish a real pre-admission absence from every unsafe failure."""
    with service._lock:
        _require(issue_id not in service._active,
                 "ios_fixture_recovery_issue_active")
        try:
            return service.get(issue_id)
        except IssueSessionError as error:
            _require(error.code == "not_found", "ios_fixture_recovery_issue")
            return None


def _result_receipt(selection, result):
    common = {"status", "allocationId", "generation", "historyOnly", "evidenceDigest"}
    _require(
        type(result) is dict
        and set(result) in (common, common | {"unstarted"})
        and result.get("status") == "complete"
        and result.get("allocationId") == selection["allocationId"]
        and result.get("generation") == selection["generation"]
        and type(result.get("historyOnly")) is bool
        and ("unstarted" not in result or type(result["unstarted"]) is bool),
        "ios_fixture_recovery_unconfirmed",
    )
    contracts.validate_digest(result["evidenceDigest"])
    return {
        **selection,
        "status": result["status"],
        "historyOnly": result["historyOnly"],
        "unstarted": result.get("unstarted", False),
        "evidenceDigest": result["evidenceDigest"],
    }


def recover_ios_fixtures(context, config, *, cancellation, deadline_monotonic):
    """Recover every replay fixture through its live coordinator binding.

    The native context is an opaque capability. This function only inspects its
    immutable operation identity and uses the live service/coordinator objects
    from the exact registered config.
    """
    try:
        _require(type(context) is IOSNativeRecoveryContext
                 and type(config) is IOSMobileInputsConfig)
        context.require()
        _require(config.definition == context._operations.definition
                 and config.scope_digest == context.scope_digest
                 and config.service.lab is config.lab
                 and config.registration.project_digest == context.intent["context"]["project_digest"]
                 and config.application_id == context.intent["context"]["application_id"]
                 and config.scope_digest == context.intent["context"]["scope_digest"]
                 and config.runtime_policy_digest == context.intent["context"]["runtime_policy_digest"]
                 and context.native["scopeDigest"] == config.scope_digest
                 and context._device._device_fingerprint == config.scope_digest,
                 "ios_fixture_recovery_binding")
        config.service._checked_preparations(
            config.preparations, config.registration, config.application_id)
        _bounds(context, cancellation, deadline_monotonic)
        service = config.service
        coordinator = service.fixtures
        plans = {item.plan.fixture_id: item for item in config.preparations}
        _require(1 <= len(plans) <= 128, "ios_fixture_recovery_configuration")
        build = "candidate_" + contracts.digest({
            "operation": context.operation_id, "request": context.request_digest})[:32]
        selections = []
        for number, phase_state in _phase_records(context):
            _bounds(context, cancellation, deadline_monotonic)
            issue_id = "mobile_" + contracts.digest({
                "context": context.context_digest, "attempt": number})[:40]
            issue = _lookup_issue(service, issue_id)
            if issue is not None:
                planned = _issue_binding(issue, issue_id, config, build, plans)
                seen = set()
                for entry in issue["fixtures"]:
                    _require(type(entry) is dict and entry.get("fixtureId") in plans
                             and entry["fixtureId"] not in seen and type(entry.get("generation")) is int
                             and entry["generation"] > 0, "ios_fixture_recovery_issue")
                    allocation_id = fixture_reservation_id(issue_id, entry["fixtureId"])
                    _require(entry.get("allocationId") == allocation_id,
                             "ios_fixture_recovery_issue")
                    selections.append({
                        "issueId": issue_id,
                        "fixtureId": entry["fixtureId"],
                        "allocationId": allocation_id,
                        "generation": entry["generation"],
                        "attempt": number,
                    })
                    seen.add(entry["fixtureId"])
                for name, preparation in plans.items():
                    if name in seen:
                        continue
                    # Only the versioned intent written before reservation
                    # admission proves that this reference may be absent.
                    _require(planned, "ios_fixture_recovery_issue")
                    allocation_id = fixture_reservation_id(issue_id, name)
                    found = coordinator.lookup_recovery_allocation(
                        preparation.plan, allocation_id=allocation_id,
                        owner=config.owner, device_id=config.device_id)
                    selections.append({
                        "issueId": issue_id,
                        "fixtureId": name,
                        "allocationId": allocation_id,
                        "generation": 0 if found is None else found["generation"],
                        "attempt": number,
                    })
            else:
                _require(phase_state == "running", "ios_fixture_recovery_issue")
                for name in plans:
                    allocation_id = fixture_reservation_id(issue_id, name)
                    found = coordinator.lookup_recovery_allocation(
                        plans[name].plan, allocation_id=allocation_id,
                        owner=config.owner, device_id=config.device_id)
                    selections.append({
                        "issueId": issue_id,
                        "fixtureId": name,
                        "allocationId": allocation_id,
                        "generation": 0 if found is None else found["generation"],
                        "attempt": number,
                    })

        receipts = []
        for selection in selections:
            _bounds(context, cancellation, deadline_monotonic)
            issue_id = selection["issueId"]
            name = selection["fixtureId"]
            allocation_id = selection["allocationId"]
            generation = selection["generation"]
            attempt = selection["attempt"]
            preparation = plans[name]
            if generation == 0:
                result = coordinator.seal_unstarted_reservation(
                    preparation.plan, allocation_id=allocation_id,
                    owner=config.owner, device_id=config.device_id)
            else:
                def authorize(_kind):
                    _bounds(context, cancellation, deadline_monotonic)
                    return True
                result = coordinator.recover_cleanup(
                    preparation.plan, allocation_id=allocation_id, generation=generation,
                    owner=config.owner, device_id=config.device_id,
                    prepare_operation_id=_operation(issue_id, name, "prepare"),
                    payload_digest=coordinator.payload_digest(preparation.payload),
                    cleanup_operation_id=_operation(issue_id, name, "native-recovery-cleanup",
                                                   str(generation), str(attempt)),
                    timeout_seconds=min(60, max(.001, deadline_monotonic - time.monotonic())),
                    effect_authorizer=authorize, allow_unstarted=True)
            receipts.append(_result_receipt(selection, result))
        selection_digest = contracts.digest(selections)
        results_digest = contracts.digest(receipts)
        evidence = {
            "schemaVersion": 1, "kind": "ios-fixture-recovery-v1",
            "operationId": context.operation_id,
            "requestDigest": context.request_digest,
            "contextDigest": context.context_digest,
            "configurationDigest": context.configuration_digest,
            "scopeDigest": context.scope_digest,
            "nativeBindingDigest": context.binding_digest,
            "allocations": len(selections), "completed": len(receipts),
            "selectionDigest": selection_digest, "resultsDigest": results_digest,
            "selections": selections, "results": receipts,
        }
        document = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        proof = IOSFixtureRecoveryObservation(
            context.context_digest, contracts.digest(evidence), len(selections), len(receipts),
            selection_digest, results_digest, context, document, _ISSUER)
        with _LOCK:
            _LIVE[id(proof)] = proof
        return proof
    except IOSFixtureRecoveryError:
        raise
    except Exception:
        raise IOSFixtureRecoveryError() from None


def require_ios_fixture_recovery(proof, context):
    try:
        _require(type(proof) is IOSFixtureRecoveryObservation
                 and proof._issuer is _ISSUER and type(context) is IOSNativeRecoveryContext
                 and proof._context is context,
                 "ios_fixture_recovery_proof")
        with _LOCK:
            _require(_LIVE.get(id(proof)) is proof, "ios_fixture_recovery_proof")
        context.require()
        evidence = json.loads(proof._document)
        _require(
            type(evidence) is dict
            and set(evidence) == {
                "schemaVersion", "kind", "operationId", "requestDigest",
                "contextDigest", "configurationDigest", "scopeDigest",
                "nativeBindingDigest", "allocations", "completed",
                "selectionDigest", "resultsDigest", "selections", "results",
            }
            and evidence["schemaVersion"] == 1
            and evidence["kind"] == "ios-fixture-recovery-v1"
            and evidence["operationId"] == context.operation_id
            and evidence["requestDigest"] == context.request_digest
            and evidence["contextDigest"] == proof.context_digest == context.context_digest
            and evidence["configurationDigest"] == context.configuration_digest
            and evidence["scopeDigest"] == context.scope_digest
            and evidence["nativeBindingDigest"] == context.binding_digest
            and type(evidence["selections"]) is list
            and type(evidence["results"]) is list
            and evidence["allocations"] == proof.allocations == len(evidence["selections"])
            and evidence["completed"] == proof.completed == len(evidence["results"])
            and proof.completed == proof.allocations
            and evidence["selectionDigest"] == proof.selection_digest
            == contracts.digest(evidence["selections"])
            and evidence["resultsDigest"] == proof.results_digest
            == contracts.digest(evidence["results"])
            and proof.evidence_digest == contracts.digest(evidence),
            "ios_fixture_recovery_proof",
        )
        return proof
    except IOSFixtureRecoveryError:
        raise
    except Exception:
        raise IOSFixtureRecoveryError("ios_fixture_recovery_proof") from None


__all__ = ["IOSFixtureRecoveryError", "IOSFixtureRecoveryObservation",
           "recover_ios_fixtures", "require_ios_fixture_recovery"]
