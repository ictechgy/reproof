"""Durable cancellation, restart quarantine, request identity and output reservation."""
from pathlib import Path
import tempfile
import sqlite3
import os
import stat
import unittest
from unittest import mock

from reproloop.execution.artifacts import BlobSet


class RepairJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'repairs'

    def journal(self, **kwargs):
        from reproloop.repair_journal import RepairJournal
        journal = RepairJournal(self.root, disk_limit=1024 * 1024, **kwargs)
        self.addCleanup(journal.close)
        return journal

    def create(self, journal, **changes):
        values = dict(project_id='checkout', owner_id='owner', issue_id='issue_1', request_id='request_1',
                      request_digest='1' * 64, reservation_bytes=4096)
        values.update(changes)
        return journal.create(**values)

    def test_cancel_is_durable_and_late_success_cannot_win(self):
        from reproloop.repair_journal import RepairJournalError
        journal = self.journal(); job = self.create(journal)
        token = journal.cancellation(job['id'])
        journal.update(job['id'], phase='building', status='running')
        journal.cancel(job['id'])
        self.assertTrue(token.is_set())
        with self.assertRaises(RepairJournalError): journal.finish(job['id'], 'verified')
        journal.close(); reopened = self.journal()
        self.assertTrue(reopened.cancellation(job['id']).is_set())
        self.assertEqual(reopened.get(job['id'])['status'], 'quarantined')

    def test_idempotency_conflicts_and_process_restart_are_retained(self):
        from reproloop.repair_journal import RepairJournalError
        journal = self.journal(); job = self.create(journal)
        self.assertEqual(self.create(journal)['id'], job['id'])
        with self.assertRaises(RepairJournalError): self.create(journal, request_digest='2' * 64)
        journal.update(job['id'], phase='proposing', status='running')
        journal.close(); reopened = self.journal()
        self.assertEqual(reopened.get(job['id'])['status'], 'interrupted')
        self.assertEqual(self.create(reopened)['id'], job['id'])
        with self.assertRaises(RepairJournalError): reopened.update(job['id'], status='running')

    def test_only_one_writer_and_bounded_private_outputs(self):
        from reproloop.repair_journal import RepairJournalError
        journal = self.journal(); job = self.create(journal, reservation_bytes=4)
        with self.assertRaises(RepairJournalError): self.journal()
        journal.write_blobs(job['id'], 'candidate', BlobSet((('app.swift', b'four'),)))
        self.assertEqual((self.root / job['id'] / 'candidate' / 'app.swift').read_bytes(), b'four')
        self.assertEqual((self.root / job['id']).stat().st_mode & 0o777, 0o700)
        with self.assertRaises(RepairJournalError):
            journal.write_blobs(job['id'], 'extra', BlobSet((('another.swift', b'x'),)))
        self.assertFalse((self.root / job['id'] / 'extra').exists())

    def test_terminal_states_and_cancelled_output_remain_closed(self):
        from reproloop.repair_journal import RepairJournalError
        journal = self.journal(); job = self.create(journal)
        journal.cancel(job['id']); journal.finish(job['id'], 'cancelled')
        with self.assertRaises(RepairJournalError):
            journal.write_blobs(job['id'], 'candidate', BlobSet((('app.swift', b'x'),)))
        with self.assertRaises(RepairJournalError): journal.finish(job['id'], 'verified')
        self.assertEqual(journal.get(job['id'])['status'], 'cancelled')

    def test_expiry_cancels_active_work_then_deletes_only_its_declared_outputs(self):
        journal = self.journal(); job = self.create(journal, retain_until_ms=10)
        journal.write_blobs(job['id'], 'candidate', BlobSet((('src/app.swift', b'private product'),)))
        journal.apply_retention(now_ms=11)
        self.assertTrue(journal.get(job['id'])['cancelRequested'])
        self.assertTrue((self.root / job['id'] / 'candidate' / 'src/app.swift').exists())
        journal.finish(job['id'], 'cancelled')
        journal.apply_retention(now_ms=11)
        self.assertTrue(journal.get(job['id'])['outputsExpired'])
        self.assertEqual(journal.get(job['id'])['outputs']['candidate']['status'], 'expired')
        self.assertFalse((self.root / job['id'] / 'candidate').exists())

    def test_expiry_refuses_unknown_files_and_keeps_failed_deletion_charged(self):
        journal = self.journal(); job = self.create(journal, retain_until_ms=10)
        journal.write_blobs(job['id'], 'candidate', BlobSet((('app.swift', b'private'),)))
        unknown = self.root / job['id'] / 'candidate' / 'unexpected'
        unknown.write_bytes(b'concurrent file')
        journal.finish(job['id'], 'proposal-ready')
        journal.apply_retention(now_ms=11)
        result = journal.get(job['id'])
        self.assertTrue(result['outputsExpired'])
        self.assertEqual(result['usedBytes'], 7)
        self.assertEqual(result['outputs']['candidate']['status'], 'cleanup-blocked')
        self.assertEqual(result['reservedBytes'], 4096)
        self.assertEqual(unknown.read_bytes(), b'concurrent file')

    def test_confirmed_expiry_releases_data_capacity_without_reviving_old_requests(self):
        from reproloop.repair_journal import RepairJournalError
        journal = self.journal()
        job = self.create(journal, reservation_bytes=1024*1024, retain_until_ms=10)
        journal.write_blobs(job['id'], 'candidate', BlobSet((('app.swift', b'owned source'),)))
        journal.finish(job['id'], 'proposal-ready')
        with self.assertRaises(RepairJournalError): self.create(journal, request_id='new_request')
        journal.apply_retention(now_ms=11)
        self.assertEqual(journal.get(job['id'])['reservedBytes'], 0)
        created = self.create(journal, request_id='new_request')
        self.assertEqual(created['status'], 'created')
        original = self.create(journal)
        self.assertEqual(original['id'], job['id'])
        self.assertEqual(original['status'], 'proposal-ready')
        self.assertTrue(original['outputsExpired'])

    def test_failed_job_insert_does_not_accumulate_uncharged_directories(self):
        from reproloop.repair_journal import RepairJournalError
        journal = self.journal()
        journal._db.set_authorizer(lambda operation, table, *_:
            sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_INSERT and table == 'jobs' else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(RepairJournalError): self.create(journal)
        finally: journal._db.set_authorizer(None)
        self.assertEqual([path.name for path in self.root.iterdir() if path.is_dir()], [])
        self.assertEqual(self.create(journal)['status'], 'created')

    def test_output_fsync_failure_cannot_publish_a_complete_blob(self):
        from reproloop.repair_journal import RepairJournalError
        journal = self.journal(); job = self.create(journal)
        real_fsync = os.fsync
        def fail_file(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode): raise OSError('owned fsync failure')
            return real_fsync(fd)
        with mock.patch('reproloop.repair_journal.os.fsync', side_effect=fail_file):
            with self.assertRaises(RepairJournalError):
                journal.write_blobs(job['id'], 'candidate', BlobSet((('app.swift', b'owned source'),)))
        self.assertEqual(journal.get(job['id'])['status'], 'quarantined')
        self.assertEqual(journal.get(job['id'])['outputs']['candidate']['status'], 'reserved')


if __name__ == '__main__': unittest.main()
