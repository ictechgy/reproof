import copy
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest

from reproof.core import ContractError
from reproof.ios_sanitation import (
    IOSSanitationPolicy,
    policy_from_app,
    validate_ios_sanitation_policy,
)


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "reproof/ios_instrumentation_templates"
SANITATION_FIXTURE = ROOT / "tests/fixtures/ios-sanitation-runtime"
XCODE_DEVELOPER = Path(os.environ.get("DEVELOPER_DIR") or subprocess.run(["xcode-select","-p"],capture_output=True,text=True,check=True).stdout.strip())


def policy_document():
    return {
        "schemaVersion": 1,
        "kind": "ios-app-owned-sanitation",
        "paths": [
            {"root": "documents", "relativePath": "Drafts/Temporary"},
            {"root": "application-support", "relativePath": "Example/Cache"},
            {"root": "caches", "relativePath": "Thumbnails"},
        ],
        "userDefaultsKeys": ["draft.name", "draft.count"],
        "keychainGenericPasswords": [
            {"service": "io.example.debug", "account": "synthetic-user"}
        ],
    }


class IOSSanitationPolicyTests(unittest.TestCase):
    def test_canonical_policy_has_stable_data_digest_and_counts(self):
        document = policy_document()
        selected = validate_ios_sanitation_policy(document)
        self.assertIsInstance(selected, IOSSanitationPolicy)
        self.assertEqual(selected.data, document)
        self.assertEqual(
            selected.counts,
            {"pathCount": 3, "userDefaultsKeyCount": 2, "keychainItemCount": 1},
        )
        self.assertEqual(len(selected.digest), 64)
        document["paths"][0]["relativePath"] = "changed"
        self.assertEqual(selected.data["paths"][0]["relativePath"], "Drafts/Temporary")

    def test_policy_rejects_unknown_store_shapes_and_oversize_lists(self):
        mutations = [
            lambda value: value.update(sharedContainer="group.example"),
            lambda value: value["paths"][0].update(appGroup="group.example"),
            lambda value: value["keychainGenericPasswords"][0].update(
                synchronizable=False
            ),
            lambda value: value.update(userDefaultsKeys=["key"] * 129),
            lambda value: value.update(paths=[
                {"root": "documents", "relativePath": f"Root{index}"}
                for index in range(65)
            ]),
            lambda value: value.update(
                paths=[], userDefaultsKeys=[], keychainGenericPasswords=[]
            ),
            lambda value: value.update(keychainGenericPasswords=[
                {"service": "service", "account": str(index)} for index in range(33)
            ]),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = copy.deepcopy(policy_document())
                mutate(value)
                with self.assertRaises(ContractError):
                    validate_ios_sanitation_policy(value)

        value = policy_document()
        value["paths"] = []
        value["keychainGenericPasswords"] = []
        value["userDefaultsKeys"] = [
            f"{index:03d}" + "x" * 253 for index in range(128)
        ]
        with self.assertRaises(ContractError):
            validate_ios_sanitation_policy(value)

    def test_paths_are_ascii_relative_nonoverlapping_and_reserve_runtime_tree(self):
        rejected = [
            "",
            "/absolute",
            "A//B",
            "A/./B",
            "A/../B",
            ".hidden/value",
            "A\\B",
            "A B",
            "café",
        ]
        for relative in rejected:
            with self.subTest(relative=relative):
                value = policy_document()
                value["paths"] = [{"root": "documents", "relativePath": relative}]
                with self.assertRaises(ContractError):
                    validate_ios_sanitation_policy(value)

        value = policy_document()
        value["paths"] = [
            {"root": "documents", "relativePath": "Drafts"},
            {"root": "documents", "relativePath": "drafts/Child"},
        ]
        with self.assertRaises(ContractError):
            validate_ios_sanitation_policy(value)

        value["paths"] = [
            {"root": "application-support", "relativePath": "reproof/Sessions"}
        ]
        with self.assertRaises(ContractError):
            validate_ios_sanitation_policy(value)

        value = policy_document()
        value["paths"] = [
            {"root": "documents", "relativePath": "Snapshots/state.json"}
        ]
        validate_ios_sanitation_policy(value)

        for malformed in [[], {}, None, 1]:
            value = policy_document()
            value["paths"][0]["root"] = malformed
            with self.assertRaises(ContractError):
                validate_ios_sanitation_policy(value)

    def test_exact_keys_and_selectors_reject_controls_duplicates_and_private_fields(self):
        for replacement in ["", "line\nbreak", "snowman-☃"]:
            value = policy_document()
            value["userDefaultsKeys"] = [replacement]
            with self.assertRaises(ContractError):
                validate_ios_sanitation_policy(value)
        for malformed in [[], {}, None, 1]:
            value = policy_document()
            value["userDefaultsKeys"] = [malformed]
            with self.assertRaises(ContractError):
                validate_ios_sanitation_policy(value)
        value = policy_document()
        value["userDefaultsKeys"] = ["same", "same"]
        with self.assertRaises(ContractError):
            validate_ios_sanitation_policy(value)
        value = policy_document()
        value["keychainGenericPasswords"] = [
            {"service": "service", "account": "account"},
            {"service": "service", "account": "account"},
        ]
        with self.assertRaises(ContractError):
            validate_ios_sanitation_policy(value)

    def test_policy_from_app_requires_the_exact_embedded_digest_pair(self):
        selected = validate_ios_sanitation_policy(policy_document())
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "Example.app"
            app.mkdir()
            with (app / "Info.plist").open("wb") as handle:
                plistlib.dump({"CFBundleIdentifier": "io.example.app"}, handle)
            self.assertIsNone(policy_from_app(app))
            for info in [
                {"ReproSanitationPolicy": selected.data},
                {"ReproSanitationPolicyDigest": selected.digest},
                {
                    "ReproSanitationPolicy": selected.data,
                    "ReproSanitationPolicyDigest": "0" * 64,
                },
            ]:
                with (app / "Info.plist").open("wb") as handle:
                    plistlib.dump(info, handle)
                with self.assertRaises(ContractError):
                    policy_from_app(app)
            with (app / "Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "ReproSanitationPolicy": selected.data,
                        "ReproSanitationPolicyDigest": selected.digest,
                    },
                    handle,
                )
            self.assertEqual(policy_from_app(app), selected)


