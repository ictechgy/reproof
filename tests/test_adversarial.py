"""Adversarial boundary tests for recording, evidence, repair, and subprocess cleanup.

These tests deliberately keep the device and Xcode boundaries deterministic.  The
filesystem, bundle compilers, and command runner remain real so that the checks
exercise the same trust boundaries used by a local repair job.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from reproof.core import ContractError, digest
from reproof.ios_core import compile_ios_capture
from reproof.ios_repair import repair_ios_job
from reproof.ios_runner import extract_attachment
from reproof.ios_storage import create_ios_bundle, tree_manifest
from reproof.ios_build import PRODUCT_FILE as IOS_PRODUCT_FILE
from reproof.orchestrator import APK_RELATIVE, PRODUCT_FILE, repair_job
from reproof.repair import CommandError, run_command, snapshot_source
from reproof.storage import create_bundle, sha_file

from tests.test_core import capture, oracle
from tests.test_ios_core import ios_capture, ios_oracle


class _IOSAgent:
    def propose(self, *args):
        return [{"path": IOS_PRODUCT_FILE, "old": "return 2", "new": "return 1"}]


class _IOSBaselineCancellingSimulator:
    udid = "00000000-0000-0000-0000-000000000000"

    def run_scenario(self, *args, **kwargs):
        raise KeyboardInterrupt()


class _IOSMutatingSimulator:
    udid = "00000000-0000-0000-0000-000000000000"

    def __init__(self, mutation):
        self.calls = 0
        self.mutation = mutation

    def run_scenario(self, products, app, scenario, output, mode="replay"):
        self.calls += 1
        output.mkdir(parents=True)
        # Three baseline runs and three verification runs are required.  Mutate
        # only after the last run has started, which catches missing final checks.
        if self.calls == 6:
            self.mutation()
        outcome = "bug" if self.calls <= 3 else "normal"
        return {
            "runId": str(self.calls),
            "runValid": True,
            "bugCondition": outcome == "bug",
            "expectedCondition": outcome == "normal",
            "evidenceValid": True,
            "protectedPathsValid": True,
            "regressionPassed": False,
        }


class _IOSSourceAwareSimulator:
    """Small deterministic stand-in whose patched outcome follows built source."""

    udid = "00000000-0000-0000-0000-000000000000"

    def __init__(self, state):
        self.calls = 0
        self.state = state

    def run_scenario(self, products, app, scenario, output, mode="replay"):
        self.calls += 1
        output.mkdir(parents=True)
        is_bug = self.calls <= 3 or not self.state.get("correctPatch", False)
        return {
            "runId": str(self.calls),
            "runValid": True,
            "bugCondition": is_bug,
            "expectedCondition": not is_bug,
            "evidenceValid": True,
            "protectedPathsValid": True,
            "regressionPassed": False,
        }


class _AndroidFinalRunMutatingDevice:
    def __init__(self, source, mutation):
        self.source = source
        self.mutation = mutation
        self.runs = 0
        self.steps = 0
        self.value = "0"
        self.name = ""

    def prepare(self, apk, fixture):
        self.value = "0"
        self.name = ""
        return {
            "installedVerified": True,
            "fixtureVerified": True,
            "apkSha256": sha_file(apk),
        }

    def observe(self):
        return {"name": self.name, "count": self.value}

    def execute(self, step):
        self.steps += 1
        if step["action"] == "replace":
            self.name = step["parameters"]["value"]
        elif step["action"] == "tap":
            self.value = "2" if self.runs < 3 else "1"

    def stop(self):
        self.runs += 1
        if self.runs == 6:
            self.mutation()


def _ios_job_fixture(root: Path):
    source = root / "ios"
    source_file = source / IOS_PRODUCT_FILE
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        "enum CounterLogic { static func increment() -> Int { return 2 } }\n",
        encoding="utf-8",
    )
    (source / "Protected.swift").write_text("let protected = true\n", encoding="utf-8")
    products = root / "products"
    app = products / "ReproSample.app"
    app.mkdir(parents=True)
    with (app / "Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleIdentifier": "io.reproof.sample.ios",
                "ReproBuildID": "original-build",
            },
            stream,
        )
    receipt = {
        "productsDigest": digest(tree_manifest(products)),
        "sourceDigest": digest(tree_manifest(source, True)),
        "buildCompleted": True,
        "buildId": "original-build",
        "appRelative": "ReproSample.app",
    }
    bundle = create_ios_bundle(ios_capture(), ios_oracle(), products, receipt, root / "bundle")
    return source, products, bundle


class RecordingBoundaryTests(unittest.TestCase):
    def test_ios_rejects_duplicate_event_identity_and_wrong_freeze_boundary(self):
        duplicate = ios_capture()
        duplicate["events"][1]["id"] = duplicate["events"][0]["id"]
        with self.assertRaises(ContractError):
            compile_ios_capture(duplicate, ios_oracle())

        truncated = ios_capture()
        truncated["endSequence"] = 1
        with self.assertRaises(ContractError):
            compile_ios_capture(truncated, ios_oracle())


class AttachmentBoundaryTests(unittest.TestCase):
    def _manifest(self, output, attachments):
        (output / "one.json").write_text(json.dumps({"runId": "one"}), encoding="utf-8")
        (output / "two.json").write_text(json.dumps({"runId": "two"}), encoding="utf-8")
        (output / "manifest.json").write_text(
            json.dumps(
                [
                    {
                        "testIdentifier": "ReproReplayTests/ReproReplayTests/testScenario()",
                        "attachments": attachments,
                    }
                ]
            ),
            encoding="utf-8",
        )

    def test_duplicate_matching_attachments_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self._manifest(
                output,
                [
                    {
                        "suggestedHumanReadableName": "repro-result-one",
                        "deviceId": "simulator-id",
                        "exportedFileName": "one.json",
                    },
                    {
                        "suggestedHumanReadableName": "repro-result-two",
                        "deviceId": "simulator-id",
                        "exportedFileName": "two.json",
                    },
                ],
            )
            with patch("reproof.ios_runner.run_command", return_value=""):
                with self.assertRaises(ContractError):
                    extract_attachment("result.xcresult", output, "simulator-id")

    def test_attachment_without_expected_device_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self._manifest(
                output,
                [
                    {
                        "suggestedHumanReadableName": "repro-result",
                        "exportedFileName": "one.json",
                    }
                ],
            )
            with patch("reproof.ios_runner.run_command", return_value=""):
                with self.assertRaises(ContractError):
                    extract_attachment("result.xcresult", output, "simulator-id")


class RepairBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _ios_build(self, source, output, simulator_id, **kwargs):
        app = output / "products/ReproSample.app"
        app.mkdir(parents=True)
        with (app / "Info.plist").open("wb") as stream:
            plistlib.dump(
                {
                    "CFBundleIdentifier": "io.reproof.sample.ios",
                    "ReproBuildID": "patched-build",
                },
                stream,
            )
        return {
            "app": app,
            "products": app.parent,
            "receipt": {
                "sourceDigest": digest(tree_manifest(source, True)),
                "buildId": "patched-build",
            },
        }

    def test_ios_baseline_cancellation_is_persisted(self):
        source, _, bundle = _ios_job_fixture(self.root)
        output = self.root / "repair"
        with patch("reproof.ios_repair.build_ios") as build:
            try:
                result = repair_ios_job(
                    _IOSBaselineCancellingSimulator(), bundle, source, output, _IOSAgent()
                )
            except KeyboardInterrupt as exc:
                self.fail(f"baseline cancellation escaped repair job: {exc!r}")
        build.assert_not_called()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(json.loads((output / "job.json").read_text())["status"], "cancelled")

    def test_ios_protected_source_mutation_on_last_run_cannot_verify(self):
        source, _, bundle = _ios_job_fixture(self.root)

        def mutate():
            (source / "Protected.swift").write_text("let protected = false\n", encoding="utf-8")

        simulator = _IOSMutatingSimulator(mutate)
        with patch("reproof.ios_repair.build_ios", self._ios_build), patch(
            "reproof.ios_repair.run_logic_test", return_value={"passedTests": 1}
        ):
            result = repair_ios_job(
                simulator, bundle, source, self.root / "repair-source", _IOSAgent()
            )
        self.assertNotEqual(result["status"], "verified")

    def test_ios_valid_but_wrong_patch_is_not_verified(self):
        source, _, bundle = _ios_job_fixture(self.root)
        state = {}

        class WrongAgent:
            def propose(self, *args):
                return [{"path": IOS_PRODUCT_FILE, "old": "return 2", "new": "return 3"}]

        def build(source_path, output, simulator_id, **kwargs):
            state["correctPatch"] = "return 1" in (source_path / IOS_PRODUCT_FILE).read_text()
            return self._ios_build(source_path, output, simulator_id, **kwargs)

        with patch("reproof.ios_repair.build_ios", build), patch(
            "reproof.ios_repair.run_logic_test", return_value={"passedTests": 1}
        ):
            result = repair_ios_job(
                _IOSSourceAwareSimulator(state),
                bundle,
                source,
                self.root / "repair-wrong-patch",
                WrongAgent(),
                max_attempts=1,
            )
        self.assertEqual(result["status"], "verification_failed")

    def test_ios_frozen_runner_mutation_on_last_run_cannot_verify(self):
        source, _, bundle = _ios_job_fixture(self.root)

        def mutate():
            app = bundle["products"] / "ReproSample.app/Info.plist"
            with app.open("rb") as stream:
                info = plistlib.load(stream)
            info["ReproBuildID"] = "tampered-runner"
            with app.open("wb") as stream:
                plistlib.dump(info, stream)

        simulator = _IOSMutatingSimulator(mutate)
        with patch("reproof.ios_repair.build_ios", self._ios_build), patch(
            "reproof.ios_repair.run_logic_test", return_value={"passedTests": 1}
        ):
            result = repair_ios_job(
                simulator, bundle, source, self.root / "repair-runner", _IOSAgent()
            )
        self.assertNotEqual(result["status"], "verified")

    def test_android_protected_source_mutation_on_last_run_cannot_verify(self):
        source = self.root / "android"
        product = source / PRODUCT_FILE
        product.parent.mkdir(parents=True)
        product.write_text("fun increment() = 2\n", encoding="utf-8")
        protected = source / "build.gradle.kts"
        protected.write_text("// protected build fixture\n", encoding="utf-8")
        apk = self.root / "original.apk"
        apk.write_bytes(b"original")
        proof = {
            "sourceDigest": digest(snapshot_source(source)),
            "apkSha256": sha_file(apk),
            "buildCompleted": True,
        }
        bundle = create_bundle(capture(), oracle(), apk, self.root / "bundle", proof)

        def build(workspace, **kwargs):
            output = workspace / APK_RELATIVE
            output.parent.mkdir(parents=True)
            output.write_bytes(b"patched")
            return output, {
                "sourceDigest": digest(snapshot_source(workspace)),
                "apkSha256": sha_file(output),
                "buildCompleted": True,
            }

        def tests_pass(command, cwd, **kwargs):
            report = Path(cwd) / "sample/build/test-results/testBuggyDebugUnitTest/TEST-Counter.xml"
            report.parent.mkdir(parents=True)
            report.write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="0"/>',
                encoding="utf-8",
            )

        def mutate():
            protected.write_text("// mutated after final run\n", encoding="utf-8")

        device = _AndroidFinalRunMutatingDevice(source, mutate)

        class Agent:
            def propose(self, *args):
                return [{"path": PRODUCT_FILE, "old": "increment() = 2", "new": "increment() = 1"}]

        with patch("reproof.orchestrator.build_android", build), patch(
            "reproof.orchestrator.run_command", tests_pass
        ):
            result = repair_job(
                device,
                bundle,
                source,
                self.root / "repair-android",
                Agent(),
                {"gradle": "fake-gradle", "java_home": "fake-jdk", "sdk_home": "fake-sdk"},
            )
        self.assertNotEqual(result["status"], "verified")


class CommandCleanupTests(unittest.TestCase):
    def test_timeout_kills_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            child = (
                "import pathlib,sys,time,os; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
            )
            parent = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]]); "
                "time.sleep(60)"
            )
            with self.assertRaises(CommandError):
                run_command(
                    [sys.executable, "-c", parent, str(pid_file), child],
                    directory,
                    timeout=0.2,
                )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not pid_file.exists():
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), "test child did not start")
            child_pid = int(pid_file.read_text())
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except OSError as exc:
                    if exc.errno == errno.ESRCH:
                        break
                else:
                    time.sleep(0.02)
            else:
                self.fail("timed out command left its descendant process alive")


if __name__ == "__main__":
    unittest.main()
