"""Independent lifecycle probes against the active G6 transfer store API."""
from __future__ import annotations
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from reproof.live.artifact_transfer import ArtifactTransferStore
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore
from reproof.live.model import LiveError

HOST = ('parent_mac', 1, 'parent_incarnation')


class TransferLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='g6-parent-transfer-')
        root = Path(self.temporary.name)
        self.now = 1000
        self.budget = DiskBudget(root / 'budget', capacity_bytes=16 * 1024 * 1024,
                                 journal_headroom_bytes=512 * 1024)
        self.evidence = EvidenceStore(root / 'evidence', self.budget,
                                      max_object_bytes=1024 * 1024)
        self.transfer = ArtifactTransferStore(
            root / 'transfer', self.budget, self.evidence,
            object_quota_bytes=1024 * 1024, project_quota_bytes=4 * 1024 * 1024,
            host_quota_bytes=4 * 1024 * 1024, clock_ms=lambda: self.now)

    def tearDown(self):
        self.transfer.close()
        self.evidence.close()
        self.budget.close()
        self.temporary.cleanup()

    def upload(self, *, retain_until=2000):
        body = b'owned lifecycle probe bytes'
        digest = hashlib.sha256(body).hexdigest()
        result = self.transfer.allocate(
            project_id='parent_project', host_identity=HOST, kind='manifest',
            size=len(body), digest=digest, metadata={'format': 'json'},
            retention_class='original', retain_until_ms=retain_until,
            authorizer=lambda _: True)
        self.transfer.put_chunk(result['objectId'], result['uploadGeneration'],
                                0, body, digest, host_identity=HOST,
                                authorizer=lambda _: True)
        return result

    def finalize(self, upload):
        return self.transfer.finalize(
            upload['objectId'], upload['uploadGeneration'],
            host_identity=HOST, authorizer=lambda _: True)

    def tombstone(self, upload):
        return self.transfer.tombstone(
            upload['objectId'], host_identity=HOST,
            authorizer=lambda _: True, reason='parent_delete')

    def test_tombstone_during_publication_leaves_no_unassociated_published_blob(self):
        upload = self.upload()
        entered = threading.Event()
        proceed = threading.Event()
        delete_done = threading.Event()
        result = {}
        original = self.evidence._publish_writer

        def held(writer):
            entered.set()
            if not proceed.wait(4):
                raise RuntimeError('owned publication probe timed out')
            return original(writer)

        def finalize():
            try:
                result['finalize'] = self.finalize(upload)
            except Exception as exc:
                result['finalizeError'] = type(exc).__name__

        def tombstone():
            try:
                result['tombstone'] = self.tombstone(upload)
            except Exception as exc:
                result['tombstoneError'] = type(exc).__name__
            finally:
                delete_done.set()

        finalizer = threading.Thread(target=finalize, daemon=True)
        deleter = threading.Thread(target=tombstone, daemon=True)
        with mock.patch.object(self.evidence, '_publish_writer', held):
            try:
                finalizer.start()
                self.assertTrue(entered.wait(3), 'Publication did not reach the held boundary')
                deleter.start()
                # A correct implementation may serialize deletion behind the
                # active publisher or reject it as busy, so release both paths.
                delete_done.wait(.2)
            finally:
                proceed.set()
                finalizer.join(5)
                if deleter.ident is not None:
                    deleter.join(5)
        self.assertFalse(finalizer.is_alive() or deleter.is_alive(),
                         'An owned lifecycle operation did not terminate')
        # Retry also checks that a denied/busy deletion remains actionable once
        # the producer is finished, and that an earlier success is idempotent.
        self.tombstone(upload)
        self.assertEqual(self.transfer.status(upload['objectId'], host_identity=HOST,
                                               authorizer=lambda _: True)['state'], 'tombstoned')
        self.assertIsNone(self.evidence.lookup(upload['digest']),
                          'A tombstoned transfer left a published blob outside its association')

    def test_deletion_can_finish_after_an_active_download_pin_closes(self):
        upload = self.upload()
        self.finalize(upload)
        reading = self.transfer.open_read(upload['objectId'], host_identity=HOST,
                                          authorizer=lambda _: True)
        try:
            try:
                self.tombstone(upload)
            except LiveError:
                pass
            self.assertIsNotNone(self.evidence.lookup(upload['digest']),
                                  'Active download bytes were deleted')
        finally:
            reading.close()
        self.tombstone(upload)
        self.assertIsNone(self.evidence.lookup(upload['digest']),
                          'Deletion forgot its pending cleanup when the active pin closed')

    def test_expired_association_cannot_open_a_new_download(self):
        upload = self.upload(retain_until=1001)
        self.finalize(upload)
        self.now = 1002
        reading = None
        try:
            with self.assertRaises(LiveError):
                reading = self.transfer.open_read(upload['objectId'], host_identity=HOST,
                                                  authorizer=lambda _: True)
        finally:
            if reading is not None:
                reading.close()

    def test_retention_removes_partial_upload_bytes(self):
        upload = self.upload(retain_until=1001)
        self.now = 1002
        self.assertEqual(self.transfer.apply_retention(), [upload['objectId']])
        self.assertFalse(self.transfer._stage_path(upload['objectId']).exists())
        status = self.transfer.status(upload['objectId'], host_identity=HOST)
        self.assertEqual(status['state'], 'tombstoned')
        self.assertEqual(status['metadata'], {})

    def test_retention_retries_after_an_existing_consumer_pin_closes(self):
        upload = self.upload(retain_until=1001)
        self.finalize(upload)
        reading = self.transfer.open_read(upload['objectId'], host_identity=HOST)
        self.now = 1002
        try:
            self.assertEqual(self.transfer.apply_retention(), [])
            self.assertIsNotNone(self.evidence.lookup(upload['digest']))
        finally:
            reading.close()
        self.assertEqual(self.transfer.apply_retention(), [upload['objectId']])
        self.assertIsNone(self.evidence.lookup(upload['digest']))

    def test_expiry_during_publication_keeps_a_cleanup_obligation(self):
        upload = self.upload(retain_until=1001)
        entered = threading.Event(); proceed = threading.Event()
        result = {}
        original = self.evidence._publish_writer

        def held(writer):
            entered.set()
            self.assertTrue(proceed.wait(4))
            return original(writer)

        def finalize():
            try:
                result['value'] = self.finalize(upload)
            except LiveError as exc:
                result['errorCode'] = exc.code

        finalizer = threading.Thread(target=finalize)
        with mock.patch.object(self.evidence, '_publish_writer', held):
            try:
                finalizer.start()
                self.assertTrue(entered.wait(3))
                self.now = 1002
            finally:
                proceed.set(); finalizer.join(5)
        self.assertFalse(finalizer.is_alive())
        self.assertEqual(result.get('errorCode'), 'retention_expired')
        self.assertEqual(self.transfer.apply_retention(), [upload['objectId']])
        self.assertIsNone(self.evidence.lookup(upload['digest']))
