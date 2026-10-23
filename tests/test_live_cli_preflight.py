"""Live CLI input errors must precede providers, state, and server creation."""
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof.live.cli import main


class ReachedLab(RuntimeError):
    pass


class LiveCLIPreflightTests(unittest.TestCase):
    def test_invalid_numeric_options_fail_before_effects(self):
        cases = [
            (['--idle-timeout', '9'], 'Idle timeout'),
            (['--idle-timeout', '3601'], 'Idle timeout'),
            (['--max-session-seconds', '0'], 'Session lifetime'),
            (['--max-session-seconds', '7201'], 'Session lifetime'),
            (['--idle-timeout', '20', '--max-session-seconds', '10'], 'Session lifetime'),
            (['--job-concurrency', '0'], 'Job concurrency'),
            (['--job-concurrency', '65'], 'Job concurrency'),
            (['--port', '-1'], 'Port'),
            (['--port', '65536'], 'Port'),
            (['--demo-count', '0'], 'Demo device count'),
            (['--demo-count', '9'], 'Demo device count'),
        ]
        for options, reason in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / 'output'
                errors = io.StringIO()
                with patch('reproof.live.cli.Lab') as lab, patch('reproof.live.cli.LiveServer') as server, \
                     patch('reproof.live.cli.demo_device', side_effect=AssertionError('provider reached')) as provider, \
                     redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
                    main(['live-serve', '--demo', '--output', str(output), *options])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(reason, errors.getvalue())
                lab.assert_not_called(); server.assert_not_called(); provider.assert_not_called()
                self.assertFalse(output.exists())

    def test_numeric_preflight_precedes_shared_configuration(self):
        with patch('reproof.live.configuration.load_shared_configuration', side_effect=AssertionError('config reached')) as load, \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['live-serve', '--shared-config', 'not-read.json', '--idle-timeout', '0'])
        self.assertEqual(raised.exception.code, 2)
        load.assert_not_called()

    def test_valid_numeric_boundaries_reach_lab_unchanged(self):
        for idle, lifetime, jobs, demos, port in ((10, 10, 1, 1, 0), (3600, 7200, 64, 8, 65535)):
            with self.subTest(idle=idle), tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / 'output'
                with patch('reproof.live.cli.demo_device', side_effect=lambda: {'id': 'demo-1'}), \
                     patch('reproof.live.cli.Lab', side_effect=ReachedLab) as lab, \
                     patch('reproof.live.cli.LiveServer') as server, self.assertRaises(ReachedLab):
                    main(['live-serve', '--demo', '--output', str(output), '--idle-timeout', str(idle),
                          '--max-session-seconds', str(lifetime), '--job-concurrency', str(jobs),
                          '--demo-count', str(demos), '--port', str(port)])
                self.assertEqual(lab.call_args.kwargs['idle_timeout'], idle)
                self.assertEqual(lab.call_args.kwargs['max_session_seconds'], lifetime)
                self.assertEqual(len(lab.call_args.args[0]), demos)
                server.assert_not_called()
                self.assertFalse(output.exists())

    def test_profile_io_error_is_concise_and_interrupt_propagates(self):
        for error in (PermissionError('private path detail'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temp:
                errors = io.StringIO(); output = Path(temp) / 'output'
                with patch('reproof.ios_profile.load_ios_profile', side_effect=error), \
                     patch('reproof.ios_runner.IosSimulator') as native, \
                     patch('reproof.live.cli.Lab') as lab, redirect_stderr(errors):
                    if isinstance(error, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            main(['live-serve', '--simulator', 'fixture', '--ios-profile', 'fixture.json', '--output', str(output)])
                    else:
                        with self.assertRaises(SystemExit) as raised:
                            main(['live-serve', '--simulator', 'fixture', '--ios-profile', 'fixture.json', '--output', str(output)])
                        self.assertEqual(raised.exception.code, 2)
                        self.assertIn('Cannot read iOS runtime profile', errors.getvalue())
                        self.assertNotIn('private path detail', errors.getvalue())
                    native.assert_not_called(); lab.assert_not_called()
                self.assertFalse(output.exists())

    def test_invalid_profiles_exit_cleanly_before_native_or_output(self):
        selectors = (
            (['--simulator', 'fixture'], '--ios-profile', 'iOS runtime profile'),
            (['--android', 'fixture', '--android-helper', 'fixture.apk', '--android-app', 'fixture.apk'],
             '--app-profile', 'Android app profile'),
            (['--android', 'fixture', '--android-helper', 'fixture.apk', '--android-app', 'fixture.apk'],
             '--android-profile', 'Android runtime profile'),
        )
        for selection, option, reason in selectors:
            for present in (False, True):
                with self.subTest(option=option, present=present), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp); profile = root / 'profile.json'; output = root / 'output'
                    if present: profile.write_bytes(b'{}')
                    errors = io.StringIO()
                    with patch('reproof.ios_runner.IosSimulator', side_effect=AssertionError('native reached')) as native, \
                         patch('reproof.live.android_live.android_live_device', side_effect=AssertionError('Android Live reached')) as android_live, \
                         patch('reproof.live.android.android_device', side_effect=AssertionError('Android reached')) as android, \
                         patch('reproof.device.AdbDevice', side_effect=AssertionError('ADB reached')) as adb, \
                         patch('reproof.live.cli.Lab', side_effect=AssertionError('Lab reached')) as lab, \
                         patch('reproof.live.cli.LiveServer') as server, \
                         redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
                        main(['live-serve', *selection, option, str(profile), '--output', str(output)])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn(reason, errors.getvalue())
                    native.assert_not_called(); lab.assert_not_called(); server.assert_not_called()
                    android_live.assert_not_called(); android.assert_not_called(); adb.assert_not_called()
                    self.assertFalse(output.exists())
                    if present: self.assertEqual(profile.read_bytes(), b'{}')
                    else: self.assertFalse(profile.exists())


if __name__ == '__main__':
    unittest.main()
