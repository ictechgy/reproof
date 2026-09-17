"""Versioned SQLite journal for durable host-side device authority."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading

from reproloop.core import ContractError


FORMAT_VERSION = 3
READER_VERSION = 3
WRITER_VERSION = 3
APPLICATION_ID = 0x52504C41  # "RPLA"
MAX_DATABASE_PAGES = 16_384
MAX_DEVICES = 4_096
MAX_OPERATIONS_PER_DEVICE = 100_000
MAX_RECEIPTS_PER_OPERATION = 16
MAX_RECONCILIATIONS_PER_DEVICE = 1_024
MAX_LEGACY_ADOPTIONS = 4_096
RELEASED_RESULT_DIGEST = hashlib.sha256(
    b"reproloop-authority-release-before-dispatch-v1"
).hexdigest()


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _canonical_digest(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StateStore:
    """Small, strongly serialized authority store.

    The connection is process-local and every state transition uses
    ``BEGIN IMMEDIATE``.  Provider callbacks never run through this class, so
    no SQLite transaction is held while native work may block.
    """

    def __init__(self, path):
        self.path = self._safe_path(path)
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=2.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 2000")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA wal_autocheckpoint = 256")
            self._connection.execute("PRAGMA journal_size_limit = 16777216")
            self._connection.execute("PRAGMA cache_size = -4096")
            self._initialize_or_validate()
            maximum = self._connection.execute(
                f"PRAGMA max_page_count = {MAX_DATABASE_PAGES}"
            ).fetchone()[0]
            _require(maximum <= MAX_DATABASE_PAGES, "Authority store size limit unavailable")
        except ContractError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise ContractError("Authority state operation failed") from None

    @staticmethod
    def _safe_path(path):
        try:
            value = Path(path).absolute()
            parent = value.parent
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            parent_stat = parent.lstat()
            _require(
                not stat.S_ISLNK(parent_stat.st_mode)
                and stat.S_ISDIR(parent_stat.st_mode)
                and parent_stat.st_uid == os.getuid(),
                "Unsafe authority state path",
            )
            if value.exists() or value.is_symlink():
                file_stat = value.lstat()
                _require(
                    not stat.S_ISLNK(file_stat.st_mode)
                    and stat.S_ISREG(file_stat.st_mode)
                    and file_stat.st_uid == os.getuid(),
                    "Unsafe authority state path",
                )
        except (OSError, TypeError, ValueError):
            raise ContractError("Unsafe authority state path") from None
        return value

    def _initialize_or_validate(self):
        with self._transaction(exclusive=True) as connection:
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if user_version == 0 and application_id == 0 and not tables:
                self._create_schema(connection)
            else:
                _require(
                    user_version in (1, 2, FORMAT_VERSION) and application_id == APPLICATION_ID,
                    "Authority store version is incompatible",
                )
                expected = {
                    "authority_metadata",
                    "devices",
                    "replay_watermarks",
                    "operations",
                    "operation_receipts",
                    "reconciliations",
                    "reconciliation_dispositions",
                    "legacy_adoptions",
                }
                _require(tables == expected, "Authority store schema is incompatible")
                metadata = dict(
                    connection.execute("SELECT key, value FROM authority_metadata")
                )
                _require(
                    metadata
                    == {
                        "format_version": str(user_version),
                        "minimum_reader_version": str(user_version),
                        "minimum_writer_version": str(user_version),
                    },
                    "Authority store version is incompatible",
                )
                _require(
                    int(metadata["minimum_reader_version"]) <= READER_VERSION
                    and int(metadata["minimum_writer_version"]) <= WRITER_VERSION,
                    "Authority store version is incompatible",
                )
                if user_version == 1:
                    self._migrate_v1(connection)
                elif user_version == 2:
                    self._migrate_v2(connection)

    @staticmethod
    def _schema_statements():
        script = """
            CREATE TABLE authority_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE devices (
                device_fingerprint TEXT PRIMARY KEY,
                device_kind TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                status TEXT NOT NULL CHECK (status IN ('owned', 'released', 'expired', 'quarantined')),
                host_incarnation TEXT NOT NULL,
                helper_incarnation TEXT NOT NULL,
                grant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                controller_id TEXT NOT NULL,
                renewal_sequence INTEGER NOT NULL CHECK (renewal_sequence >= 1),
                parent_deadline_ns INTEGER NOT NULL CHECK (parent_deadline_ns >= 0),
                mapping_id TEXT NOT NULL,
                quarantine_reason TEXT,
                updated_ns INTEGER NOT NULL CHECK (updated_ns >= 0)
            ) WITHOUT ROWID;

            CREATE TABLE replay_watermarks (
                device_fingerprint TEXT NOT NULL REFERENCES devices(device_fingerprint),
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                highest_sequence INTEGER NOT NULL CHECK (highest_sequence >= 0),
                PRIMARY KEY (device_fingerprint, generation, controller_id)
            ) WITHOUT ROWID;

            CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY,
                operation_fingerprint TEXT NOT NULL UNIQUE,
                device_fingerprint TEXT NOT NULL REFERENCES devices(device_fingerprint),
                protocol_version INTEGER NOT NULL CHECK (protocol_version = 1),
                generation INTEGER NOT NULL CHECK (generation >= 1),
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                controller_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 1),
                payload_digest TEXT NOT NULL,
                host_incarnation TEXT NOT NULL,
                helper_incarnation TEXT NOT NULL,
                deadline_ns INTEGER NOT NULL CHECK (deadline_ns >= 0),
                admitted_ns INTEGER NOT NULL CHECK (admitted_ns >= 0),
                status TEXT NOT NULL CHECK (status IN ('queued', 'uncertain', 'succeeded', 'rejected', 'expired', 'recovered')),
                provider_incarnation TEXT,
                result_digest TEXT,
                terminal_ns INTEGER,
                UNIQUE (device_fingerprint, generation, controller_id, sequence)
            ) WITHOUT ROWID;

            CREATE TABLE operation_receipts (
                receipt_id TEXT PRIMARY KEY,
                receipt_fingerprint TEXT NOT NULL UNIQUE,
                operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                generation INTEGER NOT NULL,
                provider_incarnation TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('succeeded', 'rejected', 'unknown')),
                result_digest TEXT NOT NULL,
                observed_ns INTEGER NOT NULL CHECK (observed_ns >= 0),
                binding TEXT NOT NULL CHECK (binding IN ('current', 'late'))
            ) WITHOUT ROWID;

            CREATE TABLE reconciliations (
                reconciliation_id TEXT PRIMARY KEY,
                reconciliation_fingerprint TEXT NOT NULL UNIQUE,
                device_fingerprint TEXT NOT NULL REFERENCES devices(device_fingerprint),
                prior_generation INTEGER NOT NULL,
                prior_host_incarnation TEXT NOT NULL,
                prior_helper_incarnation TEXT NOT NULL,
                fresh_helper_incarnation TEXT NOT NULL,
                prior_helper_exit_digest TEXT NOT NULL,
                pointer_cleanup_digest TEXT NOT NULL,
                fresh_handshake_digest TEXT NOT NULL,
                observed_ns INTEGER NOT NULL CHECK (observed_ns >= 0)
            ) WITHOUT ROWID;

            CREATE TABLE reconciliation_dispositions (
                reconciliation_id TEXT NOT NULL REFERENCES reconciliations(reconciliation_id),
                operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                terminal_status TEXT NOT NULL CHECK (terminal_status IN ('not-dispatched', 'succeeded', 'rejected', 'recovered')),
                result_digest TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                disposition_fingerprint TEXT NOT NULL,
                PRIMARY KEY (reconciliation_id, operation_id)
            ) WITHOUT ROWID;

            CREATE TABLE legacy_adoptions (
                adoption_id TEXT PRIMARY KEY,
                artifact_digest TEXT NOT NULL,
                semantics TEXT NOT NULL CHECK (semantics IN ('lock-only', 'unverified-history')),
                adoption_fingerprint TEXT NOT NULL UNIQUE
            ) WITHOUT ROWID;
            """
        return tuple(statement.strip() for statement in script.split(";") if statement.strip())

    def _create_schema(self, connection):
        for statement in self._schema_statements():
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO authority_metadata(key, value) VALUES (?, ?)",
            (
                ("format_version", str(FORMAT_VERSION)),
                ("minimum_reader_version", str(READER_VERSION)),
                ("minimum_writer_version", str(WRITER_VERSION)),
            ),
        )
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {FORMAT_VERSION}")

    def _migrate_v1(self, connection):
        """Rebuild affected CHECK constraints atomically with foreign keys enabled.

        Child rows are copied before dropping their parent table. Temporary
        copies, schema changes, history restoration and version metadata all
        belong to the caller's transaction, including rollback on interruption.
        """
        self._validate_migration_schema(connection, version=1)
        _require(not connection.execute("PRAGMA foreign_key_check").fetchall(),
                 "Authority store references are incompatible")
        tables = ("operations", "operation_receipts", "reconciliation_dispositions")
        for table in tables:
            connection.execute(f"CREATE TEMP TABLE migration_{table} AS SELECT * FROM {table}")
        for table in reversed(tables):
            connection.execute(f"DROP TABLE {table}")
        for statement in self._schema_statements():
            if statement.split()[2] in tables:
                connection.execute(statement)
        for table in tables:
            connection.execute(f"INSERT INTO {table} SELECT * FROM migration_{table}")
            connection.execute(f"DROP TABLE migration_{table}")
        _require(not connection.execute("PRAGMA foreign_key_check").fetchall(),
                 "Authority store references are incompatible")
        self._set_migrated_version(connection)

    def _validate_migration_schema(self, connection, *, version):
        expected_schema = {}
        for statement in self._schema_statements():
            if version == 1:
                statement = statement.replace(", 'recovered'", "")
            expected_schema[statement.split()[2]] = " ".join(statement.split())
        actual_schema = {
            name: " ".join(sql.split()) for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            )
        }
        _require(actual_schema == expected_schema, "Authority store schema is incompatible")

    def _migrate_v2(self, connection):
        # Table layouts are unchanged. The version barrier prevents an older
        # reader/writer from treating pending final cleanup as ordinary quarantine.
        self._validate_migration_schema(connection, version=2)
        _require(not connection.execute("PRAGMA foreign_key_check").fetchall(),
                 "Authority store references are incompatible")
        self._set_migrated_version(connection)

    @staticmethod
    def _set_migrated_version(connection):
        connection.executemany(
            "UPDATE authority_metadata SET value = ? WHERE key = ?",
            ((str(FORMAT_VERSION), "format_version"),
             (str(READER_VERSION), "minimum_reader_version"),
             (str(WRITER_VERSION), "minimum_writer_version")),
        )
        connection.execute(f"PRAGMA user_version = {FORMAT_VERSION}")

    @contextmanager
    def _transaction(self, *, exclusive=False):
        with self._lock:
            _require(not self._closed, "Authority state store is closed")
            connection = self._connection
            try:
                connection.execute("BEGIN EXCLUSIVE" if exclusive else "BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except ContractError:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise
            except sqlite3.Error:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise ContractError("Authority state operation failed") from None

    @staticmethod
    def _row(row):
        return None if row is None else dict(row)

    def close(self):
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    @property
    def max_page_count(self):
        with self._lock:
            _require(not self._closed, "Authority state store is closed")
            try:
                return self._connection.execute("PRAGMA max_page_count").fetchone()[0]
            except sqlite3.Error:
                raise ContractError("Authority state operation failed") from None

    def device(self, device_fingerprint):
        with self._lock:
            _require(not self._closed, "Authority state store is closed")
            try:
                row = self._connection.execute(
                    "SELECT * FROM devices WHERE device_fingerprint = ?",
                    (device_fingerprint,),
                ).fetchone()
            except sqlite3.Error:
                raise ContractError("Authority state operation failed") from None
        return self._row(row)

    def operation(self, operation_id):
        with self._lock:
            _require(not self._closed, "Authority state store is closed")
            try:
                row = self._connection.execute(
                    "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
                ).fetchone()
            except sqlite3.Error:
                raise ContractError("Authority state operation failed") from None
        return self._row(row)

    def reconciliation(self, reconciliation_id):
        with self._lock:
            _require(not self._closed, "Authority state store is closed")
            try:
                return self._row(self._connection.execute(
                    "SELECT * FROM reconciliations WHERE reconciliation_id = ?",
                    (reconciliation_id,),
                ).fetchone())
            except sqlite3.Error:
                raise ContractError("Authority state operation failed") from None

    def claim_device(
        self,
        *,
        device_fingerprint,
        device_kind,
        host_incarnation,
        helper_incarnation,
        grant,
        now_ns,
    ):
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?",
                (device_fingerprint,),
            ).fetchone()
            if row is None:
                count = connection.execute("SELECT count(*) FROM devices").fetchone()[0]
                _require(count < MAX_DEVICES, "Authority device limit reached")
                generation = 1
                connection.execute(
                    """INSERT INTO devices(
                        device_fingerprint, device_kind, generation, status,
                        host_incarnation, helper_incarnation, grant_id, project_id,
                        controller_id, renewal_sequence, parent_deadline_ns,
                        mapping_id, quarantine_reason, updated_ns
                    ) VALUES (?, ?, ?, 'owned', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                    (
                        device_fingerprint,
                        device_kind,
                        generation,
                        host_incarnation,
                        helper_incarnation,
                        grant["grant_id"],
                        grant["project_id"],
                        grant["controller_id"],
                        grant["renewal_sequence"],
                        grant["local_deadline_ns"],
                        grant["mapping_id"],
                        now_ns,
                    ),
                )
                return self._row(connection.execute(
                    "SELECT * FROM devices WHERE device_fingerprint = ?",
                    (device_fingerprint,),
                ).fetchone())

            current = dict(row)
            _require(current["device_kind"] == device_kind, "Canonical device identity conflict")
            if current["status"] == "released":
                generation = current["generation"] + 1
                connection.execute(
                    """UPDATE devices SET generation = ?, status = 'owned',
                        host_incarnation = ?, helper_incarnation = ?, grant_id = ?,
                        project_id = ?, controller_id = ?, renewal_sequence = ?,
                        parent_deadline_ns = ?, mapping_id = ?, quarantine_reason = NULL,
                        updated_ns = ? WHERE device_fingerprint = ?""",
                    (
                        generation,
                        host_incarnation,
                        helper_incarnation,
                        grant["grant_id"],
                        grant["project_id"],
                        grant["controller_id"],
                        grant["renewal_sequence"],
                        grant["local_deadline_ns"],
                        grant["mapping_id"],
                        now_ns,
                        device_fingerprint,
                    ),
                )
            else:
                reason = current["quarantine_reason"] or "restart-ownership-unresolved"
                connection.execute(
                    """UPDATE devices SET status = 'quarantined',
                        quarantine_reason = ?, updated_ns = ?
                        WHERE device_fingerprint = ?""",
                    (reason, now_ns, device_fingerprint),
                )
            return self._row(connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?",
                (device_fingerprint,),
            ).fetchone())

    def renew_device(self, *, device_fingerprint, generation, host_incarnation, grant, now_ns):
        expired = False
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?", (device_fingerprint,)
            ).fetchone()
            _require(row is not None, "Device authority is unavailable")
            current = dict(row)
            _require(
                current["generation"] == generation
                and current["host_incarnation"] == host_incarnation,
                "Device authority incarnation mismatch",
            )
            if current["status"] == "expired" or now_ns >= current["parent_deadline_ns"]:
                connection.execute(
                    """UPDATE devices SET status = 'expired',
                       quarantine_reason = 'parent-expired', updated_ns = ?
                       WHERE device_fingerprint = ? AND status IN ('owned', 'expired')""",
                    (now_ns, device_fingerprint),
                )
                expired = True
            else:
                _require(current["status"] == "owned", "Device requires reconciliation")
                _require(
                    grant["grant_id"] == current["grant_id"]
                    and grant["project_id"] == current["project_id"]
                    and grant["controller_id"] == current["controller_id"],
                    "Parent grant binding mismatch",
                )
                _require(
                    grant["renewal_sequence"] > current["renewal_sequence"],
                    "Renewal sequence is stale",
                )
                connection.execute(
                    """UPDATE devices SET renewal_sequence = ?, parent_deadline_ns = ?,
                        mapping_id = ?, updated_ns = ? WHERE device_fingerprint = ?""",
                    (
                        grant["renewal_sequence"],
                        grant["local_deadline_ns"],
                        grant["mapping_id"],
                        now_ns,
                        device_fingerprint,
                    ),
                )
            result = self._row(connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?", (device_fingerprint,)
            ).fetchone())
        # The denial must not roll back the durable expiry transition.
        _require(not expired, "Expired authority cannot be renewed")
        return result

    def admit_operation(self, operation):
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation["operation_id"],),
            ).fetchone()
            if existing is not None:
                current = dict(existing)
                _require(
                    current["operation_fingerprint"] == operation["operation_fingerprint"],
                    "Operation identity conflict",
                )
                current["is_new"] = False
                return current

            device = connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?",
                (operation["device_fingerprint"],),
            ).fetchone()
            _require(device is not None, "Device authority is unavailable")
            device = dict(device)
            _require(
                device["status"] == "owned"
                and device["generation"] == operation["generation"]
                and device["host_incarnation"] == operation["host_incarnation"],
                "Device requires reconciliation",
            )
            _require(operation["admitted_ns"] < device["parent_deadline_ns"],
                     "Operation authority expired")
            _require(operation["deadline_ns"] <= device["parent_deadline_ns"],
                     "Operation exceeds parent grant")
            watermark = connection.execute(
                """SELECT highest_sequence FROM replay_watermarks
                   WHERE device_fingerprint = ? AND generation = ? AND controller_id = ?""",
                (
                    operation["device_fingerprint"],
                    operation["generation"],
                    operation["controller_id"],
                ),
            ).fetchone()
            highest = 0 if watermark is None else watermark[0]
            _require(operation["sequence"] > highest,
                     "Operation sequence was already consumed")
            _require(operation["sequence"] == highest + 1,
                     "Operation sequence is not contiguous")
            count = connection.execute(
                "SELECT count(*) FROM operations WHERE device_fingerprint = ?",
                (operation["device_fingerprint"],),
            ).fetchone()[0]
            _require(count < MAX_OPERATIONS_PER_DEVICE, "Authority operation limit reached")
            connection.execute(
                """INSERT INTO operations(
                    operation_id, operation_fingerprint, device_fingerprint,
                    protocol_version, generation, project_id, session_id,
                    controller_id, sequence, payload_digest, host_incarnation,
                    helper_incarnation, deadline_ns, admitted_ns,
                    status, provider_incarnation, result_digest, terminal_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL)""",
                (
                    operation["operation_id"],
                    operation["operation_fingerprint"],
                    operation["device_fingerprint"],
                    operation["protocol_version"],
                    operation["generation"],
                    operation["project_id"],
                    operation["session_id"],
                    operation["controller_id"],
                    operation["sequence"],
                    operation["payload_digest"],
                    operation["host_incarnation"],
                    operation["helper_incarnation"],
                    operation["deadline_ns"],
                    operation["admitted_ns"],
                ),
            )
            connection.execute(
                """INSERT INTO replay_watermarks(
                       device_fingerprint, generation, controller_id, highest_sequence
                   ) VALUES (?, ?, ?, ?)
                   ON CONFLICT(device_fingerprint, generation, controller_id)
                   DO UPDATE SET highest_sequence = excluded.highest_sequence""",
                (
                    operation["device_fingerprint"],
                    operation["generation"],
                    operation["controller_id"],
                    operation["sequence"],
                ),
            )
            result = dict(connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation["operation_id"],),
            ).fetchone())
            result["is_new"] = True
            return result

    def expire_queued_operation(
        self, *, device_fingerprint, generation, host_incarnation, operation_id, now_ns
    ):
        with self._transaction() as connection:
            device = connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?", (device_fingerprint,)
            ).fetchone()
            _require(device is not None, "Device authority is unavailable")
            current = dict(device)
            _require(
                current["generation"] == generation
                and current["host_incarnation"] == host_incarnation,
                "Device authority incarnation mismatch",
            )
            connection.execute(
                """UPDATE operations SET status = 'expired', terminal_ns = ?
                   WHERE operation_id = ? AND device_fingerprint = ?
                   AND generation = ? AND host_incarnation = ? AND status = 'queued'""",
                (now_ns, operation_id, device_fingerprint, generation, host_incarnation),
            )
            if now_ns >= current["parent_deadline_ns"]:
                connection.execute(
                    """UPDATE devices SET status = 'expired',
                       quarantine_reason = 'parent-expired', updated_ns = ?
                       WHERE device_fingerprint = ? AND status IN ('owned', 'expired')""",
                    (now_ns, device_fingerprint),
                )

    def quarantine_clock(self, *, device_fingerprint, generation, host_incarnation, now_ns):
        with self._transaction() as connection:
            changed = connection.execute(
                """UPDATE devices SET status = 'quarantined',
                   quarantine_reason = CASE WHEN quarantine_reason = 'recovery-cleanup-pending'
                       THEN quarantine_reason ELSE 'clock-incompatible' END,
                   updated_ns = COALESCE(?, updated_ns)
                   WHERE device_fingerprint = ? AND generation = ? AND host_incarnation = ?""",
                (now_ns, device_fingerprint, generation, host_incarnation),
            ).rowcount
            _require(changed == 1, "Device authority incarnation mismatch")

    def revoke_dispatches(self, *, device_fingerprint, generation, host_incarnation, now_ns):
        """Durably prevent an in-flight permit from restoring live ownership."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?", (device_fingerprint,)
            ).fetchone()
            _require(row is not None and row["generation"] == generation
                     and row["host_incarnation"] == host_incarnation,
                     "Device authority incarnation mismatch")
            if row["status"] in ("owned", "expired", "quarantined"):
                connection.execute(
                    """UPDATE devices SET status = 'quarantined',
                       quarantine_reason = CASE WHEN quarantine_reason = 'recovery-cleanup-pending'
                           THEN quarantine_reason ELSE 'host-session-revoked' END,
                       updated_ns = COALESCE(?, updated_ns)
                       WHERE device_fingerprint = ?""",
                    (now_ns, device_fingerprint),
                )

    def fence_native_recovery(self, *, device_fingerprint, generation, host_incarnation,
                              helper_incarnation, now_ns):
        """Fence original receipts atomically before a recovery owner gets descriptors."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?", (device_fingerprint,)
            ).fetchone()
            _require(row is not None and row["generation"] == generation
                and row["host_incarnation"] == host_incarnation
                and row["helper_incarnation"] == helper_incarnation
                and row["status"] == "quarantined", "Current device recovery snapshot required")
            if row["quarantine_reason"] == "provider-outcome-unconfirmed":
                connection.execute(
                    """UPDATE devices SET quarantine_reason = 'host-recovery-started', updated_ns = ?
                       WHERE device_fingerprint = ?""", (now_ns, device_fingerprint),
                )

    def prepare_dispatch(
        self,
        *,
        operation_id,
        operation_fingerprint,
        device_fingerprint,
        generation,
        host_incarnation,
        provider_incarnation,
        now_ns,
    ):
        with self._transaction() as connection:
            device = connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?", (device_fingerprint,)
            ).fetchone()
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            _require(device is not None and operation is not None,
                     "Operation authority is unavailable")
            device = dict(device)
            operation = dict(operation)
            _require(
                device["status"] == "owned"
                and device["generation"] == generation
                and device["host_incarnation"] == host_incarnation,
                "Device requires reconciliation",
            )
            _require(
                operation["operation_fingerprint"] == operation_fingerprint
                and operation["device_fingerprint"] == device_fingerprint
                and operation["protocol_version"] == 1
                and operation["generation"] == generation
                and operation["host_incarnation"] == host_incarnation,
                "Operation authority mismatch",
            )
            _require(operation["status"] == "queued", "Operation is not dispatchable")
            earlier = connection.execute(
                """SELECT 1 FROM operations
                   WHERE device_fingerprint = ? AND generation = ?
                   AND sequence < ? AND status IN ('queued', 'uncertain') LIMIT 1""",
                (device_fingerprint, generation, operation["sequence"]),
            ).fetchone()
            _require(earlier is None, "Earlier operation is not terminal")
            _require(
                now_ns < operation["deadline_ns"]
                and now_ns < device["parent_deadline_ns"],
                "Operation authority expired",
            )
            connection.execute(
                """UPDATE operations SET status = 'uncertain', provider_incarnation = ?
                   WHERE operation_id = ?""",
                (provider_incarnation, operation_id),
            )
            connection.execute(
                """UPDATE devices SET status = 'quarantined',
                   quarantine_reason = 'provider-outcome-unconfirmed', updated_ns = ?
                   WHERE device_fingerprint = ?""",
                (now_ns, device_fingerprint),
            )
            return self._row(connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone())

    def acknowledge_result(
        self,
        *,
        receipt_id,
        receipt_fingerprint,
        operation_id,
        generation,
        host_incarnation,
        provider_incarnation,
        status,
        result_digest,
        observed_ns,
    ):
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM operation_receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if existing is not None:
                existing = dict(existing)
                _require(existing["receipt_fingerprint"] == receipt_fingerprint,
                         "Provider receipt identity conflict")
                return existing
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            _require(operation is not None, "Unknown operation receipt")
            operation = dict(operation)
            count = connection.execute(
                "SELECT count(*) FROM operation_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()[0]
            _require(count < MAX_RECEIPTS_PER_OPERATION, "Provider receipt limit reached")
            device = connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?",
                (operation["device_fingerprint"],),
            ).fetchone()
            device = None if device is None else dict(device)
            current = (
                operation["status"] == "uncertain"
                and operation["generation"] == generation
                and operation["host_incarnation"] == host_incarnation
                and operation["provider_incarnation"] == provider_incarnation
                and device is not None
                and device["generation"] == generation
                and device["host_incarnation"] == host_incarnation
                and device["status"] == "quarantined"
                and device["quarantine_reason"] == "provider-outcome-unconfirmed"
            )
            binding = "current" if current else "late"
            connection.execute(
                """INSERT INTO operation_receipts(
                    receipt_id, receipt_fingerprint, operation_id, generation,
                    provider_incarnation, status, result_digest, observed_ns, binding
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt_id,
                    receipt_fingerprint,
                    operation_id,
                    generation,
                    provider_incarnation,
                    status,
                    result_digest,
                    observed_ns,
                    binding,
                ),
            )
            if current and status in ("succeeded", "rejected"):
                connection.execute(
                    """UPDATE operations SET status = ?, result_digest = ?, terminal_ns = ?
                       WHERE operation_id = ?""",
                    (status, result_digest, observed_ns, operation_id),
                )
                next_status = "owned" if observed_ns < device["parent_deadline_ns"] else "expired"
                reason = None if next_status == "owned" else "parent-expired"
                connection.execute(
                    """UPDATE devices SET status = ?, quarantine_reason = ?, updated_ns = ?
                       WHERE device_fingerprint = ?""",
                    (next_status, reason, observed_ns, operation["device_fingerprint"]),
                )
            return self._row(connection.execute(
                "SELECT * FROM operation_receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone())

    def recovery_state(self, device_fingerprint):
        with self._lock:
            _require(not self._closed, "Authority state store is closed")
            try:
                device = self._connection.execute(
                    "SELECT * FROM devices WHERE device_fingerprint = ?",
                    (device_fingerprint,),
                ).fetchone()
                _require(device is not None and device["status"] == "quarantined",
                         "Device does not require reconciliation")
                operations = [
                    dict(row)
                    for row in self._connection.execute(
                        """SELECT * FROM operations WHERE device_fingerprint = ?
                           AND generation = ? AND status IN ('queued', 'uncertain')
                           ORDER BY sequence""",
                        (device_fingerprint, device["generation"]),
                    )
                ]
            except sqlite3.Error:
                raise ContractError("Authority state operation failed") from None
        return dict(device), operations

    def reconcile_device(
        self,
        *,
        reconciliation,
        dispositions,
        host_incarnation,
        fresh_helper_incarnation,
        grant,
        now_ns,
        cleanup_pending=False,
    ):
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?",
                (reconciliation["device_fingerprint"],),
            ).fetchone()
            _require(row is not None, "Device authority is unavailable")
            device = dict(row)
            _require(type(cleanup_pending) is bool, "Invalid reconciliation cleanup state")
            _require(device["quarantine_reason"] != "recovery-cleanup-pending",
                     "Recovery cleanup is still pending")
            _require(
                device["status"] == "quarantined"
                and device["generation"] == reconciliation["prior_generation"]
                and device["host_incarnation"] == reconciliation["prior_host_incarnation"]
                and device["helper_incarnation"] == reconciliation["prior_helper_incarnation"],
                "Reconciliation binding mismatch",
            )
            _require(grant["grant_id"] != device["grant_id"],
                     "Recovery requires a fresh parent grant")
            _require(now_ns < grant["local_deadline_ns"], "Operation authority expired")
            unfinished = {
                row["operation_id"]: dict(row)
                for row in connection.execute(
                    """SELECT * FROM operations WHERE device_fingerprint = ?
                       AND generation = ? AND status IN ('queued', 'uncertain')""",
                    (device["device_fingerprint"], device["generation"]),
                )
            }
            _require(set(unfinished) == set(dispositions),
                     "Reconciliation does not cover unfinished operations")
            for operation_id, disposition in dispositions.items():
                operation = unfinished[operation_id]
                status = disposition["terminal_status"]
                if operation["status"] == "queued":
                    _require(status == "not-dispatched",
                             "Queued operation disposition is invalid")
                    terminal_status = "rejected"
                else:
                    _require(status in ("succeeded", "rejected", "recovered"),
                             "Uncertain operation needs a terminal disposition")
                    terminal_status = status
                connection.execute(
                    """UPDATE operations SET status = ?,
                       result_digest = CASE WHEN ? = 'recovered' THEN result_digest ELSE ? END,
                       terminal_ns = ?
                       WHERE operation_id = ?""",
                    (
                        terminal_status,
                        terminal_status,
                        disposition["result_digest"],
                        now_ns,
                        operation_id,
                    ),
                )
            count = connection.execute(
                "SELECT count(*) FROM reconciliations WHERE device_fingerprint = ?",
                (device["device_fingerprint"],),
            ).fetchone()[0]
            _require(count < MAX_RECONCILIATIONS_PER_DEVICE,
                     "Authority reconciliation limit reached")
            connection.execute(
                """INSERT INTO reconciliations(
                    reconciliation_id, reconciliation_fingerprint,
                    device_fingerprint, prior_generation, prior_host_incarnation,
                    prior_helper_incarnation, fresh_helper_incarnation,
                    prior_helper_exit_digest, pointer_cleanup_digest,
                    fresh_handshake_digest, observed_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    reconciliation["reconciliation_id"],
                    reconciliation["reconciliation_fingerprint"],
                    device["device_fingerprint"],
                    device["generation"],
                    device["host_incarnation"],
                    device["helper_incarnation"],
                    fresh_helper_incarnation,
                    reconciliation["prior_helper_exit_digest"],
                    reconciliation["pointer_cleanup_digest"],
                    reconciliation["fresh_handshake_digest"],
                    now_ns,
                ),
            )
            for operation_id, disposition in dispositions.items():
                connection.execute(
                    """INSERT INTO reconciliation_dispositions(
                        reconciliation_id, operation_id, terminal_status,
                        result_digest, evidence_digest, disposition_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        reconciliation["reconciliation_id"],
                        operation_id,
                        disposition["terminal_status"],
                        disposition["result_digest"],
                        disposition["evidence_digest"],
                        disposition["disposition_fingerprint"],
                    ),
                )
            generation = device["generation"] + 1
            connection.execute(
                """UPDATE devices SET generation = ?, status = ?,
                   host_incarnation = ?, helper_incarnation = ?, grant_id = ?,
                   project_id = ?, controller_id = ?, renewal_sequence = ?,
                   parent_deadline_ns = ?, mapping_id = ?, quarantine_reason = ?,
                   updated_ns = ? WHERE device_fingerprint = ?""",
                (
                    generation,
                    "quarantined" if cleanup_pending else "owned",
                    host_incarnation,
                    fresh_helper_incarnation,
                    grant["grant_id"],
                    grant["project_id"],
                    grant["controller_id"],
                    grant["renewal_sequence"],
                    grant["local_deadline_ns"],
                    grant["mapping_id"],
                    "recovery-cleanup-pending" if cleanup_pending else None,
                    now_ns,
                    device["device_fingerprint"],
                ),
            )
            return self._row(connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?",
                (device["device_fingerprint"],),
            ).fetchone())

    def finish_reconciliation_cleanup(self, *, reconciliation_id, reconciliation_fingerprint,
                                      device_fingerprint, generation, host_incarnation,
                                      helper_incarnation, now_ns):
        """Release a reconciled generation only after its trusted owner cleaned it."""
        with self._transaction() as connection:
            reconciliation = connection.execute(
                "SELECT * FROM reconciliations WHERE reconciliation_id = ?", (reconciliation_id,)
            ).fetchone()
            device = connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?", (device_fingerprint,)
            ).fetchone()
            _require(reconciliation is not None and device is not None
                and reconciliation["reconciliation_fingerprint"] == reconciliation_fingerprint
                and reconciliation["device_fingerprint"] == device_fingerprint
                and reconciliation["prior_generation"] + 1 == generation
                and reconciliation["fresh_helper_incarnation"] == helper_incarnation
                and device["generation"] == generation
                and device["host_incarnation"] == host_incarnation
                and device["helper_incarnation"] == helper_incarnation
                and device["status"] == "quarantined"
                and device["quarantine_reason"] == "recovery-cleanup-pending",
                "Reconciliation cleanup binding mismatch")
            connection.execute(
                """UPDATE devices SET status = 'released', quarantine_reason = NULL,
                   updated_ns = ? WHERE device_fingerprint = ?""",
                (now_ns, device_fingerprint),
            )
            return self._row(connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?", (device_fingerprint,)
            ).fetchone())

    def release_device(self, *, device_fingerprint, generation, host_incarnation, now_ns):
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE device_fingerprint = ?",
                (device_fingerprint,),
            ).fetchone()
            if row is None:
                return
            device = dict(row)
            if (
                device["generation"] != generation
                or device["host_incarnation"] != host_incarnation
            ):
                return
            if device["status"] == "quarantined":
                return
            connection.execute(
                """UPDATE operations SET status = 'rejected', result_digest = ?, terminal_ns = ?
                   WHERE device_fingerprint = ? AND generation = ? AND status = 'queued'""",
                (RELEASED_RESULT_DIGEST, now_ns, device_fingerprint, generation),
            )
            connection.execute(
                """UPDATE devices SET status = 'released', quarantine_reason = NULL,
                   updated_ns = ? WHERE device_fingerprint = ?""",
                (now_ns, device_fingerprint),
            )

    def record_legacy_adoption(self, *, adoption_id, artifact_digest, semantics):
        value = {
            "adoption_id": adoption_id,
            "artifact_digest": artifact_digest,
            "semantics": semantics,
        }
        fingerprint = _canonical_digest(value)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM legacy_adoptions WHERE adoption_id = ?", (adoption_id,)
            ).fetchone()
            if existing is not None:
                _require(existing["adoption_fingerprint"] == fingerprint,
                         "Legacy adoption identity conflict")
                return dict(existing)
            count = connection.execute("SELECT count(*) FROM legacy_adoptions").fetchone()[0]
            _require(count < MAX_LEGACY_ADOPTIONS, "Legacy adoption limit reached")
            connection.execute(
                """INSERT INTO legacy_adoptions(
                    adoption_id, artifact_digest, semantics, adoption_fingerprint
                ) VALUES (?, ?, ?, ?)""",
                (adoption_id, artifact_digest, semantics, fingerprint),
            )
            return dict(connection.execute(
                "SELECT * FROM legacy_adoptions WHERE adoption_id = ?", (adoption_id,)
            ).fetchone())


__all__ = [
    "FORMAT_VERSION",
    "READER_VERSION",
    "StateStore",
    "WRITER_VERSION",
]
