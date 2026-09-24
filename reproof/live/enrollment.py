"""Bounded coordinator enrollment client and private credential delivery."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import threading
from urllib.parse import urlsplit

from ..core import ContractError
from .access import AccessError


MAX_ENROLLMENT_RESPONSE = 64 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _require(value, message="Invalid enrollment configuration"):
    if not value:
        raise ContractError(message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _is_loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class PrivateCredentialOutput:
    """An owned mode-0600 output reserved before a live grant is issued."""

    def __init__(self, path):
        self.destination = Path(path).absolute()
        self._descriptor = None
        self._identity = None
        self._committed = False
        try:
            self.destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            parent = self.destination.parent.lstat()
            _require(stat.S_ISDIR(parent.st_mode) and not stat.S_ISLNK(parent.st_mode)
                     and parent.st_uid == os.getuid() and not self.destination.is_symlink(),
                     "Unsafe credential output path")
            self._descriptor = os.open(
                self.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            info = os.fstat(self._descriptor)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(),
                     "Unsafe credential output path")
            self._identity = (info.st_dev, info.st_ino)
        except FileExistsError:
            raise ContractError("Credential output already exists") from None
        except ContractError:
            self.abort()
            raise
        except OSError:
            self.abort()
            raise ContractError("Credential output could not be reserved") from None

    def write(self, value):
        _require(type(value) is dict and set(value) == {"credential"}
                 and isinstance(value["credential"], str),
                 "Invalid credential envelope")
        _require(self._descriptor is not None and not self._committed,
                 "Credential output is unavailable")
        encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        descriptor = self._descriptor
        try:
            with os.fdopen(descriptor, "wb") as handle:
                self._descriptor = None
                handle.write(encoded);handle.flush();os.fsync(handle.fileno())
            os.chmod(self.destination, 0o600)
            self._committed = True
            return self.destination
        except OSError:
            self.abort()
            raise ContractError("Credential output could not be written") from None

    def abort(self):
        descriptor = self._descriptor
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._descriptor = None
        if self._committed or self._identity is None:
            return
        try:
            info = self.destination.lstat()
            if (not stat.S_ISLNK(info.st_mode)
                    and (info.st_dev, info.st_ino) == self._identity):
                self.destination.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def write_private_credential(path, value):
    """Write one credential envelope without exposing it on stdout or argv."""
    output = PrivateCredentialOutput(path)
    try:
        return output.write(value)
    finally:
        output.abort()


class EnrollmentClient:
    """Direct no-proxy client for credential-only coordinator routes."""

    def __init__(self, coordinator_url, ca_file=None, *, request_timeout=10):
        parsed = urlsplit(coordinator_url)
        _require(parsed.scheme in {"http", "https"} and parsed.hostname is not None
                 and parsed.port is not None and not parsed.username and not parsed.password
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
                 "Invalid coordinator URL")
        _require(_is_loopback(parsed.hostname) or parsed.scheme == "https",
                 "Non-loopback enrollment requires verified HTTPS")
        if ca_file is not None:
            _require(parsed.scheme == "https", "A CA file requires HTTPS")
        _require(type(request_timeout) in {int, float}
                 and not isinstance(request_timeout, bool)
                 and 0.1 <= request_timeout <= 10,
                 "Invalid coordinator request deadline")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port
        self.netloc = parsed.netloc
        self.request_timeout = float(request_timeout)
        self.context = (ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
                        if parsed.scheme == "https" else None)

    def _call(self, path, credential, body):
        _require(path in {"/api/hosts/enroll", "/api/hosts/authenticate",
                          "/api/hosts/authorize", "/api/hosts/inventory"},
                 "Invalid enrollment route")
        prefix = "rpe" if path.endswith("/enroll") else "rph"
        _require(isinstance(credential, str) and re.fullmatch(
            rf"{prefix}\.[A-Za-z0-9_-]{{1,128}}\.[A-Za-z0-9_-]{{32,512}}",
            credential) is not None,
                 "Invalid enrollment credential")
        _require(type(body) is dict, "Invalid enrollment request")
        payload = json.dumps(body, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()
        connection = (http.client.HTTPSConnection(
            self.host, self.port, timeout=self.request_timeout,
            context=self.context)
            if self.scheme == "https" else
            http.client.HTTPConnection(
                self.host, self.port, timeout=self.request_timeout))
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
            timer = threading.Timer(self.request_timeout, abort_request)
            timer.daemon = True
            timer.start()
            connection.request("POST", path, payload, {
                "Host": self.netloc, "Authorization": "Bearer " + credential,
                "Content-Type": "application/json", "Connection": "close",
            })
            active_socket["value"] = connection.sock
            response = connection.getresponse()
            _require(not response.headers.get_all("Transfer-Encoding", [])
                     and len(response.headers.get_all("Content-Length", [])) == 1
                     and len(response.headers.get_all("Content-Type", [])) == 1,
                     "Invalid coordinator enrollment framing")
            length_value = response.headers.get("Content-Length", "")
            _require(length_value.isdigit()
                     and int(length_value) <= MAX_ENROLLMENT_RESPONSE,
                     "Invalid coordinator enrollment length")
            expected_length = int(length_value)
            blocks = [];size = 0
            while True:
                block = response.read1(min(16 * 1024, MAX_ENROLLMENT_RESPONSE + 1 - size))
                if not block:
                    break
                blocks.append(block);size += len(block)
                _require(size <= MAX_ENROLLMENT_RESPONSE,
                         "Coordinator enrollment response is too large")
            raw = b"".join(blocks)
            _require(len(raw) <= MAX_ENROLLMENT_RESPONSE,
                     "Coordinator enrollment response is too large")
            if len(raw) != expected_length:
                raise AccessError(
                    "transport_unavailable",
                    "Coordinator enrollment response was truncated", 503)
            try:
                value = json.loads(raw, object_pairs_hook=_pairs,
                                   parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
            except (ValueError, TypeError, UnicodeError):
                raise AccessError("enrollment_failed", "Coordinator enrollment failed", 502) from None
            if response.status not in {200, 201}:
                code = (value.get("error", {}).get("code") if isinstance(value, dict) else None)
                if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
                    code = "enrollment_failed"
                raise AccessError(code, "Coordinator enrollment failed", response.status)
            _require(isinstance(value, dict), "Invalid coordinator enrollment response")
            return value
        except (AccessError, ContractError):
            raise
        except (OSError, http.client.HTTPException, TimeoutError):
            raise AccessError("transport_unavailable", "Coordinator enrollment is unavailable", 503) from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()

    def enroll(self, enrollment_token, *, host_id, incarnation):
        _require(isinstance(host_id, str) and _ID.fullmatch(host_id), "Invalid host identity")
        _require(isinstance(incarnation, str) and _ID.fullmatch(incarnation),
                 "Invalid host incarnation")
        value = self._call("/api/hosts/enroll", enrollment_token,
                           {"hostId": host_id, "incarnation": incarnation})
        expected = {"hostId", "generation", "incarnation", "credentialId",
                    "expiresAt", "credential"}
        _require(set(value) == expected and value["hostId"] == host_id
                 and value["incarnation"] == incarnation
                 and type(value["generation"]) is int and value["generation"] >= 1
                 and type(value["expiresAt"]) is int
                 and isinstance(value["credential"], str),
                 "Invalid coordinator enrollment response")
        return value

    def authenticate(self, host_credential):
        value = self._call("/api/hosts/authenticate", host_credential, {})
        expected = {"hostId", "generation", "incarnation", "credentialId",
                    "expiresAt", "projectIds", "trustGroups"}
        _require(set(value) == expected and isinstance(value["hostId"], str)
                 and type(value["generation"]) is int and value["generation"] >= 1
                 and isinstance(value["incarnation"], str)
                 and isinstance(value["projectIds"], list)
                 and isinstance(value["trustGroups"], list),
                 "Invalid coordinator host response")
        return value

    def authorize(self, host_credential, *, project_id):
        _require(isinstance(project_id, str) and _ID.fullmatch(project_id),
                 "Invalid project identity")
        value = self._call(
            "/api/hosts/authorize", host_credential, {"projectId": project_id})
        expected = {"hostId", "generation", "incarnation", "projectId", "authorized"}
        _require(set(value) == expected and value["projectId"] == project_id
                 and value["authorized"] is True
                 and isinstance(value["hostId"], str)
                 and type(value["generation"]) is int and value["generation"] >= 1
                 and isinstance(value["incarnation"], str),
                 "Invalid coordinator host response")
        return value

    def refresh_inventory(self, host_credential, document):
        from .inventory import INVENTORY_VERSION
        value = self._call("/api/hosts/inventory", host_credential, document)
        expected = {"schemaVersion", "hostId", "generation", "incarnation",
                    "sequence", "devices"}
        _require(set(value) == expected and value["schemaVersion"] == INVENTORY_VERSION
                 and type(value["schemaVersion"]) is int
                 and type(value["generation"]) is int and value["generation"] >= 1
                 and isinstance(value["hostId"], str)
                 and isinstance(value["incarnation"], str)
                 and type(value["sequence"]) is int and value["sequence"] >= 1
                 and isinstance(value["devices"], list),
                 "Invalid coordinator inventory response")
        for item in value["devices"]:
            _require(type(item) is dict and set(item) == {
                "inventoryId", "alias", "deviceKind", "profileDigest", "state"},
                "Invalid coordinator inventory response")
        return value


__all__ = ["EnrollmentClient", "MAX_ENROLLMENT_RESPONSE", "PrivateCredentialOutput",
           "write_private_credential"]
