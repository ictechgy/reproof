"""Strict validation of the app-produced iOS runtime identity sidecar."""
from copy import deepcopy
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid
import tempfile

from reproloop.core import ContractError
from reproloop.ios_runtime_identity import (
    IOSRuntimeIdentityObservation,
    validate_ios_runtime_identity,
)

HELPER = Path(__file__).resolve().parents[1] / "reproloop/ios_instrumentation_templates/ReproRuntimeIdentity.swift"
XCODE_DEVELOPER = Path("/Applications/Xcode-27.0.0-beta.app/Contents/Developer")


RUN_ID = "11111111-1111-4111-8111-111111111111"
PROFILE = "a" * 64
BASE = {
    "schemaVersion": 1,
    "kind": "ios-runtime-identity",
    "bundleId": "io.reproloop.sample.ios",
    "buildId": "fixture-build-id",
    "runId": RUN_ID,
    "profileDigest": PROFILE,
    "startedAtMs": 1234,
}


class IOSRuntimeIdentityTests(unittest.TestCase):
    def validate(self, value=None, **changes):
        marker = deepcopy(BASE if value is None else value)
        marker.update(changes)
        return validate_ios_runtime_identity(
            marker,
            bundle_id="io.reproloop.sample.ios",
            build_id="fixture-build-id",
            profile_digest=PROFILE,
            run_id=RUN_ID,
            min_started_at_ms=1200,
        )

    def test_valid_marker_is_app_reported_and_has_no_binary_hash(self):
        observation = self.validate()

        self.assertIs(type(observation), IOSRuntimeIdentityObservation)
        self.assertEqual(observation.grade, "app-reported-runtime-id")
        self.assertEqual(observation.bundle_id, BASE["bundleId"])
        self.assertEqual(observation.build_id, BASE["buildId"])
        self.assertEqual(observation.run_id, RUN_ID)
        self.assertIsNone(getattr(observation, "installed_binary_sha256", None))
        self.assertEqual(observation.public(), BASE | {"grade": "app-reported-runtime-id"})

    def test_marker_binds_bundle_build_profile_run_and_minimum_start(self):
        cases = {
            "bundleId": "io.example.foreign",
            "buildId": "other-build",
            "profileDigest": "b" * 64,
            "runId": str(uuid.uuid4()),
            "startedAtMs": 1199,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(ContractError):
                    self.validate(**{field: value})

    def test_marker_schema_types_and_unknown_fields_fail_closed(self):
        cases = [
            {"extra": True},
            {"schemaVersion": True},
            {"kind": "other"},
            {"bundleId": "unknown"},
            {"buildId": "unknown"},
            {"runId": "not-a-uuid"},
            {"profileDigest": PROFILE.upper()},
            {"startedAtMs": True},
            {"startedAtMs": -1},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.validate(**changes)

    def test_expected_arguments_are_themselves_strict(self):
        kwargs = dict(
            bundle_id="io.reproloop.sample.ios",
            build_id="fixture-build-id",
            profile_digest=PROFILE,
            run_id=RUN_ID,
        )
        for field, value in (
            ("bundle_id", "unknown"),
            ("build_id", "unknown"),
            ("profile_digest", "bad"),
            ("run_id", "bad"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ContractError):
                    validate_ios_runtime_identity(BASE, **{**kwargs, field: value})



@unittest.skipUnless(XCODE_DEVELOPER.is_dir() and shutil.which("xcrun"),
                     "Xcode toolchain is unavailable")
class IOSRuntimeIdentitySwiftTests(unittest.TestCase):
    def test_foundation_writer_emits_private_rotating_marker_and_rejects_bad_build(self):
        with tempfile.TemporaryDirectory(prefix="ios-runtime-identity-") as directory:
            root = Path(directory)
            harness = root / "main.swift"
            output = root / "runtime-identity-harness"
            harness.write_text(
                """
import Foundation

let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let firstRun = "11111111-1111-4111-8111-111111111111"
let secondRun = "22222222-2222-4222-8222-222222222222"
try ReproRuntimeIdentityWriter.write(bundleID: "io.reproloop.sample.ios",
    buildID: "fixture-build-id", runID: firstRun,
    profileDigest: String(repeating: "a", count: 64), startedAtMs: 10, to: root)
let marker = root.appendingPathComponent(ReproRuntimeIdentityWriter.filename)
let first = try Data(contentsOf: marker)
let firstObject = try JSONSerialization.jsonObject(with: first) as! [String: Any]
precondition(firstObject["runId"] as? String == firstRun)
let attributes = try FileManager.default.attributesOfItem(atPath: marker.path)
precondition((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
try ReproRuntimeIdentityWriter.write(bundleID: "io.reproloop.sample.ios",
    buildID: "fixture-build-id", runID: secondRun,
    profileDigest: String(repeating: "b", count: 64), startedAtMs: 20, to: root)
let second = try Data(contentsOf: marker)
let secondObject = try JSONSerialization.jsonObject(with: second) as! [String: Any]
precondition(secondObject["runId"] as? String == secondRun)
precondition(secondObject["profileDigest"] as? String == String(repeating: "b", count: 64))
do {
    try ReproRuntimeIdentityWriter.write(bundleID: "io.reproloop.sample.ios",
        buildID: "unknown", runID: secondRun,
        profileDigest: String(repeating: "b", count: 64), startedAtMs: 21, to: root)
    fatalError("invalid build ID was accepted")
} catch { }
let retained = try JSONSerialization.jsonObject(with: Data(contentsOf: marker)) as! [String: Any]
precondition(retained["runId"] as? String == secondRun)
print("PASS")
"""
            )
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "DEVELOPER_DIR": str(XCODE_DEVELOPER),
            }
            compile_result = subprocess.run(
                ["xcrun", "swiftc", "-D", "DEBUG", str(HELPER), str(harness), "-o", str(output)],
                capture_output=True, text=True, env=environment, timeout=30,
            )
            self.assertEqual(compile_result.returncode, 0,
                             compile_result.stdout + compile_result.stderr)
            result = subprocess.run([str(output), str(root)], capture_output=True,
                                    text=True, env=environment, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "PASS")


if __name__ == "__main__":
    unittest.main()
