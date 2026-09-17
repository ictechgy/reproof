"""Explicit VM and disposable synthetic-device doubles, never actual acceptance."""
import copy
import threading
import time
from unittest import mock

from reproloop import contracts
from reproloop.execution.artifacts import ArtifactValidationAuthority, BlobSet
from reproloop.execution.backend import QualificationAuthority, REQUIRED_PROBES
from reproloop.execution.journal import RunStore
from reproloop.execution.resources import provision
from reproloop.execution.runtime import MacOSVirtualizationBackend
from reproloop.repair_execution import ProtectedBuildSupervisor
from reproloop.repair_signing import TrustedSigningSupervisor, SigningObservation, SignatureObservation
from reproloop.validation import TrustedValidationAuthority, ValidationObservation
from tests.test_execution_resources import resource_inputs
from tests.test_execution_runtime import VMDouble
from tests.test_execution_protocol import build_route


class SyntheticRepairExecution:
    def __init__(self, env):
        self.env = env; self.root = env.root
        self.installs = []; self.replays = []; self.cleanups = []; self.executions = []
        self.replay_errors = []
        self.cancel_after_replay = None; self.fail_cleanup = False; self.forge_result = False
        self.verification_result = 'pass'; self.inspection_valid = True; self.next_value = 'success'
        self.install_wait = None; self.install_returned = threading.Event()
        self.patch = mock.patch('reproloop.execution.runtime.NativeVM', VMDouble)
        self.patch.start()
        VMDouble.instances = []; VMDouble.stop_confirmed = True; VMDouble.wait_for_cancel = False
        VMDouble.started_recipe = threading.Event()
        metadata, paths = resource_inputs(self.root)
        metadata['catalog'][0]['id'] = 'build_app'
        bundle = provision(self.root / 'bundle', metadata=metadata, resources=paths)
        self.authority = QualificationAuthority()
        self.plan_document = {'schemaVersion': 1, 'id': 'repair-validation',
            'projectDigest': env.registration.project_digest, 'candidateReports': 'supplemental-only',
            'checks': [{'id': 'independent-ui', 'recipeId': 'regression_ui',
                        'kind': 'external-observation', 'evidenceSourceId': 'protected-device-observer'}]}
        self.plan = self.authority.register_validation_plan(self.plan_document)
        route = build_route(); route.update(projectDigest=env.registration.project_digest,
            environmentDigest=bundle.environment_digest, recipeId='build_app', cleanupPolicyId='dispose-overlay')
        self.build_route = self.authority.register_execution_route(route)
        self.build_qualification = self.qualify('apple-vm', 'build-guest', bundle.environment_digest)
        self.vm_store = RunStore(self.root / 'vm-state', environment_digest=bundle.environment_digest, disk_limit=1024)
        self.backend = MacOSVirtualizationBackend('apple-vm', self.authority, bundle, self.vm_store)
        unsigned = ArtifactValidationAuthority()
        unsigned.register('bounded-artifacts', paths=('product.bin',), max_bytes=4096,
                          checker=lambda blobs: blobs.entries[0][1] == b'candidate artifact')
        self.builder = ProtectedBuildSupervisor(self.backend, qualification=self.build_qualification,
            route=self.build_route, validation_plan=self.plan, artifact_authority=unsigned, application_id='ios_app')
        policy = {'schemaVersion': 1, 'id': 'test-signing', 'platform': 'ios', 'applicationId': 'ios_app',
            'identityReferenceId': 'synthetic-identity', 'entitlementsDigest': 'e' * 64,
            'provisioningReferenceId': 'synthetic-profile', 'tool': 'host-codesign-fixed',
            'candidateHooks': 'forbidden', 'artifactRelation': 'pre-post-digests'}
        self.signing_policy = self.authority.register_signing_policy(policy)
        self.signed_blobs = BlobSet((('product.bin', b'candidate artifact signed'),))
        signed = ArtifactValidationAuthority()
        signed.register('signed-artifacts', paths=('product.bin',), max_bytes=4096,
                        checker=lambda blobs: blobs == self.signed_blobs)
        sign_scope = contracts.digest({'syntheticSigning': str(self.root)})
        self.signer = TrustedSigningSupervisor(self.builder, authority=self.authority,
            policy=self.signing_policy, policy_document=policy, signer=self.sign, inspector=self.inspect,
            artifact_authority=signed, artifact_policy_id='signed-artifacts', scope_digest=sign_scope,
            store=RunStore(self.root / 'sign-state', environment_digest=sign_scope, disk_limit=1))
        self.mobile_scope = contracts.digest({'disposableSyntheticDevice': str(self.root)})
        mobile_env = contracts.digest({'syntheticEnvironment': self.mobile_scope})
        route.update(id='protected-mobile-route', executionClass='mobile-device', backendId='synthetic-device',
            environmentDigest=mobile_env, inputKind='validated-artifact', recipeId='mobile-replay',
            artifactPolicyId='signed-artifacts', cleanupPolicyId='synthetic-cleanup', platform='ios',
            applicationId='ios_app', signingPolicyId='test-signing')
        self.mobile_route = self.authority.register_execution_route(route)
        self.mobile_qualification = self.qualify('synthetic-device', 'mobile-device', mobile_env,
                                                  signing_policy_id='test-signing')
        self.mobile_store = RunStore(self.root / 'mobile-state', environment_digest=mobile_env, disk_limit=1)
        self.validators = TrustedValidationAuthority(self.plan_document)
        self.validators.register('protected-device-observer', self.validate, kind='external-observation')

    def close(self):
        self.patch.stop()

    def qualify(self, backend, kind, environment, signing_policy_id=None):
        now = int(time.time() * 1000); probes = sorted(REQUIRED_PROBES[kind])
        receipts = [self.authority.record_probe(probe_id=probe, backend_id=backend, execution_class=kind,
            environment_digest=environment, outcome='pass', evidence_digest=contracts.digest('explicit test double'),
            observed_at_ms=now) for probe in probes]
        value = {'schemaVersion': 1, 'id': 'test-' + kind, 'backendId': backend,
            'executionClass': kind, 'environmentDigest': environment,
            'issuedAtMs': now, 'expiresAtMs': now + 600000, 'probeIds': probes}
        if signing_policy_id: value['signingPolicyId'] = signing_policy_id
        return self.authority.issue_backend_qualification(value, receipts, evaluated_at_ms=now)

    def sign(self, context, artifacts, **kwargs):
        return SigningObservation(context.digest, self.signed_blobs, contracts.digest('synthetic signing'), True, True)

    def inspect(self, context, artifacts, **kwargs):
        return SignatureObservation(context.digest, self.inspection_valid and artifacts == self.signed_blobs,
                                    contracts.digest('synthetic signature inspection'), True, True)

    def validate(self, context, **kwargs):
        if self.verification_result == 'json': return {'verified': True, 'status': 'pass'}
        return ValidationObservation(context.digest, self.verification_result,
                                     contracts.digest('synthetic independent observation'), True, True)

    def install(self, context, artifacts, **kwargs):
        from reproloop.repair_mobile import MobileInstallationObservation
        self.installs.append(context)
        self.original_identity = copy.deepcopy(self.env.lab.devices['device']['capabilities']['applicationIdentity'])
        self.env.lab.devices['device']['capabilities']['applicationIdentity']['artifactDigest'] = context.artifact_digest
        self.env.observations._adapters['screen'].value = self.next_value
        if self.install_wait: self.install_wait.wait(2)
        self.install_returned.set()
        return MobileInstallationObservation(context.digest, context.artifact_digest, 'ios_app', self.mobile_scope,
                                             contracts.digest('synthetic exclusive install'), True)

    def replay(self, context, execution, number, *, cancellation, deadline_monotonic):
        from dataclasses import replace
        self.executions.append(execution)
        try:
            result = self.env.service.replay(execution, registration=self.env.registration, device_id='device',
                owner='owner', controller_id='candidate_replay', preparations=self.env.preparations(),
                cancellation=cancellation, timeout_seconds=max(.001, deadline_monotonic-time.monotonic()))
        except Exception as error:
            self.replay_errors.append((type(error).__name__, getattr(error, 'code', None)))
            raise
        self.replays.append(result)
        if self.cancel_after_replay == number: self.cancel_target.set()
        return replace(result, expected=True, defect=False) if self.forge_result else result

    def cleanup(self, context, **kwargs):
        from reproloop.repair_mobile import MobileCleanupObservation
        self.cleanups.append(context)
        if hasattr(self, 'original_identity'):
            self.env.lab.devices['device']['capabilities']['applicationIdentity'] = self.original_identity
            self.env.observations._adapters['screen'].value = 'error'
        return MobileCleanupObservation(context.digest, contracts.digest('synthetic device sanitation'),
                                         True, True, not self.fail_cleanup, True)

    def mobile(self, *, timeout=5, qualification=True, store=None):
        from reproloop.repair_mobile import ProtectedMobileSupervisor, TrustedMobileAdapter
        adapter = TrustedMobileAdapter('synthetic-test-adapter', self.mobile_scope, 'device',
            self.env.registration.project_digest, self.env.approved.runtime_policy_digest,
            self.install, self.replay, self.cleanup)
        return ProtectedMobileSupervisor(authority=self.authority,
            qualification=self.mobile_qualification if qualification else None, route=self.mobile_route,
            validation_plan=self.plan, signer=self.signer, validators=self.validators,
            adapter=adapter, runner=self.env.runner, store=store or self.mobile_store, timeout_seconds=timeout)

    def signed_build(self):
        built = self.builder.build(self.env.source_blobs, operation_id='build_candidate',
            repair_plan_digest='b' * 64, cancellation=threading.Event())
        return self.signer.sign(built, operation_id='sign_candidate', cancellation=threading.Event())

    def executor(self):
        from reproloop.repair_verification import ProtectedRepairExecutor
        return ProtectedRepairExecutor(self.builder, self.signer, self.mobile())
