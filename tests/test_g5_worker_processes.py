"""Actual enrolled worker CLI with owned loopback credentials and no device."""
import json
import http.client
from pathlib import Path
import secrets
import selectors
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]

from reproloop.live.authority import HostAuthority
from reproloop.live.configuration import issue_bounded_project_grant
from reproloop.live.enrollment import EnrollmentClient
from reproloop.live.model import LiveError
from reproloop.live.worker import RemoteProvider, WorkerClient
from tests.test_project_access import SharedHttpFixture


class ParentEnrolledWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SharedHttpFixture()
        self.addCleanup(self.fixture.close)
        store = self.fixture.store
        enrollment = store.create_host_enrollment(
            "root-admin", host_id="probe-mac", project_ids=["checkout"],
            trust_groups=[], lifetime_seconds=300, credential_lifetime_seconds=300)
        enrolled = EnrollmentClient(self.fixture.server.origin).enroll(
            enrollment["token"], host_id="probe-mac", incarnation="probe-boot")
        transport = secrets.token_urlsafe(40)
        self.transport = transport
        root = self.fixture.root
        self.process = subprocess.Popen([
            sys.executable, "-m", "reproloop", "live-worker", "--demo",
            "--port", "0", "--output", str(root / "worker-output"),
            "--authority-root", str(root / "worker-authority"),
            "--host-credential-stdin", "--coordinator", self.fixture.server.origin,
            "--host-id", "probe-mac", "--host-incarnation", "probe-boot",
        ], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_worker)
        self.process.stdin.write(json.dumps({
            "hostCredential": enrolled["credential"], "transportToken": transport}))
        self.process.stdin.close()
        with selectors.DefaultSelector() as selected:
            selected.register(self.process.stdout, selectors.EVENT_READ)
            self.assertTrue(selected.select(8), "Owned enrolled worker did not start")
        line = self.process.stdout.readline()
        try:
            public = json.loads(line)
        except ValueError:
            self.fail("Owned enrolled worker did not produce public startup metadata")
        self.assertIn("worker", public, "Enrolled worker startup failed")
        self.assertEqual(public["hostId"], "probe-mac")
        self.client = WorkerClient(public["worker"], transport)
        self.worker_origin = public["worker"]
        self.authority = HostAuthority(root / "caller-authority" / "authority.sqlite3")
        self.addCleanup(self.authority.close)

    def stop_worker(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()

    def delegate(self, project_id):
        grant = issue_bounded_project_grant(
            self.authority, project_id, lifetime_seconds=60)
        parent = SimpleNamespace(authority=self.authority, parent_grant=grant)
        provider = RemoteProvider(self.client, "demo-device", authority_mode="shared-v2")
        return provider._delegate_authority(parent)

    def test_transport_token_cannot_delegate_a_foreign_project_to_enrolled_host(self):
        self.assertTrue(self.delegate("checkout"))
        with self.assertRaises(LiveError):
            self.delegate("foreign-project")

    def test_running_enrolled_worker_refuses_new_grants_after_host_revocation(self):
        self.assertTrue(self.delegate("checkout"))
        self.fixture.store.revoke_host("root-admin", "probe-mac")
        with self.assertRaises(LiveError):
            self.delegate("checkout")

    def test_revocation_while_reading_input_body_prevents_the_worker_effect(self):
        session = self.client.call("/v1/sessions", {
            "deviceId": "demo", "clientId": "coordinator"})["session"]

        def command(sequence):
            frame, _ = self.client.frame(session["id"])
            return {"controllerId": session["controllerId"], "epoch": session["epoch"],
                    "sequence": sequence, "commandId": f"held-{sequence}",
                    "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
                    "action": "tap", "payload": {"x": .5, "y": .5}}

        path = f"/v1/sessions/{session['id']}/input"
        self.assertIn("receipt", self.client.call(path, command(1)))
        payload = json.dumps(command(2)).encode()
        checked = threading.Event()
        original = self.fixture.store.authenticate_host

        def authenticated(*args, **kwargs):
            current = original(*args, **kwargs)
            checked.set()
            return current

        self.fixture.store.authenticate_host = authenticated
        self.addCleanup(setattr, self.fixture.store, "authenticate_host", original)
        address = urlsplit(self.worker_origin)
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=5)
        try:
            connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", address.netloc)
            connection.putheader("Authorization", "Bearer " + self.transport)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders()
            self.assertTrue(checked.wait(3), "The worker did not admit the held request")
            self.fixture.store.revoke_host("root-admin", "probe-mac")
            connection.send(payload)
            response = connection.getresponse()
            response.read()
            self.assertIn(response.status, (401, 403),
                          "The revoked worker accepted an input body admitted earlier")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