class IOSSanitationPreparationTests(unittest.TestCase):
    def test_legacy_preparation_compiles_policy_and_embeds_matching_info(self):
        from tests.test_ios_instrumentation import minimal_project
        from reproof.ios_instrumentation import (
            prepare_ios_instrumentation,
            validate_ios_preparation,
        )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / "plain"
            minimal_project(source)
            output = root / "prepared"
            policy = validate_ios_sanitation_policy(policy_document())
            prepare_ios_instrumentation(
                source, output, sanitation_policy=policy
            )
            prepared = output / "source"
            validate_ios_preparation(prepared)
            info = plistlib.loads(
                (prepared / "ReproofInstrumentation/Info.plist").read_bytes()
            )
            self.assertEqual(info["ReproRuntimeIdentitySchemaVersion"], 2)
            self.assertEqual(info["ReproSanitationPolicy"], policy.data)
            self.assertEqual(info["ReproSanitationPolicyDigest"], policy.digest)
            config = (
                prepared
                / "ReproofInstrumentation/Runtime/RLSanitationConfig.swift"
            ).read_text()
            self.assertIn(policy.digest, config)
            self.assertNotIn("static let policyJSON: String? = nil", config)
            project = plistlib.loads(
                (prepared / "Reproof.xcodeproj/project.pbxproj").read_bytes()
            )
            release = project["objects"]["RELEASE"]["buildSettings"]
            self.assertIn(
                "RLSanitationRuntime.swift", release["EXCLUDED_SOURCE_FILE_NAMES"]
            )
            self.assertIn(
                "RLSanitationConfig.swift", release["EXCLUDED_SOURCE_FILE_NAMES"]
            )

    def test_omitted_policy_generates_nil_config_and_no_info_capability(self):
        from tests.test_ios_instrumentation import minimal_project
        from reproof.ios_instrumentation import prepare_ios_instrumentation

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "plain"
            minimal_project(source)
            output = root / "prepared"
            prepare_ios_instrumentation(source, output)
            prepared = output / "source"
            info = plistlib.loads(
                (prepared / "ReproofInstrumentation/Info.plist").read_bytes()
            )
            self.assertNotIn("ReproSanitationPolicy", info)
            self.assertNotIn("ReproSanitationPolicyDigest", info)
            config = (
                prepared
                / "ReproofInstrumentation/Runtime/RLSanitationConfig.swift"
            ).read_text()
            self.assertIn("static let policyJSON: String? = nil", config)
            self.assertIn("static let policyDigest: String? = nil", config)

    def test_observation_preparation_uses_the_same_fixed_policy_contract(self):
        from tests.test_ios_observation import ordinary_project
        from reproof.ios_instrumentation import (
            prepare_ios_instrumentation,
            validate_ios_preparation,
        )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / "plain"
            profile = ordinary_project(source)
            policy = validate_ios_sanitation_policy(policy_document())
            result = prepare_ios_instrumentation(
                source,
                root / "prepared",
                profile=profile,
                sanitation_policy=policy,
            )
            prepared = Path(result["source"])
            receipt = validate_ios_preparation(prepared)
            self.assertEqual(receipt["sanitationPolicyDigest"], policy.digest)
            info = plistlib.loads(
                (prepared / "ReproofInstrumentation/Info.plist").read_bytes()
            )
            self.assertEqual(info["ReproRuntimeIdentitySchemaVersion"], 2)
            self.assertEqual(info["ReproSanitationPolicy"], policy.data)
            self.assertEqual(info["ReproSanitationPolicyDigest"], policy.digest)


