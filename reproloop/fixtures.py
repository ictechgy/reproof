"""Trusted, durable G4 fixture allocation and loopback transport.

Serialized project/specification data can name a recipe, but only a
process-local :class:`TrustedFixturePlan` can execute it.  The journal never
stores fixture payloads: it stores their canonical digest and bounded outcome
metadata only.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid

from . import contracts
from .live.recording_session import TrustedProjectRegistration


MAX_PAYLOAD_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
MAX_OPERATIONS = 100_000
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_TERMINAL = frozenset({"complete", "failed"})


class FixtureError(RuntimeError):
    """A fixture mutation could not be proved safe."""

    def __init__(self, code: str, message: str = "Fixture operation failed"):
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str = "Fixture operation failed"):
    raise FixtureError(code, message)


def _require(condition, code="invalid_fixture", message="Fixture operation is invalid"):
    if not condition:
        _fail(code, message)


def _identifier(value, name="identifier"):
    _require(type(value) is str and _ID.fullmatch(value) is not None,
             "invalid_fixture", f"Invalid fixture {name}")
    return value


def _canonical(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail("invalid_fixture", "Fixture payload is invalid")
    _require(len(encoded) <= MAX_PAYLOAD_BYTES, "invalid_fixture",
             "Fixture payload exceeds its bound")
    return encoded


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    remote_fencing: bool
    terminal_status: bool
    idempotency_retention_ms: int

    def __post_init__(self):
        _require(type(self.remote_fencing) is bool
                 and type(self.terminal_status) is bool
                 and type(self.idempotency_retention_ms) is int
                 and 1 <= self.idempotency_retention_ms <= 30 * 86_400_000,
                 "invalid_adapter", "Fixture adapter capabilities are invalid")

    def wire(self):
        return {
            "remoteFencing": self.remote_fencing,
            "terminalStatus": self.terminal_status,
            "idempotencyRetentionMs": self.idempotency_retention_ms,
        }


@dataclass(frozen=True, slots=True)
class FixtureOperationRequest:
    operation_id: str
    allocation_id: str
    generation: int
    idempotency_key: str
    operation: str
    recipe_id: str
    payload_digest: str
    payload: object = field(repr=False, compare=False)

    def wire(self, *, include_payload=True):
        value = {
            "operationId": self.operation_id,
            "allocationId": self.allocation_id,
            "generation": self.generation,
            "idempotencyKey": self.idempotency_key,
            "operation": self.operation,
            "recipeId": self.recipe_id,
            "payloadDigest": self.payload_digest,
        }
        if include_payload:
            value["payload"] = copy.deepcopy(self.payload)
        return value


@dataclass(frozen=True, slots=True)
class FixtureOperationResult:
    operation_id: str
    generation: int
    status: str
    completed_at_ms: int | None
    retention_expires_at_ms: int | None
    fence: int


class LoopbackFixtureAdapter:
    """A fixed-path JSON adapter restricted to an owned loopback service."""

    def __init__(self, endpoint_id: str, base_url: str, *,
                 capabilities: AdapterCapabilities):
        self.endpoint_id = _identifier(endpoint_id, "endpoint identity")
        _require(type(capabilities) is AdapterCapabilities, "invalid_adapter",
                 "Fixture adapter capabilities are invalid")
        parsed = urlsplit(base_url)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except (ValueError, TypeError):
            _fail("invalid_adapter", "Fixture endpoint must be loopback")
        _require(parsed.scheme == "http" and address.is_loopback and port is not None
                 and parsed.username is None and parsed.password is None
                 and parsed.path in ("", "/") and not parsed.query
                 and not parsed.fragment, "invalid_adapter",
                 "Fixture endpoint must be loopback")
        self._host = str(address)
        self._port = port
        self.capabilities = capabilities

    def _request(self, path: str, document, timeout_seconds: float):
        _require(type(timeout_seconds) in (int, float)
                 and not isinstance(timeout_seconds, bool)
                 and 0 < timeout_seconds <= 60, "invalid_timeout",
                 "Fixture timeout is invalid")
        body = _canonical(document)
        connection = http.client.HTTPConnection(self._host, self._port,
                                                 timeout=float(timeout_seconds))
        deadline = time.monotonic() + float(timeout_seconds)
        expired = threading.Event()
        transport = {}
        def expire():
            expired.set()
            sock = transport.get("socket")
            if sock is not None:
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass
        timer = threading.Timer(float(timeout_seconds), expire)
        timer.daemon = True
        timer.start()
        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                _require(key not in value, "fixture_protocol", "Fixture response has duplicate fields")
                value[key] = item
            return value
        try:
            connection.connect()
            transport["socket"] = connection.sock
            _require(not expired.is_set() and time.monotonic() < deadline,
                     "fixture_timeout", "Fixture service timed out")
            connection.request("POST", path, body=body,
                               headers={"Content-Type": "application/json",
                                        "Content-Length": str(len(body))})
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            _require(not expired.is_set() and time.monotonic() < deadline,
                     "fixture_timeout", "Fixture service timed out")
            _require(response.status == 200 and len(raw) <= MAX_RESPONSE_BYTES,
                     "fixture_transport", "Fixture service response is unavailable")
            value = json.loads(raw, object_pairs_hook=unique_object)
        except (TimeoutError, socket.timeout):
            _fail("fixture_timeout", "Fixture service timed out")
        except FixtureError:
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError):
            if expired.is_set():
                _fail("fixture_timeout", "Fixture service timed out")
            _fail("fixture_transport", "Fixture service response is unavailable")
        finally:
            timer.cancel()
            connection.close()
        return self._result(value)

    @staticmethod
    def _result(value):
        expected = {"operationId", "generation", "status", "completedAtMs",
                    "retentionExpiresAtMs", "fence"}
        _require(type(value) is dict and set(value) == expected,
                 "fixture_protocol", "Fixture service response is invalid")
        _identifier(value["operationId"], "operation identity")
        _require(type(value["generation"]) is int and value["generation"] >= 1
                 and type(value["fence"]) is int and value["fence"] >= 1
                 and type(value["status"]) is str
                 and value["status"] in {"running", "complete", "failed",
                                         "unknown", "expired"},
                 "fixture_protocol", "Fixture service response is invalid")
        for field_name in ("completedAtMs", "retentionExpiresAtMs"):
            item = value[field_name]
            _require(item is None or (type(item) is int and 0 <= item <= 32_503_680_000_000),
                     "fixture_protocol", "Fixture service response is invalid")
        if value["status"] in _TERMINAL:
            _require(value["completedAtMs"] is not None
                     and value["retentionExpiresAtMs"] is not None,
                     "fixture_protocol", "Terminal fixture evidence is incomplete")
        return FixtureOperationResult(
            value["operationId"], value["generation"], value["status"],
            value["completedAtMs"], value["retentionExpiresAtMs"], value["fence"])

    def execute(self, request: FixtureOperationRequest, *, timeout_seconds: float):
        _require(type(request) is FixtureOperationRequest, "invalid_fixture")
        return self._request("/operations", request.wire(), timeout_seconds)

    def status(self, request: FixtureOperationRequest, *, timeout_seconds: float):
        _require(type(request) is FixtureOperationRequest, "invalid_fixture")
        return self._request("/status", request.wire(include_payload=False),
                             timeout_seconds)


@dataclass(frozen=True, slots=True)
class TrustedFixturePlan:
    project_id: str
    project_revision: str
    project_digest: str
    application_id: str
    fixture_id: str
    check_recipe_ids: tuple[str, ...]
    cleanup_recipe_id: str
    equivalence_digest: str
    adapter: object = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class FixtureAllocation:
    allocation_id: str
    fixture_id: str
    generation: int
    _owner_digest: str = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparationOutcome:
    allocation: FixtureAllocation
    receipts: tuple[dict, ...]
    status: str


class FixtureCoordinator:
    """Single-owner durable allocator with fail-closed restart recovery."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stat = self.root.lstat()
        _require(self.root.is_dir() and not self.root.is_symlink()
                 and stat.st_uid == os.getuid(), "fixture_store",
                 "Fixture store path is invalid")
        self._lock = threading.RLock()
        self._recovery_lock = threading.Lock()
        self._issuer = object()
        self._plans = {}
        self._closed = False
        self._owner_fd = os.open(self.root / ".writer.lock",
                                 os.O_RDWR | os.O_CREAT
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(self._owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._owner_fd)
            self._owner_fd = None
            _fail("fixture_store_busy", "Fixture store already has a live owner")
        try:
            database = self.root / "allocations.sqlite3"
            _require(not database.is_symlink(), "fixture_store",
                     "Fixture store path is invalid")
            self._db = sqlite3.connect(database, timeout=10,
                                       isolation_level=None,
                                       check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._check_existing_schema()
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute("PRAGMA secure_delete=ON")
            self._initialize()
            self._recover_unfinished()
        except Exception:
            self.close()
            raise

    def _check_existing_schema(self):
        tables = {row[0] for row in self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return
        _require(tables in ({"metadata", "allocations", "operations"},
                            {"metadata", "allocations", "operations", "allocation_history"}),
                 "fixture_store", "Unsupported fixture store")
        version = self._db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(allocations)")}
        _require(version is not None and version[0] in (1,2)
                 and ('allocation_history' in tables)==(version[0]==2)
                 and columns == {"slot_key", "allocation_id", "project_id", "project_revision",
                                 "project_digest", "plan_digest", "application_id", "fixture_id",
                                 "owner_digest", "device_digest", "generation", "state",
                                 "created_at_ms", "updated_at_ms", "reason"},
                 "fixture_store", "Unsupported fixture store")
        if version[0]==2:
            _require({row[1] for row in self._db.execute('PRAGMA table_info(allocation_history)')}
                =={'allocation_id','binding_json'},'fixture_store','Unsupported fixture history')

    def _initialize(self):
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS allocations (
                slot_key TEXT PRIMARY KEY,
                allocation_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                project_revision TEXT NOT NULL,
                project_digest TEXT NOT NULL,
                plan_digest TEXT NOT NULL,
                application_id TEXT NOT NULL,
                fixture_id TEXT NOT NULL,
                owner_digest TEXT NOT NULL,
                device_digest TEXT NOT NULL,
                generation INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                allocation_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                operation TEXT NOT NULL,
                recipe_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                started_at_ms INTEGER NOT NULL,
                completed_at_ms INTEGER,
                retention_expires_at_ms INTEGER,
                unsafe_prior INTEGER NOT NULL DEFAULT 0
            );
            """)
        row = self._db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()
        if row is None:
            self._db.execute("INSERT INTO metadata(key,value) VALUES('version',1)")
        else:
            _require(row[0] in (1,2), "fixture_store", "Unsupported fixture store")
        self._db.execute('BEGIN IMMEDIATE')
        try:
            self._db.execute('CREATE TABLE IF NOT EXISTS allocation_history (allocation_id TEXT PRIMARY KEY, binding_json TEXT NOT NULL)')
            self._db.execute("UPDATE metadata SET value=2 WHERE key='version'")
            self._db.commit()
        except Exception:
            self._db.rollback();raise

    def _recover_unfinished(self):
        now = int(time.time() * 1000)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE operations SET state='unknown' WHERE state IN ('admitted','dispatched','running')")
            self._db.execute(
                "UPDATE allocations SET state='quarantined', updated_at_ms=?, "
                "reason='process_restarted' WHERE state != 'available' AND state != 'quarantined'",
                (now,))
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def register_plan(self, registration, *, application_id: str,
                      fixture_id: str, adapter,
                      check_recipe_ids=(), cleanup_recipe_id: str):
        _require(type(registration) is TrustedProjectRegistration,
                 "trusted_registration", "Trusted project registration is required")
        project = registration.project
        contracts.validate_project_revision(project)
        _identifier(application_id, "application identity")
        _identifier(fixture_id, "fixture identity")
        _identifier(cleanup_recipe_id, "cleanup recipe identity")
        _require(type(check_recipe_ids) in (list, tuple)
                 and len(check_recipe_ids) <= 32, "invalid_fixture")
        checks = tuple(_identifier(item, "check recipe identity")
                       for item in check_recipe_ids)
        _require(len(set(checks)) == len(checks), "invalid_fixture")
        applications = {item["id"] for item in project["applications"]}
        recipes = {item["id"]: item for item in project["fixtures"]}
        _require(application_id in applications and fixture_id in recipes
                 and recipes[fixture_id]["operation"] == "prepare"
                 and cleanup_recipe_id in recipes
                 and recipes[cleanup_recipe_id]["operation"] == "cleanup"
                 and all(item in recipes and recipes[item]["operation"] == "check"
                         for item in checks), "invalid_fixture",
                 "Fixture plan does not match the registered project")
        capabilities = getattr(adapter, "capabilities", None)
        endpoint_id = getattr(adapter, "endpoint_id", None)
        _require(type(capabilities) is AdapterCapabilities
                 and callable(getattr(adapter, "execute", None))
                 and callable(getattr(adapter, "status", None)),
                 "invalid_adapter", "Fixture adapter is invalid")
        selected = (recipes[fixture_id], *(recipes[item] for item in checks),
                    recipes[cleanup_recipe_id])
        for recipe in selected:
            _require(recipe.get("exclusive") is True
                     and recipe.get("capabilities") == capabilities.wire()
                     and recipe.get("endpointId") == endpoint_id,
                     "invalid_adapter", "Fixture adapter contract mismatch")
        plan_wire = {
            "projectDigest": registration.project_digest,
            "applicationId": application_id,
            "fixtureId": fixture_id,
            "checks": list(checks),
            "cleanup": cleanup_recipe_id,
            "recipes": list(selected),
            "adapterCapabilities": capabilities.wire(),
        }
        plan = TrustedFixturePlan(
            project["id"], project["revision"], registration.project_digest,
            application_id, fixture_id, checks, cleanup_recipe_id,
            contracts.digest(plan_wire), adapter, self._issuer)
        key = (project["id"], project["revision"], application_id, fixture_id)
        with self._lock:
            existing = self._plans.get(key)
            _require(existing is None
                     or existing.equivalence_digest == plan.equivalence_digest,
                     "fixture_registration_conflict",
                     "Fixture plan registration changed")
            self._plans[key] = plan
        return plan

    def _plan(self, plan):
        _require(type(plan) is TrustedFixturePlan and plan._issuer is self._issuer,
                 "trusted_fixture", "Trusted fixture plan is required")
        current = self._plans.get((plan.project_id, plan.project_revision,
                                   plan.application_id, plan.fixture_id))
        _require(current is plan, "trusted_fixture",
                 "Trusted fixture plan is unavailable")
        return plan

    def require_plan(self, plan):
        """Validate a local registration without allocating or dispatching."""
        return self._plan(plan)

    @staticmethod
    def payload_digest(payload):
        return _sha(_canonical(payload))

    def reserve(self, plan, *, owner: str, device_id: str, allocation_id: str | None = None):
        plan = self._plan(plan)
        _require(type(owner) is str and 0 < len(owner.encode("utf-8")) <= 512
                 and type(device_id) is str
                 and 0 < len(device_id.encode("utf-8")) <= 512,
                 "invalid_fixture")
        owner_digest = _sha(owner.encode("utf-8"))
        device_digest = _sha(device_id.encode("utf-8"))
        slot_key = _sha((plan.project_id + "\0" + plan.fixture_id).encode("utf-8"))
        now = int(time.time() * 1000)
        allocation_id = "allocation_" + uuid.uuid4().hex if allocation_id is None else allocation_id
        _identifier(allocation_id,'fixture reservation identity')
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                _require(self._db.execute('SELECT 1 FROM allocations WHERE allocation_id=?',(allocation_id,)).fetchone() is None
                    and self._db.execute('SELECT 1 FROM allocation_history WHERE allocation_id=?',(allocation_id,)).fetchone() is None,
                    'fixture_reservation_conflict','Fixture reservation identity was already used')
                row = self._db.execute(
                    "SELECT * FROM allocations WHERE slot_key=?", (slot_key,)).fetchone()
                if row is None:
                    generation = 1
                    self._db.execute(
                        "INSERT INTO allocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (slot_key, allocation_id, plan.project_id,
                         plan.project_revision, plan.project_digest, plan.equivalence_digest,
                         plan.application_id, plan.fixture_id,
                         owner_digest, device_digest, generation, "reserved", now,
                         now, None))
                else:
                    _require(row["state"] == "available", "fixture_busy",
                             "Fixture allocation is unavailable")
                    _require(self._db.execute('SELECT COUNT(*) FROM allocation_history').fetchone()[0]<MAX_OPERATIONS,
                             'fixture_store','Fixture history limit reached')
                    self._db.execute('INSERT OR IGNORE INTO allocation_history VALUES(?,?)',
                        (row['allocation_id'],_canonical(dict(row)).decode('utf-8')))
                    generation = row["generation"] + 1
                    self._db.execute(
                        "UPDATE allocations SET allocation_id=?,project_revision=?,"
                        "project_digest=?,plan_digest=?,"
                        "application_id=?,owner_digest=?,device_digest=?,generation=?,"
                        "state='reserved',created_at_ms=?,updated_at_ms=?,reason=NULL "
                        "WHERE slot_key=?",
                        (allocation_id, plan.project_revision, plan.project_digest,
                         plan.equivalence_digest, plan.application_id,
                         owner_digest, device_digest, generation, now, now, slot_key))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return FixtureAllocation(allocation_id, plan.fixture_id, generation,
                                 owner_digest, self._issuer)

    def _allocation(self, allocation):
        _require(type(allocation) is FixtureAllocation
                 and allocation._issuer is self._issuer,
                 "trusted_fixture", "Trusted fixture allocation is required")
        row = self._db.execute(
            "SELECT * FROM allocations WHERE allocation_id=?",
            (allocation.allocation_id,)).fetchone()
        _require(row is not None and row["fixture_id"] == allocation.fixture_id
                 and row["generation"] == allocation.generation
                 and row["owner_digest"] == allocation._owner_digest,
                 "stale_generation", "Fixture allocation generation is stale")
        return row

    @staticmethod
    def _require_plan_binding(row, plan):
        _require(row["project_id"] == plan.project_id
                 and row["project_revision"] == plan.project_revision
                 and row["project_digest"] == plan.project_digest
                 and row["plan_digest"] == plan.equivalence_digest
                 and row["application_id"] == plan.application_id
                 and row["fixture_id"] == plan.fixture_id,
                 "fixture_binding", "Fixture allocation belongs to a different plan")
        return row

    @staticmethod
    def _derived_operation(base: str, suffix: str):
        _identifier(base, "operation identity")
        return "operation_" + _sha((base + "\0" + suffix).encode("utf-8"))[:40]

    def prepare(self, plan, allocation, *, payload, operation_id: str,
                timeout_seconds: float = 10, effect_authorizer=None):
        plan = self._plan(plan)
        self._allocation(allocation)
        def authorize(kind):
            if effect_authorizer is not None:
                _require(callable(effect_authorizer) and effect_authorizer(kind) is True,
                         "fixture_authorization", "Fixture authorization was revoked")
        receipts = []
        authorize("fixture_prepare")
        result = self._operate(plan, allocation, plan.fixture_id, "prepare",
                               payload, operation_id, timeout_seconds)
        if result["status"] != "complete":
            return PreparationOutcome(allocation, tuple(), result["status"])
        receipts.append(self._preparation_receipt(plan, result))
        for index, recipe_id in enumerate(plan.check_recipe_ids, 1):
            authorize("fixture_check")
            child_id = self._derived_operation(operation_id, f"check-{index}")
            result = self._operate(plan, allocation, recipe_id, "check",
                                   payload, child_id, timeout_seconds)
            if result["status"] != "complete":
                return PreparationOutcome(allocation, tuple(receipts), result["status"])
            receipts.append(self._preparation_receipt(plan, result))
        authorize("fixture_complete")
        with self._lock:
            row = self._require_plan_binding(self._allocation(allocation), plan)
            if row["state"] not in {"prepared", "checking", "ready"}:
                return PreparationOutcome(allocation, tuple(copy.deepcopy(receipts)), "unknown")
            self._db.execute(
                "UPDATE allocations SET state='ready',updated_at_ms=?,reason=NULL "
                "WHERE allocation_id=? AND generation=?",
                (int(time.time() * 1000), allocation.allocation_id,
                 allocation.generation))
        return PreparationOutcome(allocation, tuple(copy.deepcopy(receipts)), "complete")

    @staticmethod
    def _preparation_receipt(plan, result):
        return {
            "receiptId": "fixture_" + _sha((result["operationId"] + "\0receipt").encode())[:40],
            "recipeId": result["recipeId"],
            "operation": result["operation"],
            "status": "complete",
            "projectId": plan.project_id,
            "applicationId": plan.application_id,
            "startedAtMs": result["startedAtMs"],
            "completedAtMs": result["completedAtMs"],
            "payloadDigest": result["payloadDigest"],
        }

    def cleanup(self, plan, allocation, *, operation_id: str,
                timeout_seconds: float = 10):
        plan = self._plan(plan)
        self._allocation(allocation)
        return self._operate(plan, allocation, plan.cleanup_recipe_id, "cleanup",
                             {}, operation_id, timeout_seconds)

    def _operate(self, plan, allocation, recipe_id, operation, payload,
                 operation_id, timeout_seconds):
        _identifier(operation_id, "operation identity")
        _require(type(timeout_seconds) in (int, float)
                 and not isinstance(timeout_seconds, bool)
                 and 0 < timeout_seconds <= 60, "invalid_timeout",
                 "Fixture timeout is invalid")
        payload_bytes = _canonical(payload)
        payload_digest = _sha(payload_bytes)
        key_wire = {"allocationGeneration": allocation.generation,
                    "operationId": operation_id,
                    "payloadDigest": payload_digest}
        idempotency_key = _sha(_canonical(key_wire))
        now = int(time.time() * 1000)
        capabilities = plan.adapter.capabilities
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                allocation_row = self._allocation(allocation)
                self._require_plan_binding(allocation_row, plan)
                _require(allocation_row["state"] not in {"available"},
                         "stale_generation", "Fixture allocation generation is stale")
                existing = self._db.execute(
                    "SELECT * FROM operations WHERE operation_id=?",
                    (operation_id,)).fetchone()
                if existing is not None:
                    _require(existing["allocation_id"] == allocation.allocation_id
                             and existing["generation"] == allocation.generation
                             and existing["recipe_id"] == recipe_id
                             and existing["payload_digest"] == payload_digest
                             and existing["idempotency_key"] == idempotency_key,
                             "idempotency_conflict",
                             "Fixture operation identity was reused")
                    if (existing["retention_expires_at_ms"] is not None
                            and existing["retention_expires_at_ms"] <= now):
                        self._db.execute(
                            "UPDATE operations SET state='unknown' "
                            "WHERE operation_id=?", (operation_id,))
                        self._db.execute(
                            "UPDATE allocations SET state='quarantined',"
                            "updated_at_ms=?,reason='retention_expired' "
                            "WHERE allocation_id=? AND generation=?",
                            (now, allocation.allocation_id,
                             allocation.generation))
                        existing = self._db.execute(
                            "SELECT * FROM operations WHERE operation_id=?",
                            (operation_id,)).fetchone()
                    self._db.commit()
                    return self._operation_public(existing)
                if operation == "prepare":
                    _require(allocation_row["state"] == "reserved",
                             "fixture_busy", "Fixture preparation is already admitted")
                elif operation == "check":
                    _require(allocation_row["state"] in {"prepared", "checking"},
                             "fixture_busy", "Fixture preparation is unavailable")
                    pending = self._db.execute(
                        "SELECT COUNT(*) FROM operations WHERE allocation_id=? AND generation=? "
                        "AND state NOT IN ('complete','failed')",
                        (allocation.allocation_id, allocation.generation)).fetchone()[0]
                    _require(pending == 0, "fixture_busy", "A prior fixture operation is unresolved")
                else:
                    _require(allocation_row["state"] != "cleaning",
                             "fixture_busy", "Fixture cleanup is already admitted")
                _require(self._db.execute(
                    "SELECT COUNT(*) FROM operations").fetchone()[0] < MAX_OPERATIONS,
                    "fixture_store", "Fixture operation limit reached")
                unsafe_prior = 0
                if operation == "cleanup":
                    unsafe_prior = int(self._db.execute(
                        "SELECT COUNT(*) FROM operations WHERE allocation_id=? "
                        "AND generation=? AND operation!='cleanup' "
                        "AND state NOT IN ('complete','failed')",
                        (allocation.allocation_id, allocation.generation)).fetchone()[0] > 0)
                next_state = {"prepare": "preparing", "check": "checking",
                              "cleanup": "cleaning"}[operation]
                self._db.execute(
                    "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (operation_id, allocation.allocation_id, allocation.generation,
                     operation, recipe_id, payload_digest, idempotency_key,
                     "admitted", now, None, None, unsafe_prior))
                self._db.execute(
                    "UPDATE allocations SET state=?,updated_at_ms=?,reason=NULL "
                    "WHERE allocation_id=? AND generation=?",
                    (next_state, now, allocation.allocation_id,
                     allocation.generation))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        if not capabilities.remote_fencing or not capabilities.terminal_status:
            return self._finish_unknown(allocation, operation_id,
                                        "adapter_unfenced")
        request = FixtureOperationRequest(
            operation_id, allocation.allocation_id, allocation.generation,
            idempotency_key, operation, recipe_id, payload_digest,
            copy.deepcopy(payload))
        with self._lock:
            self._db.execute("UPDATE operations SET state='dispatched' WHERE operation_id=?",
                             (operation_id,))
        try:
            remote = plan.adapter.execute(request, timeout_seconds=timeout_seconds)
        except FixtureError as error:
            if error.code not in {"fixture_timeout", "fixture_transport"}:
                return self._finish_unknown(allocation, operation_id,
                                            "protocol_unknown")
            try:
                remote = plan.adapter.status(
                    request, timeout_seconds=min(float(timeout_seconds), 5.0))
            except FixtureError:
                return self._finish_unknown(allocation, operation_id,
                                            "status_unknown")
        return self._finish_remote(allocation, operation_id, remote)

    def _finish_remote(self, allocation, operation_id, remote):
        # The process writer lease excludes other coordinators; this lock also
        # keeps admission and the receipt's reuse decision in one critical section.
        with self._lock:
            return self._finish_remote_locked(allocation, operation_id, remote)

    def _finish_remote_locked(self, allocation, operation_id, remote):
        now = int(time.time() * 1000)
        _require(type(remote) is FixtureOperationResult,
                 "fixture_protocol", "Fixture service response is invalid")
        allocation_row = self._allocation(allocation)
        row = self._db.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if (remote.operation_id != operation_id
                or remote.generation != allocation.generation
                or remote.fence != allocation.generation):
            return self._finish_unknown(allocation, operation_id, "fence_mismatch")
        state = remote.status
        reason = None
        if state in _TERMINAL:
            if (remote.retention_expires_at_ms is None
                    or remote.retention_expires_at_ms <= now
                    or remote.completed_at_ms is None
                    or remote.completed_at_ms > now):
                state = "unknown"
                reason = "retention_expired"
        elif state in {"running", "unknown", "expired"}:
            reason = ("retention_expired" if state == "expired"
                      else "remote_nonterminal")
            state = "unknown"
        allocation_state = "quarantined"
        if state == "complete":
            if (row["operation"] == "cleanup" and not row["unsafe_prior"]
                    and allocation_row["state"] == "cleaning"):
                nonterminal = self._db.execute(
                    "SELECT COUNT(*) FROM operations WHERE allocation_id=? "
                    "AND generation=? AND operation_id!=? "
                    "AND state NOT IN ('complete','failed')",
                    (allocation.allocation_id, allocation.generation,
                     operation_id)).fetchone()[0]
                allocation_state = "available" if nonterminal == 0 else "quarantined"
                if nonterminal:
                    reason = "prior_operation_unknown"
            elif row["operation"] == "prepare" and allocation_row["state"] == "preparing":
                allocation_state = "prepared"
            elif row["operation"] == "check" and allocation_row["state"] == "checking":
                allocation_state = "checking"
        elif state == "failed":
            allocation_state = "quarantined"
            reason = "remote_failed"
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "UPDATE operations SET state=?,completed_at_ms=?,"
                    "retention_expires_at_ms=? WHERE operation_id=?",
                    (state, remote.completed_at_ms,
                     remote.retention_expires_at_ms, operation_id))
                self._db.execute(
                    "UPDATE allocations SET state=?,updated_at_ms=?,reason=? "
                    "WHERE allocation_id=? AND generation=?",
                    (allocation_state, now, reason, allocation.allocation_id,
                     allocation.generation))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self._operation_public(self._db.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone())

    def _finish_unknown(self, allocation, operation_id, reason):
        now = int(time.time() * 1000)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "UPDATE operations SET state='unknown' WHERE operation_id=?",
                    (operation_id,))
                self._db.execute(
                    "UPDATE allocations SET state='quarantined',updated_at_ms=?,"
                    "reason=? WHERE allocation_id=? AND generation=?",
                    (now, reason, allocation.allocation_id,
                     allocation.generation))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self._operation_public(self._db.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone())

    @staticmethod
    def _operation_public(row):
        return {
            "operationId": row["operation_id"],
            "generation": row["generation"],
            "operation": row["operation"],
            "recipeId": row["recipe_id"],
            "payloadDigest": row["payload_digest"],
            "idempotencyKey": row["idempotency_key"],
            "status": row["state"],
            "startedAtMs": row["started_at_ms"],
            "completedAtMs": row["completed_at_ms"],
        }

    def status(self, allocation):
        row = self._allocation(allocation)
        return {"allocationId": row["allocation_id"],
                "fixtureId": row["fixture_id"],
                "generation": row["generation"], "state": row["state"],
                "reason": row["reason"]}

    def retain_for_cleanup(self, allocation):
        """Keep a slot unavailable while a related producer may still run."""
        with self._lock:
            self._allocation(allocation)
            self._db.execute(
                "UPDATE allocations SET state='quarantined',reason='producer_cleanup_unknown',"
                "updated_at_ms=? WHERE allocation_id=? AND generation=?",
                (int(time.time() * 1000), allocation.allocation_id, allocation.generation))
        return self.status(allocation)

    def recover_allocation(self, plan, *, allocation_id: str, owner: str):
        """Reissue local authority for an already quarantined durable slot."""
        plan=self._plan(plan);_identifier(allocation_id,"allocation identity")
        _require(type(owner) is str and 0<len(owner.encode("utf-8"))<=512,
                 "invalid_fixture")
        row=self._db.execute(
            "SELECT * FROM allocations WHERE allocation_id=?",
            (allocation_id,)).fetchone()
        _require(row is not None and row["state"]=="quarantined"
                 and row["project_id"]==plan.project_id
                 and row["project_revision"]==plan.project_revision
                 and row["application_id"]==plan.application_id
                 and row["fixture_id"]==plan.fixture_id
                 and row["owner_digest"]==_sha(owner.encode("utf-8")),
                 "recovery_unavailable",
                 "Fixture allocation recovery is unavailable")
        self._require_plan_binding(row, plan)
        return FixtureAllocation(row["allocation_id"],row["fixture_id"],
                                 row["generation"],row["owner_digest"],self._issuer)

    def lookup_recovery_allocation(self,plan,*,allocation_id,owner,device_id):
        """Read a bound current or historical reservation; absence grants no authority."""
        plan=self._plan(plan);_identifier(allocation_id,'fixture reservation identity')
        _require(all(type(value) is str and 0<len(value.encode())<=512 for value in (owner,device_id)),
                 'recovery_unavailable')
        with self._lock:
            row=self._db.execute('SELECT * FROM allocations WHERE allocation_id=?',(allocation_id,)).fetchone()
            if row is None:
                saved=self._db.execute('SELECT binding_json FROM allocation_history WHERE allocation_id=?',(allocation_id,)).fetchone()
                if saved is None:return None
                row=json.loads(saved[0])
            self._require_plan_binding(row,plan)
            _require(row['allocation_id']==allocation_id and row['owner_digest']==_sha(owner.encode())
                and row['device_digest']==_sha(device_id.encode()),'recovery_unavailable','Fixture reservation binding changed')
            return {'allocationId':allocation_id,'fixtureId':row['fixture_id'],'generation':row['generation'],'state':row['state']}

    def seal_unstarted_reservation(self,plan,*,allocation_id,owner,device_id):
        """Persist a bound absence marker so a delayed reserve cannot start later."""
        plan=self._plan(plan);_identifier(allocation_id,'fixture reservation identity')
        _require(all(type(value) is str and 0<len(value.encode())<=512 for value in (owner,device_id)),
                 'recovery_unavailable')
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                _require(self._db.execute('SELECT 1 FROM allocations WHERE allocation_id=?',(allocation_id,)).fetchone() is None,
                    'recovery_unavailable','Fixture reservation already exists')
                saved=self._db.execute('SELECT binding_json FROM allocation_history WHERE allocation_id=?',(allocation_id,)).fetchone()
                if saved is not None:
                    row=json.loads(saved[0]);self._require_plan_binding(row,plan)
                    _require(row['allocation_id']==allocation_id and row['generation']==0 and row['state']=='available'
                        and row['reason']=='unstarted-reservation-sealed'
                        and row['owner_digest']==_sha(owner.encode()) and row['device_digest']==_sha(device_id.encode()),
                        'recovery_unavailable','Fixture reservation history changed')
                else:
                    _require(self._db.execute('SELECT COUNT(*) FROM allocation_history').fetchone()[0]<MAX_OPERATIONS,
                             'fixture_store','Fixture history limit reached')
                    now=int(time.time()*1000)
                    row={'slot_key':_sha((plan.project_id+'\0'+plan.fixture_id).encode()),'allocation_id':allocation_id,
                        'project_id':plan.project_id,'project_revision':plan.project_revision,'project_digest':plan.project_digest,
                        'plan_digest':plan.equivalence_digest,'application_id':plan.application_id,'fixture_id':plan.fixture_id,
                        'owner_digest':_sha(owner.encode()),'device_digest':_sha(device_id.encode()),'generation':0,
                        'state':'available','reason':'unstarted-reservation-sealed','created_at_ms':now,'updated_at_ms':now}
                    self._db.execute('INSERT INTO allocation_history VALUES(?,?)',(allocation_id,_canonical(row).decode()))
                self._db.commit()
                return {'status':'complete','allocationId':allocation_id,'generation':0,'unstarted':True,
                    'historyOnly':True,'evidenceDigest':_sha(_canonical(row))}
            except Exception:
                self._db.rollback();raise

    def recover_cleanup(self,plan,*,allocation_id,generation,owner,device_id,prepare_operation_id,
                        payload_digest,cleanup_operation_id,timeout_seconds=5,effect_authorizer=None,allow_unstarted=False):
        """Clean only a bound quarantined slot, or report its retained terminal history."""
        plan=self._plan(plan)
        for value in (allocation_id,prepare_operation_id,cleanup_operation_id):_identifier(value,'fixture recovery identity')
        contracts.validate_digest(payload_digest)
        _require(type(generation) is int and generation>0 and type(allow_unstarted) is bool
            and all(type(value) is str and 0<len(value.encode())<=512 for value in (owner,device_id))
            and type(timeout_seconds) in (int,float) and 0<timeout_seconds<=60,'recovery_unavailable')
        deadline=time.monotonic()+timeout_seconds
        def authorize(_kind='fixture_recovery'):
            _require(time.monotonic()<deadline and (effect_authorizer is None or effect_authorizer(_kind) is True),
                     'fixture_authorization','Fixture recovery authority expired')
            return True
        _require(self._recovery_lock.acquire(timeout=timeout_seconds),'fixture_busy','Fixture recovery is active')
        try:
            authorize()
            with self._lock:
                row=self._db.execute('SELECT * FROM allocations WHERE allocation_id=?',(allocation_id,)).fetchone()
                historical=row is None
                if historical:
                    saved=self._db.execute('SELECT binding_json FROM allocation_history WHERE allocation_id=?',(allocation_id,)).fetchone()
                    _require(saved is not None,'recovery_unavailable','Fixture history is unavailable')
                    row=json.loads(saved[0])
                self._require_plan_binding(row,plan)
                _require(row['allocation_id']==allocation_id and row['generation']==generation
                    and row['owner_digest']==_sha(owner.encode()) and row['device_digest']==_sha(device_id.encode()),
                    'recovery_unavailable','Fixture recovery binding changed')
                operations=self._db.execute('SELECT * FROM operations WHERE allocation_id=? AND generation=? ORDER BY operation_id',
                    (allocation_id,generation)).fetchall()
                if allow_unstarted and not operations:
                    _require(row['state'] in ('reserved','quarantined','available')
                        and (not historical or row['state']=='available'),'recovery_unavailable','Unstarted fixture state changed')
                    if not historical and row['state']!='available':
                        self._db.execute("UPDATE allocations SET state='available',reason='unstarted_recovery',updated_at_ms=? "
                            'WHERE allocation_id=? AND generation=?',(int(time.time()*1000),allocation_id,generation))
                    return {'status':'complete','allocationId':allocation_id,'generation':generation,
                        'historyOnly':historical,'unstarted':True,
                        'evidenceDigest':_sha(_canonical({'allocationId':allocation_id,'generation':generation,'operations':[]}))}
                prepared=next((item for item in operations if item['operation_id']==prepare_operation_id),None)
                _require(prepared is not None and prepared['operation']=='prepare'
                    and prepared['recipe_id']==plan.fixture_id and prepared['payload_digest']==payload_digest,
                    'recovery_unavailable','Original fixture preparation is unavailable')
                if row['state']=='available':
                    _require(all(item['state'] in ('complete','failed') for item in operations)
                        and any(item['operation']=='cleanup' and item['recipe_id']==plan.cleanup_recipe_id
                            and item['state']=='complete' and item['unsafe_prior']==0 for item in operations),
                        'recovery_unavailable','Fixture cleanup history is incomplete')
                    digest=_sha(_canonical([dict(item) for item in operations]))
                    return {'status':'complete','allocationId':allocation_id,'generation':generation,
                            'historyOnly':historical,'evidenceDigest':digest}
                _require(not historical and row['state']=='quarantined','recovery_unavailable','Fixture is not quarantined')
                allocation=FixtureAllocation(allocation_id,plan.fixture_id,generation,row['owner_digest'],self._issuer)
            self.reconcile(plan,allocation,timeout_seconds=max(.001,deadline-time.monotonic()),effect_authorizer=authorize)
            authorize()
            result=self.cleanup(plan,allocation,operation_id=cleanup_operation_id,
                timeout_seconds=max(.001,deadline-time.monotonic()))
            authorize()
            status='complete' if result['status']=='complete' and self.status(allocation)['state']=='available' else 'unknown'
            return {'status':status,'allocationId':allocation_id,'generation':generation,'historyOnly':False,
                    'evidenceDigest':_sha(_canonical(result))}
        finally:
            self._recovery_lock.release()

    def reconcile(self, plan, allocation, *, timeout_seconds: float = 5, effect_authorizer=None):
        plan = self._plan(plan)
        self._require_plan_binding(self._allocation(allocation), plan)
        operations = self._db.execute(
            "SELECT * FROM operations WHERE allocation_id=? AND generation=? "
            "ORDER BY started_at_ms,operation_id",
            (allocation.allocation_id, allocation.generation)).fetchall()
        results = []
        deadline=time.monotonic()+timeout_seconds
        for row in operations:
            _require(effect_authorizer is None or effect_authorizer('fixture_reconcile') is True,
                     'fixture_authorization','Fixture reconciliation authority expired')
            request = FixtureOperationRequest(
                row["operation_id"], row["allocation_id"], row["generation"],
                row["idempotency_key"], row["operation"], row["recipe_id"],
                row["payload_digest"], None)
            try:
                remote = plan.adapter.status(request,
                                             timeout_seconds=max(.001,deadline-time.monotonic()))
            except FixtureError:
                self._finish_unknown(allocation, row["operation_id"],
                                     "status_unknown")
            else:
                self._finish_remote(allocation, row["operation_id"], remote)
            results.append(self._operation_public(self._db.execute(
                "SELECT * FROM operations WHERE operation_id=?",
                (row["operation_id"],)).fetchone()))
        # Reconciliation proves history but never turns an orphaned allocation
        # into reusable state.  A cleanup admitted after all effects are known
        # is required for that transition.
        with self._lock:
            current = self._allocation(allocation)
            if current["state"] != "available":
                self._db.execute(
                    "UPDATE allocations SET state='quarantined',updated_at_ms=?,"
                    "reason=COALESCE(reason,'reconciliation_required') "
                    "WHERE allocation_id=? AND generation=?",
                    (int(time.time() * 1000), allocation.allocation_id,
                     allocation.generation))
        return {"allocation": self.status(allocation),
                "operations": copy.deepcopy(results)}

    def close(self):
        with getattr(self, "_lock", threading.RLock()):
            if getattr(self, "_closed", False):
                return
            self._closed = True
            database = getattr(self, "_db", None)
            if database is not None:
                database.close()
                self._db = None
            owner_fd = getattr(self, "_owner_fd", None)
            if owner_fd is not None:
                try:
                    fcntl.flock(owner_fd, fcntl.LOCK_UN)
                finally:
                    os.close(owner_fd)
                    self._owner_fd = None


__all__ = [
    "AdapterCapabilities", "FixtureAllocation", "FixtureCoordinator",
    "FixtureError", "FixtureOperationRequest", "FixtureOperationResult",
    "LoopbackFixtureAdapter", "PreparationOutcome", "TrustedFixturePlan",
]
