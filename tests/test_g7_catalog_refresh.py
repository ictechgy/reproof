"""Actual browser observes normal synthetic reservation release without reload."""
import json
import shutil
import subprocess
import threading
import time
import unittest
import uuid

from reproloop.live.server import LiveServer
from tests import test_issue_workflow as support


class CatalogRefreshBrowserTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.IssueWorkflowTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        issue_id, view = self.fixture.recorded()
        saved = self.fixture.save(issue_id, view['recording'])
        self.fixture.approve(issue_id, saved)
        self.lab = self.fixture.env.lab
        self.server = LiveServer(self.lab, access=self.fixture.access,
                                 issue_workflow=self.fixture.workflow)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def stop_server():
            self.server.shutdown(); thread.join(2); self.server.server_close()
        self.addCleanup(stop_server)
        executable = shutil.which('agent-browser')
        self.assertIsNotNone(executable, 'Installed agent-browser is required')
        self.prefix = [executable, '--session', 'codex-g7-catalog-' + uuid.uuid4().hex[:12],
                       '--allowed-domains', '127.0.0.1,localhost', '--json']
        self.addCleanup(lambda: self.browser('close'))

    def browser(self, *args, stdin=None):
        result = subprocess.run([*self.prefix, *args], input=stdin, capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, 'Owned browser command failed')
        body = json.loads(result.stdout)
        self.assertTrue(body.get('success'), 'Owned browser operation failed')
        return body.get('data')

    def evaluate(self, script):
        return self.browser('eval', '--stdin', stdin=script)['result']

    def wait_for(self, condition):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.evaluate(condition): return
            time.sleep(.2)
        self.fail('Browser did not observe the current authorized catalog')

    def reserve(self, identifier):
        reservation = self.lab.reserve_release_device(
            'device', 'owner', identifier, self.fixture.env.registration,
            application_id='ios_app', build_id='original')
        self.addCleanup(lambda: self.lab.release_device_reservation(reservation)
                        if reservation.reservation_id in self.lab._device_reservations else None)
        return reservation

    def test_reservations_refresh_before_selection_and_preserve_unsaved_specification(self):
        held = self.reserve('catalog_first')
        self.browser('open', self.server.origin)
        self.wait_for("!!document.querySelector('#qa-credential')")
        credential = self.fixture.access_store.issue_principal_credential(
            'admin', 'owner', lifetime_seconds=60)['token']
        self.evaluate("(() => {document.querySelector('#qa-credential').value="
                      + json.dumps(credential)
                      + ";document.querySelector('#qa-login-form').requestSubmit();return true;})()")
        credential = None
        self.browser('snapshot', '-i')
        self.wait_for("!document.querySelector('#qa-workspace').hidden")
        self.assertTrue(self.evaluate("document.querySelector('#qa-start').disabled"))
        self.lab.release_device_reservation(held)
        self.wait_for("!document.querySelector('#qa-start').disabled && document.querySelector('#qa-device-note').textContent.startsWith('available')")

        self.evaluate("document.querySelector('#qa-library button').scrollIntoView({block:'center'});true")
        self.browser('click', '#qa-library button'); self.browser('snapshot', '-i')
        self.wait_for("!document.querySelector('#qa-replay').disabled")
        held = self.reserve('catalog_second')
        self.wait_for("document.querySelector('#qa-device-note').textContent.startsWith('reserved') && document.querySelector('#qa-replay').disabled")
        field = '#qa-assertions [name="value"]'
        self.evaluate('document.querySelector(' + json.dumps(field) + ').scrollIntoView({block:"center"});true')
        self.browser('fill', field, 'keep-local-edit'); self.browser('snapshot', '-i')
        self.lab.release_device_reservation(held)
        self.wait_for("!document.querySelector('#qa-start').disabled && document.querySelector('#qa-device-note').textContent.startsWith('available')")
        self.assertEqual(self.evaluate('document.querySelector(' + json.dumps(field) + ').value'), 'keep-local-edit')
        self.assertTrue(self.evaluate("document.querySelector('#qa-approve').disabled && document.querySelector('#qa-replay').disabled"))


if __name__ == '__main__': unittest.main()
