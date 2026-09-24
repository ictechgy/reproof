import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest

from reproof.live.server import LiveServer
from tests import test_live_project_repair as support


class ProjectRepairCliTests(unittest.TestCase):
    def test_cli_proposes_verifies_waits_exports_and_preserves_existing_output(self):
        fixture = support.LiveProjectRepairTests('runTest'); self.addCleanup(fixture.doCleanups)
        fixture.setUp(); fixture.service(executor=fixture.protected_runtime())
        server = LiveServer(fixture.env.lab, access=fixture.fixture.access, issue_workflow=fixture.workflow)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def close(): server.shutdown(); thread.join(2); server.server_close()
        self.addCleanup(close)
        token = fixture.fixture.access_store.issue_principal_credential('admin', 'owner', lifetime_seconds=600)['token']
        def cli(*args):
            result = subprocess.run([sys.executable, '-m', 'reproof', 'live-issues', *args,
                '--server', server.origin, '--credential-stdin'], input=token + '\n', text=True,
                capture_output=True, timeout=15, cwd=Path(__file__).resolve().parents[1])
            self.assertNotIn(token, result.stdout + result.stderr)
            return result
        result = cli('repair-propose', fixture.issue_id, '--specification-digest', fixture.spec_digest,
                     '--request-id', 'cli_request', '--wait')
        self.assertEqual(result.returncode, 0, result.stderr)
        job = json.loads(result.stdout)['repair']; self.assertEqual(job['status'], 'proposal-ready')
        result = cli('repairs', fixture.issue_id)
        self.assertEqual(json.loads(result.stdout)['repairs'][0]['id'], job['id'])
        result = cli('repair-show', job['id'])
        self.assertFalse(json.loads(result.stdout)['repair']['result']['verified'])
        output = fixture.env.root / 'review-proposal'
        result = cli('repair-patch', job['id'], '--output', str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        before = (output / 'proposal.json').read_bytes()
        self.assertIn('if ready', (output / 'change.diff').read_text())
        self.assertNotIn('if ready', result.stdout)
        self.assertNotEqual(cli('repair-patch', job['id'], '--output', str(output)).returncode, 0)
        self.assertEqual((output / 'proposal.json').read_bytes(), before)
        result = cli('repair-verify', fixture.issue_id, '--specification-digest', fixture.spec_digest,
                     '--request-id', 'cli_verify_request', '--wait')
        self.assertEqual(result.returncode, 0, result.stderr)
        verified = json.loads(result.stdout)['repair']
        self.assertEqual((verified['status'], verified['result']['verified']), ('verified', True), verified['reason'])
        self.assertEqual(len(verified['result']['afterEvidence']['attempts']), 3)


if __name__ == '__main__': unittest.main()
