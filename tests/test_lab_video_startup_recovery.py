"""Trusted project registration reconciles interrupted video setup."""
from pathlib import Path
import multiprocessing
import os
import tempfile
import unittest
from unittest import mock

from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.frame_spool import FrameSpool
from reproof.live.model import Lab
from reproof.live.recording_session import RecordingSession
from reproof.live.video import VideoCatalog, VideoFrameSink
from tests.test_clock_sync import FakeClock
from tests.test_recording_recovery import begin_recording, collection_policy, project_document
from tests.test_video_state_machine import FakeEncoder, limits


def _open_lab(root):
    return Lab([], root, recording_clock_sync=ClockSynchronizer(FakeClock(1_000_000_000)),
               recording_wall_clock_ms=lambda: 5_000_000)


def _crash_setup(root, boundary):
    lab = _open_lab(root)
    lab.register_recording_project(project_document(), collection_policy())
    _, session = begin_recording(lab._recording_store)
    sink = VideoFrameSink(lab._evidence_store, Path(root) / 'evidence-v1' / 'video',
                         encoder=FakeEncoder(), limits=limits(max_segments=8),
                         source_mode='transient-spool-v2')
    if boundary == 'before-spool':
        with mock.patch.object(FrameSpool, '__init__', side_effect=lambda *a, **kw: os._exit(61)):
            session.attach_frame_sink(sink)
    else:
        original = RecordingSession.use_frame_spool
        def bind_then_crash(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(62)
        with mock.patch.object(RecordingSession, 'use_frame_spool', bind_then_crash):
            session.attach_frame_sink(sink)
    raise AssertionError('setup boundary did not exit')


class LabVideoStartupRecoveryTests(unittest.TestCase):
    def test_project_registration_reconciles_both_prebinding_source_modes(self):
        for boundary, expected in (('before-spool', 'aborted'), ('after-g2', 'bound')):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                process = multiprocessing.get_context('spawn').Process(
                    target=_crash_setup, args=(str(root), boundary))
                process.start()
                process.join(20)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                self.assertEqual(process.exitcode, 61 if boundary == 'before-spool' else 62)
                lab = _open_lab(root)
                catalog = None
                try:
                    registration = lab.register_recording_project(project_document(), collection_policy())
                    catalog = VideoCatalog(root / 'evidence-v1' / 'video', lab._evidence_store)
                    row = catalog.journal.connection.execute(
                        'SELECT binding_state FROM sessions WHERE recording_id=?', ('recording_one',)).fetchone()
                    self.assertEqual(row['binding_state'], expected)
                    self.assertEqual(lab._recording_store.pending_video_recoveries(
                        registration.project_digest), ())
                    for category in ('spool', 'encoding'):
                        self.assertFalse(any(item['state'] == 'active' for item in
                            lab._recording_budget.reservations_for_owner('recording_one', category=category)))
                finally:
                    if catalog is not None:
                        catalog.close()
                    lab.close_all()


if __name__ == '__main__':
    unittest.main()
