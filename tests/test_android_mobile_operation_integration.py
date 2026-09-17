"""Persistent mobile adapter integration; only native tools/UI are doubles."""
from dataclasses import replace
import hashlib
import json
import threading
import time
import unittest
from unittest.mock import patch

from reproloop import contracts
from reproloop.device import DeviceError
from reproloop.execution.artifacts import ArtifactValidationAuthority, BlobSet
from reproloop.execution.backend import REQUIRED_PROBES
from reproloop.execution.journal import RunStore
from reproloop.repair_android import AndroidTrustedMobileAdapter
from reproloop.repair_android_operation import AndroidOperationStore
from reproloop.repair_mobile import MobileFailureObservation, MobileInstallationObservation
from reproloop.repair_composition import ProtectedRepairComposition
from reproloop.repair_execution import RepairExecutionError
from reproloop.repair_signing import TrustedSigningSupervisor
from tests import test_repair_android as support
from tests.g9_execution_support import SyntheticRepairExecution
from tests.test_execution_protocol import build_route


class PersistentAndroidAdapterTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.AndroidAdapterTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        serial = 'android-test-' + hashlib.sha256(str(f.root).encode()).hexdigest()[:24]
        f.tools.adb.write_text(f.tools.adb.read_text().replace('android-test', serial))
        f.tools = replace(f.tools, adb_digest=hashlib.sha256(f.tools.adb.read_bytes()).hexdigest())
        f.lab.devices['device']['_authority']['physicalId'] = serial
        f.config = replace(f.config, serial=serial, tools=f.tools)
        f.context = replace(f.context, scope_digest=f.config.scope_digest)
        self.run_store = RunStore(f.root / 'mobile-runs', environment_digest='e' * 64,
                                  disk_limit=8 * 1024 * 1024)
        self.operations = AndroidOperationStore(self.run_store, f.config, f.root / 'mobile-operations')
        self.addCleanup(lambda: self.operations.close(deadline_monotonic=time.monotonic() + 1))
        self.adapter = AndroidTrustedMobileAdapter(f.config, operations=self.operations)
        f.adapter = self.adapter
        self.blobs = BlobSet((('candidate.apk', f.candidate),))

    def context(self, operation):
        context = replace(self.fixture.context, _operation_binding=operation)
        self.assertEqual(context.digest, self.fixture.context.digest)
        return context

    def cleaned(self, context):
        result = self.adapter.cleanup(context, **self.fixture.bounds())
        self.assertTrue(result.termination_confirmed and result.fixture_cleanup_confirmed
                        and result.sanitation_confirmed and result.ownership_released, result)
        return result

    def test_three_replays_bind_actual_lease_and_remove_staged_files_before_release(self):
        f = self.fixture
        execution = f._original_and_execution()
        with self.operations.admit(f.context, self.blobs) as operation:
            context = self.context(operation)
            observation = self.adapter.install(context, self.blobs, **f.bounds())
            self.assertIs(type(observation), MobileInstallationObservation)
            self.assertIsNone(self.adapter._temporary)
            self.assertEqual(self.adapter._candidate_path, operation.candidate_path)
            root = operation.staging_root.parent
            native = json.loads((root / 'native.json').read_bytes())
            handle = self.adapter._scope._reservation._authority_handle
            self.assertEqual(native['ownershipGeneration'], handle.generation)
            self.assertEqual(native['hostIncarnation'], f.authority.host_incarnation)
            self.assertEqual(native['helperIncarnation'], handle.helper_incarnation)
            self.assertEqual(json.loads((root / 'phases/install.json').read_bytes())['state'], 'completed')
            for number in (1, 2, 3):
                result = self.adapter.replay(context, execution, number, **f.bounds())
                self.assertEqual(result.cleanup, 'complete')
                self.assertEqual(f.lab.list_devices()[0]['state'], 'reserved')
                self.assertEqual(json.loads((root / f'phases/replay-{number:03d}.json').read_bytes())['state'],
                                 'completed')
            self.cleaned(context)
            self.assertEqual(list(operation.staging_root.iterdir()), [])
            self.assertEqual(json.loads((root / 'state.json').read_bytes())['stage'], 'discarded')
            self.assertGreater(self.run_store.status(context.operation_id)['reservedBytes'], 0)
            self.assertEqual(f.lab.list_devices()[0]['state'], 'available')
            self.assertEqual(json.loads(f.state_path.read_bytes())['installed'], f.original_sha)
            operation.run.finish('failed', stopped=True)
        self.assertEqual(self.run_store.status(f.context.operation_id)['reservedBytes'], 0)

    def test_cancelled_install_discards_without_native_binding_or_tools(self):
        f = self.fixture
        with self.operations.admit(f.context, self.blobs) as operation:
            context = self.context(operation)
            stop = threading.Event(); stop.set()
            result = self.adapter.install(context, self.blobs, cancellation=stop,
                                          deadline_monotonic=time.monotonic() + 5)
            self.assertIs(type(result), MobileFailureObservation)
            self.cleaned(context)
            self.assertFalse((operation.staging_root.parent / 'native.json').exists())
            self.assertEqual(list(operation.staging_root.iterdir()), [])
            self.assertEqual(json.loads(f.state_path.read_bytes())['commands'], [])
            operation.run.finish('failed', stopped=True)

    def test_cleanup_before_install_discards_only_exact_operation(self):
        f = self.fixture
        with self.operations.admit(f.context, self.blobs) as operation:
            context = self.context(operation)
            forged = replace(context, _operation_binding=replace(operation))
            with self.assertRaises(Exception):
                self.adapter.install(forged, self.blobs, **f.bounds())
            self.cleaned(context)
            self.assertFalse((operation.staging_root.parent / 'native.json').exists())
            self.assertEqual(json.loads(f.state_path.read_bytes())['commands'], [])
            self.assertEqual(list(operation.staging_root.iterdir()), [])
            operation.run.finish('failed', stopped=True)

    def test_failed_native_binding_does_not_dispatch_or_claim_cleanup(self):
        f = self.fixture
        with self.operations.admit(f.context, self.blobs) as operation:
            context = self.context(operation)
            actual = self.operations.bind_native
            with patch.object(self.operations, 'bind_native', side_effect=OSError('injected private failure')):
                result = self.adapter.install(context, self.blobs, **f.bounds())
            self.assertIs(type(result), MobileFailureObservation)
            self.assertEqual(json.loads(f.state_path.read_bytes())['commands'], [])
            self.assertFalse(self.adapter.cleanup(context, **f.bounds()).ownership_released)
            self.assertTrue(operation.candidate_path.is_file())
            # The injected failure happened before any journal/native effect.
            handle = self.adapter._scope._reservation._authority_handle
            self.adapter._native_binding = actual(operation, context, ownership_generation=handle.generation,
                host_incarnation=f.authority.host_incarnation, helper_incarnation=handle.helper_incarnation,
                provider_incarnation='provider_android_scope')
            self.cleaned(context)
            operation.run.finish('failed', stopped=True)

    def test_scope_release_failure_never_turns_file_discard_into_native_cleanup(self):
        f = self.fixture
        with self.operations.admit(f.context, self.blobs) as operation:
            context = self.context(operation)
            self.assertIs(type(self.adapter.install(context, self.blobs, **f.bounds())),
                          MobileInstallationObservation)
            with patch.object(f.service, 'release_retained_device_scope', side_effect=RuntimeError('injected')):
                result = self.adapter.cleanup(context, **f.bounds())
            self.assertFalse(result.ownership_released)
            self.assertEqual(list(operation.staging_root.iterdir()), [])
            self.assertEqual(f.lab.list_devices()[0]['state'], 'reserved')
            operation.run.finish('failed', stopped=False)
        self.assertEqual(self.run_store.status(f.context.operation_id)['state'], 'quarantined')
        self.assertGreater(self.run_store.status(f.context.operation_id)['reservedBytes'], 0)

    def test_fixture_payload_cannot_change_after_operation_configuration(self):
        f = self.fixture
        f.config.preparations[0].payload['seed'] = 'changed-owned-payload'
        try:
            with self.assertRaises(Exception):
                with self.operations.admit(f.context, self.blobs):
                    pass
            self.assertEqual(json.loads(f.state_path.read_bytes())['commands'], [])
        finally:
            f.config.preparations[0].payload.clear()

    def test_failed_final_journal_write_keeps_budget_after_native_cleanup(self):
        f = self.fixture
        with self.operations.admit(f.context, self.blobs) as operation:
            context = self.context(operation)
            self.assertIs(type(self.adapter.install(context, self.blobs, **f.bounds())),
                          MobileInstallationObservation)
            with patch.object(self.operations, 'complete_phase', side_effect=OSError('injected fsync failure')):
                result = self.adapter.cleanup(context, **f.bounds())
            self.assertEqual(f.lab.list_devices()[0]['state'], 'available')
            self.assertFalse(result.ownership_released)
            operation.run.finish('failed', stopped=False)
        self.assertEqual(self.run_store.status(f.context.operation_id)['state'], 'quarantined')
        self.assertGreater(self.run_store.status(f.context.operation_id)['reservedBytes'], 0)

    def protected(self):
        f = self.fixture
        execution = f._original_and_execution()
        approved = execution.approved
        f.registry.revoke_candidate_build(execution.candidate_binding)
        runtime = SyntheticRepairExecution(f)
        self.addCleanup(runtime.close)
        runtime.signed_blobs = self.blobs
        policy = {'schemaVersion': 1, 'id': 'android-signing-test', 'platform': 'android',
            'applicationId': f.config.application_id, 'identityReferenceId': 'android-identity-double',
            'entitlementsDigest': 'e' * 64, 'tool': 'host-apksigner-fixed',
            'candidateHooks': 'forbidden', 'artifactRelation': 'pre-post-digests'}
        signing_policy = runtime.authority.register_signing_policy(policy)
        artifacts = ArtifactValidationAuthority()
        artifacts.register('signed-apk', paths=('candidate.apk',), max_bytes=4096,
                           checker=lambda blobs: blobs == self.blobs)
        scope = contracts.digest({'androidSigningDouble': str(f.root)})
        signer = TrustedSigningSupervisor(runtime.builder, authority=runtime.authority,
            policy=signing_policy, policy_document=policy, signer=runtime.sign, inspector=runtime.inspect,
            artifact_authority=artifacts, artifact_policy_id='signed-apk', scope_digest=scope,
            store=RunStore(f.root / 'android-signing-state', environment_digest=scope, disk_limit=1))
        route = build_route()
        route.update(id='android-journal-route', projectDigest=f.registration.project_digest,
            executionClass='mobile-device', backendId='android-test-backend', environmentDigest='e' * 64,
            inputKind='validated-artifact', recipeId='mobile-replay', artifactPolicyId='signed-apk',
            cleanupPolicyId='android-cleanup', platform='android', applicationId=f.config.application_id,
            signingPolicyId=policy['id'])
        now = int(time.time() * 1000)
        probes = sorted(REQUIRED_PROBES['mobile-device'])
        receipts = [runtime.authority.record_probe(probe_id=probe, backend_id='android-test-backend',
            execution_class='mobile-device', environment_digest='e' * 64, outcome='pass',
            evidence_digest=contracts.digest('explicit mobile boundary test double'),
            observed_at_ms=now) for probe in probes]
        qualification = runtime.authority.issue_backend_qualification({'schemaVersion': 1,
            'id': 'android-journal-test-qualification', 'backendId': 'android-test-backend',
            'executionClass': 'mobile-device', 'environmentDigest': 'e' * 64,
            'issuedAtMs': now, 'expiresAtMs': now + 600000, 'probeIds': probes,
            'signingPolicyId': policy['id']}, receipts, evaluated_at_ms=now)
        owner = ProtectedRepairComposition(authority=runtime.authority)
        self.addCleanup(owner.close)
        args = dict(config=f.config, signer=signer, validators=runtime.validators,
                    qualification=qualification, route_document=route, store=self.run_store,
                    work_root=f.root / 'mobile-operations', adapter_id='owned-android-mobile',
                    timeout_seconds=20)
        return owner, args, runtime, approved

    def test_protected_composition_runs_budgeted_callback_threads_and_three_replays(self):
        owner, args, runtime, approved = self.protected()
        supervisor = owner.configure_android_mobile(**args)
        self.adapter = self.fixture.adapter = supervisor.adapter.install.__self__
        source = BlobSet((('src/owned.py', b'public bounded fixture'),))
        build = runtime.builder.build(source, operation_id='journal-build', repair_plan_digest='b' * 64,
                                      cancellation=threading.Event())
        signed = args['signer'].sign(build, operation_id='journal-sign', cancellation=threading.Event())
        progress = []
        proof = supervisor.verify(signed, approved, operation_id='journal-supervised',
            cancellation=threading.Event(), boundary=lambda: None,
            progress=lambda phase, data: progress.append(phase))
        supervisor.require_verified(proof, signed, approved)
        self.assertEqual(len(proof.public()['attempts']), 3)
        self.assertFalse(proof.public()['verified'])
        root = self.fixture.root / 'mobile-operations/operations/journal-supervised'
        state = json.loads((root / 'state.json').read_bytes())
        self.assertEqual(state['stage'], 'discarded')
        self.assertEqual(set(state['phases']), {'install', 'replay-001', 'replay-002', 'replay-003', 'cleanup'})
        self.assertTrue(all(row['state'] == 'completed' for row in state['phases'].values()))
        self.assertGreater(json.loads((root / 'intent.json').read_bytes())['reservedBytes'], 512 * 1024)
        self.assertEqual(self.run_store.status('journal-supervised')['state'], 'succeeded')
        self.assertEqual(self.run_store.status('journal-supervised')['reservedBytes'], 0)
        owner.close()
        self.assertEqual(owner.status()['cleanupPending'], 0)

    def test_supervisor_cancellation_before_dispatch_discards_admitted_payload(self):
        owner, args, runtime, approved = self.protected()
        supervisor = owner.configure_android_mobile(**args)
        self.adapter = self.fixture.adapter = supervisor.adapter.install.__self__
        source = BlobSet((('src/owned.py', b'public bounded fixture'),))
        build = runtime.builder.build(source, operation_id='journal-build', repair_plan_digest='b' * 64,
                                      cancellation=threading.Event())
        signed = args['signer'].sign(build, operation_id='journal-sign', cancellation=threading.Event())
        stop = threading.Event()
        def progress(phase, data):
            if phase == 'installing':
                stop.set()
        with self.assertRaises(RepairExecutionError) as caught:
            supervisor.verify(signed, approved, operation_id='journal-cancelled', cancellation=stop,
                              boundary=lambda: None, progress=progress)
        self.assertEqual(caught.exception.code, 'cancelled')
        root = self.fixture.root / 'mobile-operations/operations/journal-cancelled'
        self.assertFalse((root / 'native.json').exists())
        self.assertEqual(list((root / 'staging').iterdir()), [])
        self.assertEqual(self.run_store.status('journal-cancelled')['state'], 'cancelled')
        self.assertEqual(self.run_store.status('journal-cancelled')['reservedBytes'], 0)
        self.assertEqual(json.loads(self.fixture.state_path.read_bytes())['commands'], [])

    def test_composition_rejects_saved_qualification_and_wrong_platform(self):
        owner, args, runtime, approved = self.protected()
        for override in ({'qualification': {'qualified': True}},
                         {'route_document': {**args['route_document'], 'platform': 'ios'}}):
            with self.subTest(override=next(iter(override))):
                with self.assertRaises(RepairExecutionError):
                    owner.configure_android_mobile(**{**args, **override})
        self.assertEqual(owner.status()['cleanupPending'], 0)
        self.assertEqual(json.loads(self.fixture.state_path.read_bytes())['commands'], [])


if __name__ == '__main__':
    unittest.main()
