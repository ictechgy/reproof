"""Package/archive capacity boundaries and the shared client transfer fences."""
from __future__ import annotations

import email.message
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from reproof.issue_package import (
    DEFAULT_LIMITS,
    IssuePackageStore,
    PackageError,
    PackageLimits,
)
from reproof.live.client import IssueClient
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import (
    MAX_OBJECT_BYTES,
    MAX_STAGING,
    EvidenceStore,
    EvidenceStoreError,
)
from reproof.live.model import LiveError


MIB = 1024 * 1024
ARCHIVE_CEILING_BYTES = 256 * MIB
SHARED_JSON_RESPONSE_BYTES = 16 * MIB
JSON_REQUEST_BYTES = 5 * MIB


class _Response:
    def __init__(self, body: bytes):
        self.body = body
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "application/json"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit=-1):
        return self.body


class EvidenceStoreCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reproof-package-capacity-")
        self.root = Path(self.temp.name)
        self.budget = DiskBudget(
            self.root / "budget", capacity_bytes=96 * MIB,
            journal_headroom_bytes=64 * 1024,
            free_bytes=lambda: 1 << 40,
        )
        self.stores = []
        self.packages = None

    def tearDown(self):
        if self.packages is not None:
            self.packages.close()
        for store in reversed(self.stores):
            store.close()
        self.budget.close()
        self.temp.cleanup()

    def _store(self, path="evidence", **kwargs):
        store = EvidenceStore(self.root / path, self.budget, **kwargs)
        self.stores.append(store)
        return store

    def test_growth_requires_opt_in_and_only_changes_the_object_ceiling(self):
        legacy = self._store()
        body = b"legacy-object-bytes"
        reference = legacy.put_bytes(
            body, owner="legacy_owner", retention_class="original", retain_until_ms=5000,
        )
        legacy.close()
        self.stores.remove(legacy)

        with self.assertRaises(EvidenceStoreError):
            self._store(max_object_bytes=ARCHIVE_CEILING_BYTES)

        grown = self._store(
            max_object_bytes=ARCHIVE_CEILING_BYTES,
            _allow_object_limit_growth=True,
        )
        self.assertEqual(grown.max_object_bytes, ARCHIVE_CEILING_BYTES)
        self.assertEqual(grown.read(reference.digest), body)
        metadata = dict(grown._connection.execute("SELECT key, value FROM metadata"))
        self.assertEqual(metadata["format_version"], 1)
        self.assertEqual(metadata["max_object_bytes"], ARCHIVE_CEILING_BYTES)
        self.assertEqual(metadata["max_objects"], grown.max_objects)
        self.assertEqual(metadata["max_staging"], MAX_STAGING)

    def test_growth_rejects_downgrade_and_any_other_metadata_change(self):
        legacy = self._store()
        legacy.close()
        self.stores.remove(legacy)

        with self.assertRaises(EvidenceStoreError):
            self._store(max_object_bytes=ARCHIVE_CEILING_BYTES,
                        max_objects=legacy.max_objects - 1,
                        _allow_object_limit_growth=True)
        with self.assertRaises(EvidenceStoreError):
            self._store(max_object_bytes=ARCHIVE_CEILING_BYTES,
                        max_staging=MAX_STAGING - 1,
                        _allow_object_limit_growth=True)
        with self.assertRaises(EvidenceStoreError):
            self._store(max_object_bytes=MAX_OBJECT_BYTES // 2,
                        _allow_object_limit_growth=True)

    def test_package_archive_namespace_accepts_a_real_boundary_object(self):
        evidence = self._store("recording-evidence")
        packages_root = self.root / "packages"
        legacy_archive = EvidenceStore(packages_root / "archive-objects", self.budget)
        legacy_body = b"legacy-archive-object"
        legacy_reference = legacy_archive.put_bytes(
            legacy_body, owner="legacy_archive_owner", retention_class="export",
            retain_until_ms=5000,
        )
        legacy_archive.close()

        packages = IssuePackageStore(packages_root, evidence,
                                     now_ms=lambda: 1000)
        self.packages = packages

        self.assertEqual(packages.archive_evidence.max_object_bytes, ARCHIVE_CEILING_BYTES)
        self.assertEqual(evidence.max_object_bytes, MAX_OBJECT_BYTES)
        self.assertEqual(packages.archive_evidence.read(legacy_reference.digest), legacy_body)

        # One actual 64 MiB object plus a byte is enough to distinguish the
        # old archive ceiling while avoiding a collection of large fixtures.
        body = b"v" * (64 * MIB + 1)
        reference = packages.archive_evidence.put_bytes(
            body, owner=packages.storage_owner, retention_class="export",
            retain_until_ms=5000,
        )
        self.assertEqual(reference.bytes, len(body))
        hasher = hashlib.sha256()
        with (packages.archive_evidence.root / reference.path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
        self.assertEqual(hasher.hexdigest(), reference.digest)


class PackageClientCapacityTests(unittest.TestCase):
    def test_package_and_profile_limits_are_the_shared_bounded_defaults(self):
        self.assertEqual(DEFAULT_LIMITS.max_archive_bytes, ARCHIVE_CEILING_BYTES)
        self.assertEqual(DEFAULT_LIMITS.max_expanded_bytes, 320 * MIB)
        self.assertEqual(DEFAULT_LIMITS.max_object_bytes, 32 * MIB)
        self.assertEqual(DEFAULT_LIMITS.max_json_bytes, 8 * MIB)
        self.assertEqual(DEFAULT_LIMITS.max_objects, 2048)
        with self.assertRaises(PackageError):
            PackageLimits(max_archive_bytes=ARCHIVE_CEILING_BYTES + 1)
        with self.assertRaises(PackageError):
            PackageLimits(max_expanded_bytes=320 * MIB + 1)

    def test_shared_json_response_allows_v2_sized_manifest_but_stays_bounded(self):
        client = IssueClient.__new__(IssueClient)
        value_bytes = SHARED_JSON_RESPONSE_BYTES - len(b'{"value":"') - len(b'"}')
        accepted = b'{"value":"' + b"x" * value_bytes + b'"}'
        client._request = mock.Mock(return_value=_Response(accepted))
        value = client.call("/api/release/issues/issue_1", {})
        self.assertEqual(len(value["value"]), value_bytes)
        self.assertEqual(value["value"][:4], "xxxx")

        oversized = _Response(accepted + b"x")
        client._request = mock.Mock(return_value=oversized)
        with self.assertRaises(LiveError):
            client.call("/api/release/issues/issue_1", {})

        with self.assertRaises(LiveError):
            client.call("/api/release/issues/issue_1", "x" * JSON_REQUEST_BYTES)

    def test_package_import_and_download_reject_only_above_the_shared_archive_bound(self):
        with tempfile.TemporaryDirectory(prefix="reproof-package-client-") as directory:
            source = Path(directory) / "oversized.zip"
            with source.open("wb") as stream:
                stream.truncate(ARCHIVE_CEILING_BYTES + 1)
            client = IssueClient.__new__(IssueClient)
            with self.assertRaises(LiveError):
                client.import_package("project_1", source)

            package = {
                "projectId": "project_1", "id": "package_1",
                "archiveDigest": hashlib.sha256(b"package").hexdigest(),
                "bytes": ARCHIVE_CEILING_BYTES + 1,
            }
            with self.assertRaises(LiveError):
                client.download_package(package, Path(directory) / "output.zip")


if __name__ == "__main__":
    unittest.main()
