"""Bounded, crash-safe transient frame storage for one recording."""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
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
from typing import Callable, Iterable

from ..core import ContractError
from .disk_budget import DiskBudget, DiskBudgetError


_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TEMP_NAME = re.compile(r"\.[0-9]+\.[0-9a-f]{32}\.tmp\Z")
_MAX_METADATA_BYTES = 4096
_MAX_PROOF_BYTES = 4096
_MAX_PAGE_COUNT = 4096
_MIN_PAGE_COUNT = 128
_JOURNAL_BYTES_PER_FRAME = 16 * 1024
_PAGE_BYTES = 4096
_BOOKKEEPING_MARGIN = 64 * 1024
_WRITER_GUARD = threading.RLock()
_WRITERS: set[tuple[str, str]] = set()


class FrameSpoolError(ContractError):
    """A frame cannot be safely staged, read, recovered, or released."""


@dataclass(frozen=True, slots=True)
class SpoolToken:
    """Opaque process-local reference to a staged frame."""

    issuer: str
    recording_id: str
    frame_sequence: int
    acquisition_sequence: int
    size: int
    digest: str
    metadata_digest: str


def _require(value: bool, message: str) -> None:
    if not value:
        raise FrameSpoolError(message)


def _canonical_json(value: object, *, limit: int, label: str) -> tuple[str, bytes]:
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
        data = text.encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise FrameSpoolError(f"Invalid {label}") from exc
    _require(len(data) <= limit, f"{label.capitalize()} exceeds its size limit")
    return text, data


