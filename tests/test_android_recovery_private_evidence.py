"""Synthetic local files/pipes only. No real credentials or native processes."""
import errno
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof.live.android_recovery_private_evidence import _EvidenceSink, _MAXIMUM
from reproof.live.android_recovery_diagnostics import IncrementalCollector, birth_summary
from reproof.live import android_recovery_birth as birth


class PrivateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'credentials').mkdir(mode=0o700)
        self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.dir_fd = os.open(self.root/'credentials', os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.root_fd)
        self.addCleanup(os.close, self.dir_fd)
        self.sinks = []
        self.addCleanup(self.finish)

    def finish(self):
        for sink in self.sinks:
            sink.finish()

    def sink(self, name='0-stderr.bin'):
        sink = _EvidenceSink(self.root, self.root_fd, self.dir_fd, name)
        self.sinks.append(sink)
        return sink

    def test_exact_private_prefix_persists_but_never_enters_report(self):
        raw = b'ERROR synthetic-public-key /private/example\n'
        sink = self.sink()
        collector = IncrementalCollector(io.BytesIO(raw), evidence=sink)
        collector.thread.start();collector.thread.join(2)
        path = self.root/'credentials/0-stderr.bin'
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(collector.snapshot()['privateEvidence']['complete'])
        report = birth_summary(SimpleNamespace(_children=[SimpleNamespace(collectors=[collector])]))
        encoded = json.dumps(report)
        for value in ('synthetic-public-key','/private/example','0-stderr.bin',str(self.root)):
            self.assertNotIn(value, encoded)

    def test_permitted_advisory_is_retained_without_raw_report(self):
        from reproof.live.android_recovery_diagnostics import _VULKAN_ADVISORY
        sink = self.sink()
        collector = IncrementalCollector(io.BytesIO(_VULKAN_ADVISORY), evidence=sink,
                                         _vulkan_advisory=True)
        collector.thread.start();collector.thread.join(2)
        self.assertEqual((self.root/'credentials/0-stderr.bin').read_bytes(), _VULKAN_ADVISORY)
        self.assertFalse(collector.alert.is_set())
        self.assertTrue(collector.snapshot()['privateEvidence']['complete'])
        self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 1)
        self.assertNotIn('Please update', json.dumps(collector.snapshot()))
        self.assertFalse(collector.auxiliary_alive())

    def test_advisory_watchdog_blocks_child_and_evidence_collection(self):
        collector = IncrementalCollector(io.BytesIO(b''), _vulkan_advisory=True)
        collector.thread.start();collector.thread.join(2)
        child = birth._Child()
        child.collectors = [collector]
        child.process = SimpleNamespace(poll=lambda:0)
        with patch.object(collector._watchdog, 'is_alive', return_value=True), \
             patch.object(birth._ProcessOwner, '_terminate', return_value=True), \
             patch.object(collector.thread, 'join', wraps=collector.thread.join) as join:
            self.assertFalse(child.collect(time.monotonic()+2))
            self.assertGreaterEqual(join.call_args.args[0], 0)
            self.assertLessEqual(join.call_args.args[0], 2)
        sink = self.sink()
        collector._evidence = sink
        child._evidence = [sink]
        with patch.object(collector._watchdog, 'is_alive', return_value=True):
            self.assertFalse(birth._finish_private_evidence(SimpleNamespace(_children=[child])))
            self.assertIsNotNone(sink.fd)
        self.assertTrue(birth._finish_private_evidence(SimpleNamespace(_children=[child])))

    def test_real_child_opt_in_is_stdout_only(self):
        from reproof.live.android_recovery_diagnostics import _VULKAN_ADVISORY
        process = SimpleNamespace(stdout=io.BytesIO(_VULKAN_ADVISORY),
                                  stderr=io.BytesIO(_VULKAN_ADVISORY), poll=lambda:0)
        child = birth._Child();child._vulkan_advisory = True
        with patch.object(birth.subprocess, 'Popen', return_value=process):
            child.start(('synthetic-not-run',), {}, self.root)
        for collector in child.collectors:collector.thread.join(2)
        self.assertEqual([c.snapshot()['vulkanAdvisoriesAccepted'] for c in child.collectors], [1, 0])
        self.assertFalse(child.collectors[0].alert.is_set())
        self.assertTrue(child.collectors[1].alert.is_set())
        self.assertTrue(all(not c.auxiliary_alive() for c in child.collectors))

    def test_persistence_has_same_fixed_cap_while_reader_drains(self):
        sink = self.sink()
        collector = IncrementalCollector(io.BytesIO(b'x'*200000), evidence=sink)
        collector.thread.start();collector.thread.join(2)
        self.assertFalse(collector.thread.is_alive())
        self.assertEqual((self.root/'credentials/0-stderr.bin').stat().st_size, _MAXIMUM)
        self.assertEqual(sink.bytes_written, _MAXIMUM)
        self.assertTrue(collector.snapshot()['truncated'])
        self.assertFalse(collector.snapshot()['complete'])

    def test_existing_symlink_or_file_never_overwritten(self):
        foreign = self.root/'foreign';foreign.write_bytes(b'keep')
        path = self.root/'credentials/0-stderr.bin';path.symlink_to(foreign)
        with self.assertRaises(OSError):self.sink()
        self.assertEqual(foreign.read_bytes(), b'keep')
        path.unlink();path.write_bytes(b'keep local')
        with self.assertRaises(OSError):self.sink()
        self.assertEqual(path.read_bytes(), b'keep local')

    def test_replaced_path_directory_and_hardlink_are_preserved(self):
        for mode in ('file','directory','hardlink'):
            with self.subTest(mode=mode):
                name = str(len(self.sinks))+'-stderr.bin'
                sink = self.sink(name)
                path = self.root/'credentials'/name
                if mode == 'file':
                    path.rename(path.with_suffix('.retained'))
                    path.write_bytes(b'foreign')
                elif mode == 'directory':
                    (self.root/'credentials').rename(self.root/'old-evidence')
                    (self.root/'credentials').mkdir(mode=0o700)
                    (self.root/'credentials'/name).write_bytes(b'foreign')
                else:
                    os.link(path, self.root/'linked')
                self.assertFalse(sink.append(b'new'))
                self.assertFalse(sink.finish())
                if mode in ('file','directory'):
                    self.assertEqual((self.root/'credentials'/name).read_bytes(), b'foreign')
                if mode == 'directory':
                    # Restore our test namespace, not production cleanup.
                    (self.root/'credentials'/name).unlink()
                    (self.root/'credentials').rmdir()
                    (self.root/'old-evidence').rename(self.root/'credentials')

    def test_warning_alert_precedes_disk_write_and_disk_error_latches(self):
        sink = self.sink()
        collector = IncrementalCollector(io.BytesIO(b'WARNING synthetic'), evidence=sink)
        original = sink.append
        def append(raw):
            self.assertTrue(collector.alert.is_set())
            return original(raw)
        with patch.object(sink, 'append', append), patch('os.write', side_effect=OSError(errno.ENOSPC, 'private path')):
            collector.thread.start();collector.thread.join(2)
        self.assertTrue(collector.evidence_failed)
        self.assertTrue(collector.snapshot()['privateEvidence']['incomplete'])
        self.assertNotIn('private path', json.dumps(collector.snapshot()))

    def test_partial_write_finishes_and_close_failure_never_retries_reused_fd(self):
        sink = self.sink();write = os.write
        with patch('os.write', side_effect=lambda fd, data:write(fd, data[:2])):
            self.assertTrue(sink.append(b'abcdef'))
        original_fd = sink.fd
        close = os.close
        replacement = []
        def close_and_raise(fd):
            close(fd)
            if fd == original_fd:
                replacement.append(os.open(self.root/'replacement', os.O_CREAT|os.O_RDWR, 0o600))
                raise OSError('synthetic consumed close')
        with patch('os.close', close_and_raise):self.assertFalse(sink.finish())
        self.assertFalse(sink.finish())
        self.assertEqual(sink.fd, None)
        try:
            os.write(replacement[0], b'preserved')
        finally:
            close(replacement[0])

    def test_parent_does_not_close_live_writer_and_finishes_unstarted_sink(self):
        sink = self.sink()
        read, write = os.pipe()
        collector = IncrementalCollector(os.fdopen(read, 'rb'), evidence=sink)
        child = SimpleNamespace(_evidence=[sink], collectors=[collector])
        owner = SimpleNamespace(_children=[child])
        collector.thread.start()
        try:
            self.assertFalse(birth._finish_private_evidence(owner))
            self.assertIsNotNone(sink.fd)
        finally:
            os.close(write);collector.thread.join(2)
        self.assertTrue(birth._finish_private_evidence(owner))
        unstarted = self.sink('1-stderr.bin')
        self.assertTrue(birth._finish_private_evidence(SimpleNamespace(
            _children=[SimpleNamespace(_evidence=[unstarted], collectors=[])])))
        self.assertIsNone(unstarted.fd)

    def test_popen_failure_retains_empty_files_and_closes_only_owned_sinks(self):
        owner = SimpleNamespace(root=self.root, _evidence_root_fd=self.root_fd,
            _evidence_directory_fd=self.dir_fd, _children=[])
        child = birth._new_private_child(owner, 'server')
        self.sinks.extend(child._evidence)
        with patch.object(birth.subprocess, 'Popen', side_effect=OSError('synthetic start failed')):
            with self.assertRaises(OSError):child.start(('never-run',), {}, self.root)
        self.assertTrue(birth._finish_private_evidence(owner))
        self.assertEqual(len(list((self.root/'credentials').iterdir())), 2)
        self.assertTrue(all(sink.fd is None for sink in child._evidence))

    def test_fsync_failure_is_incomplete_and_not_prematurely_complete(self):
        sink = self.sink();self.assertTrue(sink.append(b'synthetic'))
        def fail(_):
            self.assertFalse(sink.summary()['complete'])
            raise OSError(errno.EIO, 'synthetic sync error')
        with patch('os.fsync', side_effect=fail):
            self.assertFalse(sink.finish())
        self.assertTrue(sink.summary()['incomplete'])
        self.assertIsNone(sink.fd)

    def test_sensitive_evidence_path_is_not_a_transfer_or_blob_input(self):
        from reproof.execution.wire import safe_transfer_path
        from reproof.execution.artifacts import BlobSet, ArtifactError
        from reproof.source_snapshot import capture, SnapshotError
        path = 'credentials/0-stderr.bin'
        with self.assertRaises(ContractError):safe_transfer_path(path)
        with self.assertRaises(ArtifactError):BlobSet(((path, b'synthetic private output'),))
        with self.assertRaises(SnapshotError):capture(self.root, (path,), self.root/'snapshot')
        self.assertFalse((self.root/'snapshot').exists())

    def test_fixed_names_and_four_child_budget(self):
        for name in ('../escape','4-stderr.bin','0-secret.bin','0-stderr.bin/extra'):
            with self.assertRaises(ContractError):self.sink(name)
        owner = SimpleNamespace(_children=[None]*4)
        with self.assertRaises(ContractError):birth._new_private_child(owner, 'server')
