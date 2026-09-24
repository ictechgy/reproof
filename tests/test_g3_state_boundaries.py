"""Parent G3 lifecycle probes using explicit test encoder doubles."""
from __future__ import annotations

import hashlib
import multiprocessing
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from reproof.core import ContractError
from reproof.live.disk_budget import DiskBudget, DiskBudgetError
from reproof.live.evidence_store import EvidenceStore, EvidenceStoreError
from reproof.live.recording_session import FramePublication
from reproof.live.video import EncodedSegment, VideoFrameSink, VideoLimits, VideoProtocolError
from tests.test_recording_recovery import begin_recording, open_store, tap_input


class ParentEncoderDouble:
    def __init__(self, *, block=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()
        if not block:
            self.release.set()

    def encode(self, frames, output_directory, *, timeout_seconds, fault_mode="none"):
        self.entered.set()
        if not self.release.wait(8):
            raise RuntimeError("parent test encoder timed out")
        body = b"parent-test-video:" + b",".join(
            str(x.acquisition_sequence).encode() for x in frames)
        output = Path(output_directory) / "segment.mp4"
        output.write_bytes(body)
        self.completed.set()
        return EncodedSegment(
            path=output,
            accepted_sequences=tuple(x.acquisition_sequence for x in frames),
            bytes=len(body), digest=hashlib.sha256(body).hexdigest(),
            codec="h264", container="mp4")


def crash_after_video_manifest(root):
    import os
    from tests.test_video_state_machine import limits
    _, evidence, recordings, _ = open_store(root)
    _, session = begin_recording(recordings)
    sink = VideoFrameSink(evidence, Path(root) / "video",
                          encoder=ParentEncoderDouble(), limits=limits())
    session.attach_frame_sink(sink)
    session.record_frame(b"sealed-frame", "image/png", 96, 160, "portrait",
                         acquisition_sequence=1, timing_source="host-acquired")
    sink._release_runtime_resources = lambda: os._exit(73)
    session.stop()


class ParentVideoStateBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="g3-parent-state-")
        self.root = Path(self.temp.name)
        self.budget, self.evidence, self.recordings, self.clock = open_store(self.root / "g2")
        self.sink = None
        self.encoder = None

    def tearDown(self):
        if self.encoder is not None:
            self.encoder.release.set()
        if self.sink is not None:
            self.sink.close()
        self.recordings.close()
        self.evidence.close()
        self.budget.close()
        self.temp.cleanup()

    def make(self, *, block=False):
        self.encoder = ParentEncoderDouble(block=block)
        self.limits = VideoLimits(
            segment_duration_ms=1000, max_frame_bytes=1024,
            max_decoded_pixels=1_000_000, max_queue_frames=4,
            max_queue_bytes=4096, max_frames_per_segment=16, max_segments=8,
            max_segment_bytes=1024 * 1024,
            max_total_video_bytes=4 * 1024 * 1024,
            max_helper_output_bytes=16 * 1024,
            max_helper_error_bytes=16 * 1024,
            max_active_finalizers=1, finalization_timeout_seconds=1)
        self.sink = VideoFrameSink(self.evidence, self.root / "video",
                                   encoder=self.encoder, limits=self.limits)
        _, self.session = begin_recording(self.recordings)
        self.session.attach_frame_sink(self.sink)
        return self.sink, self.session

    def frame(self, sequence):
        body = b"parent-frame:" + str(sequence).encode()
        result = self.session.record_frame(
            body, "image/png", 96, 160, "portrait",
            acquisition_sequence=sequence, timing_source="host-acquired")
        self.assertIsNotNone(result)
        return hashlib.sha256(body).hexdigest()

    def until(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("parent test boundary was not reached")

    def test_durable_segment_stays_pinned_while_recording_is_active(self):
        sink, session = self.make()
        self.frame(1)
        self.clock.advance(1_100_000_000)
        self.frame(2)
        self.until(lambda: bool(sink._segments))
        segment_digest = sink._segments[0]["digest"]
        with self.assertRaises(EvidenceStoreError):
            self.evidence.tombstone(segment_digest, reason="retention-expired")
        session.stop()

    def test_timeout_keeps_source_pinned_until_encoder_actually_finishes(self):
        sink, session = self.make(block=True)
        source_digest = self.frame(1)
        started = time.monotonic()
        result = session.stop()
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(result["status"], "frozen-incomplete")
        self.assertTrue(self.encoder.entered.is_set())
        self.assertFalse(self.encoder.completed.is_set())
        with self.assertRaises(EvidenceStoreError):
            self.evidence.tombstone(source_digest, reason="retention-expired")

    def test_failed_work_unlink_keeps_its_disk_reservation(self):
        sink, session = self.make()
        self.frame(1)
        original_unlink = Path.unlink

        def deny_work_unlink(path, *args, **kwargs):
            if path.name == "segment.mp4" and path.parent.parent == sink.work:
                raise PermissionError("parent injected work unlink failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", deny_work_unlink):
            session.stop()
            leftovers = list(sink.work.rglob("*.mp4"))
            self.assertTrue(leftovers, "unlink fault did not reach a work file")
            charged = self.budget._connection.execute(
                "SELECT COALESCE(SUM(charged_bytes),0) FROM reservations "
                "WHERE owner=? AND category='encoding'",
                (session.recording_id,)).fetchone()[0]
            self.assertGreaterEqual(charged, sum(p.stat().st_size for p in leftovers))

    def test_clock_discontinuity_notifications_do_not_make_an_unbounded_queue(self):
        sink, session = self.make(block=True)
        self.frame(1)
        self.clock.advance(1_100_000_000)
        self.frame(2)
        self.assertTrue(self.encoder.entered.wait(2))
        for _ in range(100):
            try:
                sink.invalidate_clock_mapping()
            except VideoProtocolError:
                break
        self.assertLessEqual(len(sink._queue), self.limits.max_queue_frames + 2)

    def test_known_gap_overrides_segment_for_action_mapping(self):
        sink, session = self.make()
        self.frame(1)
        self.clock.advance(200_000_000)
        session.admit_input("operation_one", 1, "provider_one", tap_input())
        session.mark_dispatched("operation_one")
        session.record_receipt("operation_one", "injected")
        self.clock.advance(200_000_000)
        self.frame(2)
        sink.declare_loss("native", reason="native-no-sample",
                          start_offset_ms=100, end_offset_ms=300)
        session.stop()
        mapping = sink.manifest()["eventMappings"]
        self.assertEqual(len(mapping), 1)
        self.assertEqual(mapping[0]["recordingOffsetMs"], 200)
        self.assertEqual(mapping[0]["mapping"]["kind"], "gap")

    def test_video_catalog_metadata_is_charged_before_creating_more_stores(self):
        tiny = self.root / "tiny"
        capacity = 1024 * 1024
        budget = DiskBudget(tiny / "budget", capacity_bytes=capacity,
                            journal_headroom_bytes=64 * 1024,
                            free_bytes=lambda: 1 << 30)
        evidence = EvidenceStore(tiny / "evidence", budget)
        created = 0
        try:
            for index in range(32):
                sink = None
                try:
                    sink = VideoFrameSink(evidence, tiny / ("video_" + str(index)),
                                          encoder=ParentEncoderDouble())
                    created += 1
                except (VideoProtocolError, DiskBudgetError):
                    break
                finally:
                    if sink is not None:
                        sink.close()
            actual = sum(path.stat().st_size for path in tiny.rglob("*") if path.is_file())
            self.assertLessEqual(actual, capacity,
                                 f"{created} unbound video stores exceeded shared capacity")
        finally:
            evidence.close()
            budget.close()

    def test_foreign_published_object_cannot_become_recording_video_input(self):
        sink, session = self.make()
        body = b"foreign-published-frame"
        reference = self.evidence.put_bytes(
            body, owner="foreign-owner", retention_class="original",
            retain_until_ms=int(time.time() * 1000) + 60_000)
        publication = FramePublication(
            digest=reference.digest, bytes=reference.bytes, path=reference.path,
            mime_type="image/png", width=96, height=160, orientation="portrait",
            acquisition_sequence=7, stamp=session.anchor.stamp(),
            timing_source="host-acquired")
        try:
            accepted = sink.accept_frame(publication, body)
        except ContractError:
            accepted = False
        self.assertIs(accepted, False,
                      "The object was never an admitted source sample of this recording")

    def test_admitted_frame_cannot_supply_different_timing_to_the_video(self):
        from dataclasses import replace
        _, session = self.make()
        publication = session.record_frame(
            b"timing-frame", "image/png", 96, 160, "portrait",
            acquisition_sequence=1, timing_source="host-acquired")
        stamp = replace(publication.stamp, offset_ms=100,
                        earliest_offset_ms=100, latest_offset_ms=100)
        with self.assertRaises(ContractError):
            session.video_frame_lineage(replace(publication, stamp=stamp))

    def test_repeated_identical_screen_frames_share_a_consumer_pin(self):
        sink, session = self.make()
        body = b"same-visible-screen"
        for sequence in range(1, 13):
            self.until(lambda: not sink._queue)
            publication = session.record_frame(
                body, "image/png", 96, 160, "portrait",
                acquisition_sequence=sequence, timing_source="host-acquired")
            self.assertIsNotNone(publication)
            self.clock.advance(120_000_000)
        result = session.stop()
        manifest = sink.manifest()
        self.assertEqual(result["status"], "frozen-complete", {
            "videoFailure": manifest["failureReason"],
            "encodedFrames": sum(x["frameCount"] for x in manifest["segments"]),
            "lossReasons": [x["reason"] for x in manifest["losses"]],
        })
        self.assertEqual(sum(x["frameCount"] for x in manifest["segments"]), 12)
        self.assertEqual(manifest["losses"], [])

    def test_publication_followed_by_journal_failure_keeps_object_charges(self):
        sink, session = self.make()
        self.frame(1)
        transaction = sink.journal.transaction
        failed = threading.Event()

        def fail_durable(callback):
            if callback.__name__ == "durable":
                failed.set()
                raise OSError("parent injected segment journal failure")
            return transaction(callback)

        with patch.object(sink.journal, "transaction", fail_durable):
            result = session.stop()
        self.assertTrue(failed.is_set())
        self.assertEqual(result["status"], "frozen-incomplete")
        rows = self.evidence._connection.execute(
            "SELECT reservation_id FROM objects WHERE state='published'").fetchall()
        missing = [row[0] for row in rows if self.budget._connection.execute(
            "SELECT 1 FROM reservations WHERE reservation_id=?", (row[0],)).fetchone() is None]
        self.assertEqual(missing, [])

    def test_encoder_inherits_ownership_lock_after_parent_descriptor_closes(self):
        import fcntl
        import os
        from reproof.live.video import AVFoundationSegmentEncoder, EncoderFrame

        _, session = self.make()
        publication = session.record_frame(
            b"lock-test-frame", "image/png", 96, 160, "portrait",
            acquisition_sequence=1, timing_source="host-acquired")
        frame = EncoderFrame.from_publication(
            publication, provider_incarnation="provider_one", native_incarnation="provider_one")
        ready, release = self.root / "ready", self.root / "release"
        helper = self.root / "owned-helper"
        helper.write_text(f"#!{sys.executable}\n" +
                          "from pathlib import Path\nimport sys,time\n" +
                          f"Path({str(ready)!r}).write_text('ready')\n" +
                          "deadline=time.monotonic()+5\n" +
                          f"while not Path({str(release)!r}).exists() and time.monotonic()<deadline: time.sleep(.005)\n" +
                          "sys.stdin.buffer.read()\nsys.exit(2)\n")
        helper.chmod(0o700)
        encoder = AVFoundationSegmentEncoder(helper, self.evidence, self.limits)
        lock = self.root / "owner.lock"
        owner_fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        errors = []

        def encode():
            try:
                encoder.encode((frame,), self.root / "encoder-work",
                               timeout_seconds=3, ownership_fd=owner_fd)
            except ContractError as error:
                errors.append(type(error).__name__)

        thread = threading.Thread(target=encode)
        thread.start()
        try:
            self.until(ready.exists)
            os.close(owner_fd)
            owner_fd = None
            contender = os.open(lock, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender)
        finally:
            release.write_text("release")
            thread.join(4)
            if owner_fd is not None:
                os.close(owner_fd)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors, "The protocol helper deliberately exits without a video")

    def test_recovery_cleanup_failure_keeps_charge_and_can_be_retried(self):
        from reproof.live.video import VideoCatalog
        from tests.test_video_state_machine import crash_after_one_durable_segment

        root = self.root / "crash"
        process = multiprocessing.get_context("fork").Process(
            target=crash_after_one_durable_segment, args=(root,))
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 71)
        budget, evidence, recordings, _ = open_store(root)
        catalog = VideoCatalog(root / "video", evidence)
        work = root / "video/work" / hashlib.sha256(b"recording_one").hexdigest() / "segment_002"
        work.mkdir(parents=True, exist_ok=True)
        partial = work / "segment.partial.mp4"
        partial.write_bytes(b"parent interrupted partial")
        original_unlink = Path.unlink

        def deny_partial(path, *args, **kwargs):
            if path == partial:
                raise PermissionError("parent injected recovery unlink failure")
            return original_unlink(path, *args, **kwargs)

        try:
            row = catalog.journal.connection.execute(
                "SELECT encoding_reservation_id FROM sessions WHERE recording_id='recording_one'").fetchone()
            reservation_id = row[0]
            with patch.object(Path, "unlink", deny_partial):
                with self.assertRaises((OSError, ContractError)):
                    catalog.recover_interrupted("recording_one")
            self.assertTrue(partial.exists())
            self.assertIsNotNone(budget._connection.execute(
                "SELECT 1 FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone())
            recovered = catalog.recover_interrupted("recording_one")
            self.assertEqual(recovered["status"], "incomplete")
            self.assertFalse(partial.exists())
            self.assertIsNone(budget._connection.execute(
                "SELECT 1 FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone())
        finally:
            catalog.close()
            recordings.close()
            evidence.close()
            budget.close()

    def test_restart_after_manifest_publication_releases_finished_pins(self):
        from reproof.live.video import VideoCatalog
        root = self.root / "sealed-crash"
        process = multiprocessing.get_context("fork").Process(
            target=crash_after_video_manifest, args=(root,))
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 73)
        budget, evidence, recordings, _ = open_store(root)
        catalog = VideoCatalog(root / "video", evidence)
        try:
            result = catalog.recover_interrupted("recording_one")
            self.assertEqual(result["status"], "complete")
            pins = evidence._connection.execute(
                "SELECT COUNT(*) FROM pins WHERE pin_id LIKE 'video_%'").fetchone()[0]
            self.assertEqual(pins, 0)
            self.assertEqual(recordings.load("recording_one")["status"], "frozen-incomplete")
        finally:
            catalog.close()
            recordings.close()
            evidence.close()
            budget.close()


if __name__ == "__main__":
    unittest.main()
