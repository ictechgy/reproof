"""Required process/browser acceptance; no device or company environment is implied."""
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]


class RecordReplayGateTests(unittest.TestCase):
    def test_actual_two_workers_restart_packages_predicates_and_browser(self):
        label = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        output = ROOT / 'artifacts/qa-delivery/g7-release-gate' / label
        process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/qa-record-replay.py'),
            '--environment', 'synthetic-local', '--output-new', str(output), '--browser'],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            log, _ = process.communicate(timeout=480)
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGINT)
            try: log, _ = process.communicate(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill(); log, _ = process.communicate(timeout=5)
            self.fail('Owned record/replay gate timed out; evidence: ' + str(output))
        finally:
            if process.stdout is not None: process.stdout.close()
        (output / 'command.log').write_text(log)
        self.assertEqual(process.returncode, 0, log[-6000:] + '\nEvidence: ' + str(output))
        result = json.loads((output / 'result.json').read_text())
        self.assertEqual(result['status'], 'passed')
        self.assertIs(result['sourcesStable'], True)
        self.assertFalse(result['twoMacAcceptance'])
        self.assertFalse(result['physicalDeviceAcceptance'])
        self.assertTrue(all(item['exitCode'] == 0 for item in result['processCleanup']))
        browser = json.loads((output / 'browser.json').read_text())
        self.assertIs(browser['passed'], True)
        self.assertEqual(len(browser['checks']), 12)
        self.assertTrue(all(item['passed'] for item in browser['checks']))
        print('G7 actual process/browser evidence: ' + str(output.relative_to(ROOT)))


class NativeDescriptorTests(unittest.TestCase):
    def test_missing_native_inputs_are_blocked_without_a_fabricated_environment(self):
        with tempfile.TemporaryDirectory(prefix='g7-native-descriptor-') as directory:
            root = Path(directory)
            process = subprocess.run([sys.executable, str(ROOT / 'scripts/qa-record-replay.py'),
                '--environment', str(root / 'not-supplied.json'), '--output-new', str(root / 'evidence')],
                cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 2)
            result = json.loads((root / 'evidence/result.json').read_text())
            self.assertEqual(result['status'], 'blocked-unqualified')
            self.assertEqual(result['reason'], 'native_descriptor_unavailable')
            self.assertFalse(result['physicalDeviceAcceptance'])
            self.assertFalse(result['twoMacAcceptance'])


if __name__ == '__main__': unittest.main()
