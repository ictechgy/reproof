import copy
import threading
import unittest
from unittest.mock import Mock

from reproloop.live.android_live import AndroidLiveProvider
from reproloop.live.model import LiveError


def capture(started=200):
    return {
        'schemaVersion': 1,
        'sessionId': 'android-session-1',
        'fixture': {'id': 'default', 'version': 1, 'inputs': {}},
        'startState': {'screen': 'main', 'nodes': {'count': '0', 'name': ''}},
        'events': [{'id': 'e1', 'seq': 1, 'action': 'tap', 'target': 'add', 'parameters': {}}],
        'truncated': False,
        'lostEvents': False,
        'endSequence': 1,
        'startedAtMs': started,
    }


def metadata(c):
    return {
        'schemaVersion': 1,
        'sessionId': c['sessionId'],
        'fixture': {'id': 'default', 'version': 1},
        'startState': copy.deepcopy(c['startState']),
        'finalized': True,
        'incomplete': False,
        'lostEvents': False,
        'unsupported': False,
    }


class AndroidCaptureContractTests(unittest.TestCase):
    def provider(self):
        provider = object.__new__(AndroidLiveProvider)
        provider.general_profile = False
        provider.record_sdk = True
        provider.automatic_app_logs = False
        provider.capture_started_at = 100
        provider.capture_session_id = 'android-session-1'
        return provider

    def test_valid_capture_requires_finalized_matching_session(self):
        provider = self.provider()
        value = capture()
        self.assertIs(provider._validate_sdk_capture(value, metadata(value)), value)

    def test_rejects_stale_incomplete_private_or_mismatched_capture(self):
        cases = []
        incomplete = capture()
        incomplete['lostEvents'] = True
        cases.append((incomplete, metadata(incomplete)))
        wrong_session = capture()
        wrong_metadata = metadata(wrong_session)
        wrong_metadata['sessionId'] = 'other-session'
        cases.append((wrong_session, wrong_metadata))
        unsafe = capture()
        unsafe['events'][0]['target'] = 'password'
        cases.append((unsafe, metadata(unsafe)))
        for value, marker in cases:
            with self.subTest(value=value, marker=marker):
                with self.assertRaises(LiveError) as raised:
                    self.provider()._validate_sdk_capture(value, marker)
                self.assertEqual(raised.exception.code, 'capture_invalid')

    def test_collect_clicks_report_without_driver_instrumentation(self):
        provider = self.provider()
        provider._execute = Mock(return_value={'ok': True})
        values = {'files/repro/capture.json': capture(),
                  'files/repro/android-session-1/session.json': metadata(capture())}
        provider._read_sdk_json = lambda path: values[path]
        provider._read_sdk_text = lambda path: 'android-session-1'
        self.assertEqual(provider.collect_sdk_capture()['sessionId'], 'android-session-1')
        provider._execute.assert_called_once_with('report_capture', {}, permit=None)

    def test_capture_publication_waits_for_finalized_metadata(self):
        provider = self.provider()
        provider._execute = Mock(return_value={'ok': True})
        value = capture()
        pending = metadata(value)
        pending['finalized'] = False
        reads = iter([pending, metadata(value)])
        provider._read_sdk_json = lambda path: next(reads) if path.endswith('session.json') else value
        provider._read_sdk_text = lambda path: 'android-session-1'
        self.assertEqual(provider.collect_sdk_capture()['sessionId'], 'android-session-1')

    def test_new_sdk_uuid_is_rejected_even_when_capture_timestamp_is_newer(self):
        provider = self.provider()
        provider._execute = Mock(return_value={'ok': True})
        value = capture(10_000)
        value['sessionId'] = 'new-process-session'
        provider._read_sdk_json = lambda path: value
        provider._read_sdk_text = lambda path: 'new-process-session'
        with self.assertRaises(LiveError) as raised:
            provider.collect_sdk_capture()
        self.assertEqual(raised.exception.code, 'capture_invalid')

    def test_device_clock_skew_is_allowed_for_matching_uuid(self):
        provider = self.provider()
        provider.capture_started_at = 20_000
        provider._execute = Mock(return_value={'ok': True})
        value = capture(1)
        provider._read_sdk_json = lambda path: value if path.endswith('capture.json') else metadata(value)
        provider._read_sdk_text = lambda path: 'android-session-1'
        self.assertEqual(provider.collect_sdk_capture()['startedAtMs'], 1)

    def test_finalized_incomplete_metadata_is_terminal(self):
        provider = self.provider()
        provider._execute = Mock(return_value={'ok': True})
        value = capture()
        marker = metadata(value)
        marker['incomplete'] = True
        provider._read_sdk_json = lambda path: value if path.endswith('capture.json') else marker
        provider._read_sdk_text = lambda path: 'android-session-1'
        with self.assertRaises(LiveError) as raised:
            provider.collect_sdk_capture()
        self.assertEqual(raised.exception.code, 'capture_invalid')

    def test_reset_pins_the_new_sdk_uuid(self):
        provider = self.provider()
        provider.stop = threading.Event()
        provider.transport = Mock()
        # The command ID is generated internally, so return its value in the
        # acknowledgement by mirroring the generated call payload below.
        def call(path, body=None, **kwargs):
            if path == '/command':
                provider._reset_command_id = body['id']
                return {'accepted': True}
            return {'pending': False, 'id': provider._reset_command_id, 'ok': True}
        provider.transport.call.side_effect = call
        provider._receive_frame = Mock()
        provider._wait_for_sdk_session = Mock(return_value='android-session-after-reset')
        self.assertTrue(provider._execute('reset', {}).get('ok'))
        self.assertEqual(provider.capture_session_id, 'android-session-after-reset')


if __name__ == '__main__':
    unittest.main()
