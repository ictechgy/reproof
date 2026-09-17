"""Fixed host signing, independent signature inspection and bound provenance."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import secrets
import threading
import time

from . import contracts
from .execution.artifacts import ArtifactError, ArtifactValidationAuthority, BlobSet, ValidatedArtifactInput
from .execution.backend import QualificationAuthority, TrustedSigningPolicy
from .execution.journal import RunDenied, RunStore
from .execution.protocol import validate_signing_policy
from .execution.wire import canonical
from .project_repair import artifact_digest
from .repair_callbacks import RunCancellation, invoke_fixed
from .repair_execution import ProtectedBuildSupervisor, RepairExecutionError, TrustedBuildProof, _require


@dataclass(frozen=True, slots=True)
class SigningContext:
    operation_id: str
    repair_plan_digest: str
    project_digest: str
    application_id: str
    source_digest: str
    unsigned_artifact_digest: str
    signing_policy_digest: str
    nonce: str = field(repr=False)
    signed_artifact_digest: str | None = None
    _operation_binding: object = field(default=None, repr=False, compare=False)

    @property
    def digest(self):
        return contracts.digest({name: getattr(self, name) for name in self.__dataclass_fields__
                                 if name != '_operation_binding'})


@dataclass(frozen=True, slots=True)
class SigningObservation:
    context_digest: str
    artifacts: BlobSet
    evidence_digest: str
    termination_confirmed: bool
    cleanup_confirmed: bool


@dataclass(frozen=True, slots=True)
class SignatureObservation:
    context_digest: str
    valid: bool
    evidence_digest: str
    termination_confirmed: bool
    cleanup_confirmed: bool


@dataclass(frozen=True, slots=True)
class SigningFailureObservation:
    """A fixed adapter's measured failure, never a signature or build proof.

    Ordinary tool rejection can release a reservation only when the adapter
    has independently confirmed termination and its exact private cleanup.
    Exceptions, late callbacks and unknown cleanup keep the scope quarantined.
    """
    context_digest: str
    code: str
    evidence_digest: str
    termination_confirmed: bool
    cleanup_confirmed: bool


@dataclass(frozen=True, slots=True)
class SignedBuildProof:
    operation_id: str
    request_digest: str
    build: TrustedBuildProof
    artifact_digest: str
    signing_policy_digest: str
    signing_evidence_digest: str
    inspection_evidence_digest: str
    validated_artifacts: ValidatedArtifactInput = field(repr=False)
    _issuer: object = field(repr=False, compare=False)

    def public(self):
        return {'operationId': self.operation_id, 'requestDigest': self.request_digest,
            'repairPlanDigest': self.build.repair_plan_digest, 'projectDigest': self.build.project_digest,
            'sourceDigest': self.build.source_digest, 'applicationId': self.build.application_id,
            'unsignedArtifactDigest': self.build.artifact_digest, 'artifactDigest': self.artifact_digest,
            'artifactSetDigest': self.validated_artifacts.blobs.digest,
            'signingPolicyDigest': self.signing_policy_digest,
            'signingEvidenceDigest': self.signing_evidence_digest,
            'inspectionEvidenceDigest': self.inspection_evidence_digest,
            'cleanupConfirmed': True, 'verified': False}


class TrustedSigningSupervisor:
    """Register fixed local signer and inspector owned by the service operator.

    The signer uses only the policy's approved opaque identity/entitlement
    references and fixed host tool. It must not execute candidate hooks or
    scripts. The separate inspector measures application identity, signature,
    entitlements and provisioning against that same policy. Neither adapter is
    loaded from project files, request JSON or AI output. No credentials enter
    a context, guest, candidate or public proof. An unknown adapter lifecycle
    permanently quarantines this journal until operator recovery is provided.
    """
    def __init__(self, builder, *, authority, policy, policy_document, signer, inspector,
                 artifact_authority, artifact_policy_id, store, scope_digest, timeout_seconds=120,
                 operations=None):
        _require(type(builder) is ProtectedBuildSupervisor and type(authority) is QualificationAuthority
            and type(policy) is TrustedSigningPolicy and type(artifact_authority) is ArtifactValidationAuthority
            and type(store) is RunStore and callable(signer) and callable(inspector) and signer is not inspector,
            'signing_unqualified')
        try:
            checked = validate_signing_policy(policy_document)
            contracts.validate_digest(scope_digest); contracts.validate_id(artifact_policy_id)
            _require(authority.register_signing_policy(checked) is policy
                and policy.application_id == builder.application_id,
                'signing_policy_mismatch')
        except contracts.ContractError:
            raise RepairExecutionError('signing_policy_mismatch') from None
        _require(type(timeout_seconds) in (float, int) and 0 < timeout_seconds <= 900,
                 'signing_policy_mismatch')
        self.builder, self.authority, self.policy = builder, authority, policy
        self._policy_document = canonical(checked).decode('utf-8')
        self.signer, self.inspector = signer, inspector
        self.artifact_authority, self.artifact_policy_id = artifact_authority, artifact_policy_id
        self.store, self.scope_digest, self.timeout_seconds = store, scope_digest, timeout_seconds
        if operations is not None:
            from .repair_signing_recovery import SigningOperationStore
            from .repair_android_signing_owner import AndroidSigningOwnerSigner, AndroidSigningOwnerInspector
            from .ios_signing_operation import IOSSigningOperationStore
            from .repair_ios_signing_owner import IOSSigningOwnerSigner, IOSSigningOwnerInspector
            android = (type(operations) is SigningOperationStore and type(signer) is AndroidSigningOwnerSigner
                       and type(inspector) is AndroidSigningOwnerInspector)
            ios = (type(operations) is IOSSigningOperationStore and type(signer) is IOSSigningOwnerSigner
                   and type(inspector) is IOSSigningOwnerInspector
                   and signer.provisioning.definition_digest == inspector.provisioning.definition_digest)
            _require((android or ios)
                and operations.run_store is store and operations.scope_digest == scope_digest
                and signer.operations is operations and inspector.operations is operations
                and signer.policy_digest == inspector.policy_digest == policy.definition_digest,
                'signing_unqualified')
        self.operations = operations
        self._proofs = {}; self._issuer = object(); self._lock = threading.RLock()

    @property
    def definition_digest(self):
        definition = {'builder': self.builder.definition_digest, 'policy': self.policy.definition_digest,
            'scope': self.scope_digest, 'environment': self.store.environment_digest,
            'artifactPolicy': self.artifact_policy_id, 'timeoutSeconds': self.timeout_seconds}
        if self.operations is not None:
            definition['signingOwner'] = self.operations.definition_digest
            from .ios_signing_operation import IOSSigningOperationStore
            if type(self.operations) is IOSSigningOperationStore:
                definition['iosProvisioning'] = self.signer.provisioning.definition_digest
        return contracts.digest(definition)

    def _require_policy(self):
        import json
        _require(self.authority.register_signing_policy(json.loads(self._policy_document)) is self.policy,
                 'signing_policy_mismatch')

    def ready(self, *, _observe=False):
        self._require_policy()
        if self.operations is not None:
            self.signer.ready()
            self.inspector.ready()
        try:
            if _observe:
                self.store.require_scope_available('signing', self.scope_digest)
            else:
                with self.store.repair_scope_lease('signing', self.scope_digest):
                    self.store.require_available()
        except RunDenied:
            raise RepairExecutionError('signing_unavailable') from None
        return self.definition_digest

    @contextmanager
    def _admit(self, context, request_digest):
        if self.operations is None:
            with self.store.repair_scope_lease('signing', self.scope_digest), self.store.admit(
                    context.operation_id, request_digest, disk_bytes=1) as run:
                yield run, context
        else:
            from .repair_signing_recovery import MIN_OPERATION_BYTES
            from .ios_signing_operation import IOSSigningOperationStore
            admission = (self.operations.admit(context, request_digest) if type(self.operations) is IOSSigningOperationStore
                         else self.operations.admit(context, request_digest, MIN_OPERATION_BYTES))
            with admission as operation:
                yield operation.run, replace(context, _operation_binding=operation)

    @staticmethod
    def _observation(value, expected_type, context):
        try:
            _require(type(value) is expected_type and value.context_digest == context.digest
                and value.termination_confirmed is True and value.cleanup_confirmed is True,
                'signing_quarantined')
            contracts.validate_digest(value.evidence_digest)
        except (contracts.ContractError, AttributeError):
            raise RepairExecutionError('signing_quarantined') from None

    @classmethod
    def _failed_observation(cls, value, context):
        cls._observation(value, SigningFailureObservation, context)
        _require(type(value.code) is str and value.code in {
            'signing_failed', 'signing_timeout', 'cancelled', 'signature_invalid', 'artifact_invalid'},
            'signing_quarantined')

    def sign(self, build, *, operation_id, cancellation):
        from .repair_signing_recovery import SigningRecoveryError
        self.ready()
        _require(type(build) is TrustedBuildProof, 'untrusted_build')
        self.builder.require_build(build, source_digest=build.source_digest,
                                   repair_plan_digest=build.repair_plan_digest)
        contracts.validate_id(operation_id)
        _require(not cancellation.is_set(), 'cancelled')
        context = SigningContext(operation_id, build.repair_plan_digest, build.project_digest,
            build.application_id, build.source_digest, build.artifact_digest,
            self.policy.definition_digest, secrets.token_hex(24))
        request_digest = contracts.digest({'context': context.digest, 'definition': self.definition_digest})
        try:
            with self._admit(context, request_digest) as (run, context):
                clean = False
                stop = RunCancellation(cancellation, run)
                try:
                    _require(not stop.is_set(), 'cancelled')
                    deadline = time.monotonic() + self.timeout_seconds
                    signed, returned = invoke_fixed(self.signer, context, build.validated_artifacts.blobs,
                        cancellation=stop, deadline_monotonic=deadline)
                    _require(returned, 'signing_quarantined')
                    if type(signed) is SigningFailureObservation:
                        self._failed_observation(signed, context)
                        clean = True
                        raise RepairExecutionError('cancelled' if stop.is_set() else signed.code)
                    self._observation(signed, SigningObservation, context)
                    clean = True
                    _require(not stop.is_set(), 'cancelled')
                    _require(type(signed.artifacts) is BlobSet and time.monotonic() < deadline, 'signature_invalid')
                    validated = self.artifact_authority.validate(signed.artifacts, policy_id=self.artifact_policy_id,
                        project_digest=build.project_digest, execution_class='mobile-device')
                    measured = artifact_digest(signed.artifacts, self.builder.artifact_identity)
                    inspection_context = SigningContext(operation_id, build.repair_plan_digest, build.project_digest,
                        build.application_id, build.source_digest, build.artifact_digest,
                        self.policy.definition_digest, secrets.token_hex(24), measured,
                        _operation_binding=context._operation_binding)
                    clean = False
                    inspected, returned = invoke_fixed(self.inspector, inspection_context, signed.artifacts,
                        cancellation=stop, deadline_monotonic=deadline)
                    _require(returned, 'signing_quarantined')
                    if type(inspected) is SigningFailureObservation:
                        self._failed_observation(inspected, inspection_context)
                        clean = True
                        raise RepairExecutionError('cancelled' if stop.is_set() else inspected.code)
                    self._observation(inspected, SignatureObservation, inspection_context)
                    clean = True
                    _require(not stop.is_set(), 'cancelled')
                    _require(inspected.valid is True and time.monotonic() < deadline, 'signature_invalid')
                    self._require_policy()
                    self.builder.require_build(build, source_digest=build.source_digest,
                                               repair_plan_digest=build.repair_plan_digest)
                    run.finish('succeeded', stopped=True)
                    _require(not stop.is_set(), 'cancelled')
                    _require(self.store.status(operation_id)['state'] == 'succeeded', 'signing_quarantined')
                    proof = SignedBuildProof(operation_id, request_digest, build, measured,
                        self.policy.definition_digest, signed.evidence_digest, inspected.evidence_digest,
                        validated, self._issuer)
                    with self._lock:
                        self._proofs[operation_id] = proof
                    return proof
                finally:
                    if not run.finished:
                        if stop.is_set(): self.store.cancel(operation_id, request_digest)
                        run.finish('failed', stopped=clean)
        except (RunDenied, ArtifactError, SigningRecoveryError, OSError):
            raise RepairExecutionError('signing_unavailable') from None

    def require_signed(self, proof, *, source_digest, repair_plan_digest):
        self._require_policy()
        with self._lock:
            _require(type(proof) is SignedBuildProof and proof._issuer is self._issuer
                and self._proofs.get(proof.operation_id) is proof
                and proof.signing_policy_digest == self.policy.definition_digest, 'untrusted_signature')
            self.builder.require_build(proof.build, source_digest=source_digest, repair_plan_digest=repair_plan_digest)
            try:
                state = self.store.status(proof.operation_id)
                _require(state['state'] == 'succeeded' and state['requestDigest'] == proof.request_digest,
                         'untrusted_signature')
                self.artifact_authority.require_input(proof.validated_artifacts,
                    input_digest=proof.validated_artifacts.blobs.digest,
                    project_digest=proof.build.project_digest, execution_class='mobile-device')
            except (ArtifactError, RunDenied, OSError):
                raise RepairExecutionError('untrusted_signature') from None
        return proof
