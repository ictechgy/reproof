import sqlite3
from pathlib import Path
import tempfile
import unittest

from reproof.live.disk_budget import DiskBudget, DiskBudgetError


class DiskBudgetCapacityGrowthTests(unittest.TestCase):
    def test_capacity_change_requires_explicit_growth_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = DiskBudget(root, capacity_bytes=1_048_576,
                                  journal_headroom_bytes=65_536,
                                  free_bytes=lambda: 1 << 30)
            original.close()
            with self.assertRaises(DiskBudgetError):
                DiskBudget(root, capacity_bytes=2_097_152,
                           journal_headroom_bytes=65_536,
                           free_bytes=lambda: 1 << 30)

    def test_growth_adopts_live_and_committed_reservations_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = DiskBudget(root, capacity_bytes=1_048_576,
                                  journal_headroom_bytes=65_536,
                                  free_bytes=lambda: 1 << 30)
            active = original.reserve("active_owner", "spool", 80_000)
            committed = original.reserve("committed_owner", "journal", 70_000)
            committed.commit(60_000)
            before = original.reservations_for_owner("active_owner", category="spool") + \
                original.reservations_for_owner("committed_owner", category="journal")
            original.close()
            grown = DiskBudget(root, capacity_bytes=2_097_152,
                               journal_headroom_bytes=65_536,
                               free_bytes=lambda: 1 << 30,
                               _allow_capacity_growth=True)
            try:
                after = grown.reservations_for_owner("active_owner", category="spool") + \
                    grown.reservations_for_owner("committed_owner", category="journal")
                self.assertEqual(after, before)
                self.assertEqual(grown.capacity_bytes, 2_097_152)
                self.assertEqual(grown.metadata_headroom_bytes, 524_288)
            finally:
                grown.release_id(active.reservation_id)
                grown.release_id(committed.reservation_id)
                grown.close()

    def test_growth_rejects_shrink_and_changed_fixed_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = DiskBudget(root, capacity_bytes=1_048_576,
                                  journal_headroom_bytes=65_536,
                                  free_bytes=lambda: 1 << 30)
            original.close()
            with self.assertRaises(DiskBudgetError):
                DiskBudget(root, capacity_bytes=524_288,
                           journal_headroom_bytes=65_536,
                           free_bytes=lambda: 1 << 30,
                           _allow_capacity_growth=True)
            connection = sqlite3.connect(root / "disk-budget.sqlite3")
            connection.execute("UPDATE metadata SET value=123 WHERE key='metadata_headroom_bytes'")
            connection.commit()
            connection.close()
            with self.assertRaises(DiskBudgetError):
                DiskBudget(root, capacity_bytes=2_097_152,
                           journal_headroom_bytes=65_536,
                           free_bytes=lambda: 1 << 30,
                           _allow_capacity_growth=True)

    def test_default_scale_growth_adopts_existing_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = DiskBudget(root, capacity_bytes=512 * 1024 * 1024,
                                  journal_headroom_bytes=8 * 1024 * 1024,
                                  free_bytes=lambda: 2 * 1024 * 1024 * 1024)
            reservation = original.reserve("repair_owner", "encoding", 128 * 1024 * 1024)
            identifier = reservation.reservation_id
            original.close()
            grown = DiskBudget(root, capacity_bytes=1024 * 1024 * 1024,
                               journal_headroom_bytes=8 * 1024 * 1024,
                               free_bytes=lambda: 2 * 1024 * 1024 * 1024,
                               _allow_capacity_growth=True)
            try:
                rows = grown.reservations_for_owner("repair_owner", category="encoding")
                self.assertEqual(rows[0]["reservation_id"], identifier)
                self.assertEqual(rows[0]["charged_bytes"], 128 * 1024 * 1024)
            finally:
                grown.release_id(identifier)
                grown.close()


if __name__ == "__main__":
    unittest.main()