class IOSSanitationRuntimeSourceContractTests(unittest.TestCase):
    def test_pre_main_call_precedes_the_existing_async_recorder_bootstrap(self):
        source = (TEMPLATES / "RLAutoBootstrap.m").read_text()
        load_method = source[source.index("+ (void)load") :]
        self.assertLess(
            load_method.index("RLApplySanitationBeforeMain();"),
            load_method.index("dispatch_async(dispatch_get_main_queue()"),
        )

    def test_keychain_runtime_never_requests_or_returns_secret_values(self):
        source = (TEMPLATES / "RLSanitationRuntime.swift").read_text()
        self.assertNotIn("kSecReturnData", source)
        self.assertNotIn("kSecValueData", source)
        self.assertIn("kSecReturnAttributes", source)
        self.assertIn("kSecAttrSynchronizable: kCFBooleanFalse", source)
        self.assertIn("kSecAttrAccessGroup: accessGroup", source)
        self.assertLess(
            source.index("try check(keychainCounts.allSatisfy { $0 <= 1 })"),
            source.index("for path in policy.paths"),
        )

    def test_runtime_environment_accepts_only_the_compiled_policy_digest(self):
        source = (TEMPLATES / "RLSanitationRuntime.swift").read_text()
        self.assertIn('"REPRO_SANITATION_POLICY_DIGEST"', source)
        self.assertNotIn("REPRO_SANITATION_POLICY_JSON", source)
        self.assertNotIn("REPRO_SANITATION_PATH", source)


