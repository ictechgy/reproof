"""Process-safe aggregate storage reservations for durable evidence."""
from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
from functools import wraps
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import threading
import uuid

from ..core import ContractError


MAX_BYTES = 10 * 1024 * 1024 * 1024
MAX_RESERVATIONS = 100_000
_CATEGORIES = frozenset({"journal", "spool", "encoding", "finalization", "transfer"})
_OWNER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class DiskBudgetError(ContractError):
    """A bounded storage reservation cannot be honored."""


def _locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DiskBudgetError(message)


def _integer(value: object, name: str, low: int = 0, high: int = MAX_BYTES) -> int:
    _require(type(value) is int and low <= value <= high, f"Invalid {name}")
    return value


def _owned_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    stat = path.lstat()
    _require(path.is_dir() and not path.is_symlink() and stat.st_uid == os.getuid(),
             "Invalid disk budget directory")
    return path


def _metadata_headroom(capacity: int) -> int:
    return (
        max(512 * 1024, capacity // 8)
        if capacity >= 1024 * 1024
        else max(1, capacity // 16)
    )


@dataclass(slots=True)
class DiskReservation:
    reservation_id: str
    owner: str
    category: str
    bytes: int
    _budget: "DiskBudget" = field(repr=False, compare=False)
    _closed: bool = field(default=False, repr=False, compare=False)

    def commit(self, actual_bytes: int | None = None) -> None:
        self._budget.commit(self, actual_bytes=actual_bytes)

    def close(self) -> None:
        if not self._closed:
            self._budget.release(self)
            self._closed = True

    def __enter__(self) -> "DiskReservation":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class DiskBudget:
    """SQLite-backed reservations shared by every process using one root.

    Non-journal reservations can never consume the configured journal
    headroom.  The real filesystem free-space check is an additional
    conservative bound, not a claim that logical reservations own disk blocks.
    """

    def __init__(self, root: Path, *, capacity_bytes: int,
                 journal_headroom_bytes: int, free_bytes=None,
                 max_reservations: int = MAX_RESERVATIONS,
                 _allow_capacity_growth: bool = False):
        self.root = _owned_directory(Path(root))
        self.capacity_bytes = _integer(capacity_bytes, "disk capacity", 1)
        self.journal_headroom_bytes = _integer(
            journal_headroom_bytes, "journal headroom", 1, self.capacity_bytes
        )
        self.metadata_headroom_bytes = _metadata_headroom(self.capacity_bytes)
        self.max_reservations = _integer(
            max_reservations, "reservation limit", 1, MAX_RESERVATIONS
        )
        _require(type(_allow_capacity_growth) is bool,
                 "Invalid capacity migration option")
        self._allow_capacity_growth = _allow_capacity_growth
        self._free_bytes = free_bytes or (lambda: shutil.disk_usage(self.root).free)
        self._issuer = object()
        self._lock = threading.RLock()
        lock_fd = os.open(self.root / ".initialize.lock",
                          os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            database = self.root / "disk-budget.sqlite3"
            _require(not database.is_symlink()
                     and (not database.exists() or database.is_file()),
                     "Invalid disk budget database path")
            self._connection = sqlite3.connect(
                database, timeout=10, isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout=10000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA journal_size_limit=131072")
            self._connection.execute("PRAGMA wal_autocheckpoint=16")
            pages = self._connection.execute("PRAGMA max_page_count=4096").fetchone()[0]
            _require(pages <= 4096, "Disk budget database is oversized")
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

    def _initialize(self) -> None:
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS reservations (
                       reservation_id TEXT PRIMARY KEY,
                       owner TEXT NOT NULL,
                       category TEXT NOT NULL,
                       reserved_bytes INTEGER NOT NULL,
                       charged_bytes INTEGER NOT NULL,
                       state TEXT NOT NULL,
                       created_ns INTEGER NOT NULL
                   )"""
            )
            existing = dict(connection.execute("SELECT key, value FROM metadata"))
            expected = {
                "format_version": 1,
                "capacity_bytes": self.capacity_bytes,
                "journal_headroom_bytes": self.journal_headroom_bytes,
                "metadata_headroom_bytes": self.metadata_headroom_bytes,
                "max_reservations": self.max_reservations,
            }
            if existing:
                if existing != expected:
                    unchanged = (
                        set(existing) == set(expected)
                        and existing.get("format_version") == 1
                        and existing.get("journal_headroom_bytes") == self.journal_headroom_bytes
                        and existing.get("max_reservations") == self.max_reservations
                    )
                    old_capacity = existing.get("capacity_bytes")
                    old_metadata = existing.get("metadata_headroom_bytes")
                    _require(
                        self._allow_capacity_growth and unchanged
                        and type(old_capacity) is int
                        and 0 < old_capacity < self.capacity_bytes
                        and type(old_metadata) is int
                        and old_metadata == _metadata_headroom(old_capacity),
                        "Disk budget configuration mismatch",
                    )
                    rows = connection.execute(
                        "SELECT category,reserved_bytes,charged_bytes,state "
                        "FROM reservations").fetchall()
                    active = total = 0
                    for row in rows:
                        _require(row["category"] in _CATEGORIES
                                 and row["state"] in {"active", "committed"}
                                 and type(row["reserved_bytes"]) is int
                                 and type(row["charged_bytes"]) is int
                                 and 0 < row["charged_bytes"] <= row["reserved_bytes"] <= old_capacity,
                                 "Disk budget reservation metadata is corrupt")
                        total += row["charged_bytes"]
                        if row["state"] == "active":
                            active += row["charged_bytes"]
                    usable = self.capacity_bytes - self.metadata_headroom_bytes
                    _require(total <= usable
                             and self._actual_free() >= active
                             + self.journal_headroom_bytes
                             + self.metadata_headroom_bytes,
                             "Storage pressure prevents capacity growth")
                    connection.execute(
                        "UPDATE metadata SET value=? WHERE key='capacity_bytes'",
                        (self.capacity_bytes,))
                    connection.execute(
                        "UPDATE metadata SET value=? WHERE key='metadata_headroom_bytes'",
                        (self.metadata_headroom_bytes,))
            else:
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)", expected.items()
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _actual_free(self) -> int:
        try:
            value = self._free_bytes()
        except Exception:
            raise DiskBudgetError("Storage availability is unknown") from None
        return _integer(value, "available storage", 0, 2 ** 63 - 1)

    def _totals(self) -> tuple[int, int, int]:
        row = self._connection.execute(
            """SELECT COUNT(*) AS count,
                      COALESCE(SUM(charged_bytes), 0) AS total,
                      COALESCE(SUM(CASE WHEN category != 'journal'
                                       THEN charged_bytes ELSE 0 END), 0) AS media
                 FROM reservations"""
        ).fetchone()
        return int(row["count"]), int(row["total"]), int(row["media"])

    @_locked
    def reserve(self, owner: str, category: str, amount: int, *,
                idempotency_key: str | None = None) -> DiskReservation:
        _require(type(owner) is str and _OWNER.fullmatch(owner) is not None,
                 "Invalid reservation owner")
        _require(category in _CATEGORIES, "Invalid reservation category")
        amount = _integer(amount, "reservation size", 1, self.capacity_bytes)
        if idempotency_key is not None:
            _require(type(idempotency_key) is str
                     and _OWNER.fullmatch(idempotency_key) is not None,
                     "Invalid reservation idempotency key")
            identifier = "reservation_" + hashlib.sha256(
                idempotency_key.encode("ascii")).hexdigest()
        else:
            identifier = "reservation_" + uuid.uuid4().hex
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key is not None:
                existing = connection.execute(
                    "SELECT * FROM reservations WHERE reservation_id = ?",
                    (identifier,),
                ).fetchone()
                if existing is not None:
                    _require(existing["owner"] == owner
                             and existing["category"] == category
                             and existing["reserved_bytes"] == amount
                             and existing["charged_bytes"] == amount
                             and existing["state"] in {"active", "committed"},
                             "Reservation idempotency conflict")
                    connection.commit()
                    return DiskReservation(identifier, owner, category, amount, self)
            count, total, media = self._totals()
            _require(count < self.max_reservations, "Storage reservation limit reached")
            usable = self.capacity_bytes - self.metadata_headroom_bytes
            _require(total + amount <= usable, "Storage capacity exhausted")
            if category != "journal":
                _require(media + amount <= usable - self.journal_headroom_bytes,
                         "Journal headroom is reserved")
            # Active promises must fit in currently free space, with journal
            # headroom still available for truthful shutdown records.
            active = int(connection.execute(
                "SELECT COALESCE(SUM(charged_bytes), 0) FROM reservations WHERE state = 'active'"
            ).fetchone()[0])
            _require(self._actual_free() >= active + amount + self.journal_headroom_bytes
                     + self.metadata_headroom_bytes,
                     "Storage pressure prevents reservation")
            connection.execute(
                """INSERT INTO reservations
                   (reservation_id, owner, category, reserved_bytes, charged_bytes, state, created_ns)
                   VALUES (?, ?, ?, ?, ?, 'active', ?)""",
                (identifier, owner, category, amount, amount, time.monotonic_ns()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return DiskReservation(identifier, owner, category, amount, self)

    def _reservation_id(self, reservation: DiskReservation) -> str:
        _require(type(reservation) is DiskReservation and reservation._budget is self,
                 "Invalid storage reservation")
        return reservation.reservation_id

    @_locked
    def adopt(self, reservation_id: str, *, owner: str, category: str,
              minimum_bytes: int = 1) -> DiskReservation:
        _require(type(reservation_id) is str and reservation_id.startswith("reservation_"),
                 "Invalid storage reservation")
        _require(type(owner) is str and _OWNER.fullmatch(owner) is not None,
                 "Invalid reservation owner")
        _require(category in _CATEGORIES, "Invalid reservation category")
        minimum = _integer(minimum_bytes, "reservation size", 1, self.capacity_bytes)
        row = self._connection.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?", (reservation_id,)
        ).fetchone()
        _require(row is not None and row["owner"] == owner
                 and row["category"] == category and row["state"] == "active"
                 and row["reserved_bytes"] >= minimum,
                 "Storage reservation is unavailable")
        return DiskReservation(reservation_id, owner, category,
                               row["reserved_bytes"], self)

    def commit(self, reservation: DiskReservation, *, actual_bytes: int | None = None) -> None:
        identifier = self._reservation_id(reservation)
        self.commit_id(identifier, reservation.bytes if actual_bytes is None else actual_bytes)

    @_locked
    def commit_id(self, reservation_id: str, actual_bytes: int) -> None:
        _require(type(reservation_id) is str and reservation_id.startswith("reservation_"),
                 "Invalid storage reservation")
        actual = _integer(actual_bytes, "committed size", 1, self.capacity_bytes)
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT reserved_bytes, state, charged_bytes FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            _require(row is not None, "Storage reservation is unavailable")
            _require(actual <= row["reserved_bytes"], "Committed size exceeds reservation")
            if row["state"] == "committed":
                _require(row["charged_bytes"] == actual, "Committed size changed")
            else:
                _require(row["state"] == "active", "Storage reservation is unavailable")
                connection.execute(
                    "UPDATE reservations SET state = 'committed', charged_bytes = ? WHERE reservation_id = ?",
                    (actual, reservation_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def release(self, reservation: DiskReservation) -> None:
        self.release_id(self._reservation_id(reservation))

    @_locked
    def release_id(self, reservation_id: str) -> None:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "DELETE FROM reservations WHERE reservation_id = ?", (reservation_id,)
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    @_locked
    def release_owner(self, owner: str, *, active_only: bool = True) -> int:
        _require(type(owner) is str and _OWNER.fullmatch(owner) is not None,
                 "Invalid reservation owner")
        clause = " AND state = 'active'" if active_only else ""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                "DELETE FROM reservations WHERE owner = ?" + clause, (owner,)
            )
            self._connection.commit()
            return cursor.rowcount
        except Exception:
            self._connection.rollback()
            raise

    @_locked
    def reservations_for_owner(self, owner: str, *, category: str) -> tuple[dict, ...]:
        """Read bounded recovery metadata for one exact trusted storage owner."""
        _require(type(owner) is str and _OWNER.fullmatch(owner) is not None,
                 "Invalid reservation owner")
        _require(type(category) is str and category in _CATEGORIES,
                 "Invalid reservation category")
        rows = self._connection.execute(
            "SELECT reservation_id,state,charged_bytes FROM reservations "
            "WHERE owner=? AND category=? LIMIT ?",
            (owner, category, self.max_reservations + 1)).fetchall()
        _require(len(rows) <= self.max_reservations, "Storage reservation limit reached")
        return tuple(dict(row) for row in rows)

    @_locked
    def check_health(self) -> None:
        active = int(self._connection.execute(
            "SELECT COALESCE(SUM(charged_bytes), 0) FROM reservations WHERE state = 'active'"
        ).fetchone()[0])
        _require(self._actual_free() >= active + self.journal_headroom_bytes
                 + self.metadata_headroom_bytes,
                 "Storage pressure prevents durable shutdown")

    @_locked
    def snapshot(self) -> dict[str, int]:
        count, total, media = self._totals()
        return {"reservations": count, "chargedBytes": total,
                "mediaBytes": media,
                "journalHeadroomBytes": self.journal_headroom_bytes,
                "metadataHeadroomBytes": self.metadata_headroom_bytes}

    @_locked
    def close(self) -> None:
        self._connection.close()


__all__ = ["DiskBudget", "DiskBudgetError", "DiskReservation"]
