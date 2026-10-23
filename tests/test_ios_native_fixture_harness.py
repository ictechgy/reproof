"""Portable CI fixture guards/oracles; never invokes Apple tools or a device."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof.ios_instrumentation import prepare_ios_instrumentation, validate_ios_preparation
from reproof.ios_observation import load_observation_profile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ios_native_fixture_qa", ROOT / "scripts/ios-native-fixture-qa.py")
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)
UUID = "01234567-89AB-CDEF-0123-456789ABCDEF"
NAME = "reproof-ci-ios-42-1"
ENV = {"GITHUB_ACTIONS": "true", "RUNNER_OS": "macOS", "RUNNER_ENVIRONMENT": "github-hosted",
       "GITHUB_REPOSITORY": "ictechgy/reproof", "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1"}
DEVICE = {"name": NAME, "udid": UUID, "state": "Booted", "isAvailable": True,
          "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-16"}


class IOSNativeFixtureHarnessTests(unittest.TestCase):
    def verify(self, devices, **kwargs):
        document = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-18-5": devices}}
        with patch.dict(HARNESS.os.environ, ENV, clear=True), \
             patch.object(HARNESS.platform, "system", return_value="Darwin"), \
             patch.object(HARNESS, "command", return_value=json.dumps(document).encode()) as command:
            result = HARNESS.verify_ci_simulator(kwargs.get("uuid", UUID), kwargs.get("name", NAME))
        return result, command

    def test_non_ci_or_unowned_host_rejects_before_any_command(self):
        for changes in ({"GITHUB_ACTIONS": "false"}, {"RUNNER_ENVIRONMENT": "self-hosted"},
                        {"GITHUB_REPOSITORY": "someone/other"}, {"RUNNER_OS": "Linux"},
                        {"GITHUB_RUN_ID": ""}, {"GITHUB_RUN_ATTEMPT": ""}):
            with self.subTest(changes=changes), patch.dict(HARNESS.os.environ, {**ENV, **changes}, clear=True), \
                 patch.object(HARNESS.platform, "system", return_value="Darwin"), \
                 patch.object(HARNESS, "command") as command:
                with self.assertRaises(HARNESS.GateFailure):
                    HARNESS.verify_ci_simulator(UUID, NAME)
                command.assert_not_called()

    def test_wrong_name_or_malformed_uuid_rejects_before_any_command(self):
        with patch.dict(HARNESS.os.environ, ENV, clear=True), \
             patch.object(HARNESS.platform, "system", return_value="Darwin"), \
             patch.object(HARNESS, "command") as command:
            for selected_uuid, name in ((UUID, "Personal iPhone"), ("-" * 36, NAME), ("booted", NAME)):
                with self.subTest(uuid=selected_uuid, name=name), self.assertRaises(HARNESS.GateFailure):
                    HARNESS.verify_ci_simulator(selected_uuid, name)
            command.assert_not_called()

    def test_exact_owned_available_booted_simulator_is_required(self):
        result, command = self.verify([DEVICE])
        self.assertEqual(result["name"], NAME)
        self.assertEqual(command.call_args.args[0],
                         ["/usr/bin/xcrun", "simctl", "list", "devices", "available", "--json"])
        for changes in ({"name": "User simulator"}, {"state": "Shutdown"},
                        {"isAvailable": False}, {"udid": "other"}):
            with self.subTest(changes=changes), self.assertRaises(HARNESS.GateFailure):
                self.verify([{**DEVICE, **changes}])
        with self.assertRaises(HARNESS.GateFailure):
            self.verify([DEVICE, DEVICE])

    def test_pixel_oracle_requires_exact_confident_strings_and_no_forbidden_state(self):
        words = HARNESS.normalized_words({"observations": [
            {"text": "INVENTORY LIST", "confidence": 1.0},
            {"text": "ITEM TOTAL ONE", "confidence": 0.89},
            {"text": "BLUE MUG", "confidence": 0.99}]})
        self.assertEqual(words, {"INVENTORY LIST", "BLUE MUG"})
        self.assertTrue(HARNESS.state_matches(words, ["INVENTORY LIST", "BLUE MUG"], ["NO ITEMS"]))
        self.assertFalse(HARNESS.state_matches(words, ["ITEM TOTAL ONE"], []))
        self.assertFalse(HARNESS.state_matches({"INVENTORY LIS7"}, ["INVENTORY LIST"], []))
        self.assertFalse(HARNESS.state_matches({"BLUE MUG!"}, ["BLUE MUG"], []))
        self.assertFalse(HARNESS.state_matches(words | {"NO ITEMS"}, ["BLUE MUG"], ["NO ITEMS"]))
        self.assertFalse(HARNESS.state_matches(words, ["IMPOSSIBLE FIXTURE STATE"], []))

    def test_configured_fixture_preparation_preserves_product_bytes(self):
        source = ROOT / "ios-native-fixture"
        profile = load_observation_profile(source / "observation-profile.json")
        before = {name: (source / name).read_bytes() for name in profile.data["sourceInputs"]}
        with tempfile.TemporaryDirectory() as temporary:
            result = prepare_ios_instrumentation(source, Path(temporary) / "prepared", profile=profile)
            receipt = validate_ios_preparation(result["source"])
            self.assertTrue(receipt["productSourcesUnchanged"])
            self.assertFalse(receipt["behaviorVerified"])
            for name, body in before.items():
                self.assertEqual((source / name).read_bytes(), body)
                if not name.endswith("project.pbxproj"):
                    self.assertEqual((Path(result["source"]) / name).read_bytes(), body)
        product = (source / "App/AppDelegate.swift").read_text()
        self.assertNotIn("Recorder.shared", product)
        self.assertNotIn("CounterLogic", product)
        self.assertIn("NAME REQUIRED", product)

    def test_log_gate_rejects_missing_screen_or_lost_events(self):
        events = [{"type": "click", "name": phase, "target": target}
                  for target in ("inventory.add", "inventory.save", "inventory.item.0", "inventory.back", "inventory.cancel")
                  for phase in ("began", "returned")]
        events += [{"type": "screen", "name": "appeared", "target": screen}
                   for screen in ("list", "editor", "detail")]
        value = {"lostEvents": False, "truncated": False, "events": events,
                 "runId": "owned-synthetic-run", "profileDigest": "a" * 64}
        self.assertTrue(HARNESS.verify_app_logs(value)["rawTextAbsent"])
        missing = copy.deepcopy(value)
        missing["events"].pop()
        for invalid in (missing, {**value, "lostEvents": True}, {**value, "truncated": True}):
            with self.assertRaises(HARNESS.GateFailure):
                HARNESS.verify_app_logs(invalid)


if __name__ == "__main__":
    unittest.main()
