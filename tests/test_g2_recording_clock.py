import unittest

from reproloop.live.clock_sync import (
    ClockSynchronizer,
    RecordingClockError,
    RecordingTimeAnchor,
)
from tests.test_clock_sync import FakeClock


class RecordingTimeAnchorTests(unittest.TestCase):
    def test_capture_interval_includes_acquisition_duration_separately_from_clock_error(self):
        clock = FakeClock(1_000_000_000)
        sync = ClockSynchronizer(clock)
        anchor = RecordingTimeAnchor(sync, wall_clock_ms=lambda: 5_000_000)
        sent = sync.sample()
        clock.advance(10_000_000)
        mapping = sync.record_probe(
            coordinator_clock_id="native-clock", coordinator_ns=900_000_000,
            host_sent=sent, host_received=sync.sample(), max_drift_ppm=1000)
        binding = anchor.bind_provider(mapping, provider_boot_digest="b" * 64,
                                       native_incarnation="native_one")
        clock.advance(700_000_000)
        stamp = anchor.stamp_provider(binding, 1_500_000_000,
                                      capture_start_ns=950_000_000)
        # Capture lasted 550 ms, while synchronization error remains < 13 ms.
        self.assertLessEqual(stamp.earliest_offset_ms, 50)
        self.assertGreaterEqual(stamp.latest_offset_ms, 610)
        self.assertGreater(stamp.uncertainty_ns, 550_000_000)
        self.assertLess(stamp.uncertainty_ns, 563_000_000)
        with self.assertRaises(RecordingClockError):
            anchor.stamp_provider(binding, 1_600_000_000,
                                  capture_start_ns=1_700_000_000)
        with self.assertRaises(RecordingClockError):
            anchor.stamp_provider(binding, 1_600_000_000,
                                  capture_start_ns=1_400_000_000)

    def test_wall_clock_correction_cannot_rewrite_elapsed_order(self):
        clock = FakeClock(1_000_000_000)
        wall = [2_000_000]
        sync = ClockSynchronizer(clock)
        anchor = RecordingTimeAnchor(sync, wall_clock_ms=lambda: wall[0])
        first = anchor.stamp()
        wall[0] = 1
        clock.advance(2_000_000_000)
        second = anchor.stamp()
        self.assertEqual(first.offset_ms, 0)
        self.assertEqual(second.offset_ms, 2000)
        self.assertEqual(second.display_ms, 2_002_000)

    def test_provider_monotonic_timestamp_maps_to_conservative_interval(self):
        clock = FakeClock(1_000_000_000)
        sync = ClockSynchronizer(clock, max_mapping_uncertainty_ns=100_000_000)
        anchor = RecordingTimeAnchor(sync, wall_clock_ms=lambda: 5_000_000)
        received = sync.sample()
        clock.advance(10_000_000)
        sent = sync.sample()
        mapping = sync.record_exchange(
            coordinator_clock_id="native-clock",
            coordinator_send_ns=900_000_000,
            host_received=received,
            host_sent=sent,
            coordinator_receive_ns=930_000_000,
            coordinator_uncertainty_ns=1_000_000,
            max_drift_ppm=1000,
        )
        binding = anchor.bind_provider(
            mapping, provider_boot_digest="b" * 64,
            native_incarnation="native_one",
        )
        stamp = anchor.stamp_provider(binding, 950_000_000)
        self.assertLessEqual(stamp.earliest_offset_ms, stamp.presentation_offset_ms)
        self.assertLessEqual(stamp.presentation_offset_ms, stamp.latest_offset_ms)
        self.assertGreater(stamp.uncertainty_ns, 0)
        self.assertEqual(stamp.provider_clock_id, "native-clock")

    def test_restart_and_monotonic_discontinuity_invalidate_anchor(self):
        clock = FakeClock(1_000_000_000)
        sync = ClockSynchronizer(clock)
        anchor = RecordingTimeAnchor(sync, wall_clock_ms=lambda: 5_000_000)
        clock.boot_digest = "c" * 64
        with self.assertRaises(RecordingClockError):
            anchor.stamp()
        clock.boot_digest = "a" * 64
        clock.nanoseconds = 2_000_000_000
        with self.assertRaises(RecordingClockError):
            anchor.stamp()

    def test_sleep_horizon_and_explicit_native_restart_invalidate_provider_mapping(self):
        clock = FakeClock(1_000_000_000)
        sync = ClockSynchronizer(clock, max_mapping_uncertainty_ns=50_000_000)
        anchor = RecordingTimeAnchor(sync, wall_clock_ms=lambda: 5_000_000)
        received = sync.sample()
        clock.advance(1_000_000)
        sent = sync.sample()
        mapping = sync.record_exchange(
            coordinator_clock_id="native-clock", coordinator_send_ns=900_000_000,
            host_received=received, host_sent=sent,
            coordinator_receive_ns=902_000_000, max_drift_ppm=10_000)
        binding = anchor.bind_provider(mapping, provider_boot_digest="d" * 64,
                                       native_incarnation="native_one")
        with self.assertRaises(RecordingClockError):
            anchor.stamp_provider(binding, 20_000_000_000)
        anchor.invalidate_provider_mappings()
        with self.assertRaises(RecordingClockError):
            anchor.stamp_provider(binding, 910_000_000)

    def test_provider_clock_cannot_move_backward_or_change_incarnation_silently(self):
        clock = FakeClock(1_000_000_000)
        sync = ClockSynchronizer(clock, max_mapping_uncertainty_ns=100_000_000)
        anchor = RecordingTimeAnchor(sync, wall_clock_ms=lambda: 5_000_000)
        received = sync.sample()
        clock.advance(1_000_000)
        sent = sync.sample()
        mapping = sync.record_exchange(
            coordinator_clock_id="native-clock", coordinator_send_ns=900_000_000,
            host_received=received, host_sent=sent,
            coordinator_receive_ns=902_000_000, max_drift_ppm=1000,
        )
        binding = anchor.bind_provider(
            mapping, provider_boot_digest="d" * 64,
            native_incarnation="native_one",
        )
        anchor.stamp_provider(binding, 910_000_000)
        with self.assertRaises(RecordingClockError):
            anchor.stamp_provider(binding, 909_000_000)
        with self.assertRaises(RecordingClockError):
            anchor.bind_provider(
                mapping, provider_boot_digest="e" * 64,
                native_incarnation="native_two",
            )
        anchor.invalidate_provider_mappings()
        replacement = anchor.bind_provider(
            mapping, provider_boot_digest="e" * 64,
            native_incarnation="native_two",
        )
        self.assertEqual(replacement.native_incarnation, "native_two")


if __name__ == "__main__":
    unittest.main()
