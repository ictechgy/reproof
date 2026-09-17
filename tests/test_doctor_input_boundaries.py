"""Root-owned doctor acceptance tests; goal workers must not weaken these checks."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]


def doctor_module():
    spec = importlib.util.spec_from_file_location("repair_backend_doctor", ROOT / "scripts/repair-backend-doctor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DoctorInputBoundaryTests(unittest.TestCase):
    def test_deep_invalid_json_has_a_static_inspection_error(self):
        doctor = doctor_module()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "guest-metadata.json"
            path.write_text('{"unexpected":' + '[' * 2000 + '0' + ']' * 2000 + '}')
            with self.assertRaises(doctor.InspectionError):
                doctor._load_descriptor(path, doctor.validate_guest_image_manifest)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Named pipes require POSIX")
    def test_a_named_pipe_is_rejected_without_waiting_for_a_writer(self):
        child = """import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('doctor', 'scripts/repair-backend-doctor.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
try:
    module._load_descriptor(Path(sys.argv[1]), module.validate_guest_image_manifest)
except module.InspectionError:
    print('rejected')
else:
    raise SystemExit('unexpected acceptance')
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "guest-metadata.json"
            os.mkfifo(path)
            try:
                process = subprocess.run([sys.executable, "-c", child, str(path)], cwd=ROOT,
                                         capture_output=True, text=True, timeout=2)
            except subprocess.TimeoutExpired:
                self.fail("Doctor blocked while opening a non-regular input")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "rejected")


if __name__ == "__main__":
    unittest.main()
