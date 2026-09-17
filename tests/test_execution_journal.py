"""Durable admission, cancellation and uncertain cleanup, with real file locks."""
import multiprocessing
import json
from pathlib import Path
import tempfile
import unittest

from reproloop.execution.journal import RunStore, RunDenied


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
        from reproloop.contracts import digest
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
