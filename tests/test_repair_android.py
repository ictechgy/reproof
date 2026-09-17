"""General Android adapter contracts; only the native UI bridge is doubled."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reproloop import contracts
from reproloop.android_profile import validate_android_runtime_profile
from reproloop.device import DeviceError
from reproloop.execution.artifacts import BlobSet
from reproloop.fixtures import AdapterCapabilities, FixtureCoordinator, LoopbackFixtureAdapter
from reproloop.live.android_live import AndroidLiveProvider
from reproloop.live.authority import HostAuthority, canonical_device_fingerprint
from reproloop.live.clock_sync import ClockSynchronizer
from reproloop.live.issue_sessions import FixturePreparation
from reproloop.live.model import Lab
from reproloop.qualification import ScenarioRegistry
from reproloop.repair_android import (AndroidMobileAdapterConfig, AndroidMobileTools,
                                      AndroidTrustedMobileAdapter, PinnedAdbDevice)
from reproloop.repair_mobile import MobileContext, MobileFailureObservation, MobileInstallationObservation
from reproloop.scenario_runner import (ObservationRegistry, ScenarioRunner,
                                       StaticVariableResolver, VariableResolverRegistry)
from tests.g4_support import (LoopbackService, SECRET, SnapshotObservationAdapter,
                              qualification, runtime_policy, specification)
from tests.test_fixture_allocations import collection_policy, project_document
from tests.test_live_authority_integration import Clock, FencedProvider
from tests.test_clock_sync import FakeClock
from tests.test_worker_profiles import android_document


def native_start(provider, session, lab, permit):
    provider._check_permit(permit)
    provider._install(provider.sample_apk, provider.target_package, permit)
    provider.session, provider.lab = session, lab
    lab.publish_frame(session['id'], b'<svg/>', 'image/svg+xml', 400, 800)
    return {'ok': True}


def native_execute(provider, action, payload, permit, frame=None):
    provider._check_permit(permit)
    provider.lab.publish_frame(provider.session['id'], b'<svg/>', 'image/svg+xml', 400, 800)
    return {'ok': True, 'timing': 'best-effort'}


def native_close(provider, permit):
    provider._check_permit(permit)
    return {'ok': True}


def native_locator(provider, target):
    frame = provider.lab.frame(provider.session['id'])
    return {'target': dict(target), 'x': .5, 'y': .5,
            'frameId': frame['id'], 'geometryVersion': frame['geometryVersion'],
            'providerIncarnation': provider.provider_incarnation,
            'observedAtMs': int(time.time() * 1000)}


class AndroidAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.original = self.root / 'original.apk'; self.original.write_bytes(b'original-apk')
        self.helper = self.root / 'helper.apk'; self.helper.write_bytes(b'helper-apk')
        self.candidate = b'candidate-apk'
        self.candidate_sha = hashlib.sha256(self.candidate).hexdigest()
        self.original_sha = hashlib.sha256(self.original.read_bytes()).hexdigest()
        self.state_path = self.root / 'adb-state.json'
        self.state_path.write_text(json.dumps({'commands': [], 'installed': self.original_sha}))
        self.tools = self._tools()
        self.clock = Clock()
        process_fixture=getattr(self,'process_recovery_fixture',False)
        authority_path=(self.root/'host-authority-v1'/'authority.sqlite3' if process_fixture
                        else self.root/'authority.sqlite3')
        self.authority = HostAuthority(authority_path, clock=self.clock,
                                       lease_directory=None if process_fixture else self.root/'leases')
        received = self.authority.clock_sync.sample(); self.clock.now += 10
        sent = self.authority.clock_sync.sample()
        mapping = self.authority.clock_sync.record_exchange(
            coordinator_clock_id='integration_clock', coordinator_send_ns=received.nanoseconds,
            host_received=received, host_sent=sent, coordinator_receive_ns=sent.nanoseconds, max_drift_ppm=0)
        self.grant = self.authority.issue_parent_grant(mapping, grant_id='integration_grant',
            project_id='integration_project', controller_id='integration_controller', renewal_sequence=1,
            coordinator_deadline_ns=sent.nanoseconds + 600_000_000_000)
        self.remote = LoopbackService(self.root)
        project = project_document(); project['id'] = 'integration_project'
        project['applications'][0].update(platform='android')
        project['builds'][0]['artifactDigest'] = self.original_sha
        profile_data = android_document(self.original_sha)
        profile_data.update(projectId=project['id'], projectDigest=contracts.digest(project),
                            applicationId='ios_app', package='com.example.app')
        profile_data['artifact'].update(bytes=self.original.stat().st_size, versionCode=27)
        profile_data['capabilities']['logAdapter'] = None
        profile_data['capabilities']['observations'] = ['pixels', 'accessibility']
        self.profile = validate_android_runtime_profile(profile_data)

        def inventory_factory():
            provider = FencedProvider()
            provider.resolve_locator = lambda target: native_locator(provider, target)
            return provider

        descriptor = {'id': 'device', 'name': 'Native bridge double', 'platform': 'android',
            'kind': 'android-live', 'factory': inventory_factory,
            'capabilities': {'actions': ['tap', 'text'], 'inputMode': 'gesture-batch',
                'locatorKinds': ['accessibility-id'],
                'recordingTextTarget': {'kind': 'accessibility-id', 'value': 'account'},
                'applicationIdentity': self.profile.application_identity,
                'applicationProfile': self.profile.data, 'applicationProfileDigest': self.profile.digest},
            '_authority': {'deviceKind': 'android', 'physicalId': 'android-test'}}
        self.lab = Lab([descriptor], self.root / 'lab', authority=self.authority,
            parent_grant=self.grant, recording_clock_sync=ClockSynchronizer(FakeClock()),
            recording_wall_clock_ms=lambda: int(time.time() * 1000))
        self.registration = self.lab.register_recording_project(project, collection_policy(),
            capacity_bytes=(1024 if process_fixture else 256) * 1024 * 1024,
            journal_headroom_bytes=16 * 1024 * 1024 if process_fixture else 512 * 1024)
        runtime_root=self.lab.output/'issue-runtime-v1'/'runtimes'/project['id']
        self.fixtures = FixtureCoordinator(runtime_root/'fixtures' if process_fixture else self.root/'fixtures')
        fixture_adapter = LoopbackFixtureAdapter('fixture_service', f'http://127.0.0.1:{self.remote.port}',
            capabilities=AdapterCapabilities(True, True, 60_000))
        self.plan = self.fixtures.register_plan(self.registration, application_id='ios_app',
            fixture_id='seed_account', adapter=fixture_adapter, check_recipe_ids=('check_account',),
            cleanup_recipe_id='cleanup_account')
        self.registry = ScenarioRegistry(runtime_root/'specifications' if process_fixture else self.root/'specs')
        variables = VariableResolverRegistry(self.registration)
        variables.register('secret_text', StaticVariableResolver(SECRET))
        observations = ObservationRegistry(self.registration)
        observations.register('screen', SnapshotObservationAdapter('success'))
        self.runner = ScenarioRunner(self.lab, self.registry, variables, observations,
                                    wall_clock_ms=lambda: int(time.time() * 1000))
        self.service = self.lab.create_issue_session_service(self.fixtures,
            root=runtime_root/'sessions' if process_fixture else self.root/'issues',
            scenario_registry=self.registry, scenario_runner=self.runner)
        self.config = AndroidMobileAdapterConfig(lab=self.lab, service=self.service,
            registration=self.registration, device_id='device', owner='repair', original_profile=self.profile,
            original_apk=self.original, helper_apk=self.helper,
            helper_digest=hashlib.sha256(self.helper.read_bytes()).hexdigest(),
            preparations=(FixturePreparation(self.plan, {}),), serial='android-test', tools=self.tools,
            runtime_policy_digest=contracts.digest(runtime_policy()))
        self.adapter = AndroidTrustedMobileAdapter(self.config)
        self.context = MobileContext('mobile-op', contracts.digest('request'), 'b' * 64,
            self.registration.project_digest, 'ios_app', '1' * 64, self.candidate_sha,
            canonical_device_fingerprint('android', 'android-test'), self.config.runtime_policy_digest, 'nonce')
        self.providers = []
        def start(provider, session, lab, permit):
            self.providers.append(provider)
            return native_start(provider, session, lab, permit)
        self.patches = [patch.object(AndroidLiveProvider, 'start_authorized', start),
                        patch.object(AndroidLiveProvider, 'execute_authorized', native_execute),
                        patch.object(AndroidLiveProvider, 'close_authorized', native_close),
                        patch.object(AndroidLiveProvider, 'resolve_locator', native_locator)]
        for item in self.patches: item.start()

    def _tools(self):
        adb = self.root / 'adb-double'
        script = r"""import hashlib,json,shlex,sys,time
