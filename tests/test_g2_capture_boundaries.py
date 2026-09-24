"""Release privacy and timing must bind before and across native transport."""
import base64
from pathlib import Path
import unittest

from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.model import Lab, LiveError
from reproof.live.providers import IosProvider
from tests import test_g2_lab_integration as fixture
from tests.test_recording_recovery import project_document


class CaptureBoundaryTests(unittest.TestCase):
    setUp = fixture.ReleaseLabTests.setUp
    tearDown = fixture.ReleaseLabTests.tearDown
    register = fixture.ReleaseLabTests.register
    create = fixture.ReleaseLabTests.create

    def _native_descriptor(self):
        self.lab.close_all()
        self.device["kind"] = "ios-simulator"
        self.device["capabilities"]["authorityMode"] = "legacy-offline-v1"
        # The provider exposes no acquisition classifier or capture-disable
        # capability. Its factory is entirely synthetic and never runs Xcode.
        self.lab = Lab([self.device], Path(self.temp.name) / "native-descriptor",
                       recording_clock_sync=ClockSynchronizer(self.clock),
                       recording_wall_clock_ms=lambda: 5_000_000)

    def test_native_sample_bound_mode_rejects_before_provider_construction(self):
        self._native_descriptor()
        with self.assertRaises(LiveError):
            self.create(self.register("sample-bound"))
        self.assertEqual(self.factory_calls, 0)

    def test_native_suppressed_mode_rejects_before_provider_construction(self):
        self._native_descriptor()
        with self.assertRaises(LiveError):
            self.create(self.register("suppressed"))
        self.assertEqual(self.factory_calls, 0)

    def test_disabled_pixels_rejects_unconditional_native_capture_before_start(self):
        self._native_descriptor()
        project = project_document()
        project["evidencePolicy"]["pixels"] = False
        with self.assertRaises(LiveError):
            self.create(self.register(project=project))
        self.assertEqual(self.factory_calls, 0)

    def test_native_bridge_without_acquisition_bound_cannot_freeze_precise_capture(self):
        session = self.create(self.register())
        provider = IosProvider.__new__(IosProvider)
        provider.profile = None
        provider.device_authority = None
        provider.native_handshake = None
        provider.sid = session["id"]
        provider.lab = self.lab
        # Exercise the real native bridge parser. The wall timestamp alone
        # cannot establish an interval on the host's monotonic recording clock.
        self.clock.advance(5_000_000_000)
        body = {
            "imageBase64": base64.b64encode(b"delayed-native-frame").decode(),
            "mime": "image/png", "width": 400, "height": 800,
            "orientation": "portrait", "capturedAt": 5_000_010,
            "nativeFrameId": 2,
        }
        try:
            provider.bridge("frame", body)
        except LiveError:
            pass  # Rejecting unknown native timing is an allowed outcome.
        frozen = self.lab.stop_release_recording(
            session["id"], "owner", session["controllerId"], session["epoch"])
        self.assertEqual(frozen["status"], "frozen-incomplete")
        # G0 serializes capture gaps as immutable original interruptions.
        self.assertGreater(len(frozen["original"]["interruptions"]), 0)


if __name__ == "__main__":
    unittest.main()
