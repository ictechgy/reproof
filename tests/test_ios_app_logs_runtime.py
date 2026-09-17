import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import unittest

from reproloop.core import digest
from reproloop.app_logs import validate_app_log


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "reproloop" / "ios_instrumentation_templates"
FIXTURE = ROOT / "tests" / "fixtures" / "ios-app-logs"
XCODE_DEVELOPER = Path("/Applications/Xcode-27.0.0-beta.app/Contents/Developer")


def _available():
    return XCODE_DEVELOPER.is_dir() and shutil.which("xcrun") is not None


@unittest.skipUnless(_available(), "Xcode iOS SDK is unavailable")
class IOSAppLogNativeCompileTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "PATH": os.environ.get("PATH", ""),
            "DEVELOPER_DIR": str(XCODE_DEVELOPER),
        }
        result = subprocess.run(
            ["xcrun", "--sdk", "iphonesimulator", "--show-sdk-path"],
            check=True,
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=10,
        )
        self.sdk = result.stdout.strip()

    def run_checked(self, command, timeout=30):
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=timeout,
        )
        if result.returncode:
            self.fail(f"command failed: {command!r}; stderr={result.stderr!r}")
        return result

    def test_debug_runtime_and_objc_bootstrap_compile_with_app_log_surface(self):
        self.run_checked(
            [
                "xcrun", "swiftc", "-D", "DEBUG", "-swift-version", "5",
                "-warnings-as-errors", "-typecheck", "-sdk", self.sdk,
                "-target", "arm64-apple-ios16.0-simulator",
                str(TEMPLATES / "RLAutomaticRecorder.swift"),
                str(TEMPLATES / "ReproRuntimeIdentity.swift"),
                str(ROOT / "tests/fixtures/ios-auto-runtime/RLAutoConfig.swift"),
            ]
        )
        self.run_checked(
            [
                "xcrun", "clang", "-fsyntax-only", "-fobjc-arc", "-fmodules",
                "-Werror", "-isysroot", self.sdk,
                "-mios-simulator-version-min=16.0", "-DREPRO_AUTO_DEBUG=1",
                str(TEMPLATES / "RLAutoBootstrap.m"),
            ]
        )


class IOSAppLogContractFixtureTests(unittest.TestCase):
    def test_schema_gate_and_marker_identity_are_exact(self):
        with (FIXTURE / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        self.assertIs(type(info["ReproAppLogSchemaVersion"]), int)
        self.assertEqual(info["ReproAppLogSchemaVersion"], 1)
        profile = json.loads((FIXTURE / "profile.json").read_text())
        self.assertEqual(info["ReproAutoProfile"], profile)
        self.assertEqual(info["ReproAutoProfileDigest"], digest(profile))

    def test_snapshot_event_schema_is_bounded_and_contains_no_raw_channels(self):
        document = json.loads((FIXTURE / "app-log.json").read_text())
        self.assertEqual(
            set(document),
            {"schemaVersion", "platform", "applicationId", "runId", "sessionId",
             "profileDigest", "startedAtMs", "endSequence", "truncated", "lostEvents", "events"},
        )
        self.assertEqual(document["endSequence"], len(document["events"]))
        marker = {key: document[key] for key in {
            "schemaVersion", "platform", "applicationId", "runId", "sessionId", "profileDigest", "startedAtMs"
        }}
        self.assertEqual(
            validate_app_log(document, marker,
                             click_targets={"counter.add", "counter.next", "counter.reset", "counter.back"},
                             screen_targets={"main", "details"}),
            document,
        )
        event_keys = {"seq", "elapsedMs", "type", "name", "component", "componentId", "target"}
        previous_elapsed = -1
        for index, event in enumerate(document["events"], 1):
            self.assertEqual(set(event), event_keys)
            self.assertEqual(event["seq"], index)
            self.assertGreaterEqual(event["elapsedMs"], previous_elapsed)
            previous_elapsed = event["elapsedMs"]
            self.assertIn(event["type"], {"lifecycle", "click", "screen"})
            self.assertRegex(event["componentId"], r"^(app|c[0-9a-f]{16})$")
            self.assertNotIn("className", event)
            self.assertNotIn("label", event)
            self.assertNotIn("exception", event)
        self.assertLessEqual(len(document["events"]), 2000)
        self.assertLessEqual(len((FIXTURE / "app-log.json").read_bytes()), 1024 * 1024)

    def test_recovered_prefix_with_io_loss_flag_remains_downloadable(self):
        document = json.loads((FIXTURE / "app-log.json").read_text())
        document["lostEvents"] = True
        marker = {key: document[key] for key in {
            "schemaVersion", "platform", "applicationId", "runId", "sessionId", "profileDigest", "startedAtMs"
        }}
        validated = validate_app_log(
            document,
            marker,
            click_targets={"counter.add", "counter.next", "counter.reset", "counter.back"},
            screen_targets={"main", "details"},
        )
        self.assertTrue(validated["lostEvents"])


if __name__ == "__main__":
    unittest.main()
