import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from reproloop.live.video import (
    EncodedSegment,
    VideoCatalog,
    VideoEncodingError,
    VideoFrameSink,
    VideoLimits,
)
from reproloop.live.model import Lab
from reproloop.live.clock_sync import ClockSynchronizer
from tests.test_clock_sync import FakeClock
from tests.test_recording_recovery import (
    begin_recording,
    collection_policy,
    open_store,
    preparation,
    project_document,
    tap_input,
)


class FakeEncoder:
    def __init__(self, *, fail=False, accepted=None, block=None, payload_size=None):
        self.fail = fail
        self.accepted = accepted
        self.block = block
        self.payload_size = payload_size
        self.calls = []

    def encode(self, frames, output_directory, *, timeout_seconds, fault_mode="none"):
        self.calls.append(tuple(frames))
        if self.block is not None:
            self.block.wait()
        path = Path(output_directory) / "segment.mp4"
        body = b"fake-h264-mp4:" + b",".join(
            str(frame.acquisition_sequence).encode("ascii") for frame in frames
        )
        if self.payload_size is not None:
            body = b"x" * self.payload_size
        path.write_bytes(body)
        accepted = tuple(frame.acquisition_sequence for frame in frames)
        if self.accepted is not None:
            accepted = tuple(self.accepted)
        if self.fail:
            raise VideoEncodingError(
                "encoder-process-failed", accepted_sequences=accepted
            )
        return EncodedSegment(
            path=path,
            accepted_sequences=accepted,
            bytes=len(body),
            digest=hashlib.sha256(body).hexdigest(),
            codec="h264",
            container="mp4",
        )


def crash_after_one_durable_segment(root):
    _, evidence, recordings, clock = open_store(root)
    _, session = begin_recording(recordings)
    sink = VideoFrameSink(
        evidence, Path(root) / "video", encoder=FakeEncoder(), limits=limits()
    )
    session.attach_frame_sink(sink)
    session.record_frame(
        b"png-frame-1", "image/png", 96, 160, "portrait",
        acquisition_sequence=1,
    )
    clock.advance(1_200_000_000)
    session.record_frame(
        b"png-frame-2", "image/png", 96, 160, "portrait",
        acquisition_sequence=2,
    )
    deadline = time.monotonic() + 5
    while not sink._segments and time.monotonic() < deadline:
        time.sleep(0.01)
    os._exit(71 if sink._segments else 72)


def limits(**changes):
    values = {
        "segment_duration_ms": 1000,
        "max_frame_bytes": 1024,
        "max_decoded_pixels": 1_000_000,
        "max_queue_frames": 4,
        "max_queue_bytes": 4096,
        "max_frames_per_segment": 16,
        "max_segments": 8,
        "max_segment_bytes": 1024 * 1024,
        "max_total_video_bytes": 4 * 1024 * 1024,
        "max_helper_output_bytes": 16 * 1024,
        "max_helper_error_bytes": 16 * 1024,
        "max_active_finalizers": 1,
        "finalization_timeout_seconds": 1,
    }
    values.update(changes)
    return VideoLimits(**values)


class VideoStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sinks = []
        self.budget, self.evidence, self.recordings, self.clock = open_store(
            Path(self.temp.name) / "g2"
        )

    def tearDown(self):
        for sink in self.sinks:
            sink.close()
        self.recordings.close()
        self.evidence.close()
        self.budget.close()
        self.temp.cleanup()

    def make_sink(self, encoder=None, **limit_changes):
        sink = VideoFrameSink(
            self.evidence,
            Path(self.temp.name) / "video",
            encoder=encoder or FakeEncoder(),
            limits=limits(**limit_changes),
        )
        self.sinks.append(sink)
        _, session = begin_recording(self.recordings)
        session.attach_frame_sink(sink)
        return sink, session

    def frame(self, session, sequence, width=96, height=160,
              timing_source="host-acquired"):
        body = b"png-frame-" + str(sequence).encode("ascii")
        result = session.record_frame(
            body, "image/png", width, height,
            "portrait" if height > width else "landscape",
            acquisition_sequence=sequence,
            timing_source=timing_source,
        )
        self.clock.advance(400_000_000)
        return result

    def test_geometry_and_duration_rotation_preserve_irregular_pts_and_lineage(self):
        encoder = FakeEncoder()
        sink, session = self.make_sink(encoder)
        self.frame(session, 1)
        self.clock.advance(700_000_000)
        self.frame(session, 2)
        self.frame(session, 3, 160, 96)
        result = session.stop()
        manifest = sink.manifest()

        self.assertEqual(result["status"], "frozen-complete")
        self.assertEqual([segment["frameCount"] for segment in manifest["segments"]], [1, 1, 1])
        self.assertEqual([segment["rotationReason"] for segment in manifest["segments"]],
                         ["duration-bound", "geometry-change", "stop-barrier"])
        self.assertEqual(manifest["samplingMode"], "irregular-source-capture")
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(len(encoder.calls), 3)
        media_types = {item["mimeType"] for item in result["original"]["media"]}
        self.assertIn("video/mp4", media_types)
        self.assertIn("application/vnd.reproloop.video-manifest+json", media_types)

    def test_native_unmapped_is_unknown_and_not_a_capture_interval(self):
        sink, session = self.make_sink()
        self.frame(session, 1, timing_source="native-unmapped")
        result = session.stop()
        manifest = sink.manifest()
        segment = manifest["segments"][0]
        self.assertEqual(result["status"], "frozen-incomplete")
        self.assertIsNone(segment["captureInterval"])
        self.assertEqual(segment["timingRelation"], "display-publication-order-only")
        self.assertIn("native-acquisition-unknown",
                      {loss["reason"] for loss in manifest["losses"]})

    def test_event_mapping_uses_frozen_barrier_without_changing_receipts(self):
        sink, session = self.make_sink()
        session.admit_input("operation_one", 7, "provider_one", tap_input())
        session.mark_dispatched("operation_one")
        session.record_receipt("operation_one", "injected")
        self.frame(session, 1)
        result = session.stop()
        mapping = sink.manifest()["eventMappings"][0]
        self.assertEqual(mapping["eventId"], "event_1")
        self.assertEqual(mapping["mapping"]["kind"], "segment")
        self.assertEqual(result["original"]["events"][0]["dispatch"], "injected")
        self.assertEqual(result["original"]["endSequence"], 1)

    def test_zero_frames_and_explicit_loss_classes_are_truthful(self):
        sink, session = self.make_sink()
        sink.declare_loss("native", reason="native-no-sample", start_offset_ms=0,
                          end_offset_ms=100)
        sink.declare_loss("transport", reason="transport-drop", start_offset_ms=100,
                          end_offset_ms=120, acquisition_sequence=1)
        sink.declare_loss("queue", reason="queue-drop", start_offset_ms=120,
                          end_offset_ms=130, acquisition_sequence=2)
        result = session.stop()
        manifest = sink.manifest()
        self.assertEqual(result["status"], "frozen-incomplete")
        self.assertEqual(manifest["segments"], [])
        self.assertEqual({item["lossClass"] for item in manifest["losses"]},
                         {"not-acquired", "captured-dropped"})

    def test_encoder_failure_keeps_accepted_not_durable_distinct(self):
        encoder = FakeEncoder(fail=True, accepted=(1,))
        sink, session = self.make_sink(encoder)
        self.frame(session, 1)
        self.frame(session, 2)
        result = session.stop()
        manifest = sink.manifest()
        self.assertEqual(result["status"], "frozen-incomplete")
        classes = {item["lossClass"] for item in manifest["losses"]}
        self.assertIn("encoder-accepted-not-durable", classes)
        self.assertIn("captured-dropped", classes)
        self.assertEqual(manifest["segments"], [])

    def test_queue_backpressure_records_captured_drop(self):
        block = threading.Event()
        self.addCleanup(block.set)
        encoder = FakeEncoder(block=block)
        sink, session = self.make_sink(
            encoder, max_queue_frames=1, segment_duration_ms=100
        )
        self.frame(session, 1)
        self.frame(session, 2)
        deadline = time.monotonic() + 2
        while not encoder.calls and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(encoder.calls)
        for sequence in range(3, 10):
            self.frame(session, sequence)
        deadline = time.monotonic() + 2
        while "queue-over-limit" not in {item["reason"] for item in sink._losses} and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIn(
            "queue-over-limit",
            {item["reason"] for item in sink._losses},
        )
        block.set()
        session.stop()

    def test_segment_and_total_size_bounds_fail_incomplete(self):
        sink, session = self.make_sink(
            FakeEncoder(payload_size=5000),
            max_segment_bytes=4096,
            max_total_video_bytes=4096,
        )
        self.frame(session, 1)
        result = session.stop()
        self.assertEqual(result["status"], "frozen-incomplete")
        self.assertEqual(sink.manifest()["segments"], [])

    def test_process_finalizer_limit_fails_closed(self):
        sink, session = self.make_sink()
        self.frame(session, 1)
        self.assertTrue(VideoFrameSink._claim_process_finalizer())
        self.assertTrue(VideoFrameSink._claim_process_finalizer())
        try:
            result = session.stop()
        finally:
            VideoFrameSink._release_process_finalizer()
            VideoFrameSink._release_process_finalizer()
        self.assertEqual(result["status"], "frozen-incomplete")
        self.assertIn(
            "finalizer-over-limit",
            {item["reason"] for item in sink.manifest()["losses"]},
        )

    def test_injected_disk_full_preserves_incomplete_manifest(self):
        sink, session = self.make_sink()
        self.frame(session, 1)
        fault = {"pending": True}

        def available_storage():
            if fault["pending"]:
                fault["pending"] = False
                return 0
            return 1 << 30

        self.budget._free_bytes = available_storage
        result = session.stop()
        self.assertEqual(result["status"], "frozen-incomplete")
        self.assertEqual(sink.manifest()["segments"], [])
        self.assertIn(
            "video-storage-exhausted",
            {item["reason"] for item in sink.manifest()["losses"]},
        )

    def test_finalization_timeout_is_bounded_and_cleans_partial_files(self):
        block = threading.Event()
        sink, session = self.make_sink(FakeEncoder(block=block),
                                       finalization_timeout_seconds=1)
        self.frame(session, 1)
        started = time.monotonic()
        result = session.stop()
        elapsed = time.monotonic() - started
        block.set()
        sink.close()
        self.assertLess(elapsed, 2.5)
        self.assertEqual(result["status"], "frozen-incomplete")
        self.assertEqual(sink.manifest()["failureReason"], "finalization-timeout")
        self.assertEqual(list((Path(self.temp.name) / "video" / "work").rglob("*.mp4")), [])

    def test_catalog_recovers_published_manifest_and_durable_segments(self):
        sink, session = self.make_sink()
        self.frame(session, 1)
        session.stop()
        expected = sink.manifest()
        sink.close()
        catalog = VideoCatalog(Path(self.temp.name) / "video", self.evidence)
        try:
            recovered = catalog.load("recording_one")
        finally:
            catalog.close()
        self.assertEqual(recovered["manifestDigest"], expected["manifestDigest"])
        self.assertEqual(recovered["segments"][0]["digest"],
                         expected["segments"][0]["digest"])

    def test_process_interruption_preserves_durable_segment_and_losses(self):
        root = Path(self.temp.name) / "crash"
        process = multiprocessing.get_context("fork").Process(
            target=crash_after_one_durable_segment, args=(root,)
        )
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 71)
        budget, evidence, recordings, _ = open_store(root)
        catalog = VideoCatalog(root / "video", evidence)
        try:
            recovered = catalog.recover_interrupted("recording_one")
            self.assertEqual(recovered["status"], "incomplete")
            self.assertEqual(len(recovered["segments"]), 1)
            self.assertEqual(
                evidence.read(recovered["segments"][0]["digest"]),
                b"fake-h264-mp4:1",
            )
            self.assertIn(
                "process-interruption",
                {item["reason"] for item in recovered["losses"]},
            )
        finally:
            catalog.close()
            recordings.close()
            evidence.close()
            budget.close()


