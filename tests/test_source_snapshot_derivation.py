"""Synthetic bounded snapshot consumers; no build, devices or private inputs."""
from contextlib import contextmanager
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import tracemalloc
import unittest
from unittest.mock import patch

import reproof.source_snapshot as implementation
from reproof.source_snapshot import (
    SnapshotBudget, SnapshotError, capture, derive_snapshot, inspect_snapshot,
    materialize, read_snapshot_files, scan_snapshot_files,
    snapshot_manifest_reference, verify,
)


class SnapshotDerivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'folder').mkdir()
        (self.source / 'a.txt').write_bytes(b'original')
        (self.source / 'b.txt').write_bytes(b'untouched')
        (self.source / 'folder/c.txt').write_bytes(b'metadata')
        self.paths = ['a.txt', 'b.txt', 'folder/c.txt']
        self.snapshot = self.root / 'snapshot'
        summary = capture(self.source, self.paths, self.snapshot)
        self.expected = {key: summary[key] for key in ('digest', 'files', 'bytes')}
        self.output = self.root / 'output'

    def test_inspect_and_metadata_reference_are_not_provenance(self):
        document = inspect_snapshot(self.snapshot, expected=self.expected)
        self.assertEqual(snapshot_manifest_reference(document), self.expected)
        changed = copy.deepcopy(document)
        changed['files'][0]['size'] += 1
        with self.assertRaises(SnapshotError):
            snapshot_manifest_reference(changed)
        (self.snapshot / 'data/a.txt').write_bytes(b'changed!')
        self.assertEqual(snapshot_manifest_reference(document), self.expected)
        with self.assertRaises(SnapshotError):
            inspect_snapshot(self.snapshot)

    def test_expected_binding_checks_all_fields_before_output_or_payload_read(self):
        for field, value in [('digest', '0' * 64), ('files', 4), ('bytes', 26), ('files', True)]:
            expected = dict(self.expected, **{field: value})
            with patch.object(implementation, '_stream') as stream, patch.object(implementation, '_Output') as output:
                with self.assertRaises(SnapshotError):
                    derive_snapshot(self.snapshot, self.output, {'a.txt': b'new'}, expected=expected)
                stream.assert_not_called()
                output.assert_not_called()
        for expected in (None, {}, {'digest': self.expected['digest']}, dict(self.expected, provenance='trusted')):
            with self.assertRaises(SnapshotError):
                read_snapshot_files(self.snapshot, ['a.txt'], expected=expected)

    def test_reference_caller_mutation_cannot_erase_comparison(self):
        expected = dict(self.expected, digest='0' * 64)
        original = implementation._summary
        def summarize(document):
            expected.clear()
            return original(document)
        with patch.object(implementation, '_summary', side_effect=summarize):
            with self.assertRaises(SnapshotError):
                inspect_snapshot(self.snapshot, expected=expected)

    def test_metadata_reads_are_selected_bounded_and_verify_unselected_bytes(self):
        self.assertEqual(read_snapshot_files(self.snapshot, ['a.txt'], expected=self.expected), {'a.txt': b'original'})
        for paths, options in [(['a.txt'], {'max_bytes': 7}), (['a.txt', 'b.txt'], {'max_files': 1}), (['missing.txt'], {})]:
            with self.assertRaises(SnapshotError):
                read_snapshot_files(self.snapshot, paths, expected=self.expected, **options)
        (self.snapshot / 'data/b.txt').write_bytes(b'corrupted')
        with self.assertRaises(SnapshotError):
            read_snapshot_files(self.snapshot, ['a.txt'], expected=self.expected)

    def test_metadata_file_hard_limit_is_128_and_byte_limit_is_16_mib(self):
        for options in ({'max_files': 129}, {'max_files': True}, {'max_files': 0}, {'max_bytes': 16 * 1024 * 1024 + 1}, {'max_bytes': True}):
            with self.assertRaises(SnapshotError):
                read_snapshot_files(self.snapshot, ['a.txt'], expected=self.expected, **options)
        source = self.root / 'many'; source.mkdir()
        names = [f'f{number:03}.txt' for number in range(128)]
        for name in names:
            (source / name).write_bytes(b'x')
        snapshot = self.root / 'many-snapshot'
        summary = capture(source, names, snapshot)
        expected = {key: summary[key] for key in self.expected}
        self.assertEqual(len(read_snapshot_files(snapshot, names, expected=expected, max_files=128)), 128)
        with self.assertRaises(SnapshotError):
            read_snapshot_files(snapshot, names, expected=expected)

    def test_scan_verifies_each_actual_file_once_and_buffers_only_each_selected_file(self):
        seen = []
        with patch.object(implementation, '_stream', wraps=implementation._stream) as stream:
            document = scan_snapshot_files(self.snapshot, ['a.txt', 'folder/c.txt'],
                lambda name, raw: seen.append((name, len(raw), hashlib.sha256(raw).hexdigest())), expected=self.expected)
            self.assertEqual(stream.call_count, 3)
        self.assertEqual(len(seen), 2)
        self.assertEqual(document['digest'], self.expected['digest'])
        (self.snapshot / 'data/b.txt').write_bytes(b'corrupted')
        with self.assertRaises(SnapshotError):
            scan_snapshot_files(self.snapshot, ['a.txt'], lambda name, raw: None, expected=self.expected)

    def test_derive_adds_and_replaces_but_streams_unchanged(self):
        summary = derive_snapshot(self.snapshot, self.output,
            {'a.txt': b'new', 'generated/runtime.swift': b'// runtime'}, expected=self.expected)
        self.assertNotEqual(summary['digest'], self.expected['digest'])
        self.assertEqual(summary['files'], 4)
        self.assertEqual(summary['bytes'], 3 + 9 + 8 + 10)
        self.assertEqual(verify(self.output), summary)
        materialized = self.root / 'materialized'
        materialize(self.output, materialized)
        self.assertEqual((materialized / 'a.txt').read_bytes(), b'new')
        self.assertEqual((materialized / 'b.txt').read_bytes(), b'untouched')
        self.assertEqual((self.snapshot / 'data/a.txt').read_bytes(), b'original')

    def test_replaced_original_bytes_cannot_be_skipped(self):
        (self.snapshot / 'data/a.txt').write_bytes(b'changed!')
        with self.assertRaises(SnapshotError):
            derive_snapshot(self.snapshot, self.output, {'a.txt': b'new'}, expected=self.expected)
        self.assertFalse(self.output.exists())
        self.assertEqual((self.snapshot / 'data/a.txt').read_bytes(), b'changed!')

    def test_changes_envelope_and_final_selection_are_bounded_before_output(self):
        invalid = [{}, {'a.txt': bytearray(b'x')}, {'a.txt': b'x' * (8 * 1024 * 1024 + 1)},
            {f'f{n}.txt': b'x' for n in range(17)},
            {'a.txt': b'x' * (8 * 1024 * 1024), 'b.txt': b'y' * (8 * 1024 * 1024), 'c.txt': b'z'},
            {'../escape.txt': b'x'}, {'a.txt/child.txt': b'x'}, {'A.txt': b'x'}, {'secret.pem': b'x'}]
        for changes in invalid:
            with patch.object(implementation, '_Output') as output:
                with self.assertRaises(SnapshotError):
                    derive_snapshot(self.snapshot, self.output, changes, expected=self.expected)
                output.assert_not_called()
        for budget, changes in [(SnapshotBudget(max_files=3), {'new.txt': b'x'}),
                                (SnapshotBudget(max_bytes=25), {'new.txt': b'x'})]:
            with patch.object(implementation, '_Output') as output:
                with self.assertRaises(SnapshotError):
                    derive_snapshot(self.snapshot, self.output, changes, expected=self.expected, budgets=budget)
                output.assert_not_called()

    def test_existing_output_and_alias_overlap_are_never_mutated(self):
        self.output.mkdir(); (self.output / 'foreign.txt').write_bytes(b'foreign')
        with self.assertRaises(SnapshotError):
            derive_snapshot(self.snapshot, self.output, {'a.txt': b'new'}, expected=self.expected)
        self.assertEqual((self.output / 'foreign.txt').read_bytes(), b'foreign')
        with patch.object(implementation, '_overlap'):
            with self.assertRaises(SnapshotError):
                derive_snapshot(self.snapshot, self.snapshot / 'data/new', {'a.txt': b'new'}, expected=self.expected)
        self.assertFalse((self.snapshot / 'data/new').exists())

    def test_cancel_and_late_input_change_do_not_publish_success(self):
        event = threading.Event(); event.set()
        with self.assertRaises(SnapshotError):
            derive_snapshot(self.snapshot, self.output, {'a.txt': b'new'}, expected=self.expected, cancel=event)
        self.assertFalse(self.output.exists())
        original = implementation._stream
        modified = False
        def stream(*args, **kwargs):
            nonlocal modified
            result = original(*args, **kwargs)
            if not modified:
                modified = True
                with (self.snapshot / 'manifest.json').open('ab') as sink:
                    sink.write(b' ')
            return result
        with patch.object(implementation, '_stream', side_effect=stream):
            with self.assertRaises(SnapshotError):
                derive_snapshot(self.snapshot, self.output, {'a.txt': b'new'}, expected=self.expected)

    def test_cancellation_during_new_payload_stream_closes_writer_and_rolls_back(self):
        event = threading.Event()
        create = implementation._Output.create
        @contextmanager
        def replacement(output, relative):
            with create(output, relative) as sink:
                if relative == 'data/0-new.bin':
                    class Proxy:
                        def write(self, raw):
                            result = sink.write(raw)
                            event.set()
                            return result
                    yield Proxy()
                else:
                    yield sink
        with patch.object(implementation._Output, 'create', replacement):
            with self.assertRaises(SnapshotError) as raised:
                derive_snapshot(self.snapshot, self.output,
                    {'0-new.bin': b'x' * (2 * implementation.CHUNK_BYTES)}, expected=self.expected, cancel=event)
        self.assertEqual(raised.exception.code, 'snapshot_cancelled')
        self.assertFalse(self.output.exists())
        self.assertEqual(verify(self.snapshot)['digest'], self.expected['digest'])

    def test_derived_output_tamper_preserves_foreign_bytes(self):
        original = implementation._validate_output
        def validate(*args, **kwargs):
            path = self.output / 'data/a.txt'
            path.unlink(); path.write_bytes(b'foreign')
            return original(*args, **kwargs)
        with patch.object(implementation, '_validate_output', side_effect=validate):
            with self.assertRaises(SnapshotError) as raised:
                derive_snapshot(self.snapshot, self.output, {'a.txt': b'new'}, expected=self.expected)
        self.assertTrue(raised.exception.cleanup_incomplete)
        self.assertEqual((self.output / 'data/a.txt').read_bytes(), b'foreign')

    def test_output_parent_fence_rejects_replacement_before_creation(self):
        for number, operation in enumerate(('capture', 'materialize', 'derive')):
            parent = self.root / f'parent{number}'; parent.mkdir()
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                info = os.fstat(fd); identity = (info.st_dev, info.st_ino)
                parent.rename(self.root / f'old{number}'); parent.mkdir()
                (parent / 'foreign.txt').write_bytes(b'foreign')
                with self.assertRaises(SnapshotError) as raised:
                    if operation == 'capture':
                        capture(self.source, self.paths, parent / 'result', output_parent_identity=identity)
                    elif operation == 'materialize':
                        materialize(self.snapshot, parent / 'result', output_parent_identity=identity)
                    else:
                        derive_snapshot(self.snapshot, parent / 'result', {'a.txt': b'new'},
                            expected=self.expected, output_parent_identity=identity)
                self.assertEqual(raised.exception.code, 'snapshot_output_parent_changed')
                self.assertFalse((parent / 'result').exists())
                self.assertEqual((parent / 'foreign.txt').read_bytes(), b'foreign')
            finally:
                os.close(fd)

    def test_output_parent_fence_accepts_same_live_parent(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            info = os.fstat(fd)
            summary = derive_snapshot(self.snapshot, self.output, {'a.txt': b'new'},
                expected=self.expected, output_parent_identity=(info.st_dev, info.st_ino))
            self.assertEqual(verify(self.output), summary)
        finally:
            os.close(fd)

    def test_scanner_memory_does_not_accumulate_selected_payloads(self):
        source = self.root / 'large'; source.mkdir()
        paths = [f'f{number:02}.bin' for number in range(12)]
        for path in paths:
            (source / path).write_bytes(b'x' * (4 * 1024 * 1024))
        snapshot = self.root / 'large-snapshot'
        summary = capture(source, paths, snapshot)
        expected = {key: summary[key] for key in self.expected}
        seen = []
        tracemalloc.start()
        try:
            scan_snapshot_files(snapshot, paths, lambda name, raw: seen.append((name, len(raw))), expected=expected)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(seen), 12)
        self.assertLess(peak, 32 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
