from pathlib import Path
import fcntl
import hashlib
import json
import multiprocessing
import os
import tempfile
import unittest
from unittest import mock

from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.disk_budget import DiskBudget, DiskReservation
from reproof.live.evidence_store import EvidenceStore
from reproof.live.frame_spool import FrameSpool, FrameSpoolError
from reproof.live.recording_session import RecordingStore
from reproof.live.video import VideoCatalog, VideoFrameSink, VideoProtocolError
from tests.test_clock_sync import FakeClock
from tests.test_recording_recovery import (
    begin_recording, collection_policy, project_document,
)
from tests.test_video_state_machine import FakeEncoder, limits


def _open_binding_store(root):
    root = Path(root)
    budget = DiskBudget(root / "budget", capacity_bytes=512 * 1024 * 1024,
                        journal_headroom_bytes=8 * 1024 * 1024,
                        free_bytes=lambda: 1 << 30)
    evidence = EvidenceStore(root / "evidence", budget)
    recordings = RecordingStore(
        root / "recordings", evidence,
        ClockSynchronizer(FakeClock(1_000_000_000)),
        wall_clock_ms=lambda: 5_000_000,
    )
    return budget, evidence, recordings


def _crash_v1_prebinding(root, boundary):
    from reproof.live.frame_spool import FrameSpool

    _, evidence, recordings = _open_binding_store(root)
    _, session = begin_recording(recordings)
    sink = VideoFrameSink(
        evidence, Path(root) / "video", encoder=FakeEncoder(),
        limits=limits(max_segments=8), source_mode="transient-spool-v2")

    if boundary == "video-row":
        original = sink.journal.transaction

        def transaction(callback):
            result = original(callback)
            if getattr(callback, "__name__", "") == "insert":
                os._exit(73)
            return result

        sink.journal.transaction = transaction
        sink.bind_recording(session)
    elif boundary == "spool-constructor":
        original = FrameSpool.__init__

        def initialize(spool, *args, **kwargs):
            original(spool, *args, **kwargs)
            os._exit(73)

        with mock.patch.object(FrameSpool, "__init__", initialize):
            sink.bind_recording(session)
    elif boundary == "first-source-reservation":
        original = DiskBudget.reserve

        def reserve(budget, owner, category, amount, **kwargs):
            reservation = original(
                budget, owner, category, amount, **kwargs)
            if owner.startswith("frame-spool-") and category == "spool":
                os._exit(73)
            return reservation

        with mock.patch.object(DiskBudget, "reserve", reserve):
            sink.bind_recording(session)
    else:
        source_failure = mock.patch.object(
            FrameSpool, "__init__",
            side_effect=RuntimeError("source setup failed"))
        if boundary == "aborting-marker":
            boundary_patch = mock.patch.object(
                sink, "_prebinding_g2_empty",
                side_effect=lambda: os._exit(73))
        elif boundary == "source-retirement":
            original = sink._retire_prebinding_source

            def retire():
                original()
                os._exit(73)

            boundary_patch = mock.patch.object(
                sink, "_retire_prebinding_source", side_effect=retire)
        elif boundary == "work-cleanup":
            from reproof.live import video as video_module
            original = video_module._remove_empty_prebinding_work

            def clean_work(*args):
                original(*args)
                os._exit(73)

            boundary_patch = mock.patch.object(
                video_module, "_remove_empty_prebinding_work",
                side_effect=clean_work)
        elif boundary == "reservation-release":
            original = DiskReservation.close

            def close(reservation):
                original(reservation)
                if (reservation.owner == session.recording_id
                        and reservation.category == "spool"):
                    os._exit(73)

            boundary_patch = mock.patch.object(
                DiskReservation, "close", close)
        elif boundary == "journal-commit":
            original = DiskReservation.commit

            def commit(reservation, actual_bytes=None):
                original(reservation, actual_bytes)
                if (reservation.owner == session.recording_id
                        and reservation.category == "journal"):
                    os._exit(73)

            boundary_patch = mock.patch.object(
                DiskReservation, "commit", commit)
        else:
            os._exit(75)
        with source_failure, boundary_patch:
            sink.bind_recording(session)
    os._exit(74)


