"""Bounded duplicate-upload stress: include retained association metadata in quota."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from reproof.live.artifact_transfer import ArtifactTransferStore
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore
from reproof.live.model import LiveError


def probe():
    capacity = 1024 * 1024
    with tempfile.TemporaryDirectory(prefix='g6-parent-budget-') as directory:
        root = Path(directory)
        budget = DiskBudget(root / 'budget', capacity_bytes=capacity,
                            journal_headroom_bytes=64 * 1024)
        evidence = EvidenceStore(root / 'evidence', budget)
        transfer = ArtifactTransferStore(
            root / 'transfer', budget, evidence, object_quota_bytes=32,
            project_quota_bytes=64 * 1024, host_quota_bytes=64 * 1024)
        try:
            body = b'{}'
            digest = hashlib.sha256(body).hexdigest()
            host = ('parent_mac', 1, 'parent_incarnation')
            metadata = {f'label_{index}': 'x' * 256 for index in range(16)}
            published = 0
            rejection = None
            peak_bytes = 0
            for _ in range(320):
                try:
                    upload = transfer.allocate(
                        project_id='parent_project', host_identity=host,
                        kind='manifest', size=len(body), digest=digest,
                        metadata=metadata, retention_class='original',
                        retain_until_ms=int(time.time() * 1000) + 60000,
                        authorizer=lambda _: True)
                    transfer.put_chunk(upload['objectId'], upload['uploadGeneration'],
                                       0, body, digest, host_identity=host,
                                       authorizer=lambda _: True)
                    transfer.finalize(upload['objectId'], upload['uploadGeneration'],
                                      host_identity=host, authorizer=lambda _: True)
                    published += 1
                except LiveError as exc:
                    rejection = exc.code
                    break
                # Only this owned, bounded temporary tree is measured. No user
                # paths, private application data, or existing files are read.
                retained = sum(path.stat().st_size for path in root.rglob('*') if path.is_file())
                peak_bytes = max(peak_bytes, retained)
                if retained > 2 * capacity:
                    break
            totals = budget.snapshot()
            return {
                'scope': 'owned temporary stores; at most 320 identical two-byte uploads',
                'capacityBytes': capacity, 'publishedAssociations': published,
                'distinctContentDigests': 1 if published else 0,
                'perUploadMetadataBytes': len(json.dumps(metadata, separators=(',', ':')).encode()),
                'rejectionCode': rejection, 'retainedFileBytes': peak_bytes,
                'budgetSnapshot': totals,
                'passed': published > 0 and peak_bytes <= capacity,
            }
        finally:
            transfer.close()
            evidence.close()
            budget.close()



class TransferMetadataBudgetTests(unittest.TestCase):
    def test_duplicate_payloads_cannot_bypass_persistent_metadata_budget(self):
        result = probe()
        self.assertTrue(result['passed'], result)
