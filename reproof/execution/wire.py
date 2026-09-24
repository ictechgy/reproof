"""Bounded authenticated host/guest records over one VM-owned byte channel.

The bootstrap is confidential only because its transport belongs to the newly
started VM, before candidate code is admitted. HMAC protects protocol framing;
it never turns candidate test output into independent validation evidence.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import select
import struct
import threading
import time
import unicodedata

from reproloop.core import ContractError
from reproloop.contracts.versions import (
    bounded_int, bounded_list, exact, require, safe_relative_path,
    validate_digest, validate_id,
)

MAX_FRAME_BYTES = 256 * 1024
CHUNK_BYTES = 32 * 1024
MAX_TRANSFER_BYTES = 64 * 1024 * 1024
MAX_FILES = 1024
MAX_SEQUENCE = 1_000_000
PROBE_MODES = frozenset({"containment", "hold", "oversize", "forged-report"})


class ProtocolError(RuntimeError):
    """Static protocol failure: no peer payload or secret is included."""


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ProtocolError("Invalid protocol data") from None


def decode_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid(_):
        raise ValueError()

    try:
        if type(raw) is not bytes or len(raw) > MAX_FRAME_BYTES:
            raise ValueError()
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("Invalid protocol JSON") from None


def decode_chunk(data):
    try:
        require(type(data) is str and len(data) <= 4 * ((CHUNK_BYTES + 2) // 3),
                "Invalid chunk")
        raw = base64.b64decode(data, validate=True)
        require(0 < len(raw) <= CHUNK_BYTES and base64.b64encode(raw).decode() == data,
                "Invalid chunk")
        return raw
    except (ValueError, ContractError):
        raise ProtocolError("Invalid transfer chunk") from None


def validate_manifest(payload):
    exact(payload, ("digest", "files"))
    validate_digest(payload["digest"])
    files = bounded_list(payload["files"], "transfer files", MAX_FILES, minimum=1)
    paths = set()
    for item in files:
        exact(item, ("path", "digest", "size"))
        path = safe_transfer_path(item["path"])
        require(unicodedata.normalize("NFC", path) == path, "Invalid transfer path")
        normalized = path.casefold()
        require(normalized not in paths, "Duplicate transfer path")
        paths.add(normalized)
        validate_digest(item["digest"])
        bounded_int(item["size"], "transfer size", 0, MAX_TRANSFER_BYTES)
    for path in paths:
        parts = path.split("/")
        require(not any("/".join(parts[:i]) in paths for i in range(1, len(parts))),
                "Conflicting transfer paths")
    require(sum(item["size"] for item in files) <= MAX_TRANSFER_BYTES, "Transfer limit exceeded")


def safe_transfer_path(value):
    safe_relative_path(value)
    require(not any(part.lower().endswith((".mobileprovision", ".keychain", ".keychain-db"))
                    for part in value.split("/")), "Protected credential path rejected")
    return value


def validate_message(role, kind, payload):
    try:
        require(role in ("host", "guest"), "Invalid role")
        allowed = ({"input-start", "input-chunk", "input-end", "run", "cancel", "probe"}
                   if role == "host" else
                   {"ready", "artifact-start", "artifact-chunk", "artifact-end", "result", "error", "probe-started"})
        require(type(kind) is str and kind in allowed, "Invalid message kind")
        if kind in ("input-start", "artifact-start"):
            validate_manifest(payload)
        elif kind in ("input-chunk", "artifact-chunk"):
            exact(payload, ("index", "offset", "data"))
            bounded_int(payload["index"], "file index", 0, MAX_FILES - 1)
            bounded_int(payload["offset"], "file offset", 0, MAX_TRANSFER_BYTES)
            decode_chunk(payload["data"])
        elif kind in ("input-end", "artifact-end", "cancel"):
            exact(payload, ())
        elif kind == "run":
            exact(payload, ("recipeId", "inputDigest"))
            validate_id(payload["recipeId"])
            validate_digest(payload["inputDigest"])
        elif kind in ("probe", "probe-started"):
            exact(payload, ("mode",))
            require(type(payload["mode"]) is str and payload["mode"] in PROBE_MODES, "Invalid probe mode")
        elif kind == "ready":
            exact(payload, ("agentDigest", "catalogDigest"))
            validate_digest(payload["agentDigest"])
            validate_digest(payload["catalogDigest"])
        elif kind == "result":
            exact(payload, ("exitCode", "outputTruncated", "logDigest"))
            bounded_int(payload["exitCode"], "exit code", -255, 255)
            require(type(payload["outputTruncated"]) is bool, "Invalid truncation flag")
            validate_digest(payload["logDigest"])
        elif kind == "error":
            exact(payload, ("code",))
            require(payload["code"] in ("input-rejected", "recipe-rejected", "execution-failed",
                                        "output-rejected", "cancelled"), "Invalid error code")
    except (ContractError, TypeError, ValueError, RecursionError):
        raise ProtocolError("Invalid protocol message") from None


class Channel:
    def __init__(self, sock, *, key, run_id, role, deadline, cancel=None):
        if type(key) is not bytes or len(key) != 32 or role not in ("host", "guest"):
            raise ProtocolError("Invalid channel configuration")
        validate_id(run_id)
        self.sock, self.key, self.run_id, self.role = sock, key, run_id, role
        self.deadline = deadline
        self.cancel = cancel if cancel is not None else threading.Event()
        self.failed = False
        self._sent = self._received = 0
        self._send_lock, self._receive_lock = threading.Lock(), threading.Lock()
        self.sock.setblocking(False)

    def _io(self, *, data=None, size=None):
        result = bytearray()
        offset = 0
        try:
            while (offset < len(data)) if data is not None else (len(result) < size):
                remaining = self.deadline - time.monotonic()
                if self.failed or self.cancel.is_set() or remaining <= 0:
                    raise ProtocolError("Execution channel interrupted")
                ready = select.select([self.sock] if data is None else [],
                                      [self.sock] if data is not None else [], [], min(0.05, remaining))
                if not (ready[0] or ready[1]):
                    continue
                if self.failed or self.cancel.is_set() or time.monotonic() >= self.deadline:
                    raise ProtocolError("Execution channel interrupted")
                try:
                    if data is not None:
                        count = self.sock.send(memoryview(data)[offset:])
                        if count <= 0:
                            raise OSError()
                        offset += count
                    else:
                        chunk = self.sock.recv(size - len(result))
                        if not chunk:
                            raise OSError()
                        result.extend(chunk)
                except BlockingIOError:
                    continue
            return bytes(result)
        except (OSError, ValueError, ProtocolError):
            self.failed = True
            raise ProtocolError("Execution channel interrupted") from None

    def _send_frame(self, value):
        raw = canonical(value)
        if not 0 < len(raw) <= MAX_FRAME_BYTES:
            raise ProtocolError("Protocol frame limit exceeded")
        self._io(data=struct.pack("!I", len(raw)) + raw)

    def _receive_frame(self):
        size = struct.unpack("!I", self._io(size=4))[0]
        if not 0 < size <= MAX_FRAME_BYTES:
            raise ProtocolError("Protocol frame limit exceeded")
        return decode_json(self._io(size=size))

    def send(self, kind, payload):
        with self._send_lock:
            try:
                if self.failed or self._sent >= MAX_SEQUENCE:
                    raise ProtocolError("Execution channel unavailable")
                validate_message(self.role, kind, payload)
                body = {"protocolVersion": 1, "runId": self.run_id, "direction": self.role,
                        "sequence": self._sent, "type": kind, "payload": payload}
                self._send_frame({**body, "mac": hmac.new(self.key, canonical(body), hashlib.sha256).hexdigest()})
                self._sent += 1
            except (ProtocolError, ContractError):
                self.failed = True
                raise ProtocolError("Execution message rejected") from None

    def receive(self):
        with self._receive_lock:
            try:
                if self.failed or self._received >= MAX_SEQUENCE:
                    raise ProtocolError("Execution channel unavailable")
                value = self._receive_frame()
                exact(value, ("protocolVersion", "runId", "direction", "sequence", "type", "payload", "mac"))
                mac = validate_digest(value.pop("mac"))
                expected = hmac.new(self.key, canonical(value), hashlib.sha256).hexdigest()
                peer = "guest" if self.role == "host" else "host"
                require(hmac.compare_digest(mac, expected), "Invalid MAC")
                require(type(value["protocolVersion"]) is int and value["protocolVersion"] == 1
                        and type(value["sequence"]) is int and value["sequence"] == self._received
                        and value["runId"] == self.run_id and value["direction"] == peer,
                        "Invalid message binding")
                validate_message(peer, value["type"], value["payload"])
                self._received += 1
                return value["type"], value["payload"]
            except (ProtocolError, ContractError, TypeError, RecursionError):
                self.failed = True
                raise ProtocolError("Execution message rejected") from None


def bootstrap(sock, *, run_id, deadline, cancel=None):
    channel = Channel(sock, key=secrets.token_bytes(32), run_id=run_id, role="host",
                      deadline=deadline, cancel=cancel)
    channel._send_frame({"protocolVersion": 1, "runId": run_id,
                         "key": base64.b64encode(channel.key).decode()})
    return channel


def accept_bootstrap(sock, *, deadline):
    temporary = Channel(sock, key=b"\0" * 32, run_id="bootstrap", role="guest", deadline=deadline)
    try:
        value = temporary._receive_frame()
        exact(value, ("protocolVersion", "runId", "key"))
        require(type(value["protocolVersion"]) is int and value["protocolVersion"] == 1,
                "Invalid protocol version")
        validate_id(value["runId"])
        require(type(value["key"]) is str and len(value["key"]) == 44, "Invalid channel key")
        key = base64.b64decode(value["key"], validate=True)
        return Channel(sock, key=key, run_id=value["runId"], role="guest", deadline=deadline)
    except (ContractError, ValueError, TypeError):
        raise ProtocolError("Execution bootstrap rejected") from None
