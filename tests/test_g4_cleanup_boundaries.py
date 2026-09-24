"""A fixture cannot be reused while its device producer may still run."""
import json
import threading
import time
import unittest
from unittest.mock import patch

from reproof.fixtures import FixtureError
from tests.g4_support import G4Environment, ScenarioProvider


class CleanupBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.env = G4Environment()
        self.handle = self.env.service.start_prepared_recording(
            device_id="device", owner="owner", controller_id="cleanup",
            registration=self.env.registration, application_id="ios_app", build_id="original",
            preparations=self.env.preparations())

    def tearDown(self):
        self.env.close()

    def cleanup_count(self):
        operations = json.loads(self.env.remote.state.read_text())["operations"]
        return sum(item["identity"][-1] == "cleanup" for item in operations.values())

    def test_provider_is_closed_before_fixture_cleanup_can_release_the_slot(self):
        before = self.cleanup_count()
        observed = []
        original = ScenarioProvider.close
        def close(provider):
            observed.append(self.cleanup_count())
            return original(provider)
        with patch.object(ScenarioProvider, "close", close):
            result = self.env.service.stop(self.handle)
        self.assertEqual(observed, [before])
        self.assertEqual(result["issue"]["state"], "complete")
        self.assertEqual(self.cleanup_count(), before + 1)

    def test_unknown_device_cleanup_retains_fixture_exclusion(self):
        self.env.control["fail_close"] = True
        result = self.env.service.stop(self.handle)
        self.assertEqual(result["issue"]["state"], "quarantined")
        with self.assertRaises(FixtureError):
            self.env.fixtures.reserve(self.env.plan, owner="next", device_id="second_device")
        self.assertEqual(result["issue"]["cleanup"][0]["status"], "unknown")

    def test_slow_device_close_returns_unknown_without_releasing_fixture(self):
        release = threading.Event()
        finished = threading.Event()
        original = ScenarioProvider.close
        def close(provider):
            release.wait(.5)
            try:
                return original(provider)
            finally:
                finished.set()
        try:
            with patch.object(ScenarioProvider, "close", close):
                started = time.monotonic()
                result = self.env.service.stop(self.handle, cleanup_timeout_seconds=.05)
            self.assertLess(time.monotonic() - started, .35)
            self.assertEqual(result["issue"]["state"], "quarantined")
            self.assertEqual(result["issue"]["deviceCleanup"], "unknown")
            with self.assertRaises(FixtureError):
                self.env.fixtures.reserve(self.env.plan, owner="next", device_id="second_device")
        finally:
            release.set()
            finished.wait(2)


if __name__ == "__main__":
    unittest.main()
