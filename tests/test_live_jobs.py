import json
from pathlib import Path
import threading
import tempfile
import time
import unittest

from reproloop.live.jobs import JobQueue
from reproloop.live.model import Lab, LiveError
from reproloop.live.providers import demo_device


CAPABILITIES = {
    "actions": ["tap", "long_press", "swipe", "text", "reset", "home"],
    "inputMode": "gesture-batch", "media": "demo-svg", "multitouch": False,
    "timing": "best-effort", "resetContract": "sample-counter-fixture-v1",
}


class ProviderControl:
    def __init__(self):
        self.start_entered = threading.Event()
        self.start_release = threading.Event()
        self.block_start = False
        self.block_tap = False
        self.tap_entered = threading.Event()
        self.tap_release = threading.Event()
        self.fail_close = False
        self.calls = []
        self.lock = threading.Lock()


class ControlledProvider:
    def __init__(self, control):
        self.control = control

    def start(self, session, lab):
        self.session = session
        self.lab = lab
        self.control.start_entered.set()
        if self.control.block_start:
            self.control.start_release.wait(5)
        lab.publish_frame(session["id"], b"<svg/>", "image/svg+xml", 400, 800, "portrait")

    def execute(self, action, payload):
        with self.control.lock:
            self.control.calls.append(action)
        if action == "tap" and self.control.block_tap:
            self.control.tap_entered.set()
            self.control.tap_release.wait(5)
        self.lab.publish_frame(self.session["id"], b"<svg/>", "image/svg+xml", 400, 800, "portrait")
        return {"ok": True, "timing": "best-effort"}

    def close(self):
        if self.control.fail_close:
            raise RuntimeError("controlled cleanup failure")


