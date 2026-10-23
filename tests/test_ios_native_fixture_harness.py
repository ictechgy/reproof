"""Portable CI fixture guards/oracles; never invokes Apple tools or a device."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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
                     patch.object(HARNESS, "helper_preflight", return_value={}), \
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

    def test_helper_preflight_uses_real_template_and_metadata_gates_without_exporting_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            products = root / "products"
            products.mkdir()
            host, target, app = (products / name for name in ("Runner.app", "Tests.xctest", "Host.app"))
            for path in (host, target, app):
                path.mkdir()
            info = target / "Info.plist"
            info.write_bytes(plistlib.dumps({"ReproLiveProtocolVersion": 2, "ReproLiveHelperVersion": 2}))
            template = {"__xctestrun_metadata__": {"FormatVersion": 1}, "ReproLiveTests": {
                "BlueprintName": "ReproLiveTests", "TestHostPath": "__TESTROOT__/Runner.app",
                "TestBundlePath": "__TESTROOT__/Tests.xctest", "UITargetAppPath": "__TESTROOT__/Host.app",
                "EnvironmentVariables": {"PRIVATE_TOKEN": "never-publish-this-sentinel"}}}
            (products / "ReproLive.xctestrun").write_bytes(plistlib.dumps(template))
            result = HARNESS.helper_preflight(products, root)
            self.assertEqual(result["fixedTargetCount"], 1)
            self.assertTrue(all(result["artifactPaths"].values()))
            self.assertNotIn("never-publish", json.dumps(result))
            self.assertNotIn(str(root), json.dumps(result))
            info.write_bytes(plistlib.dumps({"ReproLiveProtocolVersion": 1, "ReproLiveHelperVersion": 2}))
            with self.assertRaises(HARNESS.LiveError):
                HARNESS.helper_preflight(products, root)

    def test_startup_observer_preserves_exact_return_exception_and_original_methods(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                provider = HARNESS.IosProvider(UUID, "/synthetic/products", "fixture.example")
                returned = object()
                failure = HARNESS.LiveError("startup_rejected", "private-token-sentinel")
                original_start = Mock(side_effect=failure if raises else None, return_value=returned)
                provider.start_authorized = original_start
                original_bridge = provider.bridge
                original_popen = HARNESS.subprocess.Popen
                original_provider_subprocess = HARNESS.ios_providers.subprocess
                diagnostics = HARNESS.StartupDiagnostics()
                self.assertIs(diagnostics.observe_provider(provider), provider)
                self.assertIs(HARNESS.subprocess.Popen, original_popen)
                if raises:
                    with self.assertRaises(HARNESS.LiveError) as caught:
                        provider.start_authorized("session", "lab", "permit")
                    self.assertIs(caught.exception, failure)
                else:
                    self.assertIs(provider.start_authorized("session", "lab", "permit"), returned)
                original_start.assert_called_once_with("session", "lab", "permit")
                self.assertIs(provider.start_authorized, original_start)
                self.assertEqual(provider.bridge, original_bridge)
                self.assertIs(HARNESS.subprocess.Popen, original_popen)
                self.assertIs(HARNESS.ios_providers.subprocess, original_provider_subprocess)
                self.assertIs(HARNESS.ios_providers.subprocess.Popen, original_popen)
                snapshot = diagnostics.snapshot()
                self.assertTrue(snapshot["startupObserversRemoved"])
                self.assertNotIn("private-token-sentinel", json.dumps(snapshot))
                self.assertNotIn("permit", json.dumps(snapshot))

    def test_popen_capture_is_exact_call_through_and_nonmatching_invocations_are_untouched(self):
        diagnostics = HARNESS.StartupDiagnostics()
        provider = HARNESS.IosProvider(UUID, "/synthetic/products", "fixture.example")
        provider.temp = SimpleNamespace(name="/private/owned-ci")
        diagnostics.provider = provider
        argv = ["/usr/bin/xcodebuild", "test-without-building", "-xctestrun", "/private/owned-ci/live.xctestrun",
            "-destination", "id=" + UUID, "-resultBundlePath", "/private/owned-ci/result.xcresult",
            "-parallel-testing-enabled", "NO", "-only-testing:ReproLiveTests/LiveControlTests/testControlSession"]
        original_kwargs = {"stdout": HARNESS.subprocess.DEVNULL, "stderr": HARNESS.subprocess.DEVNULL,
                           "start_new_session": True}
        process = SimpleNamespace(stdout=None)
        calls = []
        def original(command, **kwargs):
            calls.append((command, kwargs))
            os.write(kwargs["stdout"], b"Testing started\nprivate-token-sentinel\n")
            return process
        self.assertIs(diagnostics.popen(original, argv, **original_kwargs), process)
        diagnostics.drainer.join(timeout=2)
        self.assertFalse(diagnostics.drainer.is_alive())

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], argv)
        self.assertTrue(calls[0][1]["start_new_session"])
        self.assertEqual(calls[0][1]["stdout"], calls[0][1]["stderr"])
        self.assertIsNone(process.stdout)
        self.assertEqual(diagnostics.snapshot()["xctest"]["stages"], ["testing_started"])
        self.assertNotIn("private-token", json.dumps(diagnostics.snapshot()))
        for changed in (["/usr/bin/xcrun", "simctl", "list"], [*argv[:-1], "-only-testing:Other"],
                        [*argv[:5], "id=someone-elses-simulator", *argv[6:]]):
            untouched = Mock(return_value=process)
            self.assertIs(diagnostics.popen(untouched, changed, **original_kwargs), process)
            untouched.assert_called_once_with(changed, **original_kwargs)
        unusual_kwargs = {**original_kwargs, "env": {"PRIVATE_TOKEN": "sentinel"}}
        untouched = Mock(return_value=process)
        diagnostics.popen(untouched, argv, **unusual_kwargs)
        untouched.assert_called_once_with(argv, **unusual_kwargs)
        failure = OSError("private-token-sentinel")
        failing = Mock(side_effect=failure)
        with self.assertRaises(OSError) as caught:
            diagnostics.popen(failing, argv, **original_kwargs)
        self.assertIs(caught.exception, failure)
        failing.assert_called_once()
        diagnostics.drainer.join(timeout=2)
        self.assertFalse(diagnostics.drainer.is_alive())

    def test_startup_snapshot_failure_never_masks_original_or_blocks_popen_type_restoration(self):
        provider = HARNESS.IosProvider(UUID, "/synthetic/products", "fixture.example")
        failure = HARNESS.LiveError("startup_rejected", "private-token-sentinel")
        provider.start_authorized = Mock(side_effect=failure)
        provider.process = SimpleNamespace(poll=Mock(side_effect=OSError("private-snapshot-error")))
        original_subprocess = HARNESS.ios_providers.subprocess
        diagnostics = HARNESS.StartupDiagnostics()
        diagnostics.observe_provider(provider)
        with self.assertRaises(HARNESS.LiveError) as caught:
            provider.start_authorized({}, None, None)
        self.assertIs(caught.exception, failure)
        self.assertIs(HARNESS.ios_providers.subprocess, original_subprocess)
        self.assertTrue(diagnostics.snapshot()["snapshotUnavailable"])
        self.assertTrue(diagnostics.snapshot()["startupObserversRemoved"])
        self.assertNotIn("private-snapshot-error", json.dumps(diagnostics.snapshot()))
        # Portable owned child, not an Apple tool/device: the cleanup guard's
        # real-Popen type check remains valid immediately after failed startup.
        with HARNESS.subprocess.Popen([HARNESS.sys.executable, "-c", "pass"],
                                     stdout=HARNESS.subprocess.DEVNULL,
                                     stderr=HARNESS.subprocess.DEVNULL) as process:
            self.assertIsInstance(process, HARNESS.ios_providers.subprocess.Popen)
            self.assertEqual(process.wait(timeout=5), 0)

    def test_native_output_classifier_is_bounded_allowlisted_and_stops_at_startup(self):
        diagnostics = HARNESS.StartupDiagnostics()
        lines = [b"Testing started", b"** TEST FAILED **",
            b"Test Case '-[ReproLiveTests.LiveControlTests testControlSession]' started.",
            b"/private/sentinel.swift:337: error: -[ReproLiveTests.LiveControlTests testControlSession] : failed - bridge_request_failed",
            b'Error Domain=NSPOSIXErrorDomain Code=13 "private-token-sentinel"',
            b'Error Domain=SecretDomain Code=123456 "private-token-sentinel"',
            b'Error Domain=NSPOSIXErrorDomain Code=1234567 "private-token-sentinel"',
            b"private-token-sentinel", b"unknown message that must not become a guessed reason"]
        for line in lines:
            diagnostics.classify_line(line)
        output = diagnostics.snapshot()["xctest"]
        self.assertEqual(output["stages"], ["testing_started", "testing_failed", "control_test_started"])
        self.assertEqual(output["nativeFailureCategories"], ["bridge_request_failed"])
        self.assertEqual(output["appleErrors"], [{"domain": "NSPOSIXErrorDomain", "code": 13}])
        self.assertEqual(output["unclassifiedLines"], 4)
        self.assertNotIn("private-token", json.dumps(output))
        self.assertNotIn("sentinel.swift", json.dumps(output))
        self.assertNotIn("SecretDomain", json.dumps(output))
        stream = io.BytesIO(b"x" * (HARNESS.StartupDiagnostics.MAX_LINE_BYTES * 20) + b"\nTesting started\n")
        diagnostics.drain(stream)
        self.assertTrue(stream.closed)
        self.assertEqual(diagnostics.snapshot()["xctest"]["oversizedLines"], 1)
        diagnostics.finish()
        before = diagnostics.snapshot()
        diagnostics.classify_line(b"** TEST SUCCEEDED **")
        diagnostics.drain(io.BytesIO(b"** TEST SUCCEEDED **\nprivate-token-sentinel\n"))
        self.assertEqual(diagnostics.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
