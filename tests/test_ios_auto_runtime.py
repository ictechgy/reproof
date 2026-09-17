import os
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest

from reproloop.core import digest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "reproloop" / "ios_instrumentation_templates"
FIXTURE = ROOT / "tests" / "fixtures" / "ios-auto-runtime"
XCODE_DEVELOPER = Path("/Applications/Xcode-27.0.0-beta.app/Contents/Developer")


def _toolchain_available():
    return XCODE_DEVELOPER.is_dir() and shutil.which("xcrun") is not None


def _scrubbed_environment():
    return {
        "PATH": os.environ.get("PATH", ""),
        "DEVELOPER_DIR": str(XCODE_DEVELOPER),
    }


@unittest.skipUnless(_toolchain_available(), "Xcode iOS SDK is unavailable")
class IOSAutomaticRuntimeCompileTests(unittest.TestCase):
    def setUp(self):
        self.environment = _scrubbed_environment()
        sdk = subprocess.run(
            ["xcrun", "--sdk", "iphonesimulator", "--show-sdk-path"],
            check=True,
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=10,
        )
        self.sdk = sdk.stdout.strip()

    def _run_checked(self, command, *, timeout=30):
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=self.environment,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            self.fail(f"subprocess timed out after {timeout}s: {command!r}; output={error.output!r}")
        if result.returncode != 0:
            self.fail(
                f"subprocess failed ({result.returncode}): {command!r}; "
                f"stdout={result.stdout!r}; stderr={result.stderr!r}"
            )
        return result

    def test_debug_swift_collector_typechecks_against_uikit_sdk(self):
        self._run_checked(
            [
                "xcrun",
                "swiftc",
                "-D",
                "DEBUG",
                "-typecheck",
                "-sdk",
                self.sdk,
                "-target",
                "arm64-apple-ios16.0-simulator",
                str(TEMPLATES / "ReproRuntimeIdentity.swift"),
                str(TEMPLATES / "RLAutomaticRecorder.swift"),
                str(FIXTURE / "RLAutoConfig.swift"),
            ],
        )

    def test_objc_bootstrap_typechecks_with_and_without_debug_macro(self):
        for macro in ("REPRO_AUTO_DEBUG=1", None):
            command = [
                "xcrun",
                "clang",
                "-fsyntax-only",
                "-fobjc-arc",
                "-fmodules",
                "-isysroot",
                self.sdk,
                "-mios-simulator-version-min=16.0",
            ]
            if macro:
                command.extend([f"-D{macro}"])
            command.append(str(TEMPLATES / "RLAutoBootstrap.m"))
            self._run_checked(command)

    def test_release_swift_compilation_emits_no_runtime_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            header = Path(directory) / "Release-Swift.h"
            self._run_checked(
                [
                    "xcrun",
                    "swiftc",
                    "-sdk",
                    self.sdk,
                    "-target",
                    "arm64-apple-ios16.0-simulator",
                    "-parse-as-library",
                    "-emit-module",
                    "-emit-objc-header-path",
                    str(header),
                    "-o",
                    str(Path(directory) / "Release.swiftmodule"),
                    "-module-name",
                    "ReproSample",
                    str(TEMPLATES / "ReproRuntimeIdentity.swift"),
                    str(TEMPLATES / "RLAutomaticRecorder.swift"),
                    str(FIXTURE / "RLAutoConfig.swift"),
                ],
            )
            self.assertNotIn("RLAutomaticRecorder", header.read_text())

    def test_generated_objc_surface_contains_manual_runtime_entrypoints(self):
        with tempfile.TemporaryDirectory() as directory:
            header = Path(directory) / "ReproSample-Swift.h"
            self._run_checked(
                [
                    "xcrun",
                    "swiftc",
                    "-D",
                    "DEBUG",
                    "-sdk",
                    self.sdk,
                    "-target",
                    "arm64-apple-ios16.0-simulator",
                    "-parse-as-library",
                    "-emit-module",
                    "-emit-objc-header-path",
                    str(header),
                    "-o",
                    str(Path(directory) / "ReproSample.swiftmodule"),
                    "-module-name",
                    "ReproSample",
                    str(TEMPLATES / "ReproRuntimeIdentity.swift"),
                    str(TEMPLATES / "RLAutomaticRecorder.swift"),
                    str(FIXTURE / "RLAutoConfig.swift"),
                ],
            )
            generated = header.read_text()
            self.assertIn("@interface RLAutomaticRecorder", generated)
            self.assertIn("bootstrap", generated)
            self.assertIn("_rlWillSendAction", generated)
            self.assertIn("_rlActionReturned", generated)
            self.assertIn("_rlActionThrew", generated)

    def test_objc_shim_behavior_harness_preserves_product_dispatch_and_exceptions(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "automatic-shim-harness"
            mac_sdk = self._run_checked(
                ["xcrun", "--sdk", "macosx", "--show-sdk-path"], timeout=10
            ).stdout.strip()
            self._run_checked(
                [
                    "xcrun",
                    "clang",
                    "-fobjc-arc",
                    "-fblocks",
                    "-fmodules",
                    "-isysroot",
                    mac_sdk,
                    "-I",
                    str(FIXTURE / "objc-harness"),
                    "-framework",
                    "Foundation",
                    str(FIXTURE / "objc-harness/AutomaticShimHarness.m"),
                    "-o",
                    str(output),
                ],
                timeout=30,
            )
            result = self._run_checked([str(output)], timeout=10)
            self.assertEqual(result.stdout.strip(), "PASS")

    def test_subprocess_environment_is_scrubbed_and_failures_are_bounded(self):
        environment = self._run_checked(["/usr/bin/env"], timeout=5).stdout.splitlines()
        self.assertEqual(
            {line.split("=", 1)[0] for line in environment},
            {"PATH", "DEVELOPER_DIR"},
        )
        self.assertEqual(
            next(line.split("=", 1)[1] for line in environment if line.startswith("DEVELOPER_DIR=")),
            str(XCODE_DEVELOPER),
        )
        with self.assertRaisesRegex(AssertionError, "stderr marker"):
            self._run_checked(
                ["/bin/sh", "-c", "printf 'stderr marker\\n' >&2; exit 7"],
                timeout=5,
            )


class IOSAutomaticRuntimeFixtureTests(unittest.TestCase):
    def test_profile_fixture_is_the_closed_runtime_contract(self):
        profile = json.loads((FIXTURE / "profile.json").read_text())
        self.assertEqual(
            set(profile),
            {
                "schemaVersion",
                "kind",
                "applicationId",
                "project",
                "target",
                "cases",
                "textTargets",
                "numericTargets",
                "tapTargets",
                "backTarget",
                "screenTargets",
                "startState",
            },
        )
        self.assertEqual(profile["kind"], "uikit-runtime-v1")
        self.assertEqual(profile["applicationId"], "io.reproloop.sample.ios")
        self.assertEqual(profile["tapTargets"], ["counter.add", "counter.next", "counter.reset"])
        self.assertEqual(profile["backTarget"], "counter.back")

    def test_fixture_info_plist_has_debug_identity_fields(self):
        with (FIXTURE / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        self.assertEqual(info["CFBundleIdentifier"], "io.reproloop.sample.ios")
        self.assertEqual(info["ReproBuildID"], "fixture-build-id")
        profile = json.loads((FIXTURE / "profile.json").read_text())
        self.assertEqual(info["ReproAutoProfileDigest"], digest(profile))
        self.assertEqual(info["ReproAutoProfile"], profile)


if __name__ == "__main__":
    unittest.main()
