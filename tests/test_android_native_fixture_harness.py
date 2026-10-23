"""Portable harness contracts only. These tests are NOT native runtime evidence."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


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


if __name__ == "__main__":
    unittest.main()
