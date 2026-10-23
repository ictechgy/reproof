"""Real authority + fake clock/native boundaries; no Apple tools or devices."""
from contextlib import ExitStack
from pathlib import Path
import plistlib
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from reproof.core import ContractError
from reproof.live.authority import HostAuthority, ProviderResult, issue_local_parent_grant
from reproof.live.clock_sync import ClockReading
from reproof.live import providers
from reproof.live.model import LiveError


class Clock:
    def __init__(self):
        self.ns = 100_000_000_000
        self.monotonic = 500.0  # Deliberately not the continuous-clock epoch.
        self.clock_id = 'mach-continuous-1-1'
        self.boot = 'a' * 64
        self.uncertainty = 1

    def read(self):
        return ClockReading(self.clock_id, self.boot, self.ns, self.uncertainty)

    def advance(self, seconds, *, suspend=False):
        self.ns += round(seconds * 1_000_000_000)
        if not suspend:
            self.monotonic += seconds


class IOSStartupBudgetTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.clock = Clock()
        self.authority = HostAuthority(self.root / 'authority.sqlite3', clock=self.clock,
                                       lease_directory=self.root / 'leases')
        self.stack.callback(self.authority.close)
        self.grant = issue_local_parent_grant(self.authority, lifetime_ns=600_000_000_000)
        self.handle = self.authority.claim_device(device_kind='ios-simulator', physical_id='owned-synthetic',
            display_alias='owned-synthetic', helper_incarnation='helper_budget', parent_grant=self.grant)
        admission = self.handle.admit_operation(operation_id='operation_budget', payload_digest='b' * 64,
                                                session_id='session_budget', sequence=1)
        self.permit = self.handle.prepare_dispatch(admission, provider_incarnation='provider_budget')
        self.provider = providers.IosProvider('owned-synthetic', self.root, 'io.fixture', {})
        self.provider.bind_authority(self.handle, 'provider_budget')
        self.provider._startup_permit = self.permit
        self.provider._startup_accepting = True
        self.provider._startup_deadline = self.clock.monotonic + 90
        self.provider._startup_generation = 1
        self.provider.handshake_ready = threading.Event()
        self.stack.enter_context(patch.object(providers.time, 'monotonic', lambda: self.clock.monotonic))
        self.lab = SimpleNamespace(authority=self.authority)
        self.provider._capture_startup_clock(self.lab)

    def initialize(self):
        return self.provider._initialize_startup_budget(self.permit)

    def body(self, **changes):
        return dict({'capabilities': {}, 'protocolVersion': 2, 'helperVersion': 2,
            'helperIncarnation': self.permit.helper_incarnation, 'hostIncarnation': self.permit.host_incarnation,
            'providerIncarnation': self.permit.provider_incarnation, 'nativeIncarnation': 'native_budget',
            'nativeClockId': 'ios-mach-continuous', 'nativeTimeMs': self.clock.ns // 1_000_000}, **changes)

    def installation(self):
        expected = {'bundle': 'io.fixture', 'artifactDigest': 'c' * 64}
        self.provider._pending_install_identity = expected
        self.provider._install_app = self.root / 'Selected.app'
        return expected

    def test_absolute_deadline_uses_continuous_epoch_and_never_renews(self):
        self.clock.advance(10)
        deadline = self.initialize()
        self.assertLessEqual(deadline * 1_000_000, 190_000_000_000)
        self.assertLessEqual(deadline * 1_000_000, self.permit.deadline_ns)
        self.assertGreater(deadline, 189_990)
        self.clock.advance(37.665)
        remaining = self.provider._startup_remaining_timeout(90)
        self.assertGreater(remaining, 42.33)
        self.assertLessEqual(remaining, 42.335)
        with self.assertRaisesRegex(LiveError, 'already fixed'):
            self.initialize()
        self.assertEqual(self.provider._startup_transport_deadline_ms, deadline)

    def test_suspend_before_budget_initialization_does_not_renew_entry_ceiling(self):
        ceiling = self.provider._startup_continuous_ceiling_ns
        self.clock.advance(91, suspend=True)
        with self.assertRaisesRegex(LiveError, 'budget is exhausted'):
            self.initialize()
        self.handle.check_dispatch_permit(self.permit)
        self.assertEqual(self.clock.monotonic, 500)
        self.assertEqual(self.provider._startup_continuous_ceiling_ns, ceiling)
        self.assertIsNone(self.provider._startup_transport_deadline_ms)

    def test_authorized_entry_fixes_budget_before_preparation_and_rechecks_success(self):
        def prepare(*args, **kwargs):
            self.assertIsNotNone(self.provider._startup_transport_deadline_ms)
            self.assertLessEqual(self.provider._startup_continuous_ceiling_ns, 190_000_000_000)
            self.provider.native_handshake = object()
            self.provider.handshake_ready.set()
            self.clock.advance(91, suspend=True)
        with patch.object(self.provider, 'start', side_effect=prepare) as start:
            with self.assertRaisesRegex(LiveError, 'budget is exhausted'):
                self.provider.start_authorized({}, self.lab, self.permit)
        start.assert_called_once()
        self.handle.check_dispatch_permit(self.permit)
        self.assertFalse(self.provider._startup_accepting)
        self.assertTrue(self.provider.stop.is_set())

    def test_suspend_after_ready_event_cannot_return_startup_success(self):
        event = Mock()
        def wait(*args):
            self.clock.advance(91, suspend=True)
            return True
        event.wait.side_effect = wait
        def prepare(*args, **kwargs):
            self.provider.native_handshake = object()
        with patch.object(providers.threading, 'Event', return_value=event), \
             patch.object(self.provider, 'start', side_effect=prepare):
            with self.assertRaisesRegex(LiveError, 'budget is exhausted'):
                self.provider.start_authorized({}, self.lab, self.permit)
        event.wait.assert_called_once()
        self.handle.check_dispatch_permit(self.permit)
        self.assertFalse(self.provider._startup_accepting)

    def test_shorter_original_permit_is_the_cap(self):
        self.handle.confirm_operation(self.permit, ProviderResult('receipt_unused', 'rejected', 'f' * 64))
        admission = self.handle.admit_operation(operation_id='operation_short', payload_digest='d' * 64,
            session_id='session_budget', sequence=2, deadline_ns=self.clock.ns + 20_000_000_000)
        self.permit = self.handle.prepare_dispatch(admission, provider_incarnation='provider_budget')
        self.provider._startup_permit = self.permit
        deadline = self.initialize()
        self.assertLess(deadline * 1_000_000, self.permit.deadline_ns)
        self.assertLess(self.provider._startup_remaining_timeout(90), 20)

    def test_unqualified_clock_expiry_revocation_and_suspend_fail_closed(self):
        # Epoch changes invalidate real authority, not a guessed conversion.
        self.initialize()
        self.clock.advance(91, suspend=True)
        with self.assertRaises(LiveError):
            self.provider._startup_remaining_timeout(90)
        self.assertEqual(self.clock.monotonic, 500.0)
        self.assertFalse(self.provider.handshake_ready.is_set())

    def test_unqualified_or_discontinuous_clock_is_rejected(self):
        for field, value in (('clock_id', 'clock-boottime'), ('boot', 'e' * 64)):
            with self.subTest(field=field):
                setattr(self.clock, field, value)
                with self.assertRaises((ContractError, LiveError)):
                    self.initialize()
        self.assertIsNone(self.provider._startup_transport_deadline_ms)

    def test_slow_ready_succeeds_with_remaining_budget_and_installs_once(self):
        deadline = self.initialize()
        expected = self.installation()
        self.clock.advance(47.665)
        phases = []
        def install(*args, **kwargs):
            self.assertIsNotNone(self.provider.native_handshake)
            self.assertGreater(kwargs['timeout'], 42.33)
            self.assertLessEqual(kwargs['timeout'], 42.335)
            phases.append('install')
            self.clock.advance(8.584)
            return SimpleNamespace(returncode=0)
        def identity(*args, **kwargs):
            phases.append('identity')
            self.assertLessEqual(kwargs['timeout'], 15)
            return expected
        with patch.object(providers.subprocess, 'run', side_effect=install) as run, \
             patch.object(providers, 'installed_identity', side_effect=identity):
            result = self.provider.bridge('ready', self.body())
            with self.assertRaisesRegex(LiveError, 'Duplicate'):
                self.provider.bridge('ready', self.body())
        run.assert_called_once()
        self.assertEqual(phases, ['install', 'identity'])
        self.assertEqual(self.provider.identity, expected)
        self.assertEqual(self.provider._startup_transport_deadline_ms, deadline)
        self.assertLess(self.provider._startup_remaining_timeout(90), 33.751)
        self.assertEqual(self.provider.bridge('started', {'authority': result['authority']}), {'ok': True})
        self.assertTrue(self.provider.handshake_ready.is_set())
        with self.assertRaisesRegex(LiveError, 'Duplicate'):
            self.provider.bridge('started', {'authority': result['authority']})

    def test_inflight_duplicate_ready_cannot_install_or_rebind(self):
        self.initialize()
        expected = self.installation()
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        def install(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return SimpleNamespace(returncode=0)
        def callback():
            try:
                outcomes.append(self.provider.bridge('ready', self.body()))
            except Exception as error:
                outcomes.append(error)
        with patch.object(providers.subprocess, 'run', side_effect=install) as run, \
             patch.object(providers, 'installed_identity', return_value=expected):
            thread = threading.Thread(target=callback)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                handshake = self.provider.native_handshake
                with self.assertRaisesRegex(LiveError, 'Duplicate'):
                    self.provider.bridge('ready', self.body(nativeIncarnation='native_duplicate'))
                self.assertIs(self.provider.native_handshake, handshake)
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        run.assert_called_once()
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0]['ok'])

    def test_revocation_during_install_cannot_read_identity_or_return_grant(self):
        self.initialize()
        self.installation()
        def install(*args, **kwargs):
            self.handle.revoke_dispatches()
            return SimpleNamespace(returncode=0)
        with patch.object(providers.subprocess, 'run', side_effect=install), \
             patch.object(providers, 'installed_identity') as identity:
            with self.assertRaises(ContractError):
                self.provider.bridge('ready', self.body())
        identity.assert_not_called()
        self.assertFalse(self.provider._ready_complete)
        self.assertFalse(self.provider.handshake_ready.is_set())

    def test_late_generation_after_identity_cannot_publish_install_or_grant(self):
        self.initialize()
        expected = self.installation()
        def identity(*args, **kwargs):
            self.provider._startup_generation += 1
            return expected
        with patch.object(providers.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
             patch.object(providers, 'installed_identity', side_effect=identity):
            with self.assertRaisesRegex(LiveError, 'generation changed'):
                self.provider.bridge('ready', self.body())
        self.assertEqual(self.provider.identity, {})
        self.assertEqual(self.provider._pending_install_identity, expected)
        self.assertFalse(self.provider._ready_complete)

    def test_suspend_after_identity_cannot_publish_ready_even_with_live_permit(self):
        self.initialize()
        expected = self.installation()
        def identity(*args, **kwargs):
            self.clock.advance(91, suspend=True)
            return expected
        with patch.object(providers.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
             patch.object(providers, 'installed_identity', side_effect=identity):
            with self.assertRaisesRegex(LiveError, 'budget is exhausted'):
                self.provider.bridge('ready', self.body())
        self.handle.check_dispatch_permit(self.permit)  # Original permit still live.
        self.assertEqual(self.clock.monotonic, 500)
        self.assertEqual(self.provider.identity, {})
        self.assertFalse(self.provider._ready_complete)

    def test_stale_host_incarnation_rejects_before_install(self):
        self.initialize()
        self.installation()
        with patch.object(providers.subprocess, 'run') as run:
            with self.assertRaisesRegex(LiveError, 'host incarnation'):
                self.provider.bridge('ready', self.body(hostIncarnation='host_wrong'))
        run.assert_not_called()
        self.assertIsNone(self.provider.native_handshake)

    def test_post_log_wait_expiry_cannot_publish_started_ack(self):
        self.initialize()
        result = self.provider.bridge('ready', self.body())
        self.provider.app_logs_only = True
        def marker(*args, **kwargs):
            self.assertLessEqual(args[0], 10)
            self.assertEqual(kwargs['startup_generation'], 1)
            self.clock.advance(91)
            return {}
        with patch.object(self.provider, '_wait_for_app_log_marker', side_effect=marker):
            with self.assertRaises(LiveError):
                self.provider.bridge('started', {'authority': result['authority']})
        self.assertFalse(self.provider.handshake_ready.is_set())

    def test_suspend_after_log_wait_cannot_publish_started_even_with_live_permit(self):
        self.initialize()
        result = self.provider.bridge('ready', self.body())
        self.provider.app_logs_only = True
        def marker(*args, **kwargs):
            self.clock.advance(91, suspend=True)
            return {}
        with patch.object(self.provider, '_wait_for_app_log_marker', side_effect=marker):
            with self.assertRaisesRegex(LiveError, 'budget is exhausted'):
                self.provider.bridge('started', {'authority': result['authority']})
        self.handle.check_dispatch_permit(self.permit)
        self.assertEqual(self.clock.monotonic, 500)
        self.assertFalse(self.provider.handshake_ready.is_set())

    def test_started_generation_change_or_cancellation_cannot_publish_ack(self):
        self.initialize()
        result = self.provider.bridge('ready', self.body())
        self.provider.app_logs_only = True
        def marker(*args, **kwargs):
            self.provider.stop.set()
        with patch.object(self.provider, '_wait_for_app_log_marker', side_effect=marker):
            with self.assertRaises(LiveError):
                self.provider.bridge('started', {'authority': result['authority']})
        self.assertFalse(self.provider.handshake_ready.is_set())

    def test_old_helper_rejected_before_native_spawn_or_install(self):
        bundle = self.root / 'OldHelper.xctest'
        bundle.mkdir()
        (bundle / 'Info.plist').write_bytes(plistlib.dumps({
            'ReproLiveProtocolVersion': 2, 'ReproLiveHelperVersion': 2}))
        config = self.root / 'old.xctestrun'
        config.write_bytes(plistlib.dumps({'__xctestrun_metadata__': {'FormatVersion': 1}, 'ReproLiveTests': {
            'BlueprintName': 'ReproLiveTests', 'TestBundlePath': str(bundle)}}))
        self.provider.lease = MagicMock()
        try:
            with patch('reproof.live.native_frame_clock.NativeFrameClock.for_session'), \
                 patch.object(providers, 'installed_identity', return_value={}), \
                 patch.object(providers, 'prepare_xctestrun', return_value=config), \
                 patch.object(self.provider, '_start_runner') as runner, \
                 patch.object(providers.subprocess, 'run') as native_effect:
                with self.assertRaisesRegex(LiveError, 'Rebuild'):
                    self.provider.start_authorized({'id': 'session_budget'}, self.lab, self.permit)
            runner.assert_not_called()
            native_effect.assert_not_called()
            self.assertIsNone(self.provider.native_handshake)
            self.assertFalse(self.provider._startup_accepting)
        finally:
            if self.provider.temp is not None:
                self.provider.temp.cleanup()

    def test_feature_marker_rejects_old_helper_without_changing_physical_gate(self):
        bundle = self.root / 'ReproLiveTests.xctest'
        bundle.mkdir()
        info = bundle / 'Info.plist'
        value = {'ReproLiveProtocolVersion': 2, 'ReproLiveHelperVersion': 2}
        info.write_bytes(plistlib.dumps(value))
        document = {'__xctestrun_metadata__': {'FormatVersion': 1}, 'ReproLiveTests': {
            'BlueprintName': 'ReproLiveTests', 'TestBundlePath': str(bundle)}}
        providers.require_ios_helper_v2(document)
        for bad in (None, True, '1', 0, 2):
            with self.subTest(bad=bad):
                value['ReproLiveSimulatorStartupBudgetVersion'] = bad if bad is not None else 'absent'
                info.write_bytes(plistlib.dumps(value))
                with self.assertRaisesRegex(LiveError, 'Rebuild'):
                    providers.require_ios_helper_v2(document, simulator_startup_budget=True)
        value['ReproLiveSimulatorStartupBudgetVersion'] = 1
        info.write_bytes(plistlib.dumps(value))
        providers.require_ios_helper_v2(document, simulator_startup_budget=True)


class IOSStartupBudgetNativeSourceTests(unittest.TestCase):
    def test_native_completion_semaphores_use_zero_initial_count(self):
        # This is a source regression guard, not a native execution claim.
        source = (Path(__file__).resolve().parents[1] /
                  'live-ios/Tests/LiveControlTests.swift').read_text()
        begin = source.index('    private func completedStartupResponseSemaphore()')
        end = source.index('    func testCaptureFormats()', begin)
        pure_tests = source[begin:end]
        self.assertEqual(re.findall(r'DispatchSemaphore\s*\(\s*value\s*:\s*([^)]+)\)',
                                    pure_tests), ['0', '0'])
        helper = pure_tests[:pure_tests.index('    func testSimulatorStartupBudget')]
        self.assertRegex(helper, r'(?s)let semaphore = DispatchSemaphore\(value: 0\).*'
                                 r'semaphore\.signal\(\).*return semaphore')
        self.assertEqual(pure_tests.count('= completedStartupResponseSemaphore()'), 3)
        for assertion in (
                'completedResponse.wait(timeout: .now()), .timedOut',
                'lateResponse.wait(timeout: .now()), .timedOut',
                'unconsumedResponse.wait(timeout: .now()), .success'):
            self.assertIn(assertion, pure_tests)


if __name__ == '__main__':
    unittest.main()
