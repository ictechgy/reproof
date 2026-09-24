"""Issue archive invariants; imported bytes never confer execution authority."""
import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock
import warnings
import zipfile

from reproof import contracts
from reproof.issue_package import (
    PackageError, PackageLimits, build_archive, inspect_archive, IssuePackageStore,
)
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore


FIXTURES = Path(__file__).parent / "fixtures" / "release"


def example():
    original = json.loads((FIXTURES / "evidence.json").read_text())
    original.update(media=[], observations=[])
    specification = json.loads((FIXTURES / "scenario.json").read_text())
    specification["originalRecordingDigest"] = contracts.digest(original)
    recording = {"original": original, "recordingDigest": contracts.digest(original),
                 "status": "frozen-complete", "lifecycleReceipts": []}
    return recording, specification


def rewrite(body, transform):
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        entries = [(item, archive.read(item)) for item in archive.infolist()]
    entries = transform(entries)
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        for info, data in entries:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
                archive.writestr(info, data)
    return result.getvalue()


class IssueArchiveTests(unittest.TestCase):
    def test_g3_vendor_manifest_survives_package_round_trip(self):
        from tests.test_video_state_machine import VideoStateMachineTests
        fixture = VideoStateMachineTests('runTest'); fixture.setUp()
        try:
            sink, session = fixture.make_sink()
            recording = session.stop()  # Actual G3 no-frame manifest; no fake media is encoded.
            _, spec = example()
            spec['originalRecordingDigest'] = recording['recordingDigest']
            body = build_archive(recording, spec, fixture.evidence.read)
            received = inspect_archive(io.BytesIO(body))
            self.assertEqual(received.video['recordingId'], recording['original']['recordingId'])
            self.assertEqual(received.video['status'], 'incomplete')
            self.assertEqual(received.recording['original'], recording['original'])
        finally: fixture.tearDown()

    def archive(self, **kwargs):
        recording, specification = example()
        return build_archive(recording, specification, lambda digest: self.fail(digest),
                             **kwargs)

    def test_round_trip_preserves_original_specification_and_lifecycle(self):
        recording, specification = example()
        receipt = {"schemaVersion": 1, "receiptId": "cleanup_one",
                   "recordingDigest": recording["recordingDigest"],
                   "operationId": "cleanup_operation", "generation": 1,
                   "sequence": 1, "kind": "cleanup", "status": "complete",
                   "observedAtMs": recording["original"]["startedAtMs"] + 5000}
        recording["lifecycleReceipts"] = [receipt]
        body = build_archive(recording, specification, lambda _: None)
        parsed = inspect_archive(io.BytesIO(body))
        self.assertEqual(parsed.recording, recording)
        self.assertEqual(parsed.specification, specification)
        self.assertIsNone(parsed.qualification)
        self.assertFalse(hasattr(parsed, "execution"))
        self.assertFalse(hasattr(parsed, "approved"))

    def test_unknown_original_remains_unknown_and_incomplete(self):
        recording, specification = example()
        recording["original"]["unknowns"] = [{"kind": "preparation_unknown", "sequence": 0}]
        recording["status"] = "frozen-incomplete"
        recording["recordingDigest"] = contracts.digest(recording["original"])
        specification["originalRecordingDigest"] = recording["recordingDigest"]
        parsed = inspect_archive(io.BytesIO(build_archive(recording, specification, lambda _: None)))
        self.assertEqual(parsed.recording, recording)

    def test_qualification_is_bound_provenance_only(self):
        recording, specification = example()
        qualification = json.loads((FIXTURES / "qualification.json").read_text())
        qualification.update(recordingDigest=recording["recordingDigest"],
                             specificationDigest=contracts.digest(specification))
        body = build_archive(recording, specification, lambda _: None,
                             qualification=qualification)
        self.assertEqual(inspect_archive(io.BytesIO(body)).qualification, qualification)
        qualification["recordingDigest"] = "f" * 64
        with self.assertRaises(PackageError):
            build_archive(recording, specification, lambda _: None,
                          qualification=qualification)

    def test_authority_and_secret_variable_fields_are_rejected(self):
        recording, specification = example()
        for key in ("approval", "runtimePolicy", "variableValues", "authorization"):
            invalid = copy.deepcopy(specification)
            invalid[key] = {"synthetic": "not-a-credential"}
            with self.subTest(key=key), self.assertRaises(PackageError):
                build_archive(recording, invalid, lambda _: None)

    def test_original_and_specification_binding_cannot_change(self):
        recording, specification = example()
        recording["original"]["buildId"] = "changed"
        with self.assertRaises(PackageError):
            build_archive(recording, specification, lambda _: None)

    def test_rejects_traversal_absolute_drive_and_case_colliding_paths(self):
        body = self.archive()
        for name in ("../escape", "/escape", "C:/escape", "objects\\escape",
                     "MANIFEST.JSON", "objects/" + "A" * 64):
            changed = rewrite(body, lambda entries: entries + [(zipfile.ZipInfo(name), b"x")])
            with self.subTest(name=name), self.assertRaises(PackageError):
                inspect_archive(io.BytesIO(changed))

    def test_rejects_symlinks_directories_and_special_entries(self):
        body = self.archive()
        for mode in (stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO, stat.S_IFSOCK):
            def change(entries):
                info, data = entries[0]
                info.external_attr = (mode | 0o600) << 16
                return [(info, data), *entries[1:]]
            with self.subTest(mode=mode), self.assertRaises(PackageError):
                inspect_archive(io.BytesIO(rewrite(body, change)))

    def test_rejects_duplicate_missing_unlisted_and_corrupt_objects(self):
        body = self.archive()
        mutations = [lambda items: items + [items[-1]], lambda items: items[:-1],
                     lambda items: items + [(zipfile.ZipInfo("objects/" + "f" * 64), b"x")],
                     lambda items: [*items[:-1], (items[-1][0], b"{}")] ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(PackageError):
                inspect_archive(io.BytesIO(rewrite(body, mutate)))

    def test_rejects_unknown_schema_duplicate_json_keys_and_unbounded_expansion(self):
        body = self.archive()
        def edit_index(entries, edit):
            result = []
            for info, value in entries:
                if info.filename == "manifest.json":
                    value = edit(value)
                result.append((info, value))
            return result
        edits = [lambda _: b'{"schemaVersion":1,"schemaVersion":1}',
                 lambda b: b.replace(b'"schemaVersion":1', b'"schemaVersion":999', 1),
                 lambda b: b.replace(b'"schemaVersion":1', b'"schemaVersion":true', 1)]
        for edit in edits:
            with self.subTest(edit=edit), self.assertRaises(PackageError):
                inspect_archive(io.BytesIO(rewrite(body, lambda e: edit_index(e, edit))))
        with self.assertRaises(PackageError):
            inspect_archive(io.BytesIO(body), limits=PackageLimits(max_expanded_bytes=512))

    def test_rejects_unsupported_compression_and_trailing_or_prepended_bytes(self):
        body = self.archive()
        def compress(entries):
            for info, _ in entries:
                info.compress_type = zipfile.ZIP_BZIP2
            return entries
        for changed in (rewrite(body, compress), body + b"trailing", b"prefix" + body, body[:-10]):
            with self.subTest(size=len(changed)), self.assertRaises(PackageError):
                inspect_archive(io.BytesIO(changed))

    def test_malformed_images_require_a_real_decoder(self):
        recording, specification = example()
        image = b"\x89PNG\r\n\x1a\nnot-an-image"
        digest = hashlib.sha256(image).hexdigest()
        recording["original"]["media"] = [{"id": "frame_one", "digest": digest,
            "path": "objects/sha256/" + digest[:2] + "/" + digest,
            "bytes": len(image), "mimeType": "image/png"}]
        recording["recordingDigest"] = contracts.digest(recording["original"])
        specification["originalRecordingDigest"] = recording["recordingDigest"]
        with self.assertRaises(PackageError):
            build_archive(recording, specification, lambda _: image)


class IssuePackageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.budget = DiskBudget(self.root / "budget", capacity_bytes=16 * 1024 * 1024,
                                 journal_headroom_bytes=64 * 1024)
        self.evidence = EvidenceStore(self.root / "evidence", self.budget)
        self.now = int(time.time() * 1000)
        self.store = IssuePackageStore(self.root / "packages", self.evidence,
                                        now_ms=lambda: self.now)
        self.recording, self.specification = example()
        self.body = build_archive(self.recording, self.specification, lambda _: None)

    def tearDown(self):
        self.store.close()
        self.evidence.close()
        self.budget.close()
        self.temp.cleanup()

    def put(self, body=None, project="project_one", authorize=lambda: True, **kwargs):
        body = self.body if body is None else body
        return self.store.import_archive(io.BytesIO(body), size=len(body),
            digest=hashlib.sha256(body).hexdigest(), project_id=project,
            project_digest="a" * 64, expires_at_ms=self.now + 60000,
            authorize=authorize, **kwargs)

    def test_import_restart_and_download_preserve_inert_bytes(self):
        record = self.put()
        self.assertEqual(record["mode"], "imported")
        self.assertEqual(len(self.store.list("project_one", authorize=lambda: True)), 1)
        self.store.close()
        self.store = IssuePackageStore(self.root / "packages", self.evidence,
                                        now_ms=lambda: self.now)
        result = self.store.get(record["id"], "project_one", authorize=lambda: True)
        self.assertEqual(result["recording"], self.recording)
        self.assertEqual(result["specification"], self.specification)
        with self.store.open_archive(record["id"], "project_one", authorize=lambda: True) as reader:
            self.assertEqual(b"".join(reader.chunks()), self.body)

    def test_same_digest_in_another_project_is_not_access(self):
        first = self.put()
        other = self.put(project="project_two")
        self.assertNotEqual(first["id"], other["id"])
        self.assertEqual(first["archiveDigest"], other["archiveDigest"])
        with self.assertRaises(PackageError):
            self.store.get(first["id"], "project_two", authorize=lambda: True)
        self.assertEqual(len(self.store.list("project_two", authorize=lambda: True)), 1)

    def test_revocation_at_publication_keeps_import_unpublished(self):
        calls = []
        def guard():
            calls.append(1)
            return len(calls) < 3
        with self.assertRaises(PackageError):
            self.put(authorize=guard)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(self.store.list("project_one", authorize=lambda: True), [])

    def test_interrupted_upload_and_invalid_archive_leave_no_visible_package(self):
        with self.assertRaises(PackageError):
            self.store.import_archive(io.BytesIO(self.body[:20]), size=len(self.body),
                digest=hashlib.sha256(self.body).hexdigest(), project_id="project_one",
                project_digest="a" * 64, expires_at_ms=self.now + 60000, authorize=lambda: True)
        with self.assertRaises(PackageError):
            self.put(b"invalid archive")
        self.assertEqual(self.store.list("project_one", authorize=lambda: True), [])
        self.assertFalse(self.store.archive_evidence.has_pending_owner(self.store.storage_owner))

    def test_failed_staging_unlink_remains_charged_until_restart_cleanup(self):
        before = self.budget.snapshot()["chargedBytes"]
        with mock.patch.object(self.store.archive_evidence, "_remove_file", return_value=False):
            with self.assertRaises(PackageError):
                self.put(b"invalid archive")
        self.assertTrue(self.store.archive_evidence.has_pending_owner(self.store.storage_owner))
        self.assertGreater(self.budget.snapshot()["chargedBytes"], before)
        self.assertEqual(self.store.list("project_one", authorize=lambda: True), [])
        self.store.close()
        self.store = IssuePackageStore(self.root / "packages", self.evidence,
                                        now_ms=lambda: self.now)
        self.assertFalse(self.store.archive_evidence.has_pending_owner(self.store.storage_owner))
        self.assertEqual(self.budget.snapshot()["chargedBytes"], before)

    def test_revocation_and_tombstone_stop_active_download(self):
        record = self.put()
        allowed = True
        def guard(): return allowed
        with self.store.open_archive(record["id"], "project_one", authorize=guard) as reader:
            chunks = reader.chunks(block_size=16)
            self.assertTrue(next(chunks))
            allowed = False
            with self.assertRaises(PackageError):
                next(chunks)
        self.store.tombstone(record["id"], "project_one", authorize=lambda: True)
        with self.assertRaises(PackageError):
            self.store.get(record["id"], "project_one", authorize=lambda: True)

    def test_expiry_rejects_retained_bytes_without_reusing_imported_approval(self):
        record = self.put()
        self.now += 60001
        with self.assertRaises(PackageError):
            self.store.get(record["id"], "project_one", authorize=lambda: True)

    def test_retention_reclaims_expired_archive_and_preserves_other_projects(self):
        first = self.put()
        self.now += 30000
        second = self.put(project="project_two")
        before = self.budget.snapshot()["chargedBytes"]
        self.now += 30001
        self.store.apply_retention()
        self.assertEqual(self.store.list("project_one", authorize=lambda: True), [])
        self.assertEqual(self.store.get(second["id"], "project_two", authorize=lambda: True)["recording"], self.recording)
        self.now += 30000
        self.store.apply_retention()
        self.assertTrue(self.store.archive_evidence.is_tombstoned(first["archiveDigest"]))
        self.assertLess(self.budget.snapshot()["chargedBytes"], before)

    def test_revised_import_preserves_original_and_expiry_without_mutating_source(self):
        source = self.put()
        changed = copy.deepcopy(self.specification)
        changed["revision"] += 1
        revised = self.store.revise(source["id"], "project_one", changed, authorize=lambda: True)
        result = self.store.get(revised["id"], "project_one", authorize=lambda: True)
        self.assertEqual(result["recording"], self.recording)
        self.assertEqual(result["specification"], changed)
        self.assertEqual(revised["mode"], "imported")
        self.assertEqual(revised["expiresAtMs"], source["expiresAtMs"])
        self.assertEqual(self.store.get(source["id"], "project_one", authorize=lambda: True)["specification"], self.specification)

    def test_tombstoned_archive_is_hidden_from_list(self):
        source = self.put()
        self.store.archive_evidence.tombstone(source["archiveDigest"], reason="test-removal")
        self.assertEqual(self.store.list("project_one", authorize=lambda: True), [])

    def test_store_ownership_cannot_be_shared_by_another_writer(self):
        with self.assertRaises(PackageError):
            IssuePackageStore(self.root / "packages", self.evidence)

    def test_local_export_pins_original_and_respects_its_retention_and_tombstone(self):
        body = json.dumps(self.recording["original"], sort_keys=True, separators=(",", ":")).encode()
        source = self.evidence.put_bytes(body, owner="local_recording", retention_class="original",
                                         retain_until_ms=self.now + 30000)
        result = self.store.create(self.recording, self.specification, project_id="project_one",
            project_digest="a" * 64, expires_at_ms=self.now + 60000, authorize=lambda: True)
        self.assertEqual(result["mode"], "local")
        self.assertEqual(result["expiresAtMs"], self.now + 30000)
        self.evidence.tombstone(source.digest, reason="test-removal")
        with self.assertRaises(PackageError):
            self.store.get(result["id"], "project_one", authorize=lambda: True)


if __name__ == "__main__":
    unittest.main()
