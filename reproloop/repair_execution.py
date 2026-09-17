"""G9 composition of qualified execution and measured build provenance."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time

from . import contracts
from .execution.artifacts import ArtifactError, ArtifactValidationAuthority, BlobSet, ValidatedArtifactInput
from .execution.backend import (ExecutionDenied, QualificationAuthority, TrustedExecutionRoute,
                                TrustedValidationPlan)
from .execution.runtime import GuestExecutionResult, HostBuildBackend, MacOSVirtualizationBackend
from .execution.journal import RunDenied
from .project_repair import artifact_digest


class RepairExecutionError(RuntimeError):
    def __init__(self, code='build_unqualified'):
        super().__init__('Protected repair execution is unavailable or unconfirmed')
        self.code = code


def _require(condition, code='build_unqualified'):
    if not condition:
        raise RepairExecutionError(code)


@dataclass(frozen=True, slots=True)
class TrustedBuildProof:
    operation_id: str
    request_digest: str
    repair_plan_digest: str
    project_digest: str
    application_id: str
    source_digest: str
    artifact_digest: str
    environment_digest: str
    validated_artifacts: ValidatedArtifactInput = field(repr=False)
    _issuer: object = field(repr=False, compare=False)
    # 격리 표시는 기본값을 두지 않는다 — 발급자가 반드시 backend의 isolation을 명시한다.
    isolation: str

    def public(self):
        return {'operationId': self.operation_id, 'requestDigest': self.request_digest,
            'repairPlanDigest': self.repair_plan_digest, 'projectDigest': self.project_digest,
            'applicationId': self.application_id, 'sourceDigest': self.source_digest,
            'artifactDigest': self.artifact_digest,
            'artifactSetDigest': self.validated_artifacts.blobs.digest,
            'environmentDigest': self.environment_digest, 'buildIsolation': self.isolation,
            'cleanupConfirmed': True, 'verified': False}


class ProtectedBuildSupervisor:
    """Build only through a qualified backend: guest VM or explicit host toolchain.

    The caller owns the authority and format checker. This supervisor measures
    the actual returned bytes and confirms the native lifecycle and journal;
    candidate exit codes and report files cannot issue a verification receipt.
    """
    def __init__(self, backend, *, qualification, route, validation_plan,
                 artifact_authority, application_id, artifact_identity='file-sha256'):
        _require(type(backend) in (MacOSVirtualizationBackend, HostBuildBackend)
            and type(backend.authority) is QualificationAuthority
            and type(route) is TrustedExecutionRoute
            and route.execution_class in ('build-guest', 'host-build')
            and type(validation_plan) is TrustedValidationPlan
            and type(artifact_authority) is ArtifactValidationAuthority)
        contracts.validate_id(application_id)
        _require(artifact_identity in {'file-sha256', 'tree-sha256'})
        self.backend = backend
        self.qualification, self.route, self.validation_plan = qualification, route, validation_plan
        self.artifact_authority = artifact_authority
        self.application_id, self.artifact_identity = application_id, artifact_identity
        self._issuer = object(); self._proofs = {}; self._lock = threading.RLock()

    @property
    def definition_digest(self):
        return contracts.digest({'route': self.route.definition_digest, 'validationPlan': self.validation_plan.definition_digest,
            'qualification': getattr(self.qualification, 'definition_digest', None),
            'environment': self.backend.bundle.environment_digest, 'applicationId': self.application_id,
            'artifactIdentity': self.artifact_identity})

    def request(self, operation_id, input_digest):
        route = self.route
        return {'protocolVersion': 1, 'operationId': operation_id, 'backendId': route.backend_id,
            'executionClass': route.execution_class, 'projectDigest': route.project_digest,
            'environmentDigest': route.environment_digest, 'inputKind': route.input_kind,
            'inputDigest': input_digest, 'recipeId': route.recipe_id, 'artifactPolicyId': route.artifact_policy_id,
            'requiredValidationIds': sorted(self.validation_plan.check_ids), 'cleanupPolicyId': route.cleanup_policy_id}

    def authorize(self, request):
        try:
            return self.backend.authority.authorize(request, qualification=self.qualification,
                execution_route=self.route, validation_plan=self.validation_plan,
                evaluated_at_ms=int(time.time() * 1000))
        except (ExecutionDenied, contracts.ContractError, TypeError):
            raise RepairExecutionError('build_unqualified') from None

    def ready(self, *, project_digest, recipe_id, validation_plan_digest, _observe=False):
        _require(project_digest == self.route.project_digest and recipe_id == self.route.recipe_id
            and validation_plan_digest == self.validation_plan.definition_digest, 'build_policy_mismatch')
        self.authorize(self.request('repair_preflight', '0' * 64))
        scope_kind = getattr(self.backend, 'scope_kind', 'vm')
        try:
            if _observe:
                self.backend.store.require_scope_available(scope_kind, self.backend.bundle.machine_digest)
            else:
                with self.backend.store.machine_lease(self.backend.bundle.machine_digest, kind=scope_kind):
                    self.backend.store.require_available()
        except RunDenied:
            raise RepairExecutionError('build_unavailable') from None
        return self.definition_digest

    def build(self, sources, *, operation_id, repair_plan_digest, cancellation):
        _require(type(sources) is BlobSet and callable(getattr(cancellation, 'is_set', None)))
        try:
            contracts.validate_digest(repair_plan_digest)
            contracts.validate_id(operation_id)
        except contracts.ContractError:
            raise RepairExecutionError('build_policy_mismatch') from None
        _require(not cancellation.is_set(), 'cancelled')
        request = self.request(operation_id, sources.digest)
        authorization = self.authorize(request)
        done = threading.Event()
        def cancel_guest():
            while not done.wait(.01):
                if cancellation.is_set():
                    try:
                        self.backend.cancel(operation_id, contracts.digest(request))
                    except ExecutionDenied:
                        # The job may be cancelled before VM admission. Retry
                        # until it is durably admitted or dispatch has finished.
                        pass
        watcher = threading.Thread(target=cancel_guest, name='repro-build-cancellation', daemon=True)
        watcher.start()
        try:
            _require(not cancellation.is_set(), 'cancelled')
            result = self.backend.execute(request, authorization, sources)
        except (ExecutionDenied, contracts.ContractError, OSError):
            raise RepairExecutionError('cancelled' if cancellation.is_set() else 'build_failed') from None
        finally:
            done.set(); watcher.join(1)
        _require(type(result) is GuestExecutionResult and result.isolation == self.backend.isolation,
            'build_failed')
        _require(result.cleanup_confirmed is True and result.status != 'quarantined', 'build_quarantined')
        _require(not cancellation.is_set() and result.status != 'cancelled', 'cancelled')
        _require(result.status == 'candidate-output' and result.operation_id == operation_id
            and result.request_digest == contracts.digest(request) and result.input_digest == sources.digest
            and type(result.artifacts) is BlobSet and result.artifact_digest == result.artifacts.digest
            and set(result.lifecycle) == {'configured', 'started', 'connected', 'stopped'}
            and all(value is True for value in result.lifecycle.values()), 'build_failed')
        try:
            state = self.backend.store.status(operation_id)
            _require(state['state'] == 'succeeded' and state['requestDigest'] == result.request_digest, 'build_failed')
            validated = self.artifact_authority.validate(result.artifacts, policy_id=self.route.artifact_policy_id,
                project_digest=self.route.project_digest, execution_class='mobile-device')
        except ArtifactError:
            raise RepairExecutionError('artifact_invalid') from None
        _require(not cancellation.is_set(), 'cancelled')
        proof = TrustedBuildProof(operation_id, result.request_digest, repair_plan_digest,
            self.route.project_digest, self.application_id, sources.digest,
            artifact_digest(result.artifacts, self.artifact_identity), self.route.environment_digest,
            validated, self._issuer, isolation=self.backend.isolation)
        with self._lock:
            _require(operation_id not in self._proofs, 'build_reused')
            self._proofs[operation_id] = proof
        return proof

    def require_build(self, proof, *, source_digest, repair_plan_digest):
        with self._lock:
            _require(type(proof) is TrustedBuildProof and proof._issuer is self._issuer
                and self._proofs.get(proof.operation_id) is proof
                and proof.source_digest == source_digest and proof.repair_plan_digest == repair_plan_digest,
                'untrusted_build')
            self.authorize(self.request(proof.operation_id, proof.source_digest))
            try:
                state = self.backend.store.status(proof.operation_id)
                _require(state['state'] == 'succeeded' and state['requestDigest'] == proof.request_digest, 'untrusted_build')
                self.artifact_authority.require_input(proof.validated_artifacts,
                    input_digest=proof.validated_artifacts.blobs.digest,
                    project_digest=proof.project_digest, execution_class='mobile-device')
            except (ArtifactError, OSError, contracts.ContractError):
                raise RepairExecutionError('untrusted_build') from None
        return proof


__all__ = ['RepairExecutionError', 'ProtectedBuildSupervisor', 'TrustedBuildProof']
