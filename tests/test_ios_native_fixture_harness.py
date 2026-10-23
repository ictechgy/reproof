"""Portable CI fixture guards/oracles; never invokes Apple tools or a device."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reproof.core import ContractError
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


def synthetic_build():
    return {"app": Path("/owned-synthetic/InventoryFixture.app"), "receipt": {
        "schemaVersion": 2, "kind": "configured-uikit-build", "platform": "ios",
        "applicationId": "io.reproof.fixture.inventory", "executionEnvironment": "simulator",
        "artifactDigest": "a" * 64, "artifactBytes": 4096, "sourceDigest": "b" * 64,
        "bundleVersion": "1.0", "bundleBuild": "1", "buildCompleted": True,
        "signed": False, "automaticObservations": False}}


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
            result = prepare_ios_instrumentation(source, Path(temporary).resolve(strict=True) / "prepared", profile=profile)
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

    def test_owned_symlink_parent_is_canonicalized_without_weakening_artifact_boundary(self):
        source = ROOT / "ios-native-fixture"
        profile = load_observation_profile(source / "observation-profile.json")
        before = {name: (source / name).read_bytes() for name in profile.data["sourceInputs"]}
        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer).resolve(strict=True)
            real_parent = root / "private-var"
            real_parent.mkdir()
            alias = root / "var"
            alias.symlink_to(real_parent, target_is_directory=True)
            with tempfile.TemporaryDirectory(prefix="owned-ci-", dir=alias) as name:
                # Reproduce the macOS /var -> /private/var boundary rejection.
                lexical_root = Path(name)
                with self.assertRaisesRegex(ContractError, "real existing parent directories"):
                    prepare_ios_instrumentation(source, lexical_root / "prepared", profile=profile)
                self.assertFalse((lexical_root / "prepared").exists())
                # Use the same helper as main with an owner-created directory.
                canonical = HARNESS.owned_temporary_root(SimpleNamespace(name=name))
                self.assertEqual(canonical.parent, real_parent)
                self.assertNotEqual(canonical, lexical_root)
                result = prepare_ios_instrumentation(source, canonical / "prepared", profile=profile)
                receipt = validate_ios_preparation(result["source"])
                self.assertTrue(receipt["productSourcesUnchanged"])
                self.assertFalse(receipt["behaviorVerified"])
                for filename, body in before.items():
                    self.assertEqual((source / filename).read_bytes(), body)
                    if not filename.endswith("project.pbxproj"):
                        self.assertEqual((Path(result["source"]) / filename).read_bytes(), body)
                # The production rejection is retained after successful preparation.
                with self.assertRaisesRegex(ContractError, "real existing parent directories"):
                    prepare_ios_instrumentation(source, lexical_root / "another", profile=profile)

    def test_semantic_probe_reaches_exact_unsupported_gate_before_provider_effects(self):
        from reproof.live import providers
        args = SimpleNamespace(simulator=UUID, helper_products=Path("/owned-synthetic/products"))
        with patch.object(HARNESS, "ios_device", wraps=providers.ios_device) as selected, \
             patch.object(providers, "_general_ios_simulator_identity") as identity, \
             patch.object(providers, "IosProvider") as provider, \
             patch.object(providers.subprocess, "run") as native_run, \
             patch.object(providers.subprocess, "Popen") as native_start:
            result = HARNESS.reject_semantic_claim(args, synthetic_build())
        self.assertEqual(result, {"status": "unsupported", "code": "unsupported_operation",
                                  "beforeProviderStartup": True, "coordinateFallback": False})
        self.assertEqual(selected.call_args.kwargs["profile"]["capabilities"]["locator"]["targets"],
                         ["inventory_save"])
        identity.assert_not_called()
        provider.assert_not_called()
        native_run.assert_not_called()
        native_start.assert_not_called()
        # A malformed declaration is an invalid contract, never capability proof.
        invalid = synthetic_build()
        invalid["receipt"]["artifactDigest"] = "invalid-sha"
        with patch.object(HARNESS, "ios_device") as selected:
            with self.assertRaises(ContractError) as rejected:
                HARNESS.reject_semantic_claim(args, invalid)
            selected.assert_not_called()
        self.assertNotIsInstance(rejected.exception, HARNESS.LiveError)

    def test_completed_build_receipts_survive_later_failure(self):
        # Exercise main's result writes with inert build/Apple-tool doubles only.
        for fail_at in ("second_build", "semantic_probe"):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve(strict=True)
                products = root / "products"
                products.mkdir()
                output = root / "evidence"
                completed = synthetic_build()
                calls = []

                def build(*args, **kwargs):
                    # Each prior build must already be persisted before this call.
                    if calls:
                        stored = json.loads((output / "result.json").read_text())
                        self.assertEqual(len(stored["builds"]), len(calls))
                    if fail_at == "second_build" and calls:
                        raise HARNESS.GateFailure("synthetic second build failure")
                    calls.append(args[1].name)
                    return copy.deepcopy(completed)

                with patch.object(HARNESS, "verify_ci_simulator", return_value={}), \
                     patch.object(HARNESS, "build_observation_app", side_effect=build), \
                     patch.object(HARNESS, "command", return_value=b""), \
                     patch.object(HARNESS, "reject_semantic_claim", side_effect=HARNESS.GateFailure("synthetic probe failure")), \
                     patch.object(HARNESS, "run_flow") as flow, redirect_stdout(io.StringIO()):
                    with self.assertRaises(HARNESS.GateFailure):
                        HARNESS.main(["--simulator", UUID, "--owned-simulator-name", NAME,
                                      "--helper-products", str(products), "--output", str(output)])
                    flow.assert_not_called()
                result = json.loads((output / "result.json").read_text())
                self.assertFalse(result["passed"])
                self.assertEqual(set(result["builds"]), {"original"} if fail_at == "second_build"
                                 else {"original", "instrumented", "release"})
                self.assertTrue(all(row["buildCompleted"] for row in result["builds"].values()))

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