def wait_for(queue, job_id, owner="owner", timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = queue.get(job_id, owner)
        if job["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return job
        time.sleep(.01)
    raise AssertionError(queue.get(job_id, owner))


class LiveJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.lab = Lab([demo_device()], Path(self.temp.name))
        self.recording = self._recording()

    def tearDown(self):
        self.lab.close_all()
        self.temp.cleanup()

    def _recording(self):
        session = self.lab.create_session("demo", "owner", "browser")
        self.lab.start_recording(session["id"], "owner", session["controllerId"], session["epoch"], reset=True)
        frame = self.lab.frame(session["id"])
        self.lab.input(session["id"], "owner", {
            "controllerId": session["controllerId"], "epoch": session["epoch"], "sequence": 1,
            "commandId": "command-1", "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
            "action": "tap", "payload": {"x": .5, "y": .5},
        })
        recording = self.lab.stop_recording(session["id"], "owner", session["controllerId"], session["epoch"])
        self.lab.close_session(session["id"], "owner")
        return recording

    def _controlled_lab(self, controls):
        devices = []
        for device_id, control in controls.items():
            devices.append({"id": device_id, "name": device_id, "platform": "demo", "kind": "demo",
                            "capabilities": dict(CAPABILITIES),
                            "factory": lambda control=control: ControlledProvider(control)})
        return Lab(devices, Path(self.temp.name) / ("controlled-" + str(len(controls))))

    def _recording_for(self, lab, device_id, owner="owner", client="recorder", event_count=1, delay_between=0):
        session = lab.create_session(device_id, owner, client)
        lab.start_recording(session["id"], owner, session["controllerId"], session["epoch"], reset=True)
        frame = lab.frame(session["id"])
        for sequence in range(1, event_count + 1):
            if sequence > 1 and delay_between:
                time.sleep(delay_between)
            lab.input(session["id"], owner, {
                "controllerId": session["controllerId"], "epoch": session["epoch"], "sequence": sequence,
                "commandId": "command-{}-{}".format(device_id, sequence), "frameId": frame["id"],
                "geometryVersion": frame["geometryVersion"], "action": "tap",
                "payload": {"x": .5, "y": .5},
            })
        recording = lab.stop_recording(session["id"], owner, session["controllerId"], session["epoch"])
        lab.close_session(session["id"], owner)
        return recording

    def _text_recording(self):
        session = self.lab.create_session("demo", "owner", "browser-text")
        self.lab.start_recording(session["id"], "owner", session["controllerId"], session["epoch"], reset=True)
        frame = self.lab.frame(session["id"])
        self.lab.input(session["id"], "owner", {
            "controllerId": session["controllerId"], "epoch": session["epoch"], "sequence": 1,
            "commandId": "command-text", "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
            "action": "text", "payload": {"value": "private-text"},
        })
        recording = self.lab.stop_recording(session["id"], "owner", session["controllerId"], session["epoch"])
        self.lab.close_session(session["id"], "owner")
        return recording

    def test_replays_repeatedly_and_persists_without_variables(self):
        queue = JobQueue(self.lab, poll_interval=.01)
        job = queue.submit("owner", {"recordingId": self.recording["id"], "variables": {},
                                      "requestId": "request-1", "repeats": 2})
        queue.start()
        result = wait_for(queue, job["id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["completedRuns"], 2)
        stored = json.dumps((Path(self.temp.name) / "jobs" / f'{job["id"]}.json').read_text())
        self.assertNotIn("variables", stored)
        queue.close()

    def test_busy_device_stays_queued_then_runs_after_release(self):
        busy = self.lab.create_session("demo", "external", "external")
        queue = JobQueue(self.lab, poll_interval=.01)
        job = queue.submit("owner", {"recordingId": self.recording["id"], "variables": {}, "requestId": "request-2"})
        queue.start()
        time.sleep(.06)
        self.assertIn(queue.get(job["id"], "owner")["state"], {"queued", "starting"})
        self.lab.close_session(busy["id"], "external")
        self.assertEqual(wait_for(queue, job["id"])["state"], "succeeded")
        queue.close()

    def test_cancel_queued_job_and_owner_fence(self):
        busy = self.lab.create_session("demo", "external", "external")
        queue = JobQueue(self.lab, poll_interval=.01)
        job = queue.submit("owner", {"recordingId": self.recording["id"], "variables": {}, "requestId": "request-3"})
        with self.assertRaises(LiveError):
            queue.get(job["id"], "other")
        self.assertEqual(queue.cancel(job["id"], "owner")["state"], "cancelled")
        self.lab.close_session(busy["id"], "external")
        queue.close()

    def test_retry_is_idempotent_and_changed_variables_conflict(self):
        queue = JobQueue(self.lab, poll_interval=.01)
        request = {"recordingId": self.recording["id"], "variables": {}, "requestId": "request-4"}
        first = queue.submit("owner", request)
        second = queue.submit("owner", request)
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(LiveError):
            queue.submit("owner", dict(request, variables={"text_1": "private"}))
        queue.close()

    def test_simultaneous_request_id_submissions_share_one_job(self):
        queue = JobQueue(self.lab, poll_interval=.01)
        request = {"recordingId": self.recording["id"], "variables": {}, "requestId": "barrier-request"}
        barrier = threading.Barrier(8)
        ids = []
        errors = []

        def submit():
            try:
                barrier.wait(timeout=2)
                ids.append(queue.submit("owner", request)["id"])
            except Exception as exc:  # pragma: no cover - failure is asserted below
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertFalse(errors)
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(queue.list("owner")), 1)
        queue.close()

    def test_max_pending_overflow_allows_retry_of_existing_job(self):
        queue = JobQueue(self.lab, max_pending=1, poll_interval=.01)
        first_request = {"recordingId": self.recording["id"], "variables": {}, "requestId": "pending-1"}
        first = queue.submit("owner", first_request)
        with self.assertRaises(LiveError) as raised:
            queue.submit("owner", {"recordingId": self.recording["id"], "variables": {}, "requestId": "pending-2"})
        self.assertEqual(raised.exception.code, "queue_full")
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(queue.submit("owner", first_request)["id"], first["id"])
        queue.close()

    def test_slow_startup_on_one_device_does_not_block_another(self):
        controls = {"one": ProviderControl(), "two": ProviderControl()}
        lab = self._controlled_lab(controls)
        queue = None
        try:
            first_recording = self._recording_for(lab, "one", client="record-one")
            second_recording = self._recording_for(lab, "two", client="record-two")
            for control in controls.values():
                control.start_entered.clear()
                control.calls.clear()
            controls["one"].block_start = True
            queue = JobQueue(lab, poll_interval=.01, max_running=2)
            first = queue.submit("owner", {"recordingId": first_recording["id"], "variables": {}, "requestId": "slow-one"})
            second = queue.submit("owner", {"recordingId": second_recording["id"], "variables": {}, "requestId": "fast-two"})
            queue.start()
            self.assertTrue(controls["one"].start_entered.wait(2))
            second_result = wait_for(queue, second["id"], timeout=3)
            self.assertEqual(second_result["state"], "succeeded")
            self.assertEqual(queue.get(first["id"], "owner")["state"], "starting")
            controls["one"].start_release.set()
            self.assertEqual(wait_for(queue, first["id"])["state"], "succeeded")
        finally:
            controls["one"].start_release.set()
            if queue is not None:
                queue.close()
            lab.close_all()

    def test_cancel_during_blocked_start_closes_without_replay(self):
        control = ProviderControl()
        lab = self._controlled_lab({"slow": control})
        queue = None
        try:
            recording = self._recording_for(lab, "slow")
            control.start_entered.clear()
            control.calls.clear()
            control.block_start = True
            queue = JobQueue(lab, poll_interval=.01)
            job = queue.submit("owner", {"recordingId": recording["id"], "variables": {}, "requestId": "cancel-start"})
            queue.start()
            self.assertTrue(control.start_entered.wait(2))
            cancel_thread = threading.Thread(target=queue.cancel, args=(job["id"], "owner"))
            cancel_thread.start()
            cancel_thread.join(timeout=2)
            self.assertFalse(cancel_thread.is_alive())
            control.start_release.set()
            result = wait_for(queue, job["id"])
            self.assertEqual(result["state"], "cancelled")
            self.assertEqual(control.calls, [])
            self.assertEqual(lab.list_devices()[0]["state"], "available")
        finally:
            control.start_release.set()
            if queue is not None:
                queue.close()
            lab.close_all()

    def test_running_cancel_stops_before_late_recorded_event(self):
        control = ProviderControl()
        lab = self._controlled_lab({"slow": control})
        queue = None
        try:
            recording = self._recording_for(lab, "slow", event_count=2, delay_between=.5)
            control.start_entered.clear()
            control.calls.clear()
            control.block_tap = True
            queue = JobQueue(lab, poll_interval=.01)
            job = queue.submit("owner", {"recordingId": recording["id"], "variables": {}, "requestId": "cancel-running"})
            queue.start()
            self.assertTrue(control.tap_entered.wait(2))
            cancel_thread = threading.Thread(target=queue.cancel, args=(job["id"], "owner"))
            cancel_thread.start()
            time.sleep(.03)
            control.tap_release.set()
            cancel_thread.join(timeout=2)
            self.assertFalse(cancel_thread.is_alive())
            result = wait_for(queue, job["id"])
            self.assertEqual(result["state"], "cancelled")
            self.assertEqual(control.calls.count("tap"), 1)
        finally:
            control.tap_release.set()
            if queue is not None:
                queue.close()
            lab.close_all()

    def test_timeout_while_device_busy_does_not_close_external_session(self):
        busy = self.lab.create_session("demo", "external", "timeout-external")
        queue = JobQueue(self.lab, poll_interval=.01)
        job = queue.submit("owner", {"recordingId": self.recording["id"], "variables": {},
                                      "requestId": "busy-timeout", "timeoutSeconds": 1})
        queue.start()
        result = wait_for(queue, job["id"], timeout=3)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["errorCode"], "timeout")
        self.assertEqual(self.lab.get_session(busy["id"], "external")["state"], "active")
        queue.close()
        self.lab.close_session(busy["id"], "external")

    def test_equal_timestamp_jobs_preserve_submission_fifo(self):
        control = ProviderControl()
        lab = self._controlled_lab({"fifo": control})
        queue = None
        try:
            recording = self._recording_for(lab, "fifo")
            control.start_entered.clear()
            control.block_start = True
            queue = JobQueue(lab, poll_interval=.01)
            request_one = {"recordingId": recording["id"], "variables": {}, "requestId": "fifo-one"}
            request_two = {"recordingId": recording["id"], "variables": {}, "requestId": "fifo-two"}
            import unittest.mock
            with unittest.mock.patch("reproloop.live.jobs._now_ms", return_value=123456789):
                first = queue.submit("owner", request_one)
                second = queue.submit("owner", request_two)
            queue.start()
            self.assertTrue(control.start_entered.wait(2))
            self.assertEqual(queue.get(first["id"], "owner")["state"], "starting")
            self.assertEqual(queue.get(second["id"], "owner")["state"], "queued")
            control.start_release.set()
            self.assertEqual(wait_for(queue, first["id"])["state"], "succeeded")
            self.assertEqual(wait_for(queue, second["id"])["state"], "succeeded")
        finally:
            control.start_release.set()
            if queue is not None:
                queue.close()
            lab.close_all()

    def test_digest_drift_before_dispatch_fails_without_session_claim(self):
        busy = self.lab.create_session("demo", "external", "digest-external")
        queue = JobQueue(self.lab, poll_interval=.01)
        job = queue.submit("owner", {"recordingId": self.recording["id"], "variables": {}, "requestId": "digest-drift"})
        self.lab.recordings[self.recording["id"]]["data"]["digest"] = "0" * 64
        queue.start()
        self.lab.close_session(busy["id"], "external")
        result = wait_for(queue, job["id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["errorCode"], "recording_changed")
        self.assertIsNone(result["sessionId"])
        queue.close()

    def test_cleanup_failure_fails_job_and_quarantines_device(self):
        control = ProviderControl()
        lab = self._controlled_lab({"cleanup": control})
        queue = None
        try:
            recording = self._recording_for(lab, "cleanup")
            control.fail_close = True
            queue = JobQueue(lab, poll_interval=.01)
            job = queue.submit("owner", {"recordingId": recording["id"], "variables": {}, "requestId": "cleanup-failure"})
            queue.start()
            result = wait_for(queue, job["id"])
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["errorCode"], "cleanup_failed")
            self.assertEqual(lab.list_devices()[0]["state"], "quarantined")
        finally:
            if queue is not None:
                queue.close()
            lab.close_all()

    def test_nonterminal_jobs_recover_as_interrupted(self):
        queue = JobQueue(self.lab, poll_interval=.01)
        job = queue.submit("owner", {"recordingId": self.recording["id"], "variables": {}, "requestId": "request-5"})
        queue.close()
        path = Path(self.temp.name) / "jobs" / f'{job["id"]}.json'
        stored = json.loads(path.read_text())
        stored["job"]["state"] = "running"
        path.write_text(json.dumps(stored))
        restored = JobQueue(self.lab, poll_interval=.01)
        self.assertEqual(restored.get(job["id"], "owner")["state"], "interrupted")
        restored.close()

    def test_text_variables_are_not_persisted_after_cancel_and_restart(self):
        recording = self._text_recording()
        busy = self.lab.create_session("demo", "external", "external-text")
        queue = JobQueue(self.lab, poll_interval=.01)
        job = queue.submit("owner", {"recordingId": recording["id"], "variables": {"text_1": "private-text"},
                                      "requestId": "request-text"})
        queue.start()
        time.sleep(.05)
        queue.cancel(job["id"], "owner")
        path = Path(self.temp.name) / "jobs" / f'{job["id"]}.json'
        self.assertNotIn("private-text", path.read_text())
        queue.close()
        self.lab.close_session(busy["id"], "external")
        restored = JobQueue(self.lab, poll_interval=.01)
        self.assertNotIn("private-text", path.read_text())
        restored.close()


if __name__ == "__main__":
    unittest.main()
