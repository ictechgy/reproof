"""Conservative recording timestamp probes against production clock APIs."""
import unittest

from reproof.live.clock_sync import ClockSynchronizer, RecordingClockError, RecordingTimeAnchor
from tests.test_clock_sync import FakeClock


class RecordingClockBoundaryTests(unittest.TestCase):
    def test_provider_interval_includes_uncertainty_of_recording_start(self):
        clock = FakeClock(1_000_000_000)
        clock.uncertainty_ns = 10_000_000
        sync = ClockSynchronizer(clock)
        anchor = RecordingTimeAnchor(sync, wall_clock_ms=lambda: 5_000_000)
        clock.advance(100_000_000)
        clock.uncertainty_ns = 0
        received = sync.sample()
        sent = sync.sample()
        mapping = sync.record_exchange(
            coordinator_clock_id="native-clock", coordinator_send_ns=2_000_000_000,
            coordinator_receive_ns=2_000_000_000, host_received=received,
            host_sent=sent, coordinator_uncertainty_ns=0, max_drift_ppm=0)
        binding = anchor.bind_provider(mapping, provider_boot_digest="b" * 64,
                                       native_incarnation="native_one")
        clock.advance(200_000_000)
        stamp = anchor.stamp_provider(binding, 2_200_000_000)
        # Provider instant is host 1.3 s; recording began in [0.99, 1.01] s.
        self.assertLessEqual(stamp.earliest_offset_ms, 290)
        self.assertGreaterEqual(stamp.latest_offset_ms, 310)

    def test_accumulated_drift_cannot_exceed_approved_mapping_uncertainty(self):
        clock = FakeClock(1_000_000_000)
        sync = ClockSynchronizer(clock, max_mapping_uncertainty_ns=10_000_000)
        anchor = RecordingTimeAnchor(sync, wall_clock_ms=lambda: 5_000_000)
        received, sent = sync.sample(), sync.sample()
        mapping = sync.record_exchange(
            coordinator_clock_id="native-clock", coordinator_send_ns=900_000_000,
            coordinator_receive_ns=901_000_000, host_received=received,
            host_sent=sent, coordinator_uncertainty_ns=0, max_drift_ppm=1000)
        binding = anchor.bind_provider(mapping, provider_boot_digest="b" * 64,
                                       native_incarnation="native_one")
        anchor.stamp_provider(binding, 901_000_000)
        clock.advance(20_000_000_000)
        with self.assertRaises(RecordingClockError):
            anchor.stamp_provider(binding, 20_900_000_000)


if __name__ == "__main__":
    unittest.main()