class VideoLabIntegrationTests(unittest.TestCase):
    def test_lab_binds_sink_to_actual_recording_before_provider_start(self):
        class EmptyProvider:
            def start(self, session, lab):
                self.started = True
                lab.publish_frame(
                    session["id"], b"not-a-png", "image/png", 96, 160,
                    "portrait", acquisition_sequence=1,
                )

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "owned-helper"
            helper.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
            helper.chmod(0o700)
            clock = FakeClock(1_000_000_000)
            provider = EmptyProvider()
            device = {
                "id": "test",
                "name": "Test",
                "platform": "ios",
                "kind": "demo",
                "factory": lambda: provider,
                "capabilities": {
                    "actions": ["tap"],
                    "inputMode": "gesture-batch",
                    "media": "none",
                    "applicationIdentity": {
                        "bundle": "com.example.app",
                        "artifactDigest": "0" * 64,
                    },
                },
            }
            lab = Lab(
                [device], root / "lab",
                recording_clock_sync=ClockSynchronizer(clock),
                recording_wall_clock_ms=lambda: 5_000_000,
            )
            try:
                registration = lab.register_recording_project(
                    project_document(),
                    collection_policy(),
                    capacity_bytes=512 * 1024 * 1024,
                    journal_headroom_bytes=1024 * 1024,
                )
                sink = lab.create_video_sink(
                    helper=helper,
                    limits=limits(max_segment_bytes=4 * 1024 * 1024,
                                  max_total_video_bytes=4 * 1024 * 1024),
                )
                session = lab.create_release_session(
                    "test", "owner", "browser_one", registration,
                    application_id="ios_app", build_id="original",
                    preparation_receipts=preparation(),
                    frame_sink=sink,
                )
                self.assertTrue(provider.started)
                self.assertEqual(sink.source_mode, 'transient-spool-v2')
                self.assertEqual(sink._recording_id,
                                 session["releaseRecordingId"])
                frozen = lab.stop_release_recording(
                    session["id"], "owner", session["controllerId"],
                    session["epoch"],
                )
                self.assertEqual(frozen["status"], "frozen-incomplete")
                self.assertIn(
                    "application/vnd.reproloop.video-manifest+json",
                    {item["mimeType"] for item in frozen["original"]["media"]},
                )
            finally:
                lab.close_all()


if __name__ == "__main__":
    unittest.main()
