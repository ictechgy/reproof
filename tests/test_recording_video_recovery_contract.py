"""Recovery has persisted observation authority, never live input authority."""
import dataclasses
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore
from reproof.live.recording_session import RecordingStore, RecordingStoreError
from reproof.live.video import VideoFrameSink
from tests.test_clock_sync import FakeClock
from tests.test_recording_recovery import begin_recording, project_document, collection_policy
from tests.test_video_state_machine import FakeEncoder, limits


class RecordingVideoRecoveryContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.budget = DiskBudget(self.root / 'budget', capacity_bytes=512 * 1024 * 1024,
            journal_headroom_bytes=8 * 1024 * 1024, free_bytes=lambda: 1 << 30)
        self.evidence = EvidenceStore(self.root / 'objects', self.budget)
        self.clock = FakeClock(1_000_000_000)
        self.store = self.open_store()
        self.registration, self.session = begin_recording(self.store)
        self.limits = limits(segment_duration_ms=997, max_segments=7)
        self.sink = VideoFrameSink(self.evidence, self.root / 'video', encoder=FakeEncoder(),
            limits=self.limits, source_mode='transient-spool-v2')
        self.session.attach_frame_sink(self.sink)

    def open_store(self):
        return RecordingStore(self.root / 'recordings', self.evidence,
            ClockSynchronizer(self.clock), wall_clock_ms=lambda: 5_000_000)

    def tearDown(self):
        self.sink.close()
        self.store.close()
        self.evidence.close()
        self.budget.close()
        self.temp.cleanup()

    def restart(self):
        self.sink.close()
        self.store.close()
        self.store = self.open_store()

    def test_recovery_requires_current_project_registration_and_has_no_input_methods(self):
        with self.assertRaises(RecordingStoreError):
            self.store.recovery_session(self.session.recording_id)
        self.restart()
        with self.assertRaises(RecordingStoreError):
            self.store.recovery_session(self.session.recording_id)
        registration = self.store.register_project(project_document(), collection_policy())
        self.assertEqual(self.store.pending_video_recoveries(registration.project_digest),
                         ('recording_one',))
        recovery = self.store.recovery_session('recording_one')
        for name in ('anchor', 'admit_input', 'record_frame', 'record_observation', 'registration'):
            self.assertFalse(hasattr(recovery, name), name)
        metadata = recovery.video_recovery_metadata()
        self.assertEqual(metadata['limits'], dataclasses.asdict(self.limits))
        self.assertIsNone(metadata['sourceManifestDigest'])
        self.assertFalse(metadata['cleanupComplete'])
        self.assertEqual(recovery.video_sources(), [])
        with self.assertRaises(RecordingStoreError):
            recovery.freeze_recovered()

    def test_historical_limits_are_digest_bound(self):
        self.restart()
        self.store.register_project(project_document(), collection_policy())
        recovery = self.store.recovery_session('recording_one')
        self.store._connection.execute(
            "UPDATE recordings SET source_config_json='{}' WHERE recording_id='recording_one'")
        with self.assertRaises(RecordingStoreError):
            recovery.video_recovery_metadata()

    def test_normal_source_retirement_removes_pending_recovery_and_pin(self):
        self.clock.advance(100_000_000)
        self.session.record_frame(b'frame', 'image/png', 96, 160, 'portrait', acquisition_sequence=1)
        result = self.session.stop()
        self.assertEqual(result['status'], 'frozen-complete')
        row = self.store._recording_row('recording_one')
        self.assertEqual(row['source_cleanup_complete'], 1)
        self.assertEqual(self.store.pending_video_recoveries(self.registration.project_digest), ())
        self.assertEqual(self.evidence.unpin_id('source_outcome_recording_one'), 0)

    def test_native_id_gap_has_a_conservative_interval_and_no_admitted_source_claim(self):
        self.clock.advance(100_000_000)
        first = self.session.record_frame(b'first', 'image/png', 96, 160, 'portrait', acquisition_sequence=1)
        self.clock.advance(200_000_000)
        last = self.session.record_frame(b'last', 'image/png', 96, 160, 'portrait',
            acquisition_sequence=4, native_sequence_gap=(2, 3))
        result = self.session.stop()
        self.assertEqual(result['status'], 'frozen-incomplete')
        manifest = self.sink.manifest()
        self.assertEqual([item['acquisitionSequence'] for item in manifest['sourceFrames']], [1, 4])
        losses = [item for item in manifest['losses'] if item['reason'] == 'native-frame-gap']
        self.assertEqual(len(losses), 1)
        self.assertEqual(losses[0]['nativeSequenceRange'], {'first': 2, 'last': 3})
        self.assertNotIn('recordingFrameRange', losses[0])
        self.assertEqual(losses[0]['recordingInterval'], {
            'startOffsetMs': first.stamp.earliest_offset_ms,
            'endOffsetMs': last.stamp.latest_offset_ms})

    def test_native_id_gap_cannot_cover_an_already_admitted_frame(self):
        self.clock.advance(100_000_000)
        self.session.record_frame(b'first', 'image/png', 96, 160, 'portrait', acquisition_sequence=1)
        self.clock.advance(200_000_000)
        with self.assertRaises(RecordingStoreError):
            self.session.record_frame(b'last', 'image/png', 96, 160, 'portrait',
                acquisition_sequence=4, native_sequence_gap=(1, 3))
        self.assertEqual(len(self.session.video_sources()), 1)

    def test_unreported_native_hole_and_partial_gap_cannot_become_complete(self):
        self.clock.advance(100_000_000)
        self.session.record_frame(b'first', 'image/png', 96, 160, 'portrait', acquisition_sequence=1)
        self.clock.advance(100_000_000)
        for sequence, gap in ((5, None), (6, (3, 5))):
            with self.subTest(gap=gap), self.assertRaises(RecordingStoreError):
                self.session.record_frame(b'last', 'image/png', 96, 160, 'portrait',
                    acquisition_sequence=sequence, native_sequence_gap=gap)
        self.assertEqual(len(self.session.video_sources()), 1)

    def test_cleanup_retry_marker_survives_unpin_failure_and_source_journal_becomes_terminal(self):
        self.clock.advance(100_000_000)
        self.session.record_frame(b'frame', 'image/png', 96, 160, 'portrait', acquisition_sequence=1)
        unpin = self.evidence.unpin_id
        def fail_source_pin(identifier):
            if identifier == 'source_outcome_recording_one':
                from reproof.live.evidence_store import EvidenceStoreError
                raise EvidenceStoreError('injected unpin failure')
            return unpin(identifier)
        with mock.patch.object(self.evidence, 'unpin_id', side_effect=fail_source_pin):
            result = self.session.stop()
        self.assertEqual(result['status'], 'frozen-complete')
        row = self.store._recording_row('recording_one')
        self.assertEqual(row['source_cleanup_complete'], 0)
        reservations = self.budget.reservations_for_owner('recording_one', category='journal')
        source = next(item for item in reservations if item['reservation_id'] == row['source_journal_reservation_id'])
        self.assertEqual(source['state'], 'committed')
        self.session.complete_source_cleanup()
        self.assertEqual(self.store._recording_row('recording_one')['source_cleanup_complete'], 1)

    def test_duration_uses_the_recording_anchor_and_closes_pixels_without_retiming(self):
        self.assertFalse(self.session.duration_reached())
        self.clock.advance(599_998_000_000)
        last = self.session.record_frame(b'last', 'image/png', 96, 160, 'portrait', acquisition_sequence=1)
        self.clock.advance(3_000_000)
        self.assertTrue(self.session.duration_reached())
        self.assertIsNone(self.session.record_frame(b'late', 'image/png', 96, 160, 'portrait', acquisition_sequence=2))
        result = self.session.stop()
        self.assertEqual(result['status'], 'frozen-complete')
        self.assertEqual(self.store._recording_row('recording_one')['barrier_offset_ms'], 600_000)
        frame = self.sink.manifest()['segments'][0]['frames'][0]
        self.assertEqual(frame['latestRecordingOffsetMs'], last.stamp.latest_offset_ms)
        self.assertEqual(len(self.session.video_sources()), 1)

    def test_duration_rejects_inputs_and_regular_observations_but_allows_terminal_logs(self):
        from tests.test_recording_recovery import tap_input
        from reproof.live.recording_session import RecordingDurationError
        from reproof.app_logs import APP_LOG_MIME
        self.clock.advance(600_000_000_000)
        with self.assertRaises(RecordingDurationError):
            self.session.admit_input('late_input', 1, 'provider_one', tap_input())
        self.assertIsNone(self.session.record_observation('accessibility', {'value': 'late'}, acquisition_sequence=1))
        artifact = self.session.record_observation('logs', {'terminal': True}, acquisition_sequence=2,
            mime_type=APP_LOG_MIME, terminal_snapshot=True)
        self.assertIsNotNone(artifact)
        self.assertEqual(self.session.journal_snapshot(), [])


if __name__ == '__main__':
    unittest.main()
