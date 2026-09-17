"""Crash-boundary tests for transient video source recovery."""
from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest

from reproloop.live.clock_sync import ClockSynchronizer
from reproloop.live.disk_budget import DiskBudget
from reproloop.live.evidence_store import EvidenceStore
from reproloop.live.frame_spool import FrameSpool
from reproloop.live.recording_session import (
    RecordingRecoverySession,
    RecordingSession,
    RecordingStore,
    RecordingStoreError,
)
from reproloop.live.video import (
    VideoCatalog,
    VideoFrameSink,
    VideoLimits,
    VideoProtocolError,
    validate_video_manifest,
)
from tests.test_clock_sync import FakeClock
from tests.test_recording_recovery import collection_policy, preparation, project_document
from tests.test_video_state_machine import FakeEncoder


ROOT = Path(__file__).resolve().parents[1]
VIDEO_MODE = "transient-spool-v2"


def _limits():
    return VideoLimits(
        segment_duration_ms=1000,
        max_frame_bytes=1024,
        max_decoded_pixels=1_000_000,
        max_queue_frames=4,
        max_queue_bytes=4096,
        max_frames_per_segment=16,
        max_segments=8,
        max_segment_bytes=1024 * 1024,
        max_total_video_bytes=4 * 1024 * 1024,
        max_helper_output_bytes=16 * 1024,
        max_helper_error_bytes=16 * 1024,
        max_active_finalizers=1,
        finalization_timeout_seconds=1,
    )


def _open(root):
    root = Path(root)
    budget = DiskBudget(
        root / "budget", capacity_bytes=512 * 1024 * 1024,
        journal_headroom_bytes=8 * 1024 * 1024,
        free_bytes=lambda: 1 << 40,
    )
    evidence = EvidenceStore(root / "evidence", budget)
    clock = FakeClock(1_000_000_000)
    recordings = RecordingStore(
        root / "recordings", evidence, ClockSynchronizer(clock),
        wall_clock_ms=lambda: 5_000_000,
    )
    return budget, evidence, recordings, clock


def _begin(recordings):
    registration = recordings.register_project(project_document(), collection_policy())
    return recordings.begin_recording(
        registration,
        recording_id="recording_one",
        session_id="session_one",
        application_id="ios_app",
        build_id="original",
        device_identity={"bundle": "com.example.app", "artifactDigest": "0" * 64},
        provider_incarnation="provider_one",
        preparation_receipts=preparation(),
    )


