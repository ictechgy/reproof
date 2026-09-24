"""Trusted G2 recording lifecycle, collection policy, and immutable freeze."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from .. import contracts
from ..core import ContractError
from ..app_logs import APP_LOG_MIME
from .clock_sync import (
    ClockSynchronizer,
    ProviderClockBinding,
    RecordingClockError,
    RecordingStamp,
    RecordingTimeAnchor,
)
from .disk_budget import DiskBudgetError, DiskReservation
from .evidence_store import (
    EvidencePin,
    EvidenceStore,
    EvidenceStoreError,
    OBJECT_METADATA_BYTES,
)
from .video_sources import MAX_SOURCE_FRAMES


MAX_RECORDINGS = 10_000
MAX_RECORDING_DURATION_MS = 600_000
MAX_EVENTS = 512
MAX_FRAMES = 256
MAX_VIDEO_ARTIFACTS = 33
MAX_OBSERVATIONS = 512
MAX_SAMPLES = 1024
MAX_GAPS = 1024
MAX_LIFECYCLE_RECEIPTS = 2048
MAX_REGISTERED_PROJECT_BYTES = 64 * 1024
REGISTRATION_METADATA_BYTES = 16 * 1024
JOURNAL_RESERVATION_BYTES = 8 * 1024 * 1024
MAX_INPUT_JOURNAL_BYTES = 256 * 1024
FINALIZATION_RESERVATION_BYTES = 4 * 1024 * 1024 + OBJECT_METADATA_BYTES
SOURCE_JOURNAL_RESERVATION_BYTES = 16 * 1024 * 1024
ORIGINAL_FRAME_MODE = "original-cas-v1"
VIDEO_SOURCE_MODE = "transient-spool-v2"
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COLLECTION_KINDS = frozenset({"pixels", "text", "accessibility", "logs"})
_CAPTURE_MODES = frozenset({"sample-bound", "test-data", "suppressed"})
_RETENTION_KEYS = frozenset({"original", "intermediate", "derivative", "export"})


class RecordingStoreError(ContractError):
    """A release recording cannot be durably or safely represented."""


class RecordingDurationError(RecordingStoreError):
    """The natural acquisition window has ended; no new effect was admitted."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RecordingStoreError(message)


def _identifier(value: object, name: str) -> str:
    _require(type(value) is str and _ID.fullmatch(value) is not None, f"Invalid {name}")
    return value


def _digest(value: object, name: str = "digest") -> str:
    _require(type(value) is str and _DIGEST.fullmatch(value) is not None,
             f"Invalid {name}")
    return value


def _integer(value: object, name: str, low: int = 0,
             high: int = 2 ** 63 - 1) -> int:
    _require(type(value) is int and low <= value <= high, f"Invalid {name}")
    return value


def _json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeError):
        raise RecordingStoreError("Invalid recording data") from None


def _load(value: str):
    try:
        return json.loads(value)
    except (TypeError, ValueError, UnicodeError):
        raise RecordingStoreError("Recording journal is corrupt") from None


def validate_collection_policy(value):
    _require(type(value) is dict
             and set(value) == {"schemaVersion", "captureMode", "retentionSeconds"},
             "Invalid collection policy")
    _require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1,
             "Unsupported collection policy")
    _require(value["captureMode"] in _CAPTURE_MODES, "Invalid capture mode")
    retention = value["retentionSeconds"]
    _require(type(retention) is dict and set(retention) == _RETENTION_KEYS,
             "Invalid retention policy")
    for key in _RETENTION_KEYS:
        _integer(retention[key], f"{key} retention", 1, 3650 * 86400)
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class SampleClassification:
    kind: str
    sample_id: str
    sample_digest: str
    provider_incarnation: str
    native_incarnation: str
    acquisition_sequence: int
    decision: str
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class TrustedProjectRegistration:
    _project_json: str
    project_digest: str
    _policy_json: str
    _store_issuer: object = field(repr=False, compare=False)
    _classification_issuer: object = field(repr=False, compare=False)

    @property
    def project(self):
        return _load(self._project_json)

    @property
    def collection_policy(self):
        return _load(self._policy_json)

    def classify_sample(self, *, kind, sample_id, sample_digest,
                        provider_incarnation, native_incarnation,
                        acquisition_sequence, decision):
        _require(kind in _COLLECTION_KINDS, "Invalid collection kind")
        _identifier(sample_id, "sample identity")
        _digest(sample_digest, "sample digest")
        _identifier(provider_incarnation, "provider incarnation")
        _identifier(native_incarnation, "native incarnation")
        _integer(acquisition_sequence, "acquisition sequence", 1)
        _require(decision in {"approved", "sensitive"}, "Invalid sample decision")
        _require(self.collection_policy["captureMode"] == "sample-bound",
                 "Sample classification is not configured")
        return SampleClassification(
            kind, sample_id, sample_digest, provider_incarnation,
            native_incarnation, acquisition_sequence, decision,
            self._classification_issuer,
        )


@dataclass(frozen=True, slots=True)
class FramePublication:
    digest: str
    bytes: int
    path: str
    mime_type: str
    width: int
    height: int
    orientation: str
    acquisition_sequence: int
    stamp: RecordingStamp
    timing_source: str
    recording_frame_sequence: int | None = None
    source_token: object = field(default=None, repr=False, compare=False)