@unittest.skipUnless(
    XCODE_DEVELOPER.is_dir() and shutil.which("xcrun"),
    "Xcode iOS SDK is unavailable",
)
class IOSSanitationRuntimeCompileTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "PATH": os.environ.get("PATH", ""),
            "DEVELOPER_DIR": str(XCODE_DEVELOPER),
        }
        result = subprocess.run(
            ["xcrun", "--sdk", "iphonesimulator", "--show-sdk-path"],
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.sdk = result.stdout.strip()

    def run_checked(self, command, *, timeout=30):
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=timeout,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_debug_and_observation_runtime_typecheck_without_keychain_execution(self):
        sources = [
            str(TEMPLATES / "ReproRuntimeIdentity.swift"),
            str(TEMPLATES / "RLSanitationRuntime.swift"),
            str(TEMPLATES / "RLAutomaticRecorder.swift"),
            str(SANITATION_FIXTURE / "RLSanitationConfig.swift"),
        ]
        for condition in ["DEBUG", "REPRO_OBSERVATIONS"]:
            auto_config = (
                ROOT / "tests/fixtures/ios-auto-runtime/RLAutoConfig.swift"
                if condition == "DEBUG"
                else SANITATION_FIXTURE / "RLAutoObservationConfig.swift"
            )
            self.run_checked(
                [
                    "xcrun", "swiftc", "-D", condition, "-swift-version", "5",
                    "-warnings-as-errors", "-typecheck", "-sdk", self.sdk,
                    "-target", "arm64-apple-ios16.0-simulator", *sources,
                    str(auto_config),
                ],
                timeout=60,
            )

    def test_release_sources_and_objc_pre_main_shim_typecheck(self):
        self.run_checked(
            [
                "xcrun", "swiftc", "-swift-version", "5", "-warnings-as-errors",
                "-typecheck", "-sdk", self.sdk,
                "-target", "arm64-apple-ios16.0-simulator",
                str(TEMPLATES / "ReproRuntimeIdentity.swift"),
                str(TEMPLATES / "RLSanitationRuntime.swift"),
                str(TEMPLATES / "RLAutomaticRecorder.swift"),
                str(SANITATION_FIXTURE / "RLSanitationConfig.swift"),
            ]
        )
        for macro in [None, "REPRO_AUTO_DEBUG=1"]:
            command = [
                "xcrun", "clang", "-fsyntax-only", "-fobjc-arc", "-fmodules",
                "-Werror", "-isysroot", self.sdk,
                "-mios-simulator-version-min=16.0",
            ]
            if macro:
                command.append("-D" + macro)
            command.append(str(TEMPLATES / "RLAutoBootstrap.m"))
            self.run_checked(command)

    def test_pure_identity_writer_builds_and_emits_schema_two_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            harness = root / "main.swift"
            executable = root / "identity-writer"
            harness.write_text(
                """
import Foundation

let runID = "11111111-1111-4111-8111-111111111111"
let receipt: [String: Any] = [
    "schemaVersion": 1,
    "kind": "ios-app-sanitation-receipt",
    "policyDigest": String(repeating: "a", count: 64),
    "runId": runID,
    "stage": "launch",
    "startedAtMs": 10,
    "completedAtMs": 11,
    "pathCount": 1,
    "userDefaultsKeyCount": 2,
    "keychainItemCount": 0,
    "status": "complete"
]
let data = try ReproRuntimeIdentityWriter.markerData(
    bundleID: "io.example.app", buildID: "build-1234", runID: runID,
    profileDigest: String(repeating: "b", count: 64), startedAtMs: 12,
    sanitation: receipt
)
let object = try JSONSerialization.jsonObject(with: data) as! [String: Any]
precondition(object["schemaVersion"] as? Int == 2)
precondition(Set(object.keys) == Set([
    "schemaVersion", "kind", "bundleId", "buildId", "runId",
    "profileDigest", "startedAtMs", "sanitation"
]))
precondition(object["sanitationPolicyDigest"] == nil)
do {
    var invalid = receipt
    invalid["runId"] = "22222222-2222-4222-8222-222222222222"
    _ = try ReproRuntimeIdentityWriter.markerData(
        bundleID: "io.example.app", buildID: "build-1234", runID: runID,
        profileDigest: String(repeating: "b", count: 64), startedAtMs: 12,
        sanitation: invalid
    )
    fatalError("mismatched receipt accepted")
} catch { }
print("PASS")
"""
            )
            self.run_checked(
                [
                    "xcrun", "swiftc", "-D", "DEBUG",
                    str(TEMPLATES / "ReproRuntimeIdentity.swift"),
                    str(harness), "-o", str(executable),
                ]
            )
            result = subprocess.run(
                [str(executable)], capture_output=True, text=True,
                env=self.environment, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "PASS")


if __name__ == "__main__":
    unittest.main()
