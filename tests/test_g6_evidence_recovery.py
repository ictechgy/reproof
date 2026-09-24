"""Probe whether failed duplicate-blob deletion remains recoverable and retained."""
from __future__ import annotations
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore


def run_probe():
    with tempfile.TemporaryDirectory(prefix='g6-parent-dedup-') as directory:
        root = Path(directory)
        budget = DiskBudget(root / 'budget', capacity_bytes=16 * 1024 * 1024,
                            journal_headroom_bytes=64 * 1024)
        store = EvidenceStore(root / 'evidence', budget)
        try:
            body = b'synthetic duplicate upload pending deletion'
            reference = store.put_bytes(body, owner='first_upload',
                                        retention_class='original', retain_until_ms=10)
            first_charge = budget.snapshot()['chargedBytes']
            writer = store.begin_blob(reference.digest, len(body), owner='second_upload',
                                      retention_class='intermediate', retain_until_ms=10)
            writer.write(body)
            staged_path = writer.path
            real_unlink = Path.unlink

            def fail_duplicate(path, *args, **kwargs):
                if path == staged_path:
                    raise PermissionError('owned duplicate unlink fault')
                return real_unlink(path, *args, **kwargs)

            failure = None
            with mock.patch.object(Path, 'unlink', fail_duplicate):
                try:
                    writer.publish()
                except Exception as exc:
                    failure = type(exc).__name__
            tracked_after_failure = store._connection.execute(
                'SELECT COUNT(*) FROM staging WHERE staging_id = ?',
                (writer.staging_id,)).fetchone()[0]
            retained_after_failure = staged_path.exists()
            charge_after_failure = budget.snapshot()['chargedBytes']
            store.close()
            store = EvidenceStore(root / 'evidence', budget)
            abandoned = store.abandon_owner('second_upload')
            store.apply_retention(now_ms=11)
            retained_after_recovery = staged_path.exists()
            final_charge = budget.snapshot()['chargedBytes']
            readable = store.lookup(reference.digest) is not None
            return {
                'scope': 'owned temporary EvidenceStore; one injected unlink failure',
                'publicationExceptionType': failure,
                'duplicateBytesRemainAfterFault': retained_after_failure,
                'duplicateStagingRowsAfterFault': tracked_after_failure,
                'firstPublishedChargeBytes': first_charge,
                'chargeBytesAfterFault': charge_after_failure,
                'abandonedStagingCountAfterReopen': abandoned,
                'duplicateBytesRemainAfterReopenCleanupAndRetention': retained_after_recovery,
                'publishedObjectRemainsReadableAfterRetention': readable,
                'chargeBytesAfterReopenCleanupAndRetention': final_charge,
                'passed': (failure is not None and retained_after_failure
                           and tracked_after_failure == 1 and charge_after_failure > first_charge
                           and not retained_after_recovery and not readable and final_charge == 0),
            }
        finally:
            store.close()
            budget.close()



class EvidenceRecoveryTests(unittest.TestCase):
    def test_failed_duplicate_unlink_remains_tracked_until_recovery(self):
        result = run_probe()
        self.assertTrue(result['passed'], result)
