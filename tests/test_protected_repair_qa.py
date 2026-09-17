import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProtectedRepairQaTests(unittest.TestCase):
    def run_qa(self, output, *arguments):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/qa-protected-repair.py'),
            '--output-new', str(output), *arguments], capture_output=True, text=True, timeout=20)
        return result.returncode, json.loads(result.stdout)

    def test_missing_environment_and_forged_json_cannot_issue_verified(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work).resolve()
            for index, arguments in enumerate(((), ('--environment', 'claimed-verified.json'))):
                code, result = self.run_qa(root / str(index), *arguments)
                self.assertEqual(code, 2)
                self.assertEqual(result['status'], 'blocked-unqualified')
                self.assertFalse(result['verified']); self.assertFalse(result['actualVM'])
                self.assertEqual(result['checks'], [])

    def test_owned_software_gate_preserves_original_and_existing_output(self):
        with tempfile.TemporaryDirectory() as work:
            output = Path(work).resolve() / 'proposal-qa'
            code, result = self.run_qa(output, '--environment', 'synthetic-local')
            self.assertEqual(code, 0, result)
            self.assertEqual(result['status'], 'passed-proposal-software')
            self.assertFalse(result['verified']); self.assertFalse(result['actualAI'])
            self.assertEqual(result['attemptBudget'], {'original': 3, 'candidate': 3, 'total': 6})
            self.assertTrue(result['originalUnchanged'])
            self.assertTrue(all(item['passed'] for item in result['checks']))
            before = (output / 'result.json').read_bytes()
            self.assertNotEqual(self.run_qa(output, '--environment', 'synthetic-local')[0], 0)
            self.assertEqual((output / 'result.json').read_bytes(), before)

    def test_protected_composition_is_explicitly_reported_as_doubles(self):
        with tempfile.TemporaryDirectory() as work:
            output = Path(work).resolve() / 'protected-protocol'
            code, result = self.run_qa(output, '--environment', 'synthetic-protected')
            self.assertEqual(code, 0, result)
            self.assertEqual(result['status'], 'passed-protected-software')
            for key in ('verified', 'actualVM', 'actualMobile', 'actualAI', 'companyAcceptance'):
                self.assertFalse(result[key])
            self.assertTrue(result['protectedComposition']['jobVerified'])
            self.assertEqual(result['protectedComposition']['candidateRuns'], 3)
            self.assertEqual(json.loads((output / 'protected-evidence.json').read_bytes())['attemptBudget'],
                             {'original': 3, 'candidate': 3, 'total': 6})


if __name__ == '__main__': unittest.main()
