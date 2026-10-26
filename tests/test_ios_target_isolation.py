"""Target-bound iOS input receipts and capture guards; no devices are accessed.

Host contract tests execute real Python provider/Lab paths. Swift boundary tests
extract the actual native guard methods and use inert closures when a compiler
is installed. Source wiring assertions are not native runtime verification.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

from reproof.live.model import Lab, LiveError
from reproof.live.providers import IosProvider
from reproof.live.iphone import PhysicalIosProvider
from tests.test_ios_cleanup_ack import InlineBridge
from tests.test_live_model import Provider

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'live-ios/Tests/LiveControlTests.swift'


class TargetReceiptTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('subprocess.Popen', side_effect=AssertionError('No native process')))

    def receipt(self, code):
        provider = IosProvider('synthetic', '/tmp/inert-products', 'example.target')
        provider.bridge('ready', {'capabilities': {'targetBoundaryVersion': 1}})
        provider.commands = InlineBridge(provider, lambda body, _: body.update(ok=False, error=code))
        return provider.execute('tap', {'x': .5, 'y': .5})

    def test_known_foreground_rejection_stays_no_effect(self):
        self.assertEqual(self.receipt('target_not_foreground'), {
            'ok': False, 'timing': 'best-effort', 'outcome': 'rejected',
            'code': 'target_not_foreground'})

    def test_post_dispatch_drift_is_not_a_no_effect_rejection(self):
        self.assertEqual(self.receipt('target_changed_during_input'), {
            'ok': False, 'timing': 'best-effort', 'outcome': 'uncertain',
            'code': 'target_changed_during_input'})

    def test_physical_receipt_preserves_boundary_without_another_capture(self):
        for code, outcome in [('target_not_foreground', 'rejected'),
                              ('target_changed_during_input', 'uncertain')]:
            with self.subTest(code=code):
                provider = object.__new__(PhysicalIosProvider)
                IosProvider.__init__(provider, 'synthetic', '/tmp/inert-products', 'example.target')
                provider.bridge('ready', {'capabilities': {'targetBoundaryVersion': 1}})
                class Transport:
                    def call(self, path, body=None):
                        if path == '/command':
                            self.command = body
                            return {'accepted': True}
                        return {'pending': False, 'id': self.command['id'], 'ok': False,
                                'timing': 'best-effort', 'error': code}
                provider.transport = Transport()
                provider._receive_frame = lambda: self.fail('Failed input must not request a frame')
                result = provider.execute('tap', {'x': .5, 'y': .5})
                self.assertEqual(result, {'ok': False, 'timing': 'best-effort',
                                         'outcome': outcome, 'code': code})

    def test_physical_refresh_preserves_good_fence_and_invalidates_bad_refresh(self):
        for bad_refresh in (False, True):
            with self.subTest(bad_refresh=bad_refresh):
                provider = object.__new__(PhysicalIosProvider)
                IosProvider.__init__(provider, 'synthetic', '/tmp/inert-products', 'example.target')
                provider.process = Mock()
                provider.process.poll.return_value = None
                provider.lab = Mock()
                provider.sid = 'session'
                provider.stop = Mock()
                provider.stop.is_set.return_value = False
                provider.stop.wait.side_effect = [False, False, False, True]
                provider.frame_clock = Mock()
                provider.frame_clock.refresh_due.return_value = True
                provider._validate_native_frame_buffer = Mock()
                provider._receive_frame = Mock()
                statuses = 0
                command = None
                def call(path, body=None, **kwargs):
                    nonlocal statuses, command
                    if path == '/command':
                        command = body
                        return {'accepted': True}
                    if path.startswith('/ack/'):
                        return {'pending': False, 'id': command['id'], 'ok': True,
                                'timing': 'best-effort'}
                    self.assertEqual(path, '/status')
                    statuses += 1
                    if statuses == 2:
                        self.assertTrue(provider._target_boundary_ready)
                        result = provider.execute('tap', {'x': .5, 'y': .5})
                        self.assertTrue(result['ok'])
                    return {'ready': True, 'capabilities': {
                        'targetBoundaryVersion': 0 if bad_refresh and statuses == 2 else 1}}
                provider.transport = Mock()
                provider.transport.call.side_effect = call
                provider._poll()
                self.assertEqual(statuses, 2)
                self.assertEqual(provider._target_boundary_ready, not bad_refresh)
                if bad_refresh:
                    provider.lab.fail.assert_called_once()
                    with self.assertRaises(LiveError):
                        provider.execute('tap', {'x': .5, 'y': .5})
                else:
                    provider.lab.fail.assert_not_called()

    def test_no_frame_before_ready_after_failed_ready_or_after_abort(self):
        provider = IosProvider('synthetic', '/tmp/inert-products', 'example.target')
        provider.lab = Mock()
        provider.sid = 'session'
        frame = {'imageBase64': 'c2FtcGxl', 'mime': 'image/jpeg', 'width': 1,
                 'height': 1, 'orientation': 'portrait', 'capturedAt': 1}
        with self.assertRaises(LiveError):
            provider.bridge('frame', frame)
        with self.assertRaises(LiveError):
            provider.bridge('ready', {'capabilities': {'targetBoundaryVersion': True}})
        with self.assertRaises(LiveError):
            provider.bridge('frame', frame)
        with self.assertRaises(LiveError):
            provider.execute('tap', {'x': .5, 'y': .5})
        self.assertTrue(provider.commands.empty())
        provider.lab.publish_frame.assert_not_called()
        provider.bridge('ready', {'capabilities': {'targetBoundaryVersion': 1}})
        provider.bridge('frame', frame)
        provider.lab.publish_frame.assert_called_once()
        with self.assertRaises(LiveError):
            provider.bridge('ready', {'capabilities': {}})
        with self.assertRaises(LiveError):
            provider.bridge('frame', frame)
        with self.assertRaises(LiveError):
            provider.execute('tap', {'x': .5, 'y': .5})
        self.assertTrue(provider.commands.empty())
        self.assertEqual(provider.lab.publish_frame.call_count, 1)
        provider.bridge('ready', {'capabilities': {'targetBoundaryVersion': 1}})
        provider._abort_startup()
        with self.assertRaises(LiveError):
            provider.bridge('frame', frame)
        with self.assertRaises(LiveError):
            provider.bridge('ready', {'capabilities': {'targetBoundaryVersion': 1}})
        self.assertEqual(provider.lab.publish_frame.call_count, 1)

    def test_real_lab_quarantines_uncertain_target_transition(self):
        result = self.receipt('target_changed_during_input')
        class DriftProvider(Provider):
            def execute(self, action, payload):
                self.calls.append((action, payload))
                return result
        with tempfile.TemporaryDirectory() as output:
            provider = DriftProvider()
            lab = Lab([{'id': 'test', 'kind': 'test',
                        'capabilities': {'actions': ['tap']}, 'factory': lambda: provider}], output)
            try:
                session = lab.create_session('test', 'owner', 'c')
                frame = lab.frame(session['id'])
                command = {'controllerId': 'c', 'epoch': 1, 'sequence': 1,
                           'commandId': 'tap-1', 'frameId': frame['id'], 'geometryVersion': 1,
                           'action': 'tap', 'payload': {'x': .5, 'y': .5}}
                with self.assertRaises(LiveError) as caught:
                    lab.input(session['id'], 'owner', command)
                self.assertEqual(caught.exception.code, 'injection_unknown')
                self.assertEqual(lab.get_session(session['id'])['state'], 'failed')
                self.assertEqual(lab.devices['test']['state'], 'quarantined')
                with self.assertRaises(LiveError):
                    lab.input(session['id'], 'owner', command)
                self.assertEqual(len(provider.calls), 1)
            finally:
                lab.close_all()


class TargetSourceWiringTests(unittest.TestCase):
    def setUp(self):
        self.source = SOURCE.read_text()
        self.execute = self.source.split('    private func execute(', 1)[1].split('    private func admitAuthority(', 1)[0]
        self.frame = self.source.split('    private func sendFrame(', 1)[1].split('\n}\n', 1)[0]

    def test_live_capture_never_uses_global_screen_or_mask_as_success(self):
        self.assertNotIn('XCUIScreen', self.frame)
        self.assertIn('try captureTargetFrame { targetApplication.screenshot() }', self.frame)
        self.assertLess(self.frame.rindex('guard targetIsForeground'), self.frame.index('try bridge.frame('))
        self.assertNotIn('UIGraphics', self.frame)

    def test_no_springboard_or_active_application_input_fallback(self):
        self.assertNotIn('springboard', self.source.lower())
        self.assertNotIn('activeApplication', self.source)
        for action in ('tap', 'long_press', 'swipe', 'text'):
            block = self.execute.split('case "' + action + '":', 1)[1].split('\n        case ', 1)[0]
            self.assertIn('return performTargetInput {', block)
            self.assertLess(block.index('effectAuthorized(command)'), block.index('performTargetInput'))

    def test_home_cannot_enable_external_screen_capture_or_input(self):
        block = self.execute.split('case "home":', 1)[1].split('\n        case ', 1)[0]
        self.assertLess(block.index('guard targetIsForeground'), block.index('press(.home)'))
        self.assertIn('isOnHomeScreen = true', block)
        guard = self.source.split('private var targetIsForeground:', 1)[1].split('\n    }', 1)[0]
        self.assertIn('!isOnHomeScreen && targetApplication.state == .runningForeground', guard)

    def test_both_native_transports_advertise_the_required_boundary_version(self):
        self.assertIn('try container.encode(1, forKey: .targetBoundaryVersion)', self.source)
        self.assertIn('"targetBoundaryVersion": 1', self.source)

    def test_target_cleanup_does_not_require_foreground(self):
        for action in ('authority_cleanup', 'authority_retire'):
            block = self.execute.split('case "' + action + '":', 1)[1].split('\n        case ', 1)[0]
            self.assertNotIn('targetIsForeground', block)
            self.assertIn('targetApplication', block)

    def test_failure_ack_does_not_require_successful_capture(self):
        loop = self.source.split('let result = execute(command)', 1)[1].split('lastFrameAt = Date()', 1)[0]
        self.assertIn('if !draining && result.ok {', loop)
        self.assertLess(loop.index('try sendFrame'), loop.index('try bridge.ack'))


@unittest.skipUnless(shutil.which('swiftc'), 'Swift compiler unavailable; no native validation claimed')
class NativeTargetGuardExecutionTests(unittest.TestCase):
    def test_actual_guard_closures_reject_before_effect_and_discard_raced_frame(self):
        source = SOURCE.read_text()
        guards = source[source.index('    private func performTargetInput('):
                        source.index('    private func normalizedPoint(')]
        program = '''
private enum BridgeFailure: Error { case targetNotForeground }
private struct CommandResult {
 let ok: Bool; let error: String?
 static var success: CommandResult { CommandResult(ok: true, error: nil) }
 static func failure(_ error: String) -> CommandResult { CommandResult(ok: false, error: error) }
}
private class Probe {
 var targetIsForeground = false
''' + guards + '''
 func exercise() throws {
  var effects = 0
  let denied = performTargetInput { effects += 1 }
  precondition(effects == 0 && denied.error == "target_not_foreground")
  var captures = 0
  do { _ = try captureTargetFrame { captures += 1; return "external" }; fatalError() }
  catch BridgeFailure.targetNotForeground {}
  precondition(captures == 0)
  targetIsForeground = true
  let changed = performTargetInput { effects += 1; targetIsForeground = false }
  precondition(effects == 1 && changed.error == "target_changed_during_input")
  targetIsForeground = true
  do { _ = try captureTargetFrame { captures += 1; targetIsForeground = false; return "discard" }; fatalError() }
  catch BridgeFailure.targetNotForeground {}
  precondition(captures == 1)
  targetIsForeground = true
  precondition(performTargetInput { effects += 1 }.ok)
  let frame = try captureTargetFrame { "target" }
  precondition(frame == "target")
 }
}
try Probe().exercise()
print("target-guards-passed")
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'Probe.swift').write_text(program)
            compiled = subprocess.run([shutil.which('swiftc'), str(path / 'Probe.swift'), '-o', str(path / 'probe')], capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            run = subprocess.run([str(path / 'probe')], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), 'target-guards-passed')


if __name__ == '__main__':
    unittest.main()
