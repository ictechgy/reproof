"""Bounded crash-safe content-addressed evidence objects."""
from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
from functools import wraps
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time
import threading
import uuid

from ..core import ContractError
from .disk_budget import DiskBudget, DiskBudgetError, DiskReservation


MAX_OBJECT_BYTES = 64 * 1024 * 1024
# Package archives use a distinct, still bounded CAS namespace.  Keep the
# ordinary EvidenceStore ceiling above as the default for every other store.
MAX_CONFIGURED_OBJECT_BYTES = 256 * 1024 * 1024
MAX_OBJECTS = 100_000
MAX_STAGING = 64
OBJECT_METADATA_BYTES = 8 * 1024
MAX_PINS_PER_OBJECT = 8
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_RETENTION = frozenset({"original", "intermediate", "derivative", "export"})
_PIN_PURPOSES = frozenset({"recording", "replay", "export", "finalizer"})
_RETENTION_PRIORITY = {"derivative": 0, "intermediate": 1, "export": 2, "original": 3}


class EvidenceStoreError(ContractError):
    """Evidence bytes cannot be safely published or retained."""


def _locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceStoreError(message)


def _digest(value: object) -> str:
    _require(type(value) is str and _DIGEST.fullmatch(value) is not None,
             "Invalid evidence digest")
    return value


def _identifier(value: object, name: str) -> str:
    _require(type(value) is str and _ID.fullmatch(value) is not None, f"Invalid {name}")
    return value


def _integer(value: object, name: str, low: int = 0,
             high: int = 2 ** 63 - 1) -> int:
    _require(type(value) is int and low <= value <= high, f"Invalid {name}")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class ObjectReference:
    digest: str
    bytes: int
    path: str
    retention_class: str
    retain_until_ms: int


@dataclass(slots=True)
class EvidencePin:
    digest: str
    pin_id: str
    purpose: str
    _store: "EvidenceStore" = field(repr=False, compare=False)
    _closed: bool = field(default=False, repr=False, compare=False)

    def close(self) -> None:
        if not self._closed:
            self._store.unpin(self.digest, self.pin_id)
            self._closed = True

    def __enter__(self) -> "EvidencePin":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class BlobWriter:
    def __init__(self, store: "EvidenceStore", staging_id: str, path: Path,
                 expected_digest: str, expected_size: int, reservation: DiskReservation):
        self._store = store
        self.staging_id = staging_id
        self.path = path
        self.expected_digest = expected_digest
        self.expected_size = expected_size
        self.reservation = reservation
        self._written = 0
        self._file = path.open("xb")
        os.chmod(path, 0o600)
        self._closed = False

    def write(self, data: bytes) -> int:
        _require(not self._closed, "Blob writer is closed")
        _require(type(data) is bytes, "Blob chunks must be bytes")
        _require(self._written + len(data) <= self.expected_size,
                 "Blob exceeds declared size")
        count = self._file.write(data)
        self._written += count
        return count

    def flush(self) -> None:
        _require(not self._closed, "Blob writer is closed")
        self._file.flush()
        os.fsync(self._file.fileno())

    def publish(self) -> ObjectReference:
        _require(not self._closed, "Blob writer is closed")
        self.flush()
        self._file.close()
        self._closed = True
        if self._written != self.expected_size:
            self._store._abort_writer(self)
            raise EvidenceStoreError("Blob size does not match declaration")
        hasher = hashlib.sha256()
        try:
            with self.path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(chunk)
        except OSError:
            self._store._abort_writer(self)
            raise EvidenceStoreError("Blob staging failed") from None
        if hasher.hexdigest() != self.expected_digest:
            self._store._abort_writer(self)
            raise EvidenceStoreError("Blob digest does not match declaration")
        return self._store._publish_writer(self)

    def abort(self) -> None:
        if not self._closed:
            self._file.close()
            self._closed = True
        self._store._abort_writer(self)


