"""Public synthetic streams only; no subprocess, socket, Android or keys."""
import io
import json
import os
import threading
from types import SimpleNamespace
import unittest

from reproof.live.android_recovery_diagnostics import (
    IncrementalCollector, OutputCancellation, birth_summary, inventory_state)


class DiagnosticsTests(unittest.TestCase):
    def test_short_warning_is_seen_before_pipe_writer_closes(self):
        read, write = os.pipe()
        collector = IncrementalCollector(os.fdopen(read, 'rb'))
        collector.thread.start()
        try:
            os.write(write, b'WARNING: synthetic short diagnostic')
            self.assertTrue(collector.alert.wait(1), 'Warning was buffered until EOF')
            self.assertTrue(collector.thread.is_alive())
            self.assertEqual(collector.snapshot()['classifications'], ['warning'])
            self.assertFalse(collector.snapshot()['complete'])
        finally:
            os.close(write)
            collector.thread.join(2)
        self.assertFalse(collector.thread.is_alive())
        self.assertTrue(collector.snapshot()['complete'])

    def collect(self, raw, maximum=65536):
        collector = IncrementalCollector(io.BytesIO(raw), maximum)
        collector.thread.start();collector.thread.join(2)
        self.assertFalse(collector.thread.is_alive())
        return collector

    def test_split_warning_and_permission_denial_are_classified(self):
        class Stream(io.BytesIO):
            def read1(self, _):
                return super().read1(3)
        collector = IncrementalCollector(Stream(b'Operation not permitted\nWARNING: private path\n'))
        collector.thread.start();collector.thread.join(2)
        self.assertEqual(collector.snapshot()['classifications'], ['permission-denied','warning'])

    def test_public_report_has_no_raw_keys_paths_or_foreign_identifiers(self):
        private = b'adb public key: synthetic-public-key material /private/path foreign-device-id\n-----BEGIN PRIVATE KEY----- synthetic-only\n'
        collector = self.collect(private)
        owner = SimpleNamespace(_children=[SimpleNamespace(collectors=[collector],
            process=SimpleNamespace(returncode=1), _diagnostic_role='emulator')],
            _inventory_state=inventory_state(b'foreign-device-id\tdevice\n', 'emulator-5554'),
            _inventory_polls=3, _inventory_query='returned', _stage='inventory')
        encoded = json.dumps(birth_summary(owner))
        for value in ('synthetic-public-key', '/private/path', 'foreign-device-id', 'adb public key', 'BEGIN PRIVATE KEY'):
            self.assertNotIn(value, encoded)
        self.assertIn('unexpected', encoded)
        self.assertFalse(collector.alert.is_set())
        self.assertLess(len(encoded), 4096)

    def test_read_failure_and_output_limit_latch_without_unbounded_storage(self):
        collector = self.collect(b'x'*100000, maximum=64)
        self.assertEqual(len(collector.data), 65)
        self.assertTrue(collector.alert.is_set())
        self.assertTrue(collector.snapshot()['truncated'])
        self.assertFalse(collector.snapshot()['complete'])
        class Broken(io.BytesIO):
            def read1(self, _):raise OSError('private path/key must not escape')
        broken = IncrementalCollector(Broken())
        broken.thread.start();broken.thread.join(2)
        self.assertTrue(broken.snapshot()['readFailed'])
        self.assertNotIn('private path', str(broken.snapshot()))

    def test_warning_composite_cancels_without_mutating_user_event(self):
        event = threading.Event()
        collector = self.collect(b'ERROR: synthetic\n')
        child = SimpleNamespace(collectors=[collector])
        self.assertTrue(OutputCancellation(event, [child]).is_set())
        self.assertFalse(event.is_set())
        clean = self.collect(b'INFO: boot complete\n')
        self.assertFalse(OutputCancellation(event, [SimpleNamespace(collectors=[clean])]).is_set())
        event.set()
        self.assertTrue(OutputCancellation(event, []).is_set())

    def test_bare_denial_codes_are_classified(self):
        for value in (b'EPERM', b'EACCES'):
            self.assertEqual(self.collect(value).snapshot()['classifications'], ['permission-denied'])

    def test_unsupported_options_fail_without_severity_prefix(self):
        collector = self.collect(b'Unknown feature: synthetic-option')
        self.assertTrue(collector.alert.is_set())
        self.assertEqual(collector.snapshot()['classifications'], ['unsupported-option'])

    def test_inventory_has_only_fixed_outcomes(self):
        for raw, expected in ((b'', 'empty'), (b'emulator-5554\tdevice\n','selected-device'),
            (b'emulator-5554\toffline\n','selected-offline'), (b'emulator-5554\tunauthorized\n','unexpected'),
            (b'secret\tdevice\n','unexpected')):
            self.assertEqual(inventory_state(raw, 'emulator-5554'), expected)
