"""Validation and rendering of the owned Xcode-generated XCTest template."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import tempfile
import unittest

from reproloop.core import ContractError, digest
from reproloop.ios_xctest_template import IOSXCTestTemplate


FIXTURE = Path(__file__).parent / "fixtures/ios-xctest-template/ReproLive_iphoneos.xctestrun"
FIXTURE_SHA256 = "db03d6cc84a883e69f4d44c8afe204fe2ef71772d28cdbf12b5e874debf0aac4"


class IOSXCTestTemplateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="owned-ios-xctest-template-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.host = self.root / "helper-host" / "ReproLiveHost.app"
        self.runner = self.root / "helper-runner" / "ReproLiveTests-Runner.app"
        (self.host).mkdir(parents=True)
        (self.runner / "PlugIns/ReproLiveTests.xctest").mkdir(parents=True)

    def paths(self):
        return {"helper-host": self.host, "helper-runner": self.runner}

    def template(self, path=FIXTURE):
        return IOSXCTestTemplate(Path(path).absolute(), FIXTURE_SHA256)

    def test_real_device_template_renders_and_preserves_generated_shape(self):
        template = self.template()
        rendered = plistlib.loads(template.render(self.paths(), {
            "REPRO_TARGET_BUNDLE": "io.reproloop.sample.ios",
            "REPRO_LIVE_PROTOCOL_VERSION": "2",
        }))
        source = plistlib.loads(FIXTURE.read_bytes())
        target = rendered["ReproLiveTests"]
        self.assertEqual(rendered["__xctestrun_metadata__"], source["__xctestrun_metadata__"])
        self.assertEqual(target["DiagnosticCollectionPolicy"], source["ReproLiveTests"]["DiagnosticCollectionPolicy"])
        self.assertEqual(target["UserAttachmentLifetime"], source["ReproLiveTests"]["UserAttachmentLifetime"])
        self.assertEqual(target["TestHostPath"], str(self.runner))
        self.assertEqual(target["TestBundlePath"], str(self.runner / "PlugIns/ReproLiveTests.xctest"))
        self.assertEqual(target["UITargetAppPath"], str(self.host))
        self.assertEqual(template.runner_bundle_identifier, "io.reproloop.live.tests.xctrunner")
        self.assertEqual(set(target["DependentProductPaths"]), {
            str(self.host), str(self.runner), str(self.runner / "PlugIns/ReproLiveTests.xctest")})
        self.assertEqual(target["EnvironmentVariables"]["REPRO_TARGET_BUNDLE"],
                         "io.reproloop.sample.ios")
        self.assertEqual(target["EnvironmentVariables"]["OS_ACTIVITY_DT_MODE"],
                         source["ReproLiveTests"]["EnvironmentVariables"]["OS_ACTIVITY_DT_MODE"])
        self.assertEqual(template.definition_digest,
                         digest({"kind": "ios-xctest-template-v1", "path": str(FIXTURE.absolute()),
                                 "sha256": FIXTURE_SHA256}))

    def test_test_bundle_path_already_expanded_from_testroot_is_normalized(self):
        source = plistlib.loads(FIXTURE.read_bytes())
        target = source["ReproLiveTests"]
        target["TestBundlePath"] = target["TestHostPath"] + "/PlugIns/ReproLiveTests.xctest"
        path = self.root / "expanded.xctestrun"
        path.write_bytes(plistlib.dumps(source, fmt=plistlib.FMT_BINARY))
        template = IOSXCTestTemplate(path, hashlib.sha256(path.read_bytes()).hexdigest())

        rendered = plistlib.loads(template.render(self.paths(), {}))["ReproLiveTests"]

        self.assertEqual(rendered["TestBundlePath"],
                         str(self.runner / "PlugIns/ReproLiveTests.xctest"))

    def test_format_two_template_is_supported_without_dropping_configuration_fields(self):
        source = plistlib.loads(FIXTURE.read_bytes())
        source["__xctestrun_metadata__"]["FormatVersion"] = 2
        target = source.pop("ReproLiveTests")
        source["TestConfigurations"] = [{"Name": "Default", "TestTargets": [target],
                                          "GeneratedSetting": {"Mode": "device"}}]
        path = self.root / "format-two.xctestrun"
        path.write_bytes(plistlib.dumps(source, fmt=plistlib.FMT_BINARY))
        template = IOSXCTestTemplate(path, hashlib.sha256(path.read_bytes()).hexdigest())

        rendered = plistlib.loads(template.render(self.paths(), {}))

        self.assertEqual(rendered["TestConfigurations"][0]["Name"], "Default")
        self.assertEqual(rendered["TestConfigurations"][0]["GeneratedSetting"], {"Mode": "device"})
        self.assertEqual(rendered["TestConfigurations"][0]["TestTargets"][0]["BlueprintName"],
                         "ReproLiveTests")

    def test_safe_generated_selection_defaults_are_preserved(self):
        source = plistlib.loads(FIXTURE.read_bytes())
        target = source["ReproLiveTests"]
        target.update(TestRepetitionMode="none", TestRepetitionCount=1,
                      ParallelizationEnabled=False)
        path = self.root / "safe-selection.xctestrun"
        path.write_bytes(plistlib.dumps(source, fmt=plistlib.FMT_BINARY))
        template = IOSXCTestTemplate(path, hashlib.sha256(path.read_bytes()).hexdigest())

        rendered = plistlib.loads(template.render(self.paths(), {}))["ReproLiveTests"]

        self.assertEqual(rendered["TestRepetitionMode"], "none")
        self.assertEqual(rendered["TestRepetitionCount"], 1)
        self.assertFalse(rendered["ParallelizationEnabled"])

    def test_fixed_test_selection_retry_and_parallelism_changes_are_rejected(self):
        cases = [
            ("OnlyTestIdentifiers", ["ReproLiveTests/OtherTests/other"]),
            ("SkipTestIdentifiers", ["ReproLiveTests/LiveControlTests/testControlSession"]),
            ("SkipTestIdentifiers", ["ReproLiveTests"]),
            ("SkipTestIdentifiers", ["ReproLiveTests/LiveControlTests"]),
            ("SkipTestIdentifiers", ["LiveControlTests"]),
            ("TestRepetitionMode", "retryOnFailure"),
            ("TestRepetitionCount", 2),
            ("RetryOnFailure", True),
            ("ParallelizationEnabled", True),
            ("MaximumConcurrentTestSimulatorDestinations", 2),
        ]
        for number, (key, value) in enumerate(cases):
            with self.subTest(key=key):
                source = plistlib.loads(FIXTURE.read_bytes())
                source["ReproLiveTests"][key] = value
                path = self.root / f"unsafe-selection-{number}.xctestrun"
                path.write_bytes(plistlib.dumps(source, fmt=plistlib.FMT_BINARY))
                with self.assertRaises(ContractError):
                    IOSXCTestTemplate(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_all_references_are_relocated_in_preserved_nested_generated_fields(self):
        source = plistlib.loads(FIXTURE.read_bytes())
        target = source["ReproLiveTests"]
        target["GeneratedNested"] = {
            "references": [target["TestHostPath"], target["TestBundlePath"],
                           target["UITargetAppPath"]]
        }
        path = self.root / "nested.xctestrun"
        path.write_bytes(plistlib.dumps(source, fmt=plistlib.FMT_BINARY))
        template = IOSXCTestTemplate(path, hashlib.sha256(path.read_bytes()).hexdigest())

        rendered = plistlib.loads(template.render(self.paths(), {}))["ReproLiveTests"]

        self.assertEqual(rendered["GeneratedNested"]["references"], [
            str(self.runner), str(self.runner / "PlugIns/ReproLiveTests.xctest"), str(self.host)])

    def test_unknown_environment_fields_are_rejected(self):
        with self.assertRaises(ContractError):
            self.template().render(self.paths(), {"REPRO_LIVE_UNAPPROVED": "value"})

    def test_mutating_the_bound_source_after_construction_is_rejected(self):
        path = self.root / "mutable.xctestrun"
        shutil.copyfile(FIXTURE, path)
        template = IOSXCTestTemplate(path, hashlib.sha256(path.read_bytes()).hexdigest())
        template.verify()
        path.write_bytes(path.read_bytes() + b"changed")

        with self.assertRaises(ContractError):
            template.verify()
        with self.assertRaises(ContractError):
            template.render(self.paths(), {})

    def test_symlink_hardlink_and_writable_other_sources_are_rejected(self):
        symlink = self.root / "linked.xctestrun"
        symlink.symlink_to(FIXTURE)
        with self.assertRaises(ContractError):
            self.template(symlink)

        hardlink = self.root / "hardlinked.xctestrun"
        os.link(FIXTURE, hardlink)
        with self.assertRaises(ContractError):
            self.template(hardlink)

        writable = self.root / "writable.xctestrun"
        shutil.copyfile(FIXTURE, writable)
        writable.chmod(0o666)
        with self.assertRaises(ContractError):
            self.template(writable)

    def test_external_targets_other_tests_and_unsafe_bundle_paths_are_rejected(self):
        cases = []
        source = plistlib.loads(FIXTURE.read_bytes())
        source["ReproLiveTests"]["TestHostPath"] = "/bin/sh"
        cases.append(source)

        source = plistlib.loads(FIXTURE.read_bytes())
        source["ReproLiveTests"]["TestBundlePath"] = "__TESTROOT__/Other.xctest"
        cases.append(source)

        source = plistlib.loads(FIXTURE.read_bytes())
        source["ReproLiveTests"]["DependentProductPaths"].append("/bin/sh")
        cases.append(source)

        source = plistlib.loads(FIXTURE.read_bytes())
        source["OtherTests"] = deepcopy(source["ReproLiveTests"])
        cases.append(source)

        for number, document in enumerate(cases):
            with self.subTest(number=number):
                path = self.root / f"unsafe-{number}.xctestrun"
                path.write_bytes(plistlib.dumps(document, fmt=plistlib.FMT_BINARY))
                with self.assertRaises(ContractError):
                    IOSXCTestTemplate(path, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
