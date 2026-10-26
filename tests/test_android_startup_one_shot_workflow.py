"""Portable static controls; no native execution."""
from pathlib import Path
import hashlib
import json
import re
import unittest
ROOT = Path(__file__).resolve().parents[1]

class WorkflowScope(unittest.TestCase):
    def test_single_active_opened_job(self):
        text = (ROOT / '.github/workflows/android-startup-one-shot.yml').read_text()
        for required in ('types: [opened]', "github.event.action == 'opened'",
                         'github.event.pull_request.head.repo.full_name == github.repository',
                         "github.event.pull_request.head.ref == 'codex/android-startup-one-shot-validation'",
                         'github.run_attempt == 1', 'runs-on: ubuntu-24.04', 'timeout-minutes: 25',
                         'persist-credentials: false', 'ref: ${{ github.event.pull_request.head.sha }}'):
            self.assertIn(required, text)
        self.assertNotIn('false &&', text)
        self.assertEqual(text.count('runs-on:'), 1)
        self.assertNotRegex(text, r'(?m)^\s*(push|workflow_dispatch|pull_request_target):')

    def test_artifacts_explicit_only(self):
        text = (ROOT / '.github/workflows/android-startup-one-shot.yml').read_text()
        paths = re.findall(r'^\s+\$\{\{ runner.temp \}\}/(.+)$', text, re.M)
        self.assertEqual(set(paths), {
            'reproof-startup-evidence/supervisor.json',
            'reproof-startup-evidence/android/result.json',
            'reproof-startup-evidence/android/build-provenance.json',
            'reproof-startup-evidence/android/first-frame.jpg'})
        self.assertIn('retention-days: 7', text)
        self.assertIn('android-startup-one-shot.py cleanup', text)
        self.assertGreaterEqual(text.count('if: always()'), 2)

    def test_reviewed_source_pin_covers_production_and_harness(self):
        manifest = json.loads((ROOT / 'scripts/ci/android-startup-source.json').read_text())
        for path in ('reproof/live/android_live.py', 'reproof/live/model.py',
                     'scripts/android-startup-one-shot-qa.py', 'scripts/ci/android-startup-one-shot.py',
                     '.github/workflows/android-startup-one-shot.yml',
                     'android/runtime-fixture/src/main/java/io/reproof/runtimefixture/MainActivity.java'):
            self.assertIn(path, manifest['files'])
        for path, expected in manifest['files'].items():
            self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), expected, path)

    def test_no_security_mutations_or_old_workflow(self):
        self.assertFalse((ROOT / '.github/workflows/native-runtime.yml').exists())
        for file in [ROOT / 'scripts/ci/android-startup-one-shot.py', ROOT / 'scripts/android-startup-one-shot-qa.py']:
            text = file.read_text()
            self.assertNotRegex(text, r'\b(?:setfacl|chmod|usermod|groupadd)\b')
            self.assertNotIn('/etc/udev', text)
            self.assertNotIn('-accel", "off', text)

if __name__ == '__main__': unittest.main()
