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


class VulkanAdvisoryTests(unittest.TestCase):
    """One public line, synthetic pipes only; no native execution."""
    def collect(self, chunks, *, enabled=True, maximum=65536):
        class Stream:
            def __init__(self):self.chunks = iter(chunks)
            def read1(self, _):return next(self.chunks, b'')
            def close(self):pass
        collector = IncrementalCollector(Stream(), maximum, _vulkan_advisory=enabled)
        collector.thread.start();collector.thread.join(2)
        self.assertFalse(collector.thread.is_alive())
        self.assertFalse(collector.auxiliary_alive())
        return collector

    def line(self):
        from reproof.live.android_recovery_diagnostics import _VULKAN_ADVISORY
        return _VULKAN_ADVISORY

    def test_default_still_rejects_exact_advisory(self):
        collector = self.collect([self.line()], enabled=False)
        self.assertTrue(collector.alert.is_set())
        self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 0)
        self.assertIsNone(collector._watchdog)

    def test_exact_public_line_pin_and_every_two_chunk_boundary(self):
        import hashlib
        line = self.line()
        self.assertEqual(len(line), 98)
        self.assertEqual(hashlib.sha256(line).hexdigest(),
                         '0472c5345c5a3f062f21e1ddcf985991ad3f3b536c48efac97acbd9a2c442352')
        for index in range(1, len(line)):
            with self.subTest(index=index):
                collector = self.collect([line[:index], line[index:], b'INFO: ready\n'])
                self.assertFalse(collector.alert.is_set())
                self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 1)
                self.assertEqual(bytes(collector.data), line+b'INFO: ready\n')
                self.assertTrue(collector.snapshot()['complete'])

    def test_one_byte_chunks_and_prior_line_boundary(self):
        raw = b'INFO: prior\n'+self.line()+b'INFO: after\n'
        collector = self.collect([bytes((byte,)) for byte in raw])
        self.assertFalse(collector.alert.is_set())
        self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 1)
        self.assertEqual(bytes(collector.data), raw)

    def test_embedded_advisory_is_not_a_line(self):
        for prefix in (b'x', b' ', b'\r', b'\x00', b'INFO: '):
            with self.subTest(prefix=prefix):
                collector = self.collect([prefix+self.line()])
                self.assertTrue(collector.alert.is_set())
                self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 0)

    def test_changed_line_or_extra_feature_never_matches(self):
        line = self.line()
        for raw in (line.replace(b'WARNING', b'Warning'), line.replace(b'      |', b'     |'),
                    line[:-1]+b'\r\n', line[:-1]+b' extra\n',
                    line.replace(b'VulkanVirtualQueue', b'VulkanVirtualQueue, Other'),
                    line.replace(b'VulkanVirtualQueue', b'Other')):
            with self.subTest(raw=raw):
                collector = self.collect([raw])
                self.assertTrue(collector.alert.is_set())
                self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 0)

    def test_every_single_byte_advisory_mutation_rejects(self):
        line = self.line()
        for index in range(len(line)):
            variants = (line[:index]+b'X'+line[index+1:],
                        line[:index]+line[index+1:],
                        line[:index]+b'X'+line[index:])
            for variant in variants:
                with self.subTest(index=index, variant=variant):
                    collector = self.collect([variant])
                    self.assertTrue(collector.alert.is_set())
                    self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 0)

    def test_body_marker_alone_rejects_in_default_mode_across_splits(self):
        marker = b'Please update the emulator to one that supports the feature(s):'
        self.assertLessEqual(len(marker), 65)
        for index in range(1, len(marker)):
            with self.subTest(index=index):
                collector = self.collect([marker[:index], marker[index:]], enabled=False)
                self.assertTrue(collector.alert.is_set())
                self.assertIn('unsupported-feature', collector.snapshot()['classifications'])
        self.assertFalse(self.collect([b'Welcome to a synthetic app\n']).alert.is_set())

    def test_duplicate_and_adjacent_warning_error_permission_still_latch(self):
        line = self.line()
        for warning in (line, b'WARNING: other\n', b'ERROR: failed\n',
                        b'Permission denied\n', b'Unknown option x\n', b'FATAL x\n'):
            for raw in (line+warning, warning+line):
                with self.subTest(raw=raw):
                    collector = self.collect([raw])
                    self.assertTrue(collector.alert.is_set())
                    self.assertLessEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 1)

    def test_prior_latched_alert_never_clears_or_accepts_later_line(self):
        collector = self.collect([b'ERROR: before\n', self.line()])
        self.assertTrue(collector.alert.is_set())
        self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 0)

    def test_every_incomplete_prefix_fails_at_eof(self):
        for index in range(1, len(self.line())):
            with self.subTest(index=index):
                collector = self.collect([self.line()[:index]])
                self.assertTrue(collector.alert.is_set())
                self.assertIn('advisory-incomplete', collector.snapshot()['classifications'])
                self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 0)

    def test_stalled_writer_alerts_without_another_read_or_eof(self):
        read, write = os.pipe()
        collector = IncrementalCollector(os.fdopen(read, 'rb'), _vulkan_advisory=True)
        collector.thread.start()
        try:
            os.write(write, self.line()[:18])
            self.assertTrue(collector.alert.wait(2))
            self.assertTrue(collector.thread.is_alive())
            os.write(write, self.line()[18:])
        finally:
            os.close(write);collector.thread.join(2)
        self.assertFalse(collector.auxiliary_alive())
        self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 0)
        self.assertIn('advisory-incomplete', collector.snapshot()['classifications'])

    def test_near_prefix_lines_use_one_watchdog_and_no_unbounded_state(self):
        collector = self.collect([b'Wow\n'*10000, self.line()])
        self.assertFalse(collector.alert.is_set())
        self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 1)
        self.assertEqual(collector._candidate, b'')
        self.assertFalse(collector.auxiliary_alive())

    def test_output_cap_rejects_even_matching_advisory(self):
        collector = self.collect([self.line()], maximum=20)
        self.assertTrue(collector.alert.is_set())
        self.assertFalse(collector.snapshot()['complete'])
        self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 0)

    def test_watchdog_start_failure_is_incomplete(self):
        from unittest.mock import patch
        collector = IncrementalCollector(io.BytesIO(self.line()), _vulkan_advisory=True)
        with patch.object(collector._watchdog, 'start', side_effect=RuntimeError('synthetic')):
            collector.thread.start();collector.thread.join(2)
        self.assertTrue(collector.alert.is_set())
        self.assertTrue(collector.snapshot()['readFailed'])
        self.assertFalse(collector.snapshot()['complete'])
        self.assertFalse(collector.auxiliary_alive())

    def test_watchdog_partial_start_failure_is_owned_and_joined(self):
        from unittest.mock import patch
        collector = IncrementalCollector(io.BytesIO(self.line()), _vulkan_advisory=True)
        start = collector._watchdog.start
        def start_then_fail():
            start()
            raise RuntimeError('synthetic uncertain thread start')
        with patch.object(collector._watchdog, 'start', side_effect=start_then_fail):
            collector.thread.start();collector.thread.join(2)
        self.assertTrue(collector.alert.is_set())
        self.assertTrue(collector.snapshot()['readFailed'])
        self.assertFalse(collector.snapshot()['complete'])
        self.assertFalse(collector.auxiliary_alive())

    def test_read_failure_during_candidate_stops_and_joins_watchdog(self):
        prefix = self.line()[:20]
        class Broken:
            sent = False
            def read1(self, _):
                if not self.sent:
                    self.sent = True
                    return prefix
                raise OSError('synthetic private path')
            def close(self):pass
        collector = IncrementalCollector(Broken(), _vulkan_advisory=True)
        collector.thread.start();collector.thread.join(2)
        self.assertTrue(collector.alert.is_set())
        self.assertTrue(collector.snapshot()['readFailed'])
        self.assertFalse(collector.snapshot()['complete'])
        self.assertFalse(collector.auxiliary_alive())
        self.assertEqual(collector.snapshot()['vulkanAdvisoriesAccepted'], 0)
        self.assertNotIn('private path', json.dumps(collector.snapshot()))

    def test_live_watchdog_blocks_complete_snapshot(self):
        from unittest.mock import patch
        collector = self.collect([self.line()])
        with patch.object(collector._watchdog, 'is_alive', return_value=True):
            self.assertFalse(collector.snapshot()['complete'])
