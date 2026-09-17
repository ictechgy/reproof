import hashlib
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from reproloop.live import artifact_transfer
from reproloop.live.artifact_transfer import ArtifactTransferStore
from reproloop.live.disk_budget import DiskBudget
from reproloop.live.evidence_store import EvidenceStore
from reproloop.live.model import LiveError
from tests.test_g6_transfer_process import HOST, open_stores, close_stores


def crash_before_association(root):
    opened = {}
    open_stores(root, opened)
    transfer = opened["transfer"]
    original = artifact_transfer._fsync_directory

    def crash(path):
        original(path)
        if path == transfer.staging:
            os._exit(23)

    with patch.object(artifact_transfer, "_fsync_directory", side_effect=crash):
        transfer.allocate(project_id="parent_project", host_identity=HOST, kind="manifest",
                          size=1024, digest=hashlib.sha256(b"x" * 1024).hexdigest(),
                          metadata={}, retention_class="original",
                          retain_until_ms=int(time.time() * 1000) + 60000)


class AllocationCrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        process = multiprocessing.get_context("spawn").Process(
            target=crash_before_association, args=(self.root,))
        process.start()
        process.join(10)
        if process.is_alive():
            process.kill(); process.join(3)
        self.assertEqual(process.exitcode, 23)
        self.assertEqual(len(list((self.root / "transfer" / "staging").iterdir())), 1)

    def test_restart_reclaims_only_this_store_unjournaled_allocation(self):
        budget = DiskBudget(self.root / "budget", capacity_bytes=16 * 1024 * 1024,
                            journal_headroom_bytes=512 * 1024)
        self.addCleanup(budget.close)
        other = budget.reserve("another_transfer_store", "transfer", 8192)
        self.addCleanup(other.close)
        evidence = EvidenceStore(self.root / "evidence", budget)
        self.addCleanup(evidence.close)
        transfer = ArtifactTransferStore(
            self.root / "transfer", budget, evidence, object_quota_bytes=1024 * 1024,
            project_quota_bytes=4 * 1024 * 1024, host_quota_bytes=4 * 1024 * 1024)
        self.addCleanup(transfer.close)
        self.assertEqual(list(transfer.staging.iterdir()), [])
        self.assertEqual(budget.snapshot()["chargedBytes"], 8192)

    def test_failed_orphan_unlink_retains_charge_and_prevents_healthy_startup(self):
        opened = {}
        original = Path.unlink

        def fail(path, *args, **kwargs):
            if path.parent == self.root / "transfer" / "staging":
                raise OSError("owned injected unlink failure")
            return original(path, *args, **kwargs)

        try:
            with patch.object(Path, "unlink", fail), self.assertRaises(LiveError):
                open_stores(self.root, opened)
            self.assertGreater(opened["budget"].snapshot()["chargedBytes"], 0)
            self.assertEqual(len(list((self.root / "transfer" / "staging").iterdir())), 1)
        finally:
            close_stores(opened)
