"""One protected build/sign/mobile chain; no host execution or JSON authority."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading

from . import contracts
from .repair_execution import ProtectedBuildSupervisor, RepairExecutionError, _require
from .repair_mobile import ProtectedMobileSupervisor
from .repair_signing import TrustedSigningSupervisor


@dataclass(frozen=True, slots=True)
class ProtectedVerificationProof:
    operation_id: str
    build: object = field(repr=False)
    signing: object = field(repr=False)
    mobile: object = field(repr=False)
    _issuer: object = field(repr=False, compare=False)

    def public(self):
        return {'build': self.build.public(), 'signing': self.signing.public(),
                'afterEvidence': self.mobile.public()}


class ProtectedRepairExecutor:
    def __init__(self, builder, signer, mobile):
        _require(type(builder) is ProtectedBuildSupervisor and type(signer) is TrustedSigningSupervisor
            and type(mobile) is ProtectedMobileSupervisor and signer.builder is builder
            and mobile.signer is signer, 'protected_verification_unavailable')
        self.builder, self.signer, self.mobile = builder, signer, mobile
        self._proofs = {}; self._issuer = object(); self._lock = threading.RLock()

    @property
    def definition_digest(self):
        return contracts.digest({'build': self.builder.definition_digest,
            'signing': self.signer.definition_digest, 'mobile': self.mobile.definition_digest})

    @property
    def device_id(self):
        return self.mobile.adapter.device_id

    def ready(self, *, project_digest, build_recipe_id, validation_recipe_ids, runtime_policy_digest=None,
              application_id=None, _observe=False):
        _require(self.builder.application_id == self.signer.policy.application_id == self.mobile.route.application_id
            and (application_id is None or application_id == self.builder.application_id), 'build_policy_mismatch')
        self.builder.ready(project_digest=project_digest, recipe_id=build_recipe_id,
                           validation_plan_digest=self.mobile.validators.definition_digest, _observe=_observe)
        self.mobile.ready(project_digest=project_digest,
            runtime_policy_digest=runtime_policy_digest or self.mobile.adapter.runtime_policy_digest,
            validation_recipe_ids=validation_recipe_ids, _observe=_observe)
        return self.definition_digest

    def execute(self, sources, approved, *, operation_id, repair_plan_digest, cancellation, boundary, progress):
        self.ready(project_digest=approved.project_digest, build_recipe_id=self.builder.route.recipe_id,
            validation_recipe_ids=approved.qualification['validationRecipeIds'],
            runtime_policy_digest=approved.runtime_policy_digest, application_id=approved.original['applicationId'])
        boundary(); _require(not cancellation.is_set(), 'cancelled')
        progress('building', {})
        build = self.builder.build(sources, operation_id=operation_id + '_build',
            repair_plan_digest=repair_plan_digest, cancellation=cancellation)
        boundary(); _require(not cancellation.is_set(), 'cancelled')
        progress('signing', {'build': build.public()})
        signing = self.signer.sign(build, operation_id=operation_id + '_sign', cancellation=cancellation)
        boundary(); _require(not cancellation.is_set(), 'cancelled')
        progress('installing', {'signing': signing.public()})
        mobile = self.mobile.verify(signing, approved, operation_id=operation_id + '_mobile',
            cancellation=cancellation, boundary=boundary, progress=progress)
        boundary(); _require(not cancellation.is_set(), 'cancelled')
        proof = ProtectedVerificationProof(operation_id, build, signing, mobile, self._issuer)
        with self._lock:
            _require(operation_id not in self._proofs, 'verification_reused')
            self._proofs[operation_id] = proof
        self.require_verified(proof, sources.digest, repair_plan_digest, approved)
        return proof

    def require_verified(self, proof, source_digest, repair_plan_digest, approved):
        with self._lock:
            _require(type(proof) is ProtectedVerificationProof and proof._issuer is self._issuer
                and self._proofs.get(proof.operation_id) is proof, 'untrusted_verification')
            self.builder.require_build(proof.build, source_digest=source_digest, repair_plan_digest=repair_plan_digest)
            self.signer.require_signed(proof.signing, source_digest=source_digest, repair_plan_digest=repair_plan_digest)
            self.mobile.require_verified(proof.mobile, proof.signing, approved)
        return proof.public()

    def discard(self, operation_id):
        """Drop process capabilities/artifact bytes after terminal publication.

        Durable evidence and quarantine stay in their journals. This never
        revives an operation or converts retained JSON into new authority.
        """
        with self._lock:
            self._proofs.pop(operation_id, None)
        for supervisor, suffix in ((self.mobile, '_mobile'), (self.signer, '_sign'), (self.builder, '_build')):
            with supervisor._lock:
                supervisor._proofs.pop(operation_id + suffix, None)
