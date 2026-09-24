import dataclasses
import hashlib
import multiprocessing
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from reproof.live.disk_budget import DiskBudget, DiskBudgetError
from reproof.live.frame_spool import FrameSpool, FrameSpoolError, SpoolToken


def _budget(root, capacity=8 * 1024 * 1024):
    return DiskBudget(Path(root), capacity_bytes=capacity, journal_headroom_bytes=64 * 1024,
                      free_bytes=lambda: 1 << 30)


def _stage_args(sequence=1, acquisition=10, body=b'frame-bytes', metadata=None):
    return sequence, acquisition, body, hashlib.sha256(body).hexdigest(), metadata or {'width': 4, 'height': 3}


def _crash_stage(root, budget_root, after_publish):
    budget = _budget(budget_root)
    spool = FrameSpool(Path(root), budget, 'recording-crash', max_frames=4, max_bytes=4096)
    if after_publish:
        spool._mark_active = lambda _sequence: os._exit(23)
    else:
        spool._write_file = lambda *_args: os._exit(22)
    spool.stage(*_stage_args())


def _attempt_writer(root, budget_root):
    budget = _budget(budget_root)
    try:
        spool = FrameSpool(Path(root), budget, 'recording-one', max_frames=4, max_bytes=4096)
    except FrameSpoolError:
        budget.close()
        return
    spool.close()
    budget.close()
    os._exit(44)


def _crash_release_group(root, budget_root):
    budget = _budget(budget_root)
    spool = FrameSpool(Path(root), budget, 'recording-one', max_frames=4, max_bytes=4096)
    unlink = spool._unlink_file
    def crash_after_first(path):
        unlink(path)
        if path.name == '1.frame':
            os._exit(24)
    spool._unlink_file = crash_after_first
    spool.recover(lambda tokens: {'frames': [t.frame_sequence for t in tokens]})


def _crash_retirement(root, budget_root, after_commit):
    budget = _budget(budget_root)
    spool = FrameSpool(Path(root), budget, 'recording-one', max_frames=4, max_bytes=4096)
    if after_commit:
        spool.close = lambda: os._exit(26)
    else:
        budget.release = lambda _: os._exit(25)
    spool.finalcleanup()


class FrameSpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.budget = _budget(root / 'budget')
        self.spool = FrameSpool(root / 'spool', self.budget, 'recording-one', max_frames=4, max_bytes=4096)

    def tearDown(self):
        self.spool.close()
        self.budget.close()
        self.temp.cleanup()

    def test_repeated_identical_bytes_are_sequence_keyed_and_releasable(self):
        body = b'repeated-frame'
        digest = hashlib.sha256(body).hexdigest()
        first = self.spool.stage(1, 11, body, digest, {'width': 10})
        second = self.spool.stage(2, 12, body, digest, {'width': 10})
        self.assertEqual(self.spool.read(first), body)
        self.assertEqual(self.spool.read_metadata(first), {'width': 10})
        self.assertEqual(self.spool.snapshot()['activeBytes'], len(body) * 2)
        self.spool.release((first,), lambda tokens: {'segment': 'seg-1', 'sequences': [t.frame_sequence for t in tokens]})
        with self.assertRaises(FrameSpoolError):
            self.spool.read(first)
        self.assertEqual(self.spool.read(second), body)
        with self.assertRaises(FrameSpoolError):
            self.spool.stage(1, 13, body, digest, {'width': 10})
        snapshot = self.spool.snapshot()
        self.assertEqual(snapshot['watermark'], 2)
        self.assertEqual(snapshot['acceptedCount'], 2)
        self.assertEqual(snapshot['releasedCount'], 1)

    def test_active_count_and_bytes_include_staging_reservations(self):
        body = b'1234'
        for sequence in range(1, 5):
            self.spool.stage(sequence, sequence, body, hashlib.sha256(body).hexdigest(), {})
        with self.assertRaises((FrameSpoolError, DiskBudgetError)):
            self.spool.stage(5, 5, body, hashlib.sha256(body).hexdigest(), {})
        limited = FrameSpool(Path(self.temp.name) / 'limited', self.budget, 'recording-two', max_frames=3, max_bytes=7)
        try:
            limited.stage(1, 1, body, hashlib.sha256(body).hexdigest(), {})
            with self.assertRaises((FrameSpoolError, DiskBudgetError)):
                limited.stage(2, 2, body, hashlib.sha256(body).hexdigest(), {})
        finally:
            limited.close()

    def test_tokens_require_exact_issuer_sequence_metadata_and_digest(self):
        sequence, acquisition, body, digest, metadata = _stage_args()
        token = self.spool.stage(sequence, acquisition, body, digest, metadata)
        for altered in (
            dataclasses.replace(token, issuer='foreign'),
            dataclasses.replace(token, frame_sequence=2),
            dataclasses.replace(token, acquisition_sequence=99),
            dataclasses.replace(token, size=token.size + 1),
            dataclasses.replace(token, digest='0' * 64),
            dataclasses.replace(token, metadata_digest='0' * 64),
        ):
            with self.subTest(altered=altered), self.assertRaises(FrameSpoolError):
                self.spool.read(altered)
        with self.assertRaises(FrameSpoolError):
            self.spool.stage(2, 2, body, '0' * 64, metadata)
        with self.assertRaises(FrameSpoolError):
            self.spool.stage(3, 3, body, digest, {'unsafe': float('nan')})

    def test_durable_proof_must_be_non_boolean_and_release_persists_proof(self):
        token = self.spool.stage(*_stage_args())
        with self.assertRaises(FrameSpoolError):
            self.spool.release((token,), lambda _tokens: False)
        self.assertEqual(self.spool.get_active_tokens(), (token,))
        proof = {'segment': 'segment-1', 'frames': [token.frame_sequence]}
        self.spool.mark_releasable((token,), lambda tokens: proof)
        self.assertEqual(self.spool.snapshot()['releasableCount'], 1)
        with self.assertRaises(FrameSpoolError):
            self.spool.release_marked((token,))
        with self.assertRaises(FrameSpoolError):
            self.spool.release_marked((token,), lambda _tokens: {'different': True})
        self.spool.release_marked((token,), lambda _tokens: proof)
        self.assertEqual(self.spool.snapshot()['activeCount'], 0)

    def test_unlink_or_directory_fsync_failure_retains_charge(self):
        token = self.spool.stage(*_stage_args())
        proof = lambda _tokens: {'segment': 'segment-1'}
        with mock.patch.object(self.spool, '_unlink_file', side_effect=OSError('unlink')):
            with self.assertRaises(FrameSpoolError):
                self.spool.release((token,), proof)
        self.assertEqual(self.spool.snapshot()['activeBytes'], token.size)
        with mock.patch.object(self.spool, '_fsync_directory', side_effect=OSError('fsync')):
            with self.assertRaises(FrameSpoolError):
                self.spool.release_marked((token,), proof)
        self.assertEqual(self.spool.snapshot()['activeBytes'], token.size)
        self.spool.release_marked((token,), proof)
        self.assertEqual(self.spool.snapshot()['activeBytes'], 0)

    def test_reopen_recovers_active_rows_and_writer_is_exclusive(self):
        token = self.spool.stage(*_stage_args())
        with self.assertRaises(FrameSpoolError):
            FrameSpool(Path(self.temp.name) / 'spool', self.budget, 'recording-one', max_frames=4, max_bytes=4096)
        self.spool.close()
        reopened = FrameSpool(Path(self.temp.name) / 'spool', self.budget, 'recording-one', max_frames=4, max_bytes=4096)
        try:
            active = reopened.get_active_tokens()
            self.assertEqual(len(active), 1)
            self.assertNotEqual(active[0].issuer, token.issuer)
            self.assertEqual(reopened.read(active[0]), b'frame-bytes')
            self.assertEqual(reopened.snapshot()['watermark'], 1)
        finally:
            reopened.close()

    def test_recovery_revalidates_persisted_releasable_proof(self):
        token = self.spool.stage(*_stage_args())
        proof = {'segment': 'segment-recovery', 'frames': [token.frame_sequence]}
        self.spool.mark_releasable((token,), lambda _tokens: proof)
        self.spool.close()
        reopened = FrameSpool(Path(self.temp.name) / 'spool', self.budget, 'recording-one', max_frames=4, max_bytes=4096)
        try:
            reopened.recover(lambda _tokens: {'wrong': True})
            self.assertEqual(reopened.snapshot()['releasableCount'], 1)
            reopened.recover(lambda _tokens: proof)
            self.assertEqual(reopened.snapshot()['releasableCount'], 0)
            self.assertEqual(reopened.snapshot()['activeBytes'], 0)
        finally:
            reopened.close()

    def test_finalcleanup_retires_sources_but_keeps_charged_identity_metadata(self):
        token = self.spool.stage(*_stage_args())
        self.spool.release((token,), lambda _tokens: {'segment': 'segment-final'})
        self.assertGreater(self.budget.snapshot()['reservations'], 0)
        root = self.spool.root
        lock_inode = (root / 'writer.lock').stat().st_ino
        self.spool.finalcleanup()
        self.assertEqual(list((root / 'frames').iterdir()), [])
        self.assertEqual((root / 'writer.lock').stat().st_ino, lock_inode)
        self.assertEqual(self.budget.reservations_for_owner(self.spool.owner, category='spool'), ())
        journals = self.budget.reservations_for_owner(self.spool.owner, category='journal')
        self.assertEqual(len(journals), 1)
        self.assertGreaterEqual(journals[0]['charged_bytes'], (root / 'spool.sqlite3').stat().st_size)
        self.spool.close()
        reopened = FrameSpool(root, self.budget, 'recording-one', max_frames=4, max_bytes=4096)
        try:
            with self.assertRaises(FrameSpoolError):
                reopened.stage(*_stage_args(sequence=2, acquisition=11))
            reopened.finalcleanup()
        finally:
            reopened.close()

    def test_watermarks_survive_release_and_reopen(self):
        token = self.spool.stage(*_stage_args(sequence=1, acquisition=1))
        self.spool.release(token, lambda _tokens: {'segment': 'segment-watermark'})
        self.spool.close()
        reopened = FrameSpool(Path(self.temp.name) / 'spool', self.budget, 'recording-one', max_frames=4, max_bytes=4096)
        try:
            with self.assertRaises(FrameSpoolError):
                reopened.stage(*_stage_args(sequence=1, acquisition=2))
            with self.assertRaises(FrameSpoolError):
                reopened.stage(*_stage_args(sequence=2, acquisition=1))
            newer = reopened.stage(*_stage_args(sequence=2, acquisition=2))
            self.assertEqual(reopened.read(newer), b'frame-bytes')
        finally:
            reopened.close()

    def test_reopen_rejects_changed_persisted_limits(self):
        self.spool.close()
        with self.assertRaises(FrameSpoolError):
            FrameSpool(Path(self.temp.name) / 'spool', self.budget, 'recording-one', max_frames=5, max_bytes=4096)

    def test_root_scoped_budget_owner_does_not_release_another_spool(self):
        self.spool.close()
        other = FrameSpool(Path(self.temp.name) / 'other-spool', self.budget, 'recording-one',
                           max_frames=4, max_bytes=4096)
        try:
            self.assertEqual(self.budget.snapshot()['reservations'], 4)
            other.finalcleanup()
            self.assertEqual(self.budget.snapshot()['reservations'], 3)
        finally:
            other.close()

    def test_recovery_cleans_crash_before_publication_and_admits_after_publication(self):
        self.spool.close()
        self.budget.close()
        root = Path(self.temp.name)
        for after_publish, expected_active in ((False, 0), (True, 1)):
            spool_root = root / ('after' if after_publish else 'before')
            budget_root = root / ('budget-after' if after_publish else 'budget-before')
            context = multiprocessing.get_context('fork')
            process = context.Process(target=_crash_stage, args=(spool_root, budget_root, after_publish))
            process.start(); process.join(10)
            self.assertEqual(process.exitcode, 23 if after_publish else 22)
            budget = _budget(budget_root)
            recovered = FrameSpool(spool_root, budget, 'recording-crash', max_frames=4, max_bytes=4096)
            try:
                recovered.recover()
                self.assertEqual(recovered.snapshot()['activeCount'], expected_active)
                if expected_active:
                    self.assertEqual(recovered.read(recovered.get_active_tokens()[0]), b'frame-bytes')
            finally:
                recovered.close(); budget.close()
        self.budget = _budget(root / 'unused-budget')
        self.spool = FrameSpool(root / 'unused-spool', self.budget, 'unused', max_frames=1, max_bytes=64)

    def test_journal_charge_covers_real_bookkeeping_before_and_after_a_write(self):
        for staged in (False, True):
            if staged:
                self.spool.stage(*_stage_args())
            actual = sum(path.stat().st_size for path in self.spool.root.iterdir() if path.is_file())
            journal = self.budget.reservations_for_owner(self.spool.owner, category='journal')[0]
            self.assertGreaterEqual(journal['charged_bytes'], actual)
        page_size = self.spool._db.execute('PRAGMA page_size').fetchone()[0]
        self.assertGreaterEqual(journal['charged_bytes'], 2 * self.spool._max_pages * page_size)

    def test_partial_group_unlink_retains_the_complete_proof_group_for_recovery(self):
        first = self.spool.stage(*_stage_args(sequence=1, acquisition=1))
        second = self.spool.stage(*_stage_args(sequence=2, acquisition=2))
        proof = lambda tokens: {'segment': 'segment-group', 'frames': [t.frame_sequence for t in tokens]}
        original_unlink = self.spool._unlink_file
        def fail_second(path):
            if path.name == '2.frame':
                raise OSError('injected second unlink failure')
            return original_unlink(path)
        with mock.patch.object(self.spool, '_unlink_file', side_effect=fail_second):
            with self.assertRaises(FrameSpoolError):
                self.spool.release((first, second), proof)
        self.assertEqual(self.spool.snapshot()['releasableCount'], 2)
        self.spool.recover(proof)
        self.assertEqual(self.spool.snapshot()['releasableCount'], 0)
        self.assertEqual(self.spool.snapshot()['releasedCount'], 2)

    def test_cleanup_marker_closes_admission_across_reopen(self):
        self.spool._db.execute("UPDATE spool_meta SET value='1' WHERE key='cleanup_pending'")
        self.spool.close()
        reopened = FrameSpool(Path(self.temp.name) / 'spool', self.budget, 'recording-one',
                              max_frames=4, max_bytes=4096)
        try:
            with self.assertRaises(FrameSpoolError):
                reopened.stage(*_stage_args())
            reopened.finalcleanup()
        finally:
            reopened.close()

    def test_retirement_keeps_cross_process_writer_exclusion(self):
        original = self.spool._fsync_root
        def check_writer():
            original()
            process = multiprocessing.get_context('spawn').Process(target=_attempt_writer,
                args=(self.spool.root, self.budget.root))
            process.start(); process.join(10)
            self.assertEqual(process.exitcode, 0)
        with mock.patch.object(self.spool, '_fsync_root', side_effect=check_writer):
            self.spool.finalcleanup()

    def test_process_crash_during_group_unlink_preserves_exact_group_recovery(self):
        tokens = [self.spool.stage(*_stage_args(sequence=i, acquisition=i)) for i in (1, 2)]
        proof = lambda values: {'frames': [t.frame_sequence for t in values]}
        self.spool.mark_releasable(tokens, proof)
        self.spool.close()
        process = multiprocessing.get_context('spawn').Process(target=_crash_release_group,
            args=(self.spool.root, self.budget.root))
        process.start(); process.join(10)
        self.assertEqual(process.exitcode, 24)
        self.spool = FrameSpool(self.spool.root, self.budget, 'recording-one', max_frames=4, max_bytes=4096)
        self.assertEqual(self.spool.snapshot()['releasableCount'], 2)
        self.spool.recover(proof)
        self.assertEqual(self.spool.snapshot()['releasedCount'], 2)

    def test_retirement_crashes_reopen_only_for_cleanup(self):
        root, budget_root = self.spool.root, self.budget.root
        self.spool.close()
        for after_commit in (False, True):
            process = multiprocessing.get_context('spawn').Process(target=_crash_retirement,
                args=(root, budget_root, after_commit))
            process.start(); process.join(10)
            self.assertEqual(process.exitcode, 26 if after_commit else 25)
            self.spool = FrameSpool(root, self.budget, 'recording-one', max_frames=4, max_bytes=4096)
            with self.assertRaises(FrameSpoolError):
                self.spool.stage(*_stage_args())
            self.spool.close()
        self.spool = FrameSpool(root, self.budget, 'recording-one', max_frames=4, max_bytes=4096)
        self.spool.finalcleanup()

    def test_maximum_active_metadata_and_proof_remain_within_reserved_bookkeeping(self):
        budget = _budget(Path(self.temp.name) / 'large-budget', capacity=128 * 1024 * 1024)
        spool = FrameSpool(Path(self.temp.name) / 'large-spool', budget, 'recording-large',
                           max_frames=1024, max_bytes=1024)
        try:
            body = b'x'
            source_digest = hashlib.sha256(body).hexdigest()
            tokens = [spool.stage(i, i, body, source_digest, {'padding': 'm' * 3900})
                      for i in range(1, 1025)]
            spool.mark_releasable(tokens, lambda _: {'padding': 'p' * 3900})
            charged = budget.reservations_for_owner(spool.owner, category='journal')[0]['charged_bytes']
            self.assertLessEqual(spool._bookkeeping_bytes(), charged)
        finally:
            spool.close()
            budget.close()

    def test_failed_construction_releases_reservations_only_after_bookkeeping_cleanup(self):
        budget = _budget(Path(self.temp.name) / 'small-budget', capacity=2 * 1024 * 1024)
        try:
            with self.assertRaises(DiskBudgetError):
                FrameSpool(Path(self.temp.name) / 'cannot-reserve', budget, 'recording-small',
                           max_frames=1, max_bytes=1024 * 1024)
            self.assertEqual(budget.snapshot()['reservations'], 0)
        finally:
            budget.close()
        before = self.budget.snapshot()['chargedBytes']
        failed_root = Path(self.temp.name) / 'cannot-initialize'
        with mock.patch.object(FrameSpool, '_initialize', side_effect=sqlite3.OperationalError('injected')):
            with self.assertRaises(sqlite3.OperationalError):
                FrameSpool(failed_root, self.budget, 'recording-init', max_frames=4, max_bytes=4096)
        self.assertEqual(self.budget.snapshot()['chargedBytes'], before)
        self.assertFalse((failed_root / 'spool.sqlite3').exists())

    def test_retired_reopen_and_cleanup_reject_unexpected_source_residue(self):
        root = self.spool.root
        self.spool.finalcleanup()
        for name in ('1.frame', '.1.' + 'a' * 32 + '.tmp'):
            extra = root / 'frames' / name
            extra.write_bytes(b'unexpected-source')
            try:
                with self.assertRaises(FrameSpoolError):
                    FrameSpool(root, self.budget, 'recording-one', max_frames=4, max_bytes=4096)
                with self.assertRaises(FrameSpoolError):
                    self.spool.finalcleanup()
            finally:
                extra.unlink()

    def test_fresh_constructor_preserves_unjournaled_source_residue(self):
        for index, name in enumerate(('1.frame', '.1.' + 'a' * 32 + '.tmp')):
            with self.subTest(name=name):
                root = Path(self.temp.name) / f'fresh-residue-{index}'
                (root / 'frames').mkdir(parents=True)
                extra = root / 'frames' / name
                extra.write_bytes(b'unjournaled')
                before = self.budget.snapshot()['chargedBytes']
                with self.assertRaises(FrameSpoolError):
                    FrameSpool(root, self.budget, 'recording-extra', max_frames=4, max_bytes=4096)
                self.assertEqual(extra.read_bytes(), b'unjournaled')
                self.assertEqual(self.budget.snapshot()['chargedBytes'], before)

    def test_active_cleanup_preserves_unjournaled_source_residue(self):
        for name in ('1.frame', '.1.' + 'a' * 32 + '.tmp'):
            with self.subTest(name=name):
                extra = self.spool.root / 'frames' / name
                extra.write_bytes(b'unjournaled')
                before = self.budget.snapshot()['chargedBytes']
                try:
                    with self.assertRaises(FrameSpoolError):
                        self.spool.finalcleanup()
                    self.assertEqual(extra.read_bytes(), b'unjournaled')
                    self.assertEqual(self.budget.snapshot()['chargedBytes'], before)
                finally:
                    extra.unlink()

    def test_open_existing_adopts_saved_bounds_and_never_creates_a_spool(self):
        root = self.spool.root
        token = self.spool.stage(*_stage_args())
        self.spool.close()
        self.spool = FrameSpool.open_existing(root, self.budget, 'recording-one')
        self.assertEqual((self.spool.max_frames, self.spool.max_bytes), (4, 4096))
        self.assertEqual(self.spool.read(self.spool.get_active_tokens()[0]), b'frame-bytes')
        with self.assertRaises(FrameSpoolError):
            self.spool.read(token)
        missing = Path(self.temp.name) / 'missing-spool'
        with self.assertRaises(FrameSpoolError):
            FrameSpool.open_existing(missing, self.budget, 'recording-missing')
        self.assertFalse(missing.exists())

    def test_uninitialized_retirement_keeps_lock_and_releases_only_exact_fixed_charge(self):
        root = Path(self.temp.name) / 'uninitialized-spool'
        (root / 'frames').mkdir(parents=True)
        lock = root / 'writer.lock'
        lock.touch(mode=0o600)
        lock_inode = lock.stat().st_ino
        owner = FrameSpool._owner_for(root, 'recording-uninitialized')
        expected = self.budget.reserve(
            owner, 'spool', 4096, idempotency_key=owner + '-source')
        unknown = self.budget.reserve(
            owner, 'journal', 123, idempotency_key=owner + '-unknown')
        try:
            self.assertEqual(FrameSpool.retire_uninitialized(
                root, self.budget, 'recording-uninitialized',
                max_frames=4, max_bytes=4096), 1)
            self.assertEqual(self.budget.reservations_for_owner(
                owner, category='spool'), ())
            self.assertEqual(len(self.budget.reservations_for_owner(
                owner, category='journal')), 1)
            self.assertEqual(lock.stat().st_ino, lock_inode)
            self.assertTrue((root / 'frames').is_dir())
        finally:
            expected.close()
            unknown.close()


if __name__ == '__main__':
    unittest.main()
