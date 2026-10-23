"""The release gate must bound child processes and reject empty evidence."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]


class ReleaseHarnessTests(unittest.TestCase):
    def invoke(self, case, *, timeout=5):
        inventory = {"version": 1, "goals": {"X": {"available": True, "checks": [{
            "id": "isolated-check", "command": ["python3", "-m", "unittest",
                "tests.fixtures.release.harness_cases." + case],
            "environment": "local-python", "effects": ["process"], "timeoutSeconds": timeout,
            "evidence": "unittest-summary", "gate": "software"}]}}}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inventory.json"
            path.write_text(json.dumps(inventory))
            process = subprocess.run([sys.executable, "scripts/release-check.py", "--inventory",
                                      str(path), "--goal", "X", "--effects", "process"],
                                     cwd=ROOT, capture_output=True, text=True, timeout=15)
        return process, json.loads(process.stdout)

    def test_a_real_positive_check_provides_nonzero_evidence(self):
        process, report = self.invoke("Cases.test_pass")
        self.assertEqual(process.returncode, 0)
        self.assertEqual(report["checks"][0]["testsRun"], 1)

    def test_zero_tests_are_not_a_release_pass(self):
        process, report = self.invoke("Empty")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(report["status"], "fail")

    def test_incomplete_or_missing_success_evidence_fails(self):
        for case, count in (("Cases.test_skipped", 1),
                            ("Cases.test_expected_failure", 1),
                            ("Partial", 2),
                            ("Cases.test_missing_verdict", 1),
                            ("Cases.test_malformed_verdict", 1)):
            with self.subTest(case=case):
                process, report = self.invoke(case)
                self.assertNotEqual(process.returncode, 0)
                check = report["checks"][0]
                self.assertEqual(check["returncode"], 0)
                self.assertEqual(check["testsRun"], count)
                self.assertEqual(check["status"], "fail")
                self.assertEqual(check["reason"], "missing_test_evidence")

    def test_normal_stdout_does_not_hide_final_summary(self):
        process, report = self.invoke("Cases.test_stdout")
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(report["checks"][0]["testsRun"], 1)

    def test_excess_output_fails_instead_of_being_silently_truncated(self):
        process, report = self.invoke("Cases.test_output_limit")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(report["checks"][0]["reason"], "output_limit")
        self.assertLess(len(process.stdout), 10000)

    def test_timeout_is_a_failed_check(self):
        process, report = self.invoke("Cases.test_timeout", timeout=1)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(report["checks"][0]["reason"], "timeout")


if __name__ == "__main__":
    unittest.main()
