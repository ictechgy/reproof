"""Byte identity checks only: no downloaded binary is executed or installed."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof.live import android_recovery_tool_pin as pin


class RecoveryToolPinTests(unittest.TestCase):
    def test_release_metadata_is_fixed_and_not_latest(self):
        self.assertEqual(pin.ADB_RELEASE, '35.0.2')
        self.assertEqual(pin.ADB_BUILD, '12147458')
        self.assertEqual(pin.ADB_ARCHIVE_URL, 'https://dl.google.com/android/repository/platform-tools_r35.0.2-darwin.zip')
        self.assertEqual(len(pin.ADB_ARCHIVE_SHA256), 64)
        self.assertEqual(len(pin.ADB_DARWIN_SHA256), 64)
        self.assertEqual(len(pin.ADB_SOURCE_COMMIT), 40)

    def test_actual_file_bytes_not_declared_digest_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / 'inert-adb'; raw = b'not an executable'
            path.write_bytes(raw)
            with self.assertRaises(ContractError):
                pin.require_pinned_adb(path)
            # Synthetic constants only exercise the same bounded read algorithm.
            with patch.object(pin, 'ADB_DARWIN_BYTES', len(raw)), patch.object(
                    pin, 'ADB_DARWIN_SHA256', hashlib.sha256(raw).hexdigest()):
                self.assertEqual(pin.require_pinned_adb(path), hashlib.sha256(raw).hexdigest())
                path.write_bytes(b'x' * len(raw))
                with self.assertRaises(ContractError):
                    pin.require_pinned_adb(path)

    def test_links_and_relative_paths_do_not_select_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'target'; target.write_bytes(b'inert')
            linked = root / 'linked'; linked.symlink_to(target)
            for selected in (linked, Path('relative-adb')):
                with self.assertRaises(ContractError):
                    pin.require_pinned_adb(selected)
