"""Durable enrolled-host inventory without retaining raw device identifiers."""
from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time

from ..core import ContractError
from .access import HostContext
from .authority import HostAuthority, canonical_device_fingerprint, _canonical_digest
from .model import LiveError


INVENTORY_VERSION = 2
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KINDS = frozenset({"android", "ios-physical", "ios-simulator"})
_STATES = frozenset({
    "available", "reserved", "busy", "uncertain", "quarantined",
    "recovering", "repairing",
})


def _fail(code, message, status=409):
    raise LiveError(code, message, status)


def _identifier(value, label):
    if type(value) is not str or _ID.fullmatch(value) is None:
        _fail("invalid_argument", f"Invalid inventory {label}", 400)
    return value


def canonical_device_digest(device_kind: str, physical_id: str) -> str:
    return canonical_device_fingerprint(device_kind, physical_id)


def enrolled_inventory_document(devices, *, generation, incarnation, authority=None):
    if type(generation) is not int or generation < 1:
        raise ContractError("Invalid host generation")
    _identifier(incarnation, "incarnation")
    result = []
    aliases = set()
    physical = set()
    for device in devices:
        binding = device.get("_authority") if isinstance(device, dict) else None
        if not isinstance(binding, dict):
            continue
        alias = _identifier(device.get("id"), "alias")
        kind = binding.get("deviceKind")
        digest = canonical_device_digest(kind, binding.get("physicalId"))
        if alias in aliases or digest in physical:
            _fail("duplicate_device", "Physical inventory contains a duplicate", 409)
        aliases.add(alias);physical.add(digest)
        capabilities = device.get("capabilities", {})
        profile_digest = capabilities.get("applicationProfileDigest")
        if profile_digest is not None and (type(profile_digest) is not str
                                           or _DIGEST.fullmatch(profile_digest) is None):
            _fail("invalid_argument", "Invalid inventory profile digest", 400)
        result.append({
            "alias": alias, "deviceKind": kind, "physicalDigest": digest,
            "profileDigest": profile_digest,
            "state": device.get("state", "available"),
            "ownership": (authority.inventory_ownership(
                device_kind=kind, physical_id=binding["physicalId"])
                if type(authority) is HostAuthority else None),
        })
    return {"schemaVersion": INVENTORY_VERSION, "generation": generation,
            "incarnation": incarnation, "devices": result}


