"""Independent checks for invalidation between synchronization exchanges."""

import sys
import unittest
from unittest.mock import patch

from reproloop.core import ContractError
from reproloop.live.clock_sync import ClockSynchronizer, SuspendInclusiveClock
from tests.test_clock_sync import FakeClock


class ParentClockBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.sync = ClockSynchronizer(self.clock)
        received = self.sync.sample()
        self.clock.advance(10)
        sent = self.sync.sample()
        self.mapping = self.sync.record_exchange(
            coordinator_clock_id="coordinator-clock",
            coordinator_send_ns=received.nanoseconds - 100,
            host_received=received, host_sent=sent,
            coordinator_receive_ns=sent.nanoseconds - 80,
        )

    def test_regression_above_exchange_timestamp_invalidates_old_mapping(self):
        self.clock.advance(1_000)
        self.mapping.require_compatible(self.sync.sample())
        self.clock.advance(-1)
        with self.assertRaises(ContractError):
            self.mapping.require_compatible(self.sync.sample())
        self.clock.advance(10_000)
        with self.assertRaises(ContractError):
            self.mapping.require_compatible(self.sync.sample())

    def test_observed_boot_change_cannot_revalidate_an_old_mapping(self):
        original_boot = self.clock.boot_digest
        self.clock.boot_digest = "b" * 64
        with self.assertRaises(ContractError):
            self.mapping.require_compatible(self.sync.sample())
        self.clock.boot_digest = original_boot
        self.clock.advance(100)
        with self.assertRaises(ContractError):
            self.mapping.require_compatible(self.sync.sample())

    def test_foreign_samples_do_not_gain_authority_from_equal_values(self):
        foreign = ClockSynchronizer(self.clock)
        received = foreign.sample()
        self.clock.advance(10)
        sent = foreign.sample()
        with self.assertRaises(ContractError):
            self.sync.record_exchange(
                coordinator_clock_id="coordinator-clock",
                coordinator_send_ns=received.nanoseconds - 100,
                host_received=received, host_sent=sent,
                coordinator_receive_ns=sent.nanoseconds - 80,
            )

    @unittest.skipUnless(sys.platform == "darwin", "macOS boot identity boundary")
    def test_unavailable_boot_identity_cannot_be_estimated_into_authority(self):
        with patch("reproloop.live.clock_sync._darwin_sysctl", side_effect=OSError):
            with self.assertRaises(ContractError):
                SuspendInclusiveClock()

    @unittest.skipUnless(sys.platform == "darwin", "macOS boot identity boundary")
    def test_empty_boot_identity_is_not_shared_by_every_boot(self):
        with patch("reproloop.live.clock_sync._darwin_sysctl", return_value=b""):
            with self.assertRaises(ContractError):
                SuspendInclusiveClock()


if __name__ == "__main__":
    unittest.main()
