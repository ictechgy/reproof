"""Bounded, inert issue archives. Content checks are separate from authority.

An archive contains canonical original/specification/lifecycle documents and a
closed graph of evidence objects. It carries qualification *provenance*, never
project registrations, runtime policies, credentials or execution handles.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sqlite3
import struct
import subprocess
import threading
import time
import uuid
import zipfile
import zlib

from . import contracts
from .app_logs import APP_LOG_MIME
from .contracts.versions import exact, validate_version, bounded_list
from .storage import _unique_object
from .live.video import VIDEO_MANIFEST_MIME, validate_video_manifest, _BoundedReader
from .live.evidence_store import (
    EvidenceStore, EvidenceStoreError, OBJECT_METADATA_BYTES, _fsync_directory,
)
from .live.disk_budget import DiskBudgetError


class PackageError(contracts.ContractError):
    def __init__(self, code="package_invalid", message="Issue package is invalid"):
        super().__init__(message)
        self.code = code


def _require(condition, code="package_invalid", message="Issue package is invalid"):
    if not condition:
        raise PackageError(code, message)


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PackageError() from None


def _json(body):
    try:
        return json.loads(body, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError, contracts.ContractError):
        raise PackageError() from None


MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 320 * 1024 * 1024
MAX_PACKAGE_OBJECT_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_JSON_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_OBJECTS = 2048


@dataclass(frozen=True, slots=True)
class PackageLimits:
    max_archive_bytes: int = MAX_ARCHIVE_BYTES
    max_expanded_bytes: int = MAX_EXPANDED_BYTES
    max_object_bytes: int = MAX_PACKAGE_OBJECT_BYTES
    max_json_bytes: int = MAX_PACKAGE_JSON_BYTES
    max_objects: int = MAX_PACKAGE_OBJECTS

    def __post_init__(self):
        bounds = {"max_archive_bytes": MAX_ARCHIVE_BYTES,
                  "max_expanded_bytes": MAX_EXPANDED_BYTES,
                  "max_object_bytes": MAX_PACKAGE_OBJECT_BYTES,
                  "max_json_bytes": MAX_PACKAGE_JSON_BYTES,
                  "max_objects": MAX_PACKAGE_OBJECTS}
        for name, ceiling in bounds.items():
            _require(type(getattr(self, name)) is int
                     and 1 <= getattr(self, name) <= ceiling)


DEFAULT_LIMITS = PackageLimits()
_OBJECT_PATH = re.compile(r"objects/[0-9a-f]{64}\Z")
_JSON_MIME_TYPES = {"application/json", VIDEO_MANIFEST_MIME, APP_LOG_MIME}
_MIME_TYPES = _JSON_MIME_TYPES | {"image/png", "image/jpeg", "video/mp4"}


@dataclass(frozen=True, slots=True)
class PackageContents:
    index: dict
    recording: dict
    specification: dict
    qualification: dict | None
    video: dict | None


def _directory(source, limits):
    """Bound the central directory before ZipFile allocates its entry list.

    The portable format deliberately excludes ZIP64, comments, descriptors,
    executable prefixes and appended data. Export always uses this subset.
    """
    source.seek(0, io.SEEK_END)
    size = source.tell()
    _require(22 <= size <= limits.max_archive_bytes, "package_size")
    source.seek(size - 22)
    trailer = source.read(22)
    _require(len(trailer) == 22)
    signature, disk, start_disk, count_disk, count, cd_size, cd_offset, comment = (
        struct.unpack("<4s4H2LH", trailer))
    _require(signature == b"PK\x05\x06" and disk == start_disk == comment == 0
             and count_disk == count and 1 <= count <= limits.max_objects + 1
             and cd_size <= (limits.max_objects + 1) * 256
             and cd_offset + cd_size == size - 22)
    source.seek(0)
    archive = zipfile.ZipFile(source, "r")
    try:
        entries = archive.infolist()
        _require(len(entries) == count and not archive.comment)
        seen = set()
        expanded = 0
        end = 0
        for info in entries:
            name = info.filename
            _require(name == "manifest.json" or _OBJECT_PATH.fullmatch(name) is not None)
            _require(name.casefold() not in seen, "package_duplicate")
            seen.add(name.casefold())
            mode = info.external_attr >> 16
            _require(stat.S_IFMT(mode) in (0, stat.S_IFREG)
                     and not info.is_dir() and not (info.external_attr & 0x10)
                     and not info.extra and not info.comment)
            _require(info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                     and info.flag_bits & ~0x800 == 0)
            maximum = limits.max_json_bytes if name == "manifest.json" else limits.max_object_bytes
            _require(0 < info.file_size <= maximum and info.compress_size > 0,
                     "package_size")
            expanded += info.file_size
            _require(expanded <= limits.max_expanded_bytes, "package_size")
            _require(info.header_offset == end)
            source.seek(info.header_offset)
            header = source.read(30)
            _require(len(header) == 30)
            local = struct.unpack("<4s5H3L2H", header)
            _require(local[0] == b"PK\x03\x04" and local[2] == info.flag_bits
                     and local[3] == info.compress_type and local[6] == info.CRC
                     and local[7] == info.compress_size and local[8] == info.file_size
                     and local[9] == len(name.encode("ascii")) and local[10] == 0)
            _require(source.read(local[9]) == name.encode("ascii"))
            end = info.header_offset + 30 + local[9] + info.compress_size
            _require(end <= cd_offset)
        _require(end == cd_offset and "manifest.json" in seen)
        return archive
    except Exception:
        archive.close()
        raise


def _read(archive, path, limit):
    with archive.open(path) as stream:
        body = stream.read(limit + 1)
        _require(0 < len(body) <= limit, "package_size")
        return body


def _validate_documents(index, objects, limits, media_validator, validate_media, checkpoint):
    exact(index, ("schemaVersion", "format", "manifest", "recordingStatus",
                            "lifecycleDigest", "qualificationDigest", "objects"))
    validate_version(index["schemaVersion"])
    _require(index["format"] == "reproloop-issue")
    manifest = contracts.validate_package_manifest(index["manifest"])
    _require(index["recordingStatus"] in ("frozen-complete", "frozen-incomplete"))
    contracts.validate_digest(index["lifecycleDigest"])
    if index["qualificationDigest"] is not None:
        contracts.validate_digest(index["qualificationDigest"])
    descriptors = {}
    for item in bounded_list(index["objects"], "objects", limits.max_objects, minimum=1):
        exact(item, ("digest", "bytes", "mimeType"))
        contracts.validate_digest(item["digest"])
        _require(item["digest"] not in descriptors, "package_duplicate")
        contracts.bounded_int(item["bytes"], "object bytes", 1, limits.max_object_bytes)
        _require(item["mimeType"] in _MIME_TYPES)
        descriptors[item["digest"]] = item
    _require(set(descriptors) == set(manifest["objects"]) == set(objects))
    documents = {}
    for digest, body in objects.items():
        checkpoint()
        item = descriptors[digest]
        _require(len(body) == item["bytes"] and hashlib.sha256(body).hexdigest() == digest,
                 "package_checksum")
        if item["mimeType"] in _JSON_MIME_TYPES:
            _require(len(body) <= limits.max_json_bytes, "package_size")
            documents[digest] = _json(body)

    def document(digest, mime="application/json"):
        _require(digest in documents and descriptors[digest]["mimeType"] == mime)
        value = documents[digest]
        _require(canonical(value) == objects[digest], "package_canonical")
        return value

    original = contracts.validate_original_evidence(document(manifest["recordingDigest"]))
    specification = contracts.validate_specification(document(manifest["specificationDigest"]))
    _require(specification["originalRecordingDigest"] == manifest["recordingDigest"])
    lifecycle = document(index["lifecycleDigest"])
    bounded_list(lifecycle, "lifecycle receipts", 10000)
    receipt_ids = set()
    for sequence, item in enumerate(lifecycle, 1):
        contracts.validate_lifecycle_receipt(item)
        _require(item["recordingDigest"] == manifest["recordingDigest"]
                 and item["sequence"] == sequence and item["receiptId"] not in receipt_ids
                 and item["observedAtMs"] >= original["startedAtMs"])
        receipt_ids.add(item["receiptId"])
    required = {manifest["recordingDigest"], manifest["specificationDigest"], index["lifecycleDigest"]}
    qualification = None
    if index["qualificationDigest"] is not None:
        qualification = contracts.validate_qualification(document(index["qualificationDigest"]))
        _require(qualification["recordingDigest"] == manifest["recordingDigest"]
                 and qualification["specificationDigest"] == manifest["specificationDigest"]
                 and qualification["projectRevision"] == original["projectRevision"]
                 and qualification["originalBuildId"] == original["buildId"])
        required.add(index["qualificationDigest"])

    def require_reference(reference):
        digest = reference["digest"]
        _require(digest in descriptors and descriptors[digest]["bytes"] == reference["bytes"]
                 and descriptors[digest]["mimeType"] == reference["mimeType"])
        required.add(digest)

    for reference in original["observations"]:
        _require(reference["mimeType"] in {"application/json", APP_LOG_MIME})
        require_reference(reference)
    video = None
    segments = {}
    for reference in original["media"]:
        require_reference(reference)
        if reference["mimeType"] in _JSON_MIME_TYPES:
            value = document(reference["digest"], reference["mimeType"])
            _require(video is None)
            video = validate_video_manifest(value)
            _require(video["recordingId"] == original["recordingId"])
            for segment in video["segments"]:
                require_reference(segment)
                segments[segment["digest"]] = segment
                if video["schemaVersion"] == 1:
                    for frame in segment["frames"]:
                        digest = frame["sourceDigest"]
                        _require(digest in descriptors
                                 and descriptors[digest]["mimeType"] in ("image/png", "image/jpeg"))
                        required.add(digest)
    _require(required == set(descriptors), "package_unlisted")
    for digest, item in descriptors.items():
        checkpoint()
        mime = item["mimeType"]
        if mime in _JSON_MIME_TYPES:
            continue
        if mime == "video/mp4":
            _require(digest in segments, "package_media")
        if not validate_media:
            continue
        _require(callable(media_validator), "media_validator_unavailable",
                 "A local media decoder is required")
        report = media_validator(objects[digest], mime)
        checkpoint()
        _require(type(report) is dict, "package_media")
        width, height = report.get("width"), report.get("height")
        _require(type(width) is int and type(height) is int
                 and 1 <= width <= 4096 and 1 <= height <= 4096
                 and width * height <= 4194304, "package_media")
        if mime == "video/mp4":
            segment = segments[digest]
            times = report.get("presentationTimesMs")
            _require(report.get("codec") == "h264"
                     and [width, height, report.get("frameCount")]
                     == [segment["width"], segment["height"], segment["frameCount"]]
                     and type(times) is list and len(times) == segment["frameCount"],
                     "package_media")
            for actual, frame in zip(times, segment["frames"]):
                _require(type(actual) in (int, float) and math.isfinite(actual)
                         and abs(actual - frame["presentationTimeMs"]) <= 1,
                         "package_media")
        else:
            _require(report.get("mimeType") == mime, "package_media")
    return PackageContents(index, {"original": original,
        "recordingDigest": manifest["recordingDigest"], "status": index["recordingStatus"],
        "lifecycleReceipts": lifecycle}, specification, qualification, video)


def inspect_archive(source, *, limits=DEFAULT_LIMITS, media_validator=None, validate_media=True,
                    checkpoint=lambda: None):
    """Validate the entire archive before returning inert, displayable content.

    ``validate_media=False`` is reserved for a previously validated local CAS
    object after its archive digest has been rechecked; never use it on import.
    """
    try:
        with _directory(source, limits) as archive:
            checkpoint()
            index = _json(_read(archive, "manifest.json", limits.max_json_bytes))
            _require(type(index) is dict and type(index.get("objects")) is list)
            _require(1 <= len(index["objects"]) <= limits.max_objects)
            objects = {}
            for item in index["objects"]:
                checkpoint()
                _require(type(item) is dict)
                digest = contracts.validate_digest(item.get("digest"))
                _require(digest not in objects, "package_duplicate")
                objects[digest] = _read(archive, "objects/" + digest, limits.max_object_bytes)
            _require(set(archive.namelist()) == {"manifest.json", *("objects/" + d for d in objects)})
            return _validate_documents(index, objects, limits, media_validator, validate_media, checkpoint)
    except PackageError:
        raise
    except (contracts.ContractError, OSError, ValueError, TypeError, KeyError,
            OverflowError, UnicodeError, RecursionError, struct.error, zipfile.BadZipFile,
            NotImplementedError, RuntimeError, zlib.error):
        raise PackageError() from None


def build_archive(recording, specification, read_object, *, qualification=None,
                  package_id=None, limits=DEFAULT_LIMITS, media_validator=None,
                  checkpoint=lambda: None):
    """Build a closed archive from selected revisions and trusted object reads.

    The caller pins all source objects and applies its current project policy
    and G2 reservation for the complete operation, including publication.
    """
    try:
        _require(type(recording) is dict and recording.get("original") is not None)
        original = contracts.validate_original_evidence(recording["original"])
        specification = contracts.validate_specification(specification)
        original_digest = contracts.digest(original)
        _require(original_digest == recording["recordingDigest"]
                 == specification["originalRecordingDigest"])
        objects, descriptors = {}, {}
        total = 0

        def add(body, mime):
            nonlocal total
            _require(type(body) is bytes and 0 < len(body) <= limits.max_object_bytes, "package_size")
            digest = hashlib.sha256(body).hexdigest()
            if digest in objects:
                _require(descriptors[digest]["mimeType"] == mime)
            else:
                total += len(body)
                _require(total <= limits.max_expanded_bytes
                         and len(objects) < limits.max_objects, "package_size")
                objects[digest] = body
                descriptors[digest] = {"digest": digest, "bytes": len(body), "mimeType": mime}
            return digest

        add(canonical(original), "application/json")
        specification_digest = add(canonical(specification), "application/json")
        lifecycle_digest = add(canonical(recording.get("lifecycleReceipts", [])), "application/json")
        qualification_digest = None
        if qualification is not None:
            qualification_digest = add(canonical(qualification), "application/json")
        pending = list(original["media"]) + list(original["observations"])
        seen = set()
        while pending:
            checkpoint()
            reference = pending.pop(0)
            digest = reference["digest"]
            if digest in seen:
                _require(descriptors[digest]["bytes"] == reference["bytes"]
                         and descriptors[digest]["mimeType"] == reference["mimeType"])
                continue
            seen.add(digest)
            body = read_object(digest)
            _require(type(body) is bytes and len(body) == reference["bytes"])
            _require(add(body, reference["mimeType"]) == digest, "package_checksum")
            if reference["mimeType"] in _JSON_MIME_TYPES:
                value = _json(body)
                if type(value) is dict and value.get("kind") == "reproloop-avfoundation-video":
                    pending.extend(validate_video_manifest(value)["segments"])
        index = {"schemaVersion": 1, "format": "reproloop-issue", "manifest": {
            "schemaVersion": 1, "packageId": package_id or "package_" + uuid.uuid4().hex,
            "recordingDigest": original_digest, "specificationDigest": specification_digest,
            "objects": sorted(objects)}, "recordingStatus": recording.get("status"),
            "lifecycleDigest": lifecycle_digest, "qualificationDigest": qualification_digest,
            "objects": [descriptors[digest] for digest in sorted(descriptors)]}
        _validate_documents(index, objects, limits, media_validator, True, checkpoint)
        manifest_body = canonical(index)
        _require(len(manifest_body) <= limits.max_json_bytes, "package_size")
        expected = (22 + 30 + 46 + len("manifest.json") * 2 + len(manifest_body)
                    + sum(30 + 46 + len("objects/" + d) * 2 + len(b) for d, b in objects.items()))
        _require(expected <= limits.max_archive_bytes, "package_size")
        result = io.BytesIO()
        with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            for path, body in [("manifest.json", manifest_body),
                               *(("objects/" + d, objects[d]) for d in sorted(objects))]:
                info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, body)
        body = result.getvalue()
        _require(len(body) == expected, "package_size")
        return body
    except PackageError:
        raise
    except (contracts.ContractError, OSError, ValueError, TypeError, KeyError,
            OverflowError, RecursionError):
        raise PackageError() from None


class PackageReader:
    """A pinned, bounded archive read; authorization is checked for every chunk."""
    def __init__(self, body, digest, guard):
        self.body = body
        self.digest = digest
        self.bytes = len(body)
        self._guard = guard
        self.closed = False

    def chunks(self, *, start=0, end=None, block_size=64 * 1024):
        end = self.bytes - 1 if end is None else end
        _require(type(start) is int and type(end) is int and 0 <= start <= end < self.bytes)
        _require(type(block_size) is int and 1 <= block_size <= 1024 * 1024)
        for position in range(start, end + 1, block_size):
            _require(not self.closed, "package_closed")
            self._guard()
            yield self.body[position:min(position + block_size, end + 1)]

    def close(self):
        self.closed = True
        self.body = b""

    def object(self, digest, *, limits=DEFAULT_LIMITS):
        contracts.validate_digest(digest)
        _require(not self.closed, 'package_closed')
        self._guard()
        with zipfile.ZipFile(io.BytesIO(self.body)) as archive:
            index=_json(_read(archive,'manifest.json',limits.max_json_bytes))
            item=next((item for item in index['objects'] if item['digest']==digest),None)
            _require(item is not None,'package_not_found')
            body=_read(archive,'objects/'+digest,limits.max_object_bytes)
            _require(hashlib.sha256(body).hexdigest()==digest,'package_checksum')
            self._guard()
            return {'body':body,'mimeType':item['mimeType'],'digest':digest}


class NativeMediaValidator:
    """Run the fixed local decoder against one G2-charged staging object."""
    def __init__(self, evidence, helper, owner):
        _require(type(evidence) is EvidenceStore, "package_configuration")
        self.evidence = evidence
        self.helper = Path(helper)
        _require(self.helper.is_file() and not self.helper.is_symlink()
                 and os.access(self.helper, os.X_OK)
                 and self.helper.stat().st_size <= 32 * 1024 * 1024,
                 "media_validator_unavailable")
        self.helper_digest = hashlib.sha256(self.helper.read_bytes()).hexdigest()
        self.owner = owner

    def __call__(self, body, mime):
        _require(type(body) is bytes and 0 < len(body) <= 32 * 1024 * 1024
                 and mime in ("image/png", "image/jpeg", "video/mp4"), "package_media")
        _require(self.helper.is_file() and not self.helper.is_symlink()
                 and self.helper.stat().st_size <= 32 * 1024 * 1024
                 and hashlib.sha256(self.helper.read_bytes()).hexdigest() == self.helper_digest,
                 "media_validator_changed")
        writer = self.evidence.begin_blob(hashlib.sha256(body).hexdigest(), len(body),
            owner=self.owner, retention_class="intermediate",
            retain_until_ms=int(time.time() * 1000) + 60000)
        process = None
        readers = []
        try:
            writer.write(body)
            writer.flush()
            process = subprocess.Popen([str(self.helper.resolve()), str(writer.path.resolve()), mime],
                cwd=self.evidence.root, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
                start_new_session=True)
            readers = [_BoundedReader(process.stdout, 16 * 1024),
                       _BoundedReader(process.stderr, 16 * 1024)]
            for reader in readers:
                reader.start()
            deadline = time.monotonic() + 20
            while process.poll() is None:
                _require(time.monotonic() < deadline, "media_validation_timeout")
                _require(not any(reader.exceeded.is_set() for reader in readers), "package_media")
                time.sleep(.01)
            for reader in readers:
                reader.join(.5)
            _require(process.returncode == 0
                     and not any(reader.is_alive() or reader.exceeded.is_set() for reader in readers),
                     "package_media", "Issue media could not be decoded")
            result = _json(bytes(readers[0].body))
            _require(type(result) is dict, "package_media")
            return result
        except OSError:
            raise PackageError("media_validator_unavailable") from None
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                for reader in readers:
                    reader.join(.5)
                process.stdout.close()
                process.stderr.close()
            writer.abort()


class IssuePackageStore:
    """Project-bound publication index over G2's charged, immutable CAS.

    One operation at a time bounds retained archive memory and pins. The index
    has a lifetime process lock. A stable, store-specific pin identity permits
    recovery of only this store's pins and uncommitted staging after a crash.
    Authorization callbacks come from the coordinator, never an archive.
    """
    def __init__(self, root, evidence, *, media_validator=None, media_helper=None, limits=DEFAULT_LIMITS,
                 now_ms=None):
        _require(type(evidence) is EvidenceStore, "package_configuration")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        properties = self.root.lstat()
        _require(self.root.is_dir() and not self.root.is_symlink()
                 and properties.st_uid == os.getuid(), "package_configuration")
        self.evidence = evidence
        self.budget = evidence.budget
        self.archive_evidence = None
        self.limits = limits
        self.media_validator = media_validator
        _require(media_validator is None or media_helper is None, "package_configuration")
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.storage_owner = "package_" + hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()
        self._pin_id = self.storage_owner
        self._lock = threading.RLock()
        self._closed = False
        self._db = None
        self._fd = os.open(self.root / ".owner.lock",
                           os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PackageError("package_store_busy", "Issue package store is already open") from None
            database = self.root / "packages.sqlite3"
            for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
                _require(not path.is_symlink() and (not path.exists() or path.is_file()),
                         "package_configuration")
            self._base = self.budget.reserve(self.storage_owner, "journal", 128 * 1024,
                                             idempotency_key=self.storage_owner + "_schema")
            self._base.commit()
            self._db = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS packages(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, project_digest TEXT NOT NULL,
                    archive_digest TEXT NOT NULL, archive_bytes INTEGER NOT NULL,
                    mode TEXT NOT NULL, expires_ms INTEGER NOT NULL, state TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL, reservation_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS packages_project ON packages(project_id);
            """)
            version = self._db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()
            if version is None:
                self._db.execute("INSERT INTO metadata VALUES('version',1)")
            else:
                _require(version[0] == 1, "package_store_version")
            # Archives have a separate bounded CAS namespace.  Its object
            # ceiling follows the archive limit so a 256 MiB video package
            # can be staged without widening the recording/artifact CAS.
            # EvidenceStore permits this migration only with the explicit
            # opt-in below, under its initialization lock.
            self.archive_evidence = EvidenceStore(
                self.root / "archive-objects", self.budget,
                max_object_bytes=self.limits.max_archive_bytes,
                _allow_object_limit_growth=True,
            )
            self.archive_evidence.unpin_id(self._pin_id)
            self.evidence.unpin_id(self._pin_id)
            self.archive_evidence.abandon_owner(self.storage_owner)
            # This namespace belongs exclusively to the locked package store.
            # It also covers a crash after file creation but before G2 records
            # the staging association. Never sweep the recording CAS here.
            entries = list(self.archive_evidence.staging.iterdir())
            _require(len(entries) <= 128, "package_recovery")
            for path in entries:
                _require(path.is_file() and not path.is_symlink()
                         and re.fullmatch(r"staging_[0-9a-f]{32}\.part", path.name),
                         "package_recovery")
                _require(self.archive_evidence._remove_file(path), "package_recovery")
            _fsync_directory(self.archive_evidence.staging)
            self.archive_evidence.abandon_owner(self.storage_owner)
            for category in ("spool", "transfer"):
                for reservation in self.budget.reservations_for_owner(self.storage_owner, category=category):
                    self.archive_evidence.release_unused_reservation(reservation["reservation_id"])
            used = {row[0] for row in self._db.execute("SELECT reservation_id FROM packages")}
            used.add(self._base.reservation_id)
            for reservation in self.budget.reservations_for_owner(self.storage_owner, category="journal"):
                if reservation["reservation_id"] not in used:
                    self.budget.release_id(reservation["reservation_id"])
            if media_helper is not None:
                self.media_validator = NativeMediaValidator(
                    self.archive_evidence, media_helper, self.storage_owner)
        except Exception:
            if self.archive_evidence is not None:
                self.archive_evidence.close()
            if self._db is not None:
                self._db.close()
            os.close(self._fd)
            self._fd = None
            raise

    @contextmanager
    def _operation(self):
        _require(self._lock.acquire(timeout=10), "package_busy", "Another package operation is still running")
        try:
            _require(not self._closed, "package_closed")
            yield
        except (EvidenceStoreError, DiskBudgetError):
            raise PackageError("package_storage", "Issue package storage is unavailable") from None
        finally:
            self._lock.release()

    def _authorized(self, authorize):
        _require(callable(authorize) and authorize() is True, "authorization_revoked",
                 "Issue package authorization is unavailable")

    def _checkpoint(self, authorize, expires_at_ms, deadline):
        self._authorized(authorize)
        _require(type(expires_at_ms) is int and self._now_ms() < expires_at_ms,
                 "package_expired", "Issue package retention has expired")
        _require(time.monotonic() < deadline, "package_timeout", "Issue package operation timed out")

    def _check_dependencies(self, dependencies):
        for digest in dependencies["tombstones"]:
            _require(not self.evidence.is_tombstoned(digest), "package_removed")
        for digest in dependencies["local"]:
            reference = self.evidence.lookup(digest)
            _require(reference is not None and reference.retain_until_ms > self._now_ms(),
                     "package_expired")

    def _row(self, package_id, project_id, authorize):
        self._authorized(authorize)
        contracts.validate_id(package_id)
        contracts.validate_id(project_id)
        row = self._db.execute("SELECT * FROM packages WHERE id=? AND project_id=?",
                               (package_id, project_id)).fetchone()
        _require(row is not None and row["state"] == "published", "package_not_found")
        _require(row["expires_ms"] > self._now_ms(), "package_expired")
        self._check_dependencies(_json(row["dependencies_json"]))
        reference = self.archive_evidence.lookup(row["archive_digest"])
        _require(reference is not None and reference.bytes == row["archive_bytes"]
                 and reference.retain_until_ms > self._now_ms(), "package_unavailable")
        return row

    @staticmethod
    def _public(row):
        return {"id": row["id"], "projectId": row["project_id"],
                "projectDigest": row["project_digest"], "archiveDigest": row["archive_digest"],
                "bytes": row["archive_bytes"], "mode": row["mode"],
                "expiresAtMs": row["expires_ms"]}

    def _import(self, source, *, size, digest, project_id, project_digest, expires_at_ms,
                authorize, mode, local_sources=(), reservation=None, already_validated=None,
                deadline=None):
        try:
            contracts.validate_id(project_id)
            contracts.validate_digest(project_digest)
            contracts.validate_digest(digest)
            _require(type(size) is int and 1 <= size <= self.limits.max_archive_bytes, "package_size")
            _require(mode in ("imported", "local"))
            deadline = deadline or time.monotonic() + 60
            checkpoint = lambda: self._checkpoint(authorize, expires_at_ms, deadline)
            checkpoint()
            _require(self._db.execute("SELECT COUNT(*) FROM packages").fetchone()[0] < 10000,
                     "package_limit")
        except Exception:
            if reservation is not None:
                reservation.close()
            raise
        writer = self.archive_evidence.begin_blob(digest, size, owner=self.storage_owner,
            retention_class="export", retain_until_ms=expires_at_ms, reservation=reservation)
        metadata = None
        published = False
        try:
            remaining = size
            while remaining:
                checkpoint()
                # The HTTP adapter provides deadline-bounded read1, avoiding a
                # caller-controlled slow read inside this storage transaction.
                block = source.read(min(64 * 1024, remaining))
                _require(type(block) is bytes and 0 < len(block) <= remaining, "package_truncated")
                writer.write(block)
                remaining -= len(block)
            writer.flush()
            checkpoint()
            if already_validated is None:
                with writer.path.open("rb") as stream:
                    contents = inspect_archive(stream, limits=self.limits,
                        media_validator=self.media_validator, checkpoint=checkpoint)
            else:
                contents = already_validated
            dependencies = {"tombstones": contents.index["manifest"]["objects"],
                            "local": sorted(set(local_sources))}
            dependencies_body = canonical(dependencies).decode()
            self._check_dependencies(dependencies)
            metadata = self.budget.reserve(self.storage_owner, "journal",
                                           8192 + len(dependencies_body.encode()))
            metadata.commit()
            checkpoint()
            reference = writer.publish()
            with self.archive_evidence.pin(reference.digest, self._pin_id, "export"):
                checkpoint()
                self._check_dependencies(dependencies)
                package_id = "package_" + uuid.uuid4().hex
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    checkpoint()
                    self._check_dependencies(dependencies)
                    _require(not self.archive_evidence.is_tombstoned(reference.digest), "package_removed")
                    self._db.execute("INSERT INTO packages VALUES(?,?,?,?,?,?,?,?,?,?)", (
                        package_id, project_id, project_digest, reference.digest, reference.bytes,
                        mode, expires_at_ms, "published", dependencies_body, metadata.reservation_id))
                    self._db.commit()
                    published = True
                except Exception:
                    self._db.rollback()
                    raise
                return self._public(self._row(package_id, project_id, authorize))
        finally:
            if not writer._closed:
                writer.abort()
            if metadata is not None and not published:
                metadata.close()

    def import_archive(self, source, *, size, digest, project_id, project_digest,
                       expires_at_ms, authorize):
        """Publish validated content under a fresh local ID, with no approval."""
        with self._operation():
            return self._import(source, size=size, digest=digest, project_id=project_id,
                project_digest=project_digest, expires_at_ms=expires_at_ms,
                authorize=authorize, mode="imported")

    def create(self, recording, specification, *, project_id, project_digest,
               expires_at_ms, authorize, qualification=None):
        with self._operation(), ExitStack() as pins:
            deadline = time.monotonic() + 60
            checkpoint = lambda: self._checkpoint(authorize, expires_at_ms, deadline)
            checkpoint()
            original = recording.get("original") if type(recording) is dict else None
            _require(type(original) is dict and contracts.digest(original) == recording.get("recordingDigest"))
            pinned = set()
            retention = expires_at_ms

            def read_object(digest):
                nonlocal retention
                checkpoint()
                if digest not in pinned:
                    pins.enter_context(self.evidence.pin(digest, self._pin_id, "export"))
                    pinned.add(digest)
                reference = self.evidence.lookup(digest)
                _require(reference is not None and reference.retain_until_ms > self._now_ms(),
                         "package_expired")
                retention = min(retention, reference.retain_until_ms)
                return self.evidence.read(digest)

            _require(read_object(recording["recordingDigest"]) == canonical(original), "package_checksum")
            references = original.get("media", []) + original.get("observations", [])
            estimated = (128 * 1024 + len(canonical(original)) + len(canonical(specification))
                         + len(canonical(recording.get("lifecycleReceipts", [])))
                         + len(canonical(qualification))
                         + sum(item["bytes"] + 512 for item in references))
            reserved = min(estimated, self.limits.max_archive_bytes) + OBJECT_METADATA_BYTES
            reservation = self.budget.reserve(self.storage_owner, "transfer", reserved)
            associated = False
            try:
                body = build_archive(recording, specification, read_object, qualification=qualification,
                    limits=self.limits, media_validator=self.media_validator, checkpoint=checkpoint)
                _require(len(body) + OBJECT_METADATA_BYTES <= reserved, "package_size")
                checkpoint()
                contents = inspect_archive(io.BytesIO(body), limits=self.limits,
                    validate_media=False, checkpoint=checkpoint)
                # From this point G2 owns cleanup and its reservation. Do not
                # release it early if the staging unlink fails.
                associated = True
                return self._import(io.BytesIO(body), size=len(body), digest=hashlib.sha256(body).hexdigest(),
                    project_id=project_id, project_digest=project_digest, expires_at_ms=retention,
                    authorize=authorize, mode="local", local_sources=pinned,
                    reservation=reservation, already_validated=contents, deadline=deadline)
            finally:
                if not associated:
                    reservation.close()

    def revise(self, package_id, project_id, specification, *, authorize, qualification=None):
        """Assemble a new revision from pinned original bytes, preserving expiry."""
        with self.open_archive(package_id, project_id, authorize=authorize) as reader:
            row = self._row(package_id, project_id, authorize)
            contents = inspect_archive(io.BytesIO(reader.body), limits=self.limits, validate_media=False)
            if contents.specification == specification and contents.qualification == qualification:
                return self._public(row)
            deadline = time.monotonic() + 60
            checkpoint = lambda: self._checkpoint(authorize, row["expires_ms"], deadline)
            reserved = min(self.limits.max_archive_bytes, len(reader.body) + 128 * 1024
                           + len(canonical(specification)) + len(canonical(qualification))) + OBJECT_METADATA_BYTES
            reservation = self.budget.reserve(self.storage_owner, "transfer", reserved)
            associated = False
            try:
                body = build_archive(contents.recording, specification,
                    lambda digest: reader.object(digest, limits=self.limits)["body"],
                    qualification=qualification, limits=self.limits,
                    media_validator=self.media_validator, checkpoint=checkpoint)
                _require(len(body) + OBJECT_METADATA_BYTES <= reserved, "package_size")
                revised = inspect_archive(io.BytesIO(body), limits=self.limits, validate_media=False,
                                          checkpoint=checkpoint)
                self._row(package_id, project_id, authorize)
                associated = True
                return self._import(io.BytesIO(body), size=len(body), digest=hashlib.sha256(body).hexdigest(),
                    project_id=project_id, project_digest=row["project_digest"], expires_at_ms=row["expires_ms"],
                    authorize=authorize, mode=row["mode"], reservation=reservation, deadline=deadline,
                    local_sources=_json(row["dependencies_json"])["local"], already_validated=revised)
            finally:
                if not associated:
                    reservation.close()

    def withdraw_created(self, publication):
        """Trusted caller rollback after its enclosing issue publication fails."""
        with self._operation():
            row = self._db.execute("SELECT * FROM packages WHERE id=?", (publication["id"],)).fetchone()
            _require(row is not None and self._public(row) == publication, "package_publication_changed")
            self._db.execute("UPDATE packages SET state='tombstoned' WHERE id=?", (row["id"],))

    def apply_retention(self):
        # A long export must not stall the workflow authorization monitor.
        # The dedicated maintenance loop retries skipped passes.
        if not self._lock.acquire(blocking=False):
            return []
        try:
            if self._closed:
                return []
            now = self._now_ms()
            self._db.execute("UPDATE packages SET state='expired' WHERE id IN "
                "(SELECT id FROM packages WHERE state='published' AND expires_ms<=? LIMIT 64)", (now,))
            return self.archive_evidence.apply_retention(now_ms=now, limit=64)
        finally:
            self._lock.release()

    @contextmanager
    def open_archive(self, package_id, project_id, *, authorize):
        with self._operation():
            row = self._row(package_id, project_id, authorize)
            with self.archive_evidence.pin(row["archive_digest"], self._pin_id, "export"):
                body = self.archive_evidence.read(row["archive_digest"])
                guard = lambda: self._row(package_id, project_id, authorize)
                guard()
                reader = PackageReader(body, row["archive_digest"], guard)
                try:
                    yield reader
                finally:
                    reader.close()

    def get(self, package_id, project_id, *, authorize):
        with self.open_archive(package_id, project_id, authorize=authorize) as reader:
            contents = inspect_archive(io.BytesIO(reader.body), limits=self.limits,
                                       validate_media=False)
            row = self._row(package_id, project_id, authorize)
            return {"package": self._public(row), "recording": contents.recording,
                    "specification": contents.specification, "qualificationProvenance": contents.qualification,
                    "video": contents.video}

    def read_object(self, package_id, project_id, digest, *, authorize):
        contracts.validate_digest(digest)
        with self.open_archive(package_id, project_id, authorize=authorize) as reader:
            return reader.object(digest,limits=self.limits)

    def list(self, project_id, *, authorize):
        with self._operation():
            self._authorized(authorize)
            contracts.validate_id(project_id)
            result = []
            for row in self._db.execute("SELECT id FROM packages WHERE project_id=? AND state='published'",
                                        (project_id,)).fetchall():
                try:
                    result.append(self._public(self._row(row[0], project_id, authorize)))
                except PackageError as error:
                    if error.code == "authorization_revoked":
                        raise
            return result

    def tombstone(self, package_id, project_id, *, authorize):
        with self._operation():
            row = self._row(package_id, project_id, authorize)
            self._db.execute("UPDATE packages SET state='tombstoned' WHERE id=?", (row["id"],))
            # Identical archives may belong to another project. Local removal
            # does not destroy another project's copy or release its charge.

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._db is not None:
                self._db.close()
                self._db = None
            if self.archive_evidence is not None:
                self.archive_evidence.close()
                self.archive_evidence = None
            if self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
                self._fd = None
