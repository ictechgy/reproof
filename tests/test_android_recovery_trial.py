"""Inert boundaries only: no sockets, subprocess, AVD, signer, or credentials."""
import copy
import hashlib
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from reproof.core import ContractError
from reproof.live import android_recovery_trial as trial
from reproof.live import android_recovery_qualification as qualification
from reproof.live import android_recovery_transport as transport
from reproof.live.android_live import AndroidLiveProvider
from reproof.live.authority import HostAuthority
from reproof.live.model import LiveError
from reproof.android_profile import validate_android_runtime_profile
from tests.test_android_recovery_qualification import FakeProducer, FakeProbe
from tests.test_android_launch import fixture
from tests import test_android_recovery_birth as birth_fixtures


class _Context:
    def require_bound(self, *args):
        pass

    def call_helper(self, path, body, **kwargs):
        kwargs['guard']()
        if path == '/command':
            self.command = copy.deepcopy(body)
            return {'accepted': True}
        if path.startswith('/ack/'):
            command = self.command
            return {'pending': False, 'id': command['id'], 'ok': True,
                    'timing': 'best-effort', 'authority': copy.deepcopy(command['authority'])}
        return {'status': 'untouched'}


def _bridge(context=None):
    bridge = object.__new__(transport._QualificationBridge)
    bridge.context = context or _Context()
    bridge.token = 'synthetic-not-a-credential'
    bridge._ordinary_guard = lambda: None
    bridge._probe = False
    return bridge


class AckOmissionTests(unittest.TestCase):
    def setUp(self):
        self.operation = 'operation_' + 'a' * 40
        self.bridge = _bridge()
        self.hook = trial._TapAckLoss(self.bridge, self.operation)
        self.command = {'id': self.operation, 'action': 'tap', 'payload': {'x': .5, 'y': .5},
                        'geometryVersion': 1, 'authority': {'generation': 1}}

    def test_only_exact_terminal_response_is_consumed_once(self):
        self.assertEqual(self.hook.call('/status'), {'status': 'untouched'})
        self.hook.call('/command', self.command)
        with self.assertRaisesRegex(LiveError, 'deliberately withheld'):
            self.hook.call('/ack/' + self.operation)
        self.assertEqual(self.hook.evidence()['deliberatelyOmittedResponses'], 1)
        self.assertFalse(self.hook.evidence()['naturalLossProven'])
        with patch.object(self.bridge.context, 'call_helper') as call, self.assertRaises(ContractError):
            self.hook.call('/ack/' + self.operation)
        call.assert_not_called()
        self.assertEqual(self.hook.evidence()['deliberatelyOmittedResponses'], 1)

    def test_wrong_command_is_rejected_before_transport(self):
        for key, value in [('id', 'operation_'+'b'*40), ('action', 'home'), ('payload', {'x': 0., 'y': .5})]:
            command = copy.deepcopy(self.command);command[key] = value
            with self.subTest(key=key), patch.object(self.bridge.context, 'call_helper') as call:
                with self.assertRaises(ContractError):self.hook.call('/command', command)
                call.assert_not_called()
        self.assertEqual(self.hook.evidence()['deliberatelyOmittedResponses'], 0)

    def test_pending_rejected_and_malformed_are_not_counted_as_drops(self):
        variants = [{'pending': True}, {'pending': True, 'extra': 1},
                    {'pending': False, 'id': self.operation, 'ok': False, 'timing': 'best-effort', 'authority': {'generation': 1}},
                    {'pending': False, 'id': self.operation, 'ok': True, 'timing': 'best-effort', 'authority': {'generation': True}},
                    {'pending': False, 'id': 'other', 'ok': True, 'timing': 'best-effort', 'authority': {'generation': 1}}]
        for response in variants:
            hook = trial._TapAckLoss(self.bridge, self.operation)
            hook.call('/command', self.command)
            with patch.object(self.bridge.context, 'call_helper', return_value=response):
                if response.get('pending') is True and len(response) > 1:
                    with self.assertRaises(ContractError):hook.call('/ack/'+self.operation)
                else:
                    self.assertEqual(hook.call('/ack/'+self.operation), response)
            self.assertEqual(hook.evidence()['deliberatelyOmittedResponses'], 0)

    def test_recovery_status_and_frames_bypass_fault_policy(self):
        guard = Mock()
        with patch.object(self.bridge.context, 'call_helper', return_value={'pending': True}) as call:
            self.assertEqual(self.hook.call('/qualification-recovery/status', guard=guard), {'pending': True})
        self.assertIs(call.call_args.kwargs['guard'], guard)
        self.assertEqual(self.hook.evidence()['deliberatelyOmittedResponses'], 0)

    def test_other_thread_cannot_deliberately_drop_input(self):
        errors = []
        def attempt():
            try:self.hook.call('/command', self.command)
            except ContractError:errors.append(True)
        selected = threading.Thread(target=attempt);selected.start();selected.join()
        self.assertEqual(errors, [True])


