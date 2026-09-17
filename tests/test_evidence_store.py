import hashlib
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from reproloop.live.disk_budget import DiskBudget, DiskBudgetError
from reproloop.live.evidence_store import EvidenceStore, EvidenceStoreError


def _reserve_once(root, ready, start, finish, results):
    budget = DiskBudget(Path(root), capacity_bytes=4096, journal_headroom_bytes=1024,
                        free_bytes=lambda: 1 << 30)
    ready.put(True)
    start.wait(5)
    try:
        reservation = budget.reserve("worker", "spool", 2048)
    except DiskBudgetError:
        results.put("denied")
    else:
        results.put("reserved")
        finish.wait(5)
        reservation.close()
    finally:
        budget.close()


def _crash_with_flushed_blob(root):
    root = Path(root)
    budget = DiskBudget(root / "budget", capacity_bytes=1024 * 1024,
                        journal_headroom_bytes=64 * 1024, free_bytes=lambda: 1 << 30)
    store = EvidenceStore(root / "evidence", budget)
    body = b"crash-before-publication"
    writer = store.begin_blob(hashlib.sha256(body).hexdigest(), len(body),
                              owner="crash_owner", retention_class="intermediate",
                              retain_until_ms=5000)
    writer.write(body)
    writer.flush()
    os._exit(29)


def _delayed_publish(root, ready, publish, result):
    root = Path(root)
    budget = DiskBudget(root / "budget", capacity_bytes=1024 * 1024,
                        journal_headroom_bytes=64 * 1024, free_bytes=lambda: 1 << 30)
    store = EvidenceStore(root / "evidence", budget)
    body = b"cross-process-delayed"
    writer = store.begin_blob(hashlib.sha256(body).hexdigest(), len(body),
                              owner="delayed_owner", retention_class="derivative",
                              retain_until_ms=5000)
    writer.write(body)
    writer.flush()
    ready.set()
    publish.wait(10)
    try:
        writer.publish()
    except EvidenceStoreError:
        result.put("denied")
    else:
        result.put("published")
    store.close()
    budget.close()


