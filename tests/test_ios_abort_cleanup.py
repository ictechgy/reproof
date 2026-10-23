"""Local-only Simulator cancellation, with no native/device execution."""
from pathlib import Path
import signal
import subprocess
import threading
import tempfile
import unittest
from unittest.mock import Mock, patch

from reproof.live import providers
from reproof.live.model import Lab, LiveError
from tests.test_live_authority_integration import Clock, parent_grant
from reproof.live.authority import HostAuthority


class Process(subprocess.Popen):
    def __init__(self, outcomes=(0,)):
        self._child_created = False
        self.pid = 424242
        self.returncode = None
        self._waitpid_lock = threading.Lock()
        self.outcomes = iter(outcomes)
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        outcome = next(self.outcomes, self.returncode)
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired('owned-runner', timeout)
        with self._waitpid_lock:
            self.returncode = outcome
        return outcome


class IOSAbortCleanupTests(unittest.TestCase):
    def setUp(self):
        self.provider = providers.IosProvider('synthetic', Path('/tmp/products'), 'example.bundle')
        self.process = Process()
        self.provider.process = self.process
        self.provider._owned_process = (self.process, self.process.pid)
        self.provider.lease = Mock()
        self.provider.device_authority = Mock()
        self.provider.temp = Mock()
        self.provider.frame_clock = Mock()
        self.provider.execute_authorized = Mock(side_effect=AssertionError('native dispatch'))
        self.provider._startup_accepting = True
        self.kill = self.enterContext(patch.object(providers.os, 'killpg'))
        self.pgid = self.enterContext(patch.object(providers.os, 'getpgid', return_value=self.process.pid))
        self.sid = self.enterContext(patch.object(providers.os, 'getsid', return_value=self.process.pid))

    def assert_local_only(self):
        self.assertTrue(self.provider.stop.is_set())
        self.assertFalse(self.provider._startup_accepting)
        self.assertEqual(self.provider.token, '')
        self.provider.execute_authorized.assert_not_called()
        self.provider.device_authority.assert_not_called()
        self.assertEqual(self.provider.device_authority.mock_calls, [])
        self.assertEqual(self.provider.lease.mock_calls, [])
        self.provider.temp.cleanup.assert_not_called()

    def test_spawn_records_exact_owned_new_session_only_under_default_reaping(self):
        for before, after in [(signal.SIG_DFL, signal.SIG_DFL),
                              (signal.SIG_IGN, signal.SIG_DFL),
                              (signal.SIG_DFL, signal.SIG_IGN)]:
            with self.subTest(before=before, after=after), \
                 patch.object(providers.subprocess, 'Popen', return_value=self.process) as spawn, \
                 patch.object(providers.signal, 'getsignal', side_effect=[before, after]):
                self.provider._start_runner(['synthetic-runner'])
                spawn.assert_called_once_with(['synthetic-runner'], stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, start_new_session=True)
                expected = ((self.process, self.process.pid)
                            if before == after == signal.SIG_DFL else None)
                self.assertEqual(self.provider._owned_process, expected)

    def test_term_is_local_and_repeated_abort_does_not_resignal(self):
        self.assertIsNone(self.provider.abort())
        self.provider.abort()
        self.kill.assert_called_once_with(self.process.pid, signal.SIGTERM)
        self.assertEqual(self.process.waits, [5, 5])
        self.assert_local_only()

    def test_escalates_with_bounded_waits(self):
        self.process.outcomes = iter(['timeout', -9])
        self.provider.abort()
        self.assertEqual(self.kill.call_args_list,
                         [((self.process.pid, signal.SIGTERM),), ((self.process.pid, signal.SIGKILL),)])
        self.assertEqual(self.process.waits, [5, 5])
        self.assert_local_only()

    def test_kill_timeout_preserves_evidence_and_does_not_claim_cleanup(self):
        self.process.outcomes = iter(['timeout', 'timeout'])
        with self.assertRaises(subprocess.TimeoutExpired):
            self.provider.abort()
        self.assert_local_only()

    def test_already_reaped_process_never_signals_reused_group(self):
        self.process.returncode = 1
        self.provider.abort()
        self.kill.assert_not_called()
        self.pgid.assert_not_called()
        self.assert_local_only()

    def test_missing_group_is_safe_but_still_waits_for_owned_child(self):
        self.pgid.side_effect = ProcessLookupError()
        self.provider.abort()
        self.kill.assert_not_called()
        self.assertEqual(self.process.waits, [5])
        self.assert_local_only()

    def test_identity_mismatches_fail_closed(self):
        for change in ['replaced', 'pid', 'group', 'session', 'unrecorded', 'no-lock', 'unsafe-pid']:
            with self.subTest(change=change):
                original_pid = self.process.pid
                original_lock = self.process._waitpid_lock
                if change == 'replaced': self.provider.process = Process()
                if change == 'pid': self.process.pid += 1
                if change == 'group': self.pgid.return_value = 123
                if change == 'session': self.sid.return_value = 123
                if change == 'unrecorded': self.provider._owned_process = None
                if change == 'no-lock': self.process._waitpid_lock = None
                if change == 'unsafe-pid':
                    self.process.pid = 1
                    self.provider._owned_process = (self.process, 1)
                with self.assertRaises(LiveError): self.provider.abort()
                self.kill.assert_not_called()
                self.assert_local_only()
                self.process.pid = original_pid
                self.process._waitpid_lock = original_lock
                self.provider.process = self.process
                self.provider._owned_process = (self.process, self.process.pid)
                self.pgid.return_value = self.sid.return_value = self.process.pid

    def test_physical_subclass_without_recorded_spawn_cannot_signal(self):
        from reproof.live.iphone import PhysicalIosProvider
        physical = object.__new__(PhysicalIosProvider)
        providers.IosProvider.__init__(physical, 'synthetic-phone', Path('/tmp/products'), 'example.bundle')
        physical.process = self.process
        with self.assertRaises(LiveError): physical.abort()
        self.kill.assert_not_called()

    def test_nonstandard_child_reaping_policy_fails_closed(self):
        for handler in [signal.SIG_IGN, lambda *_: None]:
            with self.subTest(handler=handler), patch.object(providers.signal, 'getsignal', return_value=handler):
                with self.assertRaises(LiveError): self.provider.abort()
                self.kill.assert_not_called()
                self.assert_local_only()

    def test_reaping_cannot_race_identity_check_and_signal(self):
        def signal_under_guard(pid, signum):
            acquired = self.process._waitpid_lock.acquire(blocking=False)
            if acquired: self.process._waitpid_lock.release()
            self.assertFalse(acquired, 'Concurrent poll must not reap while signaling')
        self.kill.side_effect = signal_under_guard
        self.provider.abort()
        self.kill.assert_called_once()
        self.assert_local_only()

    def test_reaped_between_term_and_kill_never_signals_again(self):
        def wait(timeout=None):
            self.process.returncode = 1
            if self.kill.call_count == 1 and not hasattr(self.process, 'timed_out'):
                self.process.timed_out = True
                raise subprocess.TimeoutExpired('owned-runner', timeout)
            return 1
        self.process.wait = wait
        self.provider.abort()
        self.kill.assert_called_once_with(self.process.pid, signal.SIGTERM)
        self.assert_local_only()

    def test_frame_clock_error_cannot_skip_runner_teardown(self):
        self.provider.frame_clock.close.side_effect = RuntimeError('clock')
        with self.assertRaises(RuntimeError): self.provider.abort()
        self.kill.assert_called_once_with(self.process.pid, signal.SIGTERM)
        self.assert_local_only()

    def test_no_spawn_is_safe(self):
        self.provider.process = self.provider._owned_process = None
        self.provider.abort()
        self.kill.assert_not_called()
        self.assert_local_only()

    def test_lab_cleanup_failure_remains_failed_and_quarantined(self):
        self._lab_cleanup_failure('rejected')

    def test_lab_cleanup_exception_remains_failed_and_quarantined(self):
        self._lab_cleanup_failure('exception')

    def test_lab_revoked_permit_remains_failed_and_quarantined(self):
        self._lab_cleanup_failure('revoked')

    def _lab_cleanup_failure(self, failure):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = Clock()
            authority = HostAuthority(root / 'authority.sqlite3', clock=clock,
                                      lease_directory=root / 'leases')
            self.addCleanup(authority.close)
            def start(session, lab, permit):
                self.provider.device_authority.check_dispatch_permit(permit)
                lab.publish_frame(session['id'], b'<svg/>', 'image/svg+xml', 400, 800)
                return {'ok': True}
            self.provider.start_authorized = start
            self.provider.execute_authorized = Mock(return_value={'ok': False})
            descriptor = {'id': 'sim', 'name': 'Synthetic', 'platform': 'ios',
                'kind': 'ios-simulator', 'capabilities': {'actions': ['tap']},
                'factory': lambda: self.provider,
                '_authority': {'deviceKind': 'ios-simulator', 'physicalId': 'synthetic'}}
            lab = Lab([descriptor], root / 'live', authority=authority,
                      parent_grant=parent_grant(authority, clock))
            session = lab.create_session('sim', 'owner', 'browser')
            handle = self.provider.device_authority
            if failure == 'exception':
                self.provider.execute_authorized.side_effect = TimeoutError('native ack missing')
            if failure == 'revoked':
                lab.fail(session['id'], 'Startup was not confirmed')
            with patch.object(handle, 'confirm_native_cleanup') as confirm:
                result = lab.close_session(session['id'], 'owner')
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(lab.list_devices()[0]['state'], 'quarantined')
            durable = authority.inventory_ownership(device_kind='ios-simulator', physical_id='synthetic')
            self.assertEqual(durable['status'], 'quarantined')
            self.assertIsNone(durable['releaseReceiptDigest'])
            confirm.assert_not_called()
            self.kill.assert_called_once_with(self.process.pid, signal.SIGTERM)
            self.provider.temp.cleanup.assert_not_called()
            if failure == 'revoked': self.provider.execute_authorized.assert_not_called()
            else: self.provider.execute_authorized.assert_called_once()


if __name__ == '__main__':
    unittest.main()
