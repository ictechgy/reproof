from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproloop.android_build import create_protected_build, validate_protected_build
from reproloop.core import ContractError, digest
from reproloop.repair import snapshot_source
from reproloop.replay import replay_suite
from reproloop.storage import create_bundle, sha_file


def _capture():
    return {
        "schemaVersion": 1,
        "sessionId": "android-build-test",
        "fixture": {"id": "default", "version": 1, "inputs": {}},
        "startState": {"screen": "main", "nodes": {"count": "0", "name": ""}},
        "events": [
            {"id": "e1", "seq": 1, "action": "replace", "target": "name", "parameters": {"value": "QA"}},
            {"id": "e2", "seq": 2, "action": "tap", "target": "add", "parameters": {}},
        ],
        "truncated": False,
        "lostEvents": False,
        "endSequence": 2,
    }


def _oracle():
    return {
        "bugCondition": {"target": "count", "text": "2"},
        "expectedCondition": {"target": "count", "text": "1"},
        "actual": "One add increments count twice",
        "expected": "One add increments count once",
    }


class _ProtectedRunnerDevice:
    def __init__(self, runner, *, swapped=False):
        self.runner = runner
        self.swapped = swapped
        self.protected_driver = (runner, sha_file(runner))
        self.name = ""
        self.count = "0"

    def install(self, apk, package):
        if package.endswith("driver"):
            return {"apkSha256": sha_file(apk), "installedVerified": True}
        return {"apkSha256": sha_file(apk), "installedVerified": True}

    def installation_proof(self, apk, package):
        if package.endswith("driver") and self.swapped:
            return {"apkSha256": "0" * 64, "installedVerified": True}
        return {"apkSha256": sha_file(apk), "installedVerified": True}

    def prepare(self, apk, fixture):
        self.name = ""
        self.count = "0"
        return {"apkSha256": sha_file(apk), "installedVerified": True, "fixtureVerified": True}

    def observe(self):
        return {"name": self.name, "count": self.count}

    def execute(self, step):
        if step["action"] == "replace":
            self.name = step["parameters"]["value"]
        elif step["action"] == "tap":
            self.count = "2"

    def stop(self):
        pass


class ProtectedAndroidBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "android"
        (self.source / "sample").mkdir(parents=True)
        (self.source / "driver").mkdir()
        (self.source / "sample" / "Main.kt").write_text("fun increment() = 2\n")
        (self.source / "driver" / "Driver.kt").write_text("package driver\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_build(self, source, *, gradle, java_home, sdk_home, task,
                    apk_relative, timeout=300):
        apk = Path(source) / apk_relative
        apk.parent.mkdir(parents=True, exist_ok=True)
        apk.write_bytes(("sample" if ":sample:" in task else "driver").encode())
        source_files = snapshot_source(source)
        return apk, {
            "sourceDigest": digest(source_files),
            "sourceFiles": source_files,
            "apkSha256": sha_file(apk),
            "buildTask": task,
            "toolchain": {"gradleExecutable": "gradle", "java": "java"},
            "buildCompleted": True,
        }

    def test_freezes_both_apks_and_receipt_proofs(self):
        output = self.root / "protected"
        with patch("reproloop.android_build.build_android", self._fake_build):
            receipt = create_protected_build(
                self.source,
                output,
                gradle="gradle",
                java_home="/java",
                sdk_home="/sdk",
            )

        self.assertEqual(receipt["schemaVersion"], 1)
        self.assertEqual(receipt["platform"], "android")
        self.assertEqual(receipt["variant"], "buggy")
        self.assertEqual(receipt["buildTask"], ":sample:assembleBuggyDebug")
        self.assertEqual(receipt["sourceDigest"], digest(receipt["sourceFiles"]))
        self.assertEqual(receipt["apkSha256"], sha_file(output / "original.apk"))
        self.assertEqual(receipt["driverSha256"], sha_file(output / "driver.apk"))
        self.assertEqual(receipt["driverProof"]["buildTask"], ":driver:assembleDebug")
        self.assertEqual(receipt["driverProof"]["sourceDigest"], receipt["sourceDigest"])
        validated = validate_protected_build(output, source=self.source)
        self.assertEqual(validated["apkSha256"], receipt["apkSha256"])

    def test_swapped_or_tampered_frozen_artifact_is_rejected(self):
        output = self.root / "protected"
        with patch("reproloop.android_build.build_android", self._fake_build):
            create_protected_build(
                self.source,
                output,
                gradle="gradle",
                java_home="/java",
                sdk_home="/sdk",
            )
        (output / "driver.apk").write_bytes(b"different-runner")
        with self.assertRaises(ContractError):
            validate_protected_build(output, source=self.source)

    def test_output_must_be_new_directory(self):
        output = self.root / "protected"
        output.mkdir()
        with self.assertRaises(ContractError):
            create_protected_build(
                self.source,
                output,
                gradle="gradle",
                java_home="/java",
                sdk_home="/sdk",
            )

    def test_swapped_installed_runner_blocks_replay(self):
        output = self.root / "protected"
        with patch("reproloop.android_build.build_android", self._fake_build):
            create_protected_build(
                self.source,
                output,
                gradle="gradle",
                java_home="/java",
                sdk_home="/sdk",
            )
        apk = self.root / "bundle.apk"
        apk.write_bytes(b"sample")
        bundle = create_bundle(_capture(), _oracle(), apk, self.root / "bundle")
        device = _ProtectedRunnerDevice(output / "driver.apk", swapped=True)
        result = replay_suite(device, bundle, apk, self.root / "replay")
        self.assertEqual(result["status"], "environment_blocked")
        self.assertFalse(result["runs"][0]["runner"]["installedVerified"])


if __name__ == "__main__":
    unittest.main()
