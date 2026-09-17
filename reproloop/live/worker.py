"""Authenticated loopback worker transport for remote Live providers.

The worker exposes the existing Live session contract over a deliberately
small HTTP API.  It has no shell, filesystem, browser, or installation
surface.  The coordinator side owns the parent lease; the worker owns only
its local provider sessions.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import hashlib
import hmac
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import quote, urlsplit

from ..core import ContractError
from .media import MAX_IMAGE_BYTES, MAX_METADATA_BYTES, encode_frame
from .model import Lab, LiveError, check, public_id
from ..storage import Lease


OWNER = "worker-coordinator"
WORKER_PROTOCOL_VERSION = 2
WORKER_CODE_VERSION = 2
MAX_BODY = 5 * 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SAFE_ERROR = re.compile(r"[a-z0-9_]{1,64}\Z")
_MIMES = {"image/jpeg", "image/png", "image/svg+xml"}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_AUTHORITY_EXCHANGES = 64
AUTHORITY_EXCHANGE_SECONDS = 10
MAX_WORKER_HEADERS = 32
REQUEST_BODY_DEADLINE_SECONDS = 10
REQUEST_DEADLINE_SECONDS = 35
ARTIFACT_RETENTION_INTERVAL = 5


def _require(condition: bool, message: str = "Invalid worker request") -> None:
    if not condition:
        raise ContractError(message)


def _id(value: Any) -> str:
    _require(isinstance(value, str) and _ID.fullmatch(value) is not None)
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _safe_error(exc: BaseException) -> tuple[str, str, int]:
    if isinstance(exc, LiveError):
        code = exc.code if isinstance(exc.code, str) and _SAFE_ERROR.fullmatch(exc.code) else "worker_error"
        known = {
            "unauthorized": "Worker authentication failed", "not_found": "Worker resource not found",
            "invalid_argument": "Invalid worker request", "forbidden": "Worker operation is not allowed",
            "session_inactive": "Worker session is inactive", "stale_controller": "Worker control epoch is stale",
            "transport_unavailable": "Worker transport unavailable",
        }
        return code, known.get(code, "Worker operation failed"), exc.status if 400 <= exc.status <= 599 else 409
    if isinstance(exc, ContractError):
        return "invalid_argument", "Invalid worker request", 400
    return "worker_error", "Worker operation failed", 500


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _frame_packet(frame: dict[str, Any]) -> bytes:
    data = frame.get("bytes")
    check(isinstance(data, bytes) and 0 < len(data) <= MAX_IMAGE_BYTES,
          "invalid_frame", "Invalid worker frame", 400)
    metadata = {key: frame[key] for key in
                ("id", "geometryVersion", "width", "height", "orientation", "capturedAt", "mime")}
    metadata["type"] = "frame"
    return encode_frame(metadata, data)


def _decode_frame(data: bytes) -> tuple[dict[str, Any], bytes]:
    check(isinstance(data, bytes) and 8 <= len(data) <= MAX_RESPONSE, "invalid_frame", "Invalid worker frame")
    metadata_size, image_size = struct.unpack(">II", data[:8])
    check(0 < metadata_size <= MAX_METADATA_BYTES and 0 < image_size <= MAX_IMAGE_BYTES
          and len(data) == 8 + metadata_size + image_size, "invalid_frame", "Invalid worker frame")
    metadata = json.loads(data[8:8 + metadata_size], object_pairs_hook=_pairs, parse_constant=_reject_constant)
    image = data[8 + metadata_size:]
    check(isinstance(metadata, dict) and set(metadata) == {"type", "id", "geometryVersion", "width", "height",
                                                          "orientation", "capturedAt", "mime"}
          and metadata["type"] == "frame" and type(metadata["id"]) is int and metadata["id"] > 0
          and type(metadata["geometryVersion"]) is int and metadata["geometryVersion"] > 0
          and type(metadata["width"]) is int and 0 < metadata["width"] <= 8192
          and type(metadata["height"]) is int and 0 < metadata["height"] <= 8192
          and metadata["orientation"] in {"portrait", "landscape"}
          and type(metadata["capturedAt"]) is int and metadata["capturedAt"] >= 0
          and metadata["mime"] in _MIMES, "invalid_frame", "Invalid worker frame")
    return metadata, image


class _WorkerHandler(BaseHTTPRequestHandler):
    server_version = "ReproWorker"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        self._deadline_timer = threading.Timer(REQUEST_DEADLINE_SECONDS, self._abort_request)
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    def _abort_request(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        try:
            super().finish()
        finally:
            self._deadline_timer.cancel()

    def parse_request(self):
        fields = self.raw_requestline.split()
        raw_target = (fields[1].decode("iso-8859-1", "strict")
                      if len(fields) >= 2 else None)
        parsed = super().parse_request()
        if parsed:
            self._raw_request_target = raw_target
        return parsed

    def log_message(self, *args):
        pass

    @property
    def worker(self):
        return self.server.worker  # type: ignore[attr-defined]

    def _respond(self, status: int, value: Any, *, binary: bool = False,
                 mime: str | None = None, headers: dict[str, str] | None = None):
        if binary:
            data = value
            mime = mime or "application/x-repro-frames"
        else:
            data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            mime = "application/json"
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for name, header_value in (headers or {}).items():
            self.send_header(name, header_value)
        self.end_headers()
        self.wfile.write(data)

    def _authorize(self):
        check(len(self.headers.items()) <= MAX_WORKER_HEADERS,
              "invalid_argument", "Too many worker headers", 431)
        for name in ("Host", "Authorization", "Content-Length", "Content-Type",
                     "Origin", "Sec-Fetch-Site", "Range",
                     "X-Repro-Upload-Generation", "X-Repro-Chunk-SHA256",
                     "X-Repro-Project-Id"):
            if len(self.headers.get_all(name, [])) > 1:
                raise LiveError("invalid_argument", "Ambiguous worker headers", 400)
        if self.headers.get_all("Transfer-Encoding", []):
            raise LiveError("invalid_argument", "Unsupported worker request framing", 400)
        expected_host = self.worker.origin_netloc
        if self.headers.get("Host") != expected_host:
            raise LiveError("invalid_host", "Worker host is invalid", 403)
        if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site"):
            raise LiveError("forbidden", "Worker does not accept browser requests", 403)
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, "Bearer " + self.worker.token):
            raise LiveError("unauthorized", "Worker authentication failed", 401)

    def _raw_body(self, content_type: str, maximum: int) -> bytes:
        if self.headers.get("Content-Type", "").split(";", 1)[0] != content_type:
            raise LiveError("invalid_content_type", "Unexpected worker content type", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise LiveError("invalid_argument", "Invalid request body", 400) from None
        check(0 < length <= maximum, "invalid_argument", "Invalid request body", 413)
        deadline = time.monotonic() + REQUEST_BODY_DEADLINE_SECONDS
        parts = []
        remaining = length
        while remaining:
            available = deadline - time.monotonic()
            check(available > 0, "request_timeout", "Worker request deadline elapsed", 408)
            self.connection.settimeout(available)
            block = self.rfile.read1(min(64 * 1024, remaining))
            check(bool(block), "invalid_argument", "Truncated request body", 400)
            parts.append(block)
            remaining -= len(block)
        return b"".join(parts)

    def _body(self) -> dict[str, Any]:
        raw = self._raw_body("application/json", MAX_BODY)
        try:
            value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        except (ValueError, TypeError, UnicodeError):
            raise LiveError("invalid_argument", "Invalid request body", 400) from None
        check(isinstance(value, dict), "invalid_argument", "Expected an object", 400)
        return value

    def _artifact_project(self):
        return _id(self.headers.get("X-Repro-Project-Id"))

    def _artifact_response(self, value):
        headers = {"Accept-Ranges": "bytes"}
        status = 200
        if self.headers.get("Range") is not None:
            status = 206
            headers["Content-Range"] = f"bytes {value.start}-{value.end - 1}/{value.size}"
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(value.end - value.start))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for name, header_value in headers.items():
            self.send_header(name, header_value)
        self.end_headers()
        for offset in range(0, len(value.body), 64 * 1024):
            self.worker.revalidate_artifact_read(value)
            self.wfile.write(value.body[offset:offset + 64 * 1024])
            self.wfile.flush()
        self.worker.revalidate_artifact_read(value)

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def _dispatch(self):
        self._response_started = False
        try:
            self._authorize()
            target = urlsplit(getattr(self, "_raw_request_target", None) or self.path)
            path = target.path
            check(not target.scheme and not target.netloc
                  and not target.query and not target.fragment and path.startswith("/")
                  and not path.endswith("/") and "//" not in path and "%" not in path
                  and all(part not in {".", ".."} for part in path.split("/")),
                  "invalid_argument", "Invalid worker path", 400)
            parts = [part for part in path.split("/") if part]
            cleanup = (len(parts) == 4 and parts[:2] == ["v1", "sessions"]
                       and parts[3] == "close" and self.command == "POST") \
                or (len(parts) == 4 and parts[:2] == ["v2", "reservations"]
                    and parts[3] == "release" and self.command == "POST")
            if not cleanup:
                check(not self.worker._closing_operations, "worker_closing",
                      "Worker is closing its operations", 503)
                self.worker.authorize_enrolled_host()
            if parts == ["v2", "artifacts", "maintenance"] and self.command == "GET":
                return self._respond(200, self.worker.artifact_maintenance_status())
            if parts == ["v2", "artifacts", "uploads"] and self.command == "POST":
                body = self._body()
                expected = {"projectId", "kind", "size", "digest", "metadata",
                            "retentionClass", "retainUntilMs"}
                check(set(body) == expected, "invalid_argument", "Invalid artifact allocation", 400)
                return self._respond(201, self.worker.allocate_artifact(body))
            if len(parts) == 4 and parts[:3] == ["v2", "artifacts", "uploads"] \
                    and self.command == "GET":
                return self._respond(200, self.worker.artifact_status(
                    _id(parts[3]), self._artifact_project()))
            if len(parts) == 5 and parts[:3] == ["v2", "artifacts", "uploads"] \
                    and parts[4] == "finalize" and self.command == "POST":
                body = self._body()
                check(set(body) == {"uploadGeneration"},
                      "invalid_argument", "Invalid artifact finalization", 400)
                return self._respond(200, self.worker.finalize_artifact(
                    _id(parts[3]), body["uploadGeneration"], self._artifact_project()))
            if len(parts) == 6 and parts[:3] == ["v2", "artifacts", "uploads"] \
                    and parts[4] == "chunks" and self.command == "PUT":
                try:
                    offset = int(parts[5])
                    upload_generation = int(self.headers.get("X-Repro-Upload-Generation", ""))
                except ValueError:
                    raise LiveError("invalid_argument", "Invalid artifact chunk", 400) from None
                chunk_digest = self.headers.get("X-Repro-Chunk-SHA256", "")
                raw = self._raw_body("application/octet-stream",
                                     self.worker.artifact_store.max_chunk_bytes)
                return self._respond(200, self.worker.put_artifact_chunk(
                    _id(parts[3]), upload_generation, offset, raw, chunk_digest,
                    self._artifact_project()))
            if len(parts) == 3 and parts[:2] == ["v2", "artifacts"] \
                    and self.command == "GET":
                range_value = self.headers.get("Range")
                start, end = 0, None
                if range_value is not None:
                    match = re.fullmatch(r"bytes=(0|[1-9][0-9]*)-(0|[1-9][0-9]*)", range_value)
                    check(match is not None, "invalid_argument", "Invalid artifact range", 416)
                    start, inclusive_end = (int(match.group(1)), int(match.group(2)))
                    check(inclusive_end >= start and inclusive_end - start < MAX_RESPONSE,
                          "response_limit", "Artifact range is too large", 416)
                    end = inclusive_end + 1
                with self.worker.open_artifact_read(
                    _id(parts[2]), start=start, end=end,
                    project_id=self._artifact_project()) as value:
                    check(value.end - value.start <= MAX_RESPONSE,
                          "response_limit", "Artifact response is too large", 413)
                    self.worker.revalidate_artifact_read(value)
                    return self._artifact_response(value)
            if len(parts) == 3 and parts[:2] == ["v2", "artifacts"] \
                    and self.command == "DELETE":
                return self._respond(200, self.worker.tombstone_artifact(
                    _id(parts[2]), self._artifact_project()))
            if parts == ["v2", "reservations"] and self.command == "POST":
                body = self._body()
                check(set(body) == {"deviceId", "projectId", "projectDigest",
                                    "applicationId", "buildId", "reservationId",
                                    "authorityDelegationId"},
                      "invalid_argument", "Invalid physical reservation", 400)
                return self._respond(201, self.worker.reserve_remote_device(body))
            if len(parts) == 3 and parts[:2] == ["v2", "reservations"] \
                    and self.command == "GET":
                return self._respond(200, self.worker.remote_reservation_status(_id(parts[2])))
            if len(parts) == 4 and parts[:2] == ["v2", "reservations"] \
                    and parts[3] == "release" and self.command == "POST":
                body = self._body()
                check(not body, "invalid_argument", "Reservation release body must be empty", 400)
                return self._respond(200, self.worker.release_remote_reservation(_id(parts[2])))
            if parts == ["v1", "authority", "exchange"]:
                if self.command == "GET":
                    return self._respond(200, self.worker.begin_authority_exchange())
                if self.command == "POST":
                    return self._respond(201, self.worker.finish_authority_exchange(self._body()))
            if parts == ["v1", "devices"] and self.command == "GET":
                return self._respond(200, {"protocolVersion": WORKER_PROTOCOL_VERSION,
                                           "codeVersion": WORKER_CODE_VERSION,
                                           "devices": self.worker.lab.list_devices()})
            if len(parts) == 2 and parts == ["v1", "sessions"] and self.command == "POST":
                body = self._body()
                device_id = _id(body.get("deviceId"))
                shared = "_authority" in self.worker.lab.devices.get(device_id, {})
                reserved = "reservationId" in body
                expected = ({"deviceId", "clientId", "reservationId"} if reserved else
                            {"deviceId", "clientId", "authorityDelegationId"} if shared else
                            {"deviceId", "clientId"})
                check(set(body) == expected, "invalid_argument", "Invalid session request", 400)
                grant = self.worker.consume_authority_delegation(
                    _id(body["authorityDelegationId"])
                ) if shared and not reserved else None
                reservation = (self.worker.remote_reservation_for_start(
                    _id(body["reservationId"]), device_id) if reserved else None)
                result = self.worker.lab.create_session(
                    device_id, OWNER, _id(body["clientId"]), authority_grant=grant,
                    _device_reservation=reservation,
                    _effect_authorizer=self.worker.enrolled_effect_authorizer(
                        grant.project_id if grant is not None else
                        self.worker.remote_reservation_project(body.get("reservationId"))),
                )
                if reserved:
                    self.worker.remote_reservation_started(body["reservationId"], result["id"])
                return self._respond(201, {"session": result})
            if len(parts) >= 3 and parts[:2] == ["v1", "sessions"]:
                sid = _id(parts[2])
                if len(parts) == 3 and self.command == "GET":
                    return self._respond(200, {"session": self.worker.lab.get_session(sid, OWNER)})
                if len(parts) == 4 and parts[3] == "frame" and self.command == "GET":
                    session = self.worker.lab._session(sid, OWNER)
                    self.worker.lab.get_session(sid, OWNER)
                    with session["frameLock"]:
                        check(session["frame"] is not None, "frame_pending", "No worker frame is available", 503)
                        frame = copy.deepcopy(session["frame"])
                    return self._respond(200, _frame_packet(frame), binary=True)
                if len(parts) == 4 and parts[3] == "observe" and self.command == "GET":
                    observe = getattr(self.worker.lab, "observe", None)
                    check(callable(observe), "unsupported_operation", "Observation is not supported", 400)
                    return self._respond(200, {"observation": observe(sid, OWNER)})
                if len(parts) == 4 and parts[3] in {"input", "close", "heartbeat"} and self.command == "POST":
                    body = self._body()
                    action = parts[3]
                    if action == "input":
                        result = self.worker.lab.input(sid, OWNER, body)
                        return self._respond(200, {"receipt": result, "session": self.worker.lab.get_session(sid, OWNER)})
                    if action == "close":
                        check(set(body) == {"controllerId", "epoch"}, "invalid_argument", "Invalid close request", 400)
                        result = self.worker.lab.close_session(sid, OWNER, _id(body["controllerId"]), body["epoch"])
                        return self._respond(200, {"session": result})
                    check(set(body) == {"clientId"}, "invalid_argument", "Invalid heartbeat request", 400)
                    return self._respond(200, {"session": self.worker.lab.heartbeat(sid, OWNER, _id(body["clientId"]))})
            raise LiveError("not_found", "Worker resource not found", 404)
        except Exception as exc:
            try:
                if getattr(self, "_response_started", False):
                    return
                code, message, status = _safe_error(exc)
                self._respond(status, {"error": {"code": code, "message": message}})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass


class WorkerServer:
    """Authenticated worker HTTP server for one local provider Lab."""

    def __init__(self, lab: Lab, token: str, host: str = "127.0.0.1", port: int = 0,
                 ssl_context: ssl.SSLContext | None = None, advertised_host: str | None = None,
                 host_authorizer=None, host_identity=None, artifact_store=None,
                 registered_projects=()):
        _require(isinstance(lab, Lab), "Invalid worker Lab")
        _require(isinstance(token, str) and 32 <= len(token) <= 512 and re.fullmatch(r"[A-Za-z0-9_-]+", token) is not None,
                 "Worker token must be at least 32 safe characters")
        _require(isinstance(host, str) and host and not any(ch in host for ch in "/\\"), "Invalid worker host")
        _require(type(port) is int and 0 <= port <= 65535, "Invalid worker port")
        wildcard = host in {"0.0.0.0", "::"}
        if wildcard:
            _require(isinstance(advertised_host, str) and advertised_host and not any(ch in advertised_host for ch in "/\\"),
                     "Wildcard workers require an advertised host")
        elif advertised_host is not None:
            _require(isinstance(advertised_host, str) and advertised_host and not any(ch in advertised_host for ch in "/\\"),
                     "Invalid advertised host")
        if not _is_loopback(host):
            _require(ssl_context is not None, "Non-loopback workers require TLS")
        _require(host_authorizer is None or callable(host_authorizer),
                 "Invalid enrolled host authorizer")
        _require((artifact_store is None and host_identity is None)
                 or (artifact_store is not None and host_identity is not None
                     and host_authorizer is not None),
                 "Artifact transfer requires enrolled host identity")
        if artifact_store is not None and lab._evidence_store is not None:
            recording_root = lab._evidence_store.root.resolve()
            transfer_root = artifact_store.evidence.root.resolve()
            _require(transfer_root != recording_root
                     and transfer_root not in recording_root.parents
                     and recording_root not in transfer_root.parents,
                     "Artifact transfer requires an isolated evidence namespace")
        self.lab = lab
        self.token = token
        self.host = host
        self.advertised_host = advertised_host or host
        self.ssl_context = ssl_context
        self.host_authorizer = host_authorizer
        self.host_identity = host_identity
        self.artifact_store = artifact_store
        self.registered_projects = {}
        for registration in registered_projects:
            try:
                project = registration.project
                key = (project["id"], registration.project_digest)
            except Exception:
                raise ContractError("Invalid trusted worker project registration") from None
            _require(key not in self.registered_projects,
                     "Duplicate worker project registration")
            self.registered_projects[key] = registration
        self.httpd = ThreadingHTTPServer((host, port), _WorkerHandler)
        self.httpd.daemon_threads = False
        self.httpd.worker = self  # type: ignore[attr-defined]
        if ssl_context is not None:
            self.httpd.socket = ssl_context.wrap_socket(self.httpd.socket, server_side=True)
        scheme = "https" if ssl_context is not None else "http"
        self.origin = f"{scheme}://{self.advertised_host}:{self.httpd.server_port}"
        self.origin_netloc = urlsplit(self.origin).netloc
        self._closed_operations = False
        self._closing_operations = False
        self._operations_close_lock = threading.RLock()
        self._artifact_maintenance_lock = threading.RLock()
        self._artifact_maintenance_stop = threading.Event()
        self._artifact_maintenance_thread = None
        self._artifact_maintenance = {"state": "disabled", "removed": 0,
                                      "pending": 0, "lastRunAtMs": None, "errorCode": None}
        self._authority_lock = threading.RLock()
        self._authority_exchanges: OrderedDict[str, tuple[float, Any, Any]] = OrderedDict()
        self._authority_delegations: OrderedDict[str, Any] = OrderedDict()
        self._remote_reservations: dict[str, dict[str, Any]] = {}
        self._output_lease = Lease("live-output:" + str(lab.output.resolve()))
        try:
            self._output_lease.__enter__()
        except Exception:
            self.httpd.server_close()
            raise
        self.lab.start_maintenance()
        if self.artifact_store is not None:
            self._artifact_maintenance_thread = threading.Thread(
                target=self._maintain_artifacts, name="reproloop-artifact-retention", daemon=True)
            self._artifact_maintenance_thread.start()

    @property
    def server_port(self):
        return self.httpd.server_port

    def serve_forever(self, *args, **kwargs):
        return self.httpd.serve_forever(*args, **kwargs)

    def begin_authority_exchange(self):
        authority = self.lab.authority
        _require(authority is not None, "Worker authority is unavailable")
        received = authority.clock_sync.sample()
        sent = authority.clock_sync.sample()
        identifier = "exchange_" + uuid.uuid4().hex
        with self._authority_lock:
            now = time.monotonic()
            self._authority_exchanges[identifier] = (now, received, sent)
            while len(self._authority_exchanges) > MAX_AUTHORITY_EXCHANGES:
                self._authority_exchanges.popitem(last=False)
        return {"protocolVersion": WORKER_PROTOCOL_VERSION,
                "codeVersion": WORKER_CODE_VERSION,
                "exchangeId": identifier}

    def authorize_enrolled_host(self, project_id=None):
        host_authorizer = getattr(self, "host_authorizer", None)
        if host_authorizer is None:
            return True
        try:
            allowed = host_authorizer(project_id)
        except LiveError:
            raise
        except Exception:
            raise LiveError("unauthorized", "Enrolled host authorization failed", 401) from None
        check(allowed is True, "unauthorized", "Enrolled host authorization failed", 401)
        return True

    def enrolled_effect_authorizer(self, project_id=None):
        if self.host_authorizer is None:
            return None

        def authorize(_kind):
            return self.authorize_enrolled_host(project_id)

        return authorize

    def _artifact_transfer(self):
        check(self.artifact_store is not None and self.host_identity is not None,
              "not_found", "Artifact transfer is unavailable", 404)
        return self.artifact_store

    def artifact_maintenance_status(self):
        self._artifact_transfer()
        with self._artifact_maintenance_lock:
            return dict(self._artifact_maintenance)

    def _maintain_artifacts(self):
        while not self._artifact_maintenance_stop.is_set():
            try:
                removed = len(self.artifact_store.apply_retention())
                pending = self.artifact_store.pending_retention_count()
                result = {"state": "pending" if pending else "idle", "removed": removed,
                          "pending": pending, "errorCode": "cleanup_pending" if pending else None}
            except Exception:
                result = {"state": "pending", "removed": 0, "pending": None,
                          "errorCode": "retention_unavailable"}
            with self._artifact_maintenance_lock:
                self._artifact_maintenance = dict(result, lastRunAtMs=int(time.time() * 1000))
            self._artifact_maintenance_stop.wait(ARTIFACT_RETENTION_INTERVAL)

    def allocate_artifact(self, body):
        from ..contracts import digest as contract_digest
        project_id = _id(body["projectId"])
        self.authorize_enrolled_host(project_id)
        matches = [registration for (identifier, _), registration in self.registered_projects.items()
                   if identifier == project_id]
        check(len(matches) == 1, "artifact_policy_required",
              "Artifact upload requires one trusted current project registration", 403)
        registration = self.lab._recording_store._require_registration(matches[0])
        project = registration.project
        policy = registration.collection_policy
        kind = body["kind"]
        if kind in {"video", "capture", "app-log"}:
            category = "logs" if kind == "app-log" else "pixels"
            check(project["evidencePolicy"][category] is True
                  and policy["captureMode"] == "test-data",
                  "capture_suppressed", "Project policy does not admit this artifact collection", 403)
        if kind == "application":
            check(any(build["artifactDigest"] == body["digest"] for build in project["builds"]),
                  "recording_identity", "Application artifact is not registered", 403)
        retention_class = body["retentionClass"]
        check(isinstance(retention_class, str)
              and retention_class in policy["retentionSeconds"],
              "invalid_retention", "Artifact retention class is not registered", 400)
        now = self._artifact_transfer()._now()
        check(type(body["retainUntilMs"]) is int
              and now < body["retainUntilMs"] <= now + policy["retentionSeconds"][retention_class] * 1000,
              "invalid_retention", "Artifact retention exceeds project policy", 400)
        return self._artifact_transfer().allocate(
            project_id=project_id, host_identity=self.host_identity,
            kind=body["kind"], size=body["size"], digest=body["digest"],
            metadata=body["metadata"], retention_class=body["retentionClass"],
            retain_until_ms=body["retainUntilMs"],
            authorizer=self.authorize_enrolled_host,
            project_digest=registration.project_digest,
            collection_policy_digest=contract_digest(policy))

    def _bound_artifact_transfer(self, object_id, project_id):
        transfer = self._artifact_transfer()
        row = transfer._row(object_id)
        transfer._check_host(row, self.host_identity)
        check(row["project_id"] == _id(project_id),
              "not_found", "Artifact object is unavailable", 404)
        self.authorize_enrolled_host(project_id)
        check(all(isinstance(row[field], str) and _DIGEST.fullmatch(row[field])
                  for field in ("project_digest", "collection_policy_digest")),
              "artifact_policy_required", "Artifact has no trusted collection policy binding", 403)
        return transfer

    def artifact_status(self, object_id, project_id=None):
        return self._bound_artifact_transfer(object_id, project_id).status(
            object_id, host_identity=self.host_identity,
            authorizer=self.authorize_enrolled_host,
            expected_project_id=project_id)

    def put_artifact_chunk(self, object_id, upload_generation, offset, body, digest,
                           project_id):
        return self._bound_artifact_transfer(object_id, project_id).put_chunk(
            object_id, upload_generation, offset, body, digest,
            host_identity=self.host_identity,
            authorizer=self.authorize_enrolled_host,
            expected_project_id=project_id)

    def finalize_artifact(self, object_id, upload_generation, project_id):
        return self._bound_artifact_transfer(object_id, project_id).finalize(
            object_id, upload_generation, host_identity=self.host_identity,
            authorizer=self.authorize_enrolled_host,
            expected_project_id=project_id)

    def open_artifact_read(self, object_id, *, start=0, end=None, project_id=None):
        return self._bound_artifact_transfer(object_id, project_id).open_read(
            object_id, host_identity=self.host_identity, start=start, end=end,
            authorizer=self.authorize_enrolled_host,
            expected_project_id=project_id)

    def tombstone_artifact(self, object_id, project_id):
        return self._bound_artifact_transfer(object_id, project_id).tombstone(
            object_id, host_identity=self.host_identity,
            authorizer=self.authorize_enrolled_host,
            expected_project_id=project_id)

    def revalidate_artifact_read(self, value):
        return self._artifact_transfer().revalidate_read(
            value, host_identity=self.host_identity,
            authorizer=self.authorize_enrolled_host)

    def reserve_remote_device(self, body):
        device_id = _id(body["deviceId"]);project_id = _id(body["projectId"])
        project_digest = body["projectDigest"]
        _require(isinstance(project_digest, str) and _DIGEST.fullmatch(project_digest),
                 "Invalid project digest")
        application_id = _id(body["applicationId"]);build_id = _id(body["buildId"])
        reservation_id = _id(body["reservationId"])
        self.authorize_enrolled_host(project_id)
        registration = self.registered_projects.get((project_id, project_digest))
        check(registration is not None, "recording_identity",
              "Worker has no trusted matching project registration", 409)
        grant = self.consume_authority_delegation(_id(body["authorityDelegationId"]))
        check(grant.project_id == project_id, "recording_identity",
              "Delegated authority project differs", 409)
        with self._authority_lock:
            check(reservation_id not in self._remote_reservations,
                  "invalid_argument", "Reservation identity is already in use", 400)
        capability = self.lab.reserve_release_device(
            device_id, OWNER, reservation_id, registration,
            application_id=application_id, build_id=build_id,
            authority_grant=grant)
        entry = {"capability": capability, "projectId": project_id,
                 "deviceId": device_id, "state": "reserved", "sessionId": None}
        with self._authority_lock:
            self._remote_reservations[reservation_id] = entry
        return self.remote_reservation_status(reservation_id)

    def _remote_reservation(self, reservation_id):
        reservation_id = _id(reservation_id)
        with self._authority_lock:
            value = self._remote_reservations.get(reservation_id)
        check(value is not None, "not_found", "Physical reservation is unavailable", 404)
        return value

    def remote_reservation_project(self, reservation_id):
        return None if reservation_id is None else self._remote_reservation(reservation_id)["projectId"]

    def remote_reservation_status(self, reservation_id):
        value = self._remote_reservation(reservation_id)
        self.authorize_enrolled_host(value["projectId"])
        capability = value["capability"]
        authority = capability._authority_handle
        state = value["state"]
        if state == "reserved":
            try:
                authority.check_ownership()
            except Exception:
                state = "uncertain"
        return {"reservationId": capability.reservation_id,
                "deviceId": capability.device_id, "projectId": value["projectId"],
                "projectDigest": capability.project_digest,
                "applicationId": capability.application_id, "buildId": capability.build_id,
                "state": state, "ownershipGeneration": authority.generation,
                "hostAuthorityIncarnation": authority._authority.host_incarnation,
                "helperIncarnation": authority.helper_incarnation,
                "sessionId": value["sessionId"]}

    def remote_reservation_for_start(self, reservation_id, device_id):
        value = self._remote_reservation(reservation_id)
        check(value["state"] == "reserved" and value["deviceId"] == device_id,
              "stale_controller", "Physical reservation is stale", 409)
        self.authorize_enrolled_host(value["projectId"])
        value["capability"]._authority_handle.check_ownership()
        return value["capability"]

    def remote_reservation_started(self, reservation_id, session_id):
        value = self._remote_reservation(reservation_id)
        with self._authority_lock:
            value["state"] = "transferred";value["sessionId"] = _id(session_id)

    def release_remote_reservation(self, reservation_id):
        value = self._remote_reservation(reservation_id)
        check(value["state"] == "reserved", "cleanup_uncertain",
              "Transferred reservation must be closed through its session", 409)
        result = self.lab.release_device_reservation(value["capability"])
        with self._authority_lock:
            value["state"] = "released"
        return {"reservationId": reservation_id, "state": "released",
                "deviceId": result["deviceId"]}

    def finish_authority_exchange(self, value):
        expected = {
            "protocolVersion", "codeVersion", "exchangeId", "originGrantFingerprint",
            "coordinatorClockId", "coordinatorSendNs", "coordinatorReceiveNs",
            "coordinatorUncertaintyNs", "maxDriftPpm", "projectId", "controllerId",
            "renewalSequence", "coordinatorDeadlineNs",
        }
        _require(isinstance(value, dict) and set(value) == expected,
                 "Invalid authority exchange")
        _require(value["protocolVersion"] == WORKER_PROTOCOL_VERSION
                 and type(value["protocolVersion"]) is int
                 and value["codeVersion"] == WORKER_CODE_VERSION
                 and type(value["codeVersion"]) is int,
                 "Incompatible worker authority protocol")
        exchange_id = _id(value["exchangeId"])
        _require(type(value["originGrantFingerprint"]) is str
                 and _DIGEST.fullmatch(value["originGrantFingerprint"]) is not None,
                 "Invalid authority exchange")
        with self._authority_lock:
            exchange = self._authority_exchanges.pop(exchange_id, None)
        _require(exchange is not None and time.monotonic() - exchange[0] <= AUTHORITY_EXCHANGE_SECONDS,
                 "Authority exchange expired")
        project_id = _id(value["projectId"])
        self.authorize_enrolled_host(project_id)
        authority = self.lab.authority
        _require(authority is not None, "Worker authority is unavailable")
        mapping = authority.clock_sync.record_exchange(
            coordinator_clock_id=value["coordinatorClockId"],
            coordinator_send_ns=value["coordinatorSendNs"],
            host_received=exchange[1], host_sent=exchange[2],
            coordinator_receive_ns=value["coordinatorReceiveNs"],
            coordinator_uncertainty_ns=value["coordinatorUncertaintyNs"],
            max_drift_ppm=value["maxDriftPpm"],
        )
        remote_grant_id = "remote_" + hashlib.sha256(
            (value["originGrantFingerprint"] + "\0" + exchange_id).encode("ascii")
        ).hexdigest()[:40]
        grant = authority.issue_parent_grant(
            mapping, grant_id=remote_grant_id,
            project_id=project_id,
            controller_id=_id(value["controllerId"]),
            renewal_sequence=value["renewalSequence"],
            coordinator_deadline_ns=value["coordinatorDeadlineNs"],
        )
        delegation_id = "delegation_" + uuid.uuid4().hex
        with self._authority_lock:
            self._authority_delegations[delegation_id] = grant
            while len(self._authority_delegations) > MAX_AUTHORITY_EXCHANGES:
                self._authority_delegations.popitem(last=False)
        return {"protocolVersion": WORKER_PROTOCOL_VERSION,
                "codeVersion": WORKER_CODE_VERSION,
                "authorityDelegationId": delegation_id}

    def consume_authority_delegation(self, identifier):
        with self._authority_lock:
            grant = self._authority_delegations.pop(identifier, None)
        _require(grant is not None, "Authority delegation is unavailable")
        self.authorize_enrolled_host(grant.project_id)
        return grant

    def shutdown(self):
        return self.httpd.shutdown()

    def close_operations(self):
        with self._operations_close_lock:
            if self._closed_operations:
                return
            self._closing_operations = True
            self._artifact_maintenance_stop.set()
            if self._artifact_maintenance_thread is not None:
                self._artifact_maintenance_thread.join(timeout=10)
                check(not self._artifact_maintenance_thread.is_alive(),
                      "cleanup_uncertain", "Artifact retention has not stopped", 503)
            if self.artifact_store is not None:
                self.artifact_store.close()
            self.lab.close_all()
            self._closed_operations = True

    def server_close(self):
        self.close_operations()
        try:
            self.httpd.server_close()
        finally:
            if self._output_lease is not None:
                self._output_lease.__exit__(None, None, None)
                self._output_lease = None

    def close(self):
        self.close_operations()
        self.shutdown()
        self.server_close()


class WorkerClient:
    """Direct, no-proxy HTTP(S) client for a WorkerServer."""

    def __init__(self, url: str, token: str, ca_file: str | Path | None = None):
        parsed = urlsplit(url)
        _require(parsed.scheme in {"http", "https"} and parsed.hostname is not None and parsed.port is not None
                 and not parsed.username and not parsed.password and parsed.path in {"", "/"}
                 and not parsed.query and not parsed.fragment, "Invalid worker URL")
        loopback = _is_loopback(parsed.hostname)
        _require(loopback or parsed.scheme == "https", "Non-loopback workers require verified HTTPS")
        _require(isinstance(token, str) and 32 <= len(token) <= 512 and re.fullmatch(r"[A-Za-z0-9_-]+", token) is not None,
                 "Worker token must be at least 32 safe characters")
        if ca_file is not None:
            _require(parsed.scheme == "https", "A CA file requires HTTPS")
        self.url = url.rstrip("/")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port
        self.token = token
        self._context = ssl.create_default_context(cafile=str(ca_file) if ca_file is not None else None) if parsed.scheme == "https" else None
        self._artifact_projects: dict[str, str] = {}

    def _request(self, method: str, path: str, body: Any = None, *, timeout: float = 10,
                 binary: bool = False, extra_headers: dict[str, str] | None = None) -> Any:
        _require(isinstance(path, str) and path.startswith(("/v1/", "/v2/"))
                 and "?" not in path and "#" not in path and "//" not in path, "Invalid worker path")
        parts = path.strip("/").split("/")
        if parts[0] == "v1":
            _require(parts[:2] in (["v1", "devices"], ["v1", "sessions"],
                                   ["v1", "authority"]), "Invalid worker path")
            if parts in (["v1", "devices"], ["v1", "sessions"], ["v1", "authority", "exchange"]):
                pass
            elif len(parts) == 3 and parts[1] == "sessions":
                _id(parts[2])
            elif len(parts) == 4 and parts[1] == "sessions" and parts[3] in {"frame", "input", "close", "heartbeat", "observe"}:
                _id(parts[2])
            else:
                _require(False, "Invalid worker path")
        else:
            valid = parts in (["v2", "artifacts", "uploads"], ["v2", "reservations"])
            if len(parts) == 3 and parts[:2] == ["v2", "artifacts"]:
                _id(parts[2]);valid = True
            elif len(parts) == 4 and parts[:3] == ["v2", "artifacts", "uploads"]:
                _id(parts[3]);valid = True
            elif len(parts) == 5 and parts[:3] == ["v2", "artifacts", "uploads"] and parts[4] == "finalize":
                _id(parts[3]);valid = True
            elif len(parts) == 6 and parts[:3] == ["v2", "artifacts", "uploads"] and parts[4] == "chunks":
                _id(parts[3]);_require(re.fullmatch(r"0|[1-9][0-9]*", parts[5]) is not None,
                                       "Invalid worker path");valid = True
            elif len(parts) == 3 and parts[:2] == ["v2", "reservations"]:
                _id(parts[2]);valid = True
            elif len(parts) == 4 and parts[:2] == ["v2", "reservations"] and parts[3] == "release":
                _id(parts[2]);valid = True
            _require(valid, "Invalid worker path")
        _require(type(timeout) in (int, float) and not isinstance(timeout, bool) and 5 <= timeout <= 35,
                 "Invalid worker timeout")
        raw_payload = isinstance(body, bytes)
        payload = (body if raw_payload else None if body is None else
                   json.dumps(body, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode())
        _require(payload is None or len(payload) <= MAX_BODY, "Worker request is too large")
        headers = {"Authorization": "Bearer " + self.token, "Connection": "close"}
        if payload is not None:
            headers["Content-Type"] = "application/octet-stream" if raw_payload else "application/json"
        for name, value in (extra_headers or {}).items():
            _require(name in {"Range", "X-Repro-Upload-Generation", "X-Repro-Chunk-SHA256",
                              "X-Repro-Project-Id"}
                     and isinstance(value, str) and len(value) <= 128,
                     "Invalid worker header")
            headers[name] = value
        connection = (http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=self._context)
                      if self.scheme == "https" else http.client.HTTPConnection(self.host, self.port, timeout=timeout))
        timer = None
        active_socket = {"value": None}
        try:
            def abort_request():
                sock = active_socket["value"] or connection.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            timer = threading.Timer(timeout, abort_request)
            timer.daemon = True
            timer.start()
            connection.request(method, path, payload, headers)
            active_socket["value"] = connection.sock
            response = connection.getresponse()
            response_headers = getattr(response, "headers", None)
            expected_length = None
            if response_headers is not None:
                check(not response_headers.get_all("Transfer-Encoding", []),
                      "worker_error", "Worker returned invalid framing", 502)
                check(len(response_headers.get_all("Content-Length", [])) == 1
                      and len(response_headers.get_all("Content-Type", [])) == 1,
                      "worker_error", "Worker returned ambiguous headers", 502)
                length_value = response_headers.get("Content-Length", "")
                check(length_value.isdigit() and int(length_value) <= MAX_RESPONSE,
                      "worker_error", "Worker returned an invalid length", 502)
                expected_length = int(length_value)
            blocks = []
            total = 0
            if not hasattr(response, "read1"):
                blocks.append(response.read(MAX_RESPONSE + 1))
                total = len(blocks[0])
            else:
                while True:
                    block = response.read1(min(64 * 1024, MAX_RESPONSE + 1 - total))
                    if not block:
                        break
                    blocks.append(block)
                    total += len(block)
                    check(total <= MAX_RESPONSE, "response_limit", "Worker response is too large")
            raw = b"".join(blocks)
            check(len(raw) <= MAX_RESPONSE, "response_limit", "Worker response is too large")
            if expected_length is not None:
                check(len(raw) == expected_length,
                      "transport_unavailable", "Worker response was truncated", 503)
            if response.status not in {200, 201, 202, 206}:
                code = "worker_error"
                try:
                    problem = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant).get("error", {})
                    if isinstance(problem, dict) and isinstance(problem.get("code"), str) and _SAFE_ERROR.fullmatch(problem["code"]):
                        code = problem["code"]
                except (ValueError, TypeError, AttributeError):
                    pass
                raise LiveError(code, "Worker operation failed", response.status)
            if binary:
                if parts[:2] == ["v2", "artifacts"]:
                    check(response_headers is not None
                          and response_headers.get("Content-Type") == "application/octet-stream",
                          "worker_error", "Worker returned invalid artifact media", 502)
                    ranges = response_headers.get_all("Content-Range", [])
                    requested = headers.get("Range")
                    if requested is None:
                        check(response.status == 200 and not ranges,
                              "worker_error", "Worker returned an unexpected artifact range", 502)
                    else:
                        wanted = re.fullmatch(r"bytes=(0|[1-9][0-9]{0,18})-(0|[1-9][0-9]{0,18})", requested)
                        actual = (re.fullmatch(
                            r"bytes (0|[1-9][0-9]{0,18})-(0|[1-9][0-9]{0,18})/([1-9][0-9]{0,18})",
                            ranges[0]) if len(ranges) == 1 else None)
                        check(response.status == 206 and wanted is not None and actual is not None,
                              "worker_error", "Worker returned an invalid artifact range", 502)
                        start, end, size = map(int, actual.groups())
                        check((start, end) == tuple(map(int, wanted.groups()))
                              and 0 <= start <= end < size <= 2 ** 63 - 1
                              and len(raw) == end - start + 1,
                              "worker_error", "Worker returned another artifact range", 502)
                return raw
            try:
                value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
            except (ValueError, TypeError, UnicodeError):
                raise LiveError("worker_error", "Worker returned invalid JSON", 502) from None
            check(isinstance(value, dict), "worker_error", "Worker returned invalid JSON", 502)
            return value
        except LiveError:
            raise
        except (OSError, http.client.HTTPException, TimeoutError):
            raise LiveError("transport_unavailable", "Worker transport unavailable", 503) from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()

    def call(self, path: str, body: Any = None, *, timeout: float = 10) -> dict[str, Any]:
        return self._request("GET" if body is None else "POST", path, body, timeout=timeout)

    def frame(self, session_id: str) -> tuple[dict[str, Any], bytes]:
        _id(session_id)
        return _decode_frame(self._request("GET", f"/v1/sessions/{quote(session_id, safe='')}/frame", binary=True))

    def observe(self, session_id: str) -> Any:
        _id(session_id)
        response = self.call(f"/v1/sessions/{quote(session_id, safe='')}/observe")
        return response.get("observation")

    def allocate_artifact(self, *, project_id: str, kind: str, size: int,
                          digest: str, metadata: dict[str, Any],
                          retention_class: str, retain_until_ms: int) -> dict[str, Any]:
        value = self._request("POST", "/v2/artifacts/uploads", {
            "projectId": project_id, "kind": kind, "size": size,
            "digest": digest, "metadata": metadata,
            "retentionClass": retention_class, "retainUntilMs": retain_until_ms,
        })
        self._artifact_projects[_id(value.get("objectId"))] = _id(project_id)
        return value

    def _artifact_project(self, object_id, project_id=None):
        selected = project_id if project_id is not None else self._artifact_projects.get(object_id)
        return _id(selected)

    def artifact_status(self, object_id: str, *, project_id=None) -> dict[str, Any]:
        object_id = _id(object_id)
        project_id = self._artifact_project(object_id, project_id)
        return self._request(
            "GET", f"/v2/artifacts/uploads/{quote(object_id, safe='')}",
            extra_headers={"X-Repro-Project-Id": project_id})

    def upload_artifact_chunk(self, object_id: str, upload_generation: int,
                              offset: int, body: bytes, *, project_id=None) -> dict[str, Any]:
        object_id = _id(object_id)
        project_id = self._artifact_project(object_id, project_id)
        _require(type(upload_generation) is int and upload_generation > 0
                 and type(offset) is int and offset >= 0
                 and type(body) is bytes and bool(body), "Invalid artifact chunk")
        return self._request(
            "PUT", f"/v2/artifacts/uploads/{quote(object_id, safe='')}/chunks/{offset}",
            body, extra_headers={
                "X-Repro-Upload-Generation": str(upload_generation),
                "X-Repro-Chunk-SHA256": hashlib.sha256(body).hexdigest(),
                "X-Repro-Project-Id": project_id,
            })

    def finalize_artifact(self, object_id: str, upload_generation: int, *,
                          project_id=None) -> dict[str, Any]:
        object_id = _id(object_id)
        project_id = self._artifact_project(object_id, project_id)
        _require(type(upload_generation) is int and upload_generation > 0,
                 "Invalid upload generation")
        return self._request(
            "POST", f"/v2/artifacts/uploads/{quote(object_id, safe='')}/finalize",
            {"uploadGeneration": upload_generation},
            extra_headers={"X-Repro-Project-Id": project_id})

    def download_artifact(self, object_id: str, *, start: int = 0,
                          end: int | None = None, project_id=None) -> bytes:
        object_id = _id(object_id)
        project_id = self._artifact_project(object_id, project_id)
        _require(type(start) is int and start >= 0
                 and (end is None or type(end) is int and end > start),
                 "Invalid artifact range")
        headers = None
        if start or end is not None:
            _require(end is not None and end - start <= MAX_RESPONSE,
                     "Artifact range is too large")
            headers = {"Range": f"bytes={start}-{end - 1}"}
        headers = dict(headers or {}, **{"X-Repro-Project-Id": project_id})
        return self._request(
            "GET", f"/v2/artifacts/{quote(object_id, safe='')}", binary=True,
            extra_headers=headers)

    def tombstone_artifact(self, object_id, *, project_id=None):
        object_id = _id(object_id)
        project_id = self._artifact_project(object_id, project_id)
        value = self._request(
            "DELETE", f"/v2/artifacts/{quote(object_id, safe='')}",
            extra_headers={"X-Repro-Project-Id": project_id})
        self._artifact_projects.pop(object_id, None)
        return value

    def upload_artifact_bytes(self, body: bytes, *, project_id: str, kind: str,
                              metadata: dict[str, Any], retention_class: str,
                              retain_until_ms: int, chunk_bytes: int = 256 * 1024):
        _require(type(body) is bytes and bool(body)
                 and type(chunk_bytes) is int and 1 <= chunk_bytes <= 1024 * 1024,
                 "Invalid artifact upload")
        upload = self.allocate_artifact(
            project_id=project_id, kind=kind, size=len(body),
            digest=hashlib.sha256(body).hexdigest(), metadata=metadata,
            retention_class=retention_class, retain_until_ms=retain_until_ms)
        object_id = upload["objectId"];generation = upload["uploadGeneration"]
        for offset in range(0, len(body), chunk_bytes):
            chunk = body[offset:offset + chunk_bytes]
            try:
                self.upload_artifact_chunk(object_id, generation, offset, chunk)
            except LiveError as error:
                if error.code != "transport_unavailable":
                    raise
                status = self.artifact_status(object_id)
                interval = [offset, offset + len(chunk)]
                if interval not in status.get("receivedRanges", []):
                    self.upload_artifact_chunk(object_id, generation, offset, chunk)
        try:
            return self.finalize_artifact(object_id, generation)
        except LiveError as error:
            if error.code != "transport_unavailable":
                raise
            status = self.artifact_status(object_id)
            if status.get("state") == "published":
                return status
            return self.finalize_artifact(object_id, generation)

    def reserve_device(self, value):
        return self._request("POST", "/v2/reservations", value, timeout=35)

    def reservation_status(self, reservation_id):
        reservation_id = _id(reservation_id)
        return self._request("GET", f"/v2/reservations/{quote(reservation_id, safe='')}")

    def release_reservation(self, reservation_id):
        reservation_id = _id(reservation_id)
        return self._request(
            "POST", f"/v2/reservations/{quote(reservation_id, safe='')}/release", {})


class RemoteReservationHandle:
    """Coordinator-side capability for a reservation held on the worker."""

    def __init__(self, client, remote_device_id, lab, *, registration,
                 application_id, build_id, reservation_id, authority_grant):
        self.client = client
        self.remote_device_id = _id(remote_device_id)
        self.reservation_id = _id(reservation_id)
        self.origin_authority = lab.authority
        self.origin_grant = authority_grant
        delegate = RemoteProvider(client, remote_device_id, authority_mode="shared-v2")
        delegation_id = delegate._delegate_authority(lab, authority_grant=authority_grant)
        project = registration.project
        response = client.reserve_device({
            "deviceId": self.remote_device_id, "projectId": project["id"],
            "projectDigest": registration.project_digest,
            "applicationId": application_id, "buildId": build_id,
            "reservationId": self.reservation_id,
            "authorityDelegationId": delegation_id,
        })
        expected = {"reservationId", "deviceId", "projectId", "projectDigest",
                    "applicationId", "buildId", "state", "ownershipGeneration",
                    "hostAuthorityIncarnation", "helperIncarnation", "sessionId"}
        _require(set(response) == expected and response["state"] == "reserved"
                 and response["reservationId"] == self.reservation_id
                 and response["deviceId"] == self.remote_device_id
                 and response["projectId"] == project["id"]
                 and response["projectDigest"] == registration.project_digest
                 and response["applicationId"] == application_id
                 and response["buildId"] == build_id
                 and type(response["ownershipGeneration"]) is int
                 and response["ownershipGeneration"] > 0,
                 "Worker returned an invalid physical reservation")
        self.generation = response["ownershipGeneration"]
        self.host_incarnation = _id(response["hostAuthorityIncarnation"])
        self.helper_incarnation = _id(response["helperIncarnation"])
        self._state = "reserved"

    @property
    def status(self):
        if self._state == "released":
            return "released"
        response = self.client.reservation_status(self.reservation_id)
        if response.get("ownershipGeneration") != self.generation \
                or response.get("hostAuthorityIncarnation") != self.host_incarnation \
                or response.get("helperIncarnation") != self.helper_incarnation:
            raise LiveError("stale_controller", "Physical reservation identity changed", 409)
        state = response.get("state")
        if state not in {"reserved", "transferred"}:
            raise LiveError("cleanup_uncertain", "Physical reservation is uncertain", 409)
        return "owned"

    def check_ownership(self):
        check(self.status == "owned", "stale_controller",
              "Physical reservation is stale", 409)
        return True

    def transfer(self):
        check(self._state == "reserved", "stale_controller",
              "Physical reservation was already transferred", 409)
        self.check_ownership()
        return self.reservation_id

    def mark_transferred(self):
        self._state = "transferred"

    def mark_released(self):
        self._state = "released"

    def close(self):
        if self._state == "released":
            return True
        if self._state == "transferred":
            return False
        response = self.client.release_reservation(self.reservation_id)
        if response.get("state") != "released":
            return False
        self._state = "released"
        return True


def remote_devices(client: WorkerClient, worker_id: str) -> list[dict[str, Any]]:
    worker_id = _id(worker_id)
    response = client.call("/v1/devices")
    _require(set(response) == {"protocolVersion", "codeVersion", "devices"}
             and type(response["protocolVersion"]) is int
             and type(response["codeVersion"]) is int
             and response["protocolVersion"] == WORKER_PROTOCOL_VERSION
             and response["codeVersion"] == WORKER_CODE_VERSION
             and isinstance(response["devices"], list), "Incompatible worker protocol")
    result = []
    for remote in response["devices"]:
        _require(isinstance(remote, dict), "Invalid worker device registry")
        remote_id = _id(remote.get("id"))
        stable_id = f"{worker_id}--{remote_id}"
        _require(_ID.fullmatch(stable_id) is not None, "Worker device identity is too long")
        remote_kind = remote.get("kind", "device")
        _require(isinstance(remote_kind, str) and _ID.fullmatch(remote_kind) is not None,
                 "Invalid worker device kind")
        capabilities = copy.deepcopy(remote.get("capabilities", {}))
        _require(isinstance(capabilities, dict), "Invalid worker capabilities")
        client_ref = client
        authority_mode = capabilities.get("authorityMode", "legacy-offline-v1")
        _require(authority_mode in {"shared-v2", "legacy-offline-v1"},
                 "Invalid worker authority mode")
        descriptor = {
            "id": stable_id,
            "name": f"{worker_id} · {remote.get('name', remote_id)}",
            "platform": remote.get("platform", "remote"),
            "kind": "remote-" + remote_kind,
            "workerId": worker_id,
            "capabilities": capabilities,
            "factory": lambda remote_id=remote_id, client_ref=client_ref, mode=authority_mode:
                RemoteProvider(client_ref, remote_id, authority_mode=mode),
        }
        if authority_mode == "shared-v2":
            descriptor["_remoteAuthority"] = True
            descriptor["_remoteReservation"] = (
                lambda lab, *, registration, application_id, build_id,
                reservation_id, authority_grant, remote_id=remote_id,
                client_ref=client_ref: RemoteReservationHandle(
                    client_ref, remote_id, lab, registration=registration,
                    application_id=application_id, build_id=build_id,
                    reservation_id=reservation_id,
                    authority_grant=authority_grant))
        result.append(descriptor)
    return result


class RemoteProvider:
    """Parent-Lab provider backed by one worker session."""

    def __init__(self, client: WorkerClient, remote_device_id: str,
                 *, authority_mode: str = "legacy-offline-v1"):
        _require(isinstance(client, WorkerClient), "Invalid worker client")
        self.client = client
        self.remote_device_id = _id(remote_device_id)
        _require(authority_mode in {"shared-v2", "legacy-offline-v1"},
                 "Invalid worker authority mode")
        self.authority_mode = authority_mode
        self.remote_session_id = None
        self.remote_controller = None
        self.remote_epoch = None
        self.parent_sid = None
        self.lab = None
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.frame_map: OrderedDict[int, tuple[int, int]] = OrderedDict()
        self.frame_ready = threading.Condition(self.lock)
        self.last_remote_frame = 0
        self.last_remote_acquisition = 0
        self.sequence = 0
        self.thread = None
        self.origin_authority = None
        self.origin_grant = None
        self.remote_reservation = None

    def _origin_current(self):
        if getattr(self, "authority_mode", "legacy-offline-v1") != "shared-v2":
            return
        try:
            self.origin_authority._require_parent_grant(self.origin_grant)
        except Exception:
            raise LiveError("authority_rejected", "Originating authority expired", 409) from None

    def _delegate_authority(self, lab, authority_grant=None):
        selected_grant = authority_grant if authority_grant is not None else lab.parent_grant
        _require(lab.authority is not None and selected_grant is not None,
                 "Shared worker devices require an originating authority")
        self.origin_authority = lab.authority
        self.origin_grant = selected_grant
        self._origin_current()
        sent = lab.authority.clock_sync.sample()
        opened = self.client.call("/v1/authority/exchange")
        received = lab.authority.clock_sync.sample()
        _require(set(opened) == {"protocolVersion", "codeVersion", "exchangeId"}
                 and type(opened["protocolVersion"]) is int
                 and type(opened["codeVersion"]) is int
                 and opened["protocolVersion"] == WORKER_PROTOCOL_VERSION
                 and opened["codeVersion"] == WORKER_CODE_VERSION,
                 "Incompatible worker authority protocol")
        grant = selected_grant
        _require(sent.clock_id == received.clock_id
                 and sent.boot_digest == received.boot_digest,
                 "Originating authority clock changed")
        body = {
            "protocolVersion": WORKER_PROTOCOL_VERSION,
            "codeVersion": WORKER_CODE_VERSION,
            "exchangeId": _id(opened["exchangeId"]),
            "originGrantFingerprint": grant.grant_fingerprint,
            "coordinatorClockId": sent.clock_id,
            "coordinatorSendNs": sent.nanoseconds,
            "coordinatorReceiveNs": received.nanoseconds,
            "coordinatorUncertaintyNs": max(sent.uncertainty_ns, received.uncertainty_ns),
            "maxDriftPpm": grant._mapping.max_drift_ppm,
            "projectId": grant.project_id,
            "controllerId": grant.controller_id,
            "renewalSequence": grant.renewal_sequence,
            "coordinatorDeadlineNs": grant.local_deadline_ns,
        }
        delegated = self.client.call("/v1/authority/exchange", body)
        _require(set(delegated) == {"protocolVersion", "codeVersion", "authorityDelegationId"}
                 and type(delegated["protocolVersion"]) is int
                 and type(delegated["codeVersion"]) is int
                 and delegated["protocolVersion"] == WORKER_PROTOCOL_VERSION
                 and delegated["codeVersion"] == WORKER_CODE_VERSION,
                 "Incompatible worker authority protocol")
        self._origin_current()
        return _id(delegated["authorityDelegationId"])

    def bind_remote_reservation(self, reservation):
        _require(type(reservation) is RemoteReservationHandle
                 and reservation.client is self.client
                 and reservation.remote_device_id == self.remote_device_id,
                 "Invalid remote physical reservation")
        reservation.check_ownership()
        self.remote_reservation = reservation
        self.origin_authority = reservation.origin_authority
        self.origin_grant = reservation.origin_grant

    def start(self, session, lab):
        self.parent_sid = session["id"]
        self.lab = lab
        self.remote_controller = "remote-" + uuid.uuid4().hex
        body = {"deviceId": self.remote_device_id, "clientId": self.remote_controller}
        if self.remote_reservation is not None:
            body["reservationId"] = self.remote_reservation.transfer()
        elif self.authority_mode == "shared-v2":
            body["authorityDelegationId"] = self._delegate_authority(
                lab, authority_grant=session.get("_authorityGrant"))
        response = self.client.call("/v1/sessions", body, timeout=35)
        remote = response.get("session") if isinstance(response, dict) else None
        _require(isinstance(remote, dict), "Worker returned an invalid session")
        self.remote_session_id = _id(remote.get("id"))
        self.remote_epoch = remote.get("epoch")
        _require(type(self.remote_epoch) is int and self.remote_epoch > 0, "Worker returned an invalid session epoch")
        if self.remote_reservation is not None:
            self.remote_reservation.mark_transferred()
        # Preserve the acknowledged ownership before checking an authority
        # that may have expired during I/O. Cleanup still needs these IDs.
        self._origin_current()
        self.thread = threading.Thread(target=self._poll, name="reproloop-remote-provider", daemon=True)
        self.thread.start()

    def _poll(self):
        startup_deadline = time.monotonic() + 95
        while not self.stop.wait(.05):
            try:
                remote = self.client.call(f"/v1/sessions/{quote(self.remote_session_id, safe='')}", timeout=5)
                remote = remote.get("session")
                if not isinstance(remote, dict) or remote.get("controllerId") != self.remote_controller:
                    raise LiveError("stale_controller", "Worker control changed", 409)
                with self.lock:
                    if remote.get("epoch") != self.remote_epoch:
                        raise LiveError("stale_controller", "Worker control epoch changed", 409)
                if remote.get("state") in {"failed", "closed", "draining"}:
                    raise LiveError("session_inactive", "Worker session stopped", 409)
                if remote.get("state") != "active":
                    if time.monotonic() >= startup_deadline:
                        raise LiveError("startup_timeout", "Worker session did not become active", 504)
                    continue
                metadata, image = self.client.frame(self.remote_session_id)
                remote_frame_id = metadata["id"]
                with self.lock:
                    if remote_frame_id <= self.last_remote_frame:
                        continue
                    acquisition_sequence = metadata.get('acquisitionSequence', remote_frame_id)
                    _require(type(acquisition_sequence) is int
                             and acquisition_sequence > self.last_remote_acquisition,
                             'Worker frame acquisition sequence changed')
                    native_gap = ((self.last_remote_acquisition + 1, acquisition_sequence - 1)
                                  if acquisition_sequence > self.last_remote_acquisition + 1 else None)
                    parent = self.lab._session(self.parent_sid)
                    published=self.lab.publish_frame(self.parent_sid, image, metadata["mime"], metadata["width"], metadata["height"],
                                                     metadata["orientation"], metadata["capturedAt"],
                                                     acquisition_sequence=acquisition_sequence,
                                                     native_sequence_gap=native_gap,
                                                     timing_source="native-unmapped")
                    self.last_remote_acquisition = acquisition_sequence
                    if not published:
                        self.last_remote_frame=remote_frame_id
                        continue
                    with parent["frameLock"]:
                        current = parent.get("frame")
                        check(isinstance(current, dict), "frame_pending", "Parent frame was not published", 503)
                        parent_frame_id = current["id"]
                    self.frame_map[parent_frame_id] = (remote_frame_id, metadata["geometryVersion"])
                    while len(self.frame_map) > 60:
                        self.frame_map.popitem(last=False)
                    self.frame_ready.notify_all()
                    self.last_remote_frame = remote_frame_id
            except Exception as exc:
                if not self.stop.is_set():
                    self.lab.fail(self.parent_sid, "Remote worker transport or session failed")
                return

    def _mapping(self, frame_id: int | None = None):
        with self.frame_ready:
            deadline = time.monotonic() + 5
            while not self.frame_map:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.frame_ready.wait(timeout=remaining)
            if frame_id is not None:
                if frame_id in self.frame_map:
                    return self.frame_map[frame_id]
                raise LiveError("stale_frame", "Remote frame mapping is no longer available", 409)
            if self.frame_map:
                return next(reversed(self.frame_map.values()))
        raise LiveError("stale_frame", "Remote frame mapping is no longer available", 409)

    def execute_with_frame(self, action, payload, frame):
        if action == "pointer" and payload.get("phase") == "cancel":
            return self._execute(action, payload, None)
        return self._execute(action, payload, self._mapping(frame["frameId"]))

    def execute(self, action, payload):
        return self._execute(action, payload, None, uuid.uuid4().hex)

    def execute_operation(self, action, payload, *, operation_id, frame=None):
        operation_id = _id(operation_id)
        mapping = (None if frame is None or (action == "pointer" and payload.get("phase") == "cancel")
                   else self._mapping(frame["frameId"]))
        return self._execute(action, payload, mapping, operation_id)

    def _execute(self, action, payload, mapping, operation_id=None):
        with self.lock:
            self._origin_current()
            check(self.remote_session_id is not None and self.remote_controller is not None,
                  "session_inactive", "Worker session is not active")
            if mapping is None:
                mapping = self._mapping()
            self.sequence += 1
            expected_epoch = self.remote_epoch
            command = {"controllerId": self.remote_controller, "epoch": expected_epoch,
                       "sequence": self.sequence, "commandId": operation_id or uuid.uuid4().hex,
                       "frameId": mapping[0], "geometryVersion": mapping[1],
                       "action": action, "payload": copy.deepcopy(payload)}
            try:
                response = self.client.call(f"/v1/sessions/{quote(self.remote_session_id, safe='')}/input", command, timeout=35)
                self._origin_current()
            except Exception as error:
                if isinstance(error,LiveError) and error.code in {'input_rejected','stale_frame','stale_geometry'}:
                    return {'ok':False,'outcome':'rejected','code':error.code}
                self.lab.fail(self.parent_sid, "Remote worker input transport failed")
                raise
            remote = response.get("session")
            if not isinstance(remote, dict) or remote.get("controllerId") != self.remote_controller:
                self.lab.fail(self.parent_sid, "Remote worker control changed")
                raise LiveError("stale_controller", "Worker control changed", 409)
            if remote.get("epoch") != expected_epoch:
                self.lab.fail(self.parent_sid, "Remote worker control epoch changed")
                raise LiveError("stale_controller", "Worker control epoch changed", 409)
            receipt = response.get("receipt")
            check(isinstance(receipt, dict) and receipt.get("status") == "injected"
                  and receipt.get("id") == command["commandId"]
                  and receipt.get("sequence") == command["sequence"]
                  and receipt.get("action") == command["action"]
                  and receipt.get("epoch") == expected_epoch,
                  "injection_unknown", "Worker did not confirm input")
            return {"ok": True, "timing": receipt.get("timing", "best-effort")}

    def observe(self, *args, **kwargs):
        check(self.remote_session_id is not None, "session_inactive", "Worker session is not active")
        return self.client.observe(self.remote_session_id)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.remote_session_id is None:
            return
        try:
            # Closing the original ownership is allowed after execution or
            # host authority is revoked. The worker checks these cached IDs;
            # an ordinary status read would unnecessarily require live access.
            closed = self.client.call(
                f"/v1/sessions/{quote(self.remote_session_id, safe='')}/close",
                {"controllerId": self.remote_controller, "epoch": self.remote_epoch}, timeout=35)
            final = closed.get("session") if isinstance(closed, dict) else None
            check(isinstance(final, dict) and final.get("id") == self.remote_session_id
                  and final.get("controllerId") == self.remote_controller
                  and type(final.get("epoch")) is int and final["epoch"] >= self.remote_epoch
                  and final.get("state") == "closed",
                  "cleanup_uncertain", "Worker did not confirm the original session cleanup")
            if self.remote_reservation is not None:
                self.remote_reservation.mark_released()
        except LiveError:
            raise
        except Exception:
            raise LiveError("cleanup_uncertain", "Worker session cleanup was not confirmed") from None
