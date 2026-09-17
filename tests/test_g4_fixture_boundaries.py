"""Independent fixture admission and real remote state regressions."""
import copy
import http.client
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import unittest

from reproloop.fixtures import AdapterCapabilities, FixtureCoordinator, FixtureError, LoopbackFixtureAdapter
from tests.g4_fixture_support import serve
from tests.test_fixture_allocations import collection_policy, project_document
from tests.test_recording_recovery import open_store


class FixtureService:
    def __init__(self):
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=serve, args=(child,))
        self.process.start()
        child.close()
        if not self.pipe.poll(5):
            self.close()
            raise AssertionError("Fixture service did not start")
        self.port = self.pipe.recv()["port"]

    def request(self, method, path, value=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, None if value is None else json.dumps(value).encode(),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise AssertionError("Fixture control request failed")
            return json.loads(response.read(128 * 1024))
        finally:
            connection.close()

    def state(self):
        return self.request("GET", "/state")

    def close(self):
        try:
            self.pipe.send("stop")
        except (OSError, BrokenPipeError):
            pass
        self.process.join(5)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(3)
        self.pipe.close()


class FixtureBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.budget, self.evidence, self.recordings, _ = open_store(self.root / "recordings")
        self.remote = FixtureService()
        self.coordinator = FixtureCoordinator(self.root / "fixtures")
        self.project = project_document()
        self.registration = self.recordings.register_project(self.project, collection_policy())
        self.adapter = LoopbackFixtureAdapter(
            "fixture_service", f"http://127.0.0.1:{self.remote.port}",
            capabilities=AdapterCapabilities(True, True, 60000))
        self.plan = self.plan_for(self.registration)

    def tearDown(self):
        self.coordinator.close()
        self.remote.close()
        self.recordings.close()
        self.evidence.close()
        self.budget.close()
        self.temp.cleanup()

    def plan_for(self, registration, checks=()):
        return self.coordinator.register_plan(
            registration, application_id="ios_app", fixture_id="seed_account",
            adapter=self.adapter, check_recipe_ids=checks, cleanup_recipe_id="cleanup_account")

    def allocate(self):
        return self.coordinator.reserve(self.plan, owner="owner", device_id="device")

    def test_foreign_project_plan_is_denied_before_a_remote_effect(self):
        allocation = self.allocate()
        other = copy.deepcopy(self.project)
        other["id"] = "other_project"
        registration = self.recordings.register_project(other, collection_policy())
        plan = self.plan_for(registration)
        with self.assertRaises(FixtureError):
            self.coordinator.prepare(plan, allocation, payload={}, operation_id="foreign_prepare")
        self.assertEqual(self.remote.state()["effects"], [])

    def test_no_new_prepare_is_admitted_while_cleanup_response_is_pending(self):
        allocation = self.allocate()
        self.coordinator.prepare(self.plan, allocation, payload={}, operation_id="prepare_first")
        self.remote.request("POST", "/control", {"holdCleanupResponse": True})
        outcomes = []
        errors = []

        def cleanup():
            try:
                outcomes.append(self.coordinator.cleanup(
                    self.plan, allocation, operation_id="cleanup_first", timeout_seconds=2))
            except Exception as exc:
                errors.append(type(exc).__name__)

        thread = threading.Thread(target=cleanup, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if any(item["operationId"] == "cleanup_first" for item in self.remote.state()["effects"]):
                    break
                time.sleep(.005)
            else:
                self.fail("Remote cleanup did not apply")
            with self.assertRaises(FixtureError):
                self.coordinator.prepare(self.plan, allocation, payload={}, operation_id="prepare_during_cleanup")
        finally:
            self.remote.request("POST", "/control", {"holdCleanupResponse": False})
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes[0]["status"], "complete")
        self.assertEqual(self.coordinator.status(allocation)["state"], "available")
        self.assertFalse(self.remote.state()["dirty"])

    def test_unknown_previous_prepare_cannot_be_replaced_by_a_new_operation(self):
        allocation = self.allocate()
        outcome = self.coordinator.prepare(self.plan, allocation, payload={"delayMs": 400},
                                           operation_id="prepare_unknown", timeout_seconds=.02)
        self.assertEqual(outcome.status, "unknown")
        with self.assertRaises(FixtureError):
            self.coordinator.prepare(self.plan, allocation, payload={}, operation_id="replacement_prepare")
        self.assertNotIn("replacement_prepare", self.remote.state()["calls"])
        self.assertEqual(self.coordinator.status(allocation)["state"], "quarantined")

    def test_restart_cannot_change_the_plan_for_an_existing_allocation(self):
        allocation = self.allocate()
        self.coordinator.close()
        self.coordinator = FixtureCoordinator(self.root / "fixtures")
        changed = self.plan_for(self.registration, checks=("check_account",))
        self.assertNotEqual(changed.equivalence_digest, self.plan.equivalence_digest)
        with self.assertRaises(FixtureError):
            self.coordinator.recover_allocation(changed, allocation_id=allocation.allocation_id,
                                                 owner="owner")
        self.assertEqual(self.remote.state()["effects"], [])


if __name__ == "__main__":
    unittest.main()
