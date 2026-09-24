import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from reproof.android_artifact import stage_apk
from reproof.core import ContractError


class AndroidArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'original.apk'
        self.source.write_bytes(b'owned apk bytes')
        self.output = self.root / 'selected.apk'
        self.expected = {'expected_digest': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                         'expected_bytes': self.source.stat().st_size}

    def test_private_copy_is_fixed_and_existing_destination_is_preserved(self):
        stage_apk(self.source, self.output, **self.expected)
        self.assertEqual(self.output.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.source.write_bytes(b'changed')
        self.assertEqual(self.output.read_bytes(), b'owned apk bytes')
        with self.assertRaises(ContractError):
            stage_apk(self.source, self.output, **self.expected)
        self.assertEqual(self.output.read_bytes(), b'owned apk bytes')

    def test_registration_verifies_the_same_regular_file_boundary_as_staging(self):
        from reproof.android_artifact import verify_apk
        verify_apk(self.source, **self.expected)
        alias = self.root / 'alias'; alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ContractError):
            verify_apk(alias / 'original.apk', **self.expected)
        linked = self.root / 'hard.apk'; os.link(self.source, linked)
        with self.assertRaises(ContractError):
            verify_apk(self.source, **self.expected)
        linked.unlink()
        with self.assertRaises(ContractError):
            verify_apk(self.source, **dict(self.expected, expected_digest='0' * 64))

    def test_links_nonregular_inputs_and_wrong_hash_leave_no_published_file(self):
        linked = self.root / 'link.apk'; linked.symlink_to(self.source)
        hard = self.root / 'hard.apk'; os.link(self.source, hard)
        fifo = self.root / 'pipe.apk'; os.mkfifo(fifo)
        directory = self.root / 'alias'; directory.symlink_to(self.root, target_is_directory=True)
        for source in (linked, hard, fifo, directory / 'original.apk'):
            with self.subTest(source=source), self.assertRaises(ContractError):
                stage_apk(source, self.output, **self.expected)
            self.assertFalse(self.output.exists())
        hard.unlink()
        with self.assertRaises(ContractError):
            stage_apk(self.source, self.output, **dict(self.expected, expected_digest='0' * 64))
        self.assertFalse(self.output.exists())
