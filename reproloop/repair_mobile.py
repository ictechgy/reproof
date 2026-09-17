"""Qualified exclusive mobile execution with independent, cleanup-gated proof."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import json
import secrets
import threading
import time

from . import contracts
from .execution.backend import (ExecutionDenied, QualificationAuthority, TrustedExecutionRoute,
                                TrustedValidationPlan)
from .execution.journal import RunDenied, RunStore
from .execution.wire import canonical
from .qualification import QualificationError
from .repair_callbacks import RunCancellation, invoke_fixed
from .repair_execution import RepairExecutionError, _require
from .repair_signing import SignedBuildProof, TrustedSigningSupervisor
from .scenario_runner import ScenarioError, ScenarioRunner
from .validation import TrustedValidationAuthority, ValidationBinding, ValidationError


@dataclass(frozen=True, slots=True)
class TrustedMobileAdapter:
    """Fixed local provider contract; never construct from an uploaded document.

    Installation acquires native exclusive ownership and enforces the qualified
    network, account, backend and device scope until cleanup ends. The device
    must stay inaccessible to other sessions between individual G4 replays.
    Replay invokes the registered G4 service and returns its original result.
    Cleanup stops all candidate activity, reconciles fixtures, sanitizes the
    device and only then releases native ownership, even after failed install.
    The generic Lab alone does not implement this enclosing ownership contract.
    """
    adapter_id: str
    scope_digest: str
    device_id: str
    project_digest: str
    runtime_policy_digest: str
    install: object = field(repr=False)
    replay: object = field(repr=False)
    cleanup: object = field(repr=False)

    def __post_init__(self):
        contracts.validate_id(self.adapter_id); contracts.validate_id(self.device_id)
        for value in (self.scope_digest, self.project_digest, self.runtime_policy_digest):
            contracts.validate_digest(value)
        _require(all(callable(value) for value in (self.install, self.replay, self.cleanup)), 'mobile_unqualified')

    @property
    def definition_digest(self):
        return contracts.digest({'id': self.adapter_id, 'scope': self.scope_digest, 'deviceId': self.device_id,
            'project': self.project_digest, 'runtimePolicy': self.runtime_policy_digest})


@dataclass(frozen=True, slots=True)
class MobileContext:
    operation_id: str
    request_digest: str
    repair_plan_digest: str
    project_digest: str
    application_id: str
    source_digest: str
    artifact_digest: str
    scope_digest: str
    runtime_policy_digest: str
    nonce: str = field(repr=False)
    _operation_binding: object = field(default=None, repr=False, compare=False)

    @property
    def digest(self):
        return contracts.digest({name: getattr(self, name) for name in self.__dataclass_fields__
                                 if name != '_operation_binding'})


@dataclass(frozen=True, slots=True)
class MobileInstallationObservation:
    context_digest: str
    artifact_digest: str
    application_id: str
    scope_digest: str
    evidence_digest: str
    ownership_confirmed: bool


@dataclass(frozen=True, slots=True)
class MobileFailureObservation:
    """Measured dispatch failure; final fixture/device cleanup is still required."""
    context_digest: str
    code: str
    evidence_digest: str
    effects_settled: bool


@dataclass(frozen=True, slots=True)
class MobileCleanupObservation:
    context_digest: str
    evidence_digest: str
    termination_confirmed: bool
    fixture_cleanup_confirmed: bool
    sanitation_confirmed: bool
    ownership_released: bool


@dataclass(frozen=True, slots=True)
class MobileVerificationProof:
    operation_id: str
    request_digest: str
    signed: SignedBuildProof = field(repr=False)
    validation: object = field(repr=False)
    _document: str = field(repr=False)
    _issuer: object = field(repr=False, compare=False)

    def public(self):
        return json.loads(self._document)


class _MobileCancellation(RunCancellation):
    def __init__(self, parent, run, boundary):
        super().__init__(parent, run)
        self.run, self.boundary = run, boundary

    def is_set(self):
        if super().is_set():
            return True
        try:
            self.boundary()
        except Exception:
            self.stopped.set()
            if not self.run.finished: self.run.store.cancel(self.run.operation_id, self.run.request_digest)
            return True
        return False


class ProtectedMobileSupervisor:
    """Compose independently qualified policy with one fixed native provider.

    Process-local capabilities come from the operator's QualificationAuthority.
    JSON flags, a build-guest qualification or the shared Lab cannot enable this
    stage. A stable physical scope lease and durable journal reject concurrent
    ownership and restart bypasses; native ownership remains the adapter's duty.
    Unknown callbacks or sanitation quarantine that scope with no automatic VM
    recovery. No candidate binary executes on the Mac host.
    """
    def __init__(self, *, authority, qualification, route, validation_plan, signer,
                 validators, adapter, runner, store, timeout_seconds=600, cleanup_timeout_seconds=30,
                 operations=None):
        _require(type(authority) is QualificationAuthority and type(route) is TrustedExecutionRoute
            and route.execution_class == 'mobile-device' and type(validation_plan) is TrustedValidationPlan
            and type(signer) is TrustedSigningSupervisor and signer.authority is authority
            and type(validators) is TrustedValidationAuthority and type(adapter) is TrustedMobileAdapter
            and type(runner) is ScenarioRunner and type(store) is RunStore, 'mobile_unqualified')
        _require(route.project_digest == adapter.project_digest == validators.project_digest
            and route.environment_digest == store.environment_digest
            and validation_plan.definition_digest == validators.definition_digest
            and validation_plan.definition_digest == signer.builder.validation_plan.definition_digest
            and route.artifact_policy_id == signer.artifact_policy_id
            and route.application_id == signer.policy.application_id
            and route.signing_policy_id == signer.policy.policy_id, 'mobile_policy_mismatch')
        _require(type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 900
            and type(cleanup_timeout_seconds) in (int, float) and 0 < cleanup_timeout_seconds <= 60,
            'mobile_policy_mismatch')
        self.authority, self.qualification, self.route = authority, qualification, route
        self.validation_plan, self.signer, self.validators = validation_plan, signer, validators
        self.adapter, self.runner, self.store = adapter, runner, store
        self.timeout_seconds, self.cleanup_timeout_seconds = timeout_seconds, cleanup_timeout_seconds
        if operations is not None:
            from .repair_android import AndroidTrustedMobileAdapter
            from .repair_android_operation import AndroidOperationStore
            from .repair_ios import IOSTrustedMobileAdapter
            from .ios_mobile_operation import IOSMobileOperationStore
            owner = getattr(adapter.install, '__self__', None)
            if type(operations) is AndroidOperationStore:
                _require(type(owner) is AndroidTrustedMobileAdapter and owner.config is operations.config
                    and route.platform=='android','mobile_unqualified')
                self._operation_kind='androidOperation'
            else:
                _require(type(operations) is IOSMobileOperationStore and type(owner) is IOSTrustedMobileAdapter
                    and operations.definition==owner.config.definition and route.platform=='ios'
                    and signer.policy.platform=='ios','mobile_unqualified')
                self._operation_kind='iosOperation'
            _require(operations.run_store is store and owner.operations is operations
                and owner.config.service.runner is runner
                and owner.config.scope_digest == adapter.scope_digest
                and owner.config.device_id == adapter.device_id
                and owner.config.application_id == route.application_id
                and owner.config.registration.project_digest == adapter.project_digest
                and owner.config.runtime_policy_digest == adapter.runtime_policy_digest
                and adapter.install == owner.install and adapter.replay == owner.replay
                and adapter.cleanup == owner.cleanup, 'mobile_unqualified')
        self.operations = operations
        self._issuer = object(); self._proofs = {}; self._lock = threading.RLock()

    @property
    def definition_digest(self):
        definition = {'route': self.route.definition_digest,
            'qualification': getattr(self.qualification, 'definition_digest', None),
            'validation': self.validators.definition_digest, 'signing': self.signer.definition_digest,
            'adapter': self.adapter.definition_digest, 'timeoutSeconds': self.timeout_seconds,
            'cleanupTimeoutSeconds': self.cleanup_timeout_seconds}
        if self.operations is not None:
            definition[self._operation_kind] = self.operations.configuration_digest
        return contracts.digest(definition)

    @contextmanager
    def _admit(self, context, artifacts):
        if self.operations is None:
            with self.store.repair_scope_lease('mobile-device', self.adapter.scope_digest), self.store.admit(
                    context.operation_id, context.request_digest, disk_bytes=1) as run:
                yield run, context
        else:
            arguments=(context,artifacts)
            if self._operation_kind=='iosOperation':
                arguments+= (self.adapter.install.__self__.config.read_baselines(),)
            with self.operations.admit(*arguments) as operation:
                yield operation.run, replace(context, _operation_binding=operation)

    def request(self, operation_id, input_digest):
        route = self.route
        return {'protocolVersion': 1, 'operationId': operation_id, 'backendId': route.backend_id,
            'executionClass': 'mobile-device', 'projectDigest': route.project_digest,
            'environmentDigest': route.environment_digest, 'inputKind': 'validated-artifact',
            'inputDigest': input_digest, 'recipeId': route.recipe_id, 'artifactPolicyId': route.artifact_policy_id,
            'requiredValidationIds': sorted(self.validation_plan.check_ids), 'cleanupPolicyId': route.cleanup_policy_id,
            'platform': route.platform, 'applicationId': route.application_id, 'signingPolicyId': route.signing_policy_id}

    def _authorize(self, request):
        try:
            return self.authority.authorize(request, qualification=self.qualification, execution_route=self.route,
                validation_plan=self.validation_plan, signing_policy=self.signer.policy,
                evaluated_at_ms=int(time.time() * 1000))
        except (ExecutionDenied, contracts.ContractError):
            raise RepairExecutionError('mobile_unqualified') from None

    def ready(self, *, project_digest, runtime_policy_digest, validation_recipe_ids, _observe=False):
        _require(project_digest == self.adapter.project_digest
            and runtime_policy_digest == self.adapter.runtime_policy_digest, 'mobile_policy_mismatch')
        self.signer.ready(_observe=_observe)
        try:
            self.validators.ready(project_digest=project_digest, recipe_ids=validation_recipe_ids)
        except ValidationError as error:
            raise RepairExecutionError(error.code) from None
        self._authorize(self.request('mobile_preflight', '0' * 64))
        try:
            if _observe:
                self.store.require_scope_available('mobile-device', self.adapter.scope_digest)
            else:
                with self.store.repair_scope_lease('mobile-device', self.adapter.scope_digest):
                    self.store.require_available()
        except RunDenied:
            raise RepairExecutionError('mobile_unavailable') from None
        return self.definition_digest

    @staticmethod
    def _installed(value, context):
        try:
            _require(type(value) is MobileInstallationObservation and value.context_digest == context.digest
                and value.artifact_digest == context.artifact_digest and value.application_id == context.application_id
                and value.scope_digest == context.scope_digest and value.ownership_confirmed is True,
                'mobile_quarantined')
            contracts.validate_digest(value.evidence_digest)
        except contracts.ContractError:
            raise RepairExecutionError('mobile_quarantined') from None

    @staticmethod
    def _cleaned(value, context):
        if type(value) is not MobileCleanupObservation: return False
        try:
            contracts.validate_digest(value.evidence_digest)
        except contracts.ContractError:
            return False
        return (value.context_digest == context.digest and value.termination_confirmed is True
            and value.fixture_cleanup_confirmed is True and value.sanitation_confirmed is True
            and value.ownership_released is True)

    @staticmethod
    def _failed(value, context, stage):
        codes = {'cancelled', 'mobile_timeout'} | (
            {'mobile_install_failed', 'artifact_invalid'} if stage == 'install'
            else {'mobile_replay_failed'})
        try:
            _require(type(value) is MobileFailureObservation and value.context_digest == context.digest
                and value.effects_settled is True and type(value.code) is str and value.code in codes,
                'mobile_quarantined')
            contracts.validate_digest(value.evidence_digest)
        except contracts.ContractError:
            raise RepairExecutionError('mobile_quarantined') from None

    @staticmethod
    def _validation_binding(signed, operation_id):
        return ValidationBinding(operation_id, signed.build.repair_plan_digest, signed.build.project_digest,
                                 signed.build.source_digest, signed.artifact_digest)

    def verify(self, signed, approved, *, operation_id, cancellation, boundary, progress):
        from .repair_android_operation import AndroidOperationError
        from .ios_mobile_operation import IOSMobileOperationError
        approved = self.runner.registry.require(approved)
        self.ready(project_digest=approved.project_digest, runtime_policy_digest=approved.runtime_policy_digest,
                   validation_recipe_ids=approved.qualification['validationRecipeIds'])
        _require(type(signed) is SignedBuildProof and callable(boundary) and callable(progress), 'untrusted_signature')
        self.signer.require_signed(signed, source_digest=signed.build.source_digest,
                                   repair_plan_digest=signed.build.repair_plan_digest)
        _require(signed.build.project_digest == approved.project_digest
            and signed.build.application_id == approved.original['applicationId'] == self.route.application_id,
            'mobile_policy_mismatch')
        boundary(); _require(not cancellation.is_set(), 'cancelled')
        request = self.request(operation_id, signed.validated_artifacts.blobs.digest)
        self._authorize(request)
        context = MobileContext(operation_id, contracts.digest(request), signed.build.repair_plan_digest,
            approved.project_digest, signed.build.application_id, signed.build.source_digest,
            signed.artifact_digest, self.adapter.scope_digest, self.adapter.runtime_policy_digest, secrets.token_hex(24))
        attempts = []; execution = None; validation = None
        try:
            with self._admit(context, signed.validated_artifacts.blobs) as (run, context):
                def guard():
                    boundary(); self._authorize(request); self.runner.registry.require(approved)
                stop = _MobileCancellation(cancellation, run, guard)
                deadline = time.monotonic() + self.timeout_seconds
                cleanup = None; dispatched = False; callbacks_known = True; success = False
                try:
                    _require(not stop.is_set(), 'cancelled')
                    progress('installing', {'attempts': []})
                    guard(); _require(not stop.is_set(), 'cancelled')
                    dispatched = True; callbacks_known = False
                    installed, returned = invoke_fixed(self.adapter.install, context, signed.validated_artifacts.blobs,
                        cancellation=stop, deadline_monotonic=deadline)
                    _require(returned, 'mobile_quarantined')
                    if type(installed) is MobileFailureObservation:
                        self._failed(installed, context, 'install')
                        callbacks_known = True
                        raise RepairExecutionError('cancelled' if stop.is_set() else installed.code)
                    self._installed(installed, context)
                    callbacks_known = True
                    _require(not stop.is_set() and time.monotonic() < deadline, 'cancelled' if stop.is_set() else 'mobile_timeout')
                    progress('validating', {'installationEvidenceDigest': installed.evidence_digest, 'attempts': []})
                    binding = self._validation_binding(signed, operation_id)
                    validation = self.validators.run(binding, cancellation=stop,
                        timeout_seconds=max(.001, deadline-time.monotonic()))
                    progress('validating', {'validation': validation.public(), 'attempts': []})
                    if validation.public()['status'] == 'quarantined':
                        callbacks_known = False
                        raise RepairExecutionError('validation_quarantined')
                    try:
                        self.validators.require_pass(validation, binding)
                    except ValidationError:
                        raise RepairExecutionError('cancelled' if stop.is_set() else 'regression_failed') from None
                    guard(); _require(not stop.is_set(), 'cancelled')
                    build_id = 'candidate_' + contracts.digest({'operation': operation_id, 'request': context.request_digest})[:32]
                    build = {'id': build_id, 'applicationId': signed.build.application_id, 'revision': build_id,
                        'sourceDigest': signed.build.source_digest, 'artifactDigest': signed.artifact_digest,
                        'provenance': 'trusted-build'}
                    approval = contracts.issue_substitution_approval(qualification_digest=approved.qualification_digest,
                        recording_digest=approved.recording_digest, specification_digest=approved.specification_digest,
                        candidate_build_id=build_id, candidate_build_digest=contracts.digest(build))
                    execution = self.runner.registry.authorize_candidate_build(approved, build, approval)
                    for number in range(1, approved.qualification['attemptBudget']['candidate'] + 1):
                        guard(); _require(not stop.is_set(), 'cancelled')
                        _require(time.monotonic() < deadline, 'mobile_timeout')
                        attempts.append({'attempt': number, 'status': 'running'})
                        progress('replaying', {'candidateBuild': build, 'attempts': attempts})
                        started = time.monotonic_ns(); callbacks_known = False
                        result, returned = invoke_fixed(self.adapter.replay, context, execution, number,
                            cancellation=stop, deadline_monotonic=deadline)
                        _require(returned, 'mobile_quarantined')
                        if type(result) is MobileFailureObservation:
                            self._failed(result, context, 'replay')
                            callbacks_known = True
                            attempts[-1].update(status='failed', reason=result.code)
                            raise RepairExecutionError('cancelled' if stop.is_set() else result.code)
                        try:
                            self.runner.require_finalized(result, execution, started_after_ns=started)
                        except (ScenarioError, QualificationError, contracts.ContractError):
                            attempts[-1].update(status='failed', reason='untrusted_replay')
                            raise RepairExecutionError('untrusted_replay') from None
                        callbacks_known = result.cleanup == 'complete'
                        public = result.public()
                        _require(result.run_id not in {row.get('runId') for row in attempts[:-1]}, 'untrusted_replay')
                        attempts[-1] = {**{key: value for key, value in public.items()
                            if key not in {'receipts', 'observations'}}, 'attempt': number,
                            'status': 'complete', 'resultDigest': contracts.digest(public)}
                        progress('replaying', {'attempts': attempts})
                        _require(callbacks_known, 'mobile_quarantined')
                        _require(not stop.is_set(), 'cancelled')
                    _require(all(row['valid'] is True and row['preparationKnown'] is True
                        and row['coverage'] == 'complete' and row['defect'] is False
                        and row['expected'] is True and row['verdict'] == 'observed' for row in attempts),
                        'candidate_mismatch')
                    guard(); _require(not stop.is_set() and time.monotonic() < deadline, 'cancelled' if stop.is_set() else 'mobile_timeout')
                    success = True
                finally:
                    try:
                        if execution is not None:
                            self.runner.registry.revoke_candidate_build(execution.candidate_binding)
                        progress('finalizing', {'attempts': attempts})
                    finally:
                        if dispatched or self.operations is not None:
                            cleanup, returned = invoke_fixed(self.adapter.cleanup, context,
                                cancellation=threading.Event(),
                                deadline_monotonic=time.monotonic() + self.cleanup_timeout_seconds)
                            clean = returned and self._cleaned(cleanup, context) and callbacks_known
                        else:
                            clean = True
                        if stop.is_set(): self.store.cancel(operation_id, context.request_digest)
                        run.finish('succeeded' if success else 'failed', stopped=clean)
                        _require(clean and self.store.status(operation_id)['state'] != 'quarantined', 'mobile_quarantined')
                guard(); _require(not stop.is_set(), 'cancelled')
                self.signer.require_signed(signed, source_digest=signed.build.source_digest,
                                           repair_plan_digest=signed.build.repair_plan_digest)
                self.validators.require_pass(validation, binding)
                document = {'operationId': operation_id, 'requestDigest': context.request_digest,
                    'repairPlanDigest': signed.build.repair_plan_digest, 'projectDigest': approved.project_digest,
                    'qualificationDigest': approved.qualification_digest, 'recordingDigest': approved.recording_digest,
                    'specificationDigest': approved.specification_digest, 'runtimePolicyDigest': approved.runtime_policy_digest,
                    'sourceDigest': signed.build.source_digest, 'artifactDigest': signed.artifact_digest,
                    'environmentDigest': self.route.environment_digest, 'definitionDigest': self.definition_digest,
                    'candidateBuild': build, 'validation': validation.public(), 'attempts': attempts,
                    'installationEvidenceDigest': installed.evidence_digest,
                    'cleanupEvidenceDigest': cleanup.evidence_digest, 'cleanupConfirmed': True, 'verified': False}
                proof = MobileVerificationProof(operation_id, context.request_digest, signed, validation,
                    canonical(document).decode('utf-8'), self._issuer)
                with self._lock:
                    self._proofs[operation_id] = proof
                return proof
        except (RunDenied, AndroidOperationError, IOSMobileOperationError, OSError):
            raise RepairExecutionError('mobile_unavailable') from None
        except ValidationError:
            raise RepairExecutionError('regression_failed') from None

    def require_verified(self, proof, signed, approved):
        approved = self.runner.registry.require(approved)
        self.ready(project_digest=approved.project_digest, runtime_policy_digest=approved.runtime_policy_digest,
                   validation_recipe_ids=approved.qualification['validationRecipeIds'])
        with self._lock:
            _require(type(proof) is MobileVerificationProof and proof._issuer is self._issuer
                and self._proofs.get(proof.operation_id) is proof and proof.signed is signed, 'untrusted_mobile_result')
            document = proof.public()
            _require(document['definitionDigest'] == self.definition_digest
                and document['qualificationDigest'] == approved.qualification_digest
                and document['cleanupConfirmed'] is True, 'untrusted_mobile_result')
            self.signer.require_signed(signed, source_digest=document['sourceDigest'], repair_plan_digest=document['repairPlanDigest'])
            try:
                state = self.store.status(proof.operation_id)
                _require(state['state'] == 'succeeded' and state['requestDigest'] == proof.request_digest,
                         'untrusted_mobile_result')
                self.validators.require_pass(proof.validation, self._validation_binding(signed, proof.operation_id))
            except (RunDenied, ValidationError, OSError):
                raise RepairExecutionError('untrusted_mobile_result') from None
        return document
