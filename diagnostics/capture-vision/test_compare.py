"""Inert condition/lifecycle regressions. No Apple tool, provider or device runs."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('comparison_under_test', HERE / 'compare.py')
c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c)


def setUpModule():
    for name in ('run', 'Popen'):
        patch = mock.patch.object(subprocess, name, side_effect=AssertionError('Unexpected subprocess'))
        guard = patch.start()
        unittest.addModuleCleanup(patch.stop)
        unittest.addModuleCleanup(guard.assert_not_called)


class InertCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.now = 100.0
        patch = mock.patch.object(c.time, 'monotonic', side_effect=lambda: self.now)
        patch.start(); self.addCleanup(patch.stop)

    def error(self, code, operation, *args, **kwargs):
        with self.assertRaisesRegex(c.replay.DiagnosticError, '^' + code + '$'):
            operation(*args, **kwargs)

    def sample(self, fid=1, received=None, **kw):
        received = self.now if received is None else received
        return dict(active=True, frameId=fid, frameBytes=81847, width=1179, height=2556,
                    mime='image/jpeg', received=received, history=[(fid, received, 1)], **kw)


class MonitorTests(InertCase):
    def test_two_strictly_inside_publications_and_bounded_metadata(self):
        monitor = c.CaptureMonitor(lambda: self.row)
        self.row = self.sample(); monitor.poll()
        for i in range(1, 5):
            self.now = 100 + i * .25
            self.row = self.sample(i + 1); monitor.poll()
        proof = monitor.interval(100, 101)
        self.assertEqual(proof['publicationCountInsideProcess'], 3)
        self.assertEqual(proof['firstFrameId'], 2)
        self.assertEqual(proof['lastFrameId'], 4)
        output = monitor.report()
        self.assertNotIn('history', json.dumps(output))
        self.assertNotIn('imageBase64', json.dumps(output))
        self.assertNotIn('at', output['samples'][0])

    def test_cached_frame_fails_inside_interval_even_when_recent(self):
        self.row = self.sample()
        monitor = c.CaptureMonitor(lambda: self.row); monitor.poll()
        self.now += .5; monitor.poll()
        self.error('capture_progress_inside_observer_unproven', monitor.interval, 100, 100.5)

    def test_pre_start_post_return_or_single_interior_frame_cannot_pass(self):
        for publications in ({1: 99.9, 2: 101.1}, {1: 100, 2: 101}, {1: 100.5}):
            monitor = c.CaptureMonitor(lambda: self.sample())
            monitor.publications = publications
            self.error('capture_progress_inside_observer_unproven', monitor.interval, 100, 101)

    def test_inactive_stale_unknown_and_regression_latch_fail_closed(self):
        for change, expected in (({'active': False}, 'capture_inactive'),
                ({'received': 97}, 'capture_frame_stale'), ({'width': 0}, 'capture_frame_invalid'),
                ({'history': []}, 'capture_history_unknown')):
            self.row = self.sample(); monitor = c.CaptureMonitor(lambda: self.row); monitor.poll()
            self.row.update(change)
            self.error(expected, monitor.poll)
            self.row = self.sample(2)
            self.error(expected, monitor.poll)
        self.row = self.sample(2); monitor = c.CaptureMonitor(lambda: self.row); monitor.poll()
        self.row = self.sample(1)
        self.error('capture_frame_regressed', monitor.poll)

    def test_capture_ending_midcall_cannot_be_reinterpreted_active(self):
        self.row = self.sample(); monitor = c.CaptureMonitor(lambda: self.row); monitor.poll()
        self.now += .25; self.row = self.sample(2); monitor.poll()
        self.now += .25; self.row = self.sample(3); monitor.poll()
        self.row['active'] = False
        self.error('capture_inactive', monitor.poll)
        self.error('capture_inactive', monitor.interval, 100, 101)

    def test_poll_gap_is_not_hidden_by_fresh_recent_frames(self):
        monitor = c.CaptureMonitor(lambda: self.sample()); monitor.poll()
        self.now += 1.01
        self.error('capture_monitor_gap', monitor.poll)

    def test_long_unobserved_publication_gap_rejected(self):
        monitor = c.CaptureMonitor(lambda: self.sample())
        monitor.publications = {1: 100.1, 2: 104.9}
        self.error('capture_progress_gap_inside_observer', monitor.interval, 100, 105)

    def test_maximum_poll_count_and_duration_bounded(self):
        for kind in ('count', 'time'):
            monitor = c.CaptureMonitor(lambda: self.sample())
            if kind == 'count':
                monitor.samples = [{'at': self.now}] * c.MAX_SAMPLES
            else:
                self.now += c.MAX_MONITOR_SECONDS + 1
            self.error('capture_monitor_bound_exceeded', monitor.poll)

    def test_source_exception_redacted(self):
        monitor = c.CaptureMonitor(mock.Mock(side_effect=RuntimeError('/private/token')))
        self.error('capture_state_unknown', monitor.poll)
        self.assertNotIn('token', json.dumps(monitor.report()))


class ObserverTests(InertCase):
    def call(self, process, monitor=None):
        report = c.new_report()
        with mock.patch.object(c.replay, 'run_process', side_effect=process) as boundary:
            saved = c.replay.run_process
            result = c.observe_once(report, self.work/'observe-frame', self.work/'image.jpg',
                c.replay.FIXTURES[0], self.work, {}, condition='active' if monitor else 'after_cleanup', monitor=monitor)
            self.assertIs(c.replay.run_process, saved)
        self.assertEqual(boundary.call_count, 1)
        self.assertEqual(boundary.call_args.kwargs['timeout'], 15)
        self.assertEqual(report['observerCalls'], 1)
        self.assertEqual(sum(report['callCounts'].values()), 1)
        return result, report

    def test_original_oracle_and_exact_15_seconds_no_retry(self):
        rows = [{'text': word, 'confidence': .9} for word in c.replay.FIXTURES[0]['required']]
        def process(*args, **kwargs):
            self.now += .8
            return subprocess.CompletedProcess([], 0, json.dumps({'observations': rows}).encode(), b'')
        result, report = self.call(process)
        self.assertTrue(result['passed'])
        self.assertEqual(result['processWallElapsedMs'], 799)
        self.assertNotIn('observations', result)
        self.assertFalse(report['nativeAcceptance'])

    def test_timeout_is_one_call_with_unchanged_original_failure(self):
        def process(*args, **kwargs):
            self.now += 15
            raise subprocess.TimeoutExpired('private-command', 15, stderr=b'/private/token')
        result, report = self.call(process)
        self.assertEqual(result['outcome'], 'observer_timeout')
        self.assertFalse(result['passed'])
        self.assertNotIn('private', json.dumps(report))

    def test_capture_boundary_failure_preserves_consumed_attempt(self):
        report = c.new_report()
        monitor = mock.Mock()
        monitor.interval.side_effect = c.replay.DiagnosticError('capture_inactive')
        with mock.patch.object(c.replay, 'run_process', return_value=subprocess.CompletedProcess([], 1, b'', b'')):
            self.error('capture_inactive', c.observe_once, report, self.work/'observer', self.work/'image',
                       c.replay.FIXTURES[0], self.work, {}, condition='active', monitor=monitor)
        self.assertEqual(report['observerCalls'], 1)
        self.assertEqual(report['attempts'][0]['actualCondition'], 'unknown')

    def test_metadata_poll_time_does_not_contaminate_original_deadline(self):
        monitor = mock.Mock()
        monitor.poll.side_effect = lambda: setattr(self, 'now', self.now + 1)
        monitor.interval.return_value = {'condition': 'verified_active'}
        rows = [{'text': word, 'confidence': 1} for word in c.replay.FIXTURES[0]['required']]
        def process(*args, **kwargs):
            self.now += 14
            return subprocess.CompletedProcess([], 0, json.dumps({'observations': rows}).encode(), b'')
        result, report = self.call(process, monitor)
        self.assertTrue(result['passed'])
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(result['processWallElapsedMs'], 14000)
        self.assertEqual(result['elapsedMs'], 14000)
        monitor.interval.assert_called_once_with(101, 115)

    def test_fifth_call_rejected_before_process(self):
        report = c.new_report(); report['observerCalls'] = 4
        self.error('observer_call_limit', c.observe_once, report, self.work/'observer', self.work/'image',
                   c.replay.FIXTURES[0], self.work, {}, condition='after_cleanup')


class LifecycleTests(InertCase):
    def fake_capture(self, confirmed=True):
        capture = mock.Mock()
        capture.start.return_value = {'initialScreen': True}
        capture.close.return_value = {'confirmed': confirmed, 'state': 'closed' if confirmed else 'failed',
                                      'deviceState': 'available' if confirmed else 'quarantined'}
        return capture

    def run_conditions(self, capture, shutdown):
        report = c.new_report()
        order = []
        monitor = mock.Mock(failure=None)
        monitor.report.return_value = {'pollCount': 20}
        def observe(report, executable, image, fixture, work, env, **kwargs):
            report['observerCalls'] += 1
            report['attempts'].append({'passed': False, 'condition': kwargs['condition']})
            order.append(kwargs['condition'])
        capture.close.side_effect = lambda: (order.append('cleanup') or capture.close.return_value)
        shutdown.side_effect = lambda: (order.append('shutdown') or shutdown.return_value)
        with mock.patch.object(c, 'CaptureMonitor', return_value=monitor), mock.patch.object(c, 'observe_once', side_effect=observe):
            try:
                c.compare_conditions(report, capture, shutdown, self.work/'observer',
                    [self.work/'one', self.work/'two'], self.work, {})
            except c.replay.DiagnosticError as error:
                report['outcome'] = str(error)
        return report, order

    def test_exact_four_calls_active_then_cleanup_then_shutdown_then_stopped(self):
        capture = self.fake_capture(); shutdown = mock.Mock(return_value={'state': 'Shutdown'})
        report, order = self.run_conditions(capture, shutdown)
        self.assertEqual(order, ['active', 'active', 'cleanup', 'shutdown', 'after_cleanup', 'after_cleanup'])
        self.assertEqual(report['observerCalls'], 4)
        self.assertTrue(report['comparisonValid'])
        self.assertFalse(report['diagnosticPassed'])  # Original OCR failure is not promoted.
        self.assertFalse(report['nativeAcceptance'])
        capture.close.assert_called_once()

    def test_cleanup_failure_preserves_quarantine_and_blocks_after_calls(self):
        capture = self.fake_capture(False); shutdown = mock.Mock(return_value={'state': 'Shutdown'})
        report, order = self.run_conditions(capture, shutdown)
        self.assertEqual(report['observerCalls'], 2)
        self.assertEqual(report['outcome'], 'exact_session_cleanup_unconfirmed')
        self.assertEqual(report['cleanup']['deviceState'], 'quarantined')
        self.assertNotIn('after_cleanup', order)
        capture.close.assert_called_once(); shutdown.assert_called_once()

    def test_shutdown_unknown_or_wrong_state_blocks_after_calls(self):
        for state in ('Booted', 'unconfirmed', None):
            report, order = self.run_conditions(self.fake_capture(), mock.Mock(return_value={'state': state}))
            self.assertEqual(report['observerCalls'], 2)
            self.assertEqual(report['outcome'], 'owned_simulator_shutdown_unconfirmed')
            self.assertNotIn('after_cleanup', order)

    def test_start_failure_always_attempts_exact_cleanup_and_shutdown_without_ocr(self):
        report = c.new_report(); capture = self.fake_capture(False)
        capture.start.side_effect = c.replay.DiagnosticError('native_provider_not_active')
        shutdown = mock.Mock(return_value={'state': 'Shutdown'})
        self.error('native_provider_not_active', c.compare_conditions, report, capture, shutdown,
                   self.work/'observer', [self.work/'one', self.work/'two'], self.work, {})
        self.assertEqual(report['observerCalls'], 0)
        capture.close.assert_called_once(); shutdown.assert_called_once()

    def test_close_is_exact_idempotent_and_keeps_bridge_alive_until_cleanup(self):
        args = types.SimpleNamespace(simulator='uuid', owned_simulator_name='name')
        capture = c.OwnedCapture(args, {}, self.work)
        capture.session = {'id': 'exact-session'}; capture.descriptor = {'id': 'exact-device'}
        capture.provider = mock.Mock(temp=None, lease=None)
        capture.provider.stop.is_set.return_value = True
        capture.provider.process.poll.return_value = 0
        capture.server = mock.Mock(); capture.authority = mock.Mock(); capture.lab = mock.Mock()
        capture.lab.devices = {'exact-device': {'state': 'available'}}
        def close(sid, owner, **kwargs):
            capture.server.shutdown.assert_not_called()
            self.assertEqual((sid, owner, kwargs), ('exact-session', c.OWNER, {'require_finished_inputs': True}))
            return {'state': 'closed'}
        capture.lab.close_session.side_effect = close
        self.assertTrue(capture.close()['confirmed'])
        capture.close()
        capture.lab.close_session.assert_called_once()
        capture.lab.close_all.assert_not_called()
        capture.authority.close.assert_called_once()

    def test_unproven_provider_cleanup_retains_authority_and_private_state(self):
        capture = c.OwnedCapture(types.SimpleNamespace(), {}, self.work)
        capture.session = {'id': 'sid'}; capture.descriptor = {'id': 'dev'}
        capture.provider = mock.Mock(temp=None, lease=None)
        capture.provider.stop.is_set.return_value = True
        capture.provider.process.poll.return_value = None
        capture.lab = mock.Mock(); capture.lab.devices = {'dev': {'state': 'quarantined'}}
        capture.lab.close_session.return_value = {'state': 'failed'}
        capture.authority = mock.Mock()
        result = capture.close()
        self.assertFalse(result['confirmed']); self.assertTrue(result['privateStateRetained'])
        capture.authority.close.assert_not_called(); capture.lab.close_all.assert_not_called()


    def test_server_not_started_never_waits_in_shutdown(self):
        capture = c.OwnedCapture(types.SimpleNamespace(), {}, self.work)
        capture.server = mock.Mock()
        capture.thread = mock.Mock()
        capture.close()
        capture.server.shutdown.assert_not_called()
        capture.server.server_close.assert_called_once()
        capture.thread.join.assert_not_called()

    def test_source_observation_pins_checked_before_host_or_any_process(self):
        report = c.new_report()
        with mock.patch.object(c.replay, 'validate_inputs', side_effect=c.replay.DiagnosticError('input_hash_mismatch')):
            with mock.patch.object(c.replay, 'verify_host') as host:
                self.error('input_hash_mismatch', c.execute, report, types.SimpleNamespace())
                host.assert_not_called()


class LiveSampleTests(InertCase):
    def capture(self):
        capture = c.OwnedCapture(types.SimpleNamespace(simulator='owned-uuid'),
                                 {'receipt': {'artifactDigest': 'digest'}}, self.work)
        capture.session = {'id': 'sid'}
        handshake = object(); capture.handshake = handshake
        provider = mock.Mock(udid='owned-uuid', native_handshake=handshake, identity={'artifactDigest': 'digest'})
        provider.handshake_ready.is_set.return_value = True
        provider.stop.is_set.return_value = False
        provider.process.poll.return_value = None
        capture.provider = provider
        session = {'state': 'active', 'stopAdmission': False, 'provider': provider,
                   'frameLock': threading.Lock(), 'frameSequence': 1,
                   'frame': {'id': 1, '_received': 100, 'bytes': b'jpeg', 'width': 100, 'height': 200,
                             'mime': 'image/jpeg'}, 'frameHistory': [(1, 100, 1)]}
        capture.lab = types.SimpleNamespace(sessions={'sid': session})
        return capture, provider, session

    def test_same_provider_handshake_identity_runner_and_session_required(self):
        mutations = [lambda p, s: s.update(state='failed'), lambda p, s: s.update(stopAdmission=True),
                     lambda p, s: s.update(provider=object()), lambda p, s: setattr(p, 'native_handshake', object()),
                     lambda p, s: setattr(p, 'udid', 'foreign'),
                     lambda p, s: p.identity.update(artifactDigest='changed'),
                     lambda p, s: setattr(p.stop.is_set, 'return_value', True),
                     lambda p, s: setattr(p.process.poll, 'return_value', 0)]
        for mutate in mutations:
            capture, provider, session = self.capture()
            self.assertTrue(capture.sample()['active'])
            mutate(provider, session)
            self.assertFalse(capture.sample()['active'])

    def test_sequence_mismatch_and_frame_lock_timeout_fail_closed(self):
        capture, provider, session = self.capture()
        session['frameSequence'] = 2
        self.error('capture_sequence_unbound', capture.sample)
        session['frameLock'] = mock.Mock()
        session['frameLock'].acquire.return_value = False
        self.error('capture_frame_lock_timeout', capture.sample)
        session['frameLock'].acquire.assert_called_once_with(timeout=.2)
        session['frameLock'].release.assert_not_called()


class IdentityTests(InertCase):
    UUID = '12345678-1234-1234-1234-123456789abc'
    def devices(self, **changes):
        device = dict(udid=self.UUID, name='owned', state='Booted', isAvailable=True, deviceTypeIdentifier=c.DEVICE)
        device.update(changes)
        return {'devices': {c.RUNTIME: [device]}}

    def test_shutdown_revalidates_exact_uuid_before_and_after(self):
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            return json.dumps(self.devices(state='Shutdown' if len(calls) == 3 else 'Booted')).encode()
        with mock.patch.object(c, 'command', side_effect=command):
            state = c.shutdown_simulator(self.UUID, 'owned')
        self.assertEqual(state['state'], 'Shutdown')
        self.assertEqual(calls[1], ['/usr/bin/xcrun', 'simctl', 'shutdown', self.UUID])
        self.assertEqual(len(calls), 3)

    def test_foreign_ambiguous_unavailable_or_wrong_runtime_blocks_mutation(self):
        docs = [self.devices(name='foreign'), self.devices(isAvailable=False), self.devices(deviceTypeIdentifier='other')]
        double = self.devices(); double['devices'][c.RUNTIME] *= 2; docs.append(double)
        wrong = self.devices(); wrong['devices']['other'] = wrong['devices'].pop(c.RUNTIME); docs.append(wrong)
        for document in docs:
            with mock.patch.object(c, 'command', return_value=json.dumps(document).encode()) as command:
                with self.assertRaises(c.replay.DiagnosticError):
                    c.shutdown_simulator(self.UUID, 'owned')
                self.assertEqual(command.call_count, 1)


class StaticAndReportTests(InertCase):
    def test_exact_offline_inputs_and_unchanged_replay_are_pinned(self):
        c.replay.validate_inputs(c.OFFLINE)
        self.assertEqual(hashlib.sha256((c.OFFLINE/'replay.py').read_bytes()).hexdigest(), c.REPLAY_SHA256)
        self.assertEqual(c.replay.OBSERVER_TIMEOUT, 15)

    def test_opened_same_repo_readonly_first_attempt_and_no_extra_workflow(self):
        root = c.ROOT
        text = (root/'.github/workflows/capture-vision-comparison.yml').read_text()
        active = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
        for expected in ('types: [opened]', 'branches: [codex/owned-native-runtime-acceptance]',
                'github.run_attempt == 1', 'github.event.repository.private == false',
                'github.event.pull_request.head.repo.full_name == github.repository',
                'permissions:\n  contents: read', 'runs-on: macos-15', 'persist-credentials: false'):
            self.assertIn(expected, active)
        for forbidden in ('workflow_dispatch:', 'push:', 'synchronize', 'secrets.', 'sudo ', '/dev/kvm'):
            self.assertNotIn(forbidden, active)
        self.assertFalse((root/'.github/workflows/offline-vision-diagnostic.yml').exists())
        self.assertNotIn('replay.py --output', active)

    def test_harness_never_invokes_original_flow_observe_state_input_or_close_all(self):
        source = (HERE/'compare.py').read_text()
        for forbidden in ('native.main(', 'native.run_flow(', 'native.observe_state(', 'lab.input(', '.close_all('):
            self.assertNotIn(forbidden, source)
        shell = (HERE/'run-ci.sh').read_text()
        self.assertNotIn('test-without-building', shell)
        self.assertNotIn('ios-native-fixture-qa.py --', shell)

    def test_exception_text_never_escapes_report_and_output_reserved_first(self):
        args = types.SimpleNamespace(output=self.work/'result.json')
        output = io.StringIO()
        with mock.patch.object(c, 'execute', side_effect=RuntimeError('/private/token env=secret')) as execute:
            result = c.run(args, output)
        self.assertEqual(result['outcome'], 'unexpected_local_failure')
        self.assertNotIn('secret', output.getvalue())
        self.assertNotIn('token', args.output.read_text())
        with mock.patch.object(c, 'execute') as execute:
            second = c.run(args, io.StringIO())
            execute.assert_not_called()
        self.assertEqual(second['outcome'], 'output_reservation_failed')


if __name__ == '__main__':
    unittest.main()
