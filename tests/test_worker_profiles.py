import copy
import hashlib
import plistlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from reproloop.android_profile import validate_android_runtime_profile
from reproloop.core import ContractError, digest
from reproloop.ios_profile import validate_ios_profile
from reproloop.ios_storage import tree_manifest
from reproloop.live.android_live import AndroidLiveProvider
from reproloop.live.iphone import PhysicalIosProvider, validate_signed_products


PROJECT_DIGEST = "1" * 64
PROVENANCE_DIGEST = "2" * 64


def ios_document(artifact_digest="3" * 64):
    return {
        "schemaVersion": 2,
        "kind": "reproloop-runtime-application",
        "id": "checkout_ios",
        "projectId": "checkout",
        "projectDigest": PROJECT_DIGEST,
        "applicationId": "ios_app",
        "buildId": "original",
        "platform": "ios",
        "bundle": "com.example.checkout",
        "launchTarget": {"kind": "bundle", "value": "com.example.checkout"},
        "artifact": {
            "kind": "ios-app", "sha256": artifact_digest, "bytes": 512,
            "provenanceDigest": PROVENANCE_DIGEST,
            "bundleVersion": "1.4", "bundleBuild": "27",
        },
        "helper": {"protocolVersion": 2, "version": 2},
        "capabilities": {
            "actions": ["tap", "long_press", "swipe", "text", "home", "launch"],
            "locator": {"kind": "xctest-accessibility", "version": 1,
                        "targets": ["checkout_button"]},
            "observations": ["pixels", "accessibility"],
            "geometry": {"maxWidth": 4096, "maxHeight": 4096,
                         "orientations": ["portrait", "landscape"]},
            "captureAdapter": {"id": "native-frame", "version": 1},
            "logAdapter": None,
        },
        "approvedReferences": {"launch": "launch_checkout", "preparations": []},
        "identityRequirement": "install-and-launch",
    }


def android_document(artifact_digest="4" * 64):
    return {
        "schemaVersion": 2,
        "kind": "reproloop-runtime-application",
        "id": "checkout_android",
        "projectId": "checkout",
        "projectDigest": PROJECT_DIGEST,
        "applicationId": "android_app",
        "buildId": "original",
        "platform": "android",
        "package": "com.example.checkout",
        "launchTarget": {"kind": "activity", "value": ".MainActivity"},
        "artifact": {
            "kind": "android-apk", "sha256": artifact_digest, "bytes": 256,
            "provenanceDigest": PROVENANCE_DIGEST, "versionCode": 27,
        },
        "helper": {"protocolVersion": 2, "version": 2},
        "capabilities": {
            "actions": ["tap", "long_press", "swipe", "text", "home", "launch"],
            "locator": {"kind": "android-resource-id", "version": 1,
                        "targets": ["checkout_button"]},
            "observations": ["pixels", "accessibility", "logs"],
            "geometry": {"maxWidth": 960, "maxHeight": 4096,
                         "orientations": ["portrait", "landscape"]},
            "captureAdapter": {"id": "native-frame", "version": 1},
            "logAdapter": {"id": "repro-app-log", "version": 1},
        },
        "approvedReferences": {"launch": "launch_checkout",
                               "preparations": ["seed_checkout"]},
        "identityRequirement": "installed-sha256",
    }


def physical_ios_document(artifact_digest="3" * 64):
    value = ios_document(artifact_digest)
    value["capabilities"]["locator"] = None
    value["capabilities"]["observations"] = ["pixels"]
    return value


