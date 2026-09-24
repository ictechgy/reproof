"""Durable admission, cancellation and uncertain cleanup, with real file locks."""
import multiprocessing
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import reproof.execution.journal as journal
from reproof.execution.journal import RunStore, RunDenied
from reproof.execution.wire import canonical


def competing_admission(root, connection):
    try:
        store = RunStore(root, environment_digest="a" * 64, disk_limit=100)
        with store.admit("second", "b" * 64, disk_bytes=10):
            connection.send("admitted")
    except RunDenied:
        connection.send("denied")
    finally:
        connection.close()


class ExecutionJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "store"
        self.store = RunStore(self.root, environment_digest="a" * 64, disk_limit=100)

    def test_duplicate_operation_and_second_process_cannot_start(self):
        with self.store.admit("one", "b" * 64, disk_bytes=10) as run:
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe(duplex=False)
            process = context.Process(target=competing_admission, args=(str(self.root), child))
            process.start()
            child.close()
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv(), "denied")
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            parent.close()
            run.finish("failed", stopped=True)
        with self.assertRaises(RunDenied):
            with self.store.admit("one", "b" * 64, disk_bytes=10):
                self.fail("duplicate admitted")

    def test_crash_or_missing_stop_keeps_budget_and_quarantine_after_restart(self):
        with self.store.admit("one", "b" * 64, disk_bytes=60):
            pass
        restarted = RunStore(self.root, environment_digest="a" * 64, disk_limit=100)
        record = restarted.status("one")
        self.assertEqual(record["state"], "quarantined")
        self.assertEqual(record["reservedBytes"], 60)
        with self.assertRaises(RunDenied):
            with restarted.admit("two", "c" * 64, disk_bytes=10):
                self.fail("quarantined environment reused")

    def test_cancel_is_durable_and_prevents_success_even_after_clean_shutdown(self):
        with self.store.admit("one", "b" * 64, disk_bytes=10) as run:
            self.store.cancel("one", "b" * 64)
            self.assertTrue(run.cancelled())
            run.finish("succeeded", stopped=True)
        restarted = RunStore(self.root, environment_digest="a" * 64, disk_limit=100)
        self.assertEqual(restarted.status("one")["state"], "cancelled")
        self.assertEqual(restarted.status("one")["reservedBytes"], 0)

    def test_cleanup_unknown_file_and_unconfirmed_stop_remain_quarantined(self):
        with self.store.admit("one", "b" * 64, disk_bytes=10) as run:
            (run.directory / "unexpected").write_bytes(b"owned test")
            run.finish("succeeded", stopped=True)
        self.assertEqual(self.store.status("one")["state"], "quarantined")
        self.assertTrue((self.root / "runs/one/unexpected").exists())
        self.assertEqual(self.store.status("one")["reservedBytes"], 10)

    def test_disk_exhaustion_and_wrong_environment_or_cancel_binding_are_denied(self):
        with self.assertRaises(RunDenied):
            with self.store.admit("one", "b" * 64, disk_bytes=101):
                self.fail("over budget")
        with self.store.admit("one", "b" * 64, disk_bytes=10) as run:
            with self.assertRaises(RunDenied):
                self.store.cancel("one", "c" * 64)
            self.assertFalse(run.cancelled())
            run.finish("failed", stopped=True)
        with self.assertRaises(RunDenied):
            RunStore(self.root, environment_digest="c" * 64, disk_limit=100)

    def test_recovery_requires_a_run_bound_native_termination_record(self):
        from reproof.contracts import digest
        machine_digest = digest({'testMachine': str(self.root)})
        with self.store.machine_lease(machine_digest):
            pass
        with self.store.admit("one", "b" * 64, disk_bytes=10):
            pass
        with self.store.machine_lease(machine_digest), self.assertRaises(RunDenied):
            self.store.reconcile("one", "b" * 64)
        receipt = self.root / "runs/one/termination.json"
        proof = {"schemaVersion": 1, "operationId": "other", "requestDigest": "b" * 64, "state": "stopped"}
        receipt.write_text(json.dumps(proof))
        with self.store.machine_lease(machine_digest), self.assertRaises(RunDenied):
            self.store.reconcile("one", "b" * 64)
        proof["operationId"] = "one"
        receipt.write_text(json.dumps(proof))
        with self.store.machine_lease(machine_digest):
            result = self.store.reconcile("one", "b" * 64)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reservedBytes"], 0)
        self.assertFalse((self.root / "runs/one").exists())

    def test_terminal_records_archive_at_limit_and_identities_never_repeat(self):
        with patch.object(journal, "MAX_RUNS", 3):
            for index in range(3):
                with self.store.admit(f"run-{index}", format(index + 1, "064x"),
                                      disk_bytes=10) as run:
                    run.finish("succeeded", stopped=True)
            with self.store.admit("run-3", format(4, "064x"), disk_bytes=10) as run:
                run.finish("failed", stopped=True)
            for index in range(3):
                operation_id = f"run-{index}"
                self.assertTrue((self.root / "archive" / operation_id).is_file())
                self.assertFalse((self.root / "runs" / operation_id).exists())
                record = self.store.status(operation_id)
                self.assertEqual(record["state"], "succeeded")
                self.assertEqual(record["requestDigest"], format(index + 1, "064x"))
                self.assertEqual(record["reservedBytes"], 0)
            self.assertEqual(self.store.status("run-3")["state"], "failed")
            with self.assertRaises(RunDenied):
                with self.store.admit("run-0", format(9, "064x"), disk_bytes=10):
                    self.fail("archived identity reused")
            restarted = RunStore(self.root, environment_digest="a" * 64, disk_limit=100)
            self.assertEqual(restarted.status("run-0")["state"], "succeeded")
            with self.assertRaises(RunDenied):
                with restarted.admit("run-0", format(9, "064x"), disk_bytes=10):
                    self.fail("archived identity reused after restart")
            with self.assertRaises(RunDenied):
                restarted.status("never-seen")

    def test_cancelled_terminal_archives_and_stays_cancelled(self):
        with patch.object(journal, "MAX_RUNS", 1):
            with self.store.admit("run-a", format(1, "064x"), disk_bytes=10) as run:
                self.store.cancel("run-a", format(1, "064x"))
                run.finish("succeeded", stopped=True)
            self.assertEqual(self.store.status("run-a")["state"], "cancelled")
            with self.store.admit("run-b", format(2, "064x"), disk_bytes=10) as run:
                run.finish("succeeded", stopped=True)
            record = self.store.status("run-a")
            self.assertEqual(record["state"], "cancelled")
            self.assertTrue((self.root / "archive" / "run-a").is_file())
            self.assertTrue((self.root / "cancellations" / "run-a").exists())
            with self.assertRaises(RunDenied):
                with self.store.admit("run-a", format(3, "064x"), disk_bytes=10):
                    self.fail("archived identity reused")

    def test_archive_tamper_or_removal_is_refused(self):
        with patch.object(journal, "MAX_RUNS", 1):
            with self.store.admit("run-a", format(1, "064x"), disk_bytes=10) as run:
                run.finish("succeeded", stopped=True)
            with self.store.admit("run-b", format(2, "064x"), disk_bytes=10) as run:
                run.finish("succeeded", stopped=True)
        path = self.root / "archive" / "run-a"
        original = path.read_bytes()
        path.write_bytes(b'{"schemaVersion":1,"operationId":"run-a"}')
        with self.assertRaises(RunDenied):
            self.store.status("run-a")
        forged = {"schemaVersion": 1, "operationId": "run-a",
                  "requestDigest": format(1, "064x"), "state": "failed", "reservedBytes": 0}
        path.write_bytes(canonical(forged))
        # Structurally valid but not in the recorded set: the next merge refuses.
        with patch.object(journal, "MAX_RUNS", 1):
            with self.assertRaises(RunDenied):
                with self.store.admit("run-c", format(3, "064x"), disk_bytes=10):
                    self.fail("admission merged a forged archive record")
        path.write_bytes(original)
        path.unlink()
        with self.assertRaises(RunDenied):
            self.store.status("run-b")
        path.write_bytes(original)
        self.assertEqual(self.store.status("run-b")["state"], "succeeded")

    def test_foreign_or_mismatching_archive_entries_are_refused(self):
        with patch.object(journal, "MAX_RUNS", 2):
            with self.store.admit("run-a", format(1, "064x"), disk_bytes=10) as run:
                run.finish("succeeded", stopped=True)
            with self.store.admit("run-b", format(2, "064x"), disk_bytes=10) as run:
                run.finish("failed", stopped=True)
            foreign = {"schemaVersion": 1, "operationId": "ghost",
                       "requestDigest": format(9, "064x"), "state": "succeeded",
                       "reservedBytes": 0}
            (self.root / "archive" / "ghost").write_bytes(canonical(foreign))
            with self.assertRaises(RunDenied):
                self.store.status("run-a")
            (self.root / "archive" / "ghost").unlink()
            mismatched = {"schemaVersion": 1, "operationId": "run-a",
                          "requestDigest": format(1, "064x"), "state": "failed",
                          "reservedBytes": 0}
            orphan_path = self.root / "archive" / "run-a"
            orphan_path.write_bytes(canonical(mismatched))
            os.chmod(orphan_path, 0o600)
            with self.assertRaises(RunDenied):
                self.store.status("run-a")
            with self.assertRaises(RunDenied):
                with self.store.admit("run-c", format(3, "064x"), disk_bytes=10):
                    self.fail("admission merged a contradicting archive record")

    def test_committed_orphan_is_adopted_and_run_directory_trace_blocks_archival(self):
        with patch.object(journal, "MAX_RUNS", 2):
            for index, operation_id in enumerate(("run-a", "run-b")):
                with self.store.admit(operation_id, format(index + 1, "064x"),
                                      disk_bytes=10) as run:
                    run.finish("succeeded", stopped=True)
            orphan = {"schemaVersion": 1, "operationId": "run-a",
                      "requestDigest": format(1, "064x"), "state": "succeeded",
                      "reservedBytes": 0}
            orphan_path = self.root / "archive" / "run-a"
            orphan_path.write_bytes(canonical(orphan))
            os.chmod(orphan_path, 0o600)
            self.assertEqual(self.store.status("run-a")["state"], "succeeded")
            (self.root / "runs" / "run-b").mkdir()
            with self.store.admit("run-c", format(3, "064x"), disk_bytes=10) as run:
                run.finish("succeeded", stopped=True)
            self.assertEqual(self.store.status("run-a")["state"], "succeeded")
            self.assertEqual(self.store.status("run-b")["state"], "succeeded")
            self.assertFalse((self.root / "archive" / "run-b").exists())
            self.assertEqual(self.store._load()["archive"]["count"], 1)

    def test_preflight_reports_archivable_capacity_without_writing(self):
        with patch.object(journal, "MAX_RUNS", 1):
            with self.store.admit("run-a", format(1, "064x"), disk_bytes=10) as run:
                run.finish("succeeded", stopped=True)
            before = (self.root / "state.json").read_bytes()
            self.store.require_available()
            self.assertEqual((self.root / "state.json").read_bytes(), before)
            (self.root / "runs" / "run-a").mkdir()
            with self.assertRaises(RunDenied):
                self.store.require_available()
            self.assertEqual((self.root / "state.json").read_bytes(), before)
