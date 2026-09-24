"""Stop excludes new frames while bounded admitted publications finish."""
import threading
import time
import unittest
from unittest import mock

from reproof.live.model import LiveError
from tests import test_g2_lab_integration as fixture


class RecordingFrameStopTests(unittest.TestCase):
    setUp = fixture.ReleaseLabTests.setUp
    tearDown = fixture.ReleaseLabTests.tearDown
    register = fixture.ReleaseLabTests.register
    create = fixture.ReleaseLabTests.create
    command = fixture.ReleaseLabTests.command

    def _exercise(self, finish_before_stop, *, publication_fails=False):
        session = self.create(self.register())
        state = self.lab._session(session['id'], 'owner')
        recorder = state['releaseRecorder']
        original = recorder.record_frame
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        results, errors = [], []
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test publication was not released')
            if publication_fails:
                raise RuntimeError('synthetic admitted publication failure')
            return original(*args, **kwargs)
        def publish():
            self.lab.publish_frame(session['id'], b'<svg>second</svg>', 'image/svg+xml',
                                   400, 800, acquisition_sequence=2)
        def stop():
            try:
                results.append(self.lab.stop_release_recording(
                    session['id'], 'owner', session['controllerId'], session['epoch']))
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                stopped.set()
        producer = threading.Thread(target=publish, daemon=True)
        finalizer = threading.Thread(target=stop, daemon=True)
        try:
            with mock.patch.object(recorder, 'record_frame', blocked):
                producer.start()
                self.assertTrue(entered.wait(2))
                finalizer.start()
                deadline = time.monotonic() + 2
                while not state.get('stopAdmission') and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(state.get('stopAdmission'))
                with self.assertRaises(LiveError):
                    self.lab.input(session['id'], 'owner', self.command(session))
                self.assertEqual(self.provider.calls, [])
                self.assertFalse(self.lab.publish_frame(
                    session['id'], b'<svg>late</svg>', 'image/svg+xml',
                    400, 800, acquisition_sequence=3))
                if finish_before_stop:
                    self.assertFalse(stopped.wait(.1), 'stop skipped an admitted publication')
                    release.set()
                self.assertTrue(stopped.wait(3))
            self.assertEqual(errors, [])
            frozen = results[0]
            complete = finish_before_stop and not publication_fails
            self.assertEqual(frozen['status'], 'frozen-complete' if complete else 'frozen-incomplete')
            self.assertEqual(len(frozen['original']['media']), 2 if complete else 1)
            if publication_fails:
                self.assertIn('frame_publication_failed',
                              {item['reason'] for item in frozen['original']['interruptions']})
        finally:
            release.set()
            producer.join(5)
            if finalizer.ident is not None:
                finalizer.join(5)
            self.assertFalse(producer.is_alive() or finalizer.is_alive())

    def test_admitted_frame_finishes_before_frozen_barrier(self):
        self._exercise(True)

    def test_stalled_frame_keeps_bounded_stop_incomplete(self):
        self._exercise(False)

    def test_failed_admitted_frame_during_drain_keeps_original_incomplete(self):
        self._exercise(True, publication_fails=True)


if __name__ == '__main__':
    unittest.main()