def _crash_v2_prebinding(root, boundary):
    _, evidence, recordings = _open_binding_store(root)
    _, session = begin_recording(recordings)
    sink = VideoFrameSink(
        evidence, Path(root) / "video", encoder=FakeEncoder(),
        limits=limits(max_segments=8), source_mode="transient-spool-v2")
    if boundary == "g2-use":
        original = session.use_frame_spool

        def use_frame_spool(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(73)

        with mock.patch.object(session, "use_frame_spool",
                               side_effect=use_frame_spool):
            sink.bind_recording(session)
    elif boundary == "bound":
        original = sink.journal.transaction

        def transaction(callback):
            result = original(callback)
            row = sink.journal.connection.execute(
                "SELECT binding_state FROM sessions WHERE recording_id=?",
                (session.recording_id,)).fetchone()
            if row is not None and row["binding_state"] == "bound":
                os._exit(73)
            return result

        sink.journal.transaction = transaction
        sink.bind_recording(session)
    else:
        os._exit(75)
    os._exit(74)


class VideoBindingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.budget = DiskBudget(root / "budget", capacity_bytes=512 * 1024 * 1024,
                                 journal_headroom_bytes=8 * 1024 * 1024,
                                 free_bytes=lambda: 1 << 30)
        self.evidence = EvidenceStore(root / "evidence", self.budget)
        self.recordings = RecordingStore(root / "recordings", self.evidence,
                                         ClockSynchronizer(FakeClock(1_000_000_000)),
                                         wall_clock_ms=lambda: 5_000_000)
        self.sink = None

    def tearDown(self):
        if self.sink is not None:
            self.sink.close()
        self.recordings.close()
        self.evidence.close()
        self.budget.close()
        self.temp.cleanup()

    def test_v2_binding_failure_leaves_bounded_aborted_tombstone(self):
        _, session = begin_recording(self.recordings)
        self.sink = VideoFrameSink(
            self.evidence, Path(self.temp.name) / "video",
            encoder=FakeEncoder(), limits=limits(max_segments=8),
            source_mode="transient-spool-v2",
        )
        with mock.patch.object(FrameSpool, "__init__",
                       side_effect=RuntimeError("source setup failed")):
            with self.assertRaises(RuntimeError):
                self.sink.bind_recording(session)
        row = self.sink.journal.connection.execute(
            "SELECT state,binding_state,failure_reason,journal_reservation_id "
            "FROM sessions WHERE recording_id=?",
            (session.recording_id,),
        ).fetchone()
        self.assertEqual((row["state"], row["binding_state"]), ("aborted", "aborted"))
        self.assertEqual(row["failure_reason"], "prebinding_failed")
        reservations = self.budget.reservations_for_owner(session.recording_id, category="journal")
        video_row = next(item for item in reservations if
                         item["reservation_id"] == row["journal_reservation_id"])
        self.assertEqual(video_row["state"], "committed")
        self.assertEqual(video_row["charged_bytes"], 256 * 1024)
        self.assertFalse(any(
            item["state"] == "active"
            for category in ("spool", "encoding")
            for item in self.budget.reservations_for_owner(
                session.recording_id, category=category)))
        self.assertEqual(self.sink._state, "aborted")

    def test_no_database_constructor_residue_requires_exclusive_empty_lock(self):
        _, session = begin_recording(self.recordings)
        self.sink = VideoFrameSink(
            self.evidence, Path(self.temp.name) / "video",
            encoder=FakeEncoder(), limits=limits(max_segments=8),
            source_mode="transient-spool-v2",
        )
        source_root = self.sink.root / "sources" / hashlib.sha256(
            session.recording_id.encode("ascii")).hexdigest()
        (source_root / "frames").mkdir(parents=True)
        lock = source_root / "writer.lock"
        lock.touch(mode=0o600)
        lock_inode = lock.stat().st_ino
        descriptor = os.open(lock, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with mock.patch.object(FrameSpool, "__init__",
                            side_effect=RuntimeError("source setup failed")):
                with self.assertRaises(FrameSpoolError):
                    self.sink.bind_recording(session)
            self.assertTrue(source_root.is_dir())
            row = self.sink.journal.connection.execute(
                "SELECT binding_state FROM sessions WHERE recording_id=?",
                (session.recording_id,),
            ).fetchone()
            self.assertEqual(row["binding_state"], "aborting")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        self.sink.close()
        self.assertTrue((source_root / "frames").is_dir())
        self.assertEqual(lock.stat().st_ino, lock_inode)
        self.assertTrue(any(
            item["state"] == "committed"
            and item["charged_bytes"] == 256 * 1024
            for item in self.budget.reservations_for_owner(
                session.recording_id, category="journal")))

    def test_unexpected_prebinding_source_payload_is_preserved(self):
        _, session = begin_recording(self.recordings)
        self.sink = VideoFrameSink(
            self.evidence, Path(self.temp.name) / "video",
            encoder=FakeEncoder(), limits=limits(max_segments=8),
            source_mode="transient-spool-v2",
        )
        source_root = self.sink.root / "sources" / hashlib.sha256(
            session.recording_id.encode("ascii")).hexdigest()
        source_root.mkdir(parents=True)
        payload = source_root / "unexpected.bin"
        payload.write_bytes(b"preserve")
        with mock.patch.object(FrameSpool, "__init__",
                       side_effect=RuntimeError("source setup failed")):
            with self.assertRaises(Exception):
                self.sink.bind_recording(session)
        self.assertEqual(payload.read_bytes(), b"preserve")
        row = self.sink.journal.connection.execute(
            "SELECT binding_state FROM sessions WHERE recording_id=?",
            (session.recording_id,),
        ).fetchone()
        self.assertEqual(row["binding_state"], "aborting")

    def test_close_after_abort_proof_failure_preserves_every_cleanup_authority(self):
        _, session = begin_recording(self.recordings)
        self.sink = VideoFrameSink(
            self.evidence, Path(self.temp.name) / "video",
            encoder=FakeEncoder(), limits=limits(max_segments=8),
            source_mode="transient-spool-v2",
        )
        source_root = self.sink.root / "sources" / hashlib.sha256(
            session.recording_id.encode("ascii")).hexdigest()
        source_root.mkdir(parents=True)
        payload = source_root / "spool.sqlite3-wal"
        payload.write_bytes(b"ordinary payload with a reserved filename")
        with mock.patch.object(FrameSpool, "__init__",
                        side_effect=RuntimeError("source setup failed")):
            with self.assertRaises(Exception):
                self.sink.bind_recording(session)

        work = self.sink.work
        reservation_ids = {
            item["reservation_id"] for category in ("journal", "spool", "encoding")
            for item in self.budget.reservations_for_owner(
                session.recording_id, category=category)
        }
        self.sink.close()

        self.assertIsNone(self.sink.journal.connection)
        self.assertIsNone(self.sink._writer_fd)
        self.assertTrue(work.is_dir())
        self.assertEqual(payload.read_bytes(), b"ordinary payload with a reserved filename")
        retained = {
            item["reservation_id"] for category in ("journal", "spool", "encoding")
            for item in self.budget.reservations_for_owner(
                session.recording_id, category=category)
        }
        self.assertTrue(reservation_ids <= retained)

    def test_reconcile_v1_requires_source_and_work_absence_before_releasing(self):
        registration, session = begin_recording(self.recordings)
        self.sink = VideoFrameSink(
            self.evidence, Path(self.temp.name) / "video",
            encoder=FakeEncoder(), limits=limits(max_segments=8),
            source_mode="transient-spool-v2",
        )
        source_root = self.sink.root / "sources" / hashlib.sha256(
            session.recording_id.encode("ascii")).hexdigest()
        source_root.mkdir(parents=True)
        payload = source_root / "spool.sqlite3-shm"
        payload.write_bytes(b"payload")
        with mock.patch.object(FrameSpool, "__init__",
                        side_effect=RuntimeError("source setup failed")):
            with self.assertRaises(Exception):
                self.sink.bind_recording(session)
        work = self.sink.work
        ids = {
            item["reservation_id"] for category in ("journal", "spool", "encoding")
            for item in self.budget.reservations_for_owner(
                session.recording_id, category=category)
        }
        self.sink.close()
        self.sink = None
        self.recordings.close()
        self.recordings = RecordingStore(
            Path(self.temp.name) / "recordings", self.evidence,
            ClockSynchronizer(FakeClock(1_000_000_000)),
            wall_clock_ms=lambda: 5_000_000,
        )
        self.recordings.register_project(
            registration.project, registration.collection_policy)
        catalog = VideoCatalog(Path(self.temp.name) / "video", self.evidence)
        try:
            with self.assertRaises((VideoProtocolError, FrameSpoolError)):
                catalog.reconcile_prebindings(
                    self.recordings, registration.project_digest)
            row = catalog.journal.connection.execute(
                "SELECT binding_state FROM sessions WHERE recording_id=?",
                (session.recording_id,),
            ).fetchone()
            self.assertEqual(row["binding_state"], "aborting")
            self.assertTrue(work.is_dir())
            self.assertEqual(payload.read_bytes(), b"payload")
            retained = {
                item["reservation_id"] for category in ("journal", "spool", "encoding")
                for item in self.budget.reservations_for_owner(
                    session.recording_id, category=category)
            }
            self.assertTrue(ids <= retained)
        finally:
            catalog.close()

    def test_reconcile_skips_an_active_g2_recording(self):
        registration, session = begin_recording(self.recordings)
        self.sink = VideoFrameSink(
            self.evidence, Path(self.temp.name) / "video",
            encoder=FakeEncoder(), limits=limits(max_segments=8),
            source_mode="transient-spool-v2",
        )
        with mock.patch.object(self.sink, "_abort_prebinding",
                               side_effect=RuntimeError("simulated crash")), \
                mock.patch.object(FrameSpool, "__init__",
                           side_effect=RuntimeError("source setup failed")):
            with self.assertRaises(RuntimeError):
                self.sink.bind_recording(session)
        catalog = VideoCatalog(Path(self.temp.name) / "video", self.evidence)
        try:
            self.assertEqual(catalog.reconcile_prebindings(
                self.recordings, registration.project_digest),
                {"aborted": 0, "bound": 0, "skipped": 1})
        finally:
            catalog.close()

    def test_reconcile_filters_other_project_before_metadata_or_video_lock(self):
        _, session = begin_recording(self.recordings)
        self.sink = VideoFrameSink(
            self.evidence, Path(self.temp.name) / "video",
            encoder=FakeEncoder(), limits=limits(max_segments=8),
            source_mode="transient-spool-v2",
        )
        with mock.patch.object(self.sink, "_abort_prebinding",
                               side_effect=RuntimeError("simulated crash")), \
                mock.patch.object(FrameSpool, "__init__",
                                  side_effect=RuntimeError("source setup failed")):
            with self.assertRaises(RuntimeError):
                self.sink.bind_recording(session)
        other_project = project_document()
        other_project["id"] = "checkout_other"
        other_project["revision"] = "r2"
        other = self.recordings.register_project(
            other_project, collection_policy())
        catalog = VideoCatalog(Path(self.temp.name) / "video", self.evidence)
        try:
            self.assertEqual(catalog.reconcile_prebindings(
                self.recordings, other.project_digest),
                {"aborted": 0, "bound": 0, "skipped": 1})
        finally:
            catalog.close()
        self.sink._state = "aborting"

    def test_reconcile_uses_only_public_exact_metadata(self):
        catalog = VideoCatalog(Path(self.temp.name) / "video", self.evidence)

        class PrivateOnlyStore:
            evidence = self.evidence

            def _recording_row(self, recording_id):
                return {"project_digest": "0" * 64,
                        "source_mode": "original-cas-v1"}

        try:
            with self.assertRaises((AttributeError, VideoProtocolError)):
                catalog.reconcile_prebindings(PrivateOnlyStore(), "0" * 64)
        finally:
            catalog.close()

    def test_process_crashes_across_v1_abort_boundaries_reconcile_idempotently(self):
        boundaries = (
            "video-row", "first-source-reservation", "spool-constructor",
            "aborting-marker",
            "source-retirement", "work-cleanup", "reservation-release",
            "journal-commit",
        )
        context = multiprocessing.get_context("spawn")
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                process = context.Process(
                    target=_crash_v1_prebinding, args=(temporary, boundary))
                process.start()
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                    self.fail(f"prebinding crash child leaked at {boundary}")
                self.assertEqual(process.exitcode, 73)

                budget, evidence, recordings = _open_binding_store(temporary)
                registration = recordings.register_project(
                    project_document(), collection_policy())
                catalog = VideoCatalog(Path(temporary) / "video", evidence)
                try:
                    source = Path(temporary) / "video" / "sources" / hashlib.sha256(
                        b"recording_one").hexdigest()
                    lock_inode = ((source / "writer.lock").stat().st_ino
                                  if boundary == "first-source-reservation" else None)
                    if lock_inode is not None:
                        root_digest = hashlib.sha256(
                            os.path.abspath(os.fspath(source)).encode("utf-8")
                        ).hexdigest()[:32]
                        owner = ("frame-spool-" + root_digest + "-" +
                                 hashlib.sha256(b"recording_one").hexdigest()[:16])
                        self.assertEqual(len(budget.reservations_for_owner(
                            owner, category="spool")), 1)
                    self.assertEqual(catalog.reconcile_prebindings(
                        recordings, registration.project_digest),
                        {"aborted": 1, "bound": 0, "skipped": 0})
                    self.assertEqual(catalog.reconcile_prebindings(
                        recordings, registration.project_digest),
                        {"aborted": 0, "bound": 0, "skipped": 0})
                    row = catalog.journal.connection.execute(
                        "SELECT state,binding_state,failure_reason "
                        "FROM sessions WHERE recording_id='recording_one'"
                    ).fetchone()
                    self.assertEqual(tuple(row),
                                     ("aborted", "aborted", "prebinding_failed"))
                    work = Path(temporary) / "video" / "work" / hashlib.sha256(
                        b"recording_one").hexdigest()
                    self.assertFalse(os.path.lexists(work))
                    if lock_inode is not None:
                        self.assertEqual((source / "writer.lock").stat().st_ino,
                                         lock_inode)
                        self.assertTrue((source / "frames").is_dir())
                        self.assertEqual(budget.reservations_for_owner(
                            owner, category="spool"), ())
                    self.assertFalse(any(
                        item["state"] == "active"
                        for category in ("spool", "encoding")
                        for item in budget.reservations_for_owner(
                            "recording_one", category=category)))
                    video_journals = budget.reservations_for_owner(
                        "recording_one", category="journal")
                    self.assertTrue(any(
                        item["state"] == "committed"
                        and item["charged_bytes"] == 256 * 1024
                        for item in video_journals))
                finally:
                    catalog.close()
                    recordings.close()
                    evidence.close()
                    budget.close()

    def test_process_crashes_after_g2_use_and_bound_preserve_v2_authority(self):
        context = multiprocessing.get_context("spawn")
        for boundary, expected_binding in (("g2-use", "preparing"),
                                           ("bound", "bound")):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                process = context.Process(
                    target=_crash_v2_prebinding, args=(temporary, boundary))
                process.start()
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                    self.fail(f"v2 binding crash child leaked at {boundary}")
                self.assertEqual(process.exitcode, 73)

                budget, evidence, recordings = _open_binding_store(temporary)
                registration = recordings.register_project(
                    project_document(), collection_policy())
                metadata = recordings.prebinding_recording_metadata(
                    "recording_one", registration.project_digest)
                self.assertEqual(metadata["sourceMode"], "transient-spool-v2")
                self.assertFalse(metadata["active"])
                catalog = VideoCatalog(Path(temporary) / "video", evidence)
                source = Path(temporary) / "video" / "sources" / hashlib.sha256(
                    b"recording_one").hexdigest()
                try:
                    row = catalog.journal.connection.execute(
                        "SELECT * FROM sessions WHERE recording_id='recording_one'"
                    ).fetchone()
                    self.assertEqual(row["binding_state"], expected_binding)
                    self.assertTrue((source / "spool.sqlite3").is_file())
                    if boundary == "g2-use":
                        class RecoveryStore:
                            def __init__(self, store, owned_evidence):
                                self.store = store
                                self.evidence = owned_evidence

                            def prebinding_recording_metadata(self, *args):
                                return self.store.prebinding_recording_metadata(*args)

                            def prebinding_recording_ids(self, *args):
                                return self.store.prebinding_recording_ids(*args)

                            def recovery_session(self, recording_id):
                                return object()

                        released = False
                        real_flock = fcntl.flock

                        def observe_flock(descriptor, operation):
                            nonlocal released
                            result = real_flock(descriptor, operation)
                            if operation == fcntl.LOCK_UN:
                                released = True
                            return result

                        def recover_after_unlock(_recovery):
                            self.assertTrue(released)
                            return {}

                        with mock.patch("reproof.live.video.fcntl.flock",
                                        side_effect=observe_flock), \
                                mock.patch.object(
                                    catalog, "recover_transient",
                                    side_effect=recover_after_unlock):
                            result = catalog.reconcile_prebindings(
                                RecoveryStore(recordings, evidence),
                                registration.project_digest)
                        self.assertEqual(result,
                                         {"aborted": 0, "bound": 1, "skipped": 0})
                    else:
                        self.assertEqual(catalog.reconcile_prebindings(
                            recordings, registration.project_digest),
                            {"aborted": 0, "bound": 0, "skipped": 0})
                    self.assertTrue(any(
                        item["state"] == "active"
                        for category in ("spool", "encoding")
                        for item in budget.reservations_for_owner(
                            "recording_one", category=category)))
                finally:
                    catalog.close()
                    recordings.close()
                    evidence.close()
                    budget.close()

    def test_v2_promotion_rejects_each_changed_historical_source_field(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temporary:
            process = context.Process(
                target=_crash_v2_prebinding, args=(temporary, "g2-use"))
            process.start()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
                self.fail("v2 configuration crash child leaked")
            self.assertEqual(process.exitcode, 73)

            budget, evidence, recordings = _open_binding_store(temporary)
            registration = recordings.register_project(
                project_document(), collection_policy())
            catalog = VideoCatalog(Path(temporary) / "video", evidence)
            original = catalog.journal.connection.execute(
                "SELECT source_config_json FROM sessions "
                "WHERE recording_id='recording_one'"
            ).fetchone()[0]
            baseline = json.loads(original)
            changed = []
            wrong_root = json.loads(original)
            wrong_root["sourceRoot"] += "-other"
            changed.append(wrong_root)
            wrong_retention = json.loads(original)
            wrong_retention["retainUntilMs"] += 1
            changed.append(wrong_retention)
            wrong_limits = json.loads(original)
            wrong_limits["limits"]["max_queue_frames"] += 1
            changed.append(wrong_limits)
            try:
                for source_config in changed:
                    with self.subTest(field=next(
                            key for key in baseline
                            if baseline[key] != source_config[key])):
                        catalog.journal.connection.execute(
                            "UPDATE sessions SET source_config_json=? "
                            "WHERE recording_id='recording_one'",
                            (json.dumps(source_config, sort_keys=True,
                                        separators=(",", ":")),))
                        with self.assertRaises(VideoProtocolError):
                            catalog.reconcile_prebindings(
                                recordings, registration.project_digest)
                        binding = catalog.journal.connection.execute(
                            "SELECT binding_state FROM sessions "
                            "WHERE recording_id='recording_one'"
                        ).fetchone()[0]
                        self.assertEqual(binding, "preparing")
                catalog.journal.connection.execute(
                    "UPDATE sessions SET source_config_json=? "
                    "WHERE recording_id='recording_one'", (original,))
            finally:
                catalog.close()
                recordings.close()
                evidence.close()
                budget.close()


if __name__ == "__main__":
    unittest.main()
