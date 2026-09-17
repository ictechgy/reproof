import json
from pathlib import Path
import platform
import struct
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "native/macos-video/Sources/ReproVideo/main.swift"


def configuration(schema=1):
    value = {
        "schemaVersion": schema,
        "codec": "h264",
        "container": "mp4",
        "width": 96,
        "height": 160,
        "maxFrames": 4,
        "maxCompressedBytes": 4096,
        "maxDecodedPixels": 1_000_000,
        "maxOutputBytes": 4096,
        "faultMode": "none",
    }
    if schema == 2:
        value.update(targetBitrate=1_200_000, maxKeyFrameInterval=30)
    return value


class VideoCompressionProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if platform.system() != "Darwin":
            raise unittest.SkipTest("AVFoundation helper requires macOS")
        cls.temp = tempfile.TemporaryDirectory(prefix="repro-video-profile-")
        cls.binary = Path(cls.temp.name) / "ReproVideo"
        compiled = subprocess.run(
            ["/usr/bin/xcrun", "swiftc", "-O", "-target", "arm64-apple-macosx13.0",
             str(SOURCE), "-o", str(cls.binary)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=90, check=False,
        )
        if compiled.returncode:
            raise AssertionError((compiled.stdout + compiled.stderr).decode("utf-8", "replace"))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_config(self, value):
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        wire = b"RLVID001" + struct.pack(">I", len(encoded)) + encoded + b"E" + b"\0" * 8
        return subprocess.run(
            [str(self.binary), "--protocol-stdio"], input=wire,
            cwd=self.temp.name, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, check=False,
        )

    def test_v1_and_v2_profiles_parse_without_changing_finish_protocol(self):
        for schema in (1, 2):
            with self.subTest(schema=schema):
                result = self.run_config(configuration(schema))
                self.assertEqual(result.returncode, 2)
                self.assertIn(b"video_helper_failed:empty_segment", result.stderr)

    def test_v1_rejects_v2_fields_and_v2_requires_both_profile_fields(self):
        legacy_with_v2 = configuration(1)
        legacy_with_v2.update(targetBitrate=1_200_000, maxKeyFrameInterval=30)
        missing = configuration(2)
        del missing["targetBitrate"]
        for value in (legacy_with_v2, missing):
            with self.subTest(value=value):
                result = self.run_config(value)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(b"empty_segment", result.stderr)

    def test_v2_profile_bounds_and_types_are_strict(self):
        cases = []
        for key, values in {
            "targetBitrate": (127_999, 4_000_001, True),
            "maxKeyFrameInterval": (0, 61, True),
        }.items():
            for value in values:
                altered = configuration(2)
                altered[key] = value
                cases.append(altered)
        unknown = configuration(2)
        unknown["targetBitrateExtra"] = 1
        cases.append(unknown)
        for value in cases:
            with self.subTest(value=value):
                result = self.run_config(value)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(b"empty_segment", result.stderr)


if __name__ == "__main__":
    unittest.main()