class DiskBudgetTests(unittest.TestCase):
    def test_concurrent_reservations_preserve_journal_headroom(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("spawn")
            ready = context.Queue()
            start = context.Event()
            finish = context.Event()
            results = context.Queue()
            processes = [context.Process(target=_reserve_once,
                                         args=(directory, ready, start, finish, results))
                         for _ in range(2)]
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=10))
            start.set()
            observed = sorted(results.get(timeout=10) for _ in processes)
            finish.set()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(observed, ["denied", "reserved"])

    def test_external_pressure_is_bounded_fault_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            available = [4096]
            budget = DiskBudget(Path(directory), capacity_bytes=4096,
                                journal_headroom_bytes=1024,
                                free_bytes=lambda: available[0])
            reservation = budget.reserve("recording", "journal", 512)
            available[0] = 100
            with self.assertRaises(DiskBudgetError):
                budget.check_health()
            reservation.close()
            budget.close()


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.budget = DiskBudget(root / "budget", capacity_bytes=1024 * 1024,
                                 journal_headroom_bytes=64 * 1024,
                                 free_bytes=lambda: 1 << 30)
        self.store = EvidenceStore(root / "evidence", self.budget)

    def tearDown(self):
        self.store.close()
        self.budget.close()
        self.temp.cleanup()

    def test_blob_is_invisible_until_exact_digest_and_size_are_published(self):
        body = b"bounded-frame-bytes"
        digest = hashlib.sha256(body).hexdigest()
        writer = self.store.begin_blob(digest, len(body), owner="recording_one",
                                       retention_class="original", retain_until_ms=5000)
        writer.write(body)
        self.assertIsNone(self.store.lookup(digest))
        reference = writer.publish()
        self.assertEqual(reference.digest, digest)
        self.assertEqual(self.store.read(digest), body)

        bad = self.store.begin_blob("0" * 64, len(body), owner="recording_two",
                                    retention_class="intermediate", retain_until_ms=5000)
        bad.write(body)
        with self.assertRaises(EvidenceStoreError):
            bad.publish()
        self.assertIsNone(self.store.lookup("0" * 64))

    def test_active_pin_blocks_retention_then_tombstone_denies_reads(self):
        body = b"original-evidence"
        digest = hashlib.sha256(body).hexdigest()
        self.store.put_bytes(body, owner="recording_one", retention_class="original",
                             retain_until_ms=10)
        pin = self.store.pin(digest, "export_one", "export")
        self.assertEqual(self.store.apply_retention(now_ms=11), [])
        pin.close()
        self.assertEqual(self.store.apply_retention(now_ms=11), [digest])
        with self.assertRaises(EvidenceStoreError):
            self.store.read(digest)

    def test_recording_replay_export_and_finalizer_pins_all_block_retention(self):
        body = b"multi-use-evidence"
        digest = hashlib.sha256(body).hexdigest()
        self.store.put_bytes(body, owner="recording_one", retention_class="original",
                             retain_until_ms=10)
        pins = [self.store.pin(digest, f"pin_{purpose}", purpose)
                for purpose in ("recording", "replay", "export", "finalizer")]
        self.assertEqual(self.store.apply_retention(now_ms=11), [])
        for pin in pins:
            pin.close()
        self.assertEqual(self.store.apply_retention(now_ms=11), [digest])

    def test_delayed_writer_cannot_republish_persistent_tombstone(self):
        body = b"delayed-producer"
        digest = hashlib.sha256(body).hexdigest()
        writer = self.store.begin_blob(digest, len(body), owner="recording_one",
                                       retention_class="derivative", retain_until_ms=5000)
        writer.write(body)
        self.store.tombstone(digest, reason="access_revoked")
        with self.assertRaises(EvidenceStoreError):
            writer.publish()
        self.store.close()
        self.store = EvidenceStore(Path(self.temp.name) / "evidence", self.budget)
        with self.assertRaises(EvidenceStoreError):
            self.store.put_bytes(body, owner="recording_two",
                                 retention_class="derivative", retain_until_ms=6000)

    def test_persisted_object_path_cannot_escape_the_digest_layout(self):
        body = b"path-bound-evidence"
        reference = self.store.put_bytes(
            body, owner="recording_one", retention_class="original",
            retain_until_ms=5000,
        )
        outside = Path(self.temp.name) / "must-not-be-read-or-removed"
        outside.write_bytes(body)
        self.store._connection.execute(
            "UPDATE objects SET relative_path = '../must-not-be-read-or-removed' WHERE digest = ?",
            (reference.digest,),
        )
        with self.assertRaises(EvidenceStoreError):
            self.store.read(reference.digest)
        with self.assertRaises(EvidenceStoreError):
            self.store.tombstone(reference.digest, reason="access_revoked")
        self.assertEqual(outside.read_bytes(), body)

    def test_pin_identity_cannot_change_purpose(self):
        body = b"pin-purpose-bound-evidence"
        reference = self.store.put_bytes(
            body, owner="recording_one", retention_class="original",
            retain_until_ms=5000,
        )
        pin = self.store.pin(reference.digest, "consumer_one", "recording")
        with self.assertRaises(EvidenceStoreError):
            self.store.pin(reference.digest, "consumer_one", "export")
        pin.close()

    def test_failed_tombstone_delete_retries_on_restart_before_releasing_charge(self):
        body = b"durable-delete-obligation"
        reference = self.store.put_bytes(
            body, owner="recording_one", retention_class="original",
            retain_until_ms=10,
        )
        target = self.store.root / reference.path
        before = self.budget.snapshot()["chargedBytes"]
        real_unlink = Path.unlink

        def fail_target(path, *args, **kwargs):
            if path == target:
                raise PermissionError("bounded deletion failure")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_target):
            self.store.tombstone(reference.digest, reason="retention_expired")
        self.assertTrue(target.is_file())
        self.assertGreaterEqual(self.budget.snapshot()["chargedBytes"], before)
        self.store.close()
        self.store = EvidenceStore(Path(self.temp.name) / "evidence", self.budget)
        self.assertFalse(target.exists())
        self.assertEqual(self.budget.snapshot()["chargedBytes"], 0)
        with self.assertRaises(EvidenceStoreError):
            self.store.read(reference.digest)


class EvidenceStoreProcessTests(unittest.TestCase):
    def test_child_crash_after_blob_flush_never_publishes_partial_object(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            process = context.Process(target=_crash_with_flushed_blob, args=(directory,))
            process.start()
            process.join(20)
            self.assertEqual(process.exitcode, 29)
            root = Path(directory)
            budget = DiskBudget(root / "budget", capacity_bytes=1024 * 1024,
                                journal_headroom_bytes=64 * 1024,
                                free_bytes=lambda: 1 << 30)
            store = EvidenceStore(root / "evidence", budget)
            try:
                digest = hashlib.sha256(b"crash-before-publication").hexdigest()
                self.assertIsNone(store.lookup(digest))
                self.assertEqual(store.abandon_owner("crash_owner"), 1)
                self.assertEqual(budget.snapshot()["reservations"], 0)
            finally:
                store.close()
                budget.close()

    def test_cross_process_delayed_publication_loses_to_tombstone(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            ready = context.Event()
            publish = context.Event()
            result = context.Queue()
            process = context.Process(target=_delayed_publish,
                                      args=(directory, ready, publish, result))
            process.start()
            self.assertTrue(ready.wait(10))
            root = Path(directory)
            budget = DiskBudget(root / "budget", capacity_bytes=1024 * 1024,
                                journal_headroom_bytes=64 * 1024,
                                free_bytes=lambda: 1 << 30)
            store = EvidenceStore(root / "evidence", budget)
            digest = hashlib.sha256(b"cross-process-delayed").hexdigest()
            store.tombstone(digest, reason="access_revoked")
            publish.set()
            self.assertEqual(result.get(timeout=10), "denied")
            process.join(20)
            self.assertEqual(process.exitcode, 0)
            store.close()
            budget.close()


if __name__ == "__main__":
    unittest.main()
