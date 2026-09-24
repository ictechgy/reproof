import copy
from contextlib import nullcontext
import unittest
from unittest.mock import Mock, patch

from reproof.android_profile import validate_app_profile
from reproof.core import ContractError
from reproof.device import AdbDevice, DeviceError
from tests.test_instrumentation_contract import auto_profile_document, captured, diagnostics


class InstrumentedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.profile = validate_app_profile(auto_profile_document())
        self.device = object.__new__(AdbDevice)
        self.device.authority_lease = nullcontext()
        self.device.app_profile = self.profile
        self.device.package = self.profile.data['package']
        self.device.sdk_session_id = 'auto-session'
        self.device.shell = Mock(return_value='Broadcast completed: result=0\n')
        self.device.driver = Mock(side_effect=AssertionError('Live capture must not start another UI driver'))
        self.device._sdk_session = Mock(return_value='auto-session')

    def test_broadcast_acceptance_waits_for_current_finalized_capture(self):
        current = captured(self.profile)
        stale = dict(current, sessionId='old-session')
        pending = {'finalized': False}
        ready = {'sessionId': 'auto-session', 'finalized': True, 'incomplete': False,
                 'lostEvents': False, 'unsupported': False}
        self.device._sdk_json = Mock(side_effect=[stale, current, pending, current, ready])
        with patch('reproof.device.time.sleep'):
            self.assertEqual(self.device.freeze_capture(), current)
        self.device.driver.assert_not_called()
        args = self.device.shell.call_args.args
        self.assertEqual(args[-1], self.device.package + '/io.reproof.autotrace.AutoExportReceiver')
        self.assertIn('io.reproof.EXPORT_CAPTURE', args)

    def test_rejected_export_does_not_read_an_old_capture(self):
        self.device.shell.return_value = 'Broadcast completed: result=1\n'
        self.device._sdk_json = Mock()
        with self.assertRaises(ContractError):
            self.device.freeze_capture()
        self.device._sdk_json.assert_not_called()

    def test_diagnostics_wait_for_publication_and_reject_sequence_mismatch(self):
        self.device._sdk_json = Mock(side_effect=[DeviceError('Not published'), diagnostics(self.profile)])
        with patch('reproof.device.time.sleep'):
            value = self.device.collect_instrumentation_diagnostics(captured(self.profile))
        self.assertEqual(value['actions'][0]['eventId'], 'e2')
        invalid = diagnostics(self.profile); invalid['endSequence'] = 1
        self.device._sdk_json = Mock(return_value=invalid)
        with self.assertRaises(ContractError):
            self.device.collect_instrumentation_diagnostics(captured(self.profile))

    def test_session_change_during_diagnostic_read_is_rejected(self):
        self.device._sdk_json = Mock(return_value=diagnostics(self.profile))
        self.device._sdk_session = Mock(side_effect=['auto-session', 'changed'])
        with self.assertRaises(ContractError):
            self.device.collect_instrumentation_diagnostics(captured(self.profile))


if __name__ == '__main__':
    unittest.main()
