from pathlib import Path
import tempfile
import threading
import time
import unittest

from reproof.live.video import VideoFrameSink
from tests.test_video_state_machine import FakeEncoder, begin_recording, limits, open_store


class AsyncVideoFinalizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / "g2"
        self.budget, self.evidence, self.recordings, self.clock = open_store(root)
        self.sinks = []

    def tearDown(self):
        for sink in self.sinks:
            sink.close()
        self.recordings.close()
        self.evidence.close()
        self.budget.close()
        self.temp.cleanup()

    def make_sink(self, encoder, **changes):
        sink = VideoFrameSink(
            self.evidence,
            Path(self.temp.name) / "video",
            encoder=encoder,
            limits=limits(**changes),
        )
        self.sinks.append(sink)
        _, session = begin_recording(self.recordings)
        session.attach_frame_sink(sink)
        return sink, session

    def add_frame(self, session, sequence):
        body = b"png-frame-" + str(sequence).encode("ascii")
        accepted = session.record_frame(
            body,
            "image/png",
            96,
            160,
            "portrait",
            acquisition_sequence=sequence,
        )
        self.clock.advance(400_000_000)
        return accepted

    def wait_for(self, predicate, sink, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            with sink._condition:
                sink._condition.wait(min(0.05, remaining))
        self.assertTrue(predicate())

    def test_capture_assembles_one_pending_segment_while_encoder_is_blocked(self):
        release = threading.Event()
        self.addCleanup(release.set)
        encoder = FakeEncoder(block=release)
        sink, session = self.make_sink(
            encoder,
            segment_duration_ms=60_000,
            max_queue_frames=4,
        )
        try:
            for sequence in range(1, 18):
                self.assertTrue(self.add_frame(session, sequence))
                self.wait_for(lambda: not sink._queue, sink)
            self.wait_for(lambda: len(encoder.calls) == 1, sink)
            self.wait_for(
                lambda: [frame.acquisition_sequence for frame in sink._current] == [17],
                sink,
            )
            for sequence in range(18, 33):
                self.assertTrue(self.add_frame(session, sequence))
                self.wait_for(lambda: not sink._queue, sink)
            self.wait_for(
                lambda: [frame.acquisition_sequence for frame in sink._current] == list(range(17, 33)),
                sink,
            )
            self.assertTrue(self.add_frame(session, 33))

            self.wait_for(
                lambda: len(encoder.calls) == 1 and len(sink._finalizer_queue) == 1,
                sink,
            )
            with sink._condition:
                self.assertTrue(sink._finalizer_active)
                self.assertLessEqual(len(sink._finalizer_queue), 1)

            release.set()
            result = session.stop()
            self.assertEqual(result["status"], "frozen-complete")
            self.assertEqual(
                [[frame.acquisition_sequence for frame in frames] for frames in encoder.calls],
                [list(range(1, 17)), list(range(17, 33)), [33]],
            )
            self.assertEqual(
                [segment["segmentIndex"] for segment in sink.manifest()["segments"]],
                [1, 2, 3],
            )
        finally:
            release.set()

    def test_timeout_does_not_release_resources_until_finalizer_joins(self):
        release = threading.Event()
        self.addCleanup(release.set)
        encoder = FakeEncoder(block=release)
        sink, session = self.make_sink(encoder, finalization_timeout_seconds=1)
        try:
            for sequence in range(1, 18):
                self.assertTrue(self.add_frame(session, sequence))
                self.wait_for(lambda: not sink._queue, sink)
            self.wait_for(lambda: len(encoder.calls) == 1, sink)

            result = session.stop()
            self.assertEqual(result["status"], "frozen-incomplete")
            self.assertFalse(sink._finalizer_done.is_set())
            self.assertIsNotNone(sink.journal.connection)
            self.assertTrue(sink._pins)

            sink.close()
            self.assertFalse(sink._finalizer_done.is_set())
            self.assertIsNotNone(sink.journal.connection)

            release.set()
            sink.close()
            self.assertTrue(sink._finalizer_done.is_set())
            self.assertIsNone(sink.journal.connection)
        finally:
            release.set()

    def test_minimum_capture_queue_continues_then_records_excess_pending_work(self):
        release = threading.Event()
        self.addCleanup(release.set)
        encoder = FakeEncoder(block=release)
        sink, session = self.make_sink(
            encoder,
            segment_duration_ms=60_000,
            max_queue_frames=1,
        )
        try:
            for sequence in range(1, 18):
                self.assertTrue(self.add_frame(session, sequence))
                self.wait_for(lambda: not sink._queue, sink)
            self.wait_for(lambda: len(encoder.calls) == 1, sink)
            self.wait_for(
                lambda: [frame.acquisition_sequence for frame in sink._current] == [17],
                sink,
            )

            for sequence in range(18, 49):
                self.assertTrue(self.add_frame(session, sequence))
                self.wait_for(lambda: not sink._queue, sink)
            self.wait_for(lambda: len(sink._current) == 16, sink)
            self.assertEqual(sink._losses, [])
            self.assertTrue(self.add_frame(session, 49))
            self.wait_for(
                lambda: "queue-over-limit" in {item["reason"] for item in sink._losses},
                sink,
            )
            self.assertLessEqual(len(sink._finalizer_queue), 1)
        finally:
            release.set()
            session.stop()

    def test_escaping_finalizer_error_discards_active_task_and_ownership(self):
        encoder = FakeEncoder()
        sink, session = self.make_sink(encoder, segment_duration_ms=60_000)

        def escaped(_task):
            raise RuntimeError("test finalizer failure")

        sink._finalize_segment = escaped
        for sequence in range(1, 18):
            self.assertTrue(self.add_frame(session, sequence))
            self.wait_for(lambda: not sink._queue, sink)
        self.wait_for(lambda: sink._finalizer_done.is_set(), sink)
        self.assertIn(
            "video-finalizer-failed",
            {item["reason"] for item in sink._losses},
        )
        self.assertFalse(sink._segments)
        self.assertFalse(list(sink.work.iterdir()))
        result = session.stop()
        self.assertEqual(result["status"], "frozen-incomplete")


if __name__ == "__main__":
    unittest.main()
