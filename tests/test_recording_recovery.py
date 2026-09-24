import copy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import unittest

from reproof import contracts
from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore
from reproof.live.recording_session import RecordingStore, RecordingStoreError
from tests.test_clock_sync import FakeClock


ROOT = Path(__file__).parents[1]


def project_document():
    return json.loads((ROOT / "tests/fixtures/release/project.json").read_text())


def collection_policy(mode="test-data"):
    return {
        "schemaVersion": 1,
        "captureMode": mode,
        "retentionSeconds": {
            "original": 3600,
            "intermediate": 60,
            "derivative": 60,
            "export": 300,
        },
    }


def preparation():
    return [{
        "receiptId": "prepare_one",
        "recipeId": "seed_account",
        "operation": "prepare",
        "status": "complete",
        "projectId": "checkout",
        "applicationId": "ios_app",
        "startedAtMs": 4_998_000,
        "completedAtMs": 4_999_000,
        "payloadDigest": "2" * 64,
    }]


def tap_input():
    return {
        "action": "tap",
        "target": {"kind": "accessibility-id", "value": "checkout"},
        "parameters": {},
    }


class FaultBoundaryRecordingStore(RecordingStore):
    def __init__(self, *args, crash_boundary=None, **kwargs):
        self.crash_boundary = crash_boundary
        super().__init__(*args, **kwargs)

    def _after_durable_boundary(self, boundary):
        if boundary == self.crash_boundary:
            os._exit(23)


def open_store(root, *, crash_boundary=None):
    root = Path(root)
    budget = DiskBudget(root / "budget", capacity_bytes=32 * 1024 * 1024,
                        journal_headroom_bytes=256 * 1024,
                        free_bytes=lambda: 1 << 30)
    evidence = EvidenceStore(root / "evidence", budget)
    clock = FakeClock(1_000_000_000)
    store_class = FaultBoundaryRecordingStore if crash_boundary is not None else RecordingStore
    recordings = store_class(
        root / "recordings", evidence, ClockSynchronizer(clock),
        wall_clock_ms=lambda: 5_000_000, crash_boundary=crash_boundary,
    ) if crash_boundary is not None else store_class(
        root / "recordings", evidence, ClockSynchronizer(clock),
        wall_clock_ms=lambda: 5_000_000,
    )
    return budget, evidence, recordings, clock


def begin_recording(recordings, mode="test-data"):
    registration = recordings.register_project(project_document(), collection_policy(mode))
    session = recordings.begin_recording(
        registration,
        recording_id="recording_one",
        session_id="session_one",
        application_id="ios_app",
        build_id="original",
        device_identity={"bundle": "com.example.app", "artifactDigest": "0" * 64},
        provider_incarnation="provider_one",
        preparation_receipts=preparation(),
    )
    return registration, session


def _crash_at_boundary(root, boundary):
    crash_during_begin = boundary if boundary in {"identity", "preparation"} else None
    budget, evidence, recordings, _ = open_store(
        root, crash_boundary=crash_during_begin)
    _, session = begin_recording(recordings)
    if boundary in {"admission", "dispatch", "receipt", "barrier", "freeze"}:
        session.admit_input("operation_one", 7, "provider_one", tap_input())
    if boundary in {"dispatch", "receipt", "barrier", "freeze"}:
        session.mark_dispatched("operation_one")
    if boundary in {"receipt", "barrier", "freeze"}:
        session.record_receipt("operation_one", "injected")
    if boundary in {"barrier", "freeze"}:
        session.stop_barrier(reason="provider_failed" if boundary == "barrier" else None)
    if boundary == "freeze":
        session.freeze()
    os._exit(23)


def _crash_after_object_publication(root):
    root = Path(root)
    budget = DiskBudget(root / "budget", capacity_bytes=32 * 1024 * 1024,
                        journal_headroom_bytes=256 * 1024,
                        free_bytes=lambda: 1 << 30)
    original_put_bytes = EvidenceStore.put_bytes

    def crash_after_put(store, body, **kwargs):
        reference = original_put_bytes(store, body, **kwargs)
        if body == b"published-before-recording-link":
            os._exit(31)
        return reference

    EvidenceStore.put_bytes = crash_after_put
    evidence = EvidenceStore(root / "evidence", budget)
    clock = FakeClock(1_000_000_000)
    recordings = RecordingStore(
        root / "recordings", evidence, ClockSynchronizer(clock),
        wall_clock_ms=lambda: 5_000_000,
    )
    _, session = begin_recording(recordings)
    session.record_frame(
        b"published-before-recording-link", "image/png", 10, 20, "portrait",
        acquisition_sequence=1,
    )
    os._exit(32)


class RecordingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.budget, self.evidence, self.recordings, self.clock = open_store(self.temp.name)

    def tearDown(self):
        self.recordings.close()
        self.evidence.close()
        self.budget.close()
        self.temp.cleanup()

    def test_freeze_builds_valid_original_with_actual_receipts(self):
        _, session = begin_recording(self.recordings)
        sequence = session.admit_input("operation_one", 7, "provider_one", tap_input())
        self.assertEqual(sequence, 1)
        self.assertEqual(session.journal_snapshot()[0]["dispatchState"], "admitted")
        session.mark_dispatched("operation_one")
        self.assertEqual(session.journal_snapshot()[0]["dispatchState"], "unknown")
        self.clock.advance(50_000_000)
        session.record_receipt("operation_one", "injected")
        session.stop_barrier()
        result = session.freeze()
        original = result["original"]
        contracts.validate_original_evidence(original)
        self.assertEqual(result["status"], "frozen-complete")
        self.assertEqual(original["events"][0]["generation"], 7)
        self.assertEqual(original["preparation"], preparation())

    def test_late_receipt_is_append_only_and_original_stays_unknown(self):
        _, session = begin_recording(self.recordings)
        session.admit_input("operation_one", 7, "provider_one", tap_input())
        session.mark_dispatched("operation_one")
        session.stop_barrier()
        frozen = session.freeze()
        before = frozen["recordingDigest"]
        session.record_receipt("operation_one", "injected")
        session.record_receipt("operation_one", "injected")
        with self.assertRaises(RecordingStoreError):
            session.record_receipt("operation_one", "rejected",
                                   error_code="input_rejected")
        after = self.recordings.load("recording_one")
        self.assertEqual(after["recordingDigest"], before)
        self.assertEqual(after["original"]["events"][0]["dispatch"], "unknown")
        self.assertEqual(after["lifecycleReceipts"][1]["kind"], "ack")
        self.assertEqual(after["lifecycleReceipts"][1]["status"], "complete")
        self.assertEqual(len(after["lifecycleReceipts"]), 2)
        contracts.validate_lifecycle_receipt(after["lifecycleReceipts"][1])

    def test_receipt_arriving_during_finalization_is_not_backfilled(self):
        _, session = begin_recording(self.recordings)
        session.admit_input("operation_one", 7, "provider_one", tap_input())
        session.mark_dispatched("operation_one")
        session.stop_barrier()
        session.record_receipt("operation_one", "injected")
        frozen = session.freeze()
        self.assertEqual(frozen["original"]["events"][0]["dispatch"], "unknown")
        self.assertEqual([item["kind"] for item in frozen["lifecycleReceipts"]],
                         ["stop", "ack"])

    def test_active_recording_pin_prevents_frame_retention(self):
        _, session = begin_recording(self.recordings)
        publication = session.record_frame(
            b"pinned-frame", "image/png", 10, 20, "portrait",
            acquisition_sequence=1)
        self.assertEqual(self.evidence.apply_retention(now_ms=2 ** 62), [])
        session.stop()
        self.assertIn(publication.digest,
                      self.evidence.apply_retention(now_ms=2 ** 62))

    def test_freeze_clears_a_crash_orphaned_recording_pin(self):
        _, session = begin_recording(self.recordings)
        reference = self.evidence.put_bytes(
            b"published-before-media-link", owner="recording_one",
            retention_class="original", retain_until_ms=10,
        )
        self.evidence.pin(
            reference.digest, "recording_recording_one", "recording")
        self.assertEqual(self.evidence.apply_retention(now_ms=11), [])
        session.stop()
        self.assertIn(reference.digest,
                      self.evidence.apply_retention(now_ms=2 ** 62))

    def test_original_tombstone_purges_journal_duplicate_and_denies_read(self):
        _, session = begin_recording(self.recordings)
        result = session.stop()
        self.evidence.tombstone(
            result["objectDigest"], reason="retention_expired")
        loaded = self.recordings.load("recording_one")
        self.assertIsNone(loaded["original"])
        self.assertEqual(session.journal_snapshot(), [])

    def test_injected_disk_pressure_freezes_truthful_incompleteness(self):
        _, session = begin_recording(self.recordings)
        self.budget._free_bytes = lambda: 1
        with self.assertRaises(RecordingStoreError):
            session.record_frame(b"cannot-reserve-spool", "image/png", 10, 20,
                                 "portrait", acquisition_sequence=1)
        frozen = self.recordings.load("recording_one")
        self.assertEqual(frozen["status"], "frozen-incomplete")
        self.assertEqual(frozen["original"]["media"], [])
        self.assertIn("storage_exhausted",
                      {item["reason"] for item in frozen["original"]["interruptions"]})

    def test_sample_token_is_exact_and_cannot_authorize_later_frame(self):
        registration, session = begin_recording(self.recordings, "sample-bound")
        first = b"approved-first-frame"
        token = registration.classify_sample(
            kind="pixels", sample_id="sample_one", sample_digest=hashlib.sha256(first).hexdigest(),
            provider_incarnation="provider_one", native_incarnation="native_one",
            acquisition_sequence=1, decision="approved",
        )
        published = session.record_frame(
            first, "image/png", 100, 200, "portrait",
            acquisition_sequence=1, classification=token,
        )
        self.assertIsNotNone(published)
        suppressed = session.record_frame(
            b"different-second-frame", "image/png", 100, 200, "portrait",
            acquisition_sequence=2, classification=token,
        )
        self.assertIsNone(suppressed)
        result = session.stop()
        self.assertEqual(result["status"], "frozen-incomplete")
        self.assertEqual(len(result["original"]["media"]), 1)
        self.assertTrue(result["original"]["interruptions"])

    def test_direct_stop_marks_blocked_frame_sink_incomplete_without_rewriting_original(self):
        _, session = begin_recording(self.recordings)
        entered = threading.Event()
        release = threading.Event()

        class BlockedSink:
            def accept_frame(self, publication, body):
                entered.set()
                release.wait(5)

        session.attach_frame_sink(BlockedSink())
        results = []

        def publish():
            results.append(session.record_frame(
                b"blocked-sink-frame", "image/png", 10, 20, "portrait",
                acquisition_sequence=1,
            ))

        producer = threading.Thread(target=publish)
        producer.start()
        self.assertTrue(entered.wait(5))
        frozen = session.stop()
        digest = frozen["recordingDigest"]
        self.assertEqual(frozen["status"], "frozen-incomplete")
        self.assertIn(
            "collection_inflight_stop",
            {item["reason"] for item in frozen["original"]["interruptions"]},
        )
        release.set()
        producer.join(5)
        self.assertFalse(producer.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(self.recordings.load("recording_one")["recordingDigest"], digest)

    def test_imported_project_json_is_not_a_registration_capability(self):
        with self.assertRaises(RecordingStoreError):
            self.recordings.begin_recording(
                project_document(), recording_id="recording_one", session_id="session_one",
                application_id="ios_app", build_id="original",
                device_identity={"bundle": "com.example.app", "artifactDigest": "0" * 64},
                provider_incarnation="provider_one", preparation_receipts=preparation(),
            )

    def test_trusted_collection_policy_does_not_change_frozen_g0_project_bytes(self):
        project = project_document()
        before = contracts.digest(project)
        registration = self.recordings.register_project(project, collection_policy())
        self.assertEqual(contracts.digest(project), before)
        self.assertEqual(registration.project_digest, before)
        self.assertNotIn("captureMode", registration.project)

    def test_unknown_policy_fields_and_pixels_boolean_alone_do_not_grant_capture(self):
        malformed = collection_policy()
        malformed["callerApproved"] = True
        with self.assertRaises(RecordingStoreError):
            self.recordings.register_project(project_document(), malformed)
        project = project_document()
        registration = self.recordings.register_project(project, collection_policy("sample-bound"))
        session = self.recordings.begin_recording(
            registration, recording_id="recording_one", session_id="session_one",
            application_id="ios_app", build_id="original",
            device_identity={"bundle": "com.example.app", "artifactDigest": "0" * 64},
            provider_incarnation="provider_one", preparation_receipts=preparation())
        self.assertIsNone(session.record_frame(b"unclassified", "image/png", 10, 10,
                                               "portrait", acquisition_sequence=1))

    def test_failed_preparation_cannot_enter_recording_or_inject_phantom_receipt(self):
        registration = self.recordings.register_project(project_document(), collection_policy())
        failed = preparation()
        failed[0]["status"] = "failed"
        with self.assertRaises(RecordingStoreError):
            self.recordings.begin_recording(
                registration, recording_id="recording_one", session_id="session_one",
                application_id="ios_app", build_id="original",
                device_identity={"bundle": "com.example.app", "artifactDigest": "0" * 64},
                provider_incarnation="provider_one", preparation_receipts=failed)
        self.assertEqual(self.recordings.list_recordings(), [])

    def test_unregistered_preparation_receipt_cannot_enter_recording(self):
        registration = self.recordings.register_project(
            project_document(), collection_policy())
        unknown = preparation()
        unknown[0]["recipeId"] = "unknown_recipe"
        with self.assertRaises(RecordingStoreError):
            self.recordings.begin_recording(
                registration, recording_id="recording_one",
                session_id="session_one", application_id="ios_app",
                build_id="original",
                device_identity={
                    "bundle": "com.example.app", "artifactDigest": "0" * 64,
                },
                provider_incarnation="provider_one",
                preparation_receipts=unknown,
            )
        self.assertEqual(self.recordings.list_recordings(), [])


class RecordingProcessRecoveryTests(unittest.TestCase):
    def test_process_crashes_recover_without_false_complete_or_reexecution(self):
        context = multiprocessing.get_context("spawn")
        for boundary in (
            "identity", "preparation", "admission", "dispatch",
            "receipt", "barrier", "freeze",
        ):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                process = context.Process(target=_crash_at_boundary, args=(directory, boundary))
                process.start()
                process.join(20)
                self.assertEqual(process.exitcode, 23)
                budget, evidence, recordings, _ = open_store(directory)
                try:
                    result = recordings.load("recording_one")
                    expected = "frozen-complete" if boundary == "freeze" else "frozen-incomplete"
                    self.assertEqual(result["status"], expected)
                    if boundary != "freeze":
                        self.assertEqual(result["status"], "frozen-incomplete")
                        self.assertEqual(
                            result["lifecycleReceipts"][-1]["kind"], "reconcile")
                        self.assertEqual(
                            result["lifecycleReceipts"][-1]["status"], "failed")
                    if boundary == "barrier":
                        self.assertEqual(
                            {item["reason"] for item in result["original"]["interruptions"]},
                            {"provider_failed"},
                        )
                    elif boundary != "freeze":
                        self.assertIn(
                            "process_restart",
                            {item["reason"] for item in result["original"]["interruptions"]},
                        )
                    self.assertLessEqual(len(result["original"]["events"]), 1)
                    if boundary == "identity":
                        self.assertEqual(result["original"]["preparation"], [])
                    elif boundary == "preparation":
                        self.assertEqual(result["original"]["preparation"], preparation())
                finally:
                    recordings.close()
                    evidence.close()
                    budget.close()

    def test_crash_after_blob_publication_cannot_create_phantom_media(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            process = context.Process(
                target=_crash_after_object_publication, args=(directory,))
            process.start()
            process.join(20)
            self.assertEqual(process.exitcode, 31)
            budget, evidence, recordings, _ = open_store(directory)
            try:
                result = recordings.load("recording_one")
                self.assertEqual(result["status"], "frozen-incomplete")
                self.assertEqual(result["original"]["media"], [])
                frame_digest = hashlib.sha256(
                    b"published-before-recording-link").hexdigest()
                self.assertIsNotNone(evidence.lookup(frame_digest))
            finally:
                recordings.close()
                evidence.close()
                budget.close()


if __name__ == "__main__":
    unittest.main()
