"""Bounded support for direct XCTest code bundles in an iOS app."""
import struct
import unittest

from reproof.core import ContractError
from reproof.ios_artifact_transfer import parse_ios_artifact
from tests import test_ios_artifact_transfer as support

MACHO_BUNDLE = support.MACHO64_ARM64[:12] + struct.pack('<I', 8) + support.MACHO64_ARM64[16:]


class IOSXCTestArtifactTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.IOSArtifactTransferTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def add_xctest(self, app):
        bundle = app / "PlugIns" / "ReproLiveTests.xctest"
        support._write(bundle / "Info.plist",
                       support._app_info("com.example.inventory.tests", "ReproLiveTests"))
        support._write(bundle / "ReproLiveTests", MACHO_BUNDLE, executable=True)
        return bundle

    def test_xctest_requires_bundle_macho_type(self):
        for file_type in (2, 6, 7, 10):
            with self.subTest(file_type=file_type):
                app = self.fixture.make_flat_app()
                bundle = self.add_xctest(app)
                body = MACHO_BUNDLE[:12] + struct.pack('<I', file_type) + MACHO_BUNDLE[16:]
                support._write(bundle / "ReproLiveTests", body, executable=True)
                with self.assertRaises(ContractError):
                    parse_ios_artifact(app)

    def test_direct_plugins_xctest_is_a_bounded_code_object(self):
        app = self.fixture.make_flat_app()
        self.add_xctest(app)

        ipa = self.fixture.make_ipa(app, self.fixture.root / "runner.ipa")
        artifact = parse_ios_artifact(ipa)

        self.assertEqual(
            [row for row in artifact.manifest["codeObjects"]
             if row["bundlePath"] == "PlugIns/ReproLiveTests.xctest"],
            [{"bundlePath": "PlugIns/ReproLiveTests.xctest",
              "executablePath": "PlugIns/ReproLiveTests.xctest/ReproLiveTests",
              "kind": "xctest"}],
        )

    def test_xctest_cannot_expand_profile_or_framework_boundaries(self):
        cases = []

        profile = self.fixture.make_flat_app()
        xctest = self.add_xctest(profile)
        support._write(xctest / "embedded.mobileprovision", b"not an allowed profile")
        cases.append(profile)

        framework = self.fixture.make_flat_app()
        xctest = self.add_xctest(framework)
        nested = xctest / "Frameworks" / "Nested.framework"
        support._write(nested / "Info.plist",
                       support._app_info("com.example.nested", "Nested"))
        support._write(nested / "Nested", support.MACHO64_ARM64, executable=True)
        cases.append(framework)

        for app in cases:
            with self.subTest(app=app.name):
                with self.assertRaises(ContractError):
                    parse_ios_artifact(app)

    def test_xctest_symlink_and_unlisted_macho_are_rejected(self):
        linked = self.fixture.make_flat_app()
        self.add_xctest(linked)
        (linked / "PlugIns" / "Linked.xctest").symlink_to(
            "ReproLiveTests.xctest", target_is_directory=True)

        unlisted = self.fixture.make_flat_app()
        xctest = self.add_xctest(unlisted)
        support._write(xctest / "Unlisted", support.MACHO64_ARM64, executable=True)

        for app in (linked, unlisted):
            with self.subTest(app=app.name):
                with self.assertRaises(ContractError):
                    parse_ios_artifact(app)

    def test_nested_xctest_remains_rejected(self):
        app = self.fixture.make_flat_app()
        nested = app / "Extras" / "Nested.xctest"
        support._write(nested / "Info.plist",
                       support._app_info("com.example.nested", "Nested"))

        with self.assertRaises(ContractError):
            parse_ios_artifact(app)


if __name__ == "__main__":
    unittest.main()
