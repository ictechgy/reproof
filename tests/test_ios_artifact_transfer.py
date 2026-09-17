import copy
from dataclasses import replace
import gc
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import weakref

from reproloop.core import ContractError


MACHO64_ARM64 = struct.pack(
    "<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0
)


def _write(path, value, *, executable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, dict):
        value = plistlib.dumps(value, fmt=plistlib.FMT_BINARY)
    path.write_bytes(value)
    os.chmod(path, 0o700 if executable else 0o600)


def _zip_file(archive, name, value, *, executable=False):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
    archive.writestr(info, value)


def _zip_directory(archive, name, *, mode=0o755):
    if not name.endswith("/"):
        name += "/"
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFDIR | mode) << 16
    archive.writestr(info, b"")


def _app_info(bundle, executable):
    return {
        "CFBundleIdentifier": bundle,
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "27",
        "CFBundleExecutable": executable,
    }


class IOSArtifactTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="repro-ios-artifacts-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.flat_count = 0

    def make_app(self, *, links=True, profiles=True):
        app = self.root / "Inventory.app"
        app.mkdir()
        _write(app / "Info.plist", _app_info("com.example.inventory", "Inventory"))
        _write(app / "Inventory", MACHO64_ARM64, executable=True)

        framework = app / "Frameworks" / "Foo.framework"
        _write(framework / "Info.plist", _app_info("com.example.foo", "Foo"))
        _write(framework / "Versions" / "A" / "Foo", MACHO64_ARM64, executable=True)
        if links:
            (framework / "Versions" / "Current").symlink_to("A", target_is_directory=True)
            (framework / "Foo").symlink_to("Versions/Current/Foo")
        else:
            _write(framework / "Foo", MACHO64_ARM64, executable=True)

        appex = app / "PlugIns" / "Widget.appex"
        _write(appex / "Info.plist", _app_info("com.example.inventory.widget", "Widget"))
        _write(appex / "Widget", MACHO64_ARM64, executable=True)
        if profiles:
            _write(app / "embedded.mobileprovision", b"owned dummy root profile")
            _write(appex / "embedded.mobileprovision", b"owned dummy extension profile")
        return app

    def make_flat_app(self):
        self.flat_count += 1
        app = self.root / f"Flat{self.flat_count}.app"
        app.mkdir()
        _write(app / "Info.plist", _app_info("com.example.flat", "Flat"))
        _write(app / "Flat", MACHO64_ARM64, executable=True)
        framework = app / "Frameworks" / "FlatKit.framework"
        _write(framework / "Info.plist", _app_info("com.example.flatkit", "FlatKit"))
        _write(framework / "FlatKit", MACHO64_ARM64, executable=True)
        return app

    def make_ipa(self, app, destination=None, *, add_symlink=False):
        destination = destination or (self.root / "Inventory.ipa")
        app_name = app.name
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(app.rglob("*")):
                if path.is_symlink():
                    if not add_symlink:
                        continue
                    info = zipfile.ZipInfo("Payload/" + app_name + "/Link")
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, "../../outside")
                    continue
                if path.is_file():
                    relative = path.relative_to(app).as_posix()
                    _zip_file(archive, "Payload/" + app_name + "/" + relative,
                              path.read_bytes(), executable=bool(path.stat().st_mode & 0o111))
            if add_symlink and not any(path.is_symlink() for path in app.rglob("*")):
                info = zipfile.ZipInfo("Payload/" + app_name + "/Link")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, "../../outside")
        return destination

    def make_ipa_with_directories(self, app, destination=None, *, unsafe_mode=None,
                                  unsafe_directory_mode=None):
        destination = destination or (self.root / "with-directories.ipa")
        app_name = app.name
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
            _zip_directory(archive, "Payload")
            _zip_directory(archive, "Payload/" + app_name)
            for directory in sorted(path for path in app.rglob("*") if path.is_dir()):
                relative = directory.relative_to(app).as_posix()
                _zip_directory(archive, "Payload/" + app_name + "/" + relative)
            for path in sorted(path for path in app.rglob("*") if path.is_file()):
                relative = path.relative_to(app).as_posix()
                _zip_file(archive, "Payload/" + app_name + "/" + relative,
                          path.read_bytes(), executable=bool(path.stat().st_mode & 0o111))
            if unsafe_mode is not None:
                info = zipfile.ZipInfo("Payload/" + app_name + "/Unsafe")
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | unsafe_mode) << 16
                archive.writestr(info, b"unsafe")
            if unsafe_directory_mode is not None:
                _zip_directory(archive, "Payload/" + app_name + "/UnsafeDir",
                               mode=unsafe_directory_mode)
        return destination

    def raw_ipa(self, destination, entries):
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, value in entries:
                _zip_file(archive, name, value)
        return destination

    def test_app_capability_recomputes_separate_digests_and_redacts_profiles(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        capability = parse_ios_artifact(self.make_app())
        manifest = capability.manifest
        self.assertEqual(manifest["format"], "app")
        self.assertEqual(manifest["applicationId"], "com.example.inventory")
        self.assertRegex(manifest["appDigest"], r"^[0-9a-f]{64}$")
        self.assertRegex(manifest["containerDigest"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(manifest["appDigest"], manifest["containerDigest"])
        self.assertEqual(capability.app_digest, manifest["appDigest"])
        self.assertEqual(capability.container_digest, manifest["containerDigest"])
        self.assertTrue(any(item["path"] == "Inventory" and item["executable"] is True
                            for item in manifest["files"]))
        self.assertTrue(any(item["path"] == "Frameworks/Foo.framework/Versions/A/Foo"
                            and item["executable"] is True
                            for item in manifest["files"]))
        self.assertTrue(any(item["path"] == "Frameworks/Foo.framework/Foo"
                            and item["target"] == "Versions/Current/Foo"
                            for item in manifest["symlinks"]))
        self.assertTrue(any(item["kind"] == "framework"
                            and item["bundlePath"] == "Frameworks/Foo.framework"
                            for item in manifest["codeObjects"]))
        self.assertTrue(any(item["kind"] == "appex"
                            and item["bundlePath"] == "PlugIns/Widget.appex"
                            for item in manifest["codeObjects"]))
        encoded = json.dumps(manifest, sort_keys=True)
        self.assertNotIn("embedded.mobileprovision", encoded)
        self.assertNotIn("dummy root profile", encoded)
        changed = copy.deepcopy(manifest)
        changed["files"].clear()
        self.assertNotEqual(capability.manifest["files"], changed["files"])

    def test_app_and_ipa_have_the_same_app_digest_but_distinct_container_digest(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        app = self.make_flat_app()
        ipa = self.make_ipa(app, self.root / "Flat.ipa")
        app_capability = parse_ios_artifact(app)
        ipa_capability = parse_ios_artifact(ipa)
        self.assertEqual(app_capability.app_digest, ipa_capability.app_digest)
        self.assertNotEqual(app_capability.container_digest, ipa_capability.container_digest)
        self.assertEqual(ipa_capability.container_digest,
                         hashlib.sha256(ipa.read_bytes()).hexdigest())
        self.assertEqual(ipa_capability.manifest["format"], "ipa")
        self.assertEqual(ipa_capability.manifest["containerBytes"], ipa.stat().st_size)

    def test_ipa_explicit_payload_directory_is_accepted_and_empty_directories_are_preserved(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        app = self.make_flat_app()
        (app / "Resources" / "Empty").mkdir(parents=True)
        ipa = self.make_ipa_with_directories(app)
        app_capability = parse_ios_artifact(app)
        ipa_capability = parse_ios_artifact(ipa)
        self.assertEqual(app_capability.app_digest, ipa_capability.app_digest)
        self.assertIn("Resources/Empty", ipa_capability.manifest["directories"])

    def test_ipa_capability_retains_the_source_container_after_private_extraction(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        ipa = self.make_ipa(self.make_flat_app(), self.root / "source.ipa")
        capability = parse_ios_artifact(ipa)
        self.assertEqual(capability._source, ipa)
        self.assertTrue(capability._source.is_file())

    def test_zip_group_world_write_and_setid_modes_are_rejected_before_normalization(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        app = self.make_flat_app()
        for mode in (0o664, 0o4755):
            archive = self.make_ipa_with_directories(
                app, self.root / ("unsafe-%o.ipa" % mode), unsafe_mode=mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(ContractError):
                parse_ios_artifact(archive)
        archive = self.make_ipa_with_directories(
            app, self.root / "unsafe-directory.ipa", unsafe_directory_mode=0o775)
        with self.assertRaises(ContractError):
            parse_ios_artifact(archive)

    def test_ipa_honors_the_code_object_limit(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        archive = self.make_ipa(self.make_app(profiles=False), self.root / "limited.ipa")
        with self.assertRaises(ContractError):
            parse_ios_artifact(archive, max_code_objects=2)

    def test_ipa_entry_count_is_rejected_before_zipfile_materializes_info_objects(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        archive = self.make_ipa(self.make_flat_app(), self.root / "too-many.ipa")
        with patch("reproloop.ios_artifact_transfer.zipfile.ZipFile",
                   side_effect=AssertionError("ZipFile must not initialize")):
            with self.assertRaises(ContractError):
                parse_ios_artifact(archive, max_entries=2)

    def test_ipa_malformed_and_multidisk_end_records_are_rejected(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        source = self.make_ipa(self.make_flat_app(), self.root / "valid.ipa")
        valid = bytearray(source.read_bytes())
        end = valid.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(end, 0)
        multidisk = self.root / "multidisk.ipa"
        value = bytearray(valid)
        value[end + 4:end + 6] = (1).to_bytes(2, "little")
        multidisk.write_bytes(value)
        malformed = self.root / "malformed.ipa"
        value = bytearray(valid)
        value[end + 16:end + 20] = (0xFFFFFFFF).to_bytes(4, "little")
        malformed.write_bytes(value)
        for archive in (multidisk, malformed):
            with self.subTest(archive=archive.name), self.assertRaises(ContractError):
                parse_ios_artifact(archive)

    def test_ipa_rejects_zip_symlink_before_extracting_it(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        ipa = self.make_ipa(self.make_flat_app(), self.root / "linked.ipa", add_symlink=True)
        with self.assertRaises(ContractError):
            parse_ios_artifact(ipa)
        self.assertFalse((self.root / "outside").exists())

    def test_ipa_requires_one_payload_app_and_rejects_traversal_or_extra_roots(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        app = self.make_flat_app()
        info = (app / "Info.plist").read_bytes()
        executable = (app / "Flat").read_bytes()
        two_apps = self.raw_ipa(self.root / "two.ipa", [
            ("Payload/One.app/Info.plist", info),
            ("Payload/One.app/Flat", executable),
            ("Payload/Two.app/Info.plist", info),
        ])
        traversal = self.raw_ipa(self.root / "traversal.ipa", [
            ("Payload/Flat.app/Info.plist", info),
            ("Payload/Flat.app/Flat", executable),
            ("Payload/Flat.app/../outside", b"escaped"),
        ])
        extra_root = self.raw_ipa(self.root / "extra.ipa", [
            ("Payload/Flat.app/Info.plist", info),
            ("Payload/Flat.app/Flat", executable),
            ("SwiftSupport/iphoneos/libswiftCore.dylib", b"unsupported"),
        ])
        for archive in (two_apps, traversal, extra_root):
            with self.subTest(archive=archive.name), self.assertRaises(ContractError):
                parse_ios_artifact(archive)

    def test_embedded_profiles_are_allowed_only_at_app_or_appex_root(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        app = self.make_flat_app()
        _write(app / "Resources" / "fake.mobileprovision", b"private dummy")
        with self.assertRaises(ContractError):
            parse_ios_artifact(app)

        app = self.make_app()
        framework = app / "Frameworks" / "Foo.framework"
        _write(framework / "embedded.mobileprovision", b"invalid framework profile")
        with self.assertRaises(ContractError):
            parse_ios_artifact(app)

    def test_accepts_a_bounded_fat_macho_without_executing_it(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        app = self.make_flat_app()
        slice_bytes = MACHO64_ARM64
        table_end = 8 + 20
        fat = struct.pack(">II", 0xCAFEBABE, 1)
        fat += struct.pack(">IIIII", 0x0100000C, 0, table_end, len(slice_bytes), 0)
        _write(app / "Flat", fat + slice_bytes, executable=True)
        capability = parse_ios_artifact(app)
        self.assertEqual(capability.manifest["applicationId"], "com.example.flat")

    def test_rejects_hardlinks_case_nfc_collisions_and_bad_links(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        cases = []
        hardlinked = self.make_flat_app()
        os.link(hardlinked / "Flat", hardlinked / "HardAlias")
        cases.append(hardlinked)

        external = self.make_flat_app()
        (external / "External").symlink_to(self.root / "outside")
        cases.append(external)

        dangling = self.make_flat_app()
        (dangling / "Dangling").symlink_to("missing")
        cases.append(dangling)

        cycle = self.make_flat_app()
        (cycle / "One").symlink_to("Two")
        (cycle / "Two").symlink_to("One")
        cases.append(cycle)

        case_collision = self.make_flat_app()
        _write(case_collision / "info.plist", b"collision")
        cases.append(case_collision)

        nfc_collision = self.make_flat_app()
        _write(nfc_collision / "cafe\u0301", b"decomposed")
        _write(nfc_collision / "caf\u00e9", b"composed")
        cases.append(nfc_collision)

        for app in cases:
            with self.subTest(app=app.name):
                with self.assertRaises(ContractError):
                    parse_ios_artifact(app)

    def test_rejects_unsupported_nested_code_bad_macho_and_nonregular_entries(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        unsupported = self.make_flat_app()
        _write(unsupported / "Extras" / "Bad.appex" / "Info.plist",
               _app_info("com.example.bad", "Bad"))
        with self.assertRaises(ContractError):
            parse_ios_artifact(unsupported)

        dylib = self.make_flat_app()
        _write(dylib / "Frameworks" / "unexpected.dylib", b"not allowed")
        with self.assertRaises(ContractError):
            parse_ios_artifact(dylib)

        bad_binary = self.make_flat_app()
        _write(bad_binary / "Flat", b"not a Mach-O", executable=True)
        with self.assertRaises(ContractError):
            parse_ios_artifact(bad_binary)

        fifo = self.make_flat_app()
        os.mkfifo(fifo / "pipe")
        with self.assertRaises(ContractError):
            parse_ios_artifact(fifo)

    def test_file_entry_and_code_object_limits_are_bounded(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        app = self.make_app()
        with self.assertRaises(ContractError):
            parse_ios_artifact(app, max_bytes=1)
        with self.assertRaises(ContractError):
            parse_ios_artifact(app, max_entries=2)
        with self.assertRaises(ContractError):
            parse_ios_artifact(app, max_code_objects=2)

    def test_capability_is_parser_issued_and_manifest_is_a_copy(self):
        from reproloop.ios_artifact_transfer import (
            IosBundleCapability,
            parse_ios_artifact,
            require_ios_artifact,
        )

        capability = parse_ios_artifact(self.make_flat_app())
        self.assertIs(type(capability), IosBundleCapability)
        require_ios_artifact(capability)
        manifest = capability.manifest
        manifest["files"].clear()
        self.assertTrue(capability.manifest["files"])
        forged = replace(capability, _issuer=object())
        with self.assertRaises(ContractError):
            require_ios_artifact(forged)
        tampered = replace(capability, _app_digest="0" * 64)
        with self.assertRaises(ContractError):
            require_ios_artifact(tampered)
        with self.assertRaises(ContractError):
            require_ios_artifact(replace(capability))
        with self.assertRaises(ContractError):
            require_ios_artifact(replace(capability, _source=self.root / "other.app"))
        changed_manifest = capability.manifest
        changed_manifest["containerBytes"] += 1
        changed_json = json.dumps(changed_manifest, sort_keys=True,
                                  separators=(",", ":"), ensure_ascii=False)
        with self.assertRaises(ContractError):
            require_ios_artifact(replace(
                capability,
                _manifest_json=changed_json,
                _app_digest=changed_manifest["appDigest"],
                _container_digest=changed_manifest["containerDigest"],
                _container_bytes=changed_manifest["containerBytes"],
            ))
        identifier = id(capability)
        reference = weakref.ref(capability)
        del capability
        gc.collect()
        from reproloop import ios_artifact_transfer
        with ios_artifact_transfer._ISSUED_CAPABILITIES_LOCK:
            self.assertIsNone(reference())
            self.assertNotIn(identifier, ios_artifact_transfer._ISSUED_CAPABILITIES)

    def test_relative_file_open_pins_parent_directories_and_never_blocks_on_fifo(self):
        from reproloop.ios_artifact_transfer import _open_relative_file

        app = self.make_flat_app()
        nested = app / "Nested"
        _write(nested / "value", b"owned")
        descriptor = _open_relative_file(app, "Nested/value")
        try:
            self.assertEqual(os.read(descriptor, 32), b"owned")
        finally:
            os.close(descriptor)

        outside = self.root / "outside"
        outside.mkdir()
        _write(outside / "value", b"escaped")
        (nested / "value").unlink()
        nested.rmdir()
        nested.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ContractError):
            _open_relative_file(app, "Nested/value")

        nested.unlink()
        nested.mkdir()
        os.mkfifo(nested / "pipe")
        with self.assertRaises(ContractError):
            _open_relative_file(app, "Nested/pipe")

    def test_tree_iteration_stops_at_the_first_entry_over_the_global_budget(self):
        from reproloop.ios_artifact_transfer import parse_ios_artifact

        app = self.make_flat_app()
        original_scandir = os.scandir

        class BoundedIterator:
            def __init__(self, iterator, maximum):
                self.iterator = iterator
                self.maximum = maximum
                self.seen = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.iterator.__exit__(*args)

            def __iter__(self):
                return self

            def __next__(self):
                if self.seen >= self.maximum:
                    raise AssertionError("directory iterator was over-consumed")
                self.seen += 1
                return next(self.iterator)

        def bounded_scandir(directory):
            return BoundedIterator(original_scandir(directory), 3)

        with patch("reproloop.ios_artifact_transfer.os.scandir",
                   side_effect=bounded_scandir):
            with self.assertRaises(ContractError):
                parse_ios_artifact(app, max_entries=2)


if __name__ == "__main__":
    unittest.main()
