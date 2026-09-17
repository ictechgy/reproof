"""Parent-owned fault probes for the existing public Lab API.

Run from the repository root with PYTHONPATH=. and unittest discover in this
directory. These tests use synthetic providers; they never open a device.
"""

from pathlib import Path
import tempfile
import threading
import unittest

from reproloop.live.model import Lab


class BlockingProvider:
    def __init__(self, blocked_operation):
        self.blocked_operation = blocked_operation
        self.entered = threading.Event()
        self.release = threading.Event()

    def start(self, session, lab):
        self.session = session
        self.lab = lab
        lab.publish_frame(session['id'], b'<svg/>', 'image/svg+xml', 400, 800)

    def _block(self, operation):
        if operation == self.blocked_operation:
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError('Synthetic callback deadline')

    def execute(self, action, payload):
        self._block(action)
        return {'ok': True, 'timing': 'best-effort'}

    def observe(self):
        self._block('observe')
        return {'ready': True}

    def collect_app_logs(self):
        self._block('app_logs')
        return {'sessionId': '12345678-1234-1234-1234-123456789abc'}

    def close(self):
        pass


class LabRevocationBoundaries(unittest.TestCase):
    def _assert_revocation_not_blocked(self, operation):
        provider = BlockingProvider(operation)
        device = {
            'id': 'synthetic-revocation', 'name': 'Synthetic revocation probe',
            'kind': 'demo', 'platform': 'demo', 'factory': lambda: provider,
            'capabilities': {
                'actions': ['tap', 'reset'], 'media': 'demo-svg',
                'automaticAppLogs': True,
            },
        }
        with tempfile.TemporaryDirectory(prefix='g1b-parent-probe-') as directory:
            lab = Lab([device], Path(directory))
            session = lab.create_session(device['id'], 'owner', 'controller')
            self.assertEqual(session['state'], 'active')
            frame = lab.frame(session['id'])
            command = {
                'controllerId': session['controllerId'], 'epoch': session['epoch'],
                'sequence': 1, 'commandId': 'blocked-input',
                'frameId': frame['id'], 'geometryVersion': frame['geometryVersion'],
                'action': 'tap', 'payload': {'x': .5, 'y': .5},
            }
            callbacks = {
                'tap': lambda: lab.input(session['id'], 'owner', command),
                'reset': lambda: lab.start_recording(
                    session['id'], 'owner', session['controllerId'],
                    session['epoch'], reset=True),
                'observe': lambda: lab.observe(session['id'], 'owner'),
                'app_logs': lambda: lab.app_logs(session['id'], 'owner'),
            }
            callback_errors = []
            fail_errors = []
            fail_returned = threading.Event()

            def invoke_callback():
                try:
                    callbacks[operation]()
                except Exception as error:
                    callback_errors.append(type(error).__name__)

            def revoke():
                try:
                    lab.fail(session['id'], 'Synthetic transport interruption')
                except Exception as error:
                    fail_errors.append(type(error).__name__)
                finally:
                    fail_returned.set()

            callback_thread = threading.Thread(target=invoke_callback, daemon=True)
            fail_thread = threading.Thread(target=revoke, daemon=True)
            returned_before_provider = False
            callback_thread.start()
            try:
                entered = provider.entered.wait(2)
                if entered:
                    fail_thread.start()
                    returned_before_provider = fail_returned.wait(.6)
            finally:
                provider.release.set()
                callback_thread.join(3)
                if fail_thread.ident is not None:
                    fail_thread.join(3)
            try:
                self.assertTrue(entered, f'Provider callback was not reached: {callback_errors}')
                self.assertFalse(callback_thread.is_alive(), 'Synthetic callback leaked')
                self.assertFalse(fail_thread.is_alive(), 'Revocation thread leaked')
                self.assertEqual(fail_errors, [])
                self.assertTrue(
                    returned_before_provider,
                    f'{operation} callback prevented the host from revoking the session',
                )
                self.assertEqual(lab.get_session(session['id'])['state'], 'failed')
                self.assertEqual(lab.list_devices()[0]['state'], 'quarantined')
            finally:
                lab.close_all()

    def test_blocked_input_does_not_delay_revocation(self):
        self._assert_revocation_not_blocked('tap')

    def test_blocked_reset_does_not_delay_revocation(self):
        self._assert_revocation_not_blocked('reset')

    def test_blocked_observation_does_not_delay_revocation(self):
        self._assert_revocation_not_blocked('observe')

    def test_blocked_app_log_collection_does_not_delay_revocation(self):
        self._assert_revocation_not_blocked('app_logs')


if __name__ == '__main__':
    unittest.main()
