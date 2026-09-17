"""Run the real Node timeline, request, pointer and legacy stream assertions."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BrowserLogicTests(unittest.TestCase):
    def test_current_and_legacy_browser_logic(self):
        self.assertIsNotNone(shutil.which('node'), 'Installed Node is required')
        files = ['issue.test.mjs', 'video.test.mjs', 'pointer.test.mjs', 'stream.test.mjs']
        result = subprocess.run(['node', '--test', *['live-web/' + name for name in files]],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout[-8000:] + result.stderr[-2000:])
        count = re.search(r'^# tests (\d+)$', result.stdout, re.MULTILINE)
        self.assertIsNotNone(count, 'Node execution summary is missing')
        self.assertGreaterEqual(int(count.group(1)), 12)
        self.assertRegex(result.stdout, r'(?m)^# fail 0$')
        self.assertRegex(result.stdout, r'(?m)^# skipped 0$')


if __name__ == '__main__': unittest.main()