class EvidenceStore:
    def __init__(self, root: Path, budget: DiskBudget, *,
                 max_object_bytes: int = MAX_OBJECT_BYTES,
                 max_objects: int = MAX_OBJECTS, max_staging: int = MAX_STAGING,
                 _allow_object_limit_growth: bool = False):
        _require(type(budget) is DiskBudget, "A shared disk budget is required")
        _require(budget.capacity_bytes >= 1024 * 1024,
                 "Evidence storage requires metadata capacity of at least one MiB")
        self.budget = budget
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stat = self.root.lstat()
        _require(self.root.is_dir() and not self.root.is_symlink()
                 and stat.st_uid == os.getuid(), "Invalid evidence store directory")
        object_root = self.root / "objects"
        object_root.mkdir(exist_ok=True, mode=0o700)
        object_stat = object_root.lstat()
        _require(object_root.is_dir() and not object_root.is_symlink()
                 and object_stat.st_uid == os.getuid(),
                 "Invalid evidence object directory")
        self.objects = object_root / "sha256"
        self.staging = self.root / "staging"
        for directory in (self.objects, self.staging):
            directory.mkdir(exist_ok=True, mode=0o700)
            directory_stat = directory.lstat()
            _require(directory.is_dir() and not directory.is_symlink()
                     and directory_stat.st_uid == os.getuid(),
                     "Invalid evidence storage directory")
        _require(type(_allow_object_limit_growth) is bool,
                 "Invalid object limit migration option")
        self._allow_object_limit_growth = _allow_object_limit_growth
        object_limit_ceiling = (MAX_CONFIGURED_OBJECT_BYTES
                                 if _allow_object_limit_growth else MAX_OBJECT_BYTES)
        self.max_object_bytes = _integer(max_object_bytes, "object size limit", 1,
                                         object_limit_ceiling)
        requested_objects = _integer(max_objects, "object limit", 1, MAX_OBJECTS)
        self.max_objects = min(
            requested_objects,
            max(1, budget.metadata_headroom_bytes // OBJECT_METADATA_BYTES),
        )
        self.max_staging = _integer(max_staging, "staging limit", 1, MAX_STAGING)
        self._lock = threading.RLock()
        lock_fd = os.open(self.root / ".initialize.lock",
                          os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            database = self.root / "evidence.sqlite3"
            _require(not database.is_symlink()
                     and (not database.exists() or database.is_file()),
                     "Invalid evidence database path")
            self._connection = sqlite3.connect(
                database, timeout=10, isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout=10000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_size_limit=262144")
            self._connection.execute("PRAGMA wal_autocheckpoint=32")
            pages = self._connection.execute("PRAGMA max_page_count=32768").fetchone()[0]
            _require(pages <= 32768, "Evidence metadata database is oversized")
            self._initialize()
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
                self._connection = None
            raise
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        try:
            self._reconcile_budget()
            self._reconcile_tombstones()
        except Exception:
            self._connection.close()
            self._connection = None
            raise

    def _initialize(self) -> None:
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS objects (
                       digest TEXT PRIMARY KEY,
                       size INTEGER NOT NULL,
                       relative_path TEXT NOT NULL,
                       retention_class TEXT NOT NULL,
                       retain_until_ms INTEGER NOT NULL,
                       reservation_id TEXT NOT NULL,
                       state TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS tombstones (
                       digest TEXT PRIMARY KEY,
                       reason TEXT NOT NULL,
                       created_ms INTEGER NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS pins (
                       digest TEXT NOT NULL,
                       pin_id TEXT NOT NULL,
                       purpose TEXT NOT NULL,
                       PRIMARY KEY (digest, pin_id),
                       FOREIGN KEY (digest) REFERENCES objects(digest)
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS staging (
                       staging_id TEXT PRIMARY KEY,
                       digest TEXT NOT NULL,
                       size INTEGER NOT NULL,
                       owner TEXT NOT NULL,
                       retention_class TEXT NOT NULL,
                       retain_until_ms INTEGER NOT NULL,
                       relative_path TEXT NOT NULL,
                       reservation_id TEXT NOT NULL,
                       created_ms INTEGER NOT NULL
                   )"""
            )
            existing = dict(connection.execute("SELECT key, value FROM metadata"))
            expected = {"format_version": 1, "max_object_bytes": self.max_object_bytes,
                        "max_objects": self.max_objects, "max_staging": self.max_staging}
            if existing:
                if existing != expected:
                    # The only supported on-disk migration is an archive
                    # namespace growing its object ceiling.  The lock held by
                    # the caller and this transaction make the update atomic.
                    unchanged = (
                        set(existing) == set(expected)
                        and existing.get("format_version") == expected["format_version"]
                        and existing.get("max_objects") == expected["max_objects"]
                        and existing.get("max_staging") == expected["max_staging"]
                    )
                    old_limit = existing.get("max_object_bytes")
                    _require(
                        self._allow_object_limit_growth and unchanged
                        and type(old_limit) is int and 0 < old_limit < self.max_object_bytes,
                        "Evidence store configuration mismatch",
                    )
                    connection.execute(
                        "UPDATE metadata SET value = ? WHERE key = 'max_object_bytes'",
                        (self.max_object_bytes,),
                    )
            else:
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)", expected.items()
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _reconcile_budget(self) -> None:
        for row in self._connection.execute(
            "SELECT reservation_id, size FROM objects WHERE state = 'published'"
        ):
            try:
                self.budget.commit_id(
                    row["reservation_id"], row["size"] + OBJECT_METADATA_BYTES)
            except DiskBudgetError:
                # A missing charge is a store-integrity failure, never free
                # capacity that can be silently reused.
                raise EvidenceStoreError("Evidence reservation is unavailable") from None

    def _reconcile_tombstones(self) -> None:
        rows = self._connection.execute(
            """SELECT digest, relative_path, reservation_id FROM objects
                 WHERE state = 'tombstoned' ORDER BY digest"""
        ).fetchall()
        for row in rows:
            path, relative = self._object_path(row["digest"])
            _require(row["relative_path"] == relative,
                     "Evidence object path is invalid")
            if not self._remove_file(path):
                continue
            self.budget.release_id(row["reservation_id"])
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "DELETE FROM objects WHERE digest = ? AND state = 'tombstoned'",
                    (row["digest"],),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _object_path(self, digest: str) -> tuple[Path, str]:
        digest = _digest(digest)
        relative = f"objects/sha256/{digest[:2]}/{digest}"
        return self.root / relative, relative

    def _staging_path(self, staging_id: str) -> tuple[Path, str]:
        staging_id = _identifier(staging_id, "staging identity")
        relative = f"staging/{staging_id}.part"
        return self.root / relative, relative

    @staticmethod
    def _remove_file(path: Path) -> bool:
        try:
            if path.is_symlink():
                return False
            if path.exists():
                _require(path.is_file(), "Evidence path is not a file")
                path.unlink()
            _fsync_directory(path.parent)
            return not path.exists() and not path.is_symlink()
        except (OSError, EvidenceStoreError):
            return False

    @_locked
    def begin_blob(self, expected_digest: str, expected_size: int, *, owner: str,
                   retention_class: str, retain_until_ms: int,
                   reservation: DiskReservation | None = None) -> BlobWriter:
        expected_digest = _digest(expected_digest)
        expected_size = _integer(expected_size, "blob size", 1, self.max_object_bytes)
        owner = _identifier(owner, "blob owner")
        _require(retention_class in _RETENTION, "Invalid retention class")
        retain_until_ms = _integer(retain_until_ms, "retention deadline")
        if reservation is None:
            reservation = self.budget.reserve(
                owner, "spool", expected_size + OBJECT_METADATA_BYTES)
        else:
            _require(type(reservation) is DiskReservation
                     and reservation._budget is self.budget
                     and reservation.owner == owner
                     and reservation.bytes >= expected_size + OBJECT_METADATA_BYTES,
                     "Invalid blob reservation")
        staging_id = "staging_" + uuid.uuid4().hex
        path, relative = self._staging_path(staging_id)
        try:
            writer = BlobWriter(self, staging_id, path, expected_digest,
                                expected_size, reservation)
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            _require(connection.execute(
                "SELECT 1 FROM tombstones WHERE digest = ?", (expected_digest,)
            ).fetchone() is None, "Evidence digest is tombstoned")
            count = int(connection.execute("SELECT COUNT(*) FROM staging").fetchone()[0])
            _require(count < self.max_staging, "Evidence staging limit reached")
            connection.execute(
                """INSERT INTO staging
                   (staging_id, digest, size, owner, retention_class, retain_until_ms,
                    relative_path, reservation_id, created_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (staging_id, expected_digest, expected_size, owner, retention_class,
                 retain_until_ms, relative, reservation.reservation_id,
                 int(time.time() * 1000)),
            )
            connection.commit()
            return writer
        except Exception:
            if 'connection' in locals() and connection.in_transaction:
                connection.rollback()
            if 'writer' in locals() and not writer._closed:
                writer._file.close()
                writer._closed = True
            try:
                removed = self._remove_file(path)
            except Exception:
                removed = False
            if removed:
                reservation.close()
            raise

    @_locked
    def put_bytes(self, body: bytes, *, owner: str, retention_class: str,
                  retain_until_ms: int,
                  reservation: DiskReservation | None = None) -> ObjectReference:
        _require(type(body) is bytes and bool(body), "Evidence body must be non-empty bytes")
        digest = hashlib.sha256(body).hexdigest()
        writer = self.begin_blob(digest, len(body), owner=owner,
                                 retention_class=retention_class,
                                 retain_until_ms=retain_until_ms,
                                 reservation=reservation)
        try:
            writer.write(body)
            return writer.publish()
        except Exception:
            if not writer._closed:
                writer.abort()
            raise

    @_locked
    def _abort_writer(self, writer: BlobWriter) -> None:
        connection = self._connection
        row = connection.execute(
            "SELECT * FROM staging WHERE staging_id = ?", (writer.staging_id,)
        ).fetchone()
        if row is None:
            return
        _, expected_relative = self._staging_path(writer.staging_id)
        _require(row["relative_path"] == expected_relative
                 and row["digest"] == writer.expected_digest,
                 "Evidence staging path is invalid")
        removed = self._remove_file(writer.path)
        final_path, _ = self._object_path(writer.expected_digest)
        published = connection.execute(
            "SELECT 1 FROM objects WHERE digest = ? AND state = 'published'",
            (writer.expected_digest,),
        ).fetchone() is not None
        if not published and final_path.exists():
            removed = self._remove_file(final_path) and removed
        if not removed:
            return
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM staging WHERE staging_id = ?",
                               (writer.staging_id,))
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            return
        writer.reservation.close()

    @_locked
    def _publish_writer(self, writer: BlobWriter) -> ObjectReference:
        connection = self._connection
        final_path, relative = self._object_path(writer.expected_digest)
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM staging WHERE staging_id = ?", (writer.staging_id,)
            ).fetchone()
            _, expected_staging_path = self._staging_path(writer.staging_id)
            _require(row is not None and row["digest"] == writer.expected_digest
                     and row["size"] == writer.expected_size
                     and row["relative_path"] == expected_staging_path
                     and row["reservation_id"] == writer.reservation.reservation_id,
                     "Blob staging record is unavailable")
            _require(connection.execute(
                "SELECT 1 FROM tombstones WHERE digest = ?", (writer.expected_digest,)
            ).fetchone() is None, "Evidence digest is tombstoned")
            existing = connection.execute(
                "SELECT * FROM objects WHERE digest = ?", (writer.expected_digest,)
            ).fetchone()
            if existing is not None:
                _require(existing["state"] == "published"
                         and existing["size"] == writer.expected_size
                         and existing["relative_path"] == relative
                         and existing["retention_class"] in _RETENTION_PRIORITY,
                         "Evidence object identity conflict")
                retained_class = max(
                    (existing["retention_class"], row["retention_class"]),
                    key=_RETENTION_PRIORITY.__getitem__,
                )
                connection.execute(
                    """UPDATE objects
                          SET retain_until_ms = MAX(retain_until_ms, ?),
                              retention_class = ?
                        WHERE digest = ?""",
                    (row["retain_until_ms"], retained_class,
                     writer.expected_digest),
                )
                if writer.path.exists():
                    writer.path.unlink()
                _fsync_directory(writer.path.parent)
                connection.execute("DELETE FROM staging WHERE staging_id = ?",
                                   (writer.staging_id,))
                connection.commit()
                writer.reservation.close()
                return self.lookup(writer.expected_digest)
            count = int(connection.execute(
                """SELECT COUNT(*) FROM (
                       SELECT digest FROM objects
                       UNION SELECT digest FROM tombstones
                   )"""
            ).fetchone()[0])
            _require(count < self.max_objects, "Evidence object limit reached")
            final_path.parent.mkdir(exist_ok=True, mode=0o700)
            prefix_stat = final_path.parent.lstat()
            _require(final_path.parent.is_dir() and not final_path.parent.is_symlink()
                     and prefix_stat.st_uid == os.getuid(),
                     "Invalid evidence object directory")
            if final_path.exists():
                _require(final_path.is_file() and not final_path.is_symlink()
                         and final_path.stat().st_size == writer.expected_size,
                         "Evidence object path conflict")
                hasher = hashlib.sha256(final_path.read_bytes()).hexdigest()
                _require(hasher == writer.expected_digest, "Evidence object path conflict")
                writer.path.unlink()
            else:
                os.replace(writer.path, final_path)
                os.chmod(final_path, 0o600)
            _fsync_directory(final_path.parent)
            _fsync_directory(final_path.parent.parent)
            connection.execute(
                """INSERT INTO objects
                   (digest, size, relative_path, retention_class, retain_until_ms,
                    reservation_id, state)
                   VALUES (?, ?, ?, ?, ?, ?, 'published')""",
                (writer.expected_digest, writer.expected_size, relative,
                 row["retention_class"], row["retain_until_ms"],
                 row["reservation_id"]),
            )
            connection.execute("DELETE FROM staging WHERE staging_id = ?",
                               (writer.staging_id,))
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            tombstoned = connection.execute(
                "SELECT 1 FROM tombstones WHERE digest = ?",
                (writer.expected_digest,),
            ).fetchone() is not None
            if tombstoned:
                self._abort_writer(writer)
            # A renamed but unregistered file is inert.  Keep the reservation
            # and staging record so restart recovery cannot over-admit space.
            raise
        self.budget.commit(
            writer.reservation,
            actual_bytes=writer.expected_size + OBJECT_METADATA_BYTES,
        )
        return self.lookup(writer.expected_digest)

    @_locked
    def lookup(self, digest: str) -> ObjectReference | None:
        digest = _digest(digest)
        row = self._connection.execute(
            "SELECT * FROM objects WHERE digest = ? AND state = 'published'", (digest,)
        ).fetchone()
        if row is None:
            return None
        _, relative = self._object_path(digest)
        _require(row["relative_path"] == relative,
                 "Evidence object path is invalid")
        _require(self._connection.execute(
            "SELECT 1 FROM tombstones WHERE digest = ?", (digest,)
        ).fetchone() is None, "Evidence digest is tombstoned")
        return ObjectReference(row["digest"], row["size"], row["relative_path"],
                               row["retention_class"], row["retain_until_ms"])

    @_locked
    def uses_reservation(self, digest: str, reservation_id: str) -> bool:
        row = self._connection.execute(
            "SELECT reservation_id FROM objects WHERE digest = ? AND state = 'published'",
            (_digest(digest),),
        ).fetchone()
        return row is not None and row["reservation_id"] == reservation_id

    @_locked
    def is_tombstoned(self, digest: str) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM tombstones WHERE digest = ?", (_digest(digest),)
        ).fetchone() is not None

    @_locked
    def read(self, digest: str) -> bytes:
        reference = self.lookup(digest)
        _require(reference is not None, "Evidence object is unavailable")
        path, relative = self._object_path(reference.digest)
        _require(reference.path == relative, "Evidence object path is invalid")
        _require(path.is_file() and not path.is_symlink()
                 and path.stat().st_size == reference.bytes,
                 "Evidence object is unavailable")
        body = path.read_bytes()
        _require(hashlib.sha256(body).hexdigest() == reference.digest,
                 "Evidence object digest mismatch")
        return body

    @_locked
    def pin(self, digest: str, pin_id: str, purpose: str) -> EvidencePin:
        digest = _digest(digest)
        pin_id = _identifier(pin_id, "pin identity")
        _require(purpose in _PIN_PURPOSES, "Invalid pin purpose")
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            _require(connection.execute(
                "SELECT 1 FROM objects WHERE digest = ? AND state = 'published'", (digest,)
            ).fetchone() is not None, "Evidence object is unavailable")
            _require(connection.execute(
                "SELECT 1 FROM tombstones WHERE digest = ?", (digest,)
            ).fetchone() is None, "Evidence digest is tombstoned")
            existing = connection.execute(
                "SELECT purpose FROM pins WHERE digest = ? AND pin_id = ?",
                (digest, pin_id),
            ).fetchone()
            _require(existing is None or existing["purpose"] == purpose,
                     "Evidence pin identity conflict")
            if existing is None:
                _require(int(connection.execute(
                    "SELECT COUNT(*) FROM pins WHERE digest = ?", (digest,)
                ).fetchone()[0]) < MAX_PINS_PER_OBJECT,
                         "Evidence consumer pin limit reached")
                connection.execute(
                    "INSERT INTO pins(digest, pin_id, purpose) VALUES (?, ?, ?)",
                    (digest, pin_id, purpose),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return EvidencePin(digest, pin_id, purpose, self)

    @_locked
    def unpin(self, digest: str, pin_id: str) -> None:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("DELETE FROM pins WHERE digest = ? AND pin_id = ?",
                                     (_digest(digest), _identifier(pin_id, "pin identity")))
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    @_locked
    def tombstone(self, digest: str, *, reason: str) -> None:
        digest = _digest(digest)
        reason = _identifier(reason, "tombstone reason")
        connection = self._connection
        reservation_id = None
        relative = None
        abandoned = []
        connection.execute("BEGIN IMMEDIATE")
        try:
            _require(connection.execute(
                "SELECT 1 FROM pins WHERE digest = ?", (digest,)
            ).fetchone() is None, "Pinned evidence cannot be tombstoned")
            known = connection.execute(
                """SELECT 1 FROM tombstones WHERE digest = ?
                   UNION SELECT 1 FROM objects WHERE digest = ?
                   UNION SELECT 1 FROM staging WHERE digest = ? LIMIT 1""",
                (digest, digest, digest),
            ).fetchone()
            if known is None:
                count = int(connection.execute(
                    """SELECT COUNT(*) FROM (
                           SELECT digest FROM objects
                           UNION SELECT digest FROM tombstones
                       )"""
                ).fetchone()[0])
                _require(count < self.max_objects,
                         "Evidence tombstone limit reached")
            connection.execute(
                "INSERT OR IGNORE INTO tombstones(digest, reason, created_ms) VALUES (?, ?, ?)",
                (digest, reason, int(time.time() * 1000)),
            )
            row = connection.execute(
                "SELECT reservation_id, relative_path FROM objects WHERE digest = ?", (digest,)
            ).fetchone()
            if row is not None:
                _, expected_relative = self._object_path(digest)
                _require(row["relative_path"] == expected_relative,
                         "Evidence object path is invalid")
                reservation_id = row["reservation_id"]
                relative = expected_relative
                connection.execute("UPDATE objects SET state = 'tombstoned' WHERE digest = ?",
                                   (digest,))
            abandoned = connection.execute(
                "SELECT staging_id, relative_path, reservation_id FROM staging WHERE digest = ?",
                (digest,),
            ).fetchall()
            for item in abandoned:
                _, expected_relative = self._staging_path(item["staging_id"])
                _require(item["relative_path"] == expected_relative,
                         "Evidence staging path is invalid")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        if relative is not None:
            path = self.root / relative
            if self._remove_file(path):
                self.budget.release_id(reservation_id)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "DELETE FROM objects WHERE digest = ? AND state = 'tombstoned'",
                        (digest,),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        for item in abandoned:
            path, _ = self._staging_path(item["staging_id"])
            # Keep the row and reservation until the producer observes the
            # tombstone or an owner-recovery path proves it is no longer live.
            self._remove_file(path)
        orphan, _ = self._object_path(digest)
        if relative is None:
            try:
                if orphan.is_file() and not orphan.is_symlink():
                    orphan.unlink()
                    _fsync_directory(orphan.parent)
            except OSError:
                pass

    @_locked
    def apply_retention(self, *, now_ms: int, limit: int | None = None) -> list[str]:
        now_ms = _integer(now_ms, "retention time")
        if limit is not None:
            limit = _integer(limit, "retention batch limit", 1, 1024)
        batch = " LIMIT ?" if limit is not None else ""
        parameters = (now_ms, limit) if limit is not None else (now_ms,)
        rows = self._connection.execute(
            """SELECT digest FROM objects
                 WHERE state = 'published' AND retain_until_ms <= ?
                   AND NOT EXISTS (SELECT 1 FROM pins WHERE pins.digest = objects.digest)
                 ORDER BY digest""" + batch, parameters
        ).fetchall()
        staged = self._connection.execute(
            "SELECT DISTINCT digest FROM staging WHERE retain_until_ms <= ? ORDER BY digest" + batch,
            parameters,
        ).fetchall()
        result = []
        for row in list(rows) + list(staged):
            if row["digest"] in result:
                continue
            try:
                self.tombstone(row["digest"], reason="retention_expired")
            except EvidenceStoreError:
                continue
            result.append(row["digest"])
        return result

    @_locked
    def has_pending_owner(self, owner: str) -> bool:
        owner = _identifier(owner, "blob owner")
        return self._connection.execute(
            "SELECT 1 FROM staging WHERE owner=? LIMIT 1", (owner,)).fetchone() is not None

    @_locked
    def abandon_owner(self, owner: str, *, exclude_reservation_id: str | None = None) -> int:
        owner = _identifier(owner, "blob owner")
        clause = " AND reservation_id != ?" if exclude_reservation_id is not None else ""
        parameters = ((owner, exclude_reservation_id) if exclude_reservation_id is not None
                      else (owner,))
        rows = self._connection.execute(
            "SELECT staging_id, digest, relative_path, reservation_id FROM staging WHERE owner = ?" + clause,
            parameters,
        ).fetchall()
        for row in rows:
            _, expected_relative = self._staging_path(row["staging_id"])
            _require(row["relative_path"] == expected_relative,
                     "Evidence staging path is invalid")
        removed_rows = []
        for row in rows:
            path, _ = self._staging_path(row["staging_id"])
            removed = self._remove_file(path)
            final_path, _ = self._object_path(row["digest"])
            published = self._connection.execute(
                "SELECT 1 FROM objects WHERE digest = ? AND state = 'published'",
                (row["digest"],),
            ).fetchone() is not None
            if not published and final_path.exists():
                removed = self._remove_file(final_path) and removed
            if removed:
                removed_rows.append(row)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.executemany(
                "DELETE FROM staging WHERE staging_id = ?",
                [(row["staging_id"],) for row in removed_rows],
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        for row in removed_rows:
            self.budget.release_id(row["reservation_id"])
        return len(removed_rows)

    @_locked
    def abandon_reservation(self, reservation_id: str, *, release: bool) -> int:
        rows = self._connection.execute(
            "SELECT staging_id, relative_path FROM staging WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchall()
        for row in rows:
            _, expected_relative = self._staging_path(row["staging_id"])
            _require(row["relative_path"] == expected_relative,
                     "Evidence staging path is invalid")
        removed_rows = []
        for row in rows:
            path, _ = self._staging_path(row["staging_id"])
            if self._remove_file(path):
                removed_rows.append(row)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.executemany(
                "DELETE FROM staging WHERE staging_id = ?",
                [(row["staging_id"],) for row in removed_rows],
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        if release and removed_rows and len(removed_rows) == len(rows):
            self.budget.release_id(reservation_id)
        return len(removed_rows)

    @_locked
    def release_unused_reservation(self, reservation_id: str) -> bool:
        """Release an unused charge while retaining failed or published files."""
        _identifier(reservation_id, "storage reservation")
        referenced = self._connection.execute(
            """SELECT 1 FROM staging WHERE reservation_id = ?
               UNION SELECT 1 FROM objects WHERE reservation_id = ? LIMIT 1""",
            (reservation_id, reservation_id),
        ).fetchone()
        if referenced is not None:
            return False
        self.budget.release_id(reservation_id)
        return True

    @_locked
    def unpin_id(self, pin_id: str) -> int:
        pin_id = _identifier(pin_id, "pin identity")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                "DELETE FROM pins WHERE pin_id = ?", (pin_id,)
            )
            self._connection.commit()
            return cursor.rowcount
        except Exception:
            self._connection.rollback()
            raise

    @_locked
    def close(self) -> None:
        self._connection.close()


__all__ = [
    "BlobWriter", "EvidencePin", "EvidenceStore", "EvidenceStoreError",
    "OBJECT_METADATA_BYTES", "ObjectReference",
]
