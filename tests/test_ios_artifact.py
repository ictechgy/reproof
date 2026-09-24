import os
from pathlib import Path
import tempfile
import unittest

from reproof.core import ContractError, digest
from reproof.ios_storage import tree_manifest


class SimulatorArtifactSnapshotTests(unittest.TestCase):
    def test_snapshot_freezes_bytes_and_uses_fresh_install_timestamps(self):
        from reproof.live.ios_artifact import stage_simulator_app
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root / 'Source.app'; source.mkdir()
            binary = source / 'Program'; binary.write_bytes(b'EXPECTED_EXECUTABLE'); binary.chmod(0o755)
            os.utime(binary, (100, 100)); os.utime(source, (100, 100))
            manifest = tree_manifest(source)
            output = root / 'Selected.app'
            stage_simulator_app(source, output, expected_digest=digest(manifest), expected_bytes=binary.stat().st_size)
            self.assertEqual(tree_manifest(output), manifest)
            self.assertEqual(binary.stat().st_mtime_ns, 100_000_000_000)
            self.assertGreater((output / 'Program').stat().st_mtime_ns, binary.stat().st_mtime_ns)
            self.assertGreater(output.stat().st_mtime_ns, source.stat().st_mtime_ns)
            self.assertTrue(os.access(output / 'Program', os.X_OK))
            binary.write_bytes(b'CHANGED_AFTER_SNAPSHOT')
            self.assertEqual((output / 'Program').read_bytes(), b'EXPECTED_EXECUTABLE')

    def test_unapproved_bytes_and_linked_inputs_are_rejected(self):
        from reproof.live.ios_artifact import stage_simulator_app
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root / 'Source.app'; source.mkdir()
            binary = source / 'Program'; binary.write_bytes(b'OLD')
            expected = digest(tree_manifest(source)); binary.write_bytes(b'NEW')
            with self.assertRaises(ContractError):
                stage_simulator_app(source, root / 'wrong.app', expected_digest=expected, expected_bytes=3)
            self.assertFalse((root / 'wrong.app').exists())
            outside = root / 'outside'; outside.write_bytes(b'NEW'); binary.unlink(); binary.symlink_to(outside)
            with self.assertRaises(ContractError):
                stage_simulator_app(source, root / 'linked.app', expected_digest=expected, expected_bytes=3)
            self.assertFalse((root / 'linked.app').exists())
