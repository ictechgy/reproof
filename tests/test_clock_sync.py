import time
import unittest
from dataclasses import replace

from reproloop.core import ContractError
from reproloop.live.clock_sync import (
    ClockReading,
    ClockSynchronizer,
    SuspendInclusiveClock,
    UnavailableClock,
)


class FakeClock:
    def __init__(self, nanoseconds=1_000_000, boot_digest="a" * 64):
        self.clock_id = "test-suspend-clock"
        self.boot_digest = boot_digest
        self.nanoseconds = nanoseconds
        self.uncertainty_ns = 2

    def read(self):
        return ClockReading(
            clock_id=self.clock_id,
            boot_digest=self.boot_digest,
            nanoseconds=self.nanoseconds,
            uncertainty_ns=self.uncertainty_ns,
        )

    def advance(self, nanoseconds):
        self.nanoseconds += nanoseconds


class ClockSyncTests(unittest.TestCase):
    def test_host_probe_contains_actual_remote_sample_and_checks_issuer(self):
        clock = FakeClock(1_000_000_000)
        sync = ClockSynchronizer(clock)
        sent = sync.sample()
        clock.advance(20_000_000)
        received = sync.sample()
        mapping = sync.record_probe(
            coordinator_clock_id="native-clock", coordinator_ns=500_000_000,
            host_sent=sent, host_received=received,
            coordinator_uncertainty_ns=1_000_000, max_drift_ppm=1000)
        interval = mapping.translate(500_000_000)
        self.assertLessEqual(interval.earliest_ns, sent.nanoseconds)
        self.assertGreaterEqual(interval.latest_ns, received.nanoseconds)
        self.assertLess(interval.uncertainty_ns, 23_000_000)
        sync.require_mapping(mapping)
        with self.assertRaises(ContractError):
            sync.require_mapping(replace(mapping, measurement="peer-exchange"))
        with self.assertRaises(ContractError):
            sync.record_probe(coordinator_clock_id="native-clock", coordinator_ns=1,
                              host_sent=received, host_received=sent)
        clock.boot_digest = "b" * 64
        sync.sample()
        with self.assertRaises(ContractError):
            sync.record_probe(coordinator_clock_id="native-clock", coordinator_ns=1,
                              host_sent=sent, host_received=received)

    def test_slow_or_untrusted_probe_cannot_create_accurate_mapping(self):
        clock = FakeClock(1_000_000_000)
        sync = ClockSynchronizer(clock)
        sent = sync.sample()
        clock.advance(300_000_000)
        received = sync.sample()
        with self.assertRaisesRegex(ContractError, "uncertainty is excessive"):
            sync.record_probe(coordinator_clock_id="native-clock", coordinator_ns=1,
                              host_sent=sent, host_received=received)
        with self.assertRaises(ContractError):
            sync.record_probe(coordinator_clock_id="native-clock", coordinator_ns=1,
                              host_sent=sent.reading, host_received=received)

    def test_exchange_produces_conservative_translation_with_drift(self):
        clock = FakeClock(150)
        sync = ClockSynchronizer(clock)
        received = sync.sample()
        clock.nanoseconds = 160
        sent = sync.sample()

        mapping = sync.record_exchange(
            coordinator_clock_id="coordinator-clock",
            coordinator_send_ns=100,
            host_received=received,
            host_sent=sent,
            coordinator_receive_ns=130,
            coordinator_uncertainty_ns=0,
            max_drift_ppm=1_000,
        )
        interval = mapping.translate(200)

        # Without uncertainty or drift, the offset is in [30, 50].  The
        # injected local reading uncertainty and drift may only widen it.
        self.assertLessEqual(interval.earliest_ns, 230)
        self.assertGreaterEqual(interval.latest_ns, 250)
        self.assertLessEqual(interval.earliest_ns, interval.latest_ns)
        mapping.require_compatible(sync.sample())

    def test_boot_change_and_monotonic_regression_invalidate_mapping(self):
        clock = FakeClock()
        sync = ClockSynchronizer(clock)
        first = sync.sample()
        clock.advance(10)
        second = sync.sample()
        mapping = sync.record_exchange(
            coordinator_clock_id="coordinator-clock",
            coordinator_send_ns=900_000,
            host_received=first,
            host_sent=second,
            coordinator_receive_ns=900_020,
            max_drift_ppm=100,
        )

        clock.boot_digest = "b" * 64
        with self.assertRaisesRegex(ContractError, "Clock mapping is incompatible"):
            mapping.require_compatible(sync.sample())
        clock.boot_digest = "a" * 64
        clock.nanoseconds = 1
        with self.assertRaisesRegex(ContractError, "Clock mapping is incompatible"):
            mapping.require_compatible(sync.sample())

    def test_unknown_clock_cannot_create_trusted_samples(self):
        sync = ClockSynchronizer(UnavailableClock())
        with self.assertRaisesRegex(ContractError, "Suspend-inclusive clock is unavailable"):
            sync.sample()

    def test_real_suspend_inclusive_clock_has_stable_boot_and_advances(self):
        clock = SuspendInclusiveClock()
        first = clock.read()
        deadline = time.monotonic() + 0.2
        second = clock.read()
        while second.nanoseconds <= first.nanoseconds and time.monotonic() < deadline:
            second = clock.read()
        self.assertEqual(first.clock_id, second.clock_id)
        self.assertEqual(first.boot_digest, second.boot_digest)
        self.assertGreater(second.nanoseconds, first.nanoseconds)

    def test_reordered_or_excessively_broad_exchange_is_rejected(self):
        clock = FakeClock(200)
        sync = ClockSynchronizer(clock, max_mapping_uncertainty_ns=25)
        received = sync.sample()
        clock.advance(10)
        sent = sync.sample()
        with self.assertRaises(ContractError):
            sync.record_exchange(
                coordinator_clock_id="coordinator-clock",
                coordinator_send_ns=300,
                host_received=received,
                host_sent=sent,
                coordinator_receive_ns=200,
            )
        with self.assertRaisesRegex(ContractError, "Clock mapping uncertainty is excessive"):
            sync.record_exchange(
                coordinator_clock_id="coordinator-clock",
                coordinator_send_ns=0,
                host_received=received,
                host_sent=sent,
                coordinator_receive_ns=1_000,
            )


if __name__ == "__main__":
    unittest.main()
