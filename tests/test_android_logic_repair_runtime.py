"""Real Gradle/JUnit sample execution, never mocked or skipped to green.

Only the explicit ANDROID-LOGIC release goal invokes these checks. Portable
contracts cannot establish host Kotlin execution. Each candidate gets its own
600-second outer budget and new evidence/workspace directories.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest

from reproof import android_logic_repair_demo as demo


class AndroidLogicRuntimeTests(unittest.TestCase):
    def execute(self, candidate, status):
        configured = os.environ.get('REPRO_ANDROID_LOGIC_OUTPUT')
        if configured:
            output = Path(configured).resolve()
            output.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            temporary = tempfile.TemporaryDirectory(prefix='reproof-android-runtime-')
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
        if candidate == 'noop':
            self.assertFalse(report['baselineDefectReproduced'])
            self.assertIsNone(report.get('baseline'))
            self.assertIsNone(report['candidate'])
            self.assertFalse((output / candidate / 'baseline').exists())
            self.assertFalse((output / candidate / 'candidate').exists())
            self.assertEqual(report['error']['code'], 'patch_invalid')
            return
        self.assertTrue(report['baselineDefectReproduced'])
        baseline, changed = report['baseline'], report['candidate']
        self.assertEqual((baseline['tests'], baseline['failures'], baseline['skipped']), (1, 1, 0))
        self.assertEqual(baseline['expectedActual'], 2)
        self.assertEqual((changed['tests'], changed['failures'], changed['skipped']),
                         (1, int(candidate == 'wrong'), 0))
        self.assertEqual(changed['expectedActual'], 3 if candidate == 'wrong' else None)
        self.assertEqual(changed['oracleDigest'], baseline['oracleDigest'])
        self.assertEqual(changed['recipeDigest'], baseline['recipeDigest'])
        self.assertNotEqual(changed['buildInputDigest'], baseline['buildInputDigest'])
        self.assertNotEqual(report['sourceDigest'], report['candidateDigest'])

    def test_fixed_candidate(self):
        self.execute('fixed', 'host_logic_repair_passed')

    def test_wrong_candidate(self):
        self.execute('wrong', 'candidate_failed')

    def test_noop_candidate(self):
        self.execute('noop', 'rejected')


if __name__ == '__main__':
    unittest.main()
