"""Local authority capabilities for protected execution backends.

Wire records are inert. A trusted supervisor owns one QualificationAuthority
instance and does not expose it to candidates or imported package handlers.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import threading

from reproloop.contracts.versions import digest, epoch_ms, require, validate_digest, validate_id

from .protocol import (
    EXECUTION_CLASSES,
    validate_backend_qualification_record,
    validate_execution_route,
    validate_execution_request,
    validate_external_validation_plan,
    validate_signing_policy,
)


REQUIRED_PROBES = {
    "build-guest": frozenset({
        "boot", "network-boundary", "filesystem-boundary", "process-termination", "cleanup",
    }),
    "host-build": frozenset({
        "toolchain-boundary", "process-termination", "cleanup",
    }),
    "desktop-guest": frozenset({
        "boot", "network-boundary", "filesystem-boundary", "process-termination", "cleanup",
    }),
    "mobile-device": frozenset({
        "device-boundary", "network-boundary", "backend-scope", "process-termination", "state-cleanup",
    }),
}
MAX_PROBE_AGE_MS = 24 * 60 * 60 * 1000


class ExecutionDenied(RuntimeError):
    """A static, non-sensitive protected-execution denial."""


def _deny(condition: bool, message: str) -> None:
    if not condition:
        raise ExecutionDenied(message)


def _register_definition(method):
    """Keep definition comparison and insertion atomic across supervisor threads."""
    @wraps(method)
    def register(self, *args, **kwargs):
        with self._definitions_lock:
            return method(self, *args, **kwargs)
    return register


@dataclass(frozen=True, slots=True)
class TrustedProbeReceipt:
    probe_id: str
    backend_id: str
    execution_class: str
    environment_digest: str
    outcome: str
    evidence_digest: str
    observed_at_ms: int
    _issuer: object


@dataclass(frozen=True, slots=True)
class BackendQualification:
    qualification_id: str
    backend_id: str
    execution_class: str
    environment_digest: str
    signing_policy_id: str | None
    issued_at_ms: int
    expires_at_ms: int
    definition_digest: str
    _issuer: object


@dataclass(frozen=True, slots=True)
class TrustedValidationPlan:
    plan_id: str
    project_digest: str
    check_ids: frozenset[str]
    definition_digest: str
    _issuer: object


@dataclass(frozen=True, slots=True)
class TrustedSigningPolicy:
    policy_id: str
    platform: str
    application_id: str
    definition_digest: str
    _issuer: object


@dataclass(frozen=True, slots=True)
class TrustedExecutionRoute:
    route_id: str
    project_digest: str
    backend_id: str
    execution_class: str
    environment_digest: str
    input_kind: str
    recipe_id: str
    artifact_policy_id: str
    validation_plan_id: str
    cleanup_policy_id: str
    platform: str | None
    application_id: str | None
    signing_policy_id: str | None
    definition_digest: str
    _issuer: object


@dataclass(frozen=True, slots=True)
class ExecutionAuthorization:
    operation_id: str
    backend_id: str
    execution_class: str
    request_digest: str
    qualification_id: str
    validation_plan_id: str
    execution_route_id: str
    authorized_at_ms: int
    expires_at_ms: int
    _issuer: object


class QualificationAuthority:
    """Process-local capability owner used only by the trusted supervisor.

    ``record_probe`` records the supervisor's own independent observation. It
    must never be exposed as an RPC or called from candidate-controlled code.
    """

    def __init__(self):
        self._issuer = object()
        self._definitions_lock = threading.RLock()
        self._qualifications = {}
        self._revoked_qualifications = set()
        self._validation_plans = {}
        self._signing_policies = {}
        self._execution_routes = {}

    def record_probe(
        self,
        *,
        probe_id,
        backend_id,
        execution_class,
        environment_digest,
        outcome,
        evidence_digest,
        observed_at_ms,
    ) -> TrustedProbeReceipt:
        validate_id(probe_id, "probe id")
        validate_id(backend_id, "backend id")
        require(execution_class in EXECUTION_CLASSES, "Invalid execution class")
        validate_digest(environment_digest, "environment digest")
        require(outcome in ("pass", "fail"), "Invalid trusted probe outcome")
        validate_digest(evidence_digest, "probe evidence digest")
        epoch_ms(observed_at_ms, "probe observation time")
        return TrustedProbeReceipt(
            probe_id,
            backend_id,
            execution_class,
            environment_digest,
            outcome,
            evidence_digest,
            observed_at_ms,
            self._issuer,
        )

    @_register_definition
    def revoke_backend(self, backend_id, execution_class, environment_digest):
        """Trusted supervisor revocation; old capability objects stay revoked."""
        validate_id(backend_id, "backend id")
        require(execution_class in EXECUTION_CLASSES, "Invalid execution class")
        validate_digest(environment_digest, "environment digest")
        self._revoked_qualifications.update(
            key for key, item in self._qualifications.items()
            if (item.backend_id, item.execution_class, item.environment_digest)
            == (backend_id, execution_class, environment_digest))

    @_register_definition
    def issue_backend_qualification(
        self, record, receipts, *, evaluated_at_ms
    ) -> BackendQualification:
        value = validate_backend_qualification_record(record)
        now = epoch_ms(evaluated_at_ms, "qualification evaluation time")
        _deny(type(receipts) in (list, tuple), "Trusted backend probe receipts required")
        _deny(all(type(item) is TrustedProbeReceipt and item._issuer is self._issuer
                  for item in receipts), "Trusted backend probe receipts required")
        ids = [item.probe_id for item in receipts]
        _deny(len(ids) == len(set(ids)) and set(ids) == set(value["probeIds"]),
              "Backend qualification probe binding mismatch")
        _deny(REQUIRED_PROBES[value["executionClass"]] <= set(ids),
              "Backend qualification probes incomplete")
        _deny(all(item.outcome == "pass" for item in receipts),
              "Backend qualification probes did not pass")
        _deny(all(
            item.backend_id == value["backendId"]
            and item.execution_class == value["executionClass"]
            and item.environment_digest == value["environmentDigest"]
            for item in receipts
        ), "Backend qualification probe binding mismatch")
        _deny(all(
            0 <= value["issuedAtMs"] - item.observed_at_ms <= MAX_PROBE_AGE_MS
            for item in receipts
        ), "Backend qualification probes are stale")
        _deny(value["issuedAtMs"] <= now <= value["expiresAtMs"],
              "Backend qualification is not current")
        receipt_values = [
            {
                "probeId": item.probe_id,
                "backendId": item.backend_id,
                "executionClass": item.execution_class,
                "environmentDigest": item.environment_digest,
                "outcome": item.outcome,
                "evidenceDigest": item.evidence_digest,
                "observedAtMs": item.observed_at_ms,
            }
            for item in sorted(receipts, key=lambda item: item.probe_id)
        ]
        definition_digest = digest({"record": value, "receipts": receipt_values})
        existing = self._qualifications.get(value["id"])
        _deny(value["id"] not in self._revoked_qualifications, "Backend qualification revoked")
        _deny(existing is None or existing.definition_digest == definition_digest,
              "Backend qualification identity conflict")
        if existing is not None:
            return existing
        qualification = BackendQualification(
            value["id"],
            value["backendId"],
            value["executionClass"],
            value["environmentDigest"],
            value.get("signingPolicyId"),
            value["issuedAtMs"],
            value["expiresAtMs"],
            definition_digest,
            self._issuer,
        )
        self._qualifications[value["id"]] = qualification
        return qualification

    @_register_definition
    def register_validation_plan(self, plan) -> TrustedValidationPlan:
        value = validate_external_validation_plan(plan)
        definition_digest = digest(value)
        existing = self._validation_plans.get(value["id"])
        _deny(existing is None or existing.definition_digest == definition_digest,
              "Trusted validation plan identity conflict")
        if existing is not None:
            return existing
        trusted = TrustedValidationPlan(
            value["id"],
            value["projectDigest"],
            frozenset(check["id"] for check in value["checks"]),
            definition_digest,
            self._issuer,
        )
        self._validation_plans[value["id"]] = trusted
        return trusted

    @_register_definition
    def register_signing_policy(self, policy) -> TrustedSigningPolicy:
        value = validate_signing_policy(policy)
        definition_digest = digest(value)
        existing = self._signing_policies.get(value["id"])
        _deny(existing is None or existing.definition_digest == definition_digest,
              "Trusted signing policy identity conflict")
        if existing is not None:
            return existing
        trusted = TrustedSigningPolicy(
            value["id"], value["platform"], value["applicationId"],
            definition_digest, self._issuer
        )
        self._signing_policies[value["id"]] = trusted
        return trusted

    @_register_definition
    def register_execution_route(self, route) -> TrustedExecutionRoute:
        value = validate_execution_route(route)
        definition_digest = digest(value)
        existing = self._execution_routes.get(value["id"])
        _deny(existing is None or existing.definition_digest == definition_digest,
              "Trusted execution route identity conflict")
        if existing is not None:
            return existing
        trusted = TrustedExecutionRoute(
            value["id"],
            value["projectDigest"],
            value["backendId"],
            value["executionClass"],
            value["environmentDigest"],
            value["inputKind"],
            value["recipeId"],
            value["artifactPolicyId"],
            value["validationPlanId"],
            value["cleanupPolicyId"],
            value.get("platform"),
            value.get("applicationId"),
            value.get("signingPolicyId"),
            definition_digest,
            self._issuer,
        )
        self._execution_routes[value["id"]] = trusted
        return trusted

    @_register_definition
    def require_qualification(self, qualification, *, backend_id, execution_class,
                              environment_digest, evaluated_at_ms, signing_policy_id=None):
        """Require the exact issued, current capability before creating owners."""
        now = epoch_ms(evaluated_at_ms, "qualification evaluation time")
        _deny(type(qualification) is BackendQualification and qualification._issuer is self._issuer
            and self._qualifications.get(qualification.qualification_id) is qualification,
            "Trusted backend qualification required")
        _deny(qualification.qualification_id not in self._revoked_qualifications,
              "Backend qualification revoked")
        _deny((qualification.backend_id, qualification.execution_class, qualification.environment_digest,
               qualification.signing_policy_id) ==
              (backend_id, execution_class, environment_digest, signing_policy_id),
              "Backend qualification mismatch")
        _deny(qualification.issued_at_ms <= now <= qualification.expires_at_ms,
              "Backend qualification is not current")
        return qualification

    @_register_definition
    def authorize(
        self,
        request,
        *,
        qualification,
        validation_plan,
        execution_route=None,
        evaluated_at_ms,
        signing_policy=None,
    ) -> ExecutionAuthorization:
        value = validate_execution_request(request)
        now = epoch_ms(evaluated_at_ms, "execution authorization time")
        self.require_qualification(qualification, backend_id=value["backendId"],
            execution_class=value["executionClass"], environment_digest=value["environmentDigest"],
            signing_policy_id=value.get("signingPolicyId"), evaluated_at_ms=now)
        _deny(type(execution_route) is TrustedExecutionRoute
              and execution_route._issuer is self._issuer,
              "Trusted execution route required")
        route_values = (
            value["projectDigest"], value["backendId"], value["executionClass"],
            value["environmentDigest"], value["inputKind"], value["recipeId"],
            value["artifactPolicyId"], value["cleanupPolicyId"],
            value.get("platform"), value.get("applicationId"), value.get("signingPolicyId"),
        )
        trusted_route_values = (
            execution_route.project_digest, execution_route.backend_id,
            execution_route.execution_class, execution_route.environment_digest,
            execution_route.input_kind, execution_route.recipe_id,
            execution_route.artifact_policy_id, execution_route.cleanup_policy_id,
            execution_route.platform, execution_route.application_id,
            execution_route.signing_policy_id,
        )
        _deny(route_values == trusted_route_values, "Trusted execution route mismatch")
        _deny(type(validation_plan) is TrustedValidationPlan
              and validation_plan._issuer is self._issuer,
              "Trusted validation plan required")
        _deny(validation_plan.project_digest == value["projectDigest"]
              and validation_plan.plan_id == execution_route.validation_plan_id
              and validation_plan.check_ids == set(value["requiredValidationIds"]),
              "Trusted validation plan mismatch")
        if value["executionClass"] == "mobile-device":
            _deny(type(signing_policy) is TrustedSigningPolicy
                  and signing_policy._issuer is self._issuer,
                  "Trusted signing policy required")
            _deny(signing_policy.policy_id == value["signingPolicyId"]
                  and signing_policy.platform == value["platform"]
                  and signing_policy.application_id == value["applicationId"]
                  and qualification.signing_policy_id == value["signingPolicyId"],
                  "Trusted signing policy mismatch")
        else:
            _deny(signing_policy is None, "Signing policy does not belong to this execution class")
        return ExecutionAuthorization(
            value["operationId"],
            value["backendId"],
            value["executionClass"],
            digest(value),
            qualification.qualification_id,
            validation_plan.plan_id,
            execution_route.route_id,
            now,
            qualification.expires_at_ms,
            self._issuer,
        )

    @_register_definition
    def check_authorization(self, authorization, request, *, evaluated_at_ms) -> None:
        value = validate_execution_request(request)
        now = epoch_ms(evaluated_at_ms, "execution dispatch time")
        _deny(type(authorization) is ExecutionAuthorization
              and authorization._issuer is self._issuer,
              "Trusted execution authorization required")
        _deny(authorization.qualification_id not in self._revoked_qualifications,
              "Backend qualification revoked")
        _deny(authorization.request_digest == digest(value)
              and authorization.operation_id == value["operationId"]
              and authorization.backend_id == value["backendId"]
              and authorization.execution_class == value["executionClass"],
              "Execution authorization binding mismatch")
        _deny(authorization.authorized_at_ms <= now <= authorization.expires_at_ms,
              "Execution authorization expired")


class DisabledExecutionBackend:
    """Explicit non-backend used until G8b supplies and qualifies a real guest."""

    def __init__(self, backend_id: str, execution_class: str):
        validate_id(backend_id, "backend id")
        require(execution_class in EXECUTION_CLASSES, "Invalid execution class")
        self.backend_id = backend_id
        self.execution_class = execution_class

    def execute(self, authorization: ExecutionAuthorization):
        raise ExecutionDenied("Protected backend implementation is unavailable")
