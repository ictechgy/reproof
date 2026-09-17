"""Original expiry must not erase the proof needed to retire source bytes."""
from pathlib import Path
import tempfile
import unittest

from reproloop.live.video import VideoCatalog
from tests.test_recording_recovery import collection_policy, project_document
from tests.test_video_source_recovery import _crash_process, _open


class VideoRecoveryRetentionTests(unittest.TestCase):
    def test_expired_original_keeps_source_ledger_until_pending_cleanup_finishes(self):
        with tempfile.TemporaryDirectory(prefix="reproloop-video-retention-") as directory:
            root = Path(directory)
            self.assertEqual(_crash_process(root, "after_g2_attach"), 43)
            budget, evidence, recordings, _clock = _open(root)
            catalog = None
            try:
                registration = recordings.register_project(project_document(), collection_policy())
                recovery = recordings.recovery_session("recording_one")
                frozen = recovery.freeze_recovered()
                sources = recordings.source_timeline("recording_one")
                self.assertEqual(len(sources), 1)
                evidence.tombstone(frozen["recordingDigest"], reason="retention_expired")

                self.assertIsNone(recordings.load("recording_one")["original"])
                self.assertEqual(recordings.source_timeline("recording_one"), sources)
                self.assertEqual(recordings.pending_video_recoveries(registration.project_digest),
                                 ("recording_one",))
                catalog = VideoCatalog(root / "video", evidence)
                result = catalog.recover_transient(recovery)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(recordings.pending_video_recoveries(registration.project_digest), ())
                self.assertEqual(recordings.source_timeline("recording_one"), [])
                self.assertIsNone(recordings.load("recording_one")["original"])
            finally:
                if catalog is not None:
                    catalog.close()
                recordings.close()
                evidence.close()
                budget.close()


if __name__ == "__main__":
    unittest.main()
