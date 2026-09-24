"""Locator evidence belongs to the current acquisition and live session."""
import unittest
from unittest.mock import patch

from reproof.live.model import LiveError
from tests.g4_support import G4Environment, ScenarioProvider


class LocatorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.env = G4Environment()
        self.handle = self.env.service.start_prepared_recording(
            device_id="device", owner="owner", controller_id="locator",
            registration=self.env.registration, application_id="ios_app", build_id="original",
            preparations=self.env.preparations())

    def tearDown(self):
        self.env.close()

    def lookup(self):
        return self.env.lab.resolve_locator(self.handle.session_id, "owner",
                                            {"kind": "accessibility-id", "value": "checkout"})

    def test_old_locator_timestamp_cannot_be_reused_with_a_current_frame(self):
        original = ScenarioProvider.resolve_locator
        def stale(provider, target):
            value = original(provider, target)
            value["observedAtMs"] -= 60000
            return value
        with patch.object(ScenarioProvider, "resolve_locator", stale):
            with self.assertRaises(LiveError):
                self.lookup()

    def test_callback_completion_after_revocation_cannot_return_evidence(self):
        original = ScenarioProvider.resolve_locator
        def revoke(provider, target):
            value = original(provider, target)
            self.env.lab.fail(self.handle.session_id, "Synthetic revocation")
            return value
        with patch.object(ScenarioProvider, "resolve_locator", revoke):
            with self.assertRaises(LiveError):
                self.lookup()


if __name__ == "__main__":
    unittest.main()
