"""Actual shared HTTP replay across browser authorization changes."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]

from reproloop.live.access import AccessController, AccessStore
from reproloop.live.clock_sync import ClockSynchronizer
from reproloop.live.model import Lab
from reproloop.live.server import LiveServer
from tests.test_clock_sync import FakeClock
from tests.test_fixture_allocations import collection_policy, project_document
from tests.test_project_access import BlockingControl, blocking_device, SharedHttpFixture


class ParentSharedJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = BlockingControl()
        self.store = AccessStore(self.root / "coordinator-v2")
        self.store.bootstrap_administrator("admin")
        self.store.create_identity("admin", "operator")
        self.store.register_project("admin", project_document())
        self.store.grant_membership("admin", "checkout", "operator", "operator")
        self.store.assign_device("admin", "shared-device", project_id="checkout")
        self.token = self.store.issue_principal_credential(
            "admin", "operator", lifetime_seconds=300)["token"]
        self.lab = Lab([blocking_device(self.control)], self.root / "lab",
                       recording_clock_sync=ClockSynchronizer(FakeClock()),
                       recording_wall_clock_ms=lambda: int(time.time() * 1000))
        self.server = None
        self.addCleanup(self.cleanup)
        session = self.lab.create_session("shared-device", "operator", "recorder")
        self.lab.start_recording(session["id"], "operator", session["controllerId"],
                                 session["epoch"], reset=True)
        for sequence in (1, 2):
            frame = self.lab.frame(session["id"])
            self.lab.input(session["id"], "operator", {
                "controllerId": session["controllerId"], "epoch": session["epoch"],
                "sequence": sequence, "commandId": f"record-{sequence}",
                "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
                "action": "tap", "payload": {"x": .5, "y": .5}})
            if sequence == 1:
                time.sleep(.05)
        self.recording = self.lab.stop_recording(
            session["id"], "operator", session["controllerId"], session["epoch"])
        self.lab.close_session(session["id"], "operator")
        self.control.calls.clear()
        self.control.block = True
        access = AccessController(self.store)
        registration = self.lab.register_recording_project(project_document(), collection_policy())
        access.bind_project(registration)
        access.bind_resource("recording", self.recording["id"], "checkout", "operator",
                             meaning="approved")
        self.server = LiveServer(self.lab, access=access)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.http = SimpleNamespace(server=self.server)
        status, value, cookie = self.request(
            "/api/auth/session", method="POST", body={}, token=self.token)
        self.assertEqual(status, 201)
        self.cookie, self.csrf = cookie.split(";", 1)[0], value["csrfToken"]

    def request(self, *args, **kwargs):
        return SharedHttpFixture.request(self.http, *args, **kwargs)

    def cleanup(self):
        self.control.release.set()
        if self.server is not None:
            self.server.close_operations()
        self.lab.close_all()
        if self.server is not None:
            self.server.shutdown()
            self.thread.join(5)
            self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def exercise(self, logout):
        status, value, _ = self.request("/api/jobs", method="POST", body={
            "recordingId": self.recording["id"], "variables": {}, "requestId": "held-job"
        }, cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 202, "Real shared HTTP job submission failed")
        job_id = value["job"]["id"]
        self.assertTrue(self.control.entered.wait(3), "Real job did not reach its first input")
        if logout:
            status, _, _ = self.request("/api/auth/logout", method="POST", body={},
                                         cookie=self.cookie, csrf=self.csrf)
            self.assertEqual(status, 200)
        self.control.release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.server.jobs.get(job_id, "operator")
            if result["state"] in {"failed", "cancelled", "succeeded"}:
                break
            time.sleep(.01)
        self.assertEqual((result["state"], self.control.calls.count("tap")),
                         ("failed", 1) if logout else ("succeeded", 2))

    def test_current_browser_session_can_complete_a_real_http_job(self):
        self.exercise(False)

    def test_logout_prevents_the_next_effect_of_a_real_http_job(self):
        self.exercise(True)

    def test_project_revision_change_still_allows_job_cancellation_and_cleanup(self):
        status, value, _ = self.request("/api/jobs", method="POST", body={
            "recordingId": self.recording["id"], "variables": {}, "requestId": "historical-job"
        }, cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 202)
        job_id = value["job"]["id"]
        self.assertTrue(self.control.entered.wait(3))
        replacement = project_document()
        replacement["revision"] = "revision-two"
        self.store.register_project("admin", replacement)
        registration = self.lab.register_recording_project(replacement, collection_policy())
        self.server.access.bind_project(registration)
        status, _, _ = self.request(f"/api/jobs/{job_id}/cancel", method="POST", body={},
                                     cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.control.release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.server.jobs.get(job_id, "operator")
            if result["state"] in {"failed", "cancelled", "succeeded"}:
                break
            time.sleep(.01)
        self.assertIn(result["state"], {"failed", "cancelled"})
        self.assertTrue(result["cancelRequested"])
        self.assertEqual(self.control.calls.count("tap"), 1)
        self.assertEqual(self.lab.list_devices()[0]["state"], "available")


if __name__ == "__main__":
    unittest.main(verbosity=2)