def _crash_boundary(root, boundary):
    budget, evidence, recordings, clock = _open(root)
    session = _begin(recordings)
    limits = _limits()
    if boundary in {"before_accept", "native_gap_before_accept"}:
        def crash_accept(*_args, **_kwargs):
            os._exit(41)
        if boundary == "before_accept":
            VideoFrameSink.accept_frame = crash_accept
    elif boundary == "spool_before_g2":
        original = FrameSpool.stage
        def stage_then_crash(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            os._exit(45)
        FrameSpool.stage = stage_then_crash
    elif boundary == "publish_before_journal":
        original = VideoFrameSink._publish_file
        def publish_then_crash(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            os._exit(46)
        VideoFrameSink._publish_file = publish_then_crash
    elif boundary == "before_source_release":
        def crash_release(*_args, **_kwargs):
            os._exit(42)
        FrameSpool.release = crash_release
    elif boundary == "after_g2_attach":
        original = RecordingSession.attach_finalized_video
        def attach_then_crash(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            os._exit(43)
        RecordingSession.attach_finalized_video = attach_then_crash
    elif boundary == "before_cleanup_marker":
        def cleanup_then_crash(*_args, **_kwargs):
            os._exit(44)
        RecordingSession.complete_source_cleanup = cleanup_then_crash
    else:
        raise AssertionError(boundary)
    sink = VideoFrameSink(
        evidence, Path(root) / "video", encoder=FakeEncoder(), limits=limits,
        source_mode=VIDEO_MODE,
    )
    session.attach_frame_sink(sink)
    session.record_frame(
        b"source-frame-1", "image/png", 96, 160, "portrait",
        acquisition_sequence=1,
    )
    if boundary == "native_gap_before_accept":
        with sink._condition:
            if not sink._condition.wait_for(
                    lambda: not sink._queue and not sink._finalizer_queue,
                    timeout=5):
                raise AssertionError("first source did not reach its boundary")
        clock.advance(1_200_000_000)
        VideoFrameSink.accept_frame = crash_accept
        session.record_frame(
            b"source-frame-3", "image/png", 96, 160, "portrait",
            acquisition_sequence=3, native_sequence_gap=(2, 2),
        )
    if boundary == "before_accept":
        raise AssertionError("the patched accept path did not exit")
    if boundary == "before_source_release":
        session.stop()
    elif boundary in {"after_g2_attach", "before_cleanup_marker", "publish_before_journal"}:
        session.stop()
    else:
        raise AssertionError(boundary)
    raise AssertionError("the crash boundary did not exit")


def _crash_process(root, boundary):
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_boundary, args=(str(root), boundary),
    )
    process.start()
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
    if process.is_alive():
        raise AssertionError(f"crash boundary process did not terminate: {boundary}")
    return process.exitcode


class VideoSourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reproloop-video-source-recovery-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _recover(self, *, freeze_before=False, delete_video_session=False):
        budget, evidence, recordings, _clock = _open(self.root)
        recordings.register_project(project_document(), collection_policy())
        recovery = recordings.recovery_session("recording_one")
        catalog = VideoCatalog(self.root / "video", evidence)
        try:
            if freeze_before:
                recovery.freeze_recovered()
            if delete_video_session:
                catalog.journal.transaction(lambda connection: connection.execute(
                    "DELETE FROM sessions WHERE recording_id=?", (recovery.recording_id,)
                ))
            result = catalog.recover_transient(recovery)
            return result
        finally:
            catalog.close()
            recordings.close()
            evidence.close()
            budget.close()

    def test_g2_commit_before_video_accept_becomes_explicit_source_loss(self):
        self.assertEqual(_crash_process(self.root, "before_accept"), 41)
        result = self._recover()
        self.assertEqual(result["status"], "incomplete")
        manifest = result
        self.assertEqual(manifest["sourceFrames"][0]["sequence"], 1)
        self.assertEqual(manifest["segments"], [])
        self.assertEqual(len(manifest["losses"]), 1)
        self.assertEqual(manifest["losses"][0]["lossClass"], "captured-dropped")
        self.assertEqual(manifest["losses"][0]["reason"], "process-interruption")
        self.assertEqual(manifest["losses"][0]["recordingFrameRange"],
                         {"first": 1, "last": 1})

    def test_durable_segment_and_source_spool_survive_crash_before_unlink(self):
        self.assertEqual(_crash_process(self.root, "before_source_release"), 42)
        result = self._recover()
        manifest = result
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(manifest["segments"]), 1)
        self.assertEqual(manifest["losses"], [])
        self.assertFalse(self._source_files())

    def test_explicit_native_gap_is_preserved_without_inferring_other_gaps(self):
        self.assertEqual(_crash_process(self.root, "native_gap_before_accept"), 41)
        result = self._recover()
        native = [item for item in result["losses"]
                  if item["reason"] == "native-frame-gap"]
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0]["nativeSequenceRange"], {"first": 2, "last": 2})
        self.assertEqual(native[0]["recordingInterval"],
                         {"startOffsetMs": 0, "endOffsetMs": 1201})
        self.assertEqual([item["acquisitionSequence"] for item in result["losses"]
                          if "acquisitionSequence" in item], [1])

    def test_existing_g2_source_manifest_is_reused_after_attach_boundary(self):
        self.assertEqual(_crash_process(self.root, "after_g2_attach"), 43)
        budget, evidence, recordings, _clock = _open(self.root)
        try:
            recordings.register_project(project_document(), collection_policy())
            recovery = recordings.recovery_session("recording_one")
            digest = recordings._recording_row("recording_one")["source_manifest_digest"]
            self.assertIsNotNone(digest)
            original_before = recordings._recording_row("recording_one")["original_json"]
            catalog = VideoCatalog(self.root / "video", evidence)
            try:
                result = catalog.recover_transient(recovery)
            finally:
                catalog.close()
            self.assertEqual(result["manifestDigest"], digest)
            self.assertEqual(recordings._recording_row("recording_one")["original_json"],
                             original_before)
        finally:
            recordings.close()
            evidence.close()
            budget.close()

    def test_frozen_before_source_cleanup_reuses_exact_manifest(self):
        self.assertEqual(_crash_process(self.root, "after_g2_attach"), 43)
        result = self._recover(freeze_before=True)
        self.assertEqual(result["status"], "complete")
        self.assertFalse(self._source_files())

    def test_retired_spool_before_cleanup_marker_can_finish_cleanup(self):
        self.assertEqual(_crash_process(self.root, "before_cleanup_marker"), 44)
        result = self._recover()
        self.assertEqual(result["status"], "complete")
        self.assertFalse(self._source_files())

    def test_expired_attached_manifest_only_finishes_retired_cleanup_marker(self):
        self.assertEqual(_crash_process(self.root, "before_cleanup_marker"), 44)
        budget, evidence, recordings, _clock = _open(self.root)
        try:
            recordings.register_project(project_document(), collection_policy())
            recovery = recordings.recovery_session("recording_one")
            recovery.freeze_recovered()
            row = recordings._recording_row("recording_one")
            original_before = row["original_json"]
            digest = row["source_manifest_digest"]
            evidence.unpin_id("source_outcome_recording_one")
            evidence.tombstone(digest, reason="retention_expired")
            catalog = VideoCatalog(self.root / "video", evidence)
            try:
                result = catalog.recover_transient(recovery)
            finally:
                catalog.close()
            self.assertEqual(result, {
                "recordingId": "recording_one",
                "status": "unavailable",
                "reason": "source-manifest-unavailable",
                "cleanupComplete": True,
            })
            self.assertEqual(recordings._recording_row("recording_one")["original_json"],
                             original_before)
            self.assertEqual(recordings._recording_row("recording_one")["source_cleanup_complete"], 1)
        finally:
            recordings.close()
            evidence.close()
            budget.close()

    def test_fake_recovery_recording_id_is_not_read_before_capability_check(self):
        budget, evidence, recordings, _clock = _open(self.root)
        catalog = VideoCatalog(self.root / "video", evidence)
        try:
            class FakeRecovery:
                @property
                def recording_id(self):
                    raise AssertionError("untrusted recovery property was accessed")

            with self.assertRaises(VideoProtocolError):
                catalog.recover_transient(FakeRecovery())
        finally:
            catalog.close()
            recordings.close()
            evidence.close()
            budget.close()

    def test_recovery_capability_rejects_direct_construction_and_expiry(self):
        self.assertEqual(_crash_process(self.root, "before_accept"), 41)
        budget, evidence, recordings, _clock = _open(self.root)
        try:
            with self.assertRaises(RecordingStoreError):
                RecordingRecoverySession(recordings, "recording_one")
            recordings.register_project(project_document(), collection_policy())
            recovery = recordings.recovery_session("recording_one")
            recordings._registered_digests.clear()
            for operation in (
                    recovery.video_sources,
                    lambda: recovery.video_source(1),
                    recovery.freeze_recovered):
                with self.subTest(operation=operation), self.assertRaises(RecordingStoreError):
                    operation()
        finally:
            recordings.close()
            evidence.close()
            budget.close()

    def test_cleanup_complete_rejects_video_journal_manifest_identity_mismatch(self):
        self.assertEqual(_crash_process(self.root, "after_g2_attach"), 43)
        self._recover()
        budget, evidence, recordings, _clock = _open(self.root)
        try:
            recordings.register_project(project_document(), collection_policy())
            recovery = recordings.recovery_session("recording_one")
            row = recordings._recording_row("recording_one")
            source_digest = row["source_manifest_digest"]
            self.assertEqual(row["source_cleanup_complete"], 1)
            catalog = VideoCatalog(self.root / "video", evidence)
            try:
                current = catalog.load("recording_one")
                alternate = copy.deepcopy(current)
                alternate.pop("manifestDigest", None)
                alternate.pop("manifestPath", None)
                alternate["status"] = "incomplete"
                alternate["failureReason"] = "process-interruption"
                alternate["segments"] = []
                source = alternate["sourceFrames"][0]
                alternate["losses"] = [{
                    "lossClass": "captured-dropped",
                    "stage": "encoder",
                    "reason": "process-interruption",
                    "recordingInterval": {"startOffsetMs": 0, "endOffsetMs": 0},
                    "recordingFrameRange": {"first": 1, "last": 1},
                    "acquisitionSequence": source["acquisitionSequence"],
                }]
                alternate["eventMappings"] = []
                alternate = validate_video_manifest(alternate)
                body = json.dumps(
                    alternate, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False,
                ).encode("utf-8")
                reference = evidence.put_bytes(
                    body, owner="alternate_manifest", retention_class="export",
                    retain_until_ms=5_000_000,
                )
                catalog.journal.transaction(lambda connection: connection.execute(
                    """UPDATE sessions SET state='frozen-incomplete', manifest_json=?,
                              manifest_digest=?, manifest_reference_json=?,
                              failure_reason='process-interruption'
                         WHERE recording_id=?""",
                    (body.decode("utf-8"), reference.digest, json.dumps({
                        "digest": reference.digest, "bytes": reference.bytes,
                        "path": reference.path,
                    }, sort_keys=True, separators=(",", ":")), "recording_one"),
                ))
                with self.assertRaises(VideoProtocolError):
                    catalog.recover_transient(recovery)
                self.assertEqual(
                    recordings._recording_row("recording_one")["source_manifest_digest"],
                    source_digest,
                )
            finally:
                catalog.close()
        finally:
            recordings.close()
            evidence.close()
            budget.close()

    def test_impossible_video_journal_states_are_rejected(self):
        mutations = {
            "durable_frame_missing_segment": lambda catalog: catalog.journal.transaction(
                lambda connection: connection.execute(
                    "DELETE FROM segments WHERE recording_id=?", ("recording_one",))),
            "durable_frame_failed_segment": lambda catalog: catalog.journal.transaction(
                lambda connection: (
                    connection.execute(
                        "UPDATE segments SET state='failed' WHERE recording_id=?",
                        ("recording_one",)),
                    connection.execute(
                        "UPDATE frames SET state='durable' WHERE recording_id=?",
                        ("recording_one",)),
                )),
            "queued_frame_with_segment": lambda catalog: catalog.journal.transaction(
                lambda connection: connection.execute(
                    "UPDATE frames SET state='queued' WHERE recording_id=?",
                    ("recording_one",))),
            "segment_index_not_found": lambda catalog: catalog.journal.transaction(
                lambda connection: connection.execute(
                    "UPDATE segments SET segment_index=99 WHERE recording_id=?",
                    ("recording_one",))),
        }
        for name, mutate in mutations.items():
            with self.subTest(state=name), tempfile.TemporaryDirectory(
                    prefix=f"reproloop-video-impossible-{name}-") as directory:
                root = Path(directory)
                self.assertEqual(_crash_process(root, "before_source_release"), 42)
                budget, evidence, recordings, _clock = _open(root)
                try:
                    recordings.register_project(project_document(), collection_policy())
                    recovery = recordings.recovery_session("recording_one")
                    catalog = VideoCatalog(root / "video", evidence)
                    try:
                        mutate(catalog)
                        with self.assertRaises(VideoProtocolError):
                            catalog.recover_transient(recovery)
                    finally:
                        catalog.close()
                finally:
                    recordings.close()
                    evidence.close()
                    budget.close()

    def test_stage_before_g2_commit_releases_only_orphaned_spool_bytes(self):
        self.assertEqual(_crash_process(self.root, "spool_before_g2"), 45)
        result = self._recover()
        self.assertEqual(result["sourceFrames"], [])
        self.assertEqual(result["segments"], [])
        self.assertEqual(result["losses"], [{
            "lossClass": "not-acquired", "stage": "native",
            "reason": "process-interruption",
            "recordingInterval": {"startOffsetMs": 0, "endOffsetMs": 0},
        }])
        self.assertFalse(self._source_files())

    def test_segment_cas_before_video_journal_durable_is_promoted(self):
        self.assertEqual(_crash_process(self.root, "publish_before_journal"), 46)
        result = self._recover()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["losses"], [])
        self.assertFalse(self._source_files())

    def test_cleanup_complete_missing_video_session_does_not_recreate_active_charges(self):
        self.assertEqual(_crash_process(self.root, "after_g2_attach"), 43)
        self._recover()
        budget, evidence, recordings, _clock = _open(self.root)
        try:
            recordings.register_project(project_document(), collection_policy())
            recovery = recordings.recovery_session("recording_one")
            catalog = VideoCatalog(self.root / "video", evidence)
            try:
                catalog.journal.transaction(lambda connection: connection.execute(
                    "DELETE FROM sessions WHERE recording_id=?", ("recording_one",)
                ))
                result = catalog.recover_transient(recovery)
            finally:
                catalog.close()
            self.assertEqual(result["status"], "complete")
            self.assertFalse(any(
                row["state"] == "active"
                for category in ("spool", "encoding")
                for row in budget.reservations_for_owner("recording_one", category=category)
            ))
        finally:
            recordings.close()
            evidence.close()
            budget.close()

    def test_missing_video_session_reconstructs_bounded_bookkeeping(self):
        self.assertEqual(_crash_process(self.root, "before_source_release"), 42)
        result = self._recover(delete_video_session=True)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(self._source_files())

    def test_recovery_authority_is_exact_and_has_no_input_or_clock_surface(self):
        budget, evidence, recordings, _clock = _open(self.root)
        try:
            session = _begin(recordings)
            with self.assertRaises(Exception):
                recordings.recovery_session(session.recording_id)
            catalog = VideoCatalog(self.root / "video", evidence)
            try:
                class FakeRecovery:
                    recording_id = "recording_one"
                with self.assertRaises(VideoProtocolError):
                    catalog.recover_transient(FakeRecovery())
            finally:
                catalog.close()
        finally:
            recordings.close()
            evidence.close()
            budget.close()

    def _source_files(self):
        root = self.root / "video" / "sources" / hashlib.sha256(
            b"recording_one"
        ).hexdigest() / "frames"
        return tuple(root.iterdir()) if root.is_dir() else ()


if __name__ == "__main__":
    unittest.main()