class InventoryRegistry:
    """Coordinator-owned physical identity journal keyed by opaque digests."""

    def __init__(self, root, *, clock_ms=None):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if (self.root.is_symlink() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()):
            raise ContractError("Unsafe inventory root")
        os.chmod(self.root, 0o700)
        self.path = self.root / "inventory.sqlite3"
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ContractError("Unsafe inventory state")
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self._closed = False
        lock_fd = os.open(self.root / ".initialize.lock",
                          os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                          0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._connection = sqlite3.connect(
                self.path, timeout=10, isolation_level=None,
                check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout=10000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            existing = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='devices'").fetchone()
            if (existing is not None and version != INVENTORY_VERSION
                    or existing is None and version not in (0, INVENTORY_VERSION)):
                _fail("migration_required", "Earlier inventory requires offline migration")
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS devices(
                    physical_digest TEXT PRIMARY KEY,
                    inventory_id TEXT NOT NULL UNIQUE,
                    host_id TEXT NOT NULL, host_generation INTEGER NOT NULL,
                    host_incarnation TEXT NOT NULL, alias TEXT NOT NULL,
                    device_kind TEXT NOT NULL, profile_digest TEXT,
                    state TEXT NOT NULL, last_sequence INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL,
                    ownership_json TEXT, authority_root TEXT,
                    ownership_generation INTEGER NOT NULL,
                    release_floor INTEGER NOT NULL,
                    held INTEGER NOT NULL,
                    UNIQUE(host_id,alias)
                ) WITHOUT ROWID""")
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS hosts(
                    host_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                    incarnation TEXT NOT NULL, sequence INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL
                ) WITHOUT ROWID""")
            self._connection.execute(f"PRAGMA user_version={INVENTORY_VERSION}")
            os.chmod(self.path, 0o600)
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    @staticmethod
    def _host(host):
        if type(host) is not HostContext:
            _fail("unauthorized", "Host authentication required", 401)
        return host

    @staticmethod
    def _ownership(value, physical_digest):
        if value is None:
            return None
        fields = {"physicalDigest", "authorityRootDigest", "generation", "status",
                  "hostIncarnation", "helperIncarnation", "releaseReceiptDigest"}
        if type(value) is not dict or set(value) != fields:
            _fail("invalid_argument", "Invalid inventory ownership", 400)
        if (value["physicalDigest"] != physical_digest
                or type(value["authorityRootDigest"]) is not str
                or _DIGEST.fullmatch(value["authorityRootDigest"]) is None
                or type(value["generation"]) is not int
                or not 0 <= value["generation"] < 2**53
                or type(value["status"]) is not str
                or value["status"] not in {"unused", "owned", "released", "expired", "quarantined"}
                or (value["generation"] == 0) != (value["status"] == "unused")):
            _fail("invalid_argument", "Invalid inventory ownership", 400)
        _identifier(value["hostIncarnation"], "authority incarnation")
        if value["status"] == "unused":
            if value["helperIncarnation"] is not None:
                _fail("invalid_argument", "Invalid inventory ownership", 400)
        else:
            _identifier(value["helperIncarnation"], "helper incarnation")
        receipt = value["releaseReceiptDigest"]
        expected = (_canonical_digest({key: item for key, item in value.items()
                                       if key != "releaseReceiptDigest"})
                    if value["status"] == "released" else None)
        if receipt != expected:
            _fail("invalid_argument", "Invalid inventory release receipt", 400)
        return value

    def refresh(self, host, document):
        host = self._host(host)
        if type(document) is not dict or set(document) != {
                "schemaVersion", "generation", "incarnation", "devices"}:
            _fail("invalid_argument", "Invalid inventory update", 400)
        if document["schemaVersion"] != INVENTORY_VERSION \
                or type(document["schemaVersion"]) is not int \
                or document["generation"] != host.generation \
                or type(document["generation"]) is not int \
                or document["incarnation"] != host.incarnation \
                or type(document["devices"]) is not list \
                or len(document["devices"]) > 128:
            _fail("stale_host", "Inventory host identity is stale", 409)
        parsed = []
        aliases = set();digests = set()
        for item in document["devices"]:
            if type(item) is not dict or set(item) != {
                    "alias", "deviceKind", "physicalDigest", "profileDigest", "state", "ownership"}:
                _fail("invalid_argument", "Invalid inventory device", 400)
            alias = _identifier(item["alias"], "alias")
            kind = item["deviceKind"]
            digest = item["physicalDigest"]
            profile = item["profileDigest"]
            if type(kind) is not str or kind not in _KINDS or type(digest) is not str or _DIGEST.fullmatch(digest) is None \
                    or (profile is not None and (type(profile) is not str
                                                 or _DIGEST.fullmatch(profile) is None)) \
                    or type(item["state"]) is not str or item["state"] not in _STATES:
                _fail("invalid_argument", "Invalid inventory device", 400)
            if alias in aliases or digest in digests:
                _fail("duplicate_device", "Physical inventory contains a duplicate", 409)
            aliases.add(alias);digests.add(digest)
            parsed.append((digest, alias, kind, profile, item["state"],
                           self._ownership(item["ownership"], digest)))
        now = self._clock()
        if type(now) is not int or now < 0:
            raise ContractError("Invalid inventory clock")
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            reconciliation_required = False
            try:
                prior_host = connection.execute(
                    "SELECT * FROM hosts WHERE host_id=?", (host.host_id,)).fetchone()
                replaced = (prior_host is not None
                            and (prior_host["generation"], prior_host["incarnation"])
                            != (host.generation, host.incarnation))
                if replaced:
                    # A registration timestamp or restarted process is not a
                    # cleanup proof.  The old inventory remains unreassignable.
                    connection.execute(
                        "UPDATE devices SET state='uncertain',held=1,updated_ms=? WHERE host_id=? "
                        "AND (held=1 OR ownership_generation>0 OR authority_root IS NULL)",
                        (now, host.host_id))
                sequence = 1 if prior_host is None else prior_host["sequence"] + 1
                for digest, alias, kind, profile, state, ownership in parsed:
                    existing = connection.execute(
                        "SELECT * FROM devices WHERE physical_digest=? OR (host_id=? AND alias=?)",
                        (digest, host.host_id, alias)).fetchall()
                    if len(existing) > 1 or (existing and existing[0]["physical_digest"] != digest):
                        _fail("duplicate_device", "Inventory alias identifies another device", 409)
                    if existing:
                        row = existing[0]
                        if row["host_id"] != host.host_id:
                            _fail("duplicate_device",
                                  "Physical device is registered by another host", 409)
                        if row["alias"] != alias:
                            _fail("duplicate_device", "Physical device alias changed", 409)
                        root = row["authority_root"]
                        watermark = row["ownership_generation"]
                        floor = row["release_floor"]
                        held = bool(row["held"])
                        compatible = (ownership is not None
                                      and ownership["authorityRootDigest"] == root
                                      and ownership["generation"] >= watermark)
                        if ownership is not None and root is None and not held:
                            root = ownership["authorityRootDigest"]
                            compatible = True
                        if ownership is not None and not compatible:
                            held = True
                        if compatible:
                            watermark = ownership["generation"]
                        can_release = (compatible and ownership["status"] == "released"
                                       and ownership["generation"] >= floor)
                        if state == "available" and held and can_release:
                            held = False
                        if state != "available":
                            held = True
                            floor = max(floor, watermark + (
                                1 if state in {"quarantined", "uncertain", "recovering", "repairing"}
                                or ownership is None or ownership["status"] in {"released", "unused"}
                                else 0))
                        if ownership is not None and ownership["status"] not in {"unused", "released"}:
                            held = True
                            floor = max(floor, watermark + (
                                1 if ownership["status"] in {"quarantined", "expired"} else 0))
                        effective = "uncertain" if held and state == "available" else state
                        if held and state == "available":
                            reconciliation_required = True
                        connection.execute(
                            "UPDATE devices SET host_generation=?,host_incarnation=?,"
                            "profile_digest=?,state=?,last_sequence=?,updated_ms=?,"
                            "ownership_json=?,authority_root=?,ownership_generation=?,release_floor=?,held=? "
                            "WHERE physical_digest=?",
                            (host.generation, host.incarnation, profile,
                             effective, sequence, now,
                             json.dumps(ownership) if compatible else row["ownership_json"],
                             root, watermark, floor, int(held), digest))
                    else:
                        inventory_id = "device_" + digest[:24]
                        watermark = 0 if ownership is None else ownership["generation"]
                        held = (state != "available" or ownership is not None
                                and ownership["status"] not in {"unused", "released"})
                        floor = watermark + (1 if held and (
                            state in {"quarantined", "uncertain", "recovering", "repairing"}
                            or ownership is None or ownership["status"] in {
                                "unused", "released", "quarantined", "expired"}) else 0)
                        effective = "uncertain" if held and state == "available" else state
                        connection.execute(
                            "INSERT INTO devices VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (digest, inventory_id, host.host_id, host.generation,
                             host.incarnation, alias, kind, profile, effective, sequence, now,
                             json.dumps(ownership) if ownership is not None else None,
                             None if ownership is None else ownership["authorityRootDigest"],
                             watermark, floor, int(held)))
                connection.execute(
                    "UPDATE devices SET state=CASE WHEN held=1 THEN 'uncertain' "
                    "ELSE 'missing' END,updated_ms=? WHERE host_id=? AND "
                    "host_generation=? AND host_incarnation=? AND last_sequence<?",
                    (now, host.host_id, host.generation, host.incarnation, sequence))
                connection.execute(
                    "INSERT INTO hosts VALUES(?,?,?,?,?) ON CONFLICT(host_id) DO UPDATE SET "
                    "generation=excluded.generation,incarnation=excluded.incarnation,"
                    "sequence=excluded.sequence,updated_ms=excluded.updated_ms",
                    (host.host_id, host.generation, host.incarnation, sequence, now))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if reconciliation_required:
            _fail("device_reconciliation_required",
                  "Physical device requires authority reconciliation", 409)
        return {"schemaVersion": INVENTORY_VERSION, "hostId": host.host_id,
                "generation": host.generation, "incarnation": host.incarnation,
                "sequence": sequence, "devices": self.list_host(host.host_id)}

    def list_host(self, host_id):
        _identifier(host_id, "host")
        with self._lock:
            rows = self._connection.execute(
                "SELECT inventory_id,alias,device_kind,profile_digest,state FROM devices "
                "WHERE host_id=? ORDER BY alias", (host_id,)).fetchall()
        return [{"inventoryId": row["inventory_id"], "alias": row["alias"],
                 "deviceKind": row["device_kind"],
                 "profileDigest": row["profile_digest"], "state": row["state"]}
                for row in rows]

    def require_available(self, binding, *, max_age_ms=15_000):
        """Fence scheduling against current qualified inventory, without claiming it."""
        if type(binding) is not dict or set(binding) != {
                "hostId", "generation", "incarnation", "alias", "profileDigest"}:
            _fail("inventory_unavailable", "Enrolled device binding is unavailable")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM devices WHERE host_id=? AND alias=?",
                (binding["hostId"], binding["alias"])).fetchone()
            now = self._clock()
            if (row is None or row["state"] != "available" or row["held"]
                    or row["host_generation"] != binding["generation"]
                    or row["host_incarnation"] != binding["incarnation"]
                    or row["profile_digest"] != binding["profileDigest"]
                    or row["ownership_json"] is None
                    or not 0 <= now - row["updated_ms"] <= max_age_ms):
                _fail("inventory_unavailable", "Enrolled device needs a current ownership report")
            ownership = json.loads(row["ownership_json"])
            if ownership["status"] not in {"unused", "released"}:
                _fail("inventory_unavailable", "Enrolled device ownership is unresolved")
            return row["inventory_id"]

    def close(self):
        with self._lock:
            if not self._closed:
                self._connection.close();self._closed = True


__all__ = ["INVENTORY_VERSION", "InventoryRegistry", "canonical_device_digest",
           "enrolled_inventory_document"]
