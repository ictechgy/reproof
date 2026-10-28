"""Compact snapshot journal reservations and conservative residue retention."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from reproof.execution.artifacts import BlobSet
from reproof.repair_journal import RepairJournal, RepairJournalError
from reproof.source_snapshot import capture, derive_snapshot


class SnapshotRepairJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.journal = RepairJournal(self.root / 'journal', disk_limit=1024 * 1024)
        self.addCleanup(self.journal.close)
        self.job = self.journal.create(project_id='project', owner_id='owner', issue_id='issue',
            request_id='request', request_digest='1' * 64, reservation_bytes=4096)
        self.identifier = self.job['id']
        self.fixture = self.root / 'fixture'
        self.fixture.mkdir()
        (self.fixture / 'file.txt').write_bytes(b'preserved')
        measured = capture(self.fixture, ['file.txt'], self.root / 'fixture-snapshot')
        self.reference = dict({key: measured[key] for key in ('digest', 'files', 'bytes')},
                              schemaVersion=1, kind='reproof-local-source-snapshot')

    def populate(self, output):
        capture(self.fixture, ['file.txt'], output.path, output_parent_identity=output.parent_identity)

    def publish(self):
        with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
            self.populate(output)
            output.complete(self.reference)
        return output.path

    def test_compact_metadata_reserves_before_mutation_and_retains_ceiling(self):
        with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
            document = self.journal.get(self.identifier)
            self.assertEqual(document['usedBytes'], 2048)
            self.assertEqual(document['outputs']['candidate']['status'], 'reserved')
            self.assertFalse(output.path.exists())
            self.assertEqual(output.parent_identity, (os.stat(output.path.parent).st_dev, os.stat(output.path.parent).st_ino))
            self.populate(output)
            compact = dict(self.reference, files=9001)
            with patch('reproof.source_snapshot.inspect_snapshot'):
                output.complete(compact)
        document = self.journal.get(self.identifier)
        self.assertEqual(document['outputs']['candidate']['reference'], compact)
        self.assertNotIn('paths', document['outputs']['candidate'])
        self.assertLess(len(json.dumps(document)), 2048)
        self.assertEqual(document['usedBytes'], 2048)
        with self.assertRaises(RepairJournalError):
            output.complete(self.reference)

    def test_quota_invalid_and_duplicate_reject_before_mutation(self):
        for reservation in (0, True, 4097, 512 * 1024 * 1024 + 1):
            with self.assertRaises(RepairJournalError):
                with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=reservation):
                    self.fail('must reject before yield')
        self.assertEqual(self.journal.get(self.identifier)['usedBytes'], 0)
        self.publish()
        with self.assertRaises(RepairJournalError):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=1):
                self.fail('duplicate')

    def test_cancel_during_derivation_preserves_output_and_reservation(self):
        with self.assertRaises(RepairJournalError):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
                output.path.mkdir()
                (output.path / 'foreign.txt').write_bytes(b'foreign')
                self.journal.cancel(self.identifier)
                output.complete(self.reference)
        document = self.journal.get(self.identifier)
        self.assertEqual(document['usedBytes'], 2048)
        self.assertTrue(document['outputs']['candidate']['cleanupBlocked'])
        self.assertEqual((output.path / 'foreign.txt').read_bytes(), b'foreign')

    def test_exception_and_missing_completion_keep_reservations(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048):
                raise KeyboardInterrupt()
        self.assertEqual(self.journal.get(self.identifier)['usedBytes'], 2048)
        with self.assertRaises(RepairJournalError):
            with self.journal.begin_snapshot_output(self.identifier, 'other', reservation_bytes=512):
                pass
        self.assertEqual(self.journal.get(self.identifier)['usedBytes'], 2560)

    def test_restart_never_discards_snapshot_or_sibling(self):
        with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
            self.populate(output)
            output.write_sidecar('proposal', b'{}')
            output.complete(self.reference)
        path = output.path
        self.journal.close()
        self.journal = RepairJournal(self.root / 'journal', disk_limit=1024 * 1024)
        self.addCleanup(self.journal.close)
        self.assertEqual(self.journal.get(self.identifier)['status'], 'interrupted')
        with patch.object(self.journal, '_discard_output', side_effect=AssertionError('legacy deletion')):
            self.journal.expire(self.identifier, now_ms=1)
        document = self.journal.get(self.identifier)
        self.assertEqual(document['reservedBytes'], 4096)
        self.assertEqual(document['usedBytes'], 2050)
        self.assertTrue(all(out['cleanupBlocked'] for out in document['outputs'].values()))
        self.assertEqual((path / 'data/file.txt').read_bytes(), b'preserved')
        self.assertEqual((path.parent / 'proposal/proposal.json').read_bytes(), b'{}')

    def test_finished_snapshot_retention_preserves_replaced_foreign_parent(self):
        path = self.publish()
        self.journal.finish(self.identifier, 'proposal-ready')
        path.parent.rename(path.parent.with_name('old-job'))
        path.parent.mkdir()
        (path.parent / 'foreign.txt').write_bytes(b'foreign')
        self.journal.expire(self.identifier, now_ms=1)
        self.assertEqual((path.parent / 'foreign.txt').read_bytes(), b'foreign')
        self.assertEqual(self.journal.get(self.identifier)['reservedBytes'], 4096)

    def test_existing_output_is_not_registered_or_changed(self):
        path = self.journal.root / self.identifier / 'candidate'
        path.mkdir(); (path / 'foreign.txt').write_bytes(b'foreign')
        with self.assertRaises(RepairJournalError):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048):
                self.fail('existing')
        self.assertEqual(self.journal.get(self.identifier)['usedBytes'], 0)
        self.assertEqual((path / 'foreign.txt').read_bytes(), b'foreign')

    def test_job_parent_swap_rejects_completion_and_snapshot_writer(self):
        with self.assertRaises(RepairJournalError):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
                parent = output.path.parent
                parent.rename(parent.with_name('old-job')); parent.mkdir()
                output.path.mkdir(); (output.path / 'foreign.txt').write_bytes(b'foreign')
                output.complete(self.reference)
        self.assertEqual((output.path / 'foreign.txt').read_bytes(), b'foreign')
        self.assertEqual(self.journal.get(self.identifier)['usedBytes'], 2048)

    def test_root_swap_rejects_before_reservation(self):
        root = self.journal.root
        root.rename(self.root / 'old-journal'); root.mkdir()
        (root / 'foreign.txt').write_bytes(b'foreign')
        with self.assertRaises(RepairJournalError):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048):
                self.fail('root swapped')
        self.assertEqual((root / 'foreign.txt').read_bytes(), b'foreign')
        self.assertEqual(self.journal.get(self.identifier)['usedBytes'], 0)

    def test_actual_snapshot_derivation_uses_pinned_parent(self):
        source = self.root / 'source'; source.mkdir(); (source / 'a.txt').write_bytes(b'old')
        snapshot = self.root / 'snapshot'
        original = capture(source, ['a.txt'], snapshot)
        expected = {key: original[key] for key in ('digest', 'files', 'bytes')}
        with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
            result = derive_snapshot(snapshot, output.path, {'a.txt': b'new'}, expected=expected,
                                     output_parent_identity=output.parent_identity)
            output.complete(dict({key: result[key] for key in expected},
                                 schemaVersion=1, kind='reproof-local-source-snapshot'))
        self.assertEqual((output.path / 'data/a.txt').read_bytes(), b'new')

    def test_sidecar_is_fixed_bounded_and_parent_fenced(self):
        with self.assertRaises(RepairJournalError):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
                for name, raw in [('other', b'{}'), ('proposal', bytearray(b'{}')),
                                  ('proposal', b'x' * (2 * 1024 * 1024 + 1))]:
                    with self.assertRaises(RepairJournalError):
                        output.write_sidecar(name, raw)
                self.assertNotIn('proposal', self.journal.get(self.identifier)['outputs'])
                output.path.parent.rename(self.root / 'old-job')
                output.path.parent.mkdir()
                (output.path.parent / 'foreign.txt').write_bytes(b'foreign')
                output.write_sidecar('proposal', b'{}')
        self.assertFalse((output.path.parent / 'proposal').exists())
        self.assertEqual((output.path.parent / 'foreign.txt').read_bytes(), b'foreign')

    def test_sidecar_replacement_and_same_inode_edit_prevent_completion(self):
        for index, replace in enumerate((False, True)):
            if index:
                self.identifier = self.journal.create(project_id='project', owner_id='owner', issue_id='issue',
                    request_id='second', request_digest='1' * 64, reservation_bytes=4096)['id']
            with self.assertRaises(RepairJournalError):
                with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
                    self.populate(output)
                    sidecar = output.write_sidecar('proposal', b'{}') / 'proposal.json'
                    if replace:
                        sidecar.unlink()
                    sidecar.write_bytes(b'foreign')
                    output.complete(self.reference)
            self.assertEqual(sidecar.read_bytes(), b'foreign')
            self.assertEqual(self.journal.get(self.identifier)['usedBytes'], 2050)

    def test_context_closes_all_retained_descriptors(self):
        descriptors = []
        with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
            self.populate(output)
            output.write_sidecar('proposal', b'{}')
            descriptors = [output.root_fd, output.parent_fd] + output.sidecar_fds[:]
            output.complete(self.reference)
            descriptors.append(output.candidate_fd)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_candidate_tamper_before_completion_fails_actual_verification(self):
        from reproof.source_snapshot import SnapshotError
        with self.assertRaises(SnapshotError):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
                self.populate(output)
                (output.path / 'data/file.txt').write_bytes(b'different')
                output.complete(self.reference)
        self.assertEqual((output.path / 'data/file.txt').read_bytes(), b'different')
        self.assertEqual(self.journal.get(self.identifier)['usedBytes'], 2048)

    def test_candidate_swap_after_completion_rejected_at_context_exit(self):
        with self.assertRaises(RepairJournalError):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
                self.populate(output)
                output.complete(self.reference)
                output.path.rename(output.path.with_name('old-candidate'))
                output.path.mkdir()
                (output.path / 'foreign.txt').write_bytes(b'foreign')
        self.assertEqual((output.path / 'foreign.txt').read_bytes(), b'foreign')
        self.assertTrue(self.journal.get(self.identifier)['outputs']['candidate']['cleanupBlocked'])

    def test_actual_scan_does_not_hold_lock_against_cancellation(self):
        from reproof.source_snapshot import SnapshotError, inspect_snapshot
        def cancel_then_scan(*args, **kwargs):
            thread = threading.Thread(target=lambda: self.journal.cancel(self.identifier))
            thread.start(); thread.join(2)
            self.assertFalse(thread.is_alive(), 'scan held journal lock against cancellation')
            return inspect_snapshot(*args, **kwargs)
        with self.assertRaises(SnapshotError):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
                self.populate(output)
                with patch('reproof.source_snapshot.inspect_snapshot', side_effect=cancel_then_scan):
                    output.complete(self.reference)
        self.assertEqual(self.journal.get(self.identifier)['usedBytes'], 2048)

    def test_complete_combines_host_and_journal_cancellation(self):
        from reproof.source_snapshot import SnapshotError
        event = threading.Event()
        with self.assertRaises(SnapshotError):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
                self.populate(output)
                event.set()
                output.complete(self.reference, cancel=event)
        self.assertFalse(self.journal.get(self.identifier)['cancelRequested'])
        self.assertEqual(self.journal.get(self.identifier)['usedBytes'], 2048)

    def test_invalid_host_cancellation_rejected_before_verification(self):
        with self.assertRaises(RepairJournalError):
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=2048) as output:
                self.populate(output)
                with patch('reproof.source_snapshot.inspect_snapshot') as inspect:
                    with self.assertRaises(RepairJournalError):
                        output.complete(self.reference, cancel=object())
                    inspect.assert_not_called()

    def test_manifest_bytes_must_fit_output_reservation(self):
        with self.assertRaises(RepairJournalError) as raised:
            with self.journal.begin_snapshot_output(self.identifier, 'candidate', reservation_bytes=100) as output:
                self.populate(output)
                output.complete(self.reference)
        self.assertEqual(raised.exception.code, 'repair_disk_limit')
        self.assertEqual(self.journal.get(self.identifier)['usedBytes'], 100)
        self.assertTrue(output.path.exists())

    def test_invalid_compact_reference_is_rejected(self):
        for number, reference in enumerate(({}, dict(self.reference, files=True), dict(self.reference, bytes=2049),
                                           dict(self.reference, digest='no'), dict(self.reference, paths=[]))):
            with self.assertRaises(RepairJournalError):
                with self.journal.begin_snapshot_output(self.identifier, f'output{number}', reservation_bytes=256) as output:
                    output.path.mkdir()
                    output.complete(reference)


if __name__ == '__main__':
    unittest.main()
