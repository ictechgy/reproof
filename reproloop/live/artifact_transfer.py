"""Durable, project-associated artifact uploads for enrolled workers.

Transfer object IDs are server allocated.  Staging paths never cross the wire,
and a content digest is not authorization: every status, chunk, finalization,
and read resolves the durable object association before reauthorization.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid
from typing import Any, Callable

from ..core import ContractError
from .disk_budget import DiskBudget, DiskBudgetError
from .evidence_store import EvidencePin, EvidenceStore, EvidenceStoreError
from .model import LiveError


TRANSFER_FORMAT_VERSION = 2
TRANSFER_APPLICATION_ID = 0x52504C36  # RPL6
MAX_CHUNK_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024
MAX_METADATA_FIELDS = 32
MAX_OBJECTS = 100_000
MAX_CHUNKS = 512
ASSOCIATION_METADATA_BYTES = 16 * 1024
CHUNK_METADATA_BYTES = 512
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KINDS = frozenset({"video", "capture", "app-log", "manifest", "application"})
_RETENTION = frozenset({"original", "intermediate", "derivative", "export"})
_STATES = frozenset({
    "uploading", "finalizing", "publication-pending", "published",
    "quarantined", "tombstoned",
})


def _fail(code: str, message: str, status: int = 409):
    raise LiveError(code, message, status)


def _id(value: object, label: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        _fail("invalid_argument", f"Invalid artifact {label}", 400)
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail("invalid_argument", "Invalid artifact digest", 400)
    return value


def _integer(value: object, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        _fail("invalid_argument", f"Invalid artifact {label}", 400)
    return value


def _host(value: object) -> tuple[str, int, str]:
    if (not isinstance(value, tuple) or len(value) != 3):
        _fail("stale_upload", "Artifact host identity is stale", 409)
    host_id = _id(value[0], "host")
    generation = _integer(value[1], "host generation", 1, 2 ** 63 - 1)
    incarnation = _id(value[2], "host incarnation")
    return host_id, generation, incarnation


def _metadata(value: object) -> tuple[dict[str, Any], str]:
    if type(value) is not dict or len(value) > MAX_METADATA_FIELDS:
        _fail("invalid_argument", "Invalid artifact metadata", 400)
    clean: dict[str, Any] = {}
    for key, item in value.items():
        _id(key, "metadata field")
        if any(part in key.lower() for part in ("secret", "token", "password", "cookie", "credential")):
            _fail("invalid_argument", "Sensitive artifact metadata is not accepted", 400)
        if item is not None and type(item) not in {str, int, bool}:
            _fail("invalid_argument", "Invalid artifact metadata value", 400)
        if type(item) is str:
            try:
                valid = len(item.encode("utf-8")) <= 256 and not any(
                    ord(character) < 32 or ord(character) == 127 for character in item)
            except UnicodeError:
                valid = False
            if not valid:
                _fail("invalid_argument", "Invalid artifact metadata value", 400)
        clean[key] = item
    encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        _fail("invalid_argument", "Artifact metadata exceeds its limit", 400)
    return clean, encoded


def _safe_root(root: Path) -> Path:
    try:
        value = Path(root).absolute()
        value.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = value.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) \
                or info.st_uid != os.getuid():
            raise OSError
        os.chmod(value, 0o700)
        return value
    except (OSError, TypeError, ValueError):
        raise ContractError("Unsafe artifact transfer root") from None


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _active_transfer(method):
    @wraps(method)
    def operation(self, *args, **kwargs):
        with self._lifecycle:
            if self._closing:
                _fail("transfer_unavailable", "Artifact transfer is closing", 503)
            self._active_operations += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            with self._lifecycle:
                self._active_operations -= 1
                self._lifecycle.notify_all()
    return operation


def _serialized_transfer(method):
    @wraps(method)
    def operation(self, *args, **kwargs):
        if not self._io_lock.acquire(timeout=10):
            _fail("transfer_busy", "Another artifact operation is still active", 503)
        try:
            return method(self, *args, **kwargs)
        finally:
            self._io_lock.release()
    return _active_transfer(operation)


@dataclass(slots=True)
class ArtifactRead:
    object_id: str
    project_id: str
    digest: str
    size: int
    start: int
    end: int
    body: bytes
    _pin: EvidencePin = field(repr=False)
    _closed: bool = field(default=False, repr=False)

    def close(self):
        if not self._closed:
            self._pin.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ArtifactTransferStore:
    """One service owns transfer recovery; HTTP requests share its journal."""

    def __init__(self, root: Path, budget: DiskBudget, evidence: EvidenceStore, *,
                 object_quota_bytes: int = 64 * 1024 * 1024,
                 project_quota_bytes: int = 512 * 1024 * 1024,
                 host_quota_bytes: int = 1024 * 1024 * 1024,
                 max_chunk_bytes: int = MAX_CHUNK_BYTES,
                 clock_ms=None):
        if type(budget) is not DiskBudget or type(evidence) is not EvidenceStore \
                or evidence.budget is not budget:
            raise ContractError("Artifact transfer requires shared evidence storage")
        self.root = _safe_root(root)
        self._budget_owner = "transfer_" + hashlib.sha256(
            str(self.root).encode("utf-8")).hexdigest()
        self.staging = _safe_root(self.root / "staging")
        self.budget = budget
        self.evidence = evidence
        self.object_quota_bytes = _integer(
            object_quota_bytes, "object quota", 1, evidence.max_object_bytes)
        self.project_quota_bytes = _integer(
            project_quota_bytes, "project quota", self.object_quota_bytes,
            10 * 1024 ** 3)
        self.host_quota_bytes = _integer(
            host_quota_bytes, "host quota", self.object_quota_bytes,
            10 * 1024 ** 3)
        self.max_chunk_bytes = _integer(
            max_chunk_bytes, "chunk limit", 1, MAX_CHUNK_BYTES)
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._lifecycle = threading.Condition()
        self._active_operations = 0
        self._closing = False
        self._closed = False
        lock_fd = os.open(self.root / ".initialize.lock",
                          os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _fail("transfer_store_busy", "Artifact transfer storage has a live owner", 503)
            self.path = self.root / "artifact-transfer.sqlite3"
            if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
                raise ContractError("Unsafe artifact transfer state")
            self._connection = sqlite3.connect(
                self.path, timeout=10, isolation_level=None, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout=10000")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA journal_size_limit=131072")
            self._connection.execute("PRAGMA wal_autocheckpoint=16")
            self._connection.execute("PRAGMA secure_delete=ON")
            self._initialize()
            os.chmod(self.path, 0o600)
            self._reconcile_staging()
            self._process_lock_fd = lock_fd
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            os.close(lock_fd)
            raise

    def _initialize(self):
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS uploads(
                    object_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    host_id TEXT NOT NULL, host_generation INTEGER NOT NULL,
                    host_incarnation TEXT NOT NULL, upload_generation INTEGER NOT NULL,
                    kind TEXT NOT NULL, expected_size INTEGER NOT NULL,
                    expected_digest TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    retention_class TEXT NOT NULL, retain_until_ms INTEGER NOT NULL,
                    reservation_id TEXT NOT NULL, state TEXT NOT NULL,
                    reason TEXT, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
                    project_digest TEXT, collection_policy_digest TEXT
                ) WITHOUT ROWID""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS chunks(
                    object_id TEXT NOT NULL REFERENCES uploads(object_id),
                    offset INTEGER NOT NULL, chunk_size INTEGER NOT NULL,
                    chunk_digest TEXT NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(object_id,offset)
                ) WITHOUT ROWID""")
            expected = {
                "format_version": str(TRANSFER_FORMAT_VERSION),
                "object_quota_bytes": str(self.object_quota_bytes),
                "project_quota_bytes": str(self.project_quota_bytes),
                "host_quota_bytes": str(self.host_quota_bytes),
                "max_chunk_bytes": str(self.max_chunk_bytes),
                "max_chunks": str(MAX_CHUNKS),
                "association_metadata_bytes": str(ASSOCIATION_METADATA_BYTES),
                "chunk_metadata_bytes": str(CHUNK_METADATA_BYTES),
                "budget_owner": self._budget_owner,
            }
            actual = dict(connection.execute("SELECT key,value FROM metadata"))
            if actual and actual != expected:
                raise ContractError("Artifact transfer configuration is incompatible")
            if not actual:
                connection.executemany(
                    "INSERT INTO metadata(key,value) VALUES(?,?)", expected.items())
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if application_id not in {0, TRANSFER_APPLICATION_ID} or user_version not in {0, TRANSFER_FORMAT_VERSION}:
                raise ContractError("Artifact transfer version is incompatible")
            connection.execute(f"PRAGMA application_id={TRANSFER_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={TRANSFER_FORMAT_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._closed:
                _fail("transfer_unavailable", "Artifact transfer is closed", 503)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
                self._connection.commit()
            except Exception:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise

    def _now(self) -> int:
        value = self._clock_ms()
        return _integer(value, "clock", 0, 2 ** 63 - 1)

    def _stage_path(self, object_id: str) -> Path:
        object_id = _id(object_id, "identity")
        return self.staging / (object_id + ".part")

    def _row(self, object_id: str):
        object_id = _id(object_id, "identity")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM uploads WHERE object_id=?", (object_id,)).fetchone()
        if row is None:
            _fail("not_found", "Artifact object is unavailable", 404)
        return row

    @staticmethod
    def _authorize(authorizer: Callable[[str], Any] | None, project_id: str):
        if authorizer is not None:
            try:
                allowed = authorizer(project_id)
            except LiveError:
                raise
            except Exception:
                _fail("unauthorized", "Artifact authorization failed", 401)
            if allowed is not True:
                _fail("unauthorized", "Artifact authorization failed", 401)

    @staticmethod
    def _check_host(row, host_identity):
        host_id, generation, incarnation = _host(host_identity)
        if (row["host_id"], row["host_generation"], row["host_incarnation"]) \
                != (host_id, generation, incarnation):
            _fail("stale_upload", "Artifact upload belongs to a stale host", 409)

    @staticmethod
    def _metadata_charge(size):
        return ASSOCIATION_METADATA_BYTES + min(size, MAX_CHUNKS) * CHUNK_METADATA_BYTES

    def _retain_metadata_charge(self, row):
        # Upload/chunk/tombstone rows outlive the payload staging file. Their
        # reservation remains charged even when payload bytes deduplicate.
        self.budget.commit_id(row["reservation_id"], self._metadata_charge(row["expected_size"]))

    @_active_transfer
    def allocate(self, *, project_id: str, host_identity, kind: str, size: int,
                 digest: str, metadata: object, retention_class: str,
                 retain_until_ms: int, authorizer=None, project_digest=None,
                 collection_policy_digest=None) -> dict[str, Any]:
        project_id = _id(project_id, "project")
        host_id, host_generation, host_incarnation = _host(host_identity)
        if kind not in _KINDS:
            _fail("invalid_argument", "Invalid artifact kind", 400)
        size = _integer(size, "size", 1, self.object_quota_bytes)
        digest = _digest(digest)
        if project_digest is not None or collection_policy_digest is not None:
            project_digest = _digest(project_digest)
            collection_policy_digest = _digest(collection_policy_digest)
        clean_metadata, metadata_json = _metadata(metadata)
        if retention_class not in _RETENTION:
            _fail("invalid_argument", "Invalid artifact retention class", 400)
        retain_until_ms = _integer(retain_until_ms, "retention deadline", 0, 2 ** 63 - 1)
        if retain_until_ms <= self._now():
            _fail("retention_expired", "Artifact retention has expired", 410)
        self._authorize(authorizer, project_id)
        object_id = "artifact_" + uuid.uuid4().hex
        owner = self._budget_owner
        journaled = False
        try:
            reservation = self.budget.reserve(
                owner, "transfer", size + self._metadata_charge(size),
                idempotency_key=object_id)
        except DiskBudgetError:
            _fail("quota_exceeded", "Artifact storage quota is unavailable", 507)
        path = self._stage_path(object_id)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(descriptor)
            _fsync_directory(self.staging)
            now = self._now()
            with self._transaction() as connection:
                count = int(connection.execute(
                    "SELECT COUNT(*) FROM uploads").fetchone()[0])
                if count >= MAX_OBJECTS:
                    _fail("quota_exceeded", "Artifact object quota is exhausted", 507)
                usage = ("SELECT COALESCE(SUM(CASE WHEN state='tombstoned' THEN 0 "
                         "ELSE expected_size END + ? + MIN(expected_size,?) * ?),0) "
                         "FROM uploads WHERE ")
                overhead = (ASSOCIATION_METADATA_BYTES, MAX_CHUNKS, CHUNK_METADATA_BYTES)
                project_total = int(connection.execute(
                    usage + "project_id=?", (*overhead, project_id)).fetchone()[0])
                host_total = int(connection.execute(
                    usage + "host_id=?", (*overhead, host_id)).fetchone()[0])
                association_cost = size + self._metadata_charge(size)
                if project_total + association_cost > self.project_quota_bytes \
                        or host_total + association_cost > self.host_quota_bytes:
                    _fail("quota_exceeded", "Artifact association quota is exhausted", 507)
                connection.execute("""
                    INSERT INTO uploads(
                        object_id,project_id,host_id,host_generation,host_incarnation,
                        upload_generation,kind,expected_size,expected_digest,metadata_json,
                        retention_class,retain_until_ms,reservation_id,state,reason,
                        created_ms,updated_ms,project_digest,collection_policy_digest)
                    VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,'uploading',NULL,?,?,?,?)""",
                    (object_id, project_id, host_id, host_generation, host_incarnation,
                     kind, size, digest, metadata_json, retention_class,
                     retain_until_ms, reservation.reservation_id, now, now,
                     project_digest, collection_policy_digest))
                journaled = True
            self._authorize(authorizer, project_id)
            return self.status(object_id, host_identity=host_identity,
                               authorizer=authorizer)
        except Exception:
            if journaled:
                # Once an association is durable, retain both its staging file
                # and aggregate charge.  A revoked allocation acknowledgement
                # must not create an unaccounted orphan.
                self._quarantine(object_id, "authorization_revoked")
                raise
            removed = False
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink();_fsync_directory(self.staging);removed = True
            except OSError:
                pass
            if removed:
                reservation.close()
            raise

    def _ranges(self, object_id: str) -> list[list[int]]:
        rows = self._connection.execute(
            "SELECT offset,chunk_size FROM chunks WHERE object_id=? AND state='received' ORDER BY offset",
            (object_id,)).fetchall()
        return [[row["offset"], row["offset"] + row["chunk_size"]] for row in rows]

    def status(self, object_id: str, *, host_identity, authorizer=None,
               expected_project_id=None) -> dict[str, Any]:
        row = self._row(object_id)
        if expected_project_id is not None and row["project_id"] != _id(
                expected_project_id, "project"):
            _fail("not_found", "Artifact object is unavailable", 404)
        self._check_host(row, host_identity)
        self._authorize(authorizer, row["project_id"])
        with self._lock:
            current = self._connection.execute(
                "SELECT * FROM uploads WHERE object_id=?", (row["object_id"],)).fetchone()
            ranges = self._ranges(row["object_id"])
        self._check_host(current, host_identity)
        self._authorize(authorizer, current["project_id"])
        return {
            "objectId": current["object_id"], "projectId": current["project_id"],
            "projectDigest": current["project_digest"],
            "collectionPolicyDigest": current["collection_policy_digest"],
            "uploadGeneration": current["upload_generation"], "kind": current["kind"],
            "size": current["expected_size"], "digest": current["expected_digest"],
            "metadata": json.loads(current["metadata_json"]),
            "retentionClass": current["retention_class"],
            "retainUntilMs": current["retain_until_ms"], "state": current["state"],
            "reason": current["reason"], "receivedRanges": ranges,
            "receivedBytes": sum(end - start for start, end in ranges),
        }

    def _require_retained(self, row):
        if row["retain_until_ms"] <= self._now():
            _fail("retention_expired", "Artifact retention has expired", 410)

    @_serialized_transfer
    def put_chunk(self, object_id: str, upload_generation: int, offset: int,
                  body: bytes, chunk_digest: str, *, host_identity,
                  authorizer=None, expected_project_id=None) -> dict[str, Any]:
        object_id = _id(object_id, "identity")
        generation = _integer(upload_generation, "upload generation", 1, 2 ** 63 - 1)
        offset = _integer(offset, "chunk offset", 0, 10 * 1024 ** 3)
        if type(body) is not bytes or not 1 <= len(body) <= self.max_chunk_bytes:
            _fail("invalid_argument", "Invalid artifact chunk", 400)
        chunk_digest = _digest(chunk_digest)
        if not hashlib.sha256(body).hexdigest() == chunk_digest:
            _fail("digest_mismatch", "Artifact chunk digest differs", 409)
        row = self._row(object_id)
        if expected_project_id is not None and row["project_id"] != _id(
                expected_project_id, "project"):
            _fail("not_found", "Artifact object is unavailable", 404)
        self._check_host(row, host_identity)
        self._authorize(authorizer, row["project_id"])
        self._require_retained(row)
        if row["upload_generation"] != generation:
            _fail("stale_upload", "Artifact upload generation is stale", 409)
        if row["state"] != "uploading" or offset + len(body) > row["expected_size"]:
            _fail("upload_inactive", "Artifact upload is not writable", 409)
        repeated = False
        needs_commit = False
        path = self._stage_path(object_id)
        try:
            descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._authorize(authorizer, row["project_id"])
                with self._transaction() as connection:
                    current = connection.execute(
                        "SELECT * FROM uploads WHERE object_id=?", (object_id,)
                    ).fetchone()
                    if current is None or current["state"] != "uploading" \
                            or current["upload_generation"] != generation \
                            or (current["host_id"], current["host_generation"],
                                current["host_incarnation"]) != _host(host_identity):
                        _fail("stale_upload", "Artifact upload changed", 409)
                    overlaps = connection.execute(
                        "SELECT * FROM chunks WHERE object_id=? AND offset<? "
                        "AND offset+chunk_size>?",
                        (object_id, offset + len(body), offset)).fetchall()
                    if overlaps:
                        exact = len(overlaps) == 1 \
                            and overlaps[0]["offset"] == offset \
                            and overlaps[0]["chunk_size"] == len(body) \
                            and overlaps[0]["chunk_digest"] == chunk_digest \
                            and overlaps[0]["state"] in {"writing", "received"}
                        if not exact:
                            _fail("chunk_conflict",
                                  "Artifact chunk overlaps changed bytes", 409)
                        repeated = True
                        needs_commit = overlaps[0]["state"] == "writing"
                    else:
                        chunk_count = connection.execute(
                            "SELECT COUNT(*) FROM chunks WHERE object_id=?", (object_id,)
                        ).fetchone()[0]
                        if chunk_count >= min(row["expected_size"], MAX_CHUNKS):
                            _fail("quota_exceeded", "Artifact chunk count limit is reached", 507)
                        connection.execute(
                            "INSERT INTO chunks(object_id,offset,chunk_size,"
                            "chunk_digest,state) VALUES(?,?,?,?, 'writing')",
                            (object_id, offset, len(body), chunk_digest))
                if repeated:
                    actual = os.pread(descriptor, len(body), offset)
                    if actual != body:
                        self._quarantine(object_id, "staged_chunk_corrupt")
                        _fail("digest_mismatch", "Stored artifact chunk differs", 409)
                else:
                    written = os.pwrite(descriptor, body, offset)
                    if written != len(body):
                        raise OSError
                    os.fsync(descriptor)
                self._authorize(authorizer, row["project_id"])
                self._require_retained(row)
                if not repeated or needs_commit:
                    with self._transaction() as connection:
                        changed = connection.execute(
                            "UPDATE chunks SET state='received' WHERE object_id=? "
                            "AND offset=? AND chunk_size=? AND chunk_digest=? "
                            "AND state='writing'",
                            (object_id, offset, len(body), chunk_digest)).rowcount
                        if changed != 1:
                            current = connection.execute(
                                "SELECT state FROM chunks WHERE object_id=? "
                                "AND offset=? AND chunk_size=? AND chunk_digest=?",
                                (object_id, offset, len(body), chunk_digest)
                            ).fetchone()
                            if current is None or current["state"] != "received":
                                _fail("stale_upload",
                                      "Artifact chunk journal changed", 409)
                        connection.execute(
                            "UPDATE uploads SET updated_ms=? WHERE object_id=?",
                            (self._now(), object_id))
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(descriptor)
        except LiveError:
            raise
        except OSError:
            self._quarantine(object_id, "chunk_write_failed")
            _fail("storage_failure", "Artifact chunk could not be stored", 507)
        result = self.status(object_id, host_identity=host_identity,
                             authorizer=authorizer,
                             expected_project_id=expected_project_id)
        result["repeated"] = repeated
        return result

    def _quarantine(self, object_id: str, reason: str):
        with self._transaction() as connection:
            connection.execute(
                "UPDATE uploads SET state='quarantined',reason=?,updated_ms=? "
                "WHERE object_id=? AND state!='tombstoned'",
                (_id(reason, "failure reason"), self._now(), object_id))

    def _complete_chunks(self, object_id: str, expected_size: int) -> bool:
        ranges = self._ranges(object_id)
        cursor = 0
        for start, end in ranges:
            if start != cursor or end <= start:
                return False
            cursor = end
        return cursor == expected_size

    def _hash_stage(self, path: Path, expected_size: int, authorizer, project_id: str) -> str:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) \
                    or info.st_size != expected_size:
                _fail("digest_mismatch", "Artifact staging size differs", 409)
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                while True:
                    block = handle.read(self.max_chunk_bytes)
                    if not block:
                        break
                    hasher.update(block)
                    self._authorize(authorizer, project_id)
            return hasher.hexdigest()
        except LiveError:
            raise
        except OSError:
            _fail("storage_failure", "Artifact staging could not be read", 507)

    @_serialized_transfer
    def finalize(self, object_id: str, upload_generation: int, *, host_identity,
                 authorizer=None, expected_project_id=None) -> dict[str, Any]:
        object_id = _id(object_id, "identity")
        generation = _integer(upload_generation, "upload generation", 1, 2 ** 63 - 1)
        row = self._row(object_id)
        if expected_project_id is not None and row["project_id"] != _id(
                expected_project_id, "project"):
            _fail("not_found", "Artifact object is unavailable", 404)
        self._check_host(row, host_identity)
        self._authorize(authorizer, row["project_id"])
        self._require_retained(row)
        if row["upload_generation"] != generation:
            _fail("stale_upload", "Artifact upload generation is stale", 409)
        if row["state"] == "published":
            return self.status(object_id, host_identity=host_identity,
                               authorizer=authorizer,
                               expected_project_id=expected_project_id)
        if row["state"] not in {"uploading", "publication-pending"}:
            _fail("upload_inactive", "Artifact upload cannot be finalized", 409)
        path = self._stage_path(object_id)
        reference = self.evidence.lookup(row["expected_digest"])
        if row["state"] == "publication-pending" and reference is not None and not path.exists():
            try:
                self._retain_metadata_charge(row)
            except DiskBudgetError:
                pass
            self._authorize(authorizer, row["project_id"])
            with self._transaction() as connection:
                connection.execute(
                    "UPDATE uploads SET state='published',reason=NULL,updated_ms=? "
                    "WHERE object_id=? AND state='publication-pending'",
                    (self._now(), object_id))
            return self.status(object_id, host_identity=host_identity,
                               authorizer=authorizer,
                               expected_project_id=expected_project_id)
        if not self._complete_chunks(object_id, row["expected_size"]):
            _fail("upload_incomplete", "Artifact upload contains an offset hole", 409)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE uploads SET state='finalizing',reason=NULL,updated_ms=? "
                "WHERE object_id=? AND state IN ('uploading','publication-pending')",
                (self._now(), object_id)).rowcount
            if changed != 1:
                _fail("stale_upload", "Artifact finalization changed", 409)
        actual = self._hash_stage(path, row["expected_size"], authorizer,
                                  row["project_id"])
        if actual != row["expected_digest"]:
            self._quarantine(object_id, "digest_mismatch")
            _fail("digest_mismatch", "Artifact digest differs", 409)
        self._authorize(authorizer, row["project_id"])
        writer = None
        try:
            writer = self.evidence.begin_blob(
                row["expected_digest"], row["expected_size"], owner=object_id,
                retention_class=row["retention_class"],
                retain_until_ms=row["retain_until_ms"])
            with path.open("rb") as source:
                while True:
                    block = source.read(self.max_chunk_bytes)
                    if not block:
                        break
                    writer.write(block)
                    self._authorize(authorizer, row["project_id"])
                    self._require_retained(row)
            reference = writer.publish()
        except LiveError:
            if writer is not None and not writer._closed:
                writer.abort()
            self._quarantine(object_id, "authorization_revoked")
            raise
        except (EvidenceStoreError, DiskBudgetError, OSError):
            if writer is not None and not writer._closed:
                writer.abort()
            self._quarantine(object_id, "publication_failed")
            _fail("storage_failure", "Artifact publication failed", 507)
        self._authorize(authorizer, row["project_id"])
        self._require_retained(row)
        with self._transaction() as connection:
            connection.execute(
                "UPDATE uploads SET state='publication-pending',reason=NULL,updated_ms=? "
                "WHERE object_id=? AND state='finalizing'",
                (self._now(), object_id))
        try:
            path.unlink()
            _fsync_directory(self.staging)
        except OSError:
            self._quarantine(object_id, "staging_cleanup_failed")
            _fail("storage_failure", "Artifact staging cleanup failed", 507)
        try:
            self._retain_metadata_charge(row)
        except DiskBudgetError:
            self._quarantine(object_id, "staging_charge_retained")
            _fail("storage_failure", "Artifact staging charge remains", 507)
        self._authorize(authorizer, row["project_id"])
        with self._transaction() as connection:
            connection.execute(
                "UPDATE uploads SET state='published',reason=NULL,updated_ms=? "
                "WHERE object_id=? AND state='publication-pending'",
                (self._now(), object_id))
        return self.status(object_id, host_identity=host_identity,
                           authorizer=authorizer,
                           expected_project_id=expected_project_id)

    @_serialized_transfer
    def open_read(self, object_id: str, *, host_identity, start: int = 0,
                  end: int | None = None, authorizer=None,
                  expected_project_id=None) -> ArtifactRead:
        row = self._row(object_id)
        if expected_project_id is not None and row["project_id"] != _id(
                expected_project_id, "project"):
            _fail("not_found", "Artifact object is unavailable", 404)
        self._check_host(row, host_identity)
        self._authorize(authorizer, row["project_id"])
        self._require_retained(row)
        if row["state"] != "published":
            _fail("not_found", "Artifact object is unavailable", 404)
        start = _integer(start, "range start", 0, row["expected_size"])
        if end is None:
            end = row["expected_size"]
        end = _integer(end, "range end", start + 1, row["expected_size"])
        pin_id = "transfer_" + uuid.uuid4().hex
        try:
            pin = self.evidence.pin(row["expected_digest"], pin_id, "export")
            body = self.evidence.read(row["expected_digest"])
            self._authorize(authorizer, row["project_id"])
            current = self._row(object_id)
            self._require_retained(current)
            if current["state"] != "published" or current["expected_digest"] != row["expected_digest"]:
                _fail("not_found", "Artifact object is unavailable", 404)
            return ArtifactRead(object_id, row["project_id"], row["expected_digest"],
                                row["expected_size"], start, end, body[start:end], pin)
        except Exception:
            if "pin" in locals():
                pin.close()
            raise

    def revalidate_read(self, value: ArtifactRead, *, host_identity,
                        authorizer=None) -> None:
        if type(value) is not ArtifactRead or value._closed:
            _fail("not_found", "Artifact object is unavailable", 404)
        row = self._row(value.object_id)
        self._check_host(row, host_identity)
        self._authorize(authorizer, row["project_id"])
        self._require_retained(row)
        if row["state"] != "published" or row["project_id"] != value.project_id \
                or row["expected_digest"] != value.digest \
                or row["expected_size"] != value.size:
            _fail("not_found", "Artifact object is unavailable", 404)

    @_serialized_transfer
    def tombstone(self, object_id: str, *, host_identity, authorizer=None,
                  reason: str = "retention_expired", expected_project_id=None):
        row = self._row(object_id)
        if expected_project_id is not None and row["project_id"] != _id(
                expected_project_id, "project"):
            _fail("not_found", "Artifact object is unavailable", 404)
        self._check_host(row, host_identity)
        self._authorize(authorizer, row["project_id"])
        reason = _id(reason, "tombstone reason")
        path = self._stage_path(object_id)
        try:
            if path.exists():
                path.unlink()
            _fsync_directory(self.staging)
            self.evidence.abandon_owner(object_id)
            if self.evidence.has_pending_owner(object_id):
                _fail("storage_failure", "Artifact evidence staging cleanup is pending", 507)
            self._retain_metadata_charge(row)
        except (OSError, DiskBudgetError, EvidenceStoreError):
            self._quarantine(object_id, "tombstone_cleanup_failed")
            _fail("storage_failure", "Artifact tombstone cleanup failed", 507)
        with self._lock:
            other_associations = int(self._connection.execute(
                "SELECT COUNT(*) FROM uploads WHERE expected_digest=? AND object_id!=? "
                "AND state!='tombstoned'", (row["expected_digest"], object_id)).fetchone()[0])
        if other_associations == 0 and self.evidence.lookup(row["expected_digest"]) is not None:
            try:
                self.evidence.tombstone(row["expected_digest"], reason=reason)
            except EvidenceStoreError:
                _fail("storage_failure", "Artifact tombstone cleanup failed", 507)
        with self._transaction() as connection:
            connection.execute(
                "UPDATE uploads SET state='tombstoned',metadata_json='{}',"
                "reason='metadata_cleanup_pending',updated_ms=? WHERE object_id=?",
                (self._now(), object_id))
            connection.execute("DELETE FROM chunks WHERE object_id=?", (object_id,))
        with self._lock:
            checkpoint = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint[0] != 0:
                _fail("storage_failure", "Artifact metadata cleanup is pending", 507)
            _fsync_directory(self.root)
        with self._transaction() as connection:
            connection.execute("UPDATE uploads SET reason=? WHERE object_id=?", (reason, object_id))
        return {"objectId": object_id, "state": "tombstoned"}

    def pending_retention_count(self):
        with self._lock:
            return int(self._connection.execute(
                "SELECT COUNT(*) FROM uploads WHERE (state!='tombstoned' AND retain_until_ms<=?) "
                "OR (state='tombstoned' AND reason='metadata_cleanup_pending')",
                (self._now(),)).fetchone()[0])

    @_active_transfer
    def apply_retention(self, *, limit=64):
        limit = _integer(limit, "retention batch", 1, 1024)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM uploads WHERE (state!='tombstoned' AND retain_until_ms<=?) "
                "OR (state='tombstoned' AND reason='metadata_cleanup_pending') "
                "ORDER BY updated_ms,object_id LIMIT ?", (self._now(), limit)).fetchall()
        removed = []
        for row in rows:
            try:
                self.tombstone(row["object_id"], host_identity=(
                    row["host_id"], row["host_generation"], row["host_incarnation"]))
            except (LiveError, EvidenceStoreError, DiskBudgetError, OSError):
                with self._transaction() as connection:
                    connection.execute("UPDATE uploads SET updated_ms=? WHERE object_id=?",
                                       (self._now(), row["object_id"]))
                continue
            removed.append(row["object_id"])
        return removed

    def _reconcile_staging(self):
        with self._lock:
            rows = self._connection.execute("SELECT * FROM uploads").fetchall()
            known = {row["object_id"] for row in rows}
            known_reservations = {row["reservation_id"] for row in rows}
            reservations = {item["reservation_id"]: item for item in
                            self.budget.reservations_for_owner(
                                self._budget_owner, category="transfer")}
            for path in self.staging.iterdir():
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or re.fullmatch(r"artifact_[0-9a-f]{32}\.part", path.name) is None):
                    _fail("storage_recovery_required", "Unexpected artifact staging requires offline recovery", 503)
                if path.stem not in known:
                    reservation_id = "reservation_" + hashlib.sha256(
                        path.stem.encode("ascii")).hexdigest()
                    if reservation_id not in reservations:
                        _fail("storage_recovery_required", "Unowned artifact staging requires offline recovery", 503)
                    try:
                        path.unlink()
                        _fsync_directory(self.staging)
                    except OSError:
                        _fail("storage_failure", "Artifact allocation cleanup is pending", 507)
            # An allocation reserves before creating a file or association.
            # Only this root's exact owner is reclaimed, after directory fsync;
            # other transfer stores may share the aggregate DiskBudget.
            try:
                _fsync_directory(self.staging)
                for reservation_id in reservations.keys() - known_reservations:
                    self.budget.release_id(reservation_id)
            except (OSError, DiskBudgetError):
                _fail("storage_failure", "Artifact allocation charge cleanup is pending", 507)
            for row in rows:
                path = self._stage_path(row["object_id"])
                if row["state"] == "finalizing":
                    state = ("publication-pending" if not path.exists()
                             and self.evidence.lookup(row["expected_digest"]) is not None
                             else "quarantined" if not path.exists() else "uploading")
                    self._connection.execute(
                        "UPDATE uploads SET state=?,reason=?,updated_ms=? WHERE object_id=?",
                        (state, None if state != "quarantined" else "staging_missing",
                         self._now(), row["object_id"]))
                if row["state"] == "uploading":
                    writing = self._connection.execute(
                        "SELECT * FROM chunks WHERE object_id=? AND state='writing'",
                        (row["object_id"],)).fetchall()
                    for chunk in writing:
                        try:
                            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                            try:
                                body = os.pread(descriptor, chunk["chunk_size"], chunk["offset"])
                            finally:
                                os.close(descriptor)
                            good = len(body) == chunk["chunk_size"] \
                                and hashlib.sha256(body).hexdigest() == chunk["chunk_digest"]
                        except OSError:
                            good = False
                        if good:
                            self._connection.execute(
                                "UPDATE chunks SET state='received' WHERE object_id=? AND offset=?",
                                (row["object_id"], chunk["offset"]))
                        else:
                            self._connection.execute(
                                "UPDATE uploads SET state='quarantined',reason='chunk_recovery_failed',updated_ms=? "
                                "WHERE object_id=?", (self._now(), row["object_id"]))

    def close(self):
        with self._lifecycle:
            self._closing = True
            if not self._lifecycle.wait_for(lambda: self._active_operations == 0, timeout=10):
                _fail("transfer_busy", "Artifact operations have not stopped", 503)
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True
                fcntl.flock(self._process_lock_fd, fcntl.LOCK_UN)
                os.close(self._process_lock_fd)


__all__ = [
    "ArtifactRead", "ArtifactTransferStore", "MAX_CHUNK_BYTES",
    "MAX_METADATA_BYTES", "TRANSFER_FORMAT_VERSION",
]
