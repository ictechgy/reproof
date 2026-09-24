"""Independent G2 storage probes; run after the goal worker has stopped."""
import hashlib
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from reproof.live.disk_budget import DiskBudget, DiskBudgetError
from reproof.live.evidence_store import EvidenceStore, EvidenceStoreError
from reproof.live import evidence_store as evidence_module


def _hold_reservation(root, ready, finish):
    budget = DiskBudget(Path(root), capacity_bytes=8192, journal_headroom_bytes=1024,
                        free_bytes=lambda: 1 << 30)
    reservation = budget.reserve("different_process", "spool", 6144)
    ready.put("held")
    try:
        if not finish.wait(10):
            raise RuntimeError("Parent did not finish bounded reservation probe")
    finally:
        reservation.close()
        budget.close()


def _limited_staging_attempt(directory, ready, start, finish, outcomes):
    root = Path(directory)
    budget = DiskBudget(root / "budget", capacity_bytes=1024 * 1024,
                        journal_headroom_bytes=64 * 1024, free_bytes=lambda: 1 << 30)
    store = EvidenceStore(root / "evidence", budget, max_staging=1)
    base_writer = evidence_module.BlobWriter

    class BarrierWriter(base_writer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            ready.put("before_staging_admission")
            if not start.wait(10):
                raise RuntimeError("Parent did not release bounded staging probe")

    body = ("owned staging producer " + str(os.getpid())).encode()
    writer = None
    try:
        with mock.patch.object(evidence_module, "BlobWriter", BarrierWriter):
            try:
                writer = store.begin_blob(hashlib.sha256(body).hexdigest(), len(body),
                                          owner="producer_" + str(os.getpid()),
                                          retention_class="intermediate", retain_until_ms=5000)
            except EvidenceStoreError:
                outcomes.put("denied")
            else:
                outcomes.put("admitted")
        # The parent observes both outcomes before a successful writer exits.
        if not finish.wait(10):
            raise RuntimeError("Parent did not finish bounded staging probe")
    finally:
        if writer is not None:
            writer.abort()
        store.close()
        budget.close()


class StorageBoundaryTests(unittest.TestCase):
    def test_staging_limit_is_atomic_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("spawn")
            ready, outcomes = context.Queue(), context.Queue()
            start = context.Event()
            finish = context.Event()
            processes = [context.Process(target=_limited_staging_attempt,
                                          args=(directory, ready, start, finish, outcomes))
                         for _ in range(2)]
            for process in processes:
                process.start()
            try:
                for _ in processes:
                    self.assertEqual(ready.get(timeout=10), "before_staging_admission")
                start.set()
                statuses = sorted(outcomes.get(timeout=10) for _ in processes)
                self.assertEqual(statuses, ["admitted", "denied"])
            finally:
                start.set()
                finish.set()
                for process in processes:
                    process.join(10)
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
                ready.close()
                outcomes.close()
            self.assertTrue(all(process.exitcode == 0 for process in processes))

    def test_other_process_reservation_stays_charged_until_released(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("spawn")
            ready = context.Queue()
            finish = context.Event()
            process = context.Process(target=_hold_reservation, args=(directory, ready, finish))
            process.start()
            budget = None
            try:
                self.assertEqual(ready.get(timeout=10), "held")
                budget = DiskBudget(Path(directory), capacity_bytes=8192,
                                    journal_headroom_bytes=1024, free_bytes=lambda: 1 << 30)
                with self.assertRaises(DiskBudgetError):
                    budget.reserve("new_owner", "spool", 2048)
            finally:
                finish.set()
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                if budget is not None:
                    budget.close()
                ready.close()
            self.assertEqual(process.exitcode, 0)

    def test_reopening_budget_cannot_silently_raise_shared_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            original = DiskBudget(Path(directory), capacity_bytes=8192,
                                  journal_headroom_bytes=1024, free_bytes=lambda: 1 << 30)
            larger = None
            reservation = None
            try:
                try:
                    larger = DiskBudget(Path(directory), capacity_bytes=16384,
                                        journal_headroom_bytes=1024, free_bytes=lambda: 1 << 30)
                except DiskBudgetError:
                    return  # Refusing a conflicting root policy is sufficient.
                with self.assertRaises(DiskBudgetError):
                    reservation = larger.reserve("alternate_owner", "spool", 10000)
            finally:
                if reservation is not None:
                    reservation.close()
                if larger is not None:
                    larger.close()
                original.close()

    def test_failed_digest_publication_cannot_block_valid_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budget = DiskBudget(root / "budget", capacity_bytes=1024 * 1024,
                                journal_headroom_bytes=64 * 1024, free_bytes=lambda: 1 << 30)
            store = EvidenceStore(root / "evidence", budget)
            body = b"valid retry after a bounded failed producer"
            digest = hashlib.sha256(body).hexdigest()
            try:
                bad = store.begin_blob(digest, len(body), owner="failed_producer",
                                       retention_class="intermediate", retain_until_ms=5000)
                bad.write(b"x" * len(body))
                with self.assertRaises(EvidenceStoreError):
                    bad.publish()
                self.assertIsNone(store.lookup(digest))
                store.put_bytes(body, owner="new_producer", retention_class="original",
                                retain_until_ms=5000)
                self.assertEqual(store.read(digest), body)
            finally:
                store.close()
                budget.close()

    def test_tombstone_survives_retention_and_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budget = DiskBudget(root / "budget", capacity_bytes=1024 * 1024,
                                journal_headroom_bytes=64 * 1024, free_bytes=lambda: 1 << 30)
            store = EvidenceStore(root / "evidence", budget)
            body = b"removed original and its delayed producer"
            digest = hashlib.sha256(body).hexdigest()
            try:
                store.put_bytes(body, owner="first_recording", retention_class="original",
                                retain_until_ms=10)
                self.assertEqual(store.apply_retention(now_ms=11), [digest])
                store.apply_retention(now_ms=99999)
                store.close()
                store = EvidenceStore(root / "evidence", budget)
                with self.assertRaises(EvidenceStoreError):
                    store.put_bytes(body, owner="late_recording", retention_class="original",
                                    retain_until_ms=100000)
            finally:
                store.close()
                budget.close()

    def test_publication_rejects_symlinked_digest_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budget = DiskBudget(root / "budget", capacity_bytes=1024 * 1024,
                                journal_headroom_bytes=64 * 1024, free_bytes=lambda: 1 << 30)
            store = EvidenceStore(root / "evidence", budget)
            body = b"a digest must not authorize writes outside the object root"
            digest = hashlib.sha256(body).hexdigest()
            outside = root / "outside"
            outside.mkdir()
            (store.objects / digest[:2]).symlink_to(outside, target_is_directory=True)
            try:
                with self.assertRaises(EvidenceStoreError):
                    store.put_bytes(body, owner="recording", retention_class="original",
                                    retain_until_ms=5000)
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                store.close()
                budget.close()

    def test_failed_tombstone_unlink_keeps_physical_bytes_charged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budget = DiskBudget(root / "budget", capacity_bytes=1024 * 1024,
                                journal_headroom_bytes=64 * 1024, free_bytes=lambda: 1 << 30)
            store = EvidenceStore(root / "evidence", budget)
            body = b"retained bytes are still charged if filesystem deletion fails"
            digest = hashlib.sha256(body).hexdigest()
            try:
                reference = store.put_bytes(body, owner="recording", retention_class="original",
                                            retain_until_ms=5000)
                target_path = store.root / reference.path
                before = budget.snapshot()["chargedBytes"]
                real_unlink = Path.unlink

                def fail_target_unlink(path, *args, **kwargs):
                    if path == target_path:
                        raise PermissionError("Injected owned-file deletion failure")
                    return real_unlink(path, *args, **kwargs)

                with mock.patch.object(Path, "unlink", fail_target_unlink):
                    try:
                        store.tombstone(digest, reason="retention_expired")
                    except EvidenceStoreError:
                        pass
                self.assertTrue(target_path.is_file())
                self.assertGreaterEqual(budget.snapshot()["chargedBytes"], before)
                with self.assertRaises(EvidenceStoreError):
                    store.read(digest)
            finally:
                store.close()
                budget.close()


class ManagedEvidenceStorageTests(unittest.TestCase):
    def test_active_pin_metadata_cannot_grow_beyond_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capacity = 1024 * 1024
            budget = DiskBudget(root / "budget", capacity_bytes=capacity,
                                journal_headroom_bytes=64 * 1024,
                                free_bytes=lambda: 1 << 30)
            store = EvidenceStore(root / "evidence", budget)
            pins = []
            try:
                reference = store.put_bytes(b"pinned", owner="recording",
                                            retention_class="original", retain_until_ms=10)
                for index in range(10000):
                    try:
                        pins.append(store.pin(reference.digest,
                                              "pin_" + str(index).zfill(5) + "x" * 110,
                                              "export"))
                    except EvidenceStoreError:
                        break
                    actual = sum(path.stat().st_size for path in root.rglob("*")
                                 if path.is_file() and not path.is_symlink())
                    self.assertLessEqual(actual, capacity,
                                         "Active pin metadata exceeds storage capacity")
                self.assertGreater(len(pins), 0)
                self.assertEqual(store.apply_retention(now_ms=11), [])
            finally:
                for pin in pins:
                    pin.close()
                store.close()
                budget.close()

    def test_media_admission_leaves_room_for_managed_metadata(self):
        self._check_media_admission(8192, 512)

    def test_small_objects_also_reserve_their_metadata_cost(self):
        self._check_media_admission(2048, 2048)

    def _check_media_admission(self, object_bytes, limit):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capacity = 4 * 1024 * 1024
            budget = store = None

            def check_managed_bytes():
                actual = sum(path.stat().st_size for path in root.rglob("*")
                             if path.is_file() and not path.is_symlink())
                # This is only a lower bound on occupied disk space; counting
                # file lengths avoids depending on filesystem allocation size.
                self.assertLessEqual(actual, capacity,
                                     "Managed media and metadata exceed the configured quota")

            try:
                try:
                    budget = DiskBudget(root / "budget", capacity_bytes=capacity,
                                        journal_headroom_bytes=64 * 1024,
                                        free_bytes=lambda: 1 << 30)
                    store = EvidenceStore(root / "evidence", budget)
                    check_managed_bytes()
                    for index in range(limit):
                        body = index.to_bytes(4, "big") + b"m" * (object_bytes - 4)
                        store.put_bytes(body, owner="recording",
                                        retention_class="intermediate",
                                        retain_until_ms=5000)
                        check_managed_bytes()
                except (DiskBudgetError, EvidenceStoreError):
                    pass  # Safe denial before exceeding capacity is allowed.
                check_managed_bytes()
            finally:
                for resource in (store, budget):
                    if resource is not None:
                        resource.close()


if __name__ == "__main__":
    unittest.main()