class RecordingStore:
    """One bounded SQLite journal and its process-local trust registry."""

    def __init__(self, root: Path, evidence: EvidenceStore,
                 clock_sync: ClockSynchronizer, *, wall_clock_ms=None,
                 max_recordings: int = MAX_RECORDINGS):
        _require(type(evidence) is EvidenceStore, "Evidence store is required")
        _require(type(clock_sync) is ClockSynchronizer,
                 "Trusted clock synchronizer is required")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stat = self.root.lstat()
        _require(self.root.is_dir() and not self.root.is_symlink()
                 and stat.st_uid == os.getuid(), "Invalid recording store directory")
        self.evidence = evidence
        self.clock_sync = clock_sync
        self.wall_clock_ms = wall_clock_ms
        self.max_recordings = _integer(max_recordings, "recording limit", 1,
                                       MAX_RECORDINGS)
        self._issuer = object()
        self._lock = threading.RLock()
        self._active: dict[str, RecordingSession] = {}
        self._registered_digests: set[str] = set()
        self._owner_fd = os.open(
            self.root / ".writer.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
        try:
            fcntl.flock(self._owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._owner_fd)
            self._owner_fd = None
            raise RecordingStoreError("Recording store already has a live owner") from None
        database = self.root / "recordings.sqlite3"
        try:
            _require(not database.is_symlink()
                     and (not database.exists() or database.is_file()),
                     "Invalid recording database path")
            self._connection = sqlite3.connect(
                database, timeout=10, isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=10000")
            self._connection.execute("PRAGMA journal_size_limit=262144")
            self._connection.execute("PRAGMA wal_autocheckpoint=32")
            self._connection.execute("PRAGMA secure_delete=ON")
            pages = self._connection.execute("PRAGMA max_page_count=32768").fetchone()[0]
            _require(pages <= 32768, "Recording journal database is oversized")
            self._initialize()
            self._reconcile_registrations()
            self._recover_unfinished()
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
                self._connection = None
            fcntl.flock(self._owner_fd, fcntl.LOCK_UN)
            os.close(self._owner_fd)
            self._owner_fd = None
            raise

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS registered_projects (
                           project_id TEXT NOT NULL,
                           revision TEXT NOT NULL,
                           project_digest TEXT NOT NULL,
                           project_json TEXT NOT NULL,
                           policy_json TEXT NOT NULL,
                           PRIMARY KEY(project_id, revision)
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS recordings (
                           recording_id TEXT PRIMARY KEY,
                           session_id TEXT NOT NULL UNIQUE,
                           project_json TEXT NOT NULL,
                           project_digest TEXT NOT NULL,
                           policy_json TEXT NOT NULL,
                           application_id TEXT NOT NULL,
                           build_id TEXT NOT NULL,
                           provider_incarnation TEXT NOT NULL,
                           device_identity_digest TEXT NOT NULL,
                           preparation_json TEXT NOT NULL,
                           started_at_ms INTEGER NOT NULL,
                           anchor_clock_id TEXT NOT NULL,
                           anchor_boot_digest TEXT NOT NULL,
                           anchor_ns INTEGER NOT NULL,
                           anchor_uncertainty_ns INTEGER NOT NULL,
                           state TEXT NOT NULL,
                           barrier_sequence INTEGER,
                           barrier_media_sequence INTEGER,
                           barrier_observation_sequence INTEGER,
                           barrier_gap_sequence INTEGER,
                           barrier_offset_ms INTEGER,
                           interruption_reason TEXT,
                           original_json TEXT,
                           recording_digest TEXT,
                           original_object_digest TEXT,
                           journal_reservation_id TEXT NOT NULL,
                           finalization_reservation_id TEXT NOT NULL,
                           lifecycle_sequence INTEGER NOT NULL DEFAULT 0
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS events (
                           recording_id TEXT NOT NULL,
                           sequence INTEGER NOT NULL,
                           event_id TEXT NOT NULL,
                           operation_id TEXT NOT NULL,
                           generation INTEGER NOT NULL,
                           provider_incarnation TEXT NOT NULL,
                           offset_ms INTEGER NOT NULL,
                           input_json TEXT NOT NULL,
                           dispatch_started INTEGER NOT NULL,
                           dispatch TEXT NOT NULL,
                           receipt_json TEXT,
                           PRIMARY KEY(recording_id, sequence),
                           UNIQUE(recording_id, operation_id),
                           FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS media (
                           recording_id TEXT NOT NULL,
                           sequence INTEGER NOT NULL,
                           reference_json TEXT NOT NULL,
                           timing_json TEXT NOT NULL,
                           pin_id TEXT NOT NULL,
                           PRIMARY KEY(recording_id, sequence),
                           FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS observations (
                           recording_id TEXT NOT NULL,
                           sequence INTEGER NOT NULL,
                           reference_json TEXT NOT NULL,
                           PRIMARY KEY(recording_id, sequence),
                           FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
                       )"""
                )
                columns = {item[1] for item in connection.execute("PRAGMA table_info(recordings)")}
                for name, definition in (
                    ("source_mode", "TEXT NOT NULL DEFAULT 'original-cas-v1'"),
                    ("barrier_source_sequence", "INTEGER"),
                    ("source_manifest_digest", "TEXT"),
                    ("source_journal_reservation_id", "TEXT"),
                    ("source_config_json", "TEXT"),
                    ("source_config_digest", "TEXT"),
                    ("source_cleanup_complete", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if name not in columns:
                        connection.execute(f"ALTER TABLE recordings ADD COLUMN {name} {definition}")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS frame_acquisitions (
                           recording_id TEXT NOT NULL,
                           sequence INTEGER NOT NULL,
                           acquisition_sequence INTEGER NOT NULL,
                           source_json TEXT NOT NULL,
                           PRIMARY KEY(recording_id, sequence),
                           UNIQUE(recording_id, acquisition_sequence),
                           FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS gaps (
                           recording_id TEXT NOT NULL,
                           sequence INTEGER NOT NULL,
                           offset_ms INTEGER NOT NULL,
                           reason TEXT NOT NULL,
                           PRIMARY KEY(recording_id, sequence),
                           FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS samples (
                           recording_id TEXT NOT NULL,
                           kind TEXT NOT NULL,
                           provider_incarnation TEXT NOT NULL,
                           native_incarnation TEXT NOT NULL,
                           acquisition_sequence INTEGER NOT NULL,
                           sample_id TEXT,
                           sample_digest TEXT,
                           decision TEXT NOT NULL,
                           PRIMARY KEY(recording_id, kind, provider_incarnation,
                                       acquisition_sequence),
                           FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS pending_lifecycle (
                           recording_id TEXT NOT NULL,
                           pending_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                           operation_id TEXT NOT NULL,
                           generation INTEGER NOT NULL,
                           kind TEXT NOT NULL,
                           status TEXT NOT NULL,
                           observed_at_ms INTEGER NOT NULL,
                           FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS lifecycle (
                           recording_id TEXT NOT NULL,
                           sequence INTEGER NOT NULL,
                           receipt_json TEXT NOT NULL,
                           PRIMARY KEY(recording_id, sequence),
                           FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
                       )"""
                )
                existing = dict(connection.execute("SELECT key, value FROM metadata"))
                expected = {"format_version": 2, "max_recordings": self.max_recordings}
                if existing == {"format_version": 1, "max_recordings": self.max_recordings}:
                    connection.execute("UPDATE metadata SET value=2 WHERE key='format_version'")
                    existing = expected
                if existing:
                    _require(existing == expected, "Recording store configuration mismatch")
                else:
                    connection.executemany(
                        "INSERT INTO metadata(key, value) VALUES (?, ?)", expected.items()
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _registration_reservation(self, project_id, revision, project_json, policy_json):
        try:
            payload_bytes = len(project_json.encode("utf-8")) + len(policy_json.encode("utf-8"))
        except UnicodeError:
            raise RecordingStoreError("Project recording policy encoding is invalid") from None
        _require(payload_bytes <= MAX_REGISTERED_PROJECT_BYTES,
                 "Project recording policy is too large")
        # Account for the stored strings, page/index growth and the reservation
        # row. The shared metadata headroom separately covers transient WAL.
        amount = 4 * payload_bytes + REGISTRATION_METADATA_BYTES
        identity = _json([str(self.root.resolve()), project_id, revision]).encode("utf-8")
        key = "project_" + hashlib.sha256(identity).hexdigest()
        try:
            reservation = self.evidence.budget.reserve(
                key, "journal", amount, idempotency_key=key)
            reservation.commit()
            return reservation
        except DiskBudgetError:
            raise RecordingStoreError("Project metadata reservation is unavailable") from None

    def _reconcile_registrations(self):
        rows = self._connection.execute(
            "SELECT project_id, revision, project_json, policy_json FROM registered_projects"
        ).fetchall()
        for row in rows:
            self._registration_reservation(
                row["project_id"], row["revision"], row["project_json"], row["policy_json"])

    def register_project(self, project_wire, collection_policy):
        try:
            project = contracts.validate_project_revision(project_wire)
            policy = validate_collection_policy(collection_policy)
        except (ContractError, TypeError, ValueError):
            raise RecordingStoreError("Project recording policy is invalid") from None
        project_json = _json(project)
        policy_json = _json(policy)
        project_digest = contracts.digest(project)
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """SELECT project_digest, project_json, policy_json
                         FROM registered_projects
                        WHERE project_id = ? AND revision = ?""",
                    (project["id"], project["revision"]),
                ).fetchone()
                if row is None:
                    _require(int(connection.execute(
                        "SELECT COUNT(*) FROM registered_projects"
                    ).fetchone()[0]) < MAX_RECORDINGS,
                             "Project registration limit reached")
                    # A crash between these two commits conservatively retains
                    # the charge. The same revision reuses its stable key.
                    self._registration_reservation(
                        project["id"], project["revision"], project_json, policy_json)
                    connection.execute(
                        """INSERT INTO registered_projects
                           (project_id, revision, project_digest, project_json, policy_json)
                           VALUES (?, ?, ?, ?, ?)""",
                        (project["id"], project["revision"], project_digest,
                         project_json, policy_json),
                    )
                else:
                    _require(row["project_digest"] == project_digest
                             and row["project_json"] == project_json
                             and row["policy_json"] == policy_json,
                             "Registered project revision changed")
                    self._registration_reservation(
                        project["id"], project["revision"], project_json, policy_json)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            self._registered_digests.add(project_digest)
        return TrustedProjectRegistration(
            project_json, project_digest, policy_json, self._issuer, object()
        )

    def _require_registration(self, registration):
        _require(type(registration) is TrustedProjectRegistration
                 and registration._store_issuer is self._issuer,
                 "Trusted project registration is required")
        return registration

    def _after_durable_boundary(self, boundary):
        """Test seam for process-crash probes; production performs no callback."""

    def begin_recording(self, registration, *, recording_id, session_id,
                        application_id, build_id, device_identity,
                        provider_incarnation, preparation_receipts,candidate_binding=None):
        registration = self._require_registration(registration)
        recording_id = _identifier(recording_id, "recording identity")
        session_id = _identifier(session_id, "session identity")
        application_id = _identifier(application_id, "application identity")
        build_id = _identifier(build_id, "build identity")
        provider_incarnation = _identifier(provider_incarnation, "provider incarnation")
        project = registration.project
        applications = {item["id"]: item for item in project["applications"]}
        builds = {item["id"]: item for item in project["builds"]}
        if candidate_binding is not None:
            from ..qualification import require_candidate_binding
            builds[build_id] = require_candidate_binding(candidate_binding,
                project_digest=registration.project_digest, application_id=application_id, build_id=build_id)
        _require(application_id in applications and build_id in builds
                 and builds[build_id]["applicationId"] == application_id,
                 "Application build is not registered")
        _require(type(device_identity) is dict
                 and type(device_identity.get("bundle")) is str
                 and _DIGEST.fullmatch(device_identity.get("artifactDigest", "")) is not None
                 and device_identity["bundle"] == applications[application_id]["bundle"]
                 and device_identity["artifactDigest"] == builds[build_id]["artifactDigest"],
                 "Installed application build does not match registration")
        _require(type(preparation_receipts) in (list, tuple)
                 and len(preparation_receipts) <= 128,
                 "Invalid preparation receipts")
        if preparation_receipts:
            _require(project["evidencePolicy"]["fixtures"] is True,
                     "Preparation evidence collection is disabled")
        anchor = RecordingTimeAnchor(self.clock_sync, wall_clock_ms=self.wall_clock_ms)
        preparations = copy.deepcopy(list(preparation_receipts))
        _require(all(type(item) is dict
                     and type(item.get("receiptId")) is str
                     and type(item.get("recipeId")) is str
                     and type(item.get("operation")) is str
                     for item in preparations),
                 "Invalid preparation receipts")
        registered_recipes = {
            item["id"]: item for item in project["fixtures"] + project["recipes"]
        }
        _require(len({item.get("receiptId") for item in preparations})
                 == len(preparations), "Duplicate preparation receipt")
        _require(all(
            item.get("recipeId") in registered_recipes
            and registered_recipes[item["recipeId"]]["operation"] == item.get("operation")
            for item in preparations
        ), "Preparation receipt is not registered")
        probe = {
            "schemaVersion": 1,
            "recordingId": recording_id,
            "projectId": project["id"],
            "projectRevision": project["revision"],
            "applicationId": application_id,
            "buildId": build_id,
            "startedAtMs": anchor.started_at_ms,
            "endSequence": 0,
            "events": [],
            "preparation": preparations,
            "observations": [],
            "media": [],
            "clock": {"source": "monotonic", "uncertaintyMs": 0},
            "interruptions": [],
            "unknowns": [],
            "sealed": True,
        }
        try:
            contracts.validate_original_evidence(probe)
        except ContractError:
            raise RecordingStoreError("Preparation receipts are invalid") from None
        _require(all(item["status"] == "complete" for item in preparations),
                 "Preparation did not complete")
        reservation = self.evidence.budget.reserve(
            recording_id, "journal", JOURNAL_RESERVATION_BYTES
        )
        try:
            finalization_reservation = self.evidence.budget.reserve(
                recording_id, "finalization", FINALIZATION_RESERVATION_BYTES
            )
        except Exception:
            reservation.close()
            raise
        identity_committed = False
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                _require(int(connection.execute(
                    "SELECT COUNT(*) FROM recordings"
                ).fetchone()[0]) < self.max_recordings, "Recording limit reached")
                connection.execute(
                    """INSERT INTO recordings
                       (recording_id, session_id, project_json, project_digest, policy_json,
                        application_id, build_id, provider_incarnation,
                        device_identity_digest, preparation_json, started_at_ms,
                        anchor_clock_id, anchor_boot_digest, anchor_ns,
                        anchor_uncertainty_ns, state, journal_reservation_id,
                        finalization_reservation_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               'preparing', ?, ?)""",
                    (recording_id, session_id, _json(project), registration.project_digest,
                     _json(registration.collection_policy), application_id, build_id,
                     provider_incarnation, contracts.digest(device_identity),
                     _json([]), anchor.started_at_ms,
                     anchor._anchor.clock_id, anchor._anchor.boot_digest,
                     anchor._anchor.nanoseconds, anchor._anchor.uncertainty_ns,
                     reservation.reservation_id, finalization_reservation.reservation_id),
                )
                connection.commit()
                identity_committed = True
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                reservation.close()
                finalization_reservation.close()
                raise
        self._after_durable_boundary("identity")
        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """UPDATE recordings SET preparation_json = ?, state = 'recording'
                         WHERE recording_id = ? AND state = 'preparing'""",
                    (_json(preparations), recording_id),
                )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                if not identity_committed:
                    reservation.close()
                    finalization_reservation.close()
                raise
        self._after_durable_boundary("preparation")
        session = RecordingSession(self, recording_id, registration, anchor, reservation)
        with self._lock:
            self._active[recording_id] = session
        return session

    def _recover_unfinished(self) -> None:
        with self._lock:
            rows = self._connection.execute(
                "SELECT recording_id, state FROM recordings WHERE state IN ('preparing','recording','finalizing')"
            ).fetchall()
        for row in rows:
            recording_id = row["recording_id"]
            with self._lock:
                connection = self._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    current = connection.execute(
                        "SELECT state, started_at_ms FROM recordings WHERE recording_id = ?",
                        (recording_id,),
                    ).fetchone()
                    if current is None or current["state"].startswith("frozen"):
                        connection.commit()
                        continue
                    if current["state"] != "finalizing":
                        last = connection.execute(
                            "SELECT COALESCE(MAX(sequence), 0), COALESCE(MAX(offset_ms), 0) FROM events WHERE recording_id = ?",
                            (recording_id,),
                        ).fetchone()
                        # Recovery cannot sample the old process's clock. Use
                        # the latest committed acquisition bound, including a
                        # frame committed before the video worker saw it.
                        known_offset = int(last[1])
                        for table, column in (("media", "timing_json"),
                                              ("frame_acquisitions", "source_json")):
                            for saved in connection.execute(
                                    f"SELECT {column} FROM {table} WHERE recording_id=?",
                                    (recording_id,)):
                                timing = _load(saved[0])
                                if table == "frame_acquisitions":
                                    timing = timing["timing"]
                                known_offset = max(known_offset, timing["latestOffsetMs"])
                        connection.execute(
                            """UPDATE recordings
                                  SET state = 'finalizing', barrier_sequence = ?,
                                      barrier_media_sequence =
                                          (SELECT COALESCE(MAX(sequence), 0) FROM media WHERE recording_id = ?),
                                      barrier_observation_sequence =
                                          (SELECT COALESCE(MAX(sequence), 0) FROM observations WHERE recording_id = ?),
                                      barrier_gap_sequence =
                                          (SELECT COALESCE(MAX(sequence), 0) FROM gaps WHERE recording_id = ?),
                                      barrier_source_sequence =
                                          (SELECT COALESCE(MAX(sequence), 0) FROM frame_acquisitions WHERE recording_id = ?),
                                      barrier_offset_ms = ?,
                                      interruption_reason = 'process_restart'
                                WHERE recording_id = ?""",
                            (int(last[0]), recording_id, recording_id, recording_id, recording_id,
                             known_offset, recording_id),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            with self._lock:
                current = self._recording_row(recording_id)
            finalization_id = current["finalization_reservation_id"]
            self.evidence.abandon_reservation(finalization_id, release=False)
            self.evidence.abandon_owner(
                recording_id, exclude_reservation_id=finalization_id)
            if current["source_mode"] == VIDEO_SOURCE_MODE and current["source_manifest_digest"] is None:
                # The video catalog must account for every admitted source
                # before a process-interrupted v2 original can be frozen.
                continue
            self._freeze_id(recording_id, recovery=True)

    def _recording_row(self, recording_id):
        # Binding checks also reach this helper outside recording mutations.
        # Serialize the shared SQLite connection through execute and fetch.
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM recordings WHERE recording_id = ?", (recording_id,)
            ).fetchone()
        _require(row is not None, "Recording is unavailable")
        return row

    def _original(self, recording_id):
        row = self._recording_row(recording_id)
        project = _load(row["project_json"])
        events = []
        for event in self._connection.execute(
            "SELECT * FROM events WHERE recording_id = ? AND sequence <= ? ORDER BY sequence",
            (recording_id, row["barrier_sequence"]),
        ):
            receipt = None if event["receipt_json"] is None else _load(event["receipt_json"])
            dispatch = event["dispatch"]
            events.append({
                "id": event["event_id"],
                "operationId": event["operation_id"],
                "generation": event["generation"],
                "sequence": event["sequence"],
                "offsetMs": event["offset_ms"],
                "input": _load(event["input_json"]),
                "dispatch": dispatch,
                "receipt": receipt,
                "provenance": {"kind": "injected" if dispatch == "injected" else "observed",
                               "source": "host"},
            })
        media_rows = self._connection.execute(
            """SELECT reference_json, timing_json FROM media
                 WHERE recording_id = ? AND sequence <= ? ORDER BY sequence""",
            (recording_id, row["barrier_media_sequence"]),
        ).fetchall()
        media = [_load(item["reference_json"]) for item in media_rows]
        observations = [_load(item[0]) for item in self._connection.execute(
            """SELECT reference_json FROM observations
                 WHERE recording_id = ? AND sequence <= ? ORDER BY sequence""",
            (recording_id, row["barrier_observation_sequence"]),
        )]
        interruptions = []
        seen = set()
        for gap in self._connection.execute(
            """SELECT offset_ms, reason FROM gaps
                 WHERE recording_id = ? AND sequence <= ? ORDER BY sequence""",
            (recording_id, row["barrier_gap_sequence"]),
        ):
            item = (row["started_at_ms"] + gap["offset_ms"], gap["reason"])
            if item not in seen:
                interruptions.append({"startMs": item[0], "endMs": item[0], "reason": item[1]})
                seen.add(item)
        if row["interruption_reason"] is not None:
            item = (row["started_at_ms"] + (row["barrier_offset_ms"] or 0),
                    row["interruption_reason"])
            if item not in seen:
                interruptions.append({"startMs": item[0], "endMs": item[0], "reason": item[1]})
        unknowns = [{"kind": "input_outcome", "sequence": event["sequence"]}
                    for event in events if event["dispatch"] == "unknown"]
        if any(item['reason'] == 'preparation_unknown' for item in interruptions):
            unknowns.append({'kind': 'preparation_unknown', 'sequence': 0})
        media_uncertainty = max(
            [_load(item["timing_json"])["uncertaintyNs"] for item in media_rows] or [0]
        )
        uncertainty = (max(row["anchor_uncertainty_ns"], media_uncertainty) + 999_999) // 1_000_000
        return {
            "schemaVersion": 1,
            "recordingId": recording_id,
            "projectId": project["id"],
            "projectRevision": project["revision"],
            "applicationId": row["application_id"],
            "buildId": row["build_id"],
            "startedAtMs": row["started_at_ms"],
            "endSequence": len(events),
            "events": events,
            "preparation": _load(row["preparation_json"]),
            "observations": observations,
            "media": media,
            "clock": {"source": "monotonic", "uncertaintyMs": uncertainty},
            "interruptions": interruptions,
            "unknowns": unknowns,
            "sealed": True,
        }

    def _append_lifecycle_locked(self, connection, row, *, operation_id,
                                 generation, kind, status, observed_at_ms):
        existing_receipts = connection.execute(
            "SELECT sequence, receipt_json FROM lifecycle WHERE recording_id = ? ORDER BY sequence",
            (row["recording_id"],),
        ).fetchall()
        for existing_row in existing_receipts:
            existing = _load(existing_row["receipt_json"])
            if existing["operationId"] == operation_id and existing["kind"] == kind:
                _require(existing["generation"] == generation
                         and existing["status"] == status,
                         "Lifecycle receipt conflicts with durable outcome")
                return existing_row["sequence"]
        _require(len(existing_receipts) < MAX_LIFECYCLE_RECEIPTS,
                 "Lifecycle receipt limit reached")
        sequence = row["lifecycle_sequence"] + 1
        receipt = {
            "schemaVersion": 1,
            "receiptId": f"lifecycle_{sequence}",
            "recordingDigest": row["recording_digest"],
            "operationId": operation_id,
            "generation": generation,
            "sequence": sequence,
            "kind": kind,
            "status": status,
            "observedAtMs": observed_at_ms,
        }
        contracts.validate_lifecycle_receipt(receipt)
        connection.execute(
            "INSERT INTO lifecycle(recording_id, sequence, receipt_json) VALUES (?, ?, ?)",
            (row["recording_id"], sequence, _json(receipt)),
        )
        connection.execute(
            "UPDATE recordings SET lifecycle_sequence = ? WHERE recording_id = ?",
            (sequence, row["recording_id"]),
        )
        return sequence

    def _freeze_id(self, recording_id, *, recovery=False):
        recording_id = _identifier(recording_id, "recording identity")
        with self._lock:
            row = self._recording_row(recording_id)
            if row["recording_digest"] is not None:
                return self.load(recording_id)
            _require(row["state"] == "finalizing", "Recording stop barrier is missing")
            _require(row["source_mode"] == ORIGINAL_FRAME_MODE
                     or (row["source_mode"] == VIDEO_SOURCE_MODE and row["source_manifest_digest"] is not None),
                     "Video source outcomes have not been durably attached")
            if row["source_mode"] == VIDEO_SOURCE_MODE:
                self.evidence.budget.commit_id(row["source_journal_reservation_id"],
                                              SOURCE_JOURNAL_RESERVATION_BYTES)
            original = self._original(recording_id)
        try:
            original = contracts.validate_original_evidence(original)
        except ContractError:
            raise RecordingStoreError("Frozen original is invalid") from None
        body = _json(original).encode("utf-8")
        _require(len(body) + OBJECT_METADATA_BYTES <= FINALIZATION_RESERVATION_BYTES
                 and len(body) <= JOURNAL_RESERVATION_BYTES,
                 "Frozen original exceeds reserved metadata capacity")
        digest = contracts.digest(original)
        _require(hashlib.sha256(body).hexdigest() == digest,
                 "Original digest encoding mismatch")
        policy = _load(row["policy_json"])
        retain_until = int(time.time() * 1000) + policy["retentionSeconds"]["original"] * 1000
        try:
            reference = self.evidence.lookup(digest)
            if reference is None:
                finalization = self.evidence.budget.adopt(
                    row["finalization_reservation_id"], owner=recording_id,
                    category="finalization", minimum_bytes=len(body))
                reference = self.evidence.put_bytes(
                    body, owner=recording_id, retention_class="original",
                    retain_until_ms=retain_until, reservation=finalization,
                )
            elif not self.evidence.uses_reservation(
                    digest, row["finalization_reservation_id"]):
                self.evidence.budget.release_id(row["finalization_reservation_id"])
        except (EvidenceStoreError, DiskBudgetError):
            raise RecordingStoreError("Original publication failed") from None
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._recording_row(recording_id)
                if row["recording_digest"] is None:
                    incomplete = bool(
                        recovery or original["interruptions"] or original["unknowns"])
                    state = "frozen-incomplete" if incomplete else "frozen-complete"
                    connection.execute(
                        """UPDATE recordings
                              SET state = ?, original_json = ?, recording_digest = ?,
                                  original_object_digest = ?
                            WHERE recording_id = ? AND state = 'finalizing'
                              AND recording_digest IS NULL""",
                        (state, _json(original), digest, reference.digest, recording_id),
                    )
                    row = self._recording_row(recording_id)
                    self._append_lifecycle_locked(
                        connection, row, operation_id="recording_stop", generation=1,
                        kind="stop", status="failed" if incomplete else "complete",
                        observed_at_ms=original["startedAtMs"] + (row["barrier_offset_ms"] or 0),
                    )
                    row = self._recording_row(recording_id)
                    pending = connection.execute(
                        "SELECT * FROM pending_lifecycle WHERE recording_id = ? ORDER BY pending_sequence",
                        (recording_id,),
                    ).fetchall()
                    for item in pending:
                        self._append_lifecycle_locked(
                            connection, row, operation_id=item["operation_id"],
                            generation=item["generation"], kind=item["kind"],
                            status=item["status"], observed_at_ms=item["observed_at_ms"],
                        )
                        row = self._recording_row(recording_id)
                    connection.execute(
                        "DELETE FROM pending_lifecycle WHERE recording_id = ?", (recording_id,)
                    )
                    if recovery:
                        row = self._recording_row(recording_id)
                        self._append_lifecycle_locked(
                            connection, row, operation_id="recording_recovery",
                            generation=1, kind="reconcile", status="failed",
                            observed_at_ms=(original["startedAtMs"]
                                            + (row["barrier_offset_ms"] or 0)),
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        pin_id = "recording_" + recording_id
        try:
            self.evidence.unpin_id(pin_id)
        except EvidenceStoreError:
            pass
        self.evidence.budget.commit_id(
            row["journal_reservation_id"], JOURNAL_RESERVATION_BYTES)
        return self.load(recording_id)

    def load(self, recording_id):
        recording_id = _identifier(recording_id, "recording identity")
        with self._lock:
            row = self._recording_row(recording_id)
            if (row["recording_digest"] is not None
                    and self.evidence.is_tombstoned(row["recording_digest"])):
                connection = self._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for table in (
                        "events", "media", "observations", "samples", "gaps", "frame_acquisitions",
                        "pending_lifecycle",
                    ):
                        if (table == "frame_acquisitions"
                                and row["source_mode"] == VIDEO_SOURCE_MODE
                                and row["source_cleanup_complete"] == 0):
                            # The expired original stays unavailable, but the
                            # source identities still authorize pending byte cleanup.
                            continue
                        connection.execute(
                            f"DELETE FROM {table} WHERE recording_id = ?",
                            (recording_id,),
                        )
                    connection.execute(
                        """UPDATE recordings
                              SET original_json = NULL, preparation_json = '[]'
                            WHERE recording_id = ?""",
                        (recording_id,),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                row = self._recording_row(recording_id)
            original = None if row["original_json"] is None else _load(row["original_json"])
            if original is not None:
                try:
                    contracts.validate_original_evidence(original)
                    _require(contracts.digest(original) == row["recording_digest"]
                             == row["original_object_digest"],
                             "Frozen original digest mismatch")
                    _require(self.evidence.read(row["original_object_digest"])
                             == _json(original).encode("utf-8"),
                             "Frozen original bytes changed")
                except (ContractError, EvidenceStoreError):
                    raise RecordingStoreError("Frozen original is unavailable") from None
            lifecycle = [_load(item[0]) for item in self._connection.execute(
                "SELECT receipt_json FROM lifecycle WHERE recording_id = ? ORDER BY sequence",
                (recording_id,),
            )]
            try:
                for sequence, receipt in enumerate(lifecycle, 1):
                    contracts.validate_lifecycle_receipt(receipt)
                    _require(receipt["sequence"] == sequence
                             and receipt["recordingDigest"] == row["recording_digest"],
                             "Lifecycle receipt binding mismatch")
            except ContractError:
                raise RecordingStoreError("Lifecycle receipt journal is corrupt") from None
            return {
                "recordingId": recording_id,
                "status": row["state"],
                "recordingDigest": row["recording_digest"],
                "objectDigest": row["original_object_digest"],
                "original": original,
                "lifecycleReceipts": lifecycle,
            }

    def append_lifecycle(self, recording_id, *, operation_id, generation,
                         kind, status, observed_at_ms=None):
        """Append post-stop evidence without changing the frozen original."""
        recording_id = _identifier(recording_id, "recording identity")
        operation_id = _identifier(operation_id, "operation identity")
        generation = _integer(generation, "generation", 1)
        _require(kind in {"cleanup", "quarantine", "reconcile"},
                 "Invalid lifecycle kind")
        _require(status in {"complete", "failed", "unknown"},
                 "Invalid lifecycle status")
        if observed_at_ms is None:
            observed_at_ms = int(time.time() * 1000)
        observed_at_ms = _integer(observed_at_ms, "lifecycle time", 0,
                                  32_503_680_000_000)
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._recording_row(recording_id)
                _require(row["recording_digest"] is not None
                         and row["state"] in {"frozen-complete", "frozen-incomplete"},
                         "Frozen original is required")
                sequence = self._append_lifecycle_locked(
                    connection, row, operation_id=operation_id,
                    generation=generation, kind=kind, status=status,
                    observed_at_ms=max(observed_at_ms, row["started_at_ms"]))
                connection.commit()
                receipt = connection.execute(
                    "SELECT receipt_json FROM lifecycle WHERE recording_id=? AND sequence=?",
                    (recording_id, sequence)).fetchone()
                return copy.deepcopy(_load(receipt[0]))
            except Exception:
                connection.rollback()
                raise

    def list_recordings(self):
        with self._lock:
            ids = [row[0] for row in self._connection.execute(
                "SELECT recording_id FROM recordings ORDER BY rowid"
            )]
        return [self.load(identifier) for identifier in ids]

    def media_timeline(self, recording_id):
        recording_id = _identifier(recording_id, "recording identity")
        with self._lock:
            recording = self._recording_row(recording_id)
            limit = (recording["barrier_media_sequence"]
                     if recording["barrier_media_sequence"] is not None else MAX_FRAMES)
            rows = self._connection.execute(
                """SELECT reference_json, timing_json FROM media
                     WHERE recording_id = ? AND sequence <= ? ORDER BY sequence""",
                (recording_id, limit),
            ).fetchall()
        return [{"reference": _load(row["reference_json"]),
                 "timing": _load(row["timing_json"])} for row in rows]

    def source_timeline(self, recording_id, *, through=None):
        recording_id = _identifier(recording_id, "recording identity")
        limit = MAX_SOURCE_FRAMES if through is None else _integer(
            through, "source barrier", 0, MAX_SOURCE_FRAMES)
        with self._lock:
            rows = self._connection.execute(
                "SELECT source_json FROM frame_acquisitions WHERE recording_id=? AND sequence<=? ORDER BY sequence",
                (recording_id, limit)).fetchall()
        return [_load(row[0]) for row in rows]

    def pending_video_recoveries(self, project_digest):
        project_digest = _digest(project_digest, "recovery project digest")
        with self._lock:
            _require(project_digest in self._registered_digests,
                     "Recovery project is not registered in this process")
            rows = self._connection.execute(
                """SELECT recording_id FROM recordings
                     WHERE project_digest=? AND source_mode=? AND source_cleanup_complete=0
                       AND state IN ('finalizing','frozen-complete','frozen-incomplete')
                     ORDER BY recording_id""", (project_digest, VIDEO_SOURCE_MODE)).fetchall()
            return tuple(row[0] for row in rows if row[0] not in self._active)

    def prebinding_recording_metadata(self, recording_id, project_digest):
        """Read the exact registered G2 identity for video binding cleanup."""
        recording_id = _identifier(recording_id, "recording identity")
        project_digest = _digest(project_digest, "recording project digest")
        with self._lock:
            row = self._recording_row(recording_id)
            _require(project_digest in self._registered_digests and row["project_digest"] == project_digest,
                     "Video binding project authority is unavailable")
            configuration = None
            if row["source_mode"] == VIDEO_SOURCE_MODE:
                from .video import VideoLimits
                configuration = _load(row["source_config_json"])
                _require(type(configuration) is dict and set(configuration) == {"limits", "retainUntilMs"}
                         and contracts.digest(configuration) == row["source_config_digest"]
                         and type(configuration["limits"]) is dict
                         and set(configuration["limits"]) == set(VideoLimits.__dataclass_fields__),
                         "Video binding source configuration differs")
                try:VideoLimits(**configuration["limits"])
                except (TypeError, ValueError, ContractError):
                    raise RecordingStoreError("Video binding limits are invalid") from None
                _integer(configuration["retainUntilMs"], "video retention deadline", 1)
            return {"recordingId": recording_id, "projectDigest": row["project_digest"],
                    "state": row["state"], "sourceMode": row["source_mode"],
                    "sourceConfig": configuration, "active": recording_id in self._active}

    def prebinding_recording_ids(self, project_digest):
        project_digest = _digest(project_digest, "recording project digest")
        with self._lock:
            _require(project_digest in self._registered_digests,
                     "Video binding project authority is unavailable")
            return tuple(row[0] for row in self._connection.execute(
                "SELECT recording_id FROM recordings WHERE project_digest=? ORDER BY recording_id",
                (project_digest,)))

    def recovery_session(self, recording_id):
        recording_id = _identifier(recording_id, "recovery recording identity")
        with self._lock:
            row = self._recording_row(recording_id)
            _require(recording_id not in self._active
                     and row["project_digest"] in self._registered_digests
                     and row["source_mode"] == VIDEO_SOURCE_MODE
                     and row["state"] in {"finalizing", "frozen-complete", "frozen-incomplete"},
                     "Recording recovery authority is unavailable")
        return RecordingRecoverySession(self, recording_id, _issuer=self._issuer)

    def close(self):
        with self._lock:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
                self._connection = None
            if self._owner_fd is not None:
                fcntl.flock(self._owner_fd, fcntl.LOCK_UN)
                os.close(self._owner_fd)
                self._owner_fd = None


class RecordingSession:
    def __init__(self, store: RecordingStore, recording_id: str,
                 registration: TrustedProjectRegistration,
                 anchor: RecordingTimeAnchor, journal_reservation: DiskReservation):
        self.store = store
        self.recording_id = recording_id
        self.registration = registration
        self.anchor = anchor
        self.journal_reservation = journal_reservation
        self._pins: list[EvidencePin] = []
        self._frame_sink = None
        self._source_spool = None
        self._source_lock = threading.RLock()
        self._lock = threading.RLock()
        self._admissions_barred = False
        self._inflight_collections = 0

    def use_frame_spool(self, spool, *, limits, retain_until_ms):
        """Bind a local source spool before any pixel sample is admitted."""
        from .frame_spool import FrameSpool
        from .video import VideoLimits
        _require(type(spool) is FrameSpool and spool.recording_id == self.recording_id
                 and spool.budget is self.store.evidence.budget,
                 "Video source spool does not belong to this recording")
        _require(type(limits) is VideoLimits, "Historical video limits are required")
        _integer(retain_until_ms, "source retention deadline", 1)
        configuration = {"limits": {name: getattr(limits, name)
                         for name in limits.__dataclass_fields__},
                         "retainUntilMs": retain_until_ms}
        configuration_json = _json(configuration)
        _require(len(configuration_json.encode("utf-8")) <= 4096,
                 "Historical video configuration is too large")
        reservation = self.store.evidence.budget.reserve(
            self.recording_id, "journal", SOURCE_JOURNAL_RESERVATION_BYTES)
        try:
            with self.store._lock:
                connection = self.store._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = self._row()
                    _require(row["state"] == "recording" and row["source_mode"] == ORIGINAL_FRAME_MODE
                             and self._source_spool is None and connection.execute(
                                 "SELECT 1 FROM samples WHERE recording_id=? AND kind='pixels' LIMIT 1",
                                 (self.recording_id,)).fetchone() is None,
                             "Video source mode must precede acquisition")
                    connection.execute(
                        """UPDATE recordings SET source_mode=?, source_journal_reservation_id=?,
                                  source_config_json=?, source_config_digest=? WHERE recording_id=?""",
                        (VIDEO_SOURCE_MODE, reservation.reservation_id, configuration_json,
                         contracts.digest(configuration), self.recording_id))
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            self._source_spool = spool
        except Exception:
            reservation.close()
            raise

    def attach_frame_sink(self, sink):
        accept = getattr(sink, "accept_frame", None)
        _require(callable(accept), "Invalid recording frame sink")
        with self._lock:
            with self.store._lock:
                row = self._row()
                count = self.store._connection.execute(
                    "SELECT COUNT(*) FROM media WHERE recording_id = ?",
                    (self.recording_id,),
                ).fetchone()[0]
                _require(row["state"] == "recording" and count == 0
                         and self._frame_sink is None,
                         "Frame sink must be attached before acquisition")
            binder = getattr(sink, "bind_recording", None)
            if binder is not None:
                _require(callable(binder), "Invalid recording frame sink")
                binder(self)
            with self.store._lock:
                row = self._row()
                count = self.store._connection.execute(
                    "SELECT COUNT(*) FROM media WHERE recording_id = ?",
                    (self.recording_id,),
                ).fetchone()[0]
                _require(row["state"] == "recording" and count == 0
                         and self._frame_sink is None,
                         "Frame sink must be attached before acquisition")
                self._frame_sink = sink

    def _row(self):
        return self.store._recording_row(self.recording_id)

    def _begin_collection(self):
        with self._lock:
            _require(not self._admissions_barred, "Recording admission is closed")
            self._inflight_collections += 1

    def _end_collection(self):
        with self._lock:
            self._inflight_collections -= 1

    def _stamp(self):
        try:
            return self.anchor.stamp()
        except RecordingClockError:
            self._gap("clock_discontinuity", offset_ms=None)
            raise RecordingStoreError("Recording clock is discontinuous") from None

    def duration_reached(self):
        return self._stamp().offset_ms >= MAX_RECORDING_DURATION_MS

    def _gap(self, reason, *, offset_ms=None):
        _identifier(reason, "gap reason")
        with self.store._lock:
            connection = self.store._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row()
                if row["state"] not in {"recording", "finalizing"}:
                    connection.commit()
                    return
                if offset_ms is None:
                    last = connection.execute(
                        "SELECT COALESCE(MAX(offset_ms), 0) FROM events WHERE recording_id = ?",
                        (self.recording_id,),
                    ).fetchone()[0]
                    offset_ms = int(last)
                sequence = int(connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM gaps WHERE recording_id = ?",
                    (self.recording_id,),
                ).fetchone()[0])
                _require(sequence <= MAX_GAPS, "Recording gap limit reached")
                connection.execute(
                    "INSERT INTO gaps(recording_id, sequence, offset_ms, reason) VALUES (?, ?, ?, ?)",
                    (self.recording_id, sequence, offset_ms, reason),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def admit_input(self, operation_id, generation, provider_incarnation, input_wire):
        operation_id = _identifier(operation_id, "operation identity")
        generation = _integer(generation, "ownership generation", 1)
        provider_incarnation = _identifier(provider_incarnation, "provider incarnation")
        try:
            checked_input = contracts.validate_input(input_wire)
        except ContractError:
            raise RecordingStoreError("Parameterized recording input is invalid") from None
        if checked_input["action"] == "text":
            variable = checked_input["parameters"]["variableId"]
            variables = {item["id"] for item in self.registration.project["variables"]}
            _require(variable in variables, "Text variable is not registered")
        stamp = self._stamp()
        if stamp.offset_ms >= MAX_RECORDING_DURATION_MS:
            raise RecordingDurationError("Recording duration limit reached")
        encoded_input = _json(checked_input)
        _require(len(encoded_input.encode("utf-8")) <= 4096,
                 "Parameterized recording input is too large")
        with self.store._lock:
            connection = self.store._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row()
                _require(row["state"] == "recording" and row["barrier_sequence"] is None,
                         "Recording admission is closed")
                sequence = int(connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE recording_id = ?",
                    (self.recording_id,),
                ).fetchone()[0])
                _require(sequence <= MAX_EVENTS, "Recording event limit reached")
                previous_offset = int(connection.execute(
                    "SELECT COALESCE(MAX(offset_ms), 0) FROM events WHERE recording_id = ?",
                    (self.recording_id,),
                ).fetchone()[0])
                _require(stamp.offset_ms >= previous_offset,
                         "Recording event time moved backward")
                journal_bytes = int(connection.execute(
                    "SELECT COALESCE(SUM(LENGTH(CAST(input_json AS BLOB))), 0) FROM events WHERE recording_id = ?",
                    (self.recording_id,),
                ).fetchone()[0])
                _require(journal_bytes + len(encoded_input.encode("utf-8"))
                         <= MAX_INPUT_JOURNAL_BYTES,
                         "Recording journal reservation is exhausted")
                connection.execute(
                    """INSERT INTO events
                       (recording_id, sequence, event_id, operation_id, generation,
                        provider_incarnation, offset_ms, input_json, dispatch_started,
                        dispatch, receipt_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'unknown', NULL)""",
                    (self.recording_id, sequence, f"event_{sequence}", operation_id,
                     generation, provider_incarnation, stamp.offset_ms, encoded_input),
                )
                connection.commit()
                return sequence
            except Exception:
                connection.rollback()
                raise

    def mark_dispatched(self, operation_id):
        operation_id = _identifier(operation_id, "operation identity")
        with self.store._lock:
            connection = self.store._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row()
                _require(row["state"] == "recording" and row["barrier_sequence"] is None,
                         "Recording admission is closed")
                cursor = connection.execute(
                    """UPDATE events SET dispatch_started = 1
                         WHERE recording_id = ? AND operation_id = ?
                           AND dispatch_started = 0""",
                    (self.recording_id, operation_id),
                )
                _require(cursor.rowcount == 1, "Input admission is unavailable")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def record_receipt(self, operation_id, status, *, error_code=None):
        operation_id = _identifier(operation_id, "operation identity")
        _require(status in {"injected", "rejected", "unknown"},
                 "Invalid input receipt status")
        if error_code is not None:
            _identifier(error_code, "receipt error code")
        stamp = self._stamp()
        with self.store._lock:
            connection = self.store._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row()
                event = connection.execute(
                    "SELECT * FROM events WHERE recording_id = ? AND operation_id = ?",
                    (self.recording_id, operation_id),
                ).fetchone()
                _require(event is not None, "Input admission is unavailable")
                if event["receipt_json"] is not None:
                    existing = _load(event["receipt_json"])
                    _require(existing["status"] == status
                             and existing.get("errorCode") == error_code,
                             "Input receipt conflicts with durable outcome")
                    connection.commit()
                    return copy.deepcopy(existing)
                receipt = {
                    "operationId": operation_id,
                    "generation": event["generation"],
                    "status": status,
                    "providerIncarnation": event["provider_incarnation"],
                    "observedAtMs": max(stamp.display_ms,
                                        row["started_at_ms"] + event["offset_ms"]),
                }
                if error_code is not None:
                    receipt["errorCode"] = error_code
                if row["state"] == "recording" and row["barrier_sequence"] is None:
                    connection.execute(
                        "UPDATE events SET dispatch = ?, receipt_json = ? WHERE recording_id = ? AND operation_id = ?",
                        (status, _json(receipt), self.recording_id, operation_id),
                    )
                else:
                    lifecycle_status = ("complete" if status == "injected"
                                        else "failed" if status == "rejected" else "unknown")
                    if row["recording_digest"] is None:
                        pending = connection.execute(
                            """SELECT generation, status FROM pending_lifecycle
                                 WHERE recording_id = ? AND operation_id = ?
                                   AND kind = 'ack'""",
                            (self.recording_id, operation_id),
                        ).fetchone()
                        if pending is None:
                            count = int(connection.execute(
                                "SELECT COUNT(*) FROM pending_lifecycle WHERE recording_id = ?",
                                (self.recording_id,),
                            ).fetchone()[0])
                            _require(count < MAX_LIFECYCLE_RECEIPTS,
                                     "Lifecycle receipt limit reached")
                            connection.execute(
                                """INSERT INTO pending_lifecycle
                                   (recording_id, operation_id, generation, kind, status, observed_at_ms)
                                   VALUES (?, ?, ?, 'ack', ?, ?)""",
                                (self.recording_id, operation_id, event["generation"],
                                 lifecycle_status, receipt["observedAtMs"]),
                            )
                        else:
                            _require(pending["generation"] == event["generation"]
                                     and pending["status"] == lifecycle_status,
                                     "Lifecycle receipt conflicts with durable outcome")
                    else:
                        self.store._append_lifecycle_locked(
                            connection, row, operation_id=operation_id,
                            generation=event["generation"], kind="ack",
                            status=lifecycle_status,
                            observed_at_ms=receipt["observedAtMs"],
                        )
                connection.commit()
                return copy.deepcopy(receipt)
            except Exception:
                connection.rollback()
                raise

    def _authorize_sample(self, *, kind, body, acquisition_sequence, classification,
                          native_incarnation=None):
        _require(kind in _COLLECTION_KINDS, "Invalid collection kind")
        acquisition_sequence = _integer(acquisition_sequence, "acquisition sequence", 1)
        sample_digest = hashlib.sha256(body).hexdigest()
        policy = self.registration.collection_policy
        project_policy = self.registration.project["evidencePolicy"]
        allowed = project_policy[kind] is True and policy["captureMode"] != "suppressed"
        sample_id = None
        decision = "suppressed"
        requested_native = native_incarnation
        bound_native = (native_incarnation if native_incarnation is not None
                        else "host_acquisition")
        _identifier(bound_native, "native incarnation")
        if allowed and policy["captureMode"] == "test-data":
            decision = "approved"
        elif allowed and policy["captureMode"] == "sample-bound":
            token = classification
            with self.store._lock:
                expected_provider = self._row()["provider_incarnation"]
            if (type(token) is SampleClassification
                    and token._issuer is self.registration._classification_issuer
                    and token.kind == kind
                    and token.sample_digest == sample_digest
                    and token.provider_incarnation == expected_provider
                    and token.acquisition_sequence == acquisition_sequence
                    and (requested_native is None
                         or token.native_incarnation == requested_native)
                    and token.decision == "approved"):
                sample_id = token.sample_id
                bound_native = token.native_incarnation
                decision = "approved"
        with self.store._lock:
            connection = self.store._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row()
                if row["state"] != "recording":
                    connection.commit()
                    return False, sample_digest, bound_native
                previous = connection.execute(
                    """SELECT COALESCE(MAX(acquisition_sequence), 0)
                         FROM samples WHERE recording_id = ? AND kind = ?
                           AND provider_incarnation = ?""",
                    (self.recording_id, kind, row["provider_incarnation"]),
                ).fetchone()[0]
                _require(acquisition_sequence > int(previous),
                         "Sample acquisition sequence is not newer")
                count = int(connection.execute(
                    "SELECT COUNT(*) FROM samples WHERE recording_id = ?",
                    (self.recording_id,),
                ).fetchone()[0])
                sample_limit = (MAX_SOURCE_FRAMES + MAX_OBSERVATIONS
                                if row["source_mode"] == VIDEO_SOURCE_MODE else MAX_SAMPLES)
                _require(count < sample_limit, "Recording sample limit reached")
                connection.execute(
                    """INSERT INTO samples
                       (recording_id, kind, provider_incarnation, native_incarnation,
                        acquisition_sequence,
                        sample_id, sample_digest, decision)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (self.recording_id, kind, row["provider_incarnation"],
                     bound_native, acquisition_sequence, sample_id,
                     sample_digest if decision == "approved" else None, decision),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if decision != "approved":
            try:
                stamp = self.anchor.stamp()
                offset = stamp.offset_ms
            except RecordingClockError:
                offset = None
            self._gap("privacy_suppressed", offset_ms=offset)
            return False, sample_digest, bound_native
        return True, sample_digest, bound_native

    def record_frame(self, body, mime_type, width, height, orientation, *,
                     acquisition_sequence, classification=None,
                     provider_clock_binding=None, provider_monotonic_ns=None,
                     provider_capture_start_ns=None,
                     native_incarnation=None,
                     native_sequence_gap=None,
                     timing_source="host-acquired"):
        _require(type(body) is bytes and 0 < len(body) <= 3 * 1024 * 1024,
                 "Invalid frame bytes")
        _require(mime_type in {"image/jpeg", "image/png", "image/svg+xml"},
                 "Invalid frame MIME type")
        _integer(width, "frame width", 1, 8192)
        _integer(height, "frame height", 1, 8192)
        _require(orientation in {"portrait", "landscape"}, "Invalid frame orientation")
        _require(timing_source in {
            "host-acquired", "provider-mapped", "native-unmapped"
        }, "Invalid frame timing source")
        _require((provider_clock_binding is None) ==
                 (timing_source != "provider-mapped"),
                 "Frame timing source does not match provider mapping")
        if native_sequence_gap is not None:
            _require(type(native_sequence_gap) is tuple and len(native_sequence_gap) == 2,
                     "Invalid native sequence gap")
            first, last = native_sequence_gap
            _integer(first, "native gap start", 1)
            _integer(last, "native gap end", first)
            _require(type(acquisition_sequence) is int and last == acquisition_sequence - 1,
                     "Native sequence gap does not precede this capture")
        if self.duration_reached():
            return None
        self._begin_collection()
        try:
            allowed, digest, bound_native = self._authorize_sample(
                kind="pixels", body=body,
                acquisition_sequence=acquisition_sequence,
                classification=classification,
                native_incarnation=native_incarnation,
            )
            if not allowed:
                return None
            if self._source_spool is not None:
                # The sample journal includes intentionally suppressed pixels.
                # Their IDs must not be misreported as native transport loss.
                with self.store._lock:
                    previous_sample = self.store._connection.execute(
                        """SELECT COALESCE(MAX(acquisition_sequence),0) FROM samples
                             WHERE recording_id=? AND kind='pixels' AND provider_incarnation=? AND native_incarnation=?
                               AND acquisition_sequence<?""",
                        (self.recording_id, self._row()["provider_incarnation"], bound_native,
                         acquisition_sequence)).fetchone()[0]
                expected_gap = ((previous_sample + 1, acquisition_sequence - 1)
                                if acquisition_sequence > previous_sample + 1 else None)
                if native_sequence_gap != expected_gap:
                    self._gap("native_frame_gap")
                    raise RecordingStoreError("Native sequence continuity is incomplete")
            if (provider_clock_binding is None and provider_monotonic_ns is None
                    and provider_capture_start_ns is None):
                stamp = self._stamp()
            else:
                _require(type(provider_clock_binding) is ProviderClockBinding
                         and type(provider_monotonic_ns) is int,
                         "Provider timestamp mapping is incomplete")
                _require(provider_clock_binding.native_incarnation == bound_native,
                         "Provider timestamp incarnation differs from sample")
                try:
                    stamp = self.anchor.stamp_provider(provider_clock_binding,
                        provider_monotonic_ns, capture_start_ns=provider_capture_start_ns)
                except RecordingClockError:
                    self._gap("clock_discontinuity", offset_ms=None)
                    raise RecordingStoreError(
                        "Provider clock is discontinuous") from None
            if stamp.latest_offset_ms > MAX_RECORDING_DURATION_MS:
                # A partially out-of-window capture is not retimestamped to
                # fit the manifest. It never becomes an admitted source.
                return None
            timing = {
                "offsetMs": stamp.offset_ms,
                "earliestOffsetMs": stamp.earliest_offset_ms,
                "latestOffsetMs": stamp.latest_offset_ms,
                "uncertaintyNs": stamp.uncertainty_ns,
                "acquisitionSequence": acquisition_sequence,
                "nativeIncarnation": bound_native,
                "timingSource": timing_source,
            }
            if stamp.provider_clock_id is not None:
                timing.update({
                    "providerClockId": stamp.provider_clock_id,
                    "providerBootDigest": stamp.provider_boot_digest,
                    "nativeIncarnation": stamp.native_incarnation,
                    "providerMonotonicNs": provider_monotonic_ns,
                })
                if provider_capture_start_ns is not None:
                    timing["providerCaptureStartNs"] = provider_capture_start_ns
            if self._source_spool is not None:
                return self._record_video_source(body, digest, mime_type, width, height,
                    orientation, acquisition_sequence, stamp, timing, timing_source,
                    native_sequence_gap=native_sequence_gap)
            if native_sequence_gap is not None:
                self._gap("native_frame_gap", offset_ms=stamp.offset_ms)
                if self._frame_sink is not None:
                    self._frame_sink.declare_loss("transport", reason="native-frame-gap",
                        start_offset_ms=0, end_offset_ms=stamp.latest_offset_ms)
            with self.store._lock:
                row = self._row()
            policy = _load(row["policy_json"])
            retain_until = (int(time.time() * 1000)
                            + policy["retentionSeconds"]["original"] * 1000)
            try:
                reference = self.store.evidence.put_bytes(
                    body, owner=self.recording_id, retention_class="original",
                    retain_until_ms=retain_until,
                )
                pin_id = "recording_" + self.recording_id
                pin = self.store.evidence.pin(reference.digest, pin_id, "recording")
            except (EvidenceStoreError, DiskBudgetError):
                self._gap("storage_exhausted", offset_ms=stamp.offset_ms)
                self.stop(reason="storage_exhausted")
                raise RecordingStoreError("Frame storage is exhausted") from None
            with self.store._lock:
                connection = self.store._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = self._row()
                    _require(row["state"] == "recording",
                             "Recording admission is closed")
                    sequence = int(connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM media WHERE recording_id = ?",
                        (self.recording_id,),
                    ).fetchone()[0])
                    _require(sequence <= MAX_FRAMES, "Recording frame limit reached")
                    artifact = {
                        "id": f"frame_{sequence}",
                        "digest": reference.digest,
                        "path": reference.path,
                        "bytes": reference.bytes,
                        "mimeType": mime_type,
                    }
                    connection.execute(
                        "INSERT INTO media(recording_id, sequence, reference_json, timing_json, pin_id) VALUES (?, ?, ?, ?, ?)",
                        (self.recording_id, sequence, _json(artifact),
                         _json(timing), pin_id),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    pin.close()
                    raise
            self._pins.append(pin)
            publication = FramePublication(
                reference.digest, reference.bytes, reference.path,
                mime_type, width, height, orientation, acquisition_sequence, stamp,
                timing_source,
            )
            if self._frame_sink is not None:
                try:
                    self._frame_sink.accept_frame(publication, body)
                except Exception:
                    self._gap("frame_sink_failed", offset_ms=stamp.offset_ms)
                    self.stop(reason="frame_sink_failed")
                    raise RecordingStoreError("Recording frame sink failed") from None
            return publication
        finally:
            self._end_collection()

    def _record_video_source(self, body, digest, mime_type, width, height, orientation,
                             acquisition_sequence, stamp, timing, timing_source, *, native_sequence_gap=None):
        with self._source_lock:
            with self.store._lock:
                row = self._row()
                _require(row["state"] == "recording" and row["source_mode"] == VIDEO_SOURCE_MODE,
                         "Video source admission is closed")
                sequence = self.store._connection.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM frame_acquisitions WHERE recording_id=?",
                    (self.recording_id,)).fetchone()[0]
                _integer(sequence, "video source sequence", 1, MAX_SOURCE_FRAMES)
                provider_incarnation = row["provider_incarnation"]
                previous = self.store._connection.execute(
                    "SELECT source_json FROM frame_acquisitions WHERE recording_id=? ORDER BY sequence DESC LIMIT 1",
                    (self.recording_id,)).fetchone() if native_sequence_gap is not None else None
                if native_sequence_gap is not None:
                    _require(previous is None or _load(previous[0])["acquisitionSequence"] < native_sequence_gap[0],
                             "Native sequence gap overlaps an admitted source")
            source = {"recordingFrameSequence": sequence, "acquisitionSequence": acquisition_sequence,
                "digest": digest, "bytes": len(body), "mimeType": mime_type,
                "width": width, "height": height, "orientation": orientation,
                "providerIncarnation": provider_incarnation, "timing": copy.deepcopy(timing)}
            if native_sequence_gap is not None:
                source["nativeSequenceGap"] = {"first": native_sequence_gap[0], "last": native_sequence_gap[1]}
                source["nativeGapInterval"] = {
                    "startOffsetMs": _load(previous[0])["timing"]["earliestOffsetMs"] if previous else 0,
                    "endOffsetMs": stamp.latest_offset_ms,
                }
            try:
                token = self._source_spool.stage(sequence, acquisition_sequence, body, digest, source)
            except (ContractError, OSError):
                self._gap("source_spool_failed", offset_ms=stamp.offset_ms)
                raise RecordingStoreError("Video source could not be durably staged") from None
            with self.store._lock:
                connection = self.store._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _require(self._row()["state"] == "recording", "Video source admission is closed")
                    connection.execute("INSERT INTO frame_acquisitions VALUES(?,?,?,?)",
                        (self.recording_id, sequence, acquisition_sequence, _json(source)))
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            publication = FramePublication(digest, len(body), f"transient/{sequence}",
                mime_type, width, height, orientation, acquisition_sequence, stamp, timing_source,
                sequence, token)
            try:
                self._frame_sink.accept_frame(publication, body)
            except Exception:
                self._gap("frame_sink_failed", offset_ms=stamp.offset_ms)
                raise RecordingStoreError("Recording frame sink failed") from None
            return publication

    def record_observation(self, kind, value, *, acquisition_sequence,
                           classification=None, mime_type="application/json", terminal_snapshot=False):
        _require(kind in {"text", "accessibility", "logs"},
                 "Invalid observation kind")
        _require(mime_type == "application/json" or kind == "logs" and mime_type == APP_LOG_MIME,
                 "Invalid observation MIME type")
        _require(type(terminal_snapshot) is bool and (not terminal_snapshot or kind == "logs"),
                 "Only terminal logs may be collected after the acquisition deadline")
        if not terminal_snapshot and self.duration_reached():
            return None
        body = _json(value).encode("utf-8")
        _require(len(body) <= 50 * 1024 * 1024, "Observation is too large")
        self._begin_collection()
        try:
            allowed, _, _ = self._authorize_sample(
                kind=kind, body=body,
                acquisition_sequence=acquisition_sequence,
                classification=classification,
            )
            if not allowed:
                return None
            stamp = self._stamp()
            policy = self.registration.collection_policy
            retain_until = (int(time.time() * 1000)
                            + policy["retentionSeconds"]["original"] * 1000)
            try:
                reference = self.store.evidence.put_bytes(
                    body, owner=self.recording_id, retention_class="original",
                    retain_until_ms=retain_until,
                )
            except (EvidenceStoreError, DiskBudgetError):
                self._gap("storage_exhausted", offset_ms=stamp.offset_ms)
                self.stop(reason="storage_exhausted")
                raise RecordingStoreError("Observation storage is exhausted") from None
            with self.store._lock:
                connection = self.store._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = self._row()
                    _require(row["state"] == "recording",
                             "Recording admission is closed")
                    sequence = int(connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM observations WHERE recording_id = ?",
                        (self.recording_id,),
                    ).fetchone()[0])
                    _require(sequence <= MAX_OBSERVATIONS,
                             "Recording observation limit reached")
                    artifact = {
                        "id": f"observation_{sequence}",
                        "digest": reference.digest,
                        "path": reference.path,
                        "bytes": reference.bytes,
                        "mimeType": mime_type,
                    }
                    connection.execute(
                        "INSERT INTO observations(recording_id, sequence, reference_json) VALUES (?, ?, ?)",
                        (self.recording_id, sequence, _json(artifact)),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            return artifact
        finally:
            self._end_collection()

    def stop_barrier(self, *, reason=None):
        if reason is not None:
            _identifier(reason, "interruption reason")
        with self._lock:
            self._admissions_barred = True
            if self._inflight_collections and reason is None:
                reason = "collection_inflight_stop"
        try:
            stamp = self.anchor.stamp()
            offset = min(stamp.offset_ms, MAX_RECORDING_DURATION_MS)
        except RecordingClockError:
            offset = None
            reason = "clock_discontinuity"
        with self.store._lock:
            connection = self.store._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row()
                if row["state"] in {"frozen-complete", "frozen-incomplete", "finalizing"}:
                    connection.commit()
                    return row["barrier_sequence"]
                _require(row["state"] == "recording", "Recording cannot be stopped")
                last = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0), COALESCE(MAX(offset_ms), 0) FROM events WHERE recording_id = ?",
                    (self.recording_id,),
                ).fetchone()
                if offset is None:
                    offset = int(last[1])
                connection.execute(
                    """UPDATE recordings SET state = 'finalizing', barrier_sequence = ?,
                              barrier_media_sequence = (SELECT COALESCE(MAX(sequence), 0) FROM media WHERE recording_id = ?),
                              barrier_observation_sequence = (SELECT COALESCE(MAX(sequence), 0) FROM observations WHERE recording_id = ?),
                              barrier_gap_sequence = (SELECT COALESCE(MAX(sequence), 0) FROM gaps WHERE recording_id = ?),
                              barrier_source_sequence = (SELECT COALESCE(MAX(sequence), 0) FROM frame_acquisitions WHERE recording_id = ?),
                              barrier_offset_ms = ?, interruption_reason = ?
                         WHERE recording_id = ? AND state = 'recording'""",
                    (int(last[0]), self.recording_id, self.recording_id,
                     self.recording_id, self.recording_id, offset, reason, self.recording_id),
                )
                connection.commit()
                return int(last[0])
            except Exception:
                connection.rollback()
                raise

    def declare_gap(self, reason, *, invalidate_provider_mappings=False):
        if invalidate_provider_mappings:
            self.anchor.invalidate_provider_mappings()
            rotate = getattr(self._frame_sink, "invalidate_clock_mapping", None)
            if rotate is not None:
                _require(callable(rotate), "Invalid recording frame sink")
                rotate()
        try:offset = self.anchor.stamp().offset_ms
        except RecordingClockError:offset = None
        self._gap(reason, offset_ms=offset)

    def video_barrier_snapshot(self):
        """Return only the already committed stop barrier for a bound sink."""
        with self.store._lock:
            row = self._row()
            _require(row["state"] == "finalizing"
                     and row["barrier_sequence"] is not None,
                     "Recording stop barrier is missing")
            return {
                "sequence": row["barrier_sequence"],
                "mediaSequence": row["barrier_media_sequence"],
                "offsetMs": row["barrier_offset_ms"],
                "sourceSequence": row["barrier_source_sequence"],
            }

    def video_frame_lineage(self, publication):
        """Resolve a sink input back to its admitted G2 sample/incarnations."""
        _require(type(publication) is FramePublication,
                 "Invalid frame publication")
        if publication.recording_frame_sequence is not None:
            source = self.video_source(publication.acquisition_sequence)
            _require(self._source_spool is not None and source is not None
                     and source["recordingFrameSequence"] == publication.recording_frame_sequence
                     and source["digest"] == publication.digest and source["bytes"] == publication.bytes
                     and source["mimeType"] == publication.mime_type
                     and [source["width"], source["height"], source["orientation"]]
                     == [publication.width, publication.height, publication.orientation]
                     and self._source_spool.read_metadata(publication.source_token) == source,
                     "Video source differs from admitted evidence")
            expected = {"offsetMs": publication.stamp.offset_ms,
                "earliestOffsetMs": publication.stamp.earliest_offset_ms,
                "latestOffsetMs": publication.stamp.latest_offset_ms,
                "uncertaintyNs": publication.stamp.uncertainty_ns,
                "timingSource": publication.timing_source}
            _require(all(source["timing"].get(key) == value for key, value in expected.items()),
                     "Video source timing differs from admission")
            _require(source["timing"].get("providerClockId") == publication.stamp.provider_clock_id
                     and source["timing"].get("providerBootDigest") == publication.stamp.provider_boot_digest,
                     "Video source clock identity differs from admission")
            return {"providerIncarnation": source["providerIncarnation"],
                    "nativeIncarnation": source["timing"]["nativeIncarnation"]}
        with self.store._lock:
            row = self._row()
            matches = []
            for item in self.store._connection.execute(
                """SELECT reference_json, timing_json FROM media
                     WHERE recording_id = ? ORDER BY sequence""",
                (self.recording_id,),
            ):
                reference = _load(item["reference_json"])
                timing = _load(item["timing_json"])
                if (reference["digest"] == publication.digest
                        and timing.get("acquisitionSequence")
                        == publication.acquisition_sequence):
                    _require(reference["path"] == publication.path
                             and reference["bytes"] == publication.bytes
                             and reference["mimeType"] == publication.mime_type,
                             "Frame publication differs from admitted evidence")
                    stamp = publication.stamp
                    expected = {
                        "offsetMs": stamp.offset_ms,
                        "earliestOffsetMs": stamp.earliest_offset_ms,
                        "latestOffsetMs": stamp.latest_offset_ms,
                        "uncertaintyNs": stamp.uncertainty_ns,
                        "timingSource": publication.timing_source,
                    }
                    _require(all(timing.get(key) == value for key, value in expected.items())
                             and timing.get("providerClockId") == stamp.provider_clock_id
                             and timing.get("providerBootDigest") == stamp.provider_boot_digest,
                             "Frame publication timing differs from admitted evidence")
                    matches.append(timing)
            _require(len(matches) == 1,
                     "Frame publication lineage is unavailable")
            return {
                "providerIncarnation": row["provider_incarnation"],
                "nativeIncarnation": matches[0]["nativeIncarnation"],
            }

    def video_source(self, acquisition_sequence):
        with self.store._lock:
            item = self.store._connection.execute(
                "SELECT source_json FROM frame_acquisitions WHERE recording_id=? AND acquisition_sequence=?",
                (self.recording_id, acquisition_sequence)).fetchone()
        return _load(item[0]) if item is not None else None

    def video_sources(self):
        with self.store._lock:
            row = self._row()
        return self.store.source_timeline(self.recording_id, through=row["barrier_source_sequence"])

    def complete_source_cleanup(self):
        """Persist the terminal spool outcome before releasing its manifest pin."""
        from .frame_spool import FrameSpool
        _require(type(self._source_spool) is FrameSpool and self._source_spool._retired,
                 "Source spool retirement has not completed")
        self._source_spool.finalcleanup()
        _complete_source_cleanup(self.store, self.recording_id)

    def video_event_snapshot(self):
        """Freeze-time event markers without admitting or revising receipts."""
        with self.store._lock:
            row = self._row()
            _require(row["state"] == "finalizing"
                     and row["barrier_sequence"] is not None,
                     "Recording stop barrier is missing")
            rows = self.store._connection.execute(
                """SELECT sequence, event_id, offset_ms FROM events
                     WHERE recording_id = ? AND sequence <= ? ORDER BY sequence""",
                (self.recording_id, row["barrier_sequence"]),
            ).fetchall()
        return [{"sequence": item["sequence"], "eventId": item["event_id"],
                 "offsetMs": item["offset_ms"]} for item in rows]

    def attach_finalized_video(self, artifacts, timings, *, incomplete_reason=None, source_manifest=None):
        """Attach only content-store-published G3 objects after the stop barrier.

        This deliberately leaves event/receipt/barrier sequences unchanged.
        The added media can only derive from frames admitted before that barrier.
        """
        with self.store._lock:
            row = self._row()
        max_artifacts = 129 if row["source_mode"] == VIDEO_SOURCE_MODE else MAX_VIDEO_ARTIFACTS
        _require(type(artifacts) is list and type(timings) is list
                 and len(artifacts) == len(timings)
                 and len(artifacts) <= max_artifacts,
                 "Invalid finalized video evidence")
        if incomplete_reason is not None:
            _identifier(incomplete_reason, "video interruption reason")
        source_manifest_digest = None
        if row["source_mode"] == VIDEO_SOURCE_MODE:
            from .video import validate_video_manifest, VIDEO_MANIFEST_MIME
            _require(source_manifest is not None, "Video source outcome manifest is required")
            manifest = validate_video_manifest(source_manifest)
            _require(manifest["schemaVersion"] == 2 and manifest["recordingId"] == self.recording_id
                     and row["state"] == "finalizing" and row["barrier_source_sequence"] is not None
                     and ((manifest["status"] == "incomplete") == (incomplete_reason is not None)),
                     "Video source manifest does not match this stop barrier")
            sources = self.store.source_timeline(self.recording_id, through=row["barrier_source_sequence"])
            expected_ledger = [{"sequence": item["recordingFrameSequence"],
                "acquisitionSequence": item["acquisitionSequence"], "digest": item["digest"]}
                for item in sources]
            _require(manifest["sourceFrames"] == expected_ledger,
                     "Video source ledger differs from admitted captures")
            for source in sources:
                if "nativeSequenceGap" in source:
                    _require(any(loss.get("nativeSequenceRange") == source["nativeSequenceGap"]
                                 and loss["recordingInterval"] == source["nativeGapInterval"]
                                 and loss["stage"] == "transport" and loss["reason"] == "native-frame-gap"
                                 for loss in manifest["losses"]),
                             "Native source gap is missing from video outcomes")
            for segment in manifest["segments"]:
                for frame in segment["frames"]:
                    source = sources[frame["recordingFrameSequence"] - 1]
                    timing = source["timing"]
                    precise = timing["timingSource"] != "native-unmapped"
                    _require([segment["width"], segment["height"], segment["orientation"]]
                             == [source["width"], source["height"], source["orientation"]]
                             and frame["providerIncarnation"] == source["providerIncarnation"]
                             and frame["nativeIncarnation"] == timing["nativeIncarnation"]
                             and frame["timingSource"] == timing["timingSource"]
                             and frame["uncertaintyNs"] == timing["uncertaintyNs"]
                             and frame["presentationTimeMs"]
                             == timing["offsetMs"] - segment["displayInterval"]["startOffsetMs"],
                             "Encoded source geometry or timing differs from admission")
                    if precise:
                        _require(frame["earliestRecordingOffsetMs"] == timing["earliestOffsetMs"]
                                 and frame["latestRecordingOffsetMs"] == timing["latestOffsetMs"],
                                 "Encoded source acquisition interval differs from admission")
                    else:
                        _require(frame["displayOffsetMs"] == timing["offsetMs"],
                                 "Encoded source publication time differs from admission")
            source_manifest_digest = contracts.digest(manifest)
            matches = [item for item in artifacts if item.get("mimeType") == VIDEO_MANIFEST_MIME]
            _require(len(matches) == 1 and matches[0].get("digest") == source_manifest_digest
                     and self.store.evidence.read(source_manifest_digest) == _json(manifest).encode("utf-8"),
                     "Video source manifest is not the published artifact")
        else:
            _require(source_manifest is None, "Legacy recording cannot attach a transient source manifest")
        checked = []
        for artifact, timing in zip(artifacts, timings):
            _require(type(artifact) is dict and set(artifact) == {
                "id", "digest", "path", "bytes", "mimeType"
            }, "Invalid finalized video reference")
            _identifier(artifact["id"], "video artifact identity")
            _digest(artifact["digest"], "video artifact digest")
            _integer(artifact["bytes"], "video artifact bytes", 1,
                     64 * 1024 * 1024)
            _require(artifact["mimeType"] in {
                "video/mp4", "application/vnd.reproof.video-manifest+json"
            }, "Invalid finalized video MIME type")
            reference = self.store.evidence.lookup(artifact["digest"])
            _require(reference is not None
                     and reference.path == artifact["path"]
                     and reference.bytes == artifact["bytes"],
                     "Finalized video is not durable")
            _require(type(timing) is dict and set(timing) == {
                "offsetMs", "earliestOffsetMs", "latestOffsetMs",
                "uncertaintyNs", "timingSource"
            }, "Invalid finalized video timing")
            _integer(timing["offsetMs"], "video offset", 0, 600_000)
            _integer(timing["earliestOffsetMs"], "video interval start", 0,
                     600_000)
            _integer(timing["latestOffsetMs"], "video interval end",
                     timing["earliestOffsetMs"], 600_000)
            _integer(timing["uncertaintyNs"], "video uncertainty", 0,
                     60_000_000_000)
            _require(timing["timingSource"] in {
                "host-acquired", "provider-mapped", "native-unmapped"
            }, "Invalid finalized video timing source")
            checked.append((copy.deepcopy(artifact), copy.deepcopy(timing)))
        video_pins = []
        try:
            if source_manifest_digest is not None:
                video_pins.append(self.store.evidence.pin(source_manifest_digest,
                    "source_outcome_" + self.recording_id, "recording"))
            for artifact, _ in checked:
                video_pins.append(self.store.evidence.pin(
                    artifact["digest"], "recording_" + self.recording_id,
                    "recording",
                ))
            with self.store._lock:
                connection = self.store._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = self._row()
                    _require(row["state"] == "finalizing"
                             and row["barrier_sequence"] is not None,
                             "Recording stop barrier is missing")
                    _require(row["source_mode"] != VIDEO_SOURCE_MODE
                             or row["source_manifest_digest"] is None,
                             "Video source outcome was already attached")
                    existing = [_load(item[0]) for item in connection.execute(
                        "SELECT reference_json FROM media WHERE recording_id = ?",
                        (self.recording_id,),
                    )]
                    existing_ids = {item["id"] for item in existing}
                    _require(not existing_ids.intersection(
                        artifact["id"] for artifact, _ in checked
                    ), "Finalized video was already attached")
                    sequence = int(connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM media WHERE recording_id = ?",
                        (self.recording_id,),
                    ).fetchone()[0])
                    _require(sequence + len(checked) <= MAX_FRAMES + MAX_VIDEO_ARTIFACTS,
                             "Recording media reference limit reached")
                    for artifact, timing in checked:
                        sequence += 1
                        connection.execute(
                            """INSERT INTO media
                               (recording_id, sequence, reference_json, timing_json, pin_id)
                               VALUES (?, ?, ?, ?, ?)""",
                            (self.recording_id, sequence, _json(artifact), _json(timing),
                             "recording_" + self.recording_id),
                        )
                    gap_sequence = row["barrier_gap_sequence"]
                    if incomplete_reason is not None:
                        gap_sequence = int(connection.execute(
                            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM gaps WHERE recording_id = ?",
                            (self.recording_id,),
                        ).fetchone()[0])
                        _require(gap_sequence <= MAX_GAPS,
                                 "Recording gap limit reached")
                        connection.execute(
                            "INSERT INTO gaps(recording_id, sequence, offset_ms, reason) VALUES (?, ?, ?, ?)",
                            (self.recording_id, gap_sequence,
                             row["barrier_offset_ms"] or 0, incomplete_reason),
                        )
                    connection.execute(
                        """UPDATE recordings SET barrier_media_sequence = ?,
                                  barrier_gap_sequence = ?, source_manifest_digest = ?
                             WHERE recording_id = ? AND state = 'finalizing'""",
                        (sequence, gap_sequence, source_manifest_digest, self.recording_id),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except Exception:
            for pin in video_pins:
                try:
                    pin.close()
                except EvidenceStoreError:
                    pass
            raise

    def freeze(self):
        with self._lock:
            video = None
            finalizer = getattr(self._frame_sink, "finalize", None)
            if finalizer is not None:
                try:
                    _require(callable(finalizer), "Invalid recording frame sink")
                    video = finalizer()
                except Exception:
                    try:
                        self.attach_finalized_video(
                            [], [], incomplete_reason="video_finalization_failed"
                        )
                    except Exception:
                        pass
            result = self.store._freeze_id(self.recording_id)
            for pin in self._pins:
                pin._closed = True
            self._pins.clear()
            if video is not None:
                result["video"] = copy.deepcopy(video)
            return result

    def journal_snapshot(self):
        with self.store._lock:
            rows = self.store._connection.execute(
                """SELECT sequence, operation_id, dispatch_started, dispatch
                     FROM events WHERE recording_id = ? ORDER BY sequence""",
                (self.recording_id,),
            ).fetchall()
        return [{"sequence": row["sequence"], "operationId": row["operation_id"],
                 "dispatchState": (row["dispatch"] if row["dispatch"] != "unknown"
                                   else "unknown" if row["dispatch_started"] else "admitted")}
                for row in rows]

    def stop(self, *, reason=None):
        with self._lock:
            self.stop_barrier(reason=reason)
            return self.freeze()


def _complete_source_cleanup(store, recording_id):
    with store._lock:
        row = store._recording_row(recording_id)
        _require(row["source_mode"] == VIDEO_SOURCE_MODE
                 and row["source_manifest_digest"] is not None,
                 "Source outcome manifest has not been attached")
    store.evidence.unpin_id("source_outcome_" + recording_id)
    with store._lock:
        row = store._recording_row(recording_id)
        expired = (row["recording_digest"] is not None
                   and store.evidence.is_tombstoned(row["recording_digest"]))
        connection = store._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "UPDATE recordings SET source_cleanup_complete=1 WHERE recording_id=?", (recording_id,))
            if expired:
                connection.execute(
                    "DELETE FROM frame_acquisitions WHERE recording_id=?", (recording_id,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise


class RecordingRecoverySession:
    """Stored evidence finalization capability without acquisition or inputs."""
    __slots__ = ("_store", "_recording_id", "_issuer")

    def __init__(self, store, recording_id, *, _issuer=None):
        _require(isinstance(store, RecordingStore) and _issuer is store._issuer,
                 "Store-issued recording recovery authority is required")
        self._store, self._recording_id, self._issuer = store, recording_id, _issuer
        self._row()

    @property
    def store(self):
        return self._store

    @property
    def recording_id(self):
        return self._recording_id

    def _row(self):
        with self.store._lock:
            row = self.store._recording_row(self.recording_id)
            _require(self._issuer is self.store._issuer
                     and self.recording_id not in self.store._active
                     and row["project_digest"] in self.store._registered_digests
                     and row["source_mode"] == VIDEO_SOURCE_MODE
                     and row["state"] in {"finalizing", "frozen-complete", "frozen-incomplete"},
                     "Recording recovery authority is unavailable")
            return row

    def video_recovery_metadata(self):
        from .video import VideoLimits
        row = self._row()
        value = _load(row["source_config_json"])
        _require(type(value) is dict and set(value) == {"limits", "retainUntilMs"}
                 and contracts.digest(value) == row["source_config_digest"],
                 "Historical video configuration differs")
        try:
            VideoLimits(**value["limits"])
        except (TypeError, ValueError, ContractError):
            raise RecordingStoreError("Historical video limits are invalid") from None
        _integer(value["retainUntilMs"], "source retention deadline", 1)
        reservations = self.store.evidence.budget.reservations_for_owner(self.recording_id, category="journal")
        reserved = next((item for item in reservations
                         if item["reservation_id"] == row["source_journal_reservation_id"]), None)
        _require(reserved is not None and reserved["state"] in {"active", "committed"}
                 and reserved["charged_bytes"] == SOURCE_JOURNAL_RESERVATION_BYTES,
                 "Source journal reservation is unavailable")
        return {**value, "sourceManifestDigest": row["source_manifest_digest"],
                "cleanupComplete": row["source_cleanup_complete"] == 1, "state": row["state"]}

    def video_barrier_snapshot(self):
        row = self._row()
        _require(row["barrier_sequence"] is not None, "Recording recovery barrier is missing")
        return {"sequence": row["barrier_sequence"], "mediaSequence": row["barrier_media_sequence"],
                "offsetMs": row["barrier_offset_ms"], "sourceSequence": row["barrier_source_sequence"]}

    video_sources = RecordingSession.video_sources
    attach_finalized_video = RecordingSession.attach_finalized_video

    def video_source(self, acquisition_sequence):
        self._row()
        return RecordingSession.video_source(self, acquisition_sequence)

    def video_event_snapshot(self):
        barrier = self.video_barrier_snapshot()
        with self.store._lock:
            rows = self.store._connection.execute(
                """SELECT sequence, event_id, offset_ms FROM events
                     WHERE recording_id=? AND sequence<=? ORDER BY sequence""",
                (self.recording_id, barrier["sequence"])).fetchall()
        return [{"sequence": row[0], "eventId": row[1], "offsetMs": row[2]} for row in rows]

    def freeze_recovered(self):
        self._row()
        return self.store._freeze_id(self.recording_id, recovery=True)

    def complete_source_cleanup(self):
        from .frame_spool import FrameSpool
        self._row()
        root = self.store.root.parent / "video" / "sources" / hashlib.sha256(
            self.recording_id.encode("ascii")).hexdigest()
        spool = FrameSpool.open_existing(root, self.store.evidence.budget, self.recording_id)
        try:
            _require(spool._retired, "Source spool retirement has not completed")
            spool.finalcleanup()
        finally:
            spool.close()
        _complete_source_cleanup(self.store, self.recording_id)


__all__ = [
    "RecordingRecoverySession",
    "RecordingDurationError", "MAX_RECORDING_DURATION_MS",
    "FramePublication", "RecordingSession", "RecordingStore",
    "RecordingStoreError", "SampleClassification", "TrustedProjectRegistration",
    "validate_collection_policy",
]
