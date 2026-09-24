"""Independent recording authority, barrier and privacy probes."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from reproof import contracts
from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.disk_budget import DiskBudget, DiskBudgetError
from reproof.live.evidence_store import EvidenceStore, EvidenceStoreError
from reproof.live.recording_session import RecordingStore, RecordingStoreError
from tests.test_clock_sync import FakeClock
from tests.test_recording_recovery import (
    begin_recording, collection_policy, open_store, project_document, tap_input,
)


class RecordingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.budget, self.evidence, self.recordings, self.clock = open_store(self.temp.name)

    def tearDown(self):
        self.recordings.close()
        self.evidence.close()
        self.budget.close()
        self.temp.cleanup()

    def test_project_registration_charge_survives_reopen_without_duplication(self):
        project = project_document()
        policy = collection_policy()
        before = self.budget.snapshot()["chargedBytes"]
        self.recordings.register_project(project, policy)
        charged = self.budget.snapshot()["chargedBytes"]
        self.assertGreater(charged, before)
        self.recordings.register_project(project, policy)
        self.assertEqual(self.budget.snapshot()["chargedBytes"], charged)
        self.recordings.close()
        self.recordings = RecordingStore(
            Path(self.temp.name) / "recordings", self.evidence,
            ClockSynchronizer(self.clock), wall_clock_ms=lambda: 5_000_000)
        self.recordings.register_project(project, policy)
        self.assertEqual(self.budget.snapshot()["chargedBytes"], charged)

    def test_registration_storage_denial_precedes_persistence(self):
        self.budget._free_bytes = lambda: 0
        with self.assertRaises((DiskBudgetError, RecordingStoreError)):
            self.recordings.register_project(project_document(), collection_policy())
        count = self.recordings._connection.execute(
            "SELECT COUNT(*) FROM registered_projects").fetchone()[0]
        self.assertEqual(count, 0)

    def test_admitted_multibyte_inputs_always_fit_frozen_evidence(self):
        _, session = begin_recording(self.recordings)
        command = tap_input()
        current = command["target"]
        current["value"] = "🚀" * 127
        for _ in range(3):
            current["ancestor"] = {"kind": "accessibility-id", "value": "🚀" * 127}
            current = current["ancestor"]
        contracts.validate_input(command)
        admitted = 0
        for index in range(1024):
            operation = "multibyte_" + str(index)
            try:
                session.admit_input(operation, 7, "provider_one", command)
            except RecordingStoreError:
                break
            session.record_receipt(operation, "injected")
            admitted += 1
        self.assertGreater(admitted, 0)
        frozen = session.stop()
        self.assertEqual(frozen["original"]["endSequence"], admitted)
        self.assertEqual(len(frozen["original"]["events"]), admitted)

    def _begin(self, registration, *, recording_id="recording_one", session_id="session_one"):
        return self.recordings.begin_recording(
            registration, recording_id=recording_id, session_id=session_id,
            application_id="ios_app", build_id="original",
            device_identity={"bundle": "com.example.app", "artifactDigest": "0" * 64},
            provider_incarnation="provider_one", preparation_receipts=[])

    def test_registration_mapping_mutation_cannot_enable_suppressed_capture(self):
        project = project_document()
        project["evidencePolicy"]["pixels"] = False
        registration = self.recordings.register_project(project, collection_policy("suppressed"))
        session = self._begin(registration)
        try:
            registration.project["evidencePolicy"]["pixels"] = True
            registration.collection_policy["captureMode"] = "test-data"
        except (TypeError, AttributeError):
            pass  # Immutable mappings may reject a mutation outright.
        self.assertIsNone(session.record_frame(b"must remain suppressed", "image/png", 10, 10,
                                               "portrait", acquisition_sequence=1))

    def test_registered_project_revision_cannot_change_its_policy(self):
        project = project_document()
        self.recordings.register_project(project, collection_policy("sample-bound"))
        changed = project_document()
        changed["evidencePolicy"]["logs"] = not changed["evidencePolicy"]["logs"]
        with self.assertRaises(RecordingStoreError):
            self.recordings.register_project(changed, collection_policy("sample-bound"))

    def test_classification_from_other_project_is_not_capture_authority(self):
        first = self.recordings.register_project(project_document(), collection_policy("sample-bound"))
        other_project = project_document()
        other_project["id"] = "other_project"
        other = self.recordings.register_project(other_project, collection_policy("sample-bound"))
        session = self._begin(other)
        body = b"classification belongs to a different project"
        classification = first.classify_sample(
            kind="pixels", sample_id="sample_one", sample_digest=hashlib.sha256(body).hexdigest(),
            provider_incarnation="provider_one", native_incarnation="native_one",
            acquisition_sequence=1, decision="approved")
        self.assertIsNone(session.record_frame(body, "image/png", 10, 10, "portrait",
                                               acquisition_sequence=1, classification=classification))

    def test_native_restart_cannot_reuse_old_sample_classification(self):
        registration, session = begin_recording(self.recordings, "sample-bound")
        received = self.recordings.clock_sync.sample()
        sent = self.recordings.clock_sync.sample()
        mapping = self.recordings.clock_sync.record_exchange(
            coordinator_clock_id="native-clock", coordinator_send_ns=900_000_000,
            coordinator_receive_ns=901_000_000, host_received=received,
            host_sent=sent, coordinator_uncertainty_ns=0, max_drift_ppm=0)
        binding = session.anchor.bind_provider(mapping, provider_boot_digest="b" * 64,
                                               native_incarnation="native_restarted")
        body = b"same visible bytes do not mean the same acquired sample"
        classification = registration.classify_sample(
            kind="pixels", sample_id="sample_one", sample_digest=hashlib.sha256(body).hexdigest(),
            provider_incarnation="provider_one", native_incarnation="native_old",
            acquisition_sequence=1, decision="approved")
        try:
            publication = session.record_frame(
                body, "image/png", 10, 10, "portrait", acquisition_sequence=1,
                classification=classification, provider_clock_binding=binding,
                provider_monotonic_ns=901_000_000)
        except RecordingStoreError:
            publication = None  # Explicit rejection is also capture denial.
        self.assertIsNone(publication)
        self.assertEqual(self.recordings.media_timeline(session.recording_id), [])

    def test_ack_during_finalization_cannot_improve_original(self):
        _, session = begin_recording(self.recordings)
        session.admit_input("operation_one", 7, "provider_one", tap_input())
        session.stop_barrier()
        session.record_receipt("operation_one", "injected")
        result = session.freeze()
        self.assertEqual(result["status"], "frozen-incomplete")
        self.assertEqual(result["original"]["events"][0]["dispatch"], "unknown")
        acks = [item for item in result["lifecycleReceipts"] if item["kind"] == "ack"]
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0]["recordingDigest"], result["recordingDigest"])
        self.assertEqual(acks[0]["status"], "complete")

    def test_suppressed_short_text_does_not_leave_dictionary_testable_digest(self):
        project = project_document()
        project["evidencePolicy"]["text"] = False
        registration = self.recordings.register_project(project, collection_policy("test-data"))
        session = self._begin(registration)
        value = {"value": "0421"}
        wire = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        expected_digest = hashlib.sha256(wire).hexdigest().encode()
        self.assertIsNone(session.record_observation("text", value, acquisition_sequence=1))
        for path in Path(self.temp.name).rglob("*"):
            if path.is_file():
                self.assertFalse(expected_digest in path.read_bytes(),
                                 "Suppressed text digest persisted in " + path.name)

    def test_opening_second_store_cannot_recover_a_still_active_recording(self):
        _, session = begin_recording(self.recordings)
        second = None
        try:
            try:
                second = RecordingStore(Path(self.temp.name) / "recordings", self.evidence,
                                        ClockSynchronizer(self.clock),
                                        wall_clock_ms=lambda: 5_000_000)
            except RecordingStoreError:
                return  # An exclusive recording-store owner may reject it.
            self.assertEqual(self.recordings.load(session.recording_id)["status"], "recording")
        finally:
            if second is not None:
                second.close()

    def test_journal_reservation_covers_even_the_admitted_input_payloads(self):
        _, session = begin_recording(self.recordings)
        action = tap_input()
        action["target"]["value"] = "x" * 500
        payload_bytes = len(json.dumps(action, sort_keys=True, separators=(",", ":")).encode())
        admitted = 0
        for index in range(200):
            try:
                session.admit_input("operation_" + str(index), 7, "provider_one", action)
            except (RecordingStoreError, DiskBudgetError):
                break  # Bounded admission may stop when its reservation ends.
            admitted += 1
        # This lower bound excludes indexes, receipts, SQLite pages and WAL.
        self.assertGreater(admitted, 0)
        self.assertGreaterEqual(self.budget.snapshot()["chargedBytes"], admitted * payload_bytes)

    def test_tombstoned_original_is_not_returned_from_duplicated_journal_json(self):
        _, session = begin_recording(self.recordings)
        result = session.stop()
        self.evidence.tombstone(result["objectDigest"], reason="retention_expired")
        try:
            loaded = self.recordings.load(session.recording_id)
        except RecordingStoreError:
            return
        self.assertIsNone(loaded["original"])

    def test_every_admitted_command_fits_the_reserved_freeze_metadata(self):
        _, session = begin_recording(self.recordings)
        admitted = 0
        for index in range(2000):
            operation = "operation_" + str(index)
            try:
                session.admit_input(operation, 7, "provider_one",
                                    {"action": "back", "parameters": {}})
            except (RecordingStoreError, DiskBudgetError):
                break
            admitted += 1
            session.record_receipt(operation, "injected")
        self.assertGreater(admitted, 0)
        frozen = session.stop()
        self.assertEqual(frozen["original"]["endSequence"], admitted)
        self.assertEqual(len(frozen["original"]["events"]), admitted)

    def test_duration_limit_rejects_new_input_and_preserves_prior_evidence(self):
        _, session = begin_recording(self.recordings)
        session.admit_input("operation_one", 7, "provider_one", tap_input())
        session.record_receipt("operation_one", "injected")
        self.clock.advance(601_000_000_000)
        with self.assertRaises(RecordingStoreError):
            session.admit_input("operation_too_late", 7, "provider_one", tap_input())
        frozen = session.stop()
        self.assertEqual(frozen["original"]["endSequence"], 1)


class ManagedStorageBudgetTests(unittest.TestCase):
    def test_registered_project_metadata_is_budgeted_before_commit(self):
        capacity = 4 * 1024 * 1024
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budget = evidence = recordings = None

            def check_managed_bytes():
                actual = sum(path.stat().st_size for path in root.rglob("*")
                             if path.is_file() and not path.is_symlink())
                self.assertLessEqual(actual, capacity,
                                     "Registered project metadata exceeds shared capacity")

            try:
                try:
                    budget = DiskBudget(root / "budget", capacity_bytes=capacity,
                                        journal_headroom_bytes=256 * 1024,
                                        free_bytes=lambda: 1 << 30)
                    evidence = EvidenceStore(root / "evidence", budget)
                    recordings = RecordingStore(root / "recordings", evidence,
                        ClockSynchronizer(FakeClock(1_000_000_000)),
                        wall_clock_ms=lambda: 5_000_000)
                    project = project_document()
                    for index in range(4096):
                        project["revision"] = "registered_revision_" + str(index)
                        recordings.register_project(project, collection_policy())
                        check_managed_bytes()
                except (DiskBudgetError, EvidenceStoreError, RecordingStoreError):
                    pass
                check_managed_bytes()
            finally:
                for resource in (recordings, evidence, budget):
                    if resource is not None:
                        resource.close()

    def test_managed_files_including_sqlite_wal_fit_shared_capacity(self):
        capacity = 4 * 1024 * 1024
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budget = evidence = recordings = None

            def check_managed_bytes():
                occupied = sum(path.stat().st_size for path in root.rglob("*")
                               if path.is_file() and not path.is_symlink())
                self.assertLessEqual(occupied, capacity,
                                     "Managed SQLite/WAL/files exceed the shared byte capacity")

            try:
                try:
                    budget = DiskBudget(root / "budget", capacity_bytes=capacity,
                                        journal_headroom_bytes=256 * 1024,
                                        free_bytes=lambda: 1 << 30)
                    evidence = EvidenceStore(root / "evidence", budget)
                    recordings = RecordingStore(root / "recordings", evidence,
                        ClockSynchronizer(FakeClock(1_000_000_000)),
                        wall_clock_ms=lambda: 5_000_000)
                    _, session = begin_recording(recordings)
                    check_managed_bytes()
                    for index in range(500):
                        operation = "operation_" + str(index)
                        session.admit_input(operation, 7, "provider_one",
                                            {"action": "back", "parameters": {}})
                        session.record_receipt(operation, "injected")
                        check_managed_bytes()
                    session.stop()
                except (DiskBudgetError, EvidenceStoreError, RecordingStoreError):
                    # A small configured quota may deny startup or admission.
                    # Denial cannot arrive after managed files already exceed it.
                    pass
                check_managed_bytes()
            finally:
                for store in (recordings, evidence, budget):
                    if store is not None:
                        store.close()


if __name__ == "__main__":
    unittest.main()
