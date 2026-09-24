"""Malformed owned archives must be bounded before ZIP object allocation."""
import io
import os
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from reproof.core import ContractError
from reproof import ios_artifact_transfer as transfer
from tests.test_ios_artifact_transfer import MACHO64_ARM64, _app_info, _zip_file


class IosArchiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="repro-ios-zip-boundary-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def archive(self, name="source.ipa"):
        destination = self.root / name
        with zipfile.ZipFile(destination, "w") as archive:
            _zip_file(archive, "Payload/Owned.app/Info.plist", plistlib.dumps(
                _app_info("com.example.owned", "Owned")))
            _zip_file(archive, "Payload/Owned.app/Owned", MACHO64_ARM64,
                      executable=True)
            _zip_file(archive, "Payload/Owned.app/first.txt", b"first")
            _zip_file(archive, "Payload/Owned.app/second.txt", b"second")
        destination.chmod(0o600)
        return destination

    def assert_rejected_before_zip_allocation(self, archive, **limits):
        with patch.object(transfer.zipfile, "ZipFile", side_effect=AssertionError(
                "Unbounded central directory reached ZipFile allocation")):
            with self.assertRaises(ContractError):
                transfer.parse_ios_artifact(archive, **limits)

    def test_forged_low_entry_count_does_not_bypass_allocation_limit(self):
        archive = self.archive()
        value = bytearray(archive.read_bytes())
        end = value.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", value, end + 8, 1, 1)
        archive.write_bytes(value)
        self.assert_rejected_before_zip_allocation(archive, max_entries=2)

    def test_gap_cannot_make_zipfile_parse_a_different_central_directory(self):
        archive = self.archive()
        value = archive.read_bytes()
        end = value.rfind(b"PK\x05\x06")
        archive.write_bytes(value[:end] + b"unexpected padding" + value[end:])
        self.assert_rejected_before_zip_allocation(archive)

    def test_implicit_directory_budget_is_checked_before_extracting_files(self):
        archive=self.root/'deep.ipa'
        with zipfile.ZipFile(archive,'w') as output:
            _zip_file(output,'Payload/Owned.app/Info.plist',plistlib.dumps(_app_info('com.example.owned','Owned')))
            _zip_file(output,'Payload/Owned.app/Owned',MACHO64_ARM64,executable=True)
            _zip_file(output,'Payload/Owned.app/a/b/c/first.txt',b'owned')
        archive.chmod(0o600)
        with patch.object(transfer,'_create_relative_file',side_effect=AssertionError('Over-budget tree reached extraction')):
            with self.assertRaises(ContractError):transfer.parse_ios_artifact(archive,max_entries=3)

    def test_implicit_parent_case_and_file_collisions_are_rejected_before_extraction(self):
        for first,second in (('Upper/one.txt','upper/two.txt'),('parent','parent/child.txt')):
            with self.subTest(first=first,second=second):
                archive=self.root/'collision.ipa'
                with zipfile.ZipFile(archive,'w') as output:
                    _zip_file(output,'Payload/Owned.app/Info.plist',plistlib.dumps(_app_info('com.example.owned','Owned')))
                    _zip_file(output,'Payload/Owned.app/Owned',MACHO64_ARM64,executable=True)
                    _zip_file(output,'Payload/Owned.app/'+first,b'first')
                    _zip_file(output,'Payload/Owned.app/'+second,b'second')
                archive.chmod(0o600)
                with patch.object(transfer,'_create_relative_file',side_effect=AssertionError('Ambiguous parent reached extraction')):
                    with self.assertRaises(ContractError):transfer.parse_ios_artifact(archive)

    def test_source_replaced_after_preflight_is_not_followed_by_zipfile(self):
        archive = self.archive()
        other = self.archive("other.ipa")
        preflight = transfer._zip_preflight
        native_open = io.open

        def swap_after_preflight(*args, **kwargs):
            result = preflight(*args, **kwargs)
            archive.rename(self.root / "saved.ipa")
            archive.symlink_to(other)
            return result

        def checked_open(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)) and Path(path) == archive:
                raise AssertionError("ZIP parser followed a replaced archive path")
            return native_open(path, *args, **kwargs)

        with patch.object(transfer, "_zip_preflight", side_effect=swap_after_preflight), \
                patch.object(io, "open", side_effect=checked_open):
            with self.assertRaises(ContractError):
                transfer.parse_ios_artifact(archive)

    def test_fixed_zip64_end_records_preserve_the_app_and_container_digests(self):
        original = self.archive()
        plain = transfer.parse_ios_artifact(original)
        value = original.read_bytes()
        end = value.rfind(b"PK\x05\x06")
        fields = list(struct.unpack("<4s4H2IH", value[end:]))
        zip64 = struct.pack("<4sQ2H2I4Q", b"PK\x06\x06", 44, 45, 45,
                            0, 0, fields[3], fields[4], fields[5], fields[6])
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, end, 1)
        fields[3:5] = [0xFFFF, 0xFFFF]
        fields[5:7] = [0xFFFFFFFF, 0xFFFFFFFF]
        converted = self.root / "zip64.ipa"
        converted.write_bytes(value[:end] + zip64 + locator + struct.pack(
            "<4s4H2IH", *fields))
        converted.chmod(0o600)
        parsed = transfer.parse_ios_artifact(converted)
        self.assertEqual(parsed.app_digest, plain.app_digest)
        self.assertNotEqual(parsed.container_digest, plain.container_digest)

    def test_in_place_producer_write_cannot_change_the_metadata_zipfile_reads(self):
        archive = self.archive()
        preflight = transfer._zip_preflight
        native_zipfile = zipfile.ZipFile
        parsed_counts = []

        def mutate_after_preflight(*args, **kwargs):
            result = preflight(*args, **kwargs)
            value = bytearray(archive.read_bytes())
            end = value.rfind(b"PK\x05\x06")
            struct.pack_into("<I", value, end + 12, 0xFFFFFFFE)
            archive.write_bytes(value)
            return result

        def inspect_private_archive(*args, **kwargs):
            result = native_zipfile(*args, **kwargs)
            parsed_counts.append(len(result.infolist()))
            return result

        with patch.object(transfer, "_zip_preflight", side_effect=mutate_after_preflight), \
                patch.object(transfer.zipfile, "ZipFile", side_effect=inspect_private_archive):
            with self.assertRaises(ContractError):
                transfer.parse_ios_artifact(archive)
        self.assertEqual(parsed_counts, [4])


if __name__ == "__main__":
    unittest.main()
