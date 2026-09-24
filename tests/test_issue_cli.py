import json
from pathlib import Path
import subprocess
import sys
import unittest

from reproof.live.client import IssueClient
from reproof.live.model import LiveError
from tests import test_issue_http as http_support


class IssueClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): http_support.IssueHttpTests.setUpClass()
    @classmethod
    def tearDownClass(cls): http_support.IssueHttpTests.tearDownClass()

    def setUp(self):
        self.fixture = http_support.IssueHttpTests('runTest'); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = IssueClient(self.fixture.server.origin, self.fixture.tokens['owner'])

    def test_actual_cli_reads_credential_from_stdin_without_echoing_it(self):
        result = subprocess.run([sys.executable, '-m', 'reproof', 'live-issues', 'projects',
            '--server', self.fixture.server.origin, '--credential-stdin'],
            input=self.fixture.tokens['owner'] + '\n', text=True, capture_output=True, timeout=15,
            cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['projects'][0]['id'], 'checkout')
        self.assertNotIn(self.fixture.tokens['owner'], result.stdout + result.stderr)

    def test_package_export_import_and_existing_output_protection_use_real_routes(self):
        issue_id, original, saved = self.fixture.record()
        output = self.fixture.fixture.env.root / 'exported-issue.zip'
        exported = self.client.export_package(issue_id, output)
        self.assertEqual(output.stat().st_size, exported['package']['bytes'])
        imported = self.client.import_package('checkout', output)
        view = self.client.call('/api/release/issues/' + imported['issue']['id'])
        self.assertIsNone(view['approval'])
        for key in ('original', 'recordingDigest', 'status', 'lifecycleReceipts'):
            self.assertEqual(view['recording'][key], original['recording'][key])
        self.assertEqual(view['specification'], saved['specification'])
        before = output.read_bytes()
        with self.assertRaises(LiveError): self.client.download_package(exported['package'], output)
        self.assertEqual(output.read_bytes(), before)

    def test_export_identity_mismatch_does_not_publish_a_partial_file(self):
        issue_id, _, _ = self.fixture.record()
        package = self.client.call('/api/release/issues/' + issue_id + '/export', {})['package']
        package['archiveDigest'] = 'f' * 64
        output = self.fixture.fixture.env.root / 'unpublished.zip'
        with self.assertRaises(LiveError): self.client.download_package(package, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.glob('.issue-download-*.part')), [])
