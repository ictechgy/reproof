from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from reproloop.ios_profile import validate_ios_profile
from reproloop.live.iphone import PhysicalIosProvider
from reproloop.live.model import LiveError
from tests.test_worker_profiles import physical_ios_document


class InstalledIosIdentityTests(unittest.TestCase):
    def _start(self, apps):
        profile = validate_ios_profile(physical_ios_document())
        provider = object.__new__(PhysicalIosProvider)
        provider.profile = profile
        provider.device = SimpleNamespace(identifier="owned-device", public_id="owned-phone",
                                          udid="owned-udid", tunnel_address="127.0.0.1")
        provider.udid = "owned-udid"
        provider.app = Path("synthetic.app")
        provider.products = Path("synthetic-products")
        provider.port = 9876
        provider.token = "owned-synthetic-token"
        provider._check_permit = Mock()
        provider.device_authority = Mock()
        provider.device_authority.native_grant.return_value.wire.return_value = {"proof": "synthetic"}

        def start(*_, **__):
            provider.native_handshake = object()
            provider.launched_identity_evidence = {
                "launchedBundle": profile.bundle, "profileDigest": profile.digest,
                "helperProtocolVersion": 2, "helperVersion": 2}
            provider.handshake_ready.set()
            provider.startup_complete.set()

        provider.start = start
        transport = Mock()
        transport.call.return_value = {"activated": True, "authority": {"proof": "synthetic"}}
        with patch("reproloop.live.iphone._devicectl", side_effect=[{}, {"apps": apps}]) as query, \
                patch("reproloop.live.iphone.validate_signed_products"), \
                patch("reproloop.live.iphone.select_iphone", return_value=provider.device), \
                patch("reproloop.live.iphone.public_device_status", return_value={"ready": True}), \
                patch("reproloop.live.iphone.TunnelClient", return_value=transport):
            value = provider.start_authorized({}, object(), object())
        return value, query.call_args_list

    def test_installed_versions_are_measured_after_install(self):
        value, calls = self._start([{
            "bundleIdentifier": "com.example.checkout", "version": "1.4", "bundleVersion": "27"}])
        evidence = value["identityEvidence"]
        self.assertEqual(evidence["installedIdentityProof"], "devicectl-device-info-apps")
        self.assertEqual((evidence["bundleVersion"], evidence["bundleBuild"]), ("1.4", "27"))
        self.assertIsNone(evidence["installedArtifactDigest"])
        self.assertEqual(calls[1].args[:3], ("device", "info", "apps"))
        self.assertIn("--bundle-id", calls[1].args)

    def test_install_success_cannot_hide_an_installed_version_mismatch(self):
        with self.assertRaises(LiveError) as wrong:
            self._start([{"bundleIdentifier": "com.example.checkout", "version": "9.9",
                          "bundleVersion": "999"}])
        self.assertEqual(wrong.exception.code, "app_changed")

    def test_absent_or_ambiguous_installed_identity_is_denied(self):
        app = {"bundleIdentifier": "com.example.checkout", "version": "1.4", "bundleVersion": "27"}
        for apps in ([], [app, app], [{"bundleIdentifier": "com.example.checkout"}]):
            with self.subTest(apps=len(apps)), self.assertRaises(LiveError):
                self._start(apps)
