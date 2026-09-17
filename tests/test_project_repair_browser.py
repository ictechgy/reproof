from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from reproloop.live.server import LiveServer
from tests import test_live_project_repair as support
from tests.fixtures.g9_browser_check import browser_check


class ProjectRepairBrowserTests(unittest.TestCase):
    def test_actual_browser_proposal_review_scope_revocation_and_mobile_layout(self):
        self.check_browser(verification=False)

    def test_actual_browser_protected_protocol_result_and_review(self):
        self.check_browser(verification=True)

    def check_browser(self, *, verification):
        fixture = support.LiveProjectRepairTests('runTest'); self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.service(executor=fixture.protected_runtime() if verification else None)
        store = fixture.fixture.access_store
        token = store.issue_principal_credential('admin', 'owner', lifetime_seconds=600)['token']
        principal = store.authenticate_principal(token)
        server = LiveServer(fixture.env.lab, access=fixture.fixture.access, issue_workflow=fixture.workflow)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def close(): server.shutdown(); thread.join(2); server.server_close()
        self.addCleanup(close)
        def diagnostics():
            return {'jobs': [{key: row.get(key) for key in ('status', 'phase', 'reason', 'cancelRequested')}
                             for row in fixture.repairs.journal.list()],
                    'replayErrors': list(getattr(getattr(fixture, 'protected', None), 'replay_errors', []))[:5]}
        with tempfile.TemporaryDirectory() as output:
            try:
                report = browser_check(server.origin, token, fixture.issue_id, Path(output),
                    revoke=lambda: store.revoke_credential('admin', principal.credential_id),
                    verification=verification, diagnostics=diagnostics)
            except RuntimeError:
                failure = Path(output)/'failure.json'
                if failure.is_file():
                    retained = Path(__file__).resolve().parents[1]/'artifacts/qa-delivery/g9-browser-failures'/uuid.uuid4().hex
                    retained.mkdir(parents=True, mode=0o700)
                    (retained/'failure.json').write_bytes(failure.read_bytes())
                    raise RuntimeError('browser-state-timeout; evidence: '+str(retained)) from None
                raise
        self.assertFalse(report['verified']); self.assertFalse(report['actualAI'])
        self.assertEqual(len(report['checks']), 6 if verification else 5)
        self.assertTrue(all(check['passed'] for check in report['checks']))


if __name__ == '__main__': unittest.main()
