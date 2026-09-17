"""Package bytes/provenance only: no Python installation or VM qualification."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reproloop.execution.guest_installation import InstallationError, package_agent, verify_package
from tests.test_execution_resources import catalog

ROOT = Path(__file__).resolve().parents[1]


class GuestPackageTests(unittest.TestCase):
    def test_package_verifies_exact_files_and_tampered_policy_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            native = root / "native"
            native.mkdir()
            for name in ("guest-connect", "guest-run"):
                (native / name).write_bytes(b"explicit packaging fixture; not a native executable")
            package = root / "agent"
            policy = package_agent(package, source_root=ROOT, native_root=native, catalog=catalog(), uid=501, gid=20)
            self.assertEqual(verify_package(package)["agentDigest"], policy["agentDigest"])
            result = subprocess.run([sys.executable, "-I", str(package / "main.py")], capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout.strip(), b"guest-agent-rejected")
            self.assertNotIn(b"Traceback", result.stderr)
            (package / "reproloop/execution/wire.py").write_bytes(b"changed source")
            with self.assertRaises(InstallationError):
                verify_package(package)

    def test_invalid_candidate_uid_and_existing_output_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name in ("guest-connect", "guest-run"):
                (root / name).write_bytes(b"explicit packaging fixture")
            with self.assertRaises(InstallationError):
                package_agent(root / "denied", source_root=ROOT, native_root=root, catalog=catalog(), uid=0, gid=0)
            self.assertFalse((root / "denied").exists())
            with self.assertRaises(InstallationError):
                package_agent(root, source_root=ROOT, native_root=root, catalog=catalog(), uid=501, gid=20)
            self.assertEqual((root / "guest-run").read_bytes(), b"explicit packaging fixture")