class _InputProducer(FakeProducer):
    """Use actual provider input dispatch; fake only Android startup/native I/O."""
    _execute = AndroidLiveProvider._execute

    def start_authorized(self, session, lab, permit):
        result = super().start_authorized(session, lab, permit)
        self.stop = threading.Event()
        self.general_profile = False
        self.transport = _bridge()
        self.lab.publish_frame(self.owner.session_id, b'\xff\xd8inert\xff\xd9', 'image/jpeg', 2, 2)
        return result

    def _check_permit(self, permit):
        self.device_authority.check_dispatch_permit(permit)

    def execute_authorized(self, action, payload, permit, frame=None):
        self._check_permit(permit)
        return self._execute(action, payload, geometry=1, permit=permit)


class PrivateTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qt-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        # Reuse the public inert manifest builder, not its test cases or effects.
        builder = object.__new__(birth_fixtures.AndroidRecoveryBirthTests)
        builder.root = self.root
        image = birth_fixtures.birth
        properties = b'AndroidVersion.ApiLevel=36\nSystemImage.Abi=arm64-v8a\nSystemImage.TagId=default\nPkg.Revision=2\n'
        builder.synthetic_layout = {name: len(properties) if name == 'source.properties' else len(b'synthetic')
                                    for name in image._AOSP_R2_FILE_SIZES}
        for name, value in (('_AOSP_R2_FILE_SIZES', builder.synthetic_layout),
                            ('_AOSP_R2_EXPANDED_BYTES', sum(builder.synthetic_layout.values()))):
            selected = patch.object(image, name, value)
            selected.start(); self.addCleanup(selected.stop)
        self.plan = builder.plan()
        profile, _ = fixture();data = profile.data
        data['package'] = 'io.reproof.sample'
        data['launchTarget']['value'] = 'io.reproof.sample.MainActivity'
        data['artifact']['sha256'] = self.plan.sample_apk.sha256
        data['artifact']['bytes'] = self.plan.sample_apk.size
        self.profile = validate_android_runtime_profile(data)
        self.context = _Context()
        self.owner = SimpleNamespace(root=self.root, deadline=time.monotonic()+900,
            context=self.context, require_live=Mock(), collect=Mock(return_value={'processesCollected':True}),
            abort=Mock(return_value={'abortOnly':True, 'processesCollected':True,
                                    'recoverySucceeded':False, 'authorityReleased':False}))
        self.harnesses = []
        create = qualification.QualificationHarness.create
        def tracked(*args, **kwargs):
            obj = create(*args, **kwargs);self.harnesses.append(obj);return obj
        self.patches = [
            patch.object(trial, 'run_sandbox_preflight', return_value={'passed': True, 'syntheticBoundary': True}),
            patch.object(trial, 'launch_qualification_birth', return_value=self.owner),
            patch.object(qualification, 'HostAuthority', side_effect=lambda path:HostAuthority(path, lease_directory=self.root/'leases')),
            patch('reproof.live.android_recovery_context.IsolatedAdbContext', _Context),
            patch.object(transport, 'QualificationAndroidProvider', _InputProducer),
            patch.object(transport, '_ProbeProducer', FakeProbe),
            patch.object(qualification.QualificationHarness, 'create', side_effect=tracked)]
        for item in self.patches:item.start()
        self.addCleanup(self.cleanup_harnesses)

    def test_preflight_failure_prevents_birth_and_authority_creation(self):
        with patch.object(trial, 'run_sandbox_preflight', side_effect=ContractError('sandbox denied')), \
                patch.object(trial, 'launch_qualification_birth') as launch, \
                patch.object(qualification.QualificationHarness, 'create') as create:
            with self.assertRaisesRegex(ContractError, 'sandbox denied'):
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt,
                                       cancellation=threading.Event())
        launch.assert_not_called()
        create.assert_not_called()

    def cleanup_harnesses(self):
        for item in reversed(self.patches):item.stop()
        import os
        for harness in self.harnesses:
            if not harness._closed:
                try:harness.authority.close()
                except ContractError:pass
                if harness._journal is not None:harness._journal.close()
                os.close(harness._root_fd);os.close(harness._parent_fd)

    def test_real_lab_input_unknown_and_private_recovery_composition(self):
        with patch.object(qualification.Lab, 'fail', side_effect=AssertionError('No fabricated failure')):
            report = trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt, cancellation=threading.Event())
        self.assertTrue(report['trialPassed'])
        self.assertEqual(report['deliberatelyOmittedResponses'], 1)
        self.assertEqual(report['originalInputOutcome'], 'unknown')
        self.assertFalse(report['naturalLossProven'])
        self.assertFalse(report['normalRecoveryEnabled'])
        harness = self.harnesses[0]
        receipt = next(iter(harness._session['receipts'].values()))[1]
        self.assertEqual(receipt['status'], 'unknown')
        self.assertTrue(harness._closed)
        self.assertTrue(harness._journal._closed)
        self.assertEqual(harness._lab.devices[harness.device_id]['state'], 'quarantined')
        self.owner.collect.assert_called_once();self.owner.abort.assert_not_called()

    def test_default_advisory_choice_is_forwarded_without_enabling_it(self):
        cancel = threading.Event()
        with patch.object(trial, 'launch_qualification_birth', return_value=self.owner) as launch:
            report = trial.run_one_lost_ack(self.plan, self.profile,
                sandbox_probe=self.plan.aapt, cancellation=cancel)
        launch.assert_called_once_with(self.plan, cancellation=cancel,
                                       allow_vulkan_virtual_queue_advisory=False, allow_jdwp_icebox_advisory=False)
        self.assertFalse(report['vulkanAdvisoryRequested'])
        self.assertFalse(report['jdwpAdvisoryRequested'])
        self.assertTrue(report['trialPassed'])
        self.assertFalse(report['birthDiagnostics']['outputAlert'])

    def test_explicit_advisory_choice_reaches_birth_and_preserves_one_fault(self):
        import io
        from reproof.live.android_recovery_diagnostics import IncrementalCollector, _VULKAN_ADVISORY
        collector = IncrementalCollector(io.BytesIO(_VULKAN_ADVISORY), _vulkan_advisory=True)
        collector.thread.start();collector.thread.join(2)
        self.owner._children = [SimpleNamespace(collectors=[collector], _diagnostic_role='emulator')]
        cancel = threading.Event()
        with patch.object(trial, 'launch_qualification_birth', return_value=self.owner) as launch:
            report = trial.run_one_lost_ack(self.plan, self.profile,
                sandbox_probe=self.plan.aapt, cancellation=cancel,
                allow_vulkan_virtual_queue_advisory=True)
        launch.assert_called_once_with(self.plan, cancellation=cancel,
                                       allow_vulkan_virtual_queue_advisory=True, allow_jdwp_icebox_advisory=False)
        self.assertTrue(report['vulkanAdvisoryRequested'])
        self.assertTrue(report['trialPassed'])
        self.assertEqual(report['deliberatelyOmittedResponses'], 1)
        self.assertEqual(report['originalInputOutcome'], 'unknown')
        self.assertFalse(report['normalRecoveryEnabled'])
        stream = report['birthDiagnostics']['processes'][0]['streams'][0]
        self.assertTrue(stream['vulkanAdvisoryEnabled'])
        self.assertEqual(stream['vulkanAdvisoriesAccepted'], 1)
        self.assertTrue(stream['complete'])
        self.assertNotIn('Please update', str(report))
        self.owner.collect.assert_called_once()
        self.owner.abort.assert_not_called()

    def test_both_advisory_choices_and_counters_reach_success_report(self):
        import io
        from reproof.live.android_recovery_diagnostics import IncrementalCollector, _VULKAN_ADVISORY, _JDWP_ADVISORY
        collector = IncrementalCollector(io.BytesIO(_JDWP_ADVISORY+_VULKAN_ADVISORY),
                                         _vulkan_advisory=True, _jdwp_advisory=True)
        collector.thread.start();collector.thread.join(2)
        self.owner._children = [SimpleNamespace(collectors=[collector], _diagnostic_role='emulator')]
        cancel = threading.Event()
        with patch.object(trial, 'launch_qualification_birth', return_value=self.owner) as launch:
            report = trial.run_one_lost_ack(self.plan, self.profile,
                sandbox_probe=self.plan.aapt, cancellation=cancel,
                allow_vulkan_virtual_queue_advisory=True, allow_jdwp_icebox_advisory=True)
        launch.assert_called_once_with(self.plan, cancellation=cancel,
            allow_vulkan_virtual_queue_advisory=True, allow_jdwp_icebox_advisory=True)
        self.assertTrue(report['trialPassed'])
        self.assertTrue(report['vulkanAdvisoryRequested']);self.assertTrue(report['jdwpAdvisoryRequested'])
        stream = report['birthDiagnostics']['processes'][0]['streams'][0]
        self.assertEqual(stream['vulkanAdvisoriesAccepted'], 1)
        self.assertEqual(stream['jdwpAdvisoriesAccepted'], 1)
        self.assertNotIn('Icebox will', str(report))
        self.assertEqual(report['deliberatelyOmittedResponses'], 1)
        self.assertFalse(report['normalRecoveryEnabled'])

    def test_jdwp_choice_alone_is_forwarded_on_failure_without_retry(self):
        cancel = threading.Event()
        with patch.object(trial, 'launch_qualification_birth',
                          side_effect=trial.BirthIncomplete(self.owner)) as launch:
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt,
                    cancellation=cancel, allow_jdwp_icebox_advisory=True)
        launch.assert_called_once_with(self.plan, cancellation=cancel,
            allow_vulkan_virtual_queue_advisory=False, allow_jdwp_icebox_advisory=True)
        self.assertFalse(caught.exception.report['vulkanAdvisoryRequested'])
        self.assertTrue(caught.exception.report['jdwpAdvisoryRequested'])
        self.assertFalse(caught.exception.report['trialPassed'])
        self.owner.abort.assert_called_once()

    def test_nonboolean_jdwp_option_fails_before_profile_and_preflight(self):
        for option in (None, 0, 1, 'true', {}, []):
            with patch.object(trial, 'run_sandbox_preflight') as preflight, \
                 patch.object(trial, 'launch_qualification_birth') as launch, \
                 patch.object(trial, 'validate_android_runtime_profile') as profile:
                with self.assertRaises(ContractError):
                    trial.run_one_lost_ack(self.plan, {}, sandbox_probe=self.plan.aapt,
                        cancellation=threading.Event(), allow_jdwp_icebox_advisory=option)
                preflight.assert_not_called();launch.assert_not_called();profile.assert_not_called()

    def test_nonboolean_advisory_choice_fails_before_preflight_or_profile_parse(self):
        for option in (None, 0, 1, 'true', {}, []):
            with self.subTest(option=option), \
                 patch.object(trial, 'run_sandbox_preflight') as preflight, \
                 patch.object(trial, 'launch_qualification_birth') as launch, \
                 patch.object(trial, 'validate_android_runtime_profile') as profile:
                with self.assertRaises(ContractError):
                    trial.run_one_lost_ack(self.plan, {}, sandbox_probe=self.plan.aapt,
                        cancellation=threading.Event(), allow_vulkan_virtual_queue_advisory=option)
                preflight.assert_not_called();launch.assert_not_called();profile.assert_not_called()

    def test_advisory_birth_failure_does_not_retry_or_lose_choice(self):
        cancel = threading.Event()
        with patch.object(trial, 'launch_qualification_birth',
                          side_effect=trial.BirthIncomplete(self.owner)) as launch:
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt,
                    cancellation=cancel, allow_vulkan_virtual_queue_advisory=True)
        launch.assert_called_once_with(self.plan, cancellation=cancel,
                                       allow_vulkan_virtual_queue_advisory=True, allow_jdwp_icebox_advisory=False)
        self.assertTrue(caught.exception.report['vulkanAdvisoryRequested'])
        self.assertFalse(caught.exception.report['trialPassed'])
        self.owner.abort.assert_called_once()
        self.assertIn('birthDiagnostics', caught.exception.report)

    def test_success_summary_failure_cannot_leave_passed_report(self):
        with patch.object(trial, 'birth_summary', side_effect=[
                RuntimeError('synthetic summary failure'), {'schemaVersion': 1}]) as summary:
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt,
                    cancellation=threading.Event(), allow_vulkan_virtual_queue_advisory=True)
        self.assertFalse(caught.exception.report['trialPassed'])
        self.assertEqual(caught.exception.report['phase'], 'collection')
        self.assertEqual(summary.call_count, 2)
        self.owner.collect.assert_called_once()
        self.owner.abort.assert_called_once()

    def test_native_recovery_failure_uses_only_abort_and_preserves_unknown(self):
        with patch.object(FakeProbe, 'start', side_effect=ContractError('synthetic probe failure')):
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt, cancellation=threading.Event())
        self.assertFalse(caught.exception.report['trialPassed'])
        self.owner.abort.assert_called_once();self.owner.collect.assert_not_called()
        harness = self.harnesses[0]
        self.assertEqual(next(iter(harness._session['receipts'].values()))[1]['status'], 'unknown')
        self.assertEqual(harness._journal.state, 'blocked')
        self.assertFalse(caught.exception.report['abort']['authorityReleased'])
        self.assertEqual(caught.exception.report['cleanupScope'], 'local-processes-and-pollers-only')
        self.assertEqual(caught.exception.report['authorityDisposition'], 'release-unconfirmed')
        self.assertTrue(caught.exception.report['liveDiagnosticOwnerRetained'])

    def test_bad_profile_or_cancel_has_no_birth_effect(self):
        data = self.profile.data;data['artifact']['sha256'] = 'f'*64
        with patch.object(trial, 'launch_qualification_birth') as launch:
            with self.assertRaises(ContractError):trial.run_one_lost_ack(self.plan, data, sandbox_probe=self.plan.aapt, cancellation=threading.Event())
            cancel = threading.Event();cancel.set()
            with self.assertRaises(ContractError):trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt, cancellation=cancel)
        launch.assert_not_called()

    def test_cancellation_after_real_ack_keeps_unknown_and_aborts(self):
        cancel = threading.Event()
        original = _Context.call_helper
        def cancelled(context, path, body, **kwargs):
            value = original(context, path, body, **kwargs)
            if path.startswith('/ack/'):
                cancel.set()
            return value
        with patch.object(_Context, 'call_helper', cancelled):
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt, cancellation=cancel)
        self.assertEqual(caught.exception.report['deliberatelyOmittedResponses'], 1)
        self.assertEqual(caught.exception.report['originalInputOutcome'], 'unknown')
        self.owner.abort.assert_called_once()
        self.assertNotEqual(self.harnesses[0]._journal.state, 'released')

    def test_rejected_ack_is_not_misreported_as_intentional_loss(self):
        original = _Context.call_helper
        def rejected(context, path, body, **kwargs):
            value = original(context, path, body, **kwargs)
            if path.startswith('/ack/'):
                value['ok'] = False
            return value
        with patch.object(_Context, 'call_helper', rejected):
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt, cancellation=threading.Event())
        self.assertEqual(caught.exception.report['deliberatelyOmittedResponses'], 0)
        self.assertFalse(caught.exception.report['trialPassed'])
        self.assertNotEqual(self.harnesses[0]._journal.state, 'released')

    def test_known_native_rejection_keeps_ordinary_rejected_semantics(self):
        from reproof.live.android_live import REJECTED_CODES
        original = _Context.call_helper
        def rejected(context, path, body, **kwargs):
            value = original(context, path, body, **kwargs)
            if path.startswith('/ack/'):
                value.update(ok=False, error=sorted(REJECTED_CODES)[0])
            return value
        with patch.object(_Context, 'call_helper', rejected):
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt, cancellation=threading.Event())
        self.assertEqual(caught.exception.report['deliberatelyOmittedResponses'], 0)
        session = self.harnesses[0]._session
        self.assertEqual(next(iter(session['receipts'].values()))[1]['status'], 'rejected')
        self.assertEqual(session['state'], 'active')
        self.owner.abort.assert_called_once()

    def test_incomplete_birth_owner_is_retained_and_abort_failure_reported(self):
        self.owner.abort.side_effect = ContractError('synthetic uncollected child')
        with patch.object(trial, 'launch_qualification_birth', side_effect=trial.BirthIncomplete(self.owner)):
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt, cancellation=threading.Event())
        self.assertIs(caught.exception.owner, self.owner)
        self.assertIsNone(caught.exception.harness)
        self.assertTrue(caught.exception.report['cleanupIncomplete'])
        self.assertFalse(caught.exception.report['abort']['recoverySucceeded'])

    def test_poller_failure_does_not_prevent_owned_abort(self):
        with patch.object(trial, '_stop_pollers', return_value=False):
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt, cancellation=threading.Event())
        self.assertTrue(caught.exception.report['privateAuthorityReleased'])
        self.assertFalse(caught.exception.report['trialPassed'])
        self.assertTrue(caught.exception.report['cleanupIncomplete'])
        self.owner.abort.assert_called_once()

    def test_poller_collection_continues_after_one_join_error(self):
        first = SimpleNamespace(stop=Mock(), thread=Mock())
        first.thread.join.side_effect = RuntimeError('not started')
        second = SimpleNamespace(stop=Mock(), thread=Mock())
        second.thread.is_alive.return_value = False
        self.assertFalse(trial._stop_pollers(SimpleNamespace(_provider=first, _probe=second)))
        first.stop.set.assert_called_once();second.stop.set.assert_called_once()
        second.thread.join.assert_called_once()

    def test_without_hook_actual_input_ack_is_injected_not_quarantined(self):
        harness = qualification.QualificationHarness.create(self.root,
            serial='emulator-'+str(self.plan.console_port), helper_apk=self.plan.helper_apk.path,
            app_apk=self.plan.sample_apk.path, runtime_profile=self.profile,
            grant_provider=trial._grant, context=self.context)
        harness.start()
        session = harness._session
        frame = harness._lab._frame_meta(session)
        command = {'controllerId': session['controllerId'], 'epoch': session['epoch'],
                   'sequence': session['lastSequence']+1, 'commandId': 'operation_'+'b'*40,
                   'frameId': frame['id'], 'geometryVersion': frame['geometryVersion'],
                   'action': 'tap', 'payload': {'x': .5, 'y': .5}}
        result = harness._lab.input(harness.session_id, 'qualification', command)
        self.assertEqual(result['status'], 'injected')
        self.assertEqual(session['state'], 'active')
        self.assertNotEqual(harness._lab.devices[harness.device_id]['state'], 'quarantined')

    def test_cancellation_during_recovery_reaches_real_issuer(self):
        cancel = threading.Event()
        original = FakeProbe.start
        def cancelled(producer):
            result = original(producer)
            cancel.set()
            return result
        with patch.object(FakeProbe, 'start', cancelled):
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt, cancellation=cancel)
        harness = self.harnesses[0]
        self.assertIs(harness.cancel, cancel)
        self.assertEqual(harness._journal.state, 'blocked')
        self.assertEqual(caught.exception.report['originalInputOutcome'], 'unknown')
        self.assertFalse(caught.exception.report.get('privateAuthorityReleased', False))
        self.owner.abort.assert_called_once()

    def test_owner_deadline_after_ack_preserves_unknown_without_release(self):
        original = _Context.call_helper
        def expires(context, path, body, **kwargs):
            value = original(context, path, body, **kwargs)
            if path.startswith('/ack/'):
                self.owner.deadline = time.monotonic()-1
            return value
        def live():
            if time.monotonic() >= self.owner.deadline:
                raise ContractError('synthetic owner expired at actual monotonic time')
        self.owner.require_live.side_effect = live
        with patch.object(_Context, 'call_helper', expires):
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt, cancellation=threading.Event())
        self.assertEqual(caught.exception.report['originalInputOutcome'], 'unknown')
        self.assertEqual(caught.exception.report['deliberatelyOmittedResponses'], 1)
        self.assertNotEqual(self.harnesses[0]._journal.state, 'released')
        self.owner.abort.assert_called_once()

    def test_partial_native_startup_retains_harness_and_aborts_owner(self):
        def partial(producer, session, lab, permit):
            FakeProducer.start_authorized(producer, session, lab, permit)
            producer.stop = threading.Event()
            raise ContractError('synthetic partial native startup')
        with patch.object(_InputProducer, 'start_authorized', partial):
            with self.assertRaises(trial.TrialIncomplete) as caught:
                trial.run_one_lost_ack(self.plan, self.profile, sandbox_probe=self.plan.aapt, cancellation=threading.Event())
        self.assertIs(caught.exception.harness, self.harnesses[0])
        self.assertEqual(caught.exception.report['phase'], 'session')
        self.assertFalse(caught.exception.report['trialPassed'])
        self.assertTrue(self.harnesses[0]._provider.stop.is_set())
        self.owner.abort.assert_called_once();self.owner.collect.assert_not_called()

    def test_actual_clock_bounded_grants_are_not_test_clock_receipts(self):
        authority = HostAuthority(self.root/'grant.sqlite', lease_directory=self.root/'grant-leases')
        try:
            grant = trial._grant(authority, 'initial')
            authority._require_parent_grant(grant)
            with self.assertRaises(ContractError):trial._grant(authority, 'arbitrary')
        finally:authority.close()
