"""Explicit optional-tool browser smoke; never part of the stdlib portable gate.

Requires already installed Python Playwright and Chromium. Missing tools fail
clearly; this test never downloads a browser or reports a skipped check as pass.
"""
import os
from pathlib import Path
import tempfile
import threading
import unittest

from reproof.live.server import LiveServer
from tests.test_ticket_references import TicketReferenceFixture
from tests.fixtures.ticket_reference_browser_check import browser_check


class TicketReferenceBrowserTests(TicketReferenceFixture, unittest.TestCase):
    def test_owned_chromium_reference_lifecycle_and_mobile_layout(self):
        first, _, _ = self.fixture()
        second, _, _ = self.fixture()
        token = self.access_store.issue_principal_credential('admin', 'owner', lifetime_seconds=600)['token']
        server = LiveServer(self.env.lab, access=self.access, issue_workflow=self.workflow)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(2), server.server_close()))
        before = self.workflow.get(self.owner, first)
        calls = list(self.env.control['calls'])
        def inspect():
            current = self.workflow.get(self.owner, first)
            return {'references': self.workflow._all('ticket-reference'),
                    'packages': len(self.workflow.packages.list('checkout', authorize=lambda: True)),
                    'unchanged': all(current[key] == before[key] for key in ('approval', 'campaign', 'recording', 'specification')),
                    'noExecution': calls == self.env.control['calls']}
        def revise():
            view = self.workflow.get(self.owner, first)
            self.save(first, view['recording'], base=view['specification']['revision'])
        def expire(package_id):
            with self.workflow.packages._operation():
                self.workflow.packages._db.execute('UPDATE packages SET expires_ms=0 WHERE id=?', (package_id,))
        def revoke(): self.access_store.revoke_membership('admin', 'checkout', 'owner', 'maintainer')
        with tempfile.TemporaryDirectory(prefix='repro-ticket-browser-') as temporary:
            output = Path(os.environ.get('REPRO_TICKET_BROWSER_OUTPUT', temporary)); output.mkdir(parents=True, exist_ok=True)
            report = browser_check(server.origin, token, first, second, output,
                                   inspect=inspect, revise=revise, expire=expire, revoke=revoke)
        self.assertGreaterEqual(len(report['checks']), 10)
        self.assertTrue(all(check['passed'] for check in report['checks']))
        self.assertFalse(report['actualDevice']); self.assertFalse(report['externalTracker'])


if __name__ == '__main__': unittest.main()
