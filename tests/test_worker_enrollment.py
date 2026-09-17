import tempfile
import threading
from pathlib import Path
import unittest

from reproloop.live.model import Lab, LiveError
from reproloop.live.providers import demo_device
from reproloop.live.worker import WorkerClient, WorkerServer


TOKEN = "enrolled-worker-transport-token-0123456789"


class EnrolledWorkerAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.current = True
        self.calls = []

        def authorize(project_id=None):
            self.calls.append(project_id)
            if not self.current or project_id not in {None, "checkout"}:
                raise LiveError("unauthorized", "Host authorization is stale", 401)
            return True

        self.lab = Lab([demo_device()], Path(self.temporary.name) / "lab")
        self.server = WorkerServer(
            self.lab, TOKEN, host_authorizer=authorize)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = WorkerClient(self.server.origin, TOKEN)

    def tearDown(self):
        self.server.close_operations()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def test_enrolled_host_scope_is_checked_for_inventory_and_project(self):
        self.assertEqual(self.client.call("/v1/devices")["protocolVersion"], 2)
        self.assertIn(None, self.calls)
        self.server.authorize_enrolled_host("checkout")
        with self.assertRaises(LiveError):
            self.server.authorize_enrolled_host("foreign-project")

    def test_revocation_blocks_reads_and_inputs_but_preserves_confirmed_close(self):
        session = self.client.call("/v1/sessions", {
            "deviceId": "demo", "clientId": "coordinator",
        })["session"]
        self.current = False
        before = self.lab._session(session["id"], "worker-coordinator")["lastActivity"]
        with self.assertRaises(LiveError) as denied:
            self.client.call(f"/v1/sessions/{session['id']}")
        self.assertEqual(denied.exception.code, "unauthorized")
        with self.assertRaises(LiveError):
            self.client.call(f"/v1/sessions/{session['id']}/input", {})
        self.assertEqual(
            self.lab._session(session["id"], "worker-coordinator")["lastActivity"], before)
        closed = self.client.call(f"/v1/sessions/{session['id']}/close", {
            "controllerId": session["controllerId"], "epoch": session["epoch"],
        })["session"]
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(self.lab.list_devices()[0]["state"], "available")


if __name__ == "__main__":
    unittest.main()
