"""G2 admission, immutable freeze, and G3 source retirement in video mode."""
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore
from reproof.live.recording_session import RecordingStore, RecordingStoreError
from reproof.live.video import VideoFrameSink, VideoLimits
from tests.test_clock_sync import FakeClock
from tests.test_recording_recovery import begin_recording
from tests.test_video_state_machine import FakeEncoder, limits


class VideoSpoolRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.budget = DiskBudget(self.root / 'budget', capacity_bytes=512 * 1024 * 1024,
            journal_headroom_bytes=8 * 1024 * 1024, free_bytes=lambda: 1 << 30)
        self.evidence = EvidenceStore(self.root / 'objects', self.budget)
        self.clock = FakeClock(1_000_000_000)
        self.recordings = RecordingStore(self.root / 'recordings', self.evidence,
            ClockSynchronizer(self.clock), wall_clock_ms=lambda: 5_000_000)
        self.sink = None

    def tearDown(self):
        if self.sink is not None:
            self.sink.close()
        self.recordings.close()
        self.evidence.close()
        self.budget.close()
        self.temp.cleanup()

    def start(self, mode='test-data', video_limits=None):
        _, session = begin_recording(self.recordings, mode=mode)
        self.sink = VideoFrameSink(self.evidence, self.root / 'video',
            encoder=FakeEncoder(), limits=video_limits or limits(segment_duration_ms=60_000, max_segments=32),
            source_mode='transient-spool-v2')
        session.attach_frame_sink(self.sink)
        return session

    def frame(self, session, sequence):
        publication = session.record_frame(b'one-repeated-source-frame', 'image/png', 96, 160,
            'portrait', acquisition_sequence=sequence)
        self.clock.advance(100_000_000)
        with self.sink._condition:
            self.assertTrue(self.sink._condition.wait_for(
                lambda: not self.sink._queue and not self.sink._finalizer_queue, timeout=2))
        return publication

    def test_sources_are_spooled_and_only_video_artifacts_enter_the_original(self):
        session = self.start()
        publication = self.frame(session, 1)
        self.assertEqual(publication.recording_frame_sequence, 1)
        self.assertIsNone(self.evidence.lookup(publication.digest))
        self.assertEqual(self.sink.source_spool.read(publication.source_token), b'one-repeated-source-frame')
        result = session.stop()
        manifest = self.sink.manifest()
        self.assertEqual(result['status'], 'frozen-complete')
        self.assertEqual(manifest['schemaVersion'], 2)
        self.assertEqual(manifest['sourceDisposition'], 'transient-after-durable-outcome')
        self.assertEqual(manifest['sourceFrames'], [
            {'sequence': 1, 'acquisitionSequence': 1, 'digest': publication.digest}])
        self.assertEqual({item['mimeType'] for item in result['original']['media']},
                         {'video/mp4', 'application/vnd.reproof.video-manifest+json'})
        self.assertIsNone(self.evidence.lookup(publication.digest))
        self.assertEqual(list((self.sink.source_spool.root / 'frames').iterdir()), [])

    def test_repeated_sources_survive_more_than_the_legacy_256_frame_limit(self):
        session = self.start()
        for sequence in range(1, 301):
            self.frame(session, sequence)
        result = session.stop()
        self.assertEqual(result['status'], 'frozen-complete')
        manifest = self.sink.manifest()
        self.assertEqual(len(manifest['sourceFrames']), 300)
        self.assertEqual(sum(segment['frameCount'] for segment in manifest['segments']), 300)
        self.assertEqual(manifest['losses'], [])
        source_digest = hashlib.sha256(b'one-repeated-source-frame').hexdigest()
        self.assertIsNone(self.evidence.lookup(source_digest))
        self.assertLess(len(result['original']['media']), 33)

    def test_suppressed_pixels_never_enter_the_spool_or_source_ledger(self):
        session = self.start(mode='suppressed')
        self.assertIsNone(self.frame(session, 1))
        self.assertEqual(self.sink.source_spool.snapshot()['acceptedCount'], 0)
        result = session.stop()
        self.assertEqual(result['status'], 'frozen-incomplete')
        self.assertEqual(self.sink.manifest()['sourceFrames'], [])
        self.assertEqual(list((self.sink.source_spool.root / 'frames').iterdir()), [])

    def test_v2_cannot_freeze_an_empty_original_without_its_source_outcome_manifest(self):
        session = self.start()
        self.frame(session, 1)
        session.stop_barrier()
        with self.assertRaises(RecordingStoreError):
            self.recordings._freeze_id(session.recording_id)
        self.assertIsNone(self.recordings.load(session.recording_id)['original'])
        self.assertEqual(session.freeze()['status'], 'frozen-complete')

    def test_package_graph_round_trip_uses_video_and_source_commitments(self):
        from reproof.issue_package import build_archive, inspect_archive
        from tests.test_issue_package import example
        session = self.start()
        publication = self.frame(session, 1)
        recording = session.stop()
        _, spec = example()
        spec['originalRecordingDigest'] = recording['recordingDigest']
        segments = {segment['digest']: segment for segment in self.sink.manifest()['segments']}
        # Explicit codec double: this test exercises the real closed-graph
        # package reader; actual AVFoundation decoding has separate native QA.
        def media_report(body, mime):
            self.assertEqual(mime, 'video/mp4')
            segment = segments[hashlib.sha256(body).hexdigest()]
            return {'mimeType': mime, 'codec': 'h264', 'width': segment['width'],
                'height': segment['height'], 'frameCount': segment['frameCount'],
                'presentationTimesMs': [item['presentationTimeMs'] for item in segment['frames']]}
        archive = build_archive(recording, spec, self.evidence.read, media_validator=media_report)
        received = inspect_archive(io.BytesIO(archive), media_validator=media_report)
        self.assertEqual(received.recording['original'], recording['original'])
        self.assertEqual(received.video['sourceFrames'][0]['digest'], publication.digest)
        self.assertNotIn(publication.digest, received.index['manifest']['objects'])

    def test_ten_minute_clock_and_9001_sources_freeze_with_the_standard_profile(self):
        session = self.start(video_limits=VideoLimits.recording_profile())
        anchor = self.clock.nanoseconds
        for sequence in range(1, 9002):
            self.clock.nanoseconds = anchor + (sequence - 1) * 599_999_000_000 // 9000
            session.record_frame(b'one-repeated-source-frame', 'image/png', 96, 160,
                'portrait', acquisition_sequence=sequence)
            with self.sink._condition:
                self.assertTrue(self.sink._condition.wait_for(
                    lambda: not self.sink._queue and not self.sink._finalizer_queue, timeout=2))
        self.clock.nanoseconds = anchor + 600_000_000_000
        result = session.stop()
        self.assertEqual(result['status'], 'frozen-complete')
        manifest = self.sink.manifest()
        self.assertEqual(len(manifest['sourceFrames']), 9001)
        self.assertEqual(sum(segment['frameCount'] for segment in manifest['segments']), 9001)
        self.assertLessEqual(len(manifest['segments']), 64)
        self.assertEqual(manifest['losses'], [])
        self.assertEqual(self.recordings._recording_row(session.recording_id)['barrier_offset_ms'], 600_000)


if __name__ == '__main__':
    unittest.main()
