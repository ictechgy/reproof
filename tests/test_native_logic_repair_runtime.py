"""Actual host Swift execution, only in the explicit Mac native release goal.

No mocks and no skip-to-green: unavailable native tools make this goal fail.
Each candidate is an independent release check so its own 360-second outer
budget exceeds all owned stages: fixed/wrong <=270s, noop <=150s plus cleanup.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest

from reproof import native_logic_repair_demo as demo


class NativeLogicRuntimeTests(unittest.TestCase):
    def execute(self, candidate, status):
        configured = os.environ.get('REPRO_NATIVE_LOGIC_OUTPUT')
        if configured:
            output = Path(configured).resolve()
            output.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            temporary = tempfile.TemporaryDirectory(prefix='reproof-native-runtime-')
            self.addCleanup(temporary.cleanup)
            output = Path(temporary.name).resolve()
        try:
            report = demo.run_demo(candidate, output=output / candidate)
        except Exception as error:
            (output / f'{candidate}-report.json').write_text(json.dumps({
                'status': 'rejected', 'nativeProofComplete': False,
                'error': getattr(error, 'code', type(error).__name__)}, indent=2) + '\n')
            raise
        (output / f'{candidate}-report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
        self.assertEqual(report['status'], status)
        self.assertTrue(report['baselineDefectReproduced'])
        baseline = report['baseline']
        self.assertEqual(baseline['testCase'], demo.CASE)
        self.assertEqual((baseline['tests'], baseline['failures'], baseline['skipped']), (1, 1, 0))
        self.assertEqual(baseline['expectedAssertion'], {'line': 13, 'assertion': 'XCTAssertFalse'})
        self.assertEqual(baseline['oracleDigest'], demo.PINS[demo.ORACLE])
        self.assertEqual(report['oracleDigest'], demo.PINS[demo.ORACLE])
        self.assertEqual(report['sourceFileDigest'], demo.PINS[demo.PRODUCT])
        if candidate == 'noop':
            self.assertIsNone(report['candidate'])
            self.assertFalse((output / candidate / 'candidate').exists())
            self.assertEqual(report['error']['code'], 'patch_invalid')
        else:
            changed = report['candidate']
            self.assertEqual(changed['tests'], 1)
            self.assertEqual(changed['failures'], int(candidate == 'wrong'))
            self.assertEqual(changed['skipped'], 0)
            self.assertEqual(changed['oracleDigest'], baseline['oracleDigest'])
            self.assertEqual(changed['packageDigest'], baseline['packageDigest'])
            self.assertEqual(changed['recipeDigest'], baseline['recipeDigest'])
            self.assertNotEqual(changed['buildInputDigest'], baseline['buildInputDigest'])
            self.assertNotEqual(report['sourceDigest'], report['candidateDigest'])
            if candidate == 'wrong':
                self.assertEqual(changed['expectedAssertion'], {'line': 12, 'assertion': 'XCTAssertTrue'})

    def test_fixed_candidate(self):
        self.execute('fixed', 'host_logic_repair_passed')

    def test_wrong_candidate(self):
        self.execute('wrong', 'candidate_failed')

    def test_noop_candidate(self):
        self.execute('noop', 'rejected')


if __name__ == '__main__':
    unittest.main()