class FrameSpool:
    """A single-process writer over a bounded SQLite-backed frame spool.

    The writer owns one lock and one budget owner for ``recording_id``. A stage
    first records a durable intent and reserves both frame bytes and journal
    headroom. The frame becomes readable only after fsync, rename, directory
    fsync, and a durable SQLite state transition.
    """

    def __init__(self, root: Path, budget: DiskBudget, recording_id: str,
                 max_frames: int, max_bytes: int, *, _open_existing=False):
        _require(isinstance(budget, DiskBudget), "Invalid disk budget")
        _require(isinstance(recording_id, str) and _IDENTIFIER.fullmatch(recording_id) is not None,
                 "Invalid recording identifier")
        _require(type(max_frames) is int and 0 < max_frames <= 1024,
                 "Invalid frame count bound")
        _require(type(max_bytes) is int and 0 < max_bytes <= 10 * 1024 * 1024 * 1024,
                 "Invalid frame byte bound")
        self.root = Path(root)
        self.budget = budget
        self.recording_id = recording_id
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self._existing_only = _open_existing
        self.owner = self._owner_for(self.root, recording_id)
        self._root_identity: tuple[int, int] | None = None
        self._frames_identity: tuple[int, int] | None = None
        self._source_reservation = None
        self._journal_reservation = None
        self._source_reservation_id: str | None = None
        self._journal_reservation_id: str | None = None
        self._cleanup_pending = 0
        self._set_bounds(max_frames, max_bytes)
        # Main DB, bounded rollback journal, and a temporary database during
        # retirement compaction can coexist. Charge all three before SQLite
        # creates or writes its files. DELETE mode avoids an unbounded WAL
        # retained by another reader.
        self._retired = False
        self._lock = threading.RLock()
        self._issuer = uuid.uuid4().hex
        self._closed = False
        self._lock_fd: int | None = None
        self._db: sqlite3.Connection | None = None
        self._open()

    @classmethod
    def open_existing(cls, root: Path, budget: DiskBudget, recording_id: str):
        """Adopt historical bounds while holding the existing writer lock."""
        return cls(root, budget, recording_id, 1, 1, _open_existing=True)

    @staticmethod
    def _owner_for(root: Path, recording_id: str) -> str:
        root_key = os.path.abspath(os.fspath(root))
        root_digest = hashlib.sha256(root_key.encode("utf-8")).hexdigest()[:32]
        return ("frame-spool-" + root_digest + "-" +
                hashlib.sha256(recording_id.encode("ascii")).hexdigest()[:16])

    @staticmethod
    def _bound_values(max_frames: int, max_bytes: int) -> tuple[int, int]:
        _require(type(max_frames) is int and 0 < max_frames <= 1024,
                 "Invalid frame count bound")
        _require(type(max_bytes) is int and 0 < max_bytes <= 10 * 1024 * 1024 * 1024,
                 "Invalid frame byte bound")
        max_pages = min(_MAX_PAGE_COUNT, max(_MIN_PAGE_COUNT,
            1 + (max_frames * (4096 + _MAX_PROOF_BYTES + 4096 + 2048) + 4095) // 4096))
        return max_pages, 3 * max_pages * _PAGE_BYTES + _BOOKKEEPING_MARGIN

    @classmethod
    def retire_uninitialized(cls, root: Path, budget: DiskBudget,
                             recording_id: str, *, max_frames: int,
                             max_bytes: int) -> int:
        """Release only exact fixed charges left before a spool DB existed."""
        _require(isinstance(budget, DiskBudget), "Invalid disk budget")
        _require(isinstance(recording_id, str)
                 and _IDENTIFIER.fullmatch(recording_id) is not None,
                 "Invalid recording identifier")
        root = Path(root)
        _, journal_capacity = cls._bound_values(max_frames, max_bytes)
        owner = cls._owner_for(root, recording_id)
        if not os.path.lexists(root):
            _require(not any(budget.reservations_for_owner(
                         owner, category=category)
                         for category in ("spool", "journal")),
                     "Uninitialized frame spool charge has no source root")
            return 0
        frames = root / "frames"
        lock_path = root / "writer.lock"
        try:
            root_info = root.lstat()
            frames_info = frames.lstat()
        except OSError as exc:
            raise FrameSpoolError(
                "Uninitialized frame spool residue is unavailable") from exc
        _require(stat.S_ISDIR(root_info.st_mode) and root_info.st_uid == os.getuid()
                 and not root.is_symlink()
                 and stat.S_ISDIR(frames_info.st_mode)
                 and frames_info.st_uid == os.getuid()
                 and not frames.is_symlink()
                 and next(frames.iterdir(), None) is None
                 and {item.name for item in root.iterdir()}
                 == {"frames", "writer.lock"},
                 "Uninitialized frame spool residue is not empty")
        key = (os.path.abspath(os.fspath(root)), recording_id)
        with _WRITER_GUARD:
            _require(key not in _WRITERS,
                     "A frame spool writer is already open for this recording")
            try:
                descriptor = os.open(
                    lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            except OSError as exc:
                raise FrameSpoolError(
                    "Uninitialized frame spool lock is unavailable") from exc
            try:
                lock_info = os.fstat(descriptor)
                named_lock = lock_path.lstat()
                _require(stat.S_ISREG(lock_info.st_mode)
                         and lock_info.st_uid == os.getuid()
                         and lock_info.st_size == 0
                         and (lock_info.st_dev, lock_info.st_ino)
                         == (named_lock.st_dev, named_lock.st_ino),
                         "Uninitialized frame spool lock changed")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise FrameSpoolError(
                        "A frame spool writer is already open") from exc
                _require((root.lstat().st_dev, root.lstat().st_ino)
                         == (root_info.st_dev, root_info.st_ino)
                         and (frames.lstat().st_dev, frames.lstat().st_ino)
                         == (frames_info.st_dev, frames_info.st_ino)
                         and next(frames.iterdir(), None) is None
                         and {item.name for item in root.iterdir()}
                         == {"frames", "writer.lock"},
                         "Uninitialized frame spool residue changed")
                expected = {
                    "spool": (owner + "-source", max_bytes),
                    "journal": (owner + "-journal", journal_capacity),
                }
                release = []
                for category, (idempotency_key, amount) in expected.items():
                    reservation_id = "reservation_" + hashlib.sha256(
                        idempotency_key.encode("ascii")).hexdigest()
                    rows = budget.reservations_for_owner(
                        owner, category=category)
                    matching = [row for row in rows
                                if row["reservation_id"] == reservation_id]
                    _require(len(matching) <= 1,
                             "Uninitialized frame spool reservation changed")
                    if matching:
                        row = matching[0]
                        _require(row["state"] == "active"
                                 and row["charged_bytes"] == amount,
                                 "Uninitialized frame spool reservation changed")
                        release.append(reservation_id)
                for reservation_id in release:
                    budget.release_id(reservation_id)
                return len(release)
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _set_bounds(self, max_frames, max_bytes):
        max_pages, journal_capacity = self._bound_values(max_frames, max_bytes)
        self.max_frames, self.max_bytes = max_frames, max_bytes
        self._max_pages = max_pages
        self._journal_capacity = journal_capacity

    def _open(self) -> None:
        root = self.root
        if self._existing_only:
            _require(root.is_dir() and (root / "frames").is_dir()
                     and (root / "spool.sqlite3").is_file(),
                     "Existing frame spool is unavailable")
        ancestor = root.absolute().parent
        _require(not ancestor.is_symlink(), "Frame spool parent is a symlink")
        if root.exists():
            _require(not root.is_symlink() and root.is_dir(), "Invalid frame spool root")
            _require(root.stat().st_uid == os.getuid(), "Frame spool root is not owned by this user")
        else:
            root.mkdir(parents=True, mode=0o700)
        frames = root / "frames"
        if frames.exists():
            _require(not frames.is_symlink() and frames.is_dir(), "Invalid frame spool frame directory")
            _require(frames.stat().st_uid == os.getuid(), "Frame spool frame directory is not owned by this user")
        else:
            frames.mkdir(mode=0o700)
        root_info = root.lstat()
        frames_info = frames.lstat()
        self._root_identity = (root_info.st_dev, root_info.st_ino)
        self._frames_identity = (frames_info.st_dev, frames_info.st_ino)
        key = (os.path.abspath(os.fspath(root)), self.recording_id)
        with _WRITER_GUARD:
            _require(key not in _WRITERS, "A frame spool writer is already open for this recording")
            lock_path = root / "writer.lock"
            try:
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as exc:
                try:
                    os.close(fd)
                except (UnboundLocalError, OSError):
                    pass
                raise FrameSpoolError("A frame spool writer is already open") from exc
            self._lock_fd = fd
            fresh = False
            try:
                database = root / "spool.sqlite3"
                _require(not database.is_symlink() and (not database.exists() or database.is_file()),
                         "Invalid frame spool database")
                if not database.exists():
                    _require(not self._existing_only and next(frames.iterdir(), None) is None,
                             "Fresh frame spool contains unjournaled source files")
                    fresh = True
                    self._reserve_fixed()
                db = sqlite3.connect(database, timeout=10, isolation_level=None,
                                     check_same_thread=False)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA busy_timeout=10000")
                self._db = db
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "spool_meta" in tables:
                    if self._existing_only:
                        saved = dict(db.execute("SELECT key,value FROM spool_meta"))
                        try:
                            saved_frames, saved_bytes = int(saved["max_frames"]), int(saved["max_bytes"])
                        except (KeyError, ValueError, TypeError):
                            raise FrameSpoolError("Stored frame spool bounds are invalid") from None
                        self._set_bounds(saved_frames, saved_bytes)
                    self._adopt_saved_reservations()
                elif not fresh:
                    _require(not self._existing_only and next(frames.iterdir(), None) is None,
                             "Frame spool journal is missing for existing source files")
                    fresh = True
                    self._reserve_fixed()
                if self._retired:
                    db.execute("PRAGMA query_only=ON")
                else:
                    db.execute(f"PRAGMA page_size={_PAGE_BYTES}")
                    _require(db.execute("PRAGMA page_size").fetchone()[0] == _PAGE_BYTES,
                             "Frame spool page size changed")
                    _require(db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete",
                             "Frame spool rollback journal is unavailable")
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("PRAGMA secure_delete=ON")
                    db.execute("PRAGMA temp_store=MEMORY")
                    _require(db.execute(f"PRAGMA max_page_count={self._max_pages}").fetchone()[0]
                             == self._max_pages, "Frame spool page limit exceeded")
                    db.execute(f"PRAGMA journal_size_limit={self._journal_capacity}")
                    self._initialize()
                _WRITERS.add(key)
            except Exception:
                if self._db is not None:
                    self._db.close()
                    self._db = None
                if fresh and self._cleanup_uninitialized():
                    for reservation in (self._source_reservation, self._journal_reservation):
                        if reservation is not None:
                            try:
                                reservation.close()
                            except DiskBudgetError:
                                pass
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                self._lock_fd = None
                raise

    def _cleanup_uninitialized(self) -> bool:
        # No token can have escaped a constructor that did not return. Remove
        # only its newly created SQLite files, keeping the stable empty lock.
        try:
            self._assert_identity()
            _require(next((self.root / "frames").iterdir(), None) is None,
                     "Failed spool initialization has source residue")
            for name in ("spool.sqlite3", "spool.sqlite3-journal", "spool.sqlite3-wal", "spool.sqlite3-shm"):
                self._unlink_bookkeeping(self.root / name)
            self._fsync_root()
            return True
        except (OSError, FrameSpoolError):
            return False

    def _reserve_fixed(self) -> None:
        self._source_reservation = self.budget.reserve(
            self.owner, "spool", self.max_bytes, idempotency_key=self.owner + "-source")
        self._source_reservation_id = self._source_reservation.reservation_id
        self._journal_reservation = self.budget.reserve(
            self.owner, "journal", self._journal_capacity, idempotency_key=self.owner + "-journal")
        self._journal_reservation_id = self._journal_reservation.reservation_id

    def _assert_identity(self, *, allow_closed=False) -> None:
        _require(not self._closed or allow_closed, "Frame spool is closed")
        try:
            root_info = self.root.lstat()
            frames_info = (self.root / "frames").lstat()
        except OSError as exc:
            raise FrameSpoolError("Frame spool directory is unavailable") from exc
        _require(root_info.st_uid == os.getuid() and stat.S_ISDIR(root_info.st_mode) and
                 (root_info.st_dev, root_info.st_ino) == self._root_identity,
                 "Frame spool root changed")
        _require(frames_info.st_uid == os.getuid() and stat.S_ISDIR(frames_info.st_mode) and
                 (frames_info.st_dev, frames_info.st_ino) == self._frames_identity,
                 "Frame spool frame directory changed")

    def _adopt_saved_reservations(self) -> None:
        db = self._db
        _require(db is not None, "Frame spool database is unavailable")
        values = dict(db.execute("SELECT key,value FROM spool_meta"))
        required = {"format_version", "recording_id", "owner", "max_frames", "max_bytes",
                    "max_pages", "journal_capacity", "root_dev", "root_ino",
                    "frames_dev", "frames_ino", "source_reservation", "journal_reservation",
                    "cleanup_pending", "page_size", "journal_mode"}
        _require(set(values) == required, "Frame spool format metadata is incomplete")
        expected = {
            "format_version": "3",
            "page_size": str(_PAGE_BYTES),
            "journal_mode": "delete",
            "recording_id": self.recording_id,
            "owner": self.owner,
            "max_frames": str(self.max_frames),
            "max_bytes": str(self.max_bytes),
            "max_pages": str(self._max_pages),
            "journal_capacity": str(self._journal_capacity),
            "root_dev": str(self._root_identity[0]),
            "root_ino": str(self._root_identity[1]),
            "frames_dev": str(self._frames_identity[0]),
            "frames_ino": str(self._frames_identity[1]),
            "cleanup_pending": "0",
        }
        _require(values["cleanup_pending"] in {"0", "1"}, "Invalid spool retirement state")
        self._cleanup_pending = int(values["cleanup_pending"])
        _require(db.execute("PRAGMA page_size").fetchone()[0] == _PAGE_BYTES
                 and db.execute("PRAGMA journal_mode").fetchone()[0] == "delete",
                 "Frame spool SQLite configuration changed")
        for key, expected_value in expected.items():
            if key == "cleanup_pending":
                continue
            _require(values[key] == expected_value, "Frame spool configuration changed")
        source_id = values["source_reservation"]
        journal_id = values["journal_reservation"]
        self._source_reservation_id = source_id
        self._journal_reservation_id = journal_id
        try:
            self._source_reservation = self.budget.adopt(
                source_id, owner=self.owner, category="spool", minimum_bytes=self.max_bytes)
        except DiskBudgetError:
            _require(self._cleanup_pending == 1, "Frame spool source reservation is unavailable")
        try:
            self._journal_reservation = self.budget.adopt(
                journal_id, owner=self.owner, category="journal", minimum_bytes=self._journal_capacity)
        except DiskBudgetError:
            journals = {item["reservation_id"]: item for item in
                        self.budget.reservations_for_owner(self.owner, category="journal")}
            retained = journals.get(journal_id)
            _require(self._cleanup_pending == 1 and self._source_reservation is None
                     and retained is not None and retained["state"] == "committed"
                     and retained["charged_bytes"] >= self._bookkeeping_bytes(),
                     "Frame spool journal reservation is unavailable")
            self._retired = True
            self._assert_retired_empty()

    def _assert_retired_empty(self) -> None:
        self._assert_identity(allow_closed=True)
        _require(next((self.root / "frames").iterdir(), None) is None,
                 "Retired spool contains unexpected source files")
        self._bookkeeping_bytes()
        db = self._db
        temporary = db is None
        if temporary:
            db = sqlite3.connect((self.root / "spool.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
        try:
            _require(db.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 0,
                     "Retired spool contains source rows")
        finally:
            if temporary:
                db.close()

    def _initialize(self) -> None:
        db = self._database()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS spool_meta (
                         key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS recordings (
                         recording_id TEXT PRIMARY KEY,
                         watermark INTEGER NOT NULL,
                         acquisition_watermark INTEGER NOT NULL,
                         accepted_count INTEGER NOT NULL,
                         released_count INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS frames (
                         recording_id TEXT NOT NULL,
                         frame_sequence INTEGER NOT NULL,
                         acquisition_sequence INTEGER NOT NULL,
                         size INTEGER NOT NULL,
                         digest TEXT NOT NULL,
                         metadata_digest TEXT NOT NULL,
                         metadata_json TEXT NOT NULL,
                         filename TEXT NOT NULL,
                         temp_filename TEXT NOT NULL,
                         source_reservation TEXT NOT NULL,
                         journal_reservation TEXT NOT NULL,
                         state TEXT NOT NULL,
                         proof_digest TEXT,
                         proof_json TEXT,
                         created_ns INTEGER NOT NULL,
                         PRIMARY KEY (recording_id, frame_sequence))""")
            db.execute("CREATE INDEX IF NOT EXISTS frames_state ON frames(recording_id, state)")
            values = {
                "format_version": "3",
                "page_size": str(_PAGE_BYTES),
                "journal_mode": "delete",
                "recording_id": self.recording_id,
                "owner": self.owner,
                "max_frames": str(self.max_frames),
                "max_bytes": str(self.max_bytes),
                "max_pages": str(self._max_pages),
                "journal_capacity": str(self._journal_capacity),
                "root_dev": str(self._root_identity[0]),
                "root_ino": str(self._root_identity[1]),
                "frames_dev": str(self._frames_identity[0]),
                "frames_ino": str(self._frames_identity[1]),
                "source_reservation": self._source_reservation_id,
                "journal_reservation": self._journal_reservation_id,
                "cleanup_pending": str(self._cleanup_pending),
            }
            prior = dict(db.execute("SELECT key,value FROM spool_meta"))
            if prior:
                _require(prior == values, "Frame spool configuration changed")
            else:
                db.executemany("INSERT INTO spool_meta(key,value) VALUES(?,?)", values.items())
            row = db.execute("SELECT * FROM recordings WHERE recording_id=?", (self.recording_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO recordings VALUES(?,?,?, ?,?)",
                           (self.recording_id, 0, 0, 0, 0))
            db.commit()
        except Exception:
            db.rollback()
            raise

    def _database(self) -> sqlite3.Connection:
        _require(not self._closed and self._db is not None, "Frame spool is closed")
        self._assert_identity()
        return self._db

    def _writable(self) -> sqlite3.Connection:
        db = self._database()
        state = db.execute("SELECT value FROM spool_meta WHERE key='cleanup_pending'").fetchone()
        _require(not self._retired and state is not None and state[0] == "0",
                 "Frame spool admission is closed")
        return db

    @staticmethod
    def _sequence(value: object, label: str) -> int:
        _require(type(value) is int and 0 < value <= 2 ** 63 - 1, f"Invalid {label}")
        return value

    def _metadata(self, value: object) -> tuple[str, str]:
        _require(isinstance(value, dict) and all(isinstance(key, str) for key in value),
                 "Frame metadata must be an object with string keys")
        text, data = _canonical_json(value, limit=_MAX_METADATA_BYTES, label="frame metadata")
        return text, hashlib.sha256(data).hexdigest()

    def _frame_path(self, sequence: int) -> Path:
        return self.root / "frames" / f"{sequence}.frame"

    def _temp_path(self, filename: str) -> Path:
        _require(isinstance(filename, str) and _TEMP_NAME.fullmatch(filename) is not None,
                 "Invalid frame temporary name")
        return self.root / "frames" / filename

    def _write_file(self, row: sqlite3.Row, body: bytes) -> None:
        destination = self._frame_path(int(row["frame_sequence"]))
        temporary = self._temp_path(row["temp_filename"])
        _require(not destination.exists() and not destination.is_symlink(), "Frame sequence file already exists")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            offset = 0
            while offset < len(body):
                written = os.write(fd, body[offset:offset + 1024 * 1024])
                _require(type(written) is int and written > 0, "Frame write made no progress")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, destination)
        self._fsync_directory()

    def _mark_active(self, sequence: int) -> None:
        db = self._database()
        db.execute("BEGIN IMMEDIATE")
        try:
            cursor = db.execute("UPDATE frames SET state='active' WHERE recording_id=? AND frame_sequence=? AND state='staging'",
                                (self.recording_id, sequence))
            _require(cursor.rowcount == 1, "Frame staging intent disappeared")
            db.commit()
        except Exception:
            db.rollback()
            raise

    def stage(self, frame_sequence: int, acquisition_sequence: int, body: bytes,
              digest: str, metadata: dict) -> SpoolToken:
        with self._lock:
            db = self._writable()
            frame_sequence = self._sequence(frame_sequence, "frame sequence")
            acquisition_sequence = self._sequence(acquisition_sequence, "acquisition sequence")
            _require(isinstance(body, bytes) and 0 < len(body) <= min(self.max_bytes, 3 * 1024 * 1024),
                     "Invalid frame bytes")
            _require(isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None,
                     "Invalid frame digest")
            _require(hashlib.sha256(body).hexdigest() == digest, "Frame digest does not match bytes")
            metadata_json, metadata_digest = self._metadata(metadata)
            row_inserted = False
            row: sqlite3.Row | None = None
            try:
                _require(self._source_reservation is not None and self._journal_reservation is not None,
                         "Frame spool reservations are unavailable")
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT state FROM frames WHERE recording_id=? AND frame_sequence=?",
                                      (self.recording_id, frame_sequence)).fetchone()
                _require(existing is None, "Frame sequence is already active")
                record = db.execute("SELECT watermark,acquisition_watermark FROM recordings WHERE recording_id=?",
                                    (self.recording_id,)).fetchone()
                _require(record is not None and frame_sequence > int(record["watermark"]) and
                         acquisition_sequence > int(record["acquisition_watermark"]),
                         "Frame and acquisition sequences must increase monotonically")
                totals = db.execute("SELECT COUNT(*) AS count, COALESCE(SUM(size),0) AS bytes FROM frames "
                                    "WHERE recording_id=? AND state IN ('staging','active','releasable')",
                                    (self.recording_id,)).fetchone()
                _require(int(totals["count"]) < self.max_frames, "Frame spool frame limit reached")
                _require(int(totals["bytes"]) + len(body) <= self.max_bytes, "Frame spool byte limit reached")
                filename = f"{frame_sequence}.frame"
                temp_filename = f".{frame_sequence}.{uuid.uuid4().hex}.tmp"
                db.execute("INSERT INTO frames VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (self.recording_id, frame_sequence, acquisition_sequence, len(body), digest,
                            metadata_digest, metadata_json, filename, temp_filename,
                            self._source_reservation.reservation_id, self._journal_reservation.reservation_id,
                            "staging", None, None, time.monotonic_ns()))
                db.execute("""UPDATE recordings SET watermark=MAX(watermark,?),
                             acquisition_watermark=MAX(acquisition_watermark,?),
                             accepted_count=accepted_count+1 WHERE recording_id=?""",
                           (frame_sequence, acquisition_sequence, self.recording_id))
                db.commit()
                row_inserted = True
                row = db.execute("SELECT * FROM frames WHERE recording_id=? AND frame_sequence=?",
                                 (self.recording_id, frame_sequence)).fetchone()
                _require(row is not None, "Frame staging intent disappeared")
                self._write_file(row, body)
                self._mark_active(frame_sequence)
                return SpoolToken(self._issuer, self.recording_id, frame_sequence,
                                  acquisition_sequence, len(body), digest, metadata_digest)
            except Exception as error:
                try:
                    db.rollback()
                except Exception:
                    pass
                cleanup_ok = self._cleanup_row_files(row)
                if cleanup_ok and row_inserted:
                    try:
                        db.execute("BEGIN IMMEDIATE")
                        db.execute("DELETE FROM frames WHERE recording_id=? AND frame_sequence=? AND state='staging'",
                                   (self.recording_id, frame_sequence))
                        db.commit()
                        row_inserted = False
                    except Exception:
                        db.rollback()
                        cleanup_ok = False
                if isinstance(error, FrameSpoolError):
                    raise
                if isinstance(error, DiskBudgetError):
                    raise FrameSpoolError(str(error)) from error
                raise FrameSpoolError("Frame could not be durably staged") from error

    def _cleanup_row_files(self, row: sqlite3.Row | None) -> bool:
        if row is None:
            return True
        try:
            self._unlink_file(self._temp_path(row["temp_filename"]))
            self._unlink_file(self._frame_path(int(row["frame_sequence"])))
            _require(not os.path.lexists(self._temp_path(row["temp_filename"])) and
                     not os.path.lexists(self._frame_path(int(row["frame_sequence"]))),
                     "Frame files could not be confirmed absent")
            self._fsync_directory()
            return True
        except Exception:
            return False

    def _owned_open(self, path: Path, expected_size: int) -> tuple[int, os.stat_result]:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(descriptor)
        except OSError as exc:
            raise FrameSpoolError("Frame file is unavailable") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size != expected_size:
            os.close(descriptor)
            raise FrameSpoolError("Frame file is not an owned regular file")
        return descriptor, info

    def _read_row_file(self, row: sqlite3.Row) -> bytes:
        descriptor, _ = self._owned_open(self._frame_path(int(row["frame_sequence"])), int(row["size"]))
        try:
            data = bytearray()
            while len(data) < int(row["size"]):
                chunk = os.read(descriptor, int(row["size"]) - len(data))
                if not chunk:
                    raise FrameSpoolError("Frame file ended before its promised size")
                data.extend(chunk)
            if os.read(descriptor, 1):
                raise FrameSpoolError("Frame file exceeds its promised size")
        finally:
            os.close(descriptor)
        result = bytes(data)
        _require(hashlib.sha256(result).hexdigest() == row["digest"], "Frame digest changed")
        return result

    def _row_for_token(self, token: SpoolToken, state: str = "active") -> sqlite3.Row:
        _require(isinstance(token, SpoolToken) and token.issuer == self._issuer,
                 "Frame token belongs to another spool issuer")
        _require(token.recording_id == self.recording_id, "Frame token belongs to another recording")
        row = self._database().execute("SELECT * FROM frames WHERE recording_id=? AND frame_sequence=? AND state=?",
                                       (self.recording_id, token.frame_sequence, state)).fetchone()
        _require(row is not None and int(row["acquisition_sequence"]) == token.acquisition_sequence and
                 int(row["size"]) == token.size and row["digest"] == token.digest and
                 row["metadata_digest"] == token.metadata_digest,
                 "Frame token does not match the durable frame record")
        return row

    def read(self, token: SpoolToken) -> bytes:
        with self._lock:
            return self._read_row_file(self._row_for_token(token))

    def read_metadata(self, token: SpoolToken) -> dict:
        with self._lock:
            row = self._row_for_token(token)
            try:
                value = json.loads(row["metadata_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise FrameSpoolError("Frame metadata is corrupt") from exc
            _require(isinstance(value, dict), "Frame metadata is corrupt")
            _require(hashlib.sha256(row["metadata_json"].encode("utf-8")).hexdigest() == token.metadata_digest,
                     "Frame metadata digest changed")
            return value

    def _token_from_row(self, row: sqlite3.Row) -> SpoolToken:
        return SpoolToken(self._issuer, self.recording_id, int(row["frame_sequence"]),
                          int(row["acquisition_sequence"]), int(row["size"]), row["digest"],
                          row["metadata_digest"])

    def get_active_tokens(self) -> tuple[SpoolToken, ...]:
        with self._lock:
            rows = self._database().execute("SELECT * FROM frames WHERE recording_id=? AND state='active' ORDER BY frame_sequence",
                                            (self.recording_id,)).fetchall()
            return tuple(self._token_from_row(row) for row in rows)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            db = self._database()
            totals = db.execute("""SELECT
                COALESCE(SUM(CASE WHEN state IN ('staging','active','releasable') THEN 1 ELSE 0 END),0) AS active_count,
                COALESCE(SUM(CASE WHEN state IN ('staging','active','releasable') THEN size ELSE 0 END),0) AS active_bytes,
                COALESCE(SUM(CASE WHEN state='staging' THEN 1 ELSE 0 END),0) AS staging_count,
                COALESCE(SUM(CASE WHEN state='releasable' THEN 1 ELSE 0 END),0) AS releasable_count
                FROM frames WHERE recording_id=?""", (self.recording_id,)).fetchone()
            record = db.execute("SELECT * FROM recordings WHERE recording_id=?", (self.recording_id,)).fetchone()
            return {"activeCount": int(totals["active_count"]), "activeBytes": int(totals["active_bytes"]),
                    "stagingCount": int(totals["staging_count"]), "releasableCount": int(totals["releasable_count"]),
                    "watermark": int(record["watermark"]), "acquisitionWatermark": int(record["acquisition_watermark"]),
                    "acceptedCount": int(record["accepted_count"]), "releasedCount": int(record["released_count"])}

    def _tokens(self, tokens: Iterable[SpoolToken], state: str) -> tuple[SpoolToken, ...]:
        if isinstance(tokens, SpoolToken):
            tokens = (tokens,)
        try:
            values = tuple(tokens)
        except TypeError as exc:
            raise FrameSpoolError("Expected frame tokens") from exc
        _require(0 < len(values) <= self.max_frames, "Invalid frame token set")
        _require(len({token.frame_sequence for token in values if isinstance(token, SpoolToken)}) == len(values),
                 "Duplicate frame token")
        for token in values:
            self._row_for_token(token, state)
        return values

    @staticmethod
    def _proof(value: object) -> tuple[str, str]:
        _require(value is not None and not isinstance(value, bool),
                 "Durable proof callback must return a proof value")
        if isinstance(value, bytes):
            value = {"encoding": "hex", "value": value.hex()}
        text, data = _canonical_json(value, limit=_MAX_PROOF_BYTES, label="durable proof")
        return text, hashlib.sha256(data).hexdigest()

    def _proof_for_tokens(self, tokens: tuple[SpoolToken, ...], callback: Callable) -> tuple[str, str]:
        _require(callable(callback), "A trusted durable proof callback is required")
        try:
            return self._proof(callback(tokens))
        except FrameSpoolError:
            raise
        except Exception as exc:
            raise FrameSpoolError("Durable proof callback rejected the frames") from exc

    def mark_releasable(self, tokens: Iterable[SpoolToken], durable_proof: Callable) -> str:
        """Persist a proof returned by trusted code before touching files.

        The callback receives the exact ordered token tuple. Its implementation
        owns G2 segment/manifest verification; this module deliberately accepts
        no caller-supplied ``durable=True`` flag.
        """
        with self._lock:
            self._writable()
            selected = self._tokens(tokens, "active")
            proof_json, proof_digest = self._proof_for_tokens(selected, durable_proof)
            db = self._database()
            db.execute("BEGIN IMMEDIATE")
            try:
                for token in selected:
                    db.execute("UPDATE frames SET state='releasable', proof_digest=?, proof_json=? "
                               "WHERE recording_id=? AND frame_sequence=? AND state='active'",
                               (proof_digest, proof_json, self.recording_id, token.frame_sequence))
                db.commit()
            except Exception:
                db.rollback()
                raise
            return proof_digest

    def _unlink_file(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        _require(stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_uid == os.getuid(),
                 "Refusing to unlink an unowned or linked frame file")
        os.unlink(path)

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.root / "frames", flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _release_rows(self, rows: tuple[sqlite3.Row, ...]) -> int:
        # A persisted proof binds the whole ordered token group. Keep every
        # row until every unlink and the directory fsync have succeeded, so a
        # retry after a partial unlink receives that same group.
        try:
            for row in rows:
                self._unlink_file(self._temp_path(row["temp_filename"]))
                self._unlink_file(self._frame_path(int(row["frame_sequence"])))
                _require(not os.path.lexists(self._temp_path(row["temp_filename"])) and
                         not os.path.lexists(self._frame_path(int(row["frame_sequence"]))),
                         "Frame files could not be confirmed absent")
            self._fsync_directory()
        except Exception as exc:
            raise FrameSpoolError("Frame unlink or directory sync failed; storage charge retained") from exc
        db = self._database()
        try:
            db.execute("BEGIN IMMEDIATE")
            for row in rows:
                cursor = db.execute("DELETE FROM frames WHERE recording_id=? AND frame_sequence=? AND state='releasable'",
                                    (self.recording_id, int(row["frame_sequence"])))
                _require(cursor.rowcount == 1, "Releasable frame disappeared")
            db.execute("UPDATE recordings SET released_count=released_count+? WHERE recording_id=?",
                       (len(rows), self.recording_id))
            db.commit()
        except Exception:
            db.rollback()
            raise FrameSpoolError("Frame release journal update failed; storage charge retained")
        return len(rows)

    def release_marked(self, tokens: Iterable[SpoolToken], durable_proof: Callable | None = None) -> int:
        with self._lock:
            selected = self._tokens(tokens, "releasable")
            rows = tuple(self._row_for_token(token, "releasable") for token in selected)
            expected = {row["proof_digest"] for row in rows}
            _require(len(expected) == 1 and None not in expected, "Releasable frame proof is missing")
            _, supplied = self._proof_for_tokens(selected, durable_proof)
            _require(supplied in expected, "Durable proof does not match releasable frames")
            return self._release_rows(rows)

    def release(self, tokens: Iterable[SpoolToken], durable_proof: Callable) -> int:
        """Verify and persist a proof, then release the corresponding files."""
        with self._lock:
            selected = self._tokens(tokens, "active")
            self.mark_releasable(selected, durable_proof)
            selected = self._tokens(selected, "releasable")
            return self._release_rows(tuple(self._row_for_token(token, "releasable") for token in selected))

    def _delete_staging_row(self, row: sqlite3.Row) -> bool:
        if not self._cleanup_row_files(row):
            return False
        db = self._database()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM frames WHERE recording_id=? AND frame_sequence=? AND state='staging'",
                       (self.recording_id, int(row["frame_sequence"])))
            db.commit()
        except Exception:
            db.rollback()
            return False
        return True

    def _recover_staging(self, row: sqlite3.Row) -> None:
        destination = self._frame_path(int(row["frame_sequence"]))
        valid = False
        try:
            self._read_row_file(row)
            valid = True
        except FrameSpoolError:
            valid = False
        temporary = self._temp_path(row["temp_filename"])
        if valid:
            try:
                self._unlink_file(temporary)
                self._fsync_directory()
                self._mark_active(int(row["frame_sequence"]))
            except Exception:
                # Keep the staging intent and all charges for a later recovery.
                return
        else:
            self._delete_staging_row(row)

    def _recover_orphan_reservations(self) -> None:
        rows = self._database().execute("SELECT source_reservation,journal_reservation FROM frames WHERE recording_id=?",
                                       (self.recording_id,)).fetchall()
        referenced = {value for row in rows for value in (row["source_reservation"], row["journal_reservation"])}
        referenced.update((self._source_reservation_id, self._journal_reservation_id))
        for category in ("spool", "journal"):
            for reservation in self.budget.reservations_for_owner(self.owner, category=category):
                if reservation["reservation_id"] not in referenced:
                    try:
                        self.budget.release_id(reservation["reservation_id"])
                    except Exception:
                        pass

    def recover(self, durable_proof: Callable | None = None) -> dict[str, int]:
        with self._lock:
            if self._retired:
                return self.snapshot()
            for row in self._database().execute("SELECT * FROM frames WHERE recording_id=? AND state='staging' ORDER BY frame_sequence",
                                                (self.recording_id,)).fetchall():
                self._recover_staging(row)
            if durable_proof is not None:
                rows = self._database().execute("SELECT * FROM frames WHERE recording_id=? AND state='releasable' ORDER BY frame_sequence",
                                                (self.recording_id,)).fetchall()
                groups: dict[str, list[sqlite3.Row]] = {}
                for row in rows:
                    if row["proof_digest"] and row["proof_json"]:
                        groups.setdefault(row["proof_digest"], []).append(row)
                for expected, group in groups.items():
                    tokens = tuple(self._token_from_row(row) for row in group)
                    try:
                        _, supplied = self._proof_for_tokens(tokens, durable_proof)
                        if supplied == expected:
                            self._release_rows(tuple(group))
                    except FrameSpoolError:
                        continue
            self._recover_orphan_reservations()
            return self.snapshot()

    def cleanup(self, durable_proof: Callable | None = None) -> dict[str, int]:
        return self.recover(durable_proof=durable_proof)

    def _unlink_bookkeeping(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        _require(stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_uid == os.getuid(),
                 "Refusing to unlink an unowned spool file")
        os.unlink(path)
        _require(not os.path.lexists(path), "Spool file could not be confirmed absent")

    def _bookkeeping_bytes(self) -> int:
        known = {"spool.sqlite3", "spool.sqlite3-journal", "spool.sqlite3-wal",
                 "spool.sqlite3-shm", "writer.lock"}
        total = _BOOKKEEPING_MARGIN
        for path in self.root.iterdir():
            if path.name == "frames":
                continue
            _require(path.name in known, "Unknown spool bookkeeping file")
            info = path.lstat()
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(),
                     "Invalid spool bookkeeping file")
            total += max(info.st_size, getattr(info, "st_blocks", 0) * 512)
        _require(total <= self._journal_capacity, "Spool bookkeeping exceeds its reservation")
        return total

    def finalcleanup(self, durable_proof: Callable | None = None) -> None:
        """Retire capture bytes, retaining a charged terminal identity journal.

        The lock inode and terminal identity remain stable. Reopening cannot
        create a second lifetime for this recording or replace a live lock.
        The compact metadata remains charged; source capacity is released only
        after all source entries are durably absent.
        """
        with self._lock:
            if self._closed and self._retired:
                self._assert_retired_empty()
                return
            self._assert_identity()
            if self._retired:
                self._assert_retired_empty()
                self.close()
                return
            self.recover(durable_proof=durable_proof)
            snapshot = self.snapshot()
            _require(snapshot["activeCount"] == 0 and snapshot["stagingCount"] == 0 and
                     snapshot["releasableCount"] == 0, "Frame spool still owns unreleased frames")
            frames = self.root / "frames"
            try:
                _require(next(frames.iterdir(), None) is None,
                         "Unjournaled source files prevent final spool cleanup")
                self._fsync_directory()
            except Exception as exc:
                raise FrameSpoolError("Frame spool cleanup failed; fixed charge retained") from exc
            db = self._database()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("UPDATE spool_meta SET value='1' WHERE key='cleanup_pending'")
                db.commit()
                self._cleanup_pending = 1
            except Exception:
                db.rollback()
                raise FrameSpoolError("Frame spool cleanup journal failed; fixed charge retained")
            try:
                # Secure-delete cleared released rows; compaction bounds the
                # retained terminal journal to its actual footprint.
                db.execute("VACUUM")
                db.close()
                self._db = None
                self._fsync_root()
                actual = self._bookkeeping_bytes()
                if self._source_reservation is not None:
                    self._source_reservation.close()
                    self._source_reservation = None
                _require(self._journal_reservation is not None,
                         "Spool retirement journal is unavailable")
                self._journal_reservation.commit(actual)
                self._retired = True
            except Exception as exc:
                # The terminal marker survives. A new owner may retry only
                # retirement, with all unreleased reservations still charged.
                raise FrameSpoolError("Spool retirement is incomplete; storage charge retained") from exc
            finally:
                self.close()

    def _fsync_root(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                             getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            key = (os.path.abspath(os.fspath(self.root)), self.recording_id)
            try:
                if self._db is not None:
                    self._db.close()
            finally:
                self._db = None
                if self._lock_fd is not None:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                    os.close(self._lock_fd)
                    self._lock_fd = None
                self._closed = True
                with _WRITER_GUARD:
                    _WRITERS.discard(key)

    def __enter__(self) -> "FrameSpool":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["FrameSpool", "FrameSpoolError", "SpoolToken"]
