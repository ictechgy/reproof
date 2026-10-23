"""Portable harness contracts only. These tests are NOT native runtime evidence."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from reproof.core import ContractError


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "android_runtime_fixture_qa", ROOT / "scripts/android-runtime-fixture-qa.py")
QA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QA)


class AndroidRuntimeFixtureHarnessTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "CI": "true", "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
            "GITHUB_REPOSITORY": "ictechgy/reproof", "REPROOF_OWNED_ANDROID_EMULATOR": "1",
            "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "2", "ANDROID_SERIAL": "emulator-5554",
        }

    def guard(self, environment=None, serial="emulator-5554", name="reproof-ci-12345-2", platform="linux"):
        with patch.dict(os.environ, self.environment if environment is None else environment, clear=True), \
                patch.object(QA.sys, "platform", platform):
            QA.environment_guard(serial, name)

    def test_exact_owned_hosted_job_is_accepted_without_device_access(self):
        self.guard()

    def test_all_ci_ownership_conditions_are_required(self):
        for key in self.environment:
            if key == "ANDROID_SERIAL":
                continue  # The explicit --serial is sufficient when the env is unset.
            with self.subTest(key=key):
                changed = dict(self.environment)
                changed.pop(key)
                with self.assertRaises(RuntimeError):
                    self.guard(changed)
        for key, value in (("CI", "false"), ("RUNNER_ENVIRONMENT", "self-hosted"),
                           ("GITHUB_REPOSITORY", "other/repository"),
                           ("ANDROID_SERIAL", "physical-device")):
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                self.guard(dict(self.environment, **{key: value}))

    def test_wrong_device_name_run_attempt_and_platform_fail_closed(self):
        for kwargs in ({"serial": "physical-device"}, {"serial": "127.0.0.1:5555"},
                       {"name": "reproof-ci-12345-1"}, {"name": "unrelated-avd"},
                       {"platform": "darwin"}, {"platform": "win32"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                self.guard(**kwargs)

    def test_remote_adb_environment_overrides_are_rejected(self):
        for key in ("ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT", "ADB_SERVER_PORT"):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.guard(dict(self.environment, **{key: "overridden"}))

    def test_non_ci_preflight_rejects_before_device_discovery(self):
        args = type("Arguments", (), {"serial": "emulator-5554", "owned_emulator_name": "reproof-ci-12345-2"})()
        with patch.dict(os.environ, {}, clear=True), patch.object(QA, "AdbDevice") as device:
            with self.assertRaises(RuntimeError):
                QA.preflight(args)
            device.assert_not_called()

    def test_profile_binds_fake_bytes_without_claiming_native_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            app, helper = Path(directory) / "fixture.apk", Path(directory) / "helper.apk"
            app.write_bytes(b"portable contract test, not an APK")
            helper.write_bytes(b"portable helper metadata only")
            profile, provenance = QA.runtime_profile(app, helper)
            self.assertEqual(profile.package, QA.PACKAGE)
            self.assertEqual(profile.data["artifact"]["sha256"], QA.sha_file(app))
            self.assertEqual(profile.data["artifact"]["bytes"], app.stat().st_size)
            self.assertEqual(profile.data["artifact"]["provenanceDigest"], QA.digest(provenance))
            self.assertEqual(profile.data["capabilities"]["actions"], ["tap", "text", "launch"])
            self.assertEqual(profile.data["capabilities"]["observations"], ["pixels", "accessibility"])
            self.assertIsNone(profile.data["capabilities"]["logAdapter"])
            self.assertNotIn("fixture", profile.native())
            self.assertNotIn("reset", profile.native()["actions"])
            self.assertEqual(profile.native()["schemaVersion"], 2)

    def test_fixture_has_exact_stable_ids_and_no_android_permissions(self):
        path = ROOT / "android/runtime-fixture/src/main"
        identifiers = ET.parse(path / "res/values/ids.xml").getroot()
        self.assertEqual(set(QA.TARGETS), {node.attrib["name"] for node in identifiers})
        manifest = ET.parse(path / "AndroidManifest.xml").getroot()
        self.assertEqual(list(manifest.iter("uses-permission")), [])
        self.assertEqual(list(manifest.iter("uses-permission-sdk-23")), [])

    def test_diagnostic_call_preserves_exact_arguments_return_and_exception(self):
        observer = QA.StartupDiagnostics()
        argument, keyword, returned = object(), object(), object()
        calls = []
        def success(*args, **kwargs):
            calls.append((args, kwargs))
            return returned
        self.assertIs(observer.call("portable_probe", success, argument, named=keyword), returned)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0][0], argument)
        self.assertIs(calls[0][1]["named"], keyword)
        failure = QA.LiveError("native_protocol_mismatch", "private-secret-must-not-appear")
        def rejected(*args, **kwargs):
            calls.append((args, kwargs))
            raise failure
        with self.assertRaises(QA.LiveError) as caught:
            observer.call("portable_failure", rejected, argument)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(calls), 2)
        report = observer.report()
        self.assertEqual(report["failures"][0]["code"], "native_protocol_mismatch")
        self.assertNotIn("private-secret", json.dumps(report))
        self.assertNotIn("message", report["failures"][0])

    def test_diagnostic_instance_wrappers_keep_provider_type_and_native_calls(self):
        # Construct no device and invoke no production/native action. This only
        # checks wrapper transparency around explicitly synthetic callables.
        provider = object.__new__(QA.AndroidLiveProvider)
        provider.app_profile = SimpleNamespace(digest="a" * 64, native_digest="b" * 64)
        calls, result = [], object()
        def original(*args, **kwargs):
            calls.append((args, kwargs))
            return result
        provider._install = original
        provider._validate_native_metadata = original
        provider._execute = original
        provider.start_authorized = original
        provider.frame_clock = SimpleNamespace(accept_status=original, frame_arguments=original)
        observer = QA.StartupDiagnostics()
        self.assertIs(observer.observe_provider(provider), provider)
        self.assertIs(type(provider), QA.AndroidLiveProvider)
        sentinel = object()
        self.assertIs(provider._install(sentinel, QA.PACKAGE), result)
        self.assertIs(provider._execute, original)
        self.assertIs(provider._execute("launch", sentinel), result)
        body = {"ready": True, "profileDigest": "a" * 64, "nativeDigest": "b" * 64,
                "targetPackage": QA.PACKAGE, "token": "private-token-must-not-appear"}
        self.assertIs(provider._validate_native_metadata(body), result)
        self.assertIs(provider.frame_clock.accept_status(sentinel), result)
        self.assertIs(provider.frame_clock.frame_arguments, original)
        self.assertIs(provider.frame_clock.frame_arguments(sentinel), result)
        self.assertIs(provider.start_authorized(sentinel), result)
        self.assertEqual(len(calls), 6)
        self.assertIs(calls[0][0][0], sentinel)
        self.assertIs(calls[2][0][0], body)
        self.assertIs(provider._install, original)
        self.assertIs(provider._validate_native_metadata, original)
        self.assertIs(provider.start_authorized, original)
        self.assertIs(provider.frame_clock.accept_status, original)
        report = observer.report()
        self.assertTrue(report["lastNativeMetadata"]["profileDigestMatches"])
        self.assertNotIn("private-token", json.dumps(report))
        self.assertEqual(report["observerScope"], "instance-call-through")
        self.assertTrue(report["startupObserversRemoved"])

    def test_diagnostics_redact_unknown_codes_messages_and_lab_notifications(self):
        observer = QA.StartupDiagnostics()
        original_calls, returned = [], object()
        def original(sid, message):
            original_calls.append((sid, message))
            return returned
        lab = SimpleNamespace(fail=original)
        observer.observe_lab(lab)
        self.assertIs(lab.fail("private-session", "private-native-body"), returned)
        self.assertEqual(original_calls, [("private-session", "private-native-body")])
        unknown = observer.error(QA.LiveError("private_code", "private-body"))
        self.assertEqual(unknown, {"type": "LiveError"})
        known = observer.error(ContractError("Clock mapping uncertainty is excessive"))
        self.assertEqual(known, {"type": "ContractError", "reason": "clock_mapping_uncertainty_excessive"})
        report = observer.report()
        self.assertEqual(report["labFailureReasons"], ["unclassified_provider_failure"])
        self.assertNotIn("private", json.dumps(report))

    def test_diagnostic_event_storage_is_bounded(self):
        observer = QA.StartupDiagnostics()
        for index in range(100):
            observer.call("portable_probe_" + str(index), lambda: None)
        report = observer.report()
        self.assertEqual(len(report["events"]), 64)
        self.assertEqual(report["droppedEvents"], 136)

    def test_healthy_repeated_startup_checks_do_not_emit_hotpath_events(self):
        observer = QA.StartupDiagnostics()
        for _ in range(100):
            observer.call("native_metadata", lambda: None)
        report = observer.report()
        self.assertEqual(len(report["events"]), 2)
        self.assertEqual(report["droppedEvents"], 0)


if __name__ == "__main__":
    unittest.main()
