"""Parent-owned physical-lock path checks using only temporary test files."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from reproof.core import ContractError
from reproof.storage import Lease


class LeasePathBoundaries(unittest.TestCase):
    serial = 'synthetic-parent-lease-probe'
    authority_root = 'a' * 64

    def test_lock_symlink_cannot_truncate_an_unrelated_file(self):
        with tempfile.TemporaryDirectory(prefix='g1b-lease-probe-') as directory:
            root = Path(directory)
            victim = root / 'synthetic-preserved-data'
            original = b'Synthetic data outside the lease lock\n'
            victim.write_bytes(original)
            key = hashlib.sha256(self.serial.encode()).hexdigest()
            (root / (key + '.lock')).symlink_to(victim)
            try:
                with self.assertRaises((ContractError, OSError)):
                    with Lease(self.serial, directory=root):
                        pass
            finally:
                self.assertEqual(victim.read_bytes(), original)

    def test_boolean_cutover_version_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='g1b-lease-probe-') as directory:
            root = Path(directory)
            key = hashlib.sha256(self.serial.encode()).hexdigest()
            with Lease(self.serial, directory=root, authority_root=self.authority_root) as lease:
                lease.mark_authority('shared')
            with Lease(self.serial, directory=root, authority_root=self.authority_root):
                pass
            marker = root / (key + '.authority.json')
            document = json.loads(marker.read_text())
            document['version'] = True
            marker.write_text(json.dumps(document))
            with self.assertRaises(ContractError):
                with Lease(self.serial, directory=root, authority_root=self.authority_root):
                    pass

    def test_regular_lock_still_excludes_a_second_owner(self):
        with tempfile.TemporaryDirectory(prefix='g1b-lease-probe-') as directory:
            with Lease(self.serial, directory=directory):
                with self.assertRaises(ContractError):
                    with Lease(self.serial, directory=directory):
                        pass
            with Lease(self.serial, directory=directory):
                pass


if __name__ == '__main__':
    unittest.main()
