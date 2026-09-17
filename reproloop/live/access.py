"""Durable G5 identities, memberships, resource bindings, and host scope.

The access database is deliberately separate from the G1 host-authority
journal.  Wire data can identify a project, but only a process-local project
registration bound through :class:`AccessController` can make it usable.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from typing import Any

from .. import contracts
from ..core import ContractError


ACCESS_FORMAT_VERSION = 2
ACCESS_READER_VERSION = 2
ACCESS_WRITER_VERSION = 2
ACCESS_APPLICATION_ID = 0x52504C35  # "RPL5"
ACCESS_SERVICE = "reproloop-coordinator-access"
MAX_DATABASE_PAGES = 16_384
MAX_LEGACY_BYTES = 100 * 1024 * 1024
MAX_BROWSER_SESSIONS = 4_096
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ROLES = frozenset({"viewer", "operator", "maintainer"})
_ROLE_CAPABILITIES = {
    "viewer": frozenset({
        "health.read", "project.read", "device.read", "session.read",
        "recording.read", "media.read", "issue.read", "job.read",
        "export.read",
    }),
    "operator": frozenset({
        "health.read", "project.read", "device.read", "session.read",
        "recording.read", "media.read", "issue.read", "job.read",
        "device.operate", "session.create", "session.operate",
        "recording.create", "evidence.collect", "replay.execute",
        "job.manage", "fixture.execute",
    }),
    "maintainer": frozenset({
        "health.read", "project.read", "device.read", "session.read",
        "recording.read", "media.read", "issue.read", "job.read",
        "export.read", "project.maintain", "specification.maintain",
        "recording.import", "recording.derive", "resource.bind",
    }),
    "administrator": frozenset({
        "administration.read", "identity.manage", "membership.manage",
        "project.register", "host.manage", "device.assign",
        "credential.manage", "legacy.adopt",
    }),
}
_GLOBAL_CAPABILITIES = _ROLE_CAPABILITIES["administrator"]
_RESOURCE_KINDS = frozenset({"session", "recording", "job", "issue", "specification"})
_RESOURCE_MEANINGS = frozenset({"release", "legacy-inert", "legacy-authorized", "approved"})
_LEGACY_MEANINGS = frozenset({
    "legacy-recording-only", "legacy-result-only", "legacy-authority-history",
    "legacy-configuration-reference",
})


class AccessError(ContractError):
    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code = code
        self.status = status


def _deny(code="forbidden", message="Access is not permitted", status=403):
    raise AccessError(code, message, status)


def _require(value, message="Invalid access configuration"):
    if not value:
        raise ContractError(message)


def _identifier(value, label="identifier"):
    _require(isinstance(value, str) and _ID.fullmatch(value) is not None,
             f"Invalid {label}")
    return value


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeError):
        raise ContractError("Invalid access data") from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_json_bytes(value):
    try:
        return json.loads(value, object_pairs_hook=_pairs,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ContractError("Invalid access data") from None


def _token(prefix, identifier):
    secret = secrets.token_urlsafe(32)
    return f"{prefix}.{identifier}.{secret}", secret


def _parse_token(value, prefix):
    if not isinstance(value, str) or len(value) > 768:
        _deny("unauthorized", "Authentication failed", 401)
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != prefix or _ID.fullmatch(parts[1]) is None \
            or re.fullmatch(r"[A-Za-z0-9_-]{32,512}", parts[2]) is None:
        _deny("unauthorized", "Authentication failed", 401)
    return parts[1], hashlib.sha256(parts[2].encode("ascii", "strict")).hexdigest()


def _public_row(row, *, omit=()):
    if row is None:
        return None
    return {key: copy.deepcopy(value) for key, value in dict(row).items() if key not in omit}


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    principal_id: str
    credential_id: str
    expires_at: int
    _issuer: object = field(repr=False, compare=False)
    authorization_id: str | None = field(default=None, repr=False, compare=False)
    _authorization_guard: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class HostContext:
    host_id: str
    generation: int
    incarnation: str
    credential_id: str
    expires_at: int
    project_ids: tuple[str, ...]
    trust_groups: tuple[str, ...]
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ResourceBinding:
    kind: str
    resource_id: str
    project_id: str
    project_digest: str
    owner_id: str
    meaning: str
    adoption_id: str | None
    authorized_digest: str | None


class AccessStore:
    """Strongly serialized coordinator access state in an isolated v2 root."""

    _TABLES = {
        "access_metadata", "identities", "projects", "memberships",
        "credentials", "resources", "device_assignments",
        "sanitation_receipts", "enrollments", "hosts", "legacy_adoptions",
    }

    def __init__(self, root, *, clock=None, reader_version=ACCESS_READER_VERSION,
                 writer_version=ACCESS_WRITER_VERSION):
        self.root = self._safe_root(root)
        self.path = self.root / "access.sqlite3"
        self._clock = clock or (lambda: int(time.time()))
        self._reader_version = reader_version
        self._writer_version = writer_version
        self._issuer = object()
        self._lock = threading.RLock()
        self._closed = False
        self._initialize_namespace()
        existing = self.path.exists() or self.path.is_symlink()
        if existing:
            self._preflight_existing()
        try:
            self._connection = sqlite3.connect(
                self.path, timeout=3.0, isolation_level=None,
                check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 3000")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA wal_autocheckpoint = 256")
            if existing:
                self._validate(self._connection)
            else:
                with self._transaction(exclusive=True) as connection:
                    self._create_schema(connection)
            pages = self._connection.execute(
                f"PRAGMA max_page_count = {MAX_DATABASE_PAGES}").fetchone()[0]
            _require(pages <= MAX_DATABASE_PAGES, "Access state exceeds its quota")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except ContractError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise ContractError("Access state operation failed") from None

    @staticmethod
    def _safe_root(root):
        try:
            value = Path(root).absolute()
            if value.exists() or value.is_symlink():
                info = value.lstat()
                _require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                         and info.st_uid == os.getuid(), "Unsafe access state root")
            else:
                value.mkdir(parents=True, mode=0o700)
            os.chmod(value, 0o700)
            return value
        except (OSError, TypeError, ValueError):
            raise ContractError("Unsafe access state root") from None

    def _initialize_namespace(self):
        marker = self.root / "namespace.json"
        expected = {
            "schemaVersion": ACCESS_FORMAT_VERSION,
            "service": ACCESS_SERVICE,
            "minimumReaderVersion": ACCESS_READER_VERSION,
            "minimumWriterVersion": ACCESS_WRITER_VERSION,
        }
        if marker.exists() or marker.is_symlink():
            try:
                info = marker.lstat()
                _require(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                         and info.st_uid == os.getuid(), "Access namespace is incompatible")
                value = _load_json_bytes(marker.read_bytes())
            except OSError:
                raise ContractError("Access namespace is incompatible") from None
            _require(value == expected, "Access namespace is incompatible")
            return
        entries = list(self.root.iterdir())
        _require(not entries, "Access namespace is incompatible")
        encoded = (_json(expected) + "\n").encode()
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded);handle.flush();os.fsync(handle.fileno())
        except FileExistsError:
            value = _load_json_bytes(marker.read_bytes())
            _require(value == expected, "Access namespace is incompatible")
        except OSError:
            raise ContractError("Access namespace could not be initialized") from None

    def _preflight_existing(self):
        try:
            info = self.path.lstat()
            _require(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                     and info.st_uid == os.getuid(), "Unsafe access state path")
            connection = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=1.0,
                isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                self._validate(connection)
            finally:
                connection.close()
        except ContractError:
            raise
        except (OSError, sqlite3.Error):
            raise ContractError("Access store version is incompatible") from None

    def _validate(self, connection):
        try:
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            _require(user_version == ACCESS_FORMAT_VERSION
                     and application_id == ACCESS_APPLICATION_ID,
                     "Access store version is incompatible")
            _require(tables == self._TABLES, "Access store schema is incompatible")
            metadata = dict(connection.execute("SELECT key,value FROM access_metadata"))
            _require(metadata == {
                "format_version": str(ACCESS_FORMAT_VERSION),
                "minimum_reader_version": str(ACCESS_READER_VERSION),
                "minimum_writer_version": str(ACCESS_WRITER_VERSION),
                "service": ACCESS_SERVICE,
            }, "Access store version is incompatible")
            _require(int(metadata["minimum_reader_version"]) <= self._reader_version
                     and int(metadata["minimum_writer_version"]) <= self._writer_version,
                     "Access store version is incompatible")
        except ContractError:
            raise
        except (sqlite3.Error, ValueError, TypeError):
            raise ContractError("Access store version is incompatible") from None

    @staticmethod
    def _create_schema(connection):
        script = """
        CREATE TABLE access_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE identities(
            identity_id TEXT PRIMARY KEY, administrator INTEGER NOT NULL CHECK(administrator IN (0,1)),
            active INTEGER NOT NULL CHECK(active IN (0,1)), created_at INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE projects(
            project_id TEXT PRIMARY KEY, revision TEXT NOT NULL, project_digest TEXT NOT NULL,
            trust_group TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)),
            registered_at INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE memberships(
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            identity_id TEXT NOT NULL REFERENCES identities(identity_id),
            role TEXT NOT NULL CHECK(role IN ('viewer','operator','maintainer')),
            active INTEGER NOT NULL CHECK(active IN (0,1)), updated_at INTEGER NOT NULL,
            PRIMARY KEY(project_id,identity_id,role)
        ) WITHOUT ROWID;
        CREATE TABLE credentials(
            credential_id TEXT PRIMARY KEY, identity_id TEXT NOT NULL REFERENCES identities(identity_id),
            secret_digest TEXT NOT NULL, issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            revoked_at INTEGER
        ) WITHOUT ROWID;
        CREATE TABLE resources(
            resource_kind TEXT NOT NULL, resource_id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(project_id), project_digest TEXT NOT NULL,
            owner_id TEXT NOT NULL REFERENCES identities(identity_id), meaning TEXT NOT NULL,
            adoption_id TEXT REFERENCES legacy_adoptions(adoption_id), authorized_digest TEXT,
            bound_at INTEGER NOT NULL, PRIMARY KEY(resource_kind,resource_id)
        ) WITHOUT ROWID;
        CREATE TABLE sanitation_receipts(
            receipt_digest TEXT PRIMARY KEY, device_id TEXT NOT NULL,
            receipt_json TEXT NOT NULL, recorded_at INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE device_assignments(
            device_id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(project_id),
            trust_group TEXT, host_id TEXT, host_generation INTEGER,
            host_incarnation TEXT, generation INTEGER NOT NULL CHECK(generation>=1),
            sanitation_digest TEXT REFERENCES sanitation_receipts(receipt_digest),
            updated_at INTEGER NOT NULL,
            CHECK((project_id IS NULL)!=(trust_group IS NULL)),
            CHECK((host_id IS NULL AND host_generation IS NULL AND host_incarnation IS NULL)
                  OR (host_id IS NOT NULL AND host_generation IS NOT NULL
                      AND host_incarnation IS NOT NULL))
        ) WITHOUT ROWID;
        CREATE TABLE enrollments(
            enrollment_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, generation INTEGER NOT NULL,
            secret_digest TEXT NOT NULL, project_ids_json TEXT NOT NULL,
            trust_groups_json TEXT NOT NULL, issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            credential_lifetime_seconds INTEGER NOT NULL, consumed_at INTEGER, revoked_at INTEGER,
            UNIQUE(host_id,generation)
        ) WITHOUT ROWID;
        CREATE TABLE hosts(
            host_id TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>=1),
            incarnation TEXT NOT NULL, credential_id TEXT NOT NULL UNIQUE,
            secret_digest TEXT NOT NULL, project_ids_json TEXT NOT NULL,
            trust_groups_json TEXT NOT NULL, issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            revoked_at INTEGER
        ) WITHOUT ROWID;
        CREATE TABLE legacy_adoptions(
            adoption_id TEXT PRIMARY KEY, original_digest TEXT NOT NULL, original_bytes INTEGER NOT NULL,
            original_format TEXT NOT NULL, meaning TEXT NOT NULL, preserved_relative TEXT NOT NULL,
            recorded_at INTEGER NOT NULL
        ) WITHOUT ROWID;
        """
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)
        connection.executemany("INSERT INTO access_metadata(key,value) VALUES(?,?)", (
            ("format_version", str(ACCESS_FORMAT_VERSION)),
            ("minimum_reader_version", str(ACCESS_READER_VERSION)),
            ("minimum_writer_version", str(ACCESS_WRITER_VERSION)),
            ("service", ACCESS_SERVICE),
        ))
        connection.execute(f"PRAGMA application_id = {ACCESS_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {ACCESS_FORMAT_VERSION}")

    @contextmanager
    def _transaction(self, *, exclusive=False):
        with self._lock:
            _require(not self._closed, "Access store is closed")
            connection = self._connection
            try:
                connection.execute("BEGIN EXCLUSIVE" if exclusive else "BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except (ContractError, AccessError):
                connection.rollback()
                raise
            except sqlite3.Error:
                connection.rollback()
                raise ContractError("Access state operation failed") from None

    def close(self):
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _now(self):
        value = self._clock()
        _require(type(value) is int and value >= 0, "Access clock is invalid")
        return value

    @staticmethod
    def _administrator(connection, actor):
        _identifier(actor, "administrator identity")
        row = connection.execute(
            "SELECT administrator,active FROM identities WHERE identity_id=?", (actor,)).fetchone()
        if row is None or row["administrator"] != 1 or row["active"] != 1:
            _deny("forbidden", "Administrator authorization is required")

    def bootstrap_administrator(self, identity_id):
        identity_id = _identifier(identity_id, "identity")
        now = self._now()
        with self._transaction() as connection:
            count = connection.execute("SELECT count(*) FROM identities").fetchone()[0]
            if count:
                _deny("bootstrap_denied", "Administrator bootstrap is already closed")
            connection.execute(
                "INSERT INTO identities(identity_id,administrator,active,created_at) VALUES(?,1,1,?)",
                (identity_id, now))
        return {"identityId": identity_id, "administrator": True, "active": True}

    def bootstrap_administrator_credential(self, identity_id, *, lifetime_seconds):
        """Atomically close bootstrap and create its only initial credential."""
        identity_id = _identifier(identity_id, "identity")
        _require(type(lifetime_seconds) is int and 60 <= lifetime_seconds <= 30 * 86400,
                 "Credential lifetime must be 60 seconds to thirty days")
        now = self._now()
        credential_id = "credential_" + secrets.token_hex(12)
        token, secret = _token("rpa", credential_id)
        secret_digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
        with self._transaction() as connection:
            count = connection.execute("SELECT count(*) FROM identities").fetchone()[0]
            if count:
                _deny("bootstrap_denied", "Administrator bootstrap is already closed")
            connection.execute(
                "INSERT INTO identities(identity_id,administrator,active,created_at) "
                "VALUES(?,1,1,?)", (identity_id, now))
            connection.execute(
                "INSERT INTO credentials(credential_id,identity_id,secret_digest,issued_at,"
                "expires_at,revoked_at) VALUES(?,?,?,?,?,NULL)",
                (credential_id, identity_id, secret_digest, now, now + lifetime_seconds))
        return ({"identityId": identity_id, "administrator": True, "active": True},
                {"credentialId": credential_id, "identityId": identity_id,
                 "issuedAt": now, "expiresAt": now + lifetime_seconds,
                 "token": token})

    def create_identity(self, actor, identity_id, *, administrator=False):
        identity_id = _identifier(identity_id, "identity")
        _require(type(administrator) is bool, "Invalid administrator flag")
        now = self._now()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            existing = connection.execute(
                "SELECT * FROM identities WHERE identity_id=?", (identity_id,)).fetchone()
            if existing is not None:
                _require(existing["administrator"] == int(administrator)
                         and existing["active"] == 1, "Identity already exists")
            else:
                connection.execute(
                    "INSERT INTO identities(identity_id,administrator,active,created_at) VALUES(?,?,1,?)",
                    (identity_id, int(administrator), now))
        return {"identityId": identity_id, "administrator": administrator, "active": True}

    def revoke_identity(self, actor, identity_id):
        identity_id = _identifier(identity_id, "identity")
        now = self._now()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            row = connection.execute(
                "SELECT identity_id FROM identities WHERE identity_id=?", (identity_id,)).fetchone()
            _require(row is not None, "Identity does not exist")
            _require(identity_id != actor, "Administrator cannot revoke the active actor")
            connection.execute("UPDATE identities SET active=0 WHERE identity_id=?", (identity_id,))
            connection.execute(
                "UPDATE credentials SET revoked_at=COALESCE(revoked_at,?) WHERE identity_id=?",
                (now, identity_id))
            connection.execute(
                "UPDATE memberships SET active=0,updated_at=? WHERE identity_id=?",
                (now, identity_id))

    def list_identities(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT identity_id,administrator,active,created_at FROM identities ORDER BY identity_id").fetchall()
        return [{"identityId": row["identity_id"],
                 "administrator": bool(row["administrator"]),
                 "active": bool(row["active"]), "createdAt": row["created_at"]}
                for row in rows]

    def require_administrator(self, actor):
        with self._lock:
            self._administrator(self._connection, actor)
        return actor

    def register_project(self, actor, project_wire):
        try:
            project = contracts.validate_project_revision(project_wire)
            project_digest = contracts.digest(project)
        except Exception:
            raise ContractError("Trusted project registration is invalid") from None
        now = self._now()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            existing = connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (project["id"],)).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO projects(project_id,revision,project_digest,trust_group,active,registered_at) VALUES(?,?,?,?,1,?)",
                    (project["id"], project["revision"], project_digest,
                     project["trustGroup"], now))
            elif (existing["revision"], existing["project_digest"], existing["trust_group"]) != \
                    (project["revision"], project_digest, project["trustGroup"]):
                connection.execute(
                    "UPDATE projects SET revision=?,project_digest=?,trust_group=?,active=1,registered_at=? WHERE project_id=?",
                    (project["revision"], project_digest, project["trustGroup"], now, project["id"]))
            else:
                connection.execute("UPDATE projects SET active=1 WHERE project_id=?", (project["id"],))
        return {"projectId": project["id"], "revision": project["revision"],
                "projectDigest": project_digest, "trustGroup": project["trustGroup"]}

    def project(self, project_id):
        project_id = _identifier(project_id, "project")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM projects WHERE project_id=? AND active=1", (project_id,)).fetchone()
        if row is None:
            _deny("project_unavailable", "Project is not locally registered", 404)
        return {"projectId": row["project_id"], "revision": row["revision"],
                "projectDigest": row["project_digest"], "trustGroup": row["trust_group"]}

    def list_projects(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM projects WHERE active=1 ORDER BY project_id").fetchall()
        return [{"projectId": row["project_id"], "revision": row["revision"],
                 "projectDigest": row["project_digest"], "trustGroup": row["trust_group"]}
                for row in rows]

    def grant_membership(self, actor, project_id, identity_id, role):
        project_id = _identifier(project_id, "project")
        identity_id = _identifier(identity_id, "identity")
        _require(role in _ROLES, "Invalid membership role")
        now = self._now()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            project = connection.execute(
                "SELECT active FROM projects WHERE project_id=?", (project_id,)).fetchone()
            identity = connection.execute(
                "SELECT active FROM identities WHERE identity_id=?", (identity_id,)).fetchone()
            _require(project is not None and project["active"] == 1, "Project does not exist")
            _require(identity is not None and identity["active"] == 1, "Identity does not exist")
            connection.execute(
                "INSERT INTO memberships(project_id,identity_id,role,active,updated_at) VALUES(?,?,?,1,?) "
                "ON CONFLICT(project_id,identity_id,role) DO UPDATE SET active=1,updated_at=excluded.updated_at",
                (project_id, identity_id, role, now))
        return {"projectId": project_id, "identityId": identity_id, "role": role, "active": True}

    def revoke_membership(self, actor, project_id, identity_id, role):
        project_id = _identifier(project_id, "project")
        identity_id = _identifier(identity_id, "identity")
        _require(role in _ROLES, "Invalid membership role")
        now = self._now()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            changed = connection.execute(
                "UPDATE memberships SET active=0,updated_at=? WHERE project_id=? AND identity_id=? AND role=? AND active=1",
                (now, project_id, identity_id, role)).rowcount
            _require(changed == 1, "Membership does not exist")

    def list_memberships(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT project_id,identity_id,role,active,updated_at FROM memberships ORDER BY project_id,identity_id,role").fetchall()
        return [{"projectId": row["project_id"], "identityId": row["identity_id"],
                 "role": row["role"], "active": bool(row["active"]),
                 "updatedAt": row["updated_at"]} for row in rows]

    def issue_principal_credential(self, actor, identity_id, *, lifetime_seconds):
        identity_id = _identifier(identity_id, "identity")
        _require(type(lifetime_seconds) is int and 60 <= lifetime_seconds <= 30 * 86400,
                 "Credential lifetime must be 60 seconds to 30 days")
        now = self._now()
        credential_id = "credential_" + secrets.token_hex(12)
        token, secret = _token("rpa", credential_id)
        secret_digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            identity = connection.execute(
                "SELECT active FROM identities WHERE identity_id=?", (identity_id,)).fetchone()
            _require(identity is not None and identity["active"] == 1, "Identity does not exist")
            connection.execute(
                "INSERT INTO credentials(credential_id,identity_id,secret_digest,issued_at,expires_at,revoked_at) VALUES(?,?,?,?,?,NULL)",
                (credential_id, identity_id, secret_digest, now, now + lifetime_seconds))
        return {"credentialId": credential_id, "identityId": identity_id,
                "expiresAt": now + lifetime_seconds, "token": token}

    def revoke_credential(self, actor, credential_id):
        credential_id = _identifier(credential_id, "credential")
        now = self._now()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            changed = connection.execute(
                "UPDATE credentials SET revoked_at=? WHERE credential_id=? AND revoked_at IS NULL",
                (now, credential_id)).rowcount
            _require(changed == 1, "Credential does not exist or is already revoked")

    def list_credentials(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT credential_id,identity_id,issued_at,expires_at,revoked_at FROM credentials ORDER BY issued_at").fetchall()
        return [{"credentialId": row["credential_id"], "identityId": row["identity_id"],
                 "issuedAt": row["issued_at"], "expiresAt": row["expires_at"],
                 "revoked": row["revoked_at"] is not None} for row in rows]

    def _principal_by_id(self, credential_id):
        now = self._now()
        with self._lock:
            row = self._connection.execute(
                "SELECT c.*,i.active FROM credentials c JOIN identities i USING(identity_id) WHERE credential_id=?",
                (credential_id,)).fetchone()
        if row is None or row["revoked_at"] is not None or row["expires_at"] <= now or row["active"] != 1:
            _deny("unauthorized", "Authentication failed", 401)
        return PrincipalContext(row["identity_id"], row["credential_id"],
                                row["expires_at"], self._issuer)

    def authenticate_principal(self, token):
        credential_id, supplied_digest = _parse_token(token, "rpa")
        with self._lock:
            row = self._connection.execute(
                "SELECT secret_digest FROM credentials WHERE credential_id=?", (credential_id,)).fetchone()
        if row is None or not hmac.compare_digest(row["secret_digest"], supplied_digest):
            _deny("unauthorized", "Authentication failed", 401)
        return self._principal_by_id(credential_id)

    def current_principal(self, principal):
        if type(principal) is not PrincipalContext or principal._issuer is not self._issuer:
            _deny("unauthorized", "Authentication failed", 401)
        current = self._principal_by_id(principal.credential_id)
        if current.principal_id != principal.principal_id:
            _deny("unauthorized", "Authentication failed", 401)
        guard = principal._authorization_guard
        if guard is not None:
            _require(callable(guard), "Invalid authorization context")
            _require(guard(principal) is True, "Invalid authorization context")
            return PrincipalContext(
                current.principal_id, current.credential_id, current.expires_at,
                self._issuer, principal.authorization_id, guard)
        if principal.authorization_id is not None:
            _deny("unauthorized", "Authentication failed", 401)
        return current

    def authorize(self, principal, project_id, capability):
        principal = self.current_principal(principal)
        _require(isinstance(capability, str), "Invalid capability")
        with self._lock:
            if project_id is None:
                identity = self._connection.execute(
                    "SELECT administrator FROM identities WHERE identity_id=? AND active=1",
                    (principal.principal_id,)).fetchone()
                allowed = identity is not None and identity["administrator"] == 1 \
                    and capability in _GLOBAL_CAPABILITIES
            else:
                project_id = _identifier(project_id, "project")
                project = self._connection.execute(
                    "SELECT active FROM projects WHERE project_id=?", (project_id,)).fetchone()
                roles = {row[0] for row in self._connection.execute(
                    "SELECT role FROM memberships WHERE project_id=? AND identity_id=? AND active=1",
                    (project_id, principal.principal_id))}
                allowed = project is not None and project["active"] == 1 and any(
                    capability in _ROLE_CAPABILITIES[role] for role in roles)
        if not allowed:
            _deny("forbidden", "Project capability is not granted")
        return principal

    def authorized_project_ids(self, principal, capability):
        """Return only durable projects for which the current credential has a capability."""
        principal = self.current_principal(principal)
        _require(isinstance(capability, str), "Invalid capability")
        with self._lock:
            rows = self._connection.execute(
                "SELECT DISTINCT p.project_id,m.role FROM projects p "
                "JOIN memberships m ON m.project_id=p.project_id "
                "WHERE p.active=1 AND m.active=1 AND m.identity_id=? "
                "ORDER BY p.project_id", (principal.principal_id,)).fetchall()
        return tuple(sorted({row["project_id"] for row in rows
                             if capability in _ROLE_CAPABILITIES[row["role"]]}))

    def bind_resource(self, kind, resource_id, project_id, project_digest,
                      owner_id, *, meaning="release"):
        _require(kind in _RESOURCE_KINDS, "Invalid resource kind")
        resource_id = _identifier(resource_id, "resource")
        project_id = _identifier(project_id, "project")
        owner_id = _identifier(owner_id, "resource owner")
        _require(isinstance(project_digest, str) and _DIGEST.fullmatch(project_digest),
                 "Invalid project digest")
        _require(meaning in _RESOURCE_MEANINGS, "Invalid resource meaning")
        now = self._now()
        with self._transaction() as connection:
            project = connection.execute(
                "SELECT * FROM projects WHERE project_id=? AND active=1", (project_id,)).fetchone()
            owner = connection.execute(
                "SELECT active FROM identities WHERE identity_id=?", (owner_id,)).fetchone()
            _require(project is not None and project["project_digest"] == project_digest,
                     "Project binding is not current")
            _require(owner is not None and owner["active"] == 1, "Resource owner is unavailable")
            existing = connection.execute(
                "SELECT * FROM resources WHERE resource_kind=? AND resource_id=?",
                (kind, resource_id)).fetchone()
            values = (project_id, project_digest, owner_id, meaning, None, None)
            if existing is not None:
                current = (existing["project_id"], existing["project_digest"],
                           existing["owner_id"], existing["meaning"],
                           existing["adoption_id"], existing["authorized_digest"])
                preserved_promotion = (
                    current[:3] == values[:3]
                    and meaning == "legacy-inert"
                    and existing["meaning"] == "legacy-authorized"
                    and existing["adoption_id"] is not None
                    and existing["authorized_digest"] is not None)
                _require(current == values or preserved_promotion,
                         "Resource binding conflict")
            else:
                connection.execute(
                    "INSERT INTO resources(resource_kind,resource_id,project_id,project_digest,owner_id,meaning,adoption_id,authorized_digest,bound_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (kind, resource_id, *values, now))
        return self.resource(kind, resource_id)

    def resource(self, kind, resource_id):
        _require(kind in _RESOURCE_KINDS, "Invalid resource kind")
        resource_id = _identifier(resource_id, "resource")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM resources WHERE resource_kind=? AND resource_id=?",
                (kind, resource_id)).fetchone()
        if row is None:
            _deny("not_found", "Resource is not locally bound", 404)
        # A resource keeps the exact project digest that admitted it.  A later
        # project revision must not rewrite or make that immutable provenance
        # disappear.  AccessController still requires a process-local trusted
        # registration for this historical digest before HTTP serialization.
        self.project(row["project_id"])
        return ResourceBinding(row["resource_kind"], row["resource_id"],
                               row["project_id"], row["project_digest"],
                               row["owner_id"], row["meaning"],
                               row["adoption_id"], row["authorized_digest"])

    def authorize_resource(self, principal, kind, resource_id, capability, *, executable=False):
        principal = self.current_principal(principal)
        binding = self.resource(kind, resource_id)
        try:
            self.authorize(principal, binding.project_id, capability)
        except AccessError as error:
            if error.status == 401:
                raise
            try:
                self.authorize(principal, binding.project_id, "project.read")
            except AccessError:
                _deny("not_found", "Resource is not locally bound", 404)
            raise
        if executable and binding.meaning == "legacy-inert":
            _deny("inert_resource", "Imported legacy resource is not locally authorized")
        if executable and binding.project_digest != self.project(binding.project_id)["projectDigest"]:
            _deny("stale_project", "Project revision changed; close the historical session", 409)
        return binding

    def list_resource_bindings(self, kind=None):
        _require(kind is None or kind in _RESOURCE_KINDS, "Invalid resource kind")
        with self._lock:
            if kind is None:
                rows = self._connection.execute(
                    "SELECT * FROM resources ORDER BY bound_at,resource_id").fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM resources WHERE resource_kind=? ORDER BY bound_at,resource_id",
                    (kind,)).fetchall()
        return [ResourceBinding(row["resource_kind"], row["resource_id"],
                                row["project_id"], row["project_digest"],
                                row["owner_id"], row["meaning"],
                                row["adoption_id"], row["authorized_digest"])
                for row in rows]

    def authorize_legacy_resource(self, actor, kind, resource_id, *,
                                  adoption_id, resource_digest):
        """Promote one inert recording after a separate local admin decision.

        The adopted source bytes and the exact imported recording digest remain
        separate audit facts.  No result status, grant, or unfinished work is
        copied from the legacy bytes.
        """
        _require(kind == "recording", "Only legacy recordings can be authorized")
        resource_id = _identifier(resource_id, "resource")
        adoption_id = _identifier(adoption_id, "adoption")
        _require(isinstance(resource_digest, str) and _DIGEST.fullmatch(resource_digest),
                 "Invalid resource digest")
        now = self._now()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            adoption = connection.execute(
                "SELECT meaning FROM legacy_adoptions WHERE adoption_id=?",
                (adoption_id,)).fetchone()
            _require(adoption is not None
                     and adoption["meaning"] == "legacy-recording-only",
                     "A recording-only legacy adoption is required")
            resource = connection.execute(
                "SELECT * FROM resources WHERE resource_kind=? AND resource_id=?",
                (kind, resource_id)).fetchone()
            _require(resource is not None and resource["meaning"] == "legacy-inert",
                     "An inert legacy resource is required")
            connection.execute(
                "UPDATE resources SET meaning='legacy-authorized',adoption_id=?,"
                "authorized_digest=?,bound_at=? WHERE resource_kind=? AND resource_id=?",
                (adoption_id, resource_digest, now, kind, resource_id))
        return self.resource(kind, resource_id)

    @staticmethod
    def _sanitation(value, device_id, previous):
        _require(type(value) is dict and set(value) == {
            "schemaVersion", "kind", "deviceId", "previousAssignment",
            "completedAtMs", "limitations"}, "Invalid sanitation receipt")
        _require(value["schemaVersion"] == 1 and type(value["schemaVersion"]) is int
                 and value["kind"] == "device-sanitation-receipt"
                 and value["deviceId"] == device_id
                 and value["previousAssignment"] == previous
                 and type(value["completedAtMs"]) is int and value["completedAtMs"] >= 0
                 and isinstance(value["limitations"], list) and len(value["limitations"]) <= 32
                 and all(isinstance(item, str) and len(item.encode()) <= 256
                         for item in value["limitations"]), "Invalid sanitation receipt")
        return copy.deepcopy(value)

    def assign_device(self, actor, device_id, *, project_id=None, trust_group=None,
                      host_id=None, sanitation_receipt=None):
        device_id = _identifier(device_id, "device")
        _require((project_id is None) != (trust_group is None),
                 "Assign a device to exactly one project or trust group")
        if project_id is not None:
            project_id = _identifier(project_id, "project")
        if trust_group is not None:
            trust_group = _identifier(trust_group, "trust group")
        if host_id is not None:
            host_id = _identifier(host_id, "host")
        now = self._now()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            project = None
            host = None
            if project_id is not None:
                project = connection.execute(
                    "SELECT project_id,trust_group FROM projects "
                    "WHERE project_id=? AND active=1", (project_id,)).fetchone()
                _require(project is not None, "Project does not exist")
            else:
                _require(connection.execute(
                    "SELECT 1 FROM projects WHERE trust_group=? AND active=1 LIMIT 1",
                    (trust_group,)).fetchone(), "Trust group does not exist")
            if host_id is not None:
                host = connection.execute(
                    "SELECT * FROM hosts WHERE host_id=? AND revoked_at IS NULL AND expires_at>?",
                    (host_id, now)).fetchone()
                _require(host is not None, "Host is unavailable")
                host_projects = set(_load_json_bytes(host["project_ids_json"]))
                host_groups = set(_load_json_bytes(host["trust_groups_json"]))
                if project is not None:
                    _require(project_id in host_projects
                             or project["trust_group"] in host_groups,
                             "Host scope does not include the assigned project")
                else:
                    _require(trust_group in host_groups,
                             "Host scope does not include the assigned trust group")
            host_generation = host["generation"] if host is not None else None
            host_incarnation = host["incarnation"] if host is not None else None
            existing = connection.execute(
                "SELECT * FROM device_assignments WHERE device_id=?", (device_id,)).fetchone()
            target = (project_id, trust_group, host_id,
                      host_generation, host_incarnation)
            sanitation_digest = None
            if existing is not None:
                current = (existing["project_id"], existing["trust_group"],
                           existing["host_id"], existing["host_generation"],
                           existing["host_incarnation"])
                if current == target:
                    return self._assignment_row(existing)
                previous = {"projectId": existing["project_id"],
                            "trustGroup": existing["trust_group"],
                            "hostId": existing["host_id"],
                            "hostGeneration": existing["host_generation"],
                            "hostIncarnation": existing["host_incarnation"]}
                receipt = self._sanitation(sanitation_receipt, device_id, previous)
                sanitation_digest = contracts.digest(receipt)
                connection.execute(
                    "INSERT OR IGNORE INTO sanitation_receipts(receipt_digest,device_id,receipt_json,recorded_at) VALUES(?,?,?,?)",
                    (sanitation_digest, device_id, _json(receipt), now))
                generation = existing["generation"] + 1
                connection.execute(
                    "UPDATE device_assignments SET project_id=?,trust_group=?,host_id=?,"
                    "host_generation=?,host_incarnation=?,generation=?,"
                    "sanitation_digest=?,updated_at=? WHERE device_id=?",
                    (project_id, trust_group, host_id, host_generation,
                     host_incarnation, generation,
                     sanitation_digest, now, device_id))
            else:
                generation = 1
                _require(sanitation_receipt is None, "Initial assignment has no prior sanitation receipt")
                connection.execute(
                    "INSERT INTO device_assignments(device_id,project_id,trust_group,host_id,"
                    "host_generation,host_incarnation,generation,sanitation_digest,updated_at) "
                    "VALUES(?,?,?,?,?,?,1,NULL,?)",
                    (device_id, project_id, trust_group, host_id,
                     host_generation, host_incarnation, now))
            row = connection.execute(
                "SELECT * FROM device_assignments WHERE device_id=?", (device_id,)).fetchone()
            return self._assignment_row(row)

    @staticmethod
    def _assignment_row(row):
        return {"deviceId": row["device_id"], "projectId": row["project_id"],
                "trustGroup": row["trust_group"], "hostId": row["host_id"],
                "hostGeneration": row["host_generation"],
                "hostIncarnation": row["host_incarnation"],
                "generation": row["generation"],
                "sanitationDigest": row["sanitation_digest"]}

    def device_assignment(self, device_id):
        device_id = _identifier(device_id, "device")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM device_assignments WHERE device_id=?", (device_id,)).fetchone()
        if row is None:
            _deny("device_unassigned", "Device is not assigned", 404)
        return self._assignment_row(row)

    def list_device_assignments(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM device_assignments ORDER BY device_id").fetchall()
        return [self._assignment_row(row) for row in rows]

    def assignment_project_ids(self, device_id):
        """Resolve current durable projects for an assignment without granting access."""
        assignment = self.device_assignment(device_id)
        projects = self.list_projects()
        candidates = tuple(sorted(
            project["projectId"] for project in projects
            if project["projectId"] == assignment["projectId"]
            or (assignment["projectId"] is None
                and project["trustGroup"] == assignment["trustGroup"])))
        if not candidates:
            _deny("device_unassigned", "Device assignment has no active project", 409)
        if assignment["hostId"] is None:
            return candidates
        now = self._now()
        with self._lock:
            host = self._connection.execute(
                "SELECT * FROM hosts WHERE host_id=?", (assignment["hostId"],)).fetchone()
        if host is None or host["revoked_at"] is not None or host["expires_at"] <= now:
            _deny("host_revoked", "Assigned host is unavailable")
        if host["generation"] != assignment["hostGeneration"] \
                or host["incarnation"] != assignment["hostIncarnation"]:
            _deny("stale_host", "Device assignment belongs to an earlier host incarnation")
        allowed_projects = set(_load_json_bytes(host["project_ids_json"]))
        allowed_groups = set(_load_json_bytes(host["trust_groups_json"]))
        eligible = tuple(project["projectId"] for project in projects
                         if project["projectId"] in candidates
                         and (project["projectId"] in allowed_projects
                              or project["trustGroup"] in allowed_groups))
        if not eligible:
            _deny("forbidden", "Host scope does not include the device assignment")
        return eligible

    def authorize_device(self, principal, device_id, project_id, capability):
        self.authorize(principal, project_id, capability)
        project = self.project(project_id)
        assignment = self.device_assignment(device_id)
        if not (assignment["projectId"] == project_id
                or assignment["trustGroup"] == project["trustGroup"]):
            _deny("forbidden", "Device assignment is outside the project scope")
        if assignment["hostId"] is not None:
            with self._lock:
                row = self._connection.execute(
                    "SELECT * FROM hosts WHERE host_id=?", (assignment["hostId"],)).fetchone()
            now = self._now()
            if row is None or row["revoked_at"] is not None or row["expires_at"] <= now:
                _deny("host_revoked", "Assigned host is unavailable")
            if row["generation"] != assignment["hostGeneration"] \
                    or row["incarnation"] != assignment["hostIncarnation"]:
                _deny("stale_host", "Device assignment belongs to an earlier host incarnation")
            projects = tuple(_load_json_bytes(row["project_ids_json"]))
            groups = tuple(_load_json_bytes(row["trust_groups_json"]))
            if project_id not in projects and project["trustGroup"] not in groups:
                _deny("forbidden", "Host scope does not include this project")
        return assignment

    def create_host_enrollment(self, actor, *, host_id, project_ids, trust_groups,
                               lifetime_seconds, credential_lifetime_seconds):
        host_id = _identifier(host_id, "host")
        _require(isinstance(project_ids, (list, tuple)) and 1 <= len(project_ids) <= 64
                 and len(set(project_ids)) == len(project_ids), "Invalid enrollment project scope")
        _require(isinstance(trust_groups, (list, tuple)) and len(trust_groups) <= 64
                 and len(set(trust_groups)) == len(trust_groups), "Invalid enrollment trust scope")
        projects = tuple(_identifier(item, "project") for item in project_ids)
        groups = tuple(_identifier(item, "trust group") for item in trust_groups)
        _require(type(lifetime_seconds) is int and 60 <= lifetime_seconds <= 86400,
                 "Enrollment lifetime must be 60 seconds to one day")
        _require(type(credential_lifetime_seconds) is int
                 and 60 <= credential_lifetime_seconds <= 7 * 86400,
                 "Host credential lifetime must be 60 seconds to seven days")
        now = self._now()
        enrollment_id = "enrollment_" + secrets.token_hex(12)
        token, secret = _token("rpe", enrollment_id)
        secret_digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            rows = connection.execute(
                f"SELECT project_id,trust_group FROM projects WHERE active=1 AND project_id IN ({','.join('?' for _ in projects)})",
                projects).fetchall()
            _require({row["project_id"] for row in rows} == set(projects),
                     "Enrollment project scope is unavailable")
            available_groups = {row["trust_group"] for row in rows}
            _require(set(groups) <= available_groups, "Enrollment trust scope is unavailable")
            host = connection.execute(
                "SELECT generation,expires_at,revoked_at FROM hosts WHERE host_id=?", (host_id,)).fetchone()
            if host is not None and host["revoked_at"] is None and host["expires_at"] > now:
                _require(False, "Active host must be revoked before reenrollment")
            outstanding = connection.execute(
                "SELECT 1 FROM enrollments WHERE host_id=? AND consumed_at IS NULL "
                "AND revoked_at IS NULL AND expires_at>? LIMIT 1", (host_id, now)).fetchone()
            _require(outstanding is None,
                     "Active enrollment must be revoked before reenrollment")
            last_enrollment = connection.execute(
                "SELECT max(generation) FROM enrollments WHERE host_id=?", (host_id,)).fetchone()[0]
            generation = max(host["generation"] if host is not None else 0,
                             last_enrollment or 0) + 1
            connection.execute(
                "INSERT INTO enrollments(enrollment_id,host_id,generation,secret_digest,project_ids_json,trust_groups_json,issued_at,expires_at,credential_lifetime_seconds,consumed_at,revoked_at) VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)",
                (enrollment_id, host_id, generation, secret_digest,
                 _json(list(projects)), _json(list(groups)), now,
                 now + lifetime_seconds, credential_lifetime_seconds))
        return {"enrollmentId": enrollment_id, "hostId": host_id,
                "generation": generation, "expiresAt": now + lifetime_seconds,
                "token": token}

    def consume_host_enrollment(self, token, *, host_id, incarnation):
        enrollment_id, supplied_digest = _parse_token(token, "rpe")
        host_id = _identifier(host_id, "host")
        incarnation = _identifier(incarnation, "host incarnation")
        now = self._now()
        credential_id = "hostcred_" + secrets.token_hex(12)
        credential, secret = _token("rph", credential_id)
        secret_digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM enrollments WHERE enrollment_id=?", (enrollment_id,)).fetchone()
            if row is None or not hmac.compare_digest(row["secret_digest"], supplied_digest):
                _deny("unauthorized", "Enrollment authentication failed", 401)
            if row["host_id"] != host_id:
                _deny("enrollment_scope", "Enrollment is scoped to another host")
            if row["revoked_at"] is not None:
                _deny("enrollment_revoked", "Enrollment was revoked")
            if row["consumed_at"] is not None:
                _deny("enrollment_replayed", "Enrollment was already consumed")
            if row["expires_at"] <= now:
                _deny("enrollment_expired", "Enrollment has expired")
            current = connection.execute(
                "SELECT * FROM hosts WHERE host_id=?", (host_id,)).fetchone()
            if current is not None and current["generation"] >= row["generation"] \
                    and current["revoked_at"] is None:
                _deny("stale_host", "Enrollment generation is stale")
            expires_at = now + row["credential_lifetime_seconds"]
            connection.execute(
                "UPDATE enrollments SET consumed_at=? WHERE enrollment_id=? AND consumed_at IS NULL",
                (now, enrollment_id))
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                _deny("enrollment_replayed", "Enrollment was already consumed")
            connection.execute(
                "INSERT INTO hosts(host_id,generation,incarnation,credential_id,secret_digest,project_ids_json,trust_groups_json,issued_at,expires_at,revoked_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(host_id) DO UPDATE SET generation=excluded.generation,incarnation=excluded.incarnation,credential_id=excluded.credential_id,secret_digest=excluded.secret_digest,project_ids_json=excluded.project_ids_json,trust_groups_json=excluded.trust_groups_json,issued_at=excluded.issued_at,expires_at=excluded.expires_at,revoked_at=NULL",
                (host_id, row["generation"], incarnation, credential_id,
                 secret_digest, row["project_ids_json"], row["trust_groups_json"],
                 now, expires_at))
        return {"hostId": host_id, "generation": row["generation"],
                "incarnation": incarnation, "credentialId": credential_id,
                "expiresAt": expires_at, "credential": credential}

    def revoke_host_enrollment(self, actor, enrollment_id):
        enrollment_id = _identifier(enrollment_id, "enrollment")
        now = self._now()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            changed = connection.execute(
                "UPDATE enrollments SET revoked_at=? WHERE enrollment_id=? "
                "AND consumed_at IS NULL AND revoked_at IS NULL",
                (now, enrollment_id)).rowcount
            _require(changed == 1,
                     "Enrollment does not exist, was consumed, or is already revoked")

    def list_host_enrollments(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT enrollment_id,host_id,generation,project_ids_json,"
                "trust_groups_json,issued_at,expires_at,consumed_at,revoked_at "
                "FROM enrollments ORDER BY issued_at,enrollment_id").fetchall()
        return [{"enrollmentId": row["enrollment_id"], "hostId": row["host_id"],
                 "generation": row["generation"],
                 "projectIds": _load_json_bytes(row["project_ids_json"]),
                 "trustGroups": _load_json_bytes(row["trust_groups_json"]),
                 "issuedAt": row["issued_at"], "expiresAt": row["expires_at"],
                 "consumed": row["consumed_at"] is not None,
                 "revoked": row["revoked_at"] is not None} for row in rows]

    def authenticate_host(self, credential, *, expected_incarnation=None,
                          expected_generation=None):
        credential_id, supplied_digest = _parse_token(credential, "rph")
        now = self._now()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM hosts WHERE credential_id=?", (credential_id,)).fetchone()
        if row is None or not hmac.compare_digest(row["secret_digest"], supplied_digest) \
                or row["revoked_at"] is not None or row["expires_at"] <= now:
            _deny("unauthorized", "Host authentication failed", 401)
        if expected_incarnation is not None and row["incarnation"] != expected_incarnation:
            _deny("stale_host", "Host incarnation is stale")
        if expected_generation is not None and row["generation"] != expected_generation:
            _deny("stale_host", "Host generation is stale")
        return HostContext(
            row["host_id"], row["generation"], row["incarnation"],
            row["credential_id"], row["expires_at"],
            tuple(_load_json_bytes(row["project_ids_json"])),
            tuple(_load_json_bytes(row["trust_groups_json"])), self._issuer)

    def authorize_host(self, host, *, project_id, trust_group):
        if type(host) is not HostContext or host._issuer is not self._issuer:
            _deny("unauthorized", "Host authentication failed", 401)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM hosts WHERE host_id=? AND credential_id=?",
                (host.host_id, host.credential_id)).fetchone()
        now = self._now()
        if row is None or row["generation"] != host.generation \
                or row["incarnation"] != host.incarnation \
                or row["revoked_at"] is not None or row["expires_at"] <= now:
            _deny("host_revoked", "Host authorization is no longer current")
        project = self.project(project_id)
        if project["trustGroup"] != trust_group:
            _deny("forbidden", "Host project trust scope is invalid")
        if project_id not in host.project_ids and trust_group not in host.trust_groups:
            _deny("forbidden", "Host scope does not include this project")
        return host

    def revoke_host(self, actor, host_id):
        host_id = _identifier(host_id, "host")
        now = self._now()
        with self._transaction() as connection:
            self._administrator(connection, actor)
            changed = connection.execute(
                "UPDATE hosts SET revoked_at=? WHERE host_id=? AND revoked_at IS NULL",
                (now, host_id)).rowcount
            _require(changed == 1, "Host does not exist or is already revoked")
            connection.execute(
                "UPDATE enrollments SET revoked_at=? WHERE host_id=? "
                "AND consumed_at IS NULL AND revoked_at IS NULL",
                (now, host_id))

    def list_hosts(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT host_id,generation,incarnation,credential_id,project_ids_json,trust_groups_json,issued_at,expires_at,revoked_at FROM hosts ORDER BY host_id").fetchall()
        return [{"hostId": row["host_id"], "generation": row["generation"],
                 "incarnation": row["incarnation"], "credentialId": row["credential_id"],
                 "projectIds": _load_json_bytes(row["project_ids_json"]),
                 "trustGroups": _load_json_bytes(row["trust_groups_json"]),
                 "issuedAt": row["issued_at"], "expiresAt": row["expires_at"],
                 "revoked": row["revoked_at"] is not None} for row in rows]

    def adopt_legacy(self, adoption_id, source, *, original_format, meaning):
        adoption_id = _identifier(adoption_id, "adoption")
        _require(isinstance(original_format, str) and 1 <= len(original_format) <= 128
                 and re.fullmatch(r"[A-Za-z0-9._-]+", original_format),
                 "Invalid legacy format")
        _require(meaning in _LEGACY_MEANINGS, "Invalid legacy meaning")
        try:
            path = Path(source).absolute()
            info = path.lstat()
            _require(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                     and 0 <= info.st_size <= MAX_LEGACY_BYTES,
                     "Legacy source must be a bounded regular file")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                data = handle.read(MAX_LEGACY_BYTES + 1)
                after = os.fstat(handle.fileno())
            _require(len(data) <= MAX_LEGACY_BYTES and after.st_ino == info.st_ino
                     and after.st_size == info.st_size,
                     "Legacy source changed during adoption")
        except ContractError:
            raise
        except (OSError, TypeError, ValueError):
            raise ContractError("Legacy source could not be adopted") from None
        original_digest = hashlib.sha256(data).hexdigest()
        preserved = self.root / "legacy-adoptions"
        preserved.mkdir(mode=0o700, exist_ok=True)
        destination = preserved / f"{original_digest}.bin"
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data);handle.flush();os.fsync(handle.fileno())
        except FileExistsError:
            if destination.read_bytes() != data:
                raise ContractError("Legacy preservation conflict") from None
        except OSError:
            raise ContractError("Legacy bytes could not be preserved") from None
        now = self._now()
        relative = destination.relative_to(self.root).as_posix()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM legacy_adoptions WHERE adoption_id=?", (adoption_id,)).fetchone()
            values = (original_digest, len(data), original_format, meaning, relative)
            if existing is not None:
                _require((existing["original_digest"], existing["original_bytes"],
                          existing["original_format"], existing["meaning"],
                          existing["preserved_relative"]) == values,
                         "Legacy adoption identity conflict")
            else:
                connection.execute(
                    "INSERT INTO legacy_adoptions(adoption_id,original_digest,original_bytes,original_format,meaning,preserved_relative,recorded_at) VALUES(?,?,?,?,?,?,?)",
                    (adoption_id, *values, now))
            row = connection.execute(
                "SELECT * FROM legacy_adoptions WHERE adoption_id=?", (adoption_id,)).fetchone()
        return {"adoptionId": row["adoption_id"], "originalDigest": row["original_digest"],
                "originalBytes": row["original_bytes"], "originalFormat": row["original_format"],
                "meaning": row["meaning"], "preservedPath": str(self.root / row["preserved_relative"]),
                "recordedAt": row["recorded_at"]}

    def list_legacy_adoptions(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM legacy_adoptions ORDER BY recorded_at,adoption_id").fetchall()
        return [{"adoptionId": row["adoption_id"], "originalDigest": row["original_digest"],
                 "originalBytes": row["original_bytes"], "originalFormat": row["original_format"],
                 "meaning": row["meaning"], "preservedPath": str(self.root / row["preserved_relative"]),
                 "recordedAt": row["recorded_at"]} for row in rows]


class AccessController:
    """Process-local bridge from durable membership to trusted registrations."""

    def __init__(self, store: AccessStore, *, browser_session_seconds=3600):
        _require(type(store) is AccessStore, "Access store is required")
        _require(type(browser_session_seconds) is int
                 and 60 <= browser_session_seconds <= 12 * 3600,
                 "Invalid browser session lifetime")
        self.store = store
        self.browser_session_seconds = browser_session_seconds
        self._registrations = {}
        self._sessions = {}
        self._lock = threading.RLock()

    def bind_project(self, registration):
        from .recording_session import TrustedProjectRegistration
        _require(type(registration) is TrustedProjectRegistration,
                 "Trusted project registration is required")
        project = registration.project
        durable = self.store.project(project["id"])
        current = (durable["revision"] == project["revision"]
                   and durable["projectDigest"] == registration.project_digest
                   and durable["trustGroup"] == project["trustGroup"])
        if not current:
            historical = any(
                binding.project_id == project["id"]
                and binding.project_digest == registration.project_digest
                for binding in self.store.list_resource_bindings())
            _require(historical,
                     "Trusted project registration does not match administration")
        with self._lock:
            key = (project["id"], registration.project_digest)
            existing = self._registrations.get(key)
            _require(existing is None or existing is registration,
                     "Project revision already has another process registration")
            self._registrations[key] = registration
        return durable

    def registration(self, project_id, *, project_digest=None):
        durable = self.store.project(project_id)
        selected_digest = durable["projectDigest"] if project_digest is None else project_digest
        _require(isinstance(selected_digest, str) and _DIGEST.fullmatch(selected_digest),
                 "Invalid project digest")
        with self._lock:
            registration = self._registrations.get((project_id, selected_digest))
        if registration is None:
            _deny("project_unavailable", "Project has no trusted process registration", 409)
        return registration

    def create_browser_session(self, token):
        principal = self.store.authenticate_principal(token)
        now = self.store._now()
        session_id = "browser_" + secrets.token_hex(12)
        raw, secret = _token("rps", session_id)
        csrf = secrets.token_urlsafe(32)
        expires_at = min(principal.expires_at, now + self.browser_session_seconds)
        with self._lock:
            self._sessions = {
                key: value for key, value in self._sessions.items()
                if value["expiresAt"] > now}
            _require(len(self._sessions) < MAX_BROWSER_SESSIONS,
                     "Browser session quota is exhausted")
            self._sessions[session_id] = {
                "secretDigest": hashlib.sha256(secret.encode("ascii")).hexdigest(),
                "csrfDigest": hashlib.sha256(csrf.encode("ascii")).hexdigest(),
                "principalId": principal.principal_id,
                "credentialId": principal.credential_id,
                "expiresAt": expires_at,
            }
        return {"cookie": raw, "csrfToken": csrf, "expiresAt": expires_at,
                "principalId": principal.principal_id}

    def authenticate_browser(self, cookie, *, csrf=None, require_csrf=False):
        session_id, supplied_digest = _parse_token(cookie, "rps")
        now = self.store._now()
        with self._lock:
            session = copy.deepcopy(self._sessions.get(session_id))
        if session is None or session["expiresAt"] <= now \
                or not hmac.compare_digest(session["secretDigest"], supplied_digest):
            if session is not None and session["expiresAt"] <= now:
                with self._lock:
                    self._sessions.pop(session_id, None)
            _deny("unauthorized", "Browser session is invalid", 401)
        if require_csrf:
            if not isinstance(csrf, str):
                _deny("csrf", "CSRF token is required")
            supplied_csrf = hashlib.sha256(csrf.encode()).hexdigest()
            if not hmac.compare_digest(session["csrfDigest"], supplied_csrf):
                _deny("csrf", "CSRF token is invalid")
        principal = self.store._principal_by_id(session["credentialId"])
        if principal.principal_id != session["principalId"]:
            _deny("unauthorized", "Browser session is invalid", 401)
        return PrincipalContext(
            principal.principal_id, principal.credential_id, principal.expires_at,
            self.store._issuer, session_id, self._guard_browser_authorization)

    def _guard_browser_authorization(self, principal):
        session_id = principal.authorization_id
        now = self.store._now()
        with self._lock:
            session = self._sessions.get(session_id)
            valid = (session is not None and session["expiresAt"] > now
                     and session["principalId"] == principal.principal_id
                     and session["credentialId"] == principal.credential_id)
        if not valid:
            _deny("unauthorized", "Browser session is invalid", 401)
        return True

    def operation_principal(self, credential_id, authorization_id=None):
        principal = self.store._principal_by_id(credential_id)
        if authorization_id is None:
            return principal
        guarded = PrincipalContext(
            principal.principal_id, principal.credential_id, principal.expires_at,
            self.store._issuer, authorization_id, self._guard_browser_authorization)
        self.store.current_principal(guarded)
        return guarded

    def close_browser_session(self, cookie):
        session_id, _ = _parse_token(cookie, "rps")
        with self._lock:
            self._sessions.pop(session_id, None)

    def bind_resource(self, kind, resource_id, project_id, owner_id, *, meaning="release"):
        registration = self.registration(project_id)
        return self.store.bind_resource(kind, resource_id, project_id,
                                        registration.project_digest, owner_id,
                                        meaning=meaning)

    def authorize_resource(self, principal, kind, resource_id, capability, *, executable=False):
        binding = self.store.authorize_resource(
            principal, kind, resource_id, capability, executable=executable)
        self.registration(binding.project_id, project_digest=binding.project_digest)
        return binding

    def authorize_device(self, principal, device_id, project_id, capability):
        self.registration(project_id)
        return self.store.authorize_device(principal, device_id, project_id, capability)

    def effect_authorizer(self, principal, *, project_id, device_id, project_digest=None):
        principal_id = principal.principal_id
        credential_id = principal.credential_id
        authorization_id = principal.authorization_id
        admitted_digest = self.registration(
            project_id, project_digest=project_digest).project_digest

        def authorize(kind="device"):
            current = self.operation_principal(credential_id, authorization_id)
            if current.principal_id != principal_id:
                _deny("unauthorized", "Operation credential is no longer current", 401)
            if self.store.project(project_id)["projectDigest"] != admitted_digest:
                _deny("stale_project", "Project revision changed; close the historical session", 409)
            capability = "evidence.collect" if kind in {
                "observe", "app_logs", "sdk_capture", "sdk_diagnostics", "locator"
            } else "device.operate"
            self.authorize_device(current, device_id, project_id, capability)
            return True

        return authorize

    def visible_bindings(self, principal, kind, capability):
        result = []
        for binding in self.store.list_resource_bindings(kind):
            try:
                self.authorize_resource(principal, kind, binding.resource_id, capability)
            except AccessError:
                continue
            result.append(binding)
        return result

    def visible_device_ids(self, principal, capability="device.read"):
        result = []
        for assignment in self.store.list_device_assignments():
            candidates = ([assignment["projectId"]] if assignment["projectId"]
                          else [project["projectId"] for project in self.store.list_projects()
                                if project["trustGroup"] == assignment["trustGroup"]])
            for project_id in candidates:
                try:
                    self.authorize_device(principal, assignment["deviceId"], project_id, capability)
                except AccessError:
                    continue
                result.append(assignment["deviceId"])
                break
        return tuple(sorted(set(result)))

    def restore_lab_bindings(self, lab):
        """Restore inert owner indexes only after process-local project binding."""
        for binding in self.store.list_resource_bindings("recording"):
            if binding.meaning != "release":
                continue
            try:
                registration = self.registration(
                    binding.project_id, project_digest=binding.project_digest)
                lab.bind_release_recording_owner(
                    binding.resource_id, binding.owner_id, registration)
            except Exception:
                # A missing/incomplete object stays unavailable; no authority
                # or unfinished work is restored from the access database.
                continue


__all__ = [
    "ACCESS_FORMAT_VERSION", "ACCESS_READER_VERSION", "ACCESS_WRITER_VERSION",
    "AccessController", "AccessError", "AccessStore", "HostContext",
    "PrincipalContext", "ResourceBinding",
]
