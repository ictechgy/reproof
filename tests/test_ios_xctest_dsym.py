"""Bounded support for Xcode XCTest dSYM sidecars."""
import struct
from pathlib import Path
import plistlib
import unittest

from reproloop.core import ContractError
from reproloop.ios_artifact_transfer import parse_ios_artifact
from tests import test_ios_artifact_transfer as support


_LC_UUID = 0x1B
_ARM64 = 0x0100000C
_X86_64 = 0x01000007
_OWNED_DEVICE_RUNNERS = tuple(
    Path(__file__).resolve().parents[1]
    / f"artifacts/product-delivery/d4-ios-runtime-r1/{build}"
    / "DerivedData/Build/Products/Debug-iphoneos/ReproLiveTests-Runner.app"
    for build in ("helper-device-build", "helper-device-build-r2")
)


def _macho64(file_type, *, cpu_type=_ARM64, uuid=None):
    commands = b"" if uuid is None else struct.pack(
        "<II16s", _LC_UUID, 24, uuid)
    return struct.pack(
        "<IiiIIIII", 0xFEEDFACF, cpu_type, 0, file_type,
        1 if commands else 0, len(commands), 0, 0,
    ) + commands


def _fat(slices):
    table_end = 8 + len(slices) * 20
    offset = table_end
    table = []
    body = []
    for cpu_type, value in slices:
        table.append(struct.pack(">IIIII", cpu_type, 0, offset, len(value), 0))
        body.append(value)
        offset += len(value)
    return struct.pack(">II", 0xCAFEBABE, len(slices)) + b"".join(table) + b"".join(body)


def _dsym_info():
    return {
        "CFBundleDevelopmentRegion": "English",
        "CFBundleIdentifier": "com.apple.xcode.dsym.com.example.tests",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundlePackageType": "dSYM",
        "CFBundleSignature": "????",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "2",
    }


class IOSXCTestDsymTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.IOSArtifactTransferTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def add_xctest(self, app, executable):
        bundle = app / "PlugIns" / "ReproLiveTests.xctest"
        support._write(
            bundle / "Info.plist",
            support._app_info("com.example.inventory.tests", "ReproLiveTests"),
        )
        support._write(bundle / "ReproLiveTests", executable, executable=True)
        return bundle

    def add_dsym(self, app, dwarf, *, relocation=True):
        sidecar = app / "PlugIns" / "ReproLiveTests.xctest.dSYM"
        support._write(sidecar / "Contents" / "Info.plist", _dsym_info())
        support._write(
            sidecar / "Contents" / "Resources" / "DWARF" / "ReproLiveTests",
            dwarf,
        )
        if relocation:
            support._write(
                sidecar / "Contents" / "Resources" / "Relocations"
                / "aarch64" / "ReproLiveTests.yml",
                b"sections: []\n",
            )
        return sidecar

    def test_accepts_bounded_sidecar_and_keeps_debug_files_as_data(self):
        app = self.fixture.make_flat_app()
        uuid = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.add_xctest(app, _macho64(8, uuid=uuid))
        sidecar = self.add_dsym(app, _macho64(10, uuid=uuid))

        artifact = parse_ios_artifact(app)
        names = {item["path"] for item in artifact.manifest["files"]}
        self.assertIn(
            "PlugIns/ReproLiveTests.xctest.dSYM/Contents/Info.plist", names
        )
        self.assertIn(
            "PlugIns/ReproLiveTests.xctest.dSYM/Contents/Resources/DWARF/ReproLiveTests",
            names,
        )
        self.assertIn(
            "PlugIns/ReproLiveTests.xctest.dSYM/Contents/Resources/Relocations"
            "/aarch64/ReproLiveTests.yml",
            names,
        )
        self.assertFalse(
            any(".dSYM" in row["bundlePath"] for row in artifact.manifest["codeObjects"])
        )
        dwarf = next(
            item for item in artifact.manifest["files"]
            if item["path"].endswith("Resources/DWARF/ReproLiveTests")
        )
        self.assertFalse(dwarf["executable"])
        self.assertEqual(
            [row["kind"] for row in artifact.manifest["codeObjects"]
             if row["bundlePath"] == "PlugIns/ReproLiveTests.xctest"],
            ["xctest"],
        )
        ipa = self.fixture.make_ipa(app, self.fixture.root / "with-dsym.ipa")
        self.assertEqual(artifact.app_digest, parse_ios_artifact(ipa).app_digest)

        original_digest = artifact.app_digest
        (sidecar / "Contents" / "Resources" / "Relocations"
         / "aarch64" / "ReproLiveTests.yml").write_bytes(b"sections: [changed]\n")
        self.assertNotEqual(original_digest, parse_ios_artifact(app).app_digest)

    def test_accepts_all_matching_fat_slices(self):
        arm_uuid = bytes.fromhex("00112233445566778899aabbccddeeff")
        x86_uuid = bytes.fromhex("ffeeddccbbaa99887766554433221100")
        xctest = _fat([
            (_ARM64, _macho64(8, uuid=arm_uuid)),
            (_X86_64, _macho64(8, cpu_type=_X86_64, uuid=x86_uuid)),
        ])
        dsym = _fat([
            (_ARM64, _macho64(10, uuid=arm_uuid)),
            (_X86_64, _macho64(10, cpu_type=_X86_64, uuid=x86_uuid)),
        ])
        app = self.fixture.make_flat_app()
        self.add_xctest(app, xctest)
        self.add_dsym(app, dsym, relocation=False)
        parse_ios_artifact(app)

        bad_slice = _fat([
            (_ARM64, _macho64(10, uuid=arm_uuid)),
            (_X86_64, _macho64(2, cpu_type=_X86_64, uuid=x86_uuid)),
        ])
        malformed = self.fixture.make_flat_app()
        self.add_xctest(malformed, xctest)
        self.add_dsym(malformed, bad_slice, relocation=False)
        with self.assertRaises(ContractError):
            parse_ios_artifact(malformed)

    def test_pair_requires_one_nonzero_uuid_on_each_side(self):
        uuid = bytes.fromhex("00112233445566778899aabbccddeeff")
        for test_uuid, debug_uuid in ((None, uuid), (uuid, None),
                                      (None, None), (bytes(16), bytes(16))):
            with self.subTest(test_uuid=test_uuid, debug_uuid=debug_uuid):
                app = self.fixture.make_flat_app()
                self.add_xctest(app, _macho64(8, uuid=test_uuid))
                self.add_dsym(app, _macho64(10, uuid=debug_uuid))
                with self.assertRaises(ContractError):
                    parse_ios_artifact(app)
        for duplicate_side in ('xctest', 'dsym'):
            with self.subTest(duplicate_side=duplicate_side):
                app = self.fixture.make_flat_app()
                binaries = {'xctest': _macho64(8, uuid=uuid), 'dsym': _macho64(10, uuid=uuid)}
                original = binaries[duplicate_side]
                binaries[duplicate_side] = (original[:16] + struct.pack('<II', 2, 48)
                                            + original[24:] + original[32:])
                self.add_xctest(app, binaries['xctest'])
                self.add_dsym(app, binaries['dsym'])
                with self.assertRaises(ContractError):
                    parse_ios_artifact(app)

    @unittest.skipUnless(
        all(path.is_dir() for path in _OWNED_DEVICE_RUNNERS),
        "owned generated device runners are unavailable",
    )
    def test_accepts_owned_generated_device_runner_sidecar(self):
        for runner in _OWNED_DEVICE_RUNNERS:
            with self.subTest(runner=runner.parent.parent.parent.parent.name):
                artifact = parse_ios_artifact(runner)
                sidecar = (
                    "PlugIns/ReproLiveTests.xctest.dSYM/Contents/Resources/DWARF"
                    "/ReproLiveTests"
                )
                self.assertTrue(any(
                    item["path"] == sidecar for item in artifact.manifest["files"]
                ))
                self.assertTrue(any(
                    row["bundlePath"] == "PlugIns/ReproLiveTests.xctest"
                    and row["kind"] == "xctest"
                    for row in artifact.manifest["codeObjects"]
                ))
                self.assertFalse(any(
                    row["bundlePath"].endswith(".dSYM")
                    for row in artifact.manifest["codeObjects"]
                ))

    def test_rejects_missing_pair_malformed_headers_uuid_and_unsupported_files(self):
        uuid = bytes.fromhex("00112233445566778899aabbccddeeff")

        missing_pair = self.fixture.make_flat_app()
        self.add_dsym(missing_pair, _macho64(10, uuid=uuid))

        bad_type = self.fixture.make_flat_app()
        self.add_xctest(bad_type, _macho64(8, uuid=uuid))
        self.add_dsym(bad_type, _macho64(2, uuid=uuid))

        bad_header = self.fixture.make_flat_app()
        self.add_xctest(bad_header, _macho64(8, uuid=uuid))
        self.add_dsym(bad_header, b"not a Mach-O")

        bad_uuid = self.fixture.make_flat_app()
        self.add_xctest(bad_uuid, _macho64(8, uuid=uuid))
        self.add_dsym(
            bad_uuid,
            _macho64(10, uuid=bytes.fromhex("ffeeddccbbaa99887766554433221100")),
        )

        missing_uuid = self.fixture.make_flat_app()
        self.add_xctest(missing_uuid, _macho64(8, uuid=uuid))
        self.add_dsym(missing_uuid, _macho64(10))

        wrong_name = self.fixture.make_flat_app()
        self.add_xctest(wrong_name, _macho64(8, uuid=uuid))
        sidecar = self.add_dsym(wrong_name, _macho64(10, uuid=uuid))
        (sidecar / "Contents" / "Resources" / "DWARF" / "ReproLiveTests").rename(
            sidecar / "Contents" / "Resources" / "DWARF" / "Other"
        )

        bad_info = self.fixture.make_flat_app()
        self.add_xctest(bad_info, _macho64(8, uuid=uuid))
        sidecar = self.add_dsym(bad_info, _macho64(10, uuid=uuid))
        info = plistlib.loads(
            (sidecar / "Contents" / "Info.plist").read_bytes()
        )
        info["CFBundlePackageType"] = "BNDL"
        support._write(sidecar / "Contents" / "Info.plist", info)

        unsupported_file = self.fixture.make_flat_app()
        self.add_xctest(unsupported_file, _macho64(8, uuid=uuid))
        sidecar = self.add_dsym(unsupported_file, _macho64(10, uuid=uuid))
        support._write(sidecar / "Contents" / "Resources" / "Unexpected", b"data")

        for app in (missing_pair, bad_type, bad_header, bad_uuid,
                    missing_uuid, wrong_name, bad_info, unsupported_file):
            with self.subTest(app=app.name):
                with self.assertRaises(ContractError):
                    parse_ios_artifact(app)


if __name__ == "__main__":
    unittest.main()