class GeneralApplicationProfileTests(unittest.TestCase):
    def test_general_ios_cannot_claim_the_sample_only_log_adapter(self):
        value = physical_ios_document()
        value['capabilities']['observations'].append('logs')
        value['capabilities']['logAdapter'] = {'id': 'repro-app-log', 'version': 1}
        profile = validate_ios_profile(value)
        with patch('reproloop.live.iphone.TunnelClient'), self.assertRaises(ContractError):
            PhysicalIosProvider(Mock(udid='synthetic', tunnel_address='fd00::1'), Path('products'), Path('app'),
                                profile.application_identity, profile=profile)

    def test_ios_general_profile_is_closed_and_refuses_unavailable_identity_strength(self):
        profile = validate_ios_profile(ios_document())
        self.assertEqual(profile.bundle, "com.example.checkout")
        self.assertTrue(profile.supports_identity_requirement("install-and-launch"))
        self.assertFalse(profile.supports_identity_requirement("installed-sha256"))
        changed = ios_document()
        changed["capabilities"]["geometry"]["maxWidth"] = 9000
        with self.assertRaises(ContractError):
            validate_ios_profile(changed)
        changed = ios_document()
        changed["shell"] = "xcodebuild"
        with self.assertRaises(ContractError):
            validate_ios_profile(changed)

    def test_android_general_profile_has_a_distinct_native_adapter(self):
        profile = validate_android_runtime_profile(android_document())
        native = profile.native()
        self.assertEqual(native["schemaVersion"], 2)
        self.assertEqual(native["package"], "com.example.checkout")
        self.assertNotIn("fixture", native)
        self.assertNotIn("oracle", native)
        self.assertNotIn("build", native)
        self.assertTrue(profile.supports_identity_requirement("installed-sha256"))
        duplicate = android_document()
        duplicate["capabilities"]["actions"].append("tap")
        with self.assertRaises(ContractError):
            validate_android_runtime_profile(duplicate)

    def test_non_sample_signed_product_validation_binds_profile_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products = root / "products"
            runner = products / "Debug-iphoneos" / "GeneralTests-Runner.app"
            host = products / "Debug-iphoneos" / "ReproLiveHost.app"
            app = root / "Checkout.app"
            for item in (runner, host, app):
                item.mkdir(parents=True)
                (item / "embedded.mobileprovision").write_bytes(b"synthetic-test-profile")
            (app / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "com.example.checkout",
                "CFBundleShortVersionString": "1.4",
                "CFBundleVersion": "27",
            }))
            artifact_digest = digest(tree_manifest(app))
            document = physical_ios_document(artifact_digest)
            document["artifact"]["bytes"] = sum(
                item.stat().st_size for item in app.rglob("*") if item.is_file())
            profile = validate_ios_profile(document)
            with patch("reproloop.live.iphone.subprocess.run",
                       return_value=Mock(returncode=0)):
                identity = validate_signed_products(products, app, profile=profile)
            self.assertEqual(identity["bundle"], "com.example.checkout")
            self.assertEqual(identity["artifactDigest"], artifact_digest)
            self.assertEqual(identity["installedDigestProof"], "unavailable")
            wrong = copy.deepcopy(document)
            wrong["bundle"] = "com.example.other"
            wrong["launchTarget"]["value"] = "com.example.other"
            with patch("reproloop.live.iphone.subprocess.run",
                       return_value=Mock(returncode=0)), self.assertRaises(Exception):
                validate_signed_products(products, app,
                                         profile=validate_ios_profile(wrong))

    def test_ios_ipa_signed_product_validation_binds_declared_payload_subset(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products = root / "products"
            runner = products / "Debug-iphoneos" / "GeneralTests-Runner.app"
            host = products / "Debug-iphoneos" / "ReproLiveHost.app"
            app = root / "Checkout.app"
            for item in (runner, host, app):
                item.mkdir(parents=True)
                (item / "embedded.mobileprovision").write_bytes(b"synthetic-test-profile")
            (app / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "com.example.checkout",
                "CFBundlePackageType": "APPL",
                "CFBundleExecutable": "Checkout",
                "CFBundleShortVersionString": "1.4",
                "CFBundleVersion": "27",
            }))
            (app / "PkgInfo").write_bytes(b"APPL????")
            (app / "Checkout").write_bytes(b"synthetic-executable")
            (app / "_CodeSignature").mkdir()
            (app / "_CodeSignature" / "CodeResources").write_bytes(b"sealed-resources")
            # 선언 IPA 페이로드에 없는 초과 파일 — 앱 자체 서명이 봉인한다.
            (app / "extra.dylib").write_bytes(b"sealed-debug-extra")
            ipa = root / "original.ipa"
            with zipfile.ZipFile(ipa, "w") as archive:
                for name in ("Info.plist", "PkgInfo", "Checkout",
                             "_CodeSignature/CodeResources",
                             "embedded.mobileprovision"):
                    archive.write(app / name, f"Payload/Checkout.app/{name}")
            body = ipa.read_bytes()
            document = physical_ios_document()
            document["artifact"]["kind"] = "ios-ipa"
            document["artifact"]["sha256"] = hashlib.sha256(body).hexdigest()
            document["artifact"]["bytes"] = len(body)
            profile = validate_ios_profile(document)
            with patch("reproloop.live.iphone.subprocess.run",
                       return_value=Mock(returncode=0)):
                identity = validate_signed_products(products, app,
                                                    profile=profile, ipa=ipa)
            self.assertEqual(identity["bundle"], "com.example.checkout")
            self.assertEqual(identity["artifactDigest"],
                             hashlib.sha256(body).hexdigest())

            # ipa 없이 ios-ipa를 검증하면 명시적으로 거절한다.
            with patch("reproloop.live.iphone.subprocess.run",
                       return_value=Mock(returncode=0)), self.assertRaises(Exception):
                validate_signed_products(products, app, profile=profile)

            # 선언 페이로드 내용이 설치 트리에서 바뀌면 거절한다.
            (app / "PkgInfo").write_bytes(b"tampered-payload")
            with patch("reproloop.live.iphone.subprocess.run",
                       return_value=Mock(returncode=0)), self.assertRaises(Exception):
                validate_signed_products(products, app, profile=profile, ipa=ipa)

            # 선언 멤버가 설치 트리에 없으면 거절한다.
            (app / "PkgInfo").write_bytes(b"APPL????")
            (app / "Checkout").unlink()
            with patch("reproloop.live.iphone.subprocess.run",
                       return_value=Mock(returncode=0)), self.assertRaises(Exception):
                validate_signed_products(products, app, profile=profile, ipa=ipa)

            # 선언 IPA 자체가 바뀌면 컨테이너 바인딩이 거절한다.
            (app / "Checkout").write_bytes(b"synthetic-executable")
            ipa.write_bytes(body + b"changed")
            with patch("reproloop.live.iphone.subprocess.run",
                       return_value=Mock(returncode=0)), self.assertRaises(Exception):
                validate_signed_products(products, app, profile=profile, ipa=ipa)

    def test_general_providers_do_not_select_sample_reset_or_sdk_defaults(self):
        ios = validate_ios_profile(physical_ios_document())
        device = Mock(udid="private-udid", tunnel_address="fd00::2")
        with patch("reproloop.live.iphone.TunnelClient"):
            provider = PhysicalIosProvider(
                device, Path("products"), Path("Checkout.app"),
                ios.application_identity, profile=ios)
        self.assertIsNone(provider.fixture)
        self.assertFalse(provider.record_sdk)
        self.assertEqual(provider.profile.digest, ios.digest)

        unsupported = validate_ios_profile(ios_document())
        with patch("reproloop.live.iphone.TunnelClient"), self.assertRaises(Exception):
            PhysicalIosProvider(
                device, Path("products"), Path("Checkout.app"),
                unsupported.application_identity, profile=unsupported)

        android = validate_android_runtime_profile(android_document())
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory).resolve() / "checkout.apk"
            helper = Path(directory).resolve() / "helper.apk"
            app.write_bytes(b"checkout")
            helper.write_bytes(b"helper")
            document = android_document(hashlib.sha256(b"checkout").hexdigest())
            document["artifact"]["bytes"] = len(b"checkout")
            document["capabilities"]["logAdapter"] = None
            document["capabilities"]["observations"].remove("logs")
            android = validate_android_runtime_profile(document)
            with patch("reproloop.live.android_live.AdbDevice"):
                provider = AndroidLiveProvider(
                    "serial", helper, app, runtime_profile=android)
            self.assertIsNone(provider.fixture)
            self.assertFalse(provider.record_sdk)
            self.assertFalse(provider.automatic_app_logs)

    def test_android_registered_locator_binds_fresh_native_bounds_to_frame(self):
        body = b"checkout"
        document = android_document(hashlib.sha256(body).hexdigest())
        document["artifact"]["bytes"] = len(body)
        document["capabilities"]["logAdapter"] = None
        document["capabilities"]["observations"].remove("logs")
        profile = validate_android_runtime_profile(document)
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory).resolve() / "checkout.apk"
            helper = Path(directory).resolve() / "helper.apk"
            app.write_bytes(body);helper.write_bytes(b"helper")
            with patch("reproloop.live.android_live.AdbDevice"):
                provider = AndroidLiveProvider(
                    "serial", helper, app, runtime_profile=profile)
        provider.transport = Mock()
        provider.transport.call.return_value = {
            "ready": True, "profileDigest": profile.digest,
            "nativeDigest": profile.native_digest,
            "targetPackage": profile.package, "generalProfile": True,
            "capabilities": {"actions": profile.data["capabilities"]["actions"]},
            "screenBounds": {"left": 0, "top": 0, "right": 1000,
                             "bottom": 2000},
            "nodes": [{"id": "checkout_button",
                       "bounds": {"left": 400, "top": 800,
                                  "right": 600, "bottom": 1000},
                       "visible": True, "enabled": True}],
        }
        provider.lab = Mock()
        provider.lab.frame.return_value = {"id": 7, "geometryVersion": 3}
        provider.sid = "session"
        provider.provider_incarnation = "provider_current"
        result = provider.resolve_locator(
            {"kind": "resource-id", "value": "checkout_button"})
        self.assertEqual((result["x"], result["y"]), (0.5, 0.45))
        self.assertEqual((result["frameId"], result["geometryVersion"]), (7, 3))
        with self.assertRaises(Exception):
            provider.resolve_locator(
                {"kind": "resource-id", "value": "unregistered"})


if __name__ == "__main__":
    unittest.main()
