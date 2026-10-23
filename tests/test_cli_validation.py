"""Read-only CLI validation errors must remain structured and preserve inputs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reproof.storage import create_bundle
from tests.test_core import capture, oracle

ROOT = Path(__file__).resolve().parents[1]


class CLIValidationTests(unittest.TestCase):
    def invoke(self, *args):
        environment = os.environ.copy()
        # Exercise the supported interpreter's integer guard deterministically.
        environment['PYTHONINTMAXSTRDIGITS'] = '4300'
        return subprocess.run([sys.executable, '-m', 'reproof', 'validate', *map(str, args)],
                              cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10)

    def test_malformed_bundle_and_profile_json_are_structured_and_unchanged(self):
        invalid = {'nesting': '[' * 10000 + '0' + ']' * 10000,
                   'integer': '{"schemaVersion":' + '9' * 5000 + '}'}
        for label, raw in invalid.items():
            for profile in (False, True):
                with self.subTest(label=label, profile=profile), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    source = root / ('profile.json' if profile else 'manifest.json')
                    source.write_text(raw)
                    sentinel = root / 'preserved.txt'
                    sentinel.write_bytes(b'existing evidence')
                    args = [root, '--app-profile', source] if profile else [root]
                    result = self.invoke(*args)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual(report['status'], 'blocked')
                    self.assertEqual(report['errorType'], 'ContractError')
                    self.assertEqual(report['message'], 'Malformed JSON artifact')
                    self.assertNotIn('Traceback', result.stderr)
                    self.assertEqual(source.read_text(), raw)
                    self.assertEqual(sentinel.read_bytes(), b'existing evidence')
                    self.assertEqual(set(root.iterdir()), {source, sentinel})

    def test_valid_bundle_still_compiles_without_modifying_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            apk = root / 'sample.apk'; apk.write_bytes(b'synthetic fixture')
            bundle = root / 'bundle'
            create_bundle(capture(), oracle(), apk, bundle)
            before = {p.name: p.read_bytes() for p in bundle.iterdir()}
            result = self.invoke(bundle)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'], 'compiled')
            self.assertEqual({p.name: p.read_bytes() for p in bundle.iterdir()}, before)

    def test_unknown_option_is_rejected_without_creating_input(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / 'missing'
            result = self.invoke(missing, '--unknown-option')
            self.assertEqual(result.returncode, 2)
            self.assertIn('unrecognized arguments', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
