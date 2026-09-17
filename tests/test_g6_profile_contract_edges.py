import base64
import threading
import unittest
from unittest.mock import Mock

from reproloop.core import ContractError
from reproloop.ios_profile import validate_ios_profile
from reproloop.live.iphone import PhysicalIosProvider
from tests.test_worker_profiles import ios_document, physical_ios_document


class RuntimeProfileEdgeTests(unittest.TestCase):
    def test_nested_non_string_values_raise_contract_errors(self):
        for path in (("capabilities", "locator", "targets"),
                     ("capabilities", "geometry", "orientations"),
                     ("approvedReferences", "preparations")):
            value = ios_document()
            selected = value
            for key in path[:-1]:
                selected = selected[key]
            selected[path[-1]] = [{}]
            with self.subTest(path=path), self.assertRaises(ContractError):
                validate_ios_profile(value)
        value = ios_document()
        value["capabilities"]["captureAdapter"]["id"] = []
        with self.assertRaises(ContractError):
            validate_ios_profile(value)

    def _receive(self, width, height, orientation):
        value = physical_ios_document()
        value["capabilities"]["geometry"] = {
            "maxWidth": 320, "maxHeight": 640, "orientations": ["portrait"]}
        provider = object.__new__(PhysicalIosProvider)
        provider.profile = validate_ios_profile(value)
        provider.frame_mutex = threading.Lock()
        provider.last_native_frame = 0
        provider.sid = "synthetic-session"
        provider.lab = Mock()
        provider.transport = Mock()
        provider.transport.call.return_value = {
            "nativeFrameId": 1, "imageBase64": base64.b64encode(b"synthetic-frame").decode(),
            "mime": "image/jpeg", "width": width, "height": height,
            "orientation": orientation, "capturedAt": 1}
        provider._receive_frame()
        return provider.lab.publish_frame.call_count

    def test_actual_frame_must_fit_the_declared_geometry(self):
        self.assertEqual(self._receive(320, 640, "portrait"), 1)
        for dimensions in ((640, 320, "landscape"), (321, 640, "portrait"),
                           (320, 641, "portrait")):
            with self.subTest(dimensions=dimensions), self.assertRaises(ContractError):
                self._receive(*dimensions)
