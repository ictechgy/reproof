"""Actual local ImageIO/AVFoundation decoding at the package boundary."""
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import unittest
import zlib

from reproof import contracts
from reproof.issue_package import IssuePackageStore, PackageError, NativeMediaValidator, build_archive, inspect_archive
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore
from tests.test_issue_package import example


ROOT = Path(__file__).resolve().parents[1]


def png(width=8, height=6):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\x10\x80\xc0" * width) * height))
            + chunk(b"IEND", b""))


class NativeIssueMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = tempfile.TemporaryDirectory()
        cls.helper = Path(cls.tools.name) / "media-validator"
        result = subprocess.run(["swiftc", str(ROOT / "native/macos-media-validator/main.swift"),
                                 "-o", str(cls.helper)], capture_output=True, timeout=30)
        if result.returncode:
            cls.tools.cleanup()
            raise RuntimeError("Local media validator compilation failed")

    @classmethod
    def tearDownClass(cls):
        cls.tools.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.budget = DiskBudget(root / "budget", capacity_bytes=16 * 1024 * 1024,
                                 journal_headroom_bytes=64 * 1024)
        self.evidence = EvidenceStore(root / "evidence", self.budget)
        self.validator = NativeMediaValidator(self.evidence, self.helper, "media_test")

    def tearDown(self):
        self.evidence.close()
        self.budget.close()
        self.temp.cleanup()

    def test_actual_image_decode_and_mime_binding(self):
        before = self.budget.snapshot()["chargedBytes"]
        result = self.validator(png(), "image/png")
        self.assertEqual(result, {"width": 8, "height": 6, "mimeType": "image/png"})
        self.assertEqual(self.budget.snapshot()["chargedBytes"], before)
        for body, mime in ((png(), "image/jpeg"), (png()[:30], "image/png"),
                           (b"\x89PNG\r\n\x1a\nmalformed", "image/png"),
                           (b"\0\0\0\x18ftypisom" + b"\0" * 80, "video/mp4")):
            with self.subTest(mime=mime), self.assertRaises(PackageError):
                self.validator(body, mime)
        self.assertEqual(self.budget.snapshot()["chargedBytes"], before)

    def test_actual_mp4_decodes_from_the_extensionless_g2_staging_path(self):
        body = (ROOT / 'tests/fixtures/media/irregular-h264.mp4').read_bytes()
        before = self.budget.snapshot()['chargedBytes']
        report = self.validator(body, 'video/mp4')
        self.assertEqual((report['codec'], report['width'], report['height'], report['frameCount']), ('h264', 96, 160, 11))
        self.assertEqual(len(report['presentationTimesMs']), 11)
        self.assertEqual(report['presentationTimesMs'][0], 0)
        self.assertAlmostEqual(report['presentationTimesMs'][-1], 1460)
        self.assertEqual(self.budget.snapshot()['chargedBytes'], before)

    def test_actual_media_archive_round_trip_and_invalid_image_rejection(self):
        recording, spec = example()
        image = png()
        digest = hashlib.sha256(image).hexdigest()
        recording["original"]["media"] = [{"id": "frame_one", "digest": digest,
            "path": "objects/sha256/" + digest[:2] + "/" + digest,
            "bytes": len(image), "mimeType": "image/png"}]
        recording["recordingDigest"] = contracts.digest(recording["original"])
        spec["originalRecordingDigest"] = recording["recordingDigest"]
        body = build_archive(recording, spec, lambda _: image, media_validator=self.validator)
        parsed = inspect_archive(io.BytesIO(body), media_validator=self.validator)
        self.assertEqual(parsed.recording, recording)
        # This negative case reaches the real decoder with a correct checksum.
        broken = b"\x89PNG\r\n\x1a\nmalformed"
        reference = recording["original"]["media"][0]
        reference.update(digest=hashlib.sha256(broken).hexdigest(), bytes=len(broken))
        recording["recordingDigest"] = contracts.digest(recording["original"])
        spec["originalRecordingDigest"] = recording["recordingDigest"]
        with self.assertRaises(PackageError) as caught:
            build_archive(recording, spec, lambda _: broken, media_validator=self.validator)
        self.assertEqual(caught.exception.code, "package_media")


if __name__ == "__main__":
    unittest.main()
