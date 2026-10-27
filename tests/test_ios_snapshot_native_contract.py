"""Host parity and wiring for the separately executed production Swift parser."""
import json
from pathlib import Path
import runpy
import unittest

from reproof.core import ContractError
from reproof.ios_instrumentation import validate_ios_auto_profile

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / 'scripts/check-ios-observation-contract.py'


class IosSnapshotNativeContractTests(unittest.TestCase):
    def test_host_profile_validation_matches_native_vectors_with_legacy_float_exception(self):
        cases = runpy.run_path(str(HARNESS))['vectors']()
        self.assertEqual(len(cases), 60)
        for index, case in enumerate(cases):
            with self.subTest(vector=index):
                document = json.loads(case['profile'])
                expected = case['accepted']
                # This preexisting v2 native acceptance is not widened into v3.
                if (document.get('kind') == 'uikit-observation-v2'
                        and type(document['schemaVersion']) is float):
                    expected = False
                try:
                    validate_ios_auto_profile(document)
                except ContractError:
                    actual = False
                else:
                    actual = True
                self.assertEqual(actual, expected)

    def test_harness_executes_complete_production_parser_not_a_model(self):
        harness = runpy.run_path(str(HARNESS))
        actual = harness['swift_source']()
        source = harness['SOURCE'].read_text()
        methods = source[source.index('    private func loadProfile()'):
                         source.index('    private func scheduleStartupAttempt()')]
        self.assertIn(methods, actual)
        self.assertIn('result?.observationOnly == true', actual)
        self.assertNotIn('import UIKit', actual)
        self.assertNotIn('UIApplication.shared', actual)

    def test_ci_requires_executed_vectors_and_exact_unsigned_compile_proof(self):
        workflow = (ROOT / '.github/workflows/ios-sdk-compile.yml').read_text()
        self.assertIn('"$REPRO_PYTHON" scripts/check-ios-observation-contract.py', workflow)
        self.assertIn("grep -Fx 'iOS observation contract: 60 synthetic vectors passed'", workflow)
        self.assertIn('"$REPRO_PYTHON" scripts/check-ios-snapshot-build.py --output', workflow)
        self.assertIn('assert len(reports) == 1', workflow)
        self.assertIn('type(report[key]) is type(value)', workflow)
        self.assertIn("'deviceExecution': False, 'behaviorVerified': False", workflow)


if __name__ == '__main__':
    unittest.main()