from pathlib import Path
state_path=Path(STATE)
state=json.loads(state_path.read_text())
args=sys.argv[1:]
state['commands'].append(args)
if args==['devices']:
    print('List of devices attached\nandroid-test\tdevice')
elif args[:2]!=['-s','android-test']:
    sys.exit(2)
elif args[2]=='delay':
    time.sleep(5)
elif args[2]=='overflow':
    sys.stdout.write('x'*(5*1024*1024))
elif args[2]=='install':
    state['installed']=hashlib.sha256(Path(args[-1]).read_bytes()).hexdigest()
    print('Success')
elif args[2]=='shell':
    command=shlex.split(args[3])
    state.setdefault('shell',[]).append(command)
    if command[:2]==['pm','path']:
        print('package:/data/app/repro/base.apk')
    elif command[0]=='sha256sum':
        print(state['installed']+'  '+command[1])
    elif command[:2]==['pm','clear']:
        state['cleared']=True
        print('Success')
    elif command[0]=='ps':
        print('NAME\ninit'+('\ncom.example.app:worker' if state.get('running') else ''))
    elif command[:2]!=['am','force-stop']:
        sys.exit(3)
else:
    sys.exit(4)
state_path.write_text(json.dumps(state))
"""
        adb.write_text('#!' + str(Path(sys.executable).resolve()) + '\n' +
                       script.replace('STATE', repr(str(self.state_path))))
        adb.chmod(0o700)
        aapt = self.root / 'aapt-double'
        aapt.write_text('#!' + str(Path(sys.executable).resolve()) + '\n' +
            "import sys\nfrom pathlib import Path\nbody=Path(sys.argv[-1]).read_bytes()\n" +
            "version=28 if body==b'candidate-apk' else 27\n" +
            "print(\"package: name='com.example.app' versionCode='%s' versionName='1'\" % version)\n")
        aapt.chmod(0o700)
        return AndroidMobileTools(adb, hashlib.sha256(adb.read_bytes()).hexdigest(),
                                  aapt, hashlib.sha256(aapt.read_bytes()).hexdigest())

    def tearDown(self):
        if getattr(self, 'adapter', None) is not None:
            if self.adapter._context is not None:
                self.adapter.cleanup(self.adapter._context, **self.bounds())
            for device in self.adapter._devices:
                device.close(deadline_monotonic=time.monotonic() + 5)
            if self.adapter._temporary is not None:
                self.adapter._temporary.cleanup()
        for item in getattr(self, 'patches', []): item.stop()
        self.registry.close(); self.fixtures.close(); self.lab.close_all()
        self.remote.close(); self.authority.close(); self.tmp.cleanup()

    def bounds(self):
        return {'cancellation': threading.Event(), 'deadline_monotonic': time.monotonic() + 60}

    def install(self):
        result = self.adapter.install(self.context, BlobSet((('candidate.apk', self.candidate),)), **self.bounds())
        self.assertIsInstance(result, MobileInstallationObservation)
        return result

    def _original_and_execution(self):
        handle = self.service.start_prepared_recording(device_id='device', owner='original',
            controller_id='original', registration=self.registration, application_id='ios_app',
            build_id='original', preparations=[FixturePreparation(self.plan, {})])
        session = self.lab.get_session(handle.session_id, 'original')
        for sequence, typed in enumerate([
            {'action': 'tap', 'target': {'kind': 'accessibility-id', 'value': 'checkout'}, 'parameters': {}},
            {'action': 'text', 'target': {'kind': 'accessibility-id', 'value': 'account'},
             'parameters': {'variableId': 'secret_text'}}], 1):
            self.runner.record_manual_input(typed, handle.session_id, 'original', session['controllerId'],
                session['epoch'], operation_id='original_' + str(sequence), sequence=sequence)
        original = self.service.stop(handle)['recording']
        approved_spec = specification(original)
        approved = self.registry.register(self.registration, original, approved_spec,
            qualification(self.registration.project, original, approved_spec, self.plan),
            runtime_policy(), fixture_plans=(self.plan,))
        build_id = 'candidate_' + contracts.digest({'operation': self.context.operation_id,
                                                    'request': self.context.request_digest})[:32]
        build = {'id': build_id, 'applicationId': 'ios_app', 'revision': build_id,
            'sourceDigest': self.context.source_digest, 'artifactDigest': self.candidate_sha,
            'provenance': 'trusted-build'}
        approval = contracts.issue_substitution_approval(qualification_digest=approved.qualification_digest,
            recording_digest=approved.recording_digest, specification_digest=approved.specification_digest,
            candidate_build_id=build_id, candidate_build_digest=contracts.digest(build))
        return self.registry.authorize_candidate_build(approved, build, approval)

    def test_install_three_replays_and_restore_keep_one_scope(self):
        execution = self._original_and_execution()
        self.install()
        scope = self.adapter._scope
        results = []
        for number in (1, 2, 3):
            results.append(self.adapter.replay(self.context, execution, number, **self.bounds()))
            self.assertEqual(self.lab.list_devices()[0]['state'], 'reserved')
            self.assertIs(self.adapter._scope, scope)
        cleaned = self.adapter.cleanup(self.context, **self.bounds())
        self.assertTrue(cleaned.ownership_released)
        self.assertEqual(len({result.run_id for result in results}), 3)
        self.assertEqual(len({id(provider) for provider in self.providers}), 3)
        self.assertTrue(all(provider.general_profile for provider in self.providers))
        self.assertEqual(self.lab.list_devices()[0]['state'], 'available')
        self.assertEqual(json.loads(self.state_path.read_text())['installed'], self.original_sha)

    def test_general_candidate_profile_binds_the_new_build(self):
        execution = self._original_and_execution()
        self.install()
        self.assertEqual(self.adapter._candidate_profile.data['buildId'], execution.build_id)
        self.assertEqual(self.adapter._candidate_profile.data['artifact']['versionCode'], 28)
        self.assertNotEqual(self.adapter._candidate_profile.digest, self.profile.digest)
        self.assertEqual(self.profile.data['buildId'], 'original')

    def test_general_original_data_is_cleared_before_release(self):
        self.install()
        result = self.adapter.cleanup(self.context, **self.bounds())
        self.assertTrue(result.sanitation_confirmed and result.ownership_released)
        state = json.loads(self.state_path.read_text())
        self.assertIn(['pm', 'clear', self.config.package], state['shell'])
        self.assertIn(['ps', '-A', '-o', 'NAME'], state['shell'])
        self.assertTrue(state['cleared'])

    def test_failed_replay_without_record_returns_cleanup_observation(self):
        self.install()
        with self.assertRaises(Exception):
            self.adapter.replay(self.context, object(), 1, **self.bounds())
        result = self.adapter.cleanup(self.context, **self.bounds())
        self.assertTrue(result.ownership_released)
        self.assertEqual(self.service.list(), [])

    def test_missing_dispatched_record_keeps_scope_and_artifact(self):
        execution = self._original_and_execution(); self.install()
        with patch.object(self.service, 'replay', side_effect=RuntimeError('native boundary double')):
            self.adapter.replay(self.context, execution, 1, **self.bounds())
        result = self.adapter.cleanup(self.context, **self.bounds())
        self.assertFalse(result.ownership_released)
        self.assertTrue(self.adapter._candidate_path.is_file())
        self.assertEqual(self.lab.list_devices()[0]['state'], 'reserved')
        # The injected failure above performed no fixture or native work.
        self.adapter._issues.clear()

    def test_original_bytes_must_match_registered_build_before_admission(self):
        self.original.write_bytes(b'changed-original')
        with self.assertRaises(Exception): replace(self.config)
        self.assertEqual(json.loads(self.state_path.read_text())['commands'], [])
        self.assertEqual(self.lab.list_devices()[0]['state'], 'available')

    def test_restore_uses_frozen_original_even_if_source_path_changes(self):
        self.install(); self.original.write_bytes(b'changed-after-install')
        result = self.adapter.cleanup(self.context, **self.bounds())
        self.assertTrue(result.ownership_released)
        self.assertEqual(json.loads(self.state_path.read_text())['installed'], self.original_sha)

    def test_live_app_subprocess_prevents_sanitation_claim(self):
        self.install()
        state = json.loads(self.state_path.read_text()); state['running'] = True
        self.state_path.write_text(json.dumps(state))
        result = self.adapter.cleanup(self.context, **self.bounds())
        self.assertFalse(result.sanitation_confirmed or result.ownership_released)
        self.assertTrue(self.adapter._candidate_path.is_file())
        state = json.loads(self.state_path.read_text()); state['running'] = False
        self.state_path.write_text(json.dumps(state))

    def test_duplicate_replay_is_rejected_before_fixture_effects(self):
        execution = self._original_and_execution(); self.install()
        self.adapter.replay(self.context, execution, 1, **self.bounds())
        records = self.service.list()
        with self.assertRaises(DeviceError):
            self.adapter.replay(self.context, execution, 1, **self.bounds())
        self.assertEqual(self.service.list(), records)

    def test_concurrent_install_and_cleanup_cannot_borrow_unstarted_scope(self):
        entered, release = threading.Event(), threading.Event()
        stage = self.adapter._stage
        result = []
        def held_stage(*args):
            entered.set(); release.wait(5); return stage(*args)
        with patch.object(self.adapter, '_stage', held_stage):
            thread = threading.Thread(target=lambda: result.append(self.adapter.install(
                self.context, BlobSet((('candidate.apk', self.candidate),)), **self.bounds())))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(DeviceError): self.install()
                cleanup = self.adapter.cleanup(self.context, **self.bounds())
                self.assertFalse(cleanup.ownership_released or cleanup.termination_confirmed)
                self.assertEqual(self.lab.list_devices()[0]['state'], 'available')
            finally:
                release.set(); thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result[0], MobileInstallationObservation)
        self.assertTrue(self.adapter.cleanup(self.context, **self.bounds()).ownership_released)

    def test_second_operation_gets_fresh_state_but_completed_context_is_denied(self):
        self.install(); self.assertTrue(self.adapter.cleanup(self.context, **self.bounds()).ownership_released)
        with self.assertRaises(DeviceError): self.install()
        self.context = replace(self.context, operation_id='mobile-op-2', nonce='nonce-2')
        self.install()
        self.assertTrue(self.adapter.cleanup(self.context, **self.bounds()).ownership_released)

    def test_invalid_artifact_cleans_without_device_effects(self):
        result = self.adapter.install(self.context, BlobSet((('wrong.apk', self.candidate),)), **self.bounds())
        self.assertIsInstance(result, MobileFailureObservation)
        self.assertTrue(result.effects_settled)
        self.assertTrue(self.adapter.cleanup(self.context, **self.bounds()).ownership_released)
        self.assertEqual(json.loads(self.state_path.read_text())['commands'], [])

    def _device(self, **overrides):
        args = dict(package=self.config.package, work_root=self.root, timeout=30, **self.bounds())
        args.update(overrides)
        device = PinnedAdbDevice('android-test', self.tools, **args)
        self.addCleanup(lambda: device.close(deadline_monotonic=time.monotonic() + 5))
        return device

    def test_pinned_adb_terminates_a_timed_out_tool(self):
        device = self._device()
        device.timeout = .2
        with self.assertRaises(DeviceError): device.adb_call('delay')
        self.assertEqual(device._owner.active_processes, 0)
        self.assertFalse(device.effects_settled)

    def test_cancelled_device_construction_does_not_dispatch(self):
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(DeviceError): self._device(cancellation=cancelled)
        self.assertEqual(json.loads(self.state_path.read_text())['commands'], [])

    def test_tool_digest_change_is_rejected_before_next_command(self):
        device = self._device()
        before = self.state_path.read_bytes()
        self.tools.adb.write_text('#!/bin/sh\nexit 0\n')
        with self.assertRaises(DeviceError): device.adb_call('delay')
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_tool_output_overflow_does_not_claim_settled_effects(self):
        device = self._device()
        with self.assertRaises(DeviceError): device.adb_call('overflow')
        self.assertEqual(device._owner.active_processes, 0)
        self.assertFalse(device.effects_settled)


if __name__ == '__main__': unittest.main()
