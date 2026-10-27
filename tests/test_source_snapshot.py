"""Synthetic local snapshots; no build, network, repository discovery or devices."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from reproof.source_snapshot import (SnapshotBudget, SnapshotError, capture,
                                     materialize, verify, MAX_FILES, MAX_BYTES)
import reproof.source_snapshot as implementation


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root/'source';self.source.mkdir()
        (self.source/'main.txt').write_bytes(b'hello\n')
        (self.source/'nested').mkdir()
        (self.source/'nested'/'empty.bin').write_bytes(b'')
        self.paths = ['nested/empty.bin', 'main.txt']
        self.snapshot = self.root/'snapshot'
        self.output = self.root/'output'

    def create(self):
        return capture(self.source, self.paths, self.snapshot)

    def assert_clean_error(self, function, *args, **kwargs):
        with self.assertRaises(SnapshotError):
            function(*args, **kwargs)
        self.assertFalse(self.output.exists())

    def test_roundtrip_empty_files_and_original_unchanged(self):
        captured = self.create()
        self.assertEqual(captured['files'], 2)
        self.assertEqual(captured['bytes'], 6)
        self.assertEqual(captured['provenance'], 'unverified-local-content')
        self.assertEqual(verify(self.snapshot), captured)
        self.assertEqual(materialize(self.snapshot, self.output), captured)
        for name in self.paths:
            self.assertEqual((self.output/name).read_bytes(), (self.source/name).read_bytes())
        self.assertEqual((self.snapshot/'data/main.txt').stat().st_nlink, 1)
        self.assertEqual((self.output/'main.txt').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.snapshot.stat().st_mode & 0o777, 0o700)

    def test_actual_filesystem_alias_or_pinned_ancestry_boundary(self):
        original = self.root/'CaseInput';original.mkdir()
        (original/'one.txt').write_bytes(b'synthetic')
        alias = self.root/'caseinput'
        case_insensitive = alias.exists()
        parent = alias if case_insensitive else original
        # On case-sensitive runners exercise the same real inode ancestry guard
        # without relying on the early lexical rejection. On default macOS the
        # differently spelled alias itself is the real filesystem condition.
        with patch.object(implementation, '_overlap'), self.assertRaises(SnapshotError) as error:
            capture(original,['one.txt'],parent/'new-output')
        self.assertEqual(error.exception.code,'snapshot_output_overlaps_input')
        self.assertFalse((original/'new-output').exists())
        print('SOURCE_SNAPSHOT_FILESYSTEM caseInsensitive=' + str(case_insensitive).lower(),flush=True)

    def test_strict_budget_envelope(self):
        for kwargs in ({'max_files': True}, {'max_files': MAX_FILES+1},
                       {'max_bytes': MAX_BYTES+1}, {'max_file_bytes': 0},
                       {'max_manifest_bytes': 24*1024*1024+1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SnapshotError):
                SnapshotBudget(**kwargs)
        self.assert_clean_error(capture, self.source, self.paths, self.output,
                                budgets=SnapshotBudget(max_bytes=5))
        self.assert_clean_error(capture, self.source, self.paths, self.output,
                                budgets=SnapshotBudget(max_file_bytes=5))
        self.assert_clean_error(capture, self.source, self.paths, self.output,
                                budgets=SnapshotBudget(max_manifest_bytes=20))

    def test_rejects_selection_before_creating_output(self):
        for paths in ([], ['main.txt']*2, ['../outside'], ['/absolute'], ['a\\b'],
                      ['.env'], ['secret.pem'], ['a', 'a/x'], ['A/x','a/y'],
                      ['CON.txt'], ['e\u0301.txt'], ['a/'*32+'x'], ['main.txt', 'MAIN.txt']):
            with self.subTest(paths=paths):
                self.assert_clean_error(capture, self.source, paths, self.output)

    def test_output_must_be_fresh_and_nonoverlapping(self):
        self.output.mkdir();(self.output/'user.txt').write_text('preserve')
        with self.assertRaises(SnapshotError):capture(self.source, self.paths, self.output)
        self.assertEqual((self.output/'user.txt').read_text(), 'preserve')
        with self.assertRaises(SnapshotError):capture(self.source, self.paths, self.source/'inside')
        self.assertFalse((self.source/'inside').exists())
        self.snapshot.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(SnapshotError):capture(self.source, self.paths, self.snapshot)
        self.assertTrue(self.snapshot.is_symlink())

    def test_symlink_hardlink_special_and_component_rejected(self):
        (self.source/'link.txt').symlink_to('main.txt')
        self.assert_clean_error(capture, self.source, ['link.txt'], self.output)
        os.link(self.source/'main.txt', self.source/'hard.txt')
        self.assert_clean_error(capture, self.source, ['hard.txt'], self.output)
        (self.source/'hard.txt').unlink()
        (self.source/'alias').symlink_to(self.source/'nested', target_is_directory=True)
        self.assert_clean_error(capture, self.source, ['alias/empty.bin'], self.output)
        os.mkfifo(self.source/'pipe')
        self.assert_clean_error(capture, self.source, ['pipe'], self.output)

    def test_cancel_and_keyboard_interrupt_roll_back(self):
        cancelled = threading.Event();cancelled.set()
        self.assert_clean_error(capture, self.source, self.paths, self.output, cancel=cancelled)
        real = implementation.os.read
        calls = []
        def interrupt(fd, size):
            calls.append(fd)
            if len(calls) == 2:raise KeyboardInterrupt()
            return real(fd, size)
        with patch.object(implementation.os, 'read', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):capture(self.source, self.paths, self.output)
        self.assertFalse(self.output.exists())

    def test_partial_write_failure_rolls_back(self):
        real = implementation._Output.create
        def fail(destination, relative):
            if relative.endswith('empty.bin'):raise OSError('synthetic disk full')
            return real(destination, relative)
        with patch.object(implementation._Output, 'create', fail):
            self.assert_clean_error(capture, self.source, self.paths, self.output)

    def test_tampered_file_rejected_during_materialize_and_cleaned(self):
        self.create()
        (self.snapshot/'data/main.txt').write_bytes(b'wrong!')
        with self.assertRaises(SnapshotError):verify(self.snapshot)
        self.assert_clean_error(materialize, self.snapshot, self.output)

    def test_unlisted_files_and_directories_rejected(self):
        self.create()
        for path, directory in (('extra.txt', False), ('extra', True)):
            item = self.snapshot/path
            item.mkdir() if directory else item.write_bytes(b'x')
            with self.assertRaises(SnapshotError):verify(self.snapshot)
            self.assert_clean_error(materialize, self.snapshot, self.output)
            item.rmdir() if directory else item.unlink()

    def test_manifest_duplicates_whitespace_wrong_fields_and_links_rejected(self):
        self.create()
        path = self.snapshot/'manifest.json';original = path.read_bytes()
        doc = json.loads(original)
        for raw in (original+b'\n', original.replace(b'"schemaVersion":1', b'"schemaVersion":1,"schemaVersion":1'),
                    json.dumps(dict(doc, command='execute')).encode(),
                    original.replace(b'"size":6',b'"size":true')):
            path.write_bytes(raw)
            with self.assertRaises(SnapshotError):verify(self.snapshot)
        path.write_bytes(original)
        outside = self.root/'manifest-copy';outside.write_bytes(original)
        path.unlink();path.symlink_to(outside)
        with self.assertRaises(SnapshotError):verify(self.snapshot)

    def test_file_changed_during_stream_rejected(self):
        real = implementation.os.read
        changed = False
        def mutate(fd, size):
            nonlocal changed
            raw = real(fd, size)
            if raw and not changed:
                changed = True
                (self.source/'main.txt').write_bytes(b'change')
            return raw
        with patch.object(implementation.os, 'read', side_effect=mutate):
            self.assert_clean_error(capture, self.source, ['main.txt'], self.output)

    def test_replaced_source_root_rejected_without_following_new_tree(self):
        real = implementation._stream
        replaced = False
        def replace(*args, **kwargs):
            nonlocal replaced
            result = real(*args, **kwargs)
            if not replaced:
                replaced = True
                self.source.rename(self.root/'old-source')
                self.source.mkdir();(self.source/'main.txt').write_text('foreign')
            return result
        with patch.object(implementation, '_stream', side_effect=replace), self.assertRaises(SnapshotError) as error:
            capture(self.source, ['main.txt'], self.output)
        self.assertEqual(error.exception.code, 'snapshot_cleanup_incomplete')
        self.assertTrue(self.output.exists())
        self.assertEqual((self.source/'main.txt').read_text(), 'foreign')

    def test_destination_replacement_is_preserved_and_cleanup_uncertain(self):
        real = implementation._stream
        def replace(*args, **kwargs):
            result = real(*args, **kwargs)
            self.output.rename(self.root/'displaced-output')
            self.output.mkdir();(self.output/'foreign.txt').write_text('preserve')
            return result
        with patch.object(implementation, '_stream', side_effect=replace), self.assertRaises(SnapshotError) as error:
            capture(self.source, ['main.txt'], self.output)
        self.assertEqual(error.exception.code, 'snapshot_cleanup_incomplete')
        self.assertTrue(error.exception.cleanup_incomplete)
        self.assertEqual((self.output/'foreign.txt').read_text(), 'preserve')

    def test_manifest_allocation_preflight_precedes_json_decoder(self):
        self.create()
        path = self.snapshot/'manifest.json'
        for raw in (b'['*1000+b']'*1000,
                    b'['+b'{},'*902+b'{}]',
                    b'"'+b'x'*5000+b'"',
                    b'['+b'0,'*6000+b'0]'):
            path.write_bytes(raw)
            with patch.object(implementation.json, 'loads', side_effect=AssertionError('decoder reached')):
                with self.assertRaises(SnapshotError):verify(self.snapshot)

    def test_intermediate_source_directory_replacement_is_rejected(self):
        (self.source/'nested/item.txt').write_text('original')
        real = implementation.os.read
        replaced = False
        def mutate(fd, size):
            nonlocal replaced
            raw = real(fd, size)
            if raw and not replaced:
                replaced = True
                (self.source/'nested').rename(self.source/'old-nested')
                (self.source/'nested').mkdir()
                (self.source/'nested/item.txt').write_text('replacement')
            return raw
        with patch.object(implementation.os, 'read', side_effect=mutate):
            self.assert_clean_error(capture, self.source, ['nested/item.txt'], self.output)

    def test_final_destination_mutation_does_not_report_success(self):
        real = implementation._manifest
        for mutation in ('same-inode', 'foreign', 'replacement'):
            with self.subTest(mutation=mutation):
                def mutate(*args, **kwargs):
                    path = self.output/'data/main.txt'
                    if mutation == 'same-inode':path.write_bytes(b'bad!!!')
                    elif mutation == 'foreign':(self.output/'foreign').write_text('preserve')
                    else:
                        path.unlink();path.write_bytes(b'foreign')
                    return real(*args, **kwargs)
                with patch.object(implementation, '_manifest', side_effect=mutate), self.assertRaises(SnapshotError) as error:
                    capture(self.source, ['main.txt'], self.output)
                self.assertEqual(error.exception.code, 'snapshot_cleanup_incomplete')
                self.assertTrue(self.output.exists())
                if mutation == 'same-inode':
                    self.assertEqual((self.output/'data/main.txt').read_bytes(), b'bad!!!')
                # Test-owned synthetic leftovers, never production cleanup logic.
                import shutil
                shutil.rmtree(self.output)

    def test_directory_cardinality_is_checked_incrementally(self):
        with patch.object(implementation, 'MAX_DIRECTORIES', 2):
            self.assert_clean_error(capture, self.source, ['a/b/c/file.txt'], self.output)

    def test_output_initialization_failure_reports_possible_residue(self):
        real = implementation.os.stat
        def fail(path, *args, **kwargs):
            if path == 'output' and kwargs.get('dir_fd') is not None:
                raise OSError('synthetic post-mkdir stat failure')
            return real(path, *args, **kwargs)
        with patch.object(implementation.os, 'stat', side_effect=fail), self.assertRaises(SnapshotError) as error:
            capture(self.source, self.paths, self.output)
        self.assertEqual(error.exception.code, 'snapshot_cleanup_incomplete')
        self.assertTrue(self.output.is_dir())

    def test_failed_child_descriptor_validation_closes_new_descriptor(self):
        destination = implementation._Output(self.output)
        real_open, real_fstat = implementation.os.open, implementation.os.fstat
        opened = []
        def opening(path, *args, **kwargs):
            descriptor = real_open(path, *args, **kwargs)
            if path == 'nested':opened.append(descriptor)
            return descriptor
        def failing(descriptor):
            if descriptor in opened:raise OSError('synthetic fstat failure')
            return real_fstat(descriptor)
        try:
            with patch.object(implementation.os, 'open', side_effect=opening), \
                    patch.object(implementation.os, 'fstat', side_effect=failing):
                with self.assertRaises(OSError):destination.create('nested/file.txt')
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):real_fstat(opened[0])
            self.assertFalse(destination.rollback())
            self.assertTrue((self.output/'nested').is_dir())
        finally:
            destination.close()

    def test_manifest_changed_during_final_output_pass_is_rejected(self):
        real = implementation._stream
        def mutate(root_fd, relative, *args, **kwargs):
            result = real(root_fd, relative, *args, **kwargs)
            if relative == 'data/main.txt':
                path = self.output/'manifest.json'
                path.write_bytes(path.read_bytes()+b' ')
            return result
        with patch.object(implementation, '_stream', side_effect=mutate), self.assertRaises(SnapshotError) as error:
            capture(self.source, ['main.txt'], self.output)
        self.assertEqual(error.exception.code, 'snapshot_cleanup_incomplete')
        self.assertTrue((self.output/'manifest.json').exists())

    def test_parent_inode_ancestry_rejects_nested_output_even_without_lexical_guard(self):
        nested = self.source/'nested'/'output'
        with patch.object(implementation, '_overlap'), self.assertRaises(SnapshotError) as error:
            capture(self.source, ['main.txt'], nested)
        self.assertEqual(error.exception.code, 'snapshot_output_overlaps_input')
        self.assertFalse(nested.exists())
        self.create()
        nested = self.snapshot/'data'/'output'
        with patch.object(implementation, '_overlap'), self.assertRaises(SnapshotError) as error:
            materialize(self.snapshot, nested)
        self.assertEqual(error.exception.code, 'snapshot_output_overlaps_input')
        self.assertFalse(nested.exists())

    def test_parent_identity_alias_is_checked_before_mkdir_with_same_fd(self):
        source = implementation._Root(self.source)
        original = implementation._require_separate
        checked = []
        def alias(root, descriptor):
            checked.append(descriptor)
            # Model case-insensitive spelling: lexical source path differs,
            # but the selected input inode equals the opened output parent.
            from types import SimpleNamespace
            identity = implementation._identity(os.fstat(descriptor))
            return original(SimpleNamespace(identity=identity), descriptor)
        try:
            with patch.object(implementation, '_require_separate', side_effect=alias), \
                    patch.object(implementation.os, 'mkdir') as mkdir, self.assertRaises(SnapshotError):
                implementation._Output(self.output, source_root=source)
            mkdir.assert_not_called()
            self.assertEqual(len(checked), 1)
            with self.assertRaises(OSError):os.fstat(checked[0])
        finally:source.close()

    def test_parent_walk_is_bounded_and_closes_traversal_descriptors(self):
        root = implementation._Root(self.source)
        descriptor = implementation.open_directory(self.root)
        real_dup, real_open = implementation.os.dup, implementation.os.open
        created = []
        def duplicate(fd):
            result = real_dup(fd);created.append(result);return result
        def opening(path, *args, **kwargs):
            result = real_open(path, *args, **kwargs);created.append(result);return result
        try:
            with patch.object(implementation, 'MAX_PARENT_DEPTH', 1), \
                    patch.object(implementation.os, 'dup', side_effect=duplicate), \
                    patch.object(implementation.os, 'open', side_effect=opening), \
                    self.assertRaises(SnapshotError) as error:
                implementation._require_separate(root, descriptor)
            self.assertEqual(error.exception.code, 'snapshot_parent_limit')
            for fd in set(created):
                with self.assertRaises(OSError):os.fstat(fd)
            self.assertEqual(implementation._identity(os.fstat(descriptor)), implementation._identity(self.root.stat()))
        finally:
            os.close(descriptor);root.close()

    def test_explicit_mutable_selection_is_frozen_before_validation(self):
        paths = ['main.txt']
        original = implementation.inert_input_path
        def mutate(path):
            paths.append('nested/empty.bin')
            return original(path)
        with patch.object(implementation, 'inert_input_path', side_effect=mutate):
            result = capture(self.source, paths, self.output)
        self.assertEqual(result['files'], 1)
        self.assertEqual(verify(self.output)['files'], 1)
        self.assertFalse((self.output/'data/nested').exists())

    def test_reused_inode_numbers_never_authorize_file_deletion(self):
        # Model both inode reuse and coarse timestamps. A stat-only ledger
        # sees a matching replacement; only the still-live old pin has nlink=0.
        real_identity, real_stamp = implementation._identity, implementation._stamp
        import stat
        def identity(info):
            return (7, 11) if stat.S_ISREG(info.st_mode) else real_identity(info)
        def coarse(info):
            return real_stamp(info)[:-2] + (0, 0)
        with patch.object(implementation, '_identity', side_effect=identity), \
                patch.object(implementation, '_stamp', side_effect=coarse):
            destination = implementation._Output(self.output)
            try:
                with destination.create('active/file.txt') as writer:writer.write(b'original')
                old_pin = destination.file_pins['active/file.txt'].fd
                sealed = destination.file_pins['active/file.txt'].stamp
                (self.output/'active/file.txt').unlink()
                (self.output/'active/file.txt').write_bytes(b'foreign!')
                (self.output/'active/file.txt').chmod(0o600)
                self.assertEqual(coarse((self.output/'active/file.txt').stat()), sealed)
                self.assertEqual(os.fstat(old_pin).st_nlink, 0)
                self.assertFalse(destination.rollback())
                self.assertEqual((self.output/'active/file.txt').read_bytes(), b'foreign!')
            finally:destination.close()

    def test_reused_directory_identity_never_authorizes_mutation(self):
        # A retained directory FD prevents inode reuse, but APFS can still
        # report nlink=2 after rmdir. Prove rejection from real bound/live
        # identity, even with nonzero nlink and no useful metadata changes.
        import stat
        real_stamp, real_fstat = implementation._stamp, os.fstat
        old_directory = None
        class LinkedDirectory:
            st_nlink = 2
            def __init__(self, info):self.info = info
            def __getattr__(self, name):return getattr(self.info, name)
        def fstat(fd):
            info = real_fstat(fd)
            return LinkedDirectory(info) if fd == old_directory else info
        def identity_only_directory_stamp(info):
            if stat.S_ISDIR(info.st_mode):
                return (implementation._identity(info), stat.S_IFDIR, 2, 0, 0, 0)
            return real_stamp(info)
        with patch.object(implementation, '_stamp', side_effect=identity_only_directory_stamp), \
                patch.object(implementation.os, 'fstat', side_effect=fstat):
            destination = implementation._Output(self.output)
            try:
                with destination.create('folder/file.txt') as writer:writer.write(b'original')
                old_directory = destination.directories[-1].fd
                (self.output/'folder/file.txt').unlink()
                (self.output/'folder').rmdir()
                (self.output/'folder').mkdir()
                (self.output/'folder/foreign.txt').write_bytes(b'preserve')
                self.assertNotEqual(implementation._identity(real_fstat(old_directory)),
                                    implementation._identity((self.output/'folder').stat()))
                self.assertEqual(os.fstat(old_directory).st_nlink, 2)
                with self.assertRaises(SnapshotError):destination.create('folder/new.txt')
                self.assertFalse(destination.rollback())
                self.assertEqual((self.output/'folder/foreign.txt').read_bytes(), b'preserve')
                self.assertFalse((self.output/'folder/new.txt').exists())
            finally:destination.close()

    def test_same_inode_completed_write_changes_are_preserved(self):
        destination = implementation._Output(self.output)
        try:
            with destination.create('file.txt') as writer:writer.write(b'original')
            (self.output/'file.txt').write_bytes(b'changed contents')
            self.assertFalse(destination.rollback())
            self.assertEqual((self.output/'file.txt').read_bytes(), b'changed contents')
        finally:destination.close()

    def test_file_pin_window_is_bounded_and_evicted_files_are_never_deleted(self):
        destination = implementation._Output(self.output)
        live_fds = []
        try:
            for index in range(80):
                with destination.create(f'file-{index:03d}.txt') as writer:
                    writer.write(b'content')
                    self.assertLessEqual(len(destination.file_pins), 32)
                self.assertTrue(all(pin.stamp is not None for pin in destination.file_pins.values()))
            self.assertEqual(len(destination.file_pins), 32)
            self.assertEqual(len(destination.retired_files), 48)
            live_fds = [destination.fd, destination.parent,
                        *(pin.fd for pin in destination.file_pins.values())]
            self.assertFalse(destination.rollback())
            self.assertTrue(all((self.output/f'file-{index:03d}.txt').exists() for index in range(48)))
            self.assertFalse(any((self.output/f'file-{index:03d}.txt').exists() for index in range(48,80)))
        finally:destination.close()
        for fd in live_fds:
            with self.assertRaises(OSError):os.fstat(fd)

    def test_closed_directory_prefixes_are_not_reopened_for_mutation(self):
        destination = implementation._Output(self.output)
        try:
            with destination.create('a/file.txt') as writer:writer.write(b'one')
            closed_pin = destination.directories[0].fd
            with destination.create('b/file.txt') as writer:writer.write(b'two')
            self.assertIn('a', destination.retired_directories)
            self.assertNotIn('a/file.txt', destination.file_pins)
            with self.assertRaises(SnapshotError) as error:destination.create('a/new.txt')
            self.assertEqual(error.exception.code, 'snapshot_output_unpinned')
            self.assertFalse((self.output/'a/new.txt').exists())
            self.assertFalse(destination.rollback())
            self.assertEqual((self.output/'a/file.txt').read_bytes(), b'one')
        finally:destination.close()

    def test_active_directory_prefix_pin_count_is_bounded(self):
        destination = implementation._Output(self.output)
        try:
            for index in range(40):
                path = f'sub-{index:03d}/' + '/'.join(f'level-{depth}' for depth in range(30)) + '/file.txt'
                with destination.create(path) as writer:
                    writer.write(b'x')
                    self.assertLessEqual(len(destination.directories), 32)
                    self.assertLessEqual(len(destination.file_pins), 32)
            self.assertFalse(destination.rollback())
        finally:destination.close()

    def test_materialize_failure_preserves_completed_file_replacement(self):
        self.create()
        real = implementation._validate_output
        def replace(destination, document, budget, cancel, *, snapshot):
            self.assertFalse(snapshot)
            path = self.output/'main.txt'
            path.unlink();path.write_bytes(b'foreign data')
            return real(destination, document, budget, cancel, snapshot=snapshot)
        with patch.object(implementation, '_validate_output', side_effect=replace), self.assertRaises(SnapshotError) as error:
            materialize(self.snapshot, self.output)
        self.assertEqual(error.exception.code, 'snapshot_cleanup_incomplete')
        self.assertEqual((self.output/'main.txt').read_bytes(), b'foreign data')

    def test_actual_descriptor_ceiling_and_no_leak_with_maximum_pins(self):
        real_open, real_dup, real_close, real_fstat = os.open, os.dup, os.close, os.fstat
        observed = set();peak = 0
        def note(fd):
            nonlocal peak
            observed.add(fd)
            live = 0
            for descriptor in tuple(observed):
                try:real_fstat(descriptor);live += 1
                except OSError:observed.discard(descriptor)
            peak = max(peak, live)
            return fd
        def opening(*args, **kwargs):return note(real_open(*args, **kwargs))
        def duplicate(*args, **kwargs):return note(real_dup(*args, **kwargs))
        def closing(fd):
            real_close(fd);observed.discard(fd)
        prefix = '/'.join(f'd{index:02d}' for index in range(31))
        with patch.object(implementation.os, 'open', side_effect=opening), \
                patch.object(implementation.os, 'dup', side_effect=duplicate), \
                patch.object(implementation.os, 'close', side_effect=closing):
            destination = implementation._Output(self.output)
            try:
                for index in range(40):
                    with destination.create(prefix+f'/file-{index:03d}') as writer:writer.write(b'x')
                self.assertEqual(len(destination.directories), 31)
                self.assertEqual(len(destination.file_pins), 32)
            finally:destination.close()
        self.assertGreaterEqual(peak, 65)
        self.assertLessEqual(peak, 2+32+32+8)
        for fd in observed:
            with self.assertRaises(OSError):real_fstat(fd)

    def test_manifest_is_not_provenance_but_actual_content_is_required(self):
        self.create()
        # A locally authored replacement is a new content set, never authorization.
        path = self.snapshot/'data/main.txt';path.write_bytes(b'new')
        document = json.loads((self.snapshot/'manifest.json').read_bytes())
        item = next(item for item in document['files'] if item['path']=='main.txt')
        import hashlib
        item.update(size=3,sha256=hashlib.sha256(b'new').hexdigest())
        document = implementation._manifest(document['files'], SnapshotBudget())
        (self.snapshot/'manifest.json').write_bytes(b''.join(implementation._chunks(document)))
        self.assertEqual(verify(self.snapshot)['provenance'], 'unverified-local-content')


if __name__ == '__main__':unittest.main()
