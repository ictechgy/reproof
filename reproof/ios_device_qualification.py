"""Physical iPhone ``mobile-device`` qualification from fixed probe observations.

The trusted supervisor measures one paired, wired iPhone through the pinned
``devicectl`` and a single issued XCTest helper session. There is no
deserializer for the returned capability: saved JSON, helper responses or test
summaries cannot create it. Only this in-process measurement, run inside the
same ``QualificationAuthority`` that later composes the protected service, can.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid

from .contracts.versions import bounded_int, digest, require, validate_digest, validate_id
from .core import ContractError
from .execution.backend import ExecutionDenied, QualificationAuthority, REQUIRED_PROBES
from .execution.qualification import _external_cancelled, _validate_cancellation
from .ios_xctest_template import IOSXCTestTemplate
from .live.model import LiveError
from .live.iphone import public_device_status, select_iphone, validate_tunnel_address

PROBES = ("device-boundary", "network-boundary", "backend-scope", "process-termination", "state-cleanup")
HELPER_BUNDLES = ("io.reproloop.live.host", "io.reproloop.live.tests.xctrunner")
HELPER_EXECUTABLES = ("ReproLiveHost.app/", "ReproLiveTests-Runner.app/")
CONTROL_TEST = "ReproLiveTests/LiveControlTests/testControlSession"
_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


class IOSDeviceQualificationError(RuntimeError):
    """Static, non-sensitive probe failure; never carries device identifiers."""


def _devicectl(*arguments, timeout=60, deadline_monotonic=None, cancellation=None):
    """Run the pinned devicectl with JSON output and return only ``result``."""
    with tempfile.TemporaryDirectory(prefix="repro-ios-qualification-") as directory:
        output = Path(directory) / "result.json"
        stop = time.monotonic() + timeout
        if deadline_monotonic is not None:
            stop = min(stop, deadline_monotonic)
        process = subprocess.Popen(["/usr/bin/xcrun", "devicectl", *arguments, "--json-output", str(output)],
                                   env=dict(_ENV, HOME=os.environ.get("HOME", "/var/empty")),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        try:
            while process.poll() is None:
                if _external_cancelled(cancellation) or time.monotonic() >= stop:
                    raise IOSDeviceQualificationError("devicectl command interrupted")
                time.sleep(min(0.2, max(0.05, stop - time.monotonic())))
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if process.returncode or not output.is_file():
            raise IOSDeviceQualificationError("devicectl command failed")
        value = json.loads(output.read_text())
        if value.get("info", {}).get("outcome") != "success":
            raise IOSDeviceQualificationError("devicectl did not confirm the operation")
        return value.get("result", {})


def _request(address, port, method, path, token, body=None, timeout=3):
    """One bounded HTTP exchange with the helper; returns ``(status, json)``."""
    connection = http.client.HTTPConnection(address, port, timeout=timeout)
    try:
        headers = {"Connection": "close"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        payload = response.read(65537)
        require(len(payload) <= 65536, "Helper response too large")
        try:
            return response.status, json.loads(payload)
        except ValueError:
            return response.status, {}
    finally:
        connection.close()


@dataclass
class PhysicalHelperSession:
    """One issued helper host/runner XCTest run bound to the USB tunnel address."""
    address: str
    port: int
    token: str
    incarnations: dict
    configuration_digest: str
    wired: bool
    _process: subprocess.Popen
    _staged: Path
    _returncode: int | None = None
    _summary: dict | None = None

    def request(self, method, path, body=None, *, token=""):
        return _request(self.address, self.port, method, path, self.token if token == "" else token, body)

    def wait(self, *, deadline_monotonic, cancellation=None):
        """Wait for xcodebuild; read the XCTest summary only after exit 0."""
        if self._returncode is None:
            while self._process.poll() is None:
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    raise IOSDeviceQualificationError("Helper XCTest did not finish in time") from None
                if _external_cancelled(cancellation):
                    raise IOSDeviceQualificationError("Helper XCTest was cancelled") from None
                time.sleep(min(0.2, remaining))
            self._returncode = self._process.returncode
            if self._returncode == 0:
                result = subprocess.run(["/usr/bin/xcrun", "xcresulttool", "get", "test-results", "summary", "--path",
                                         str(self._staged / "result.xcresult"), "-f", "json"],
                                        env=_ENV, capture_output=True, timeout=60)
                parsed = json.loads(result.stdout) if result.returncode == 0 else {}
                self._summary = {key: parsed.get(key) for key in ("passedTests", "failedTests", "skippedTests", "totalTestCount")}
        return {"returncode": self._returncode, "testSummary": self._summary}

    def close(self):
        """Stop a still-running xcodebuild group and remove the staged files."""
        if self._process.poll() is None:
            os.killpg(self._process.pid, signal.SIGTERM)
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self._process.pid, signal.SIGKILL)
                self._process.wait(timeout=5)
        shutil.rmtree(self._staged, ignore_errors=True)
        return not self._staged.exists()


@dataclass
class PhysicalIOSProbeSubject:
    """Trusted measurement surface for one selected paired iPhone.

    ``products`` must hold locally signed helper host/runner products with
    their Xcode-generated ``.xctestrun``. The subject never reads signing
    material and never exposes the UDID or tunnel address in evidence.
    """
    public_id: str
    products: Path
    port: int = 8766
    _device: object = field(default=None, repr=False)

    def device_status(self, *, cancellation=None, deadline_monotonic=None):
        self._device = select_iphone(self.public_id)
        return {**public_device_status(self._device), "udid": self._device.udid,
                "tunnelAddress": self._device.tunnel_address, "wired": self._device.wired}

    def lock_state(self, *, cancellation=None, deadline_monotonic=None):
        return _devicectl("device", "info", "lockState", "--device", self._device.identifier,
                          deadline_monotonic=deadline_monotonic, cancellation=cancellation)

    def installed_bundles(self, *, cancellation=None, deadline_monotonic=None):
        rows = _devicectl("device", "info", "apps", "--device", self._device.identifier,
                          deadline_monotonic=deadline_monotonic, cancellation=cancellation)["apps"]
        return sorted(row["bundleIdentifier"] for row in rows if row["bundleIdentifier"] in HELPER_BUNDLES)

    def process_executables(self, *, cancellation=None, deadline_monotonic=None):
        rows = _devicectl("device", "info", "processes", "--device", self._device.identifier,
                          deadline_monotonic=deadline_monotonic, cancellation=cancellation)["runningProcesses"]
        return sorted(row.get("executable", "") for row in rows)

    def uninstall(self, bundle, *, cancellation=None, deadline_monotonic=None):
        require(bundle in HELPER_BUNDLES, "Only helper bundles are removed by qualification")
        _devicectl("device", "uninstall", "app", "--device", self._device.identifier, bundle,
                   deadline_monotonic=deadline_monotonic, cancellation=cancellation)

    @contextmanager
    def open_helper_session(self):
        products = Path(self.products).resolve()
        source = next(iter(sorted(products.glob("*.xctestrun"))), None)
        require(source is not None, "Helper XCTest configuration missing")
        template = IOSXCTestTemplate(source, hashlib.sha256(source.read_bytes()).hexdigest())
        token = secrets.token_urlsafe(32)
        incarnations = {key: "ios-qual-" + key + "-" + secrets.token_hex(4) for key in ("host", "helper", "provider")}
        address = validate_tunnel_address(self._device.tunnel_address)
        body = template.render(
            {"helper-host": products / "Debug-iphoneos/ReproLiveHost.app",
             "helper-runner": products / "Debug-iphoneos/ReproLiveTests-Runner.app"},
            {"REPRO_TARGET_BUNDLE": HELPER_BUNDLES[0], "REPRO_LIVE_LISTEN_HOST": address,
             "REPRO_LIVE_LISTEN_PORT": str(self.port), "REPRO_LIVE_TOKEN": token,
             "REPRO_LIVE_APPLICATION_ID": "owned_qualification", "REPRO_LIVE_GENERAL_PROFILE_DIGEST": "a" * 64,
             "REPRO_LIVE_GENERAL_ACTIONS": "home,launch,terminate",
             "REPRO_LIVE_PROTOCOL_VERSION": "2", "REPRO_LIVE_HELPER_VERSION": "2",
             "REPRO_LIVE_HELPER_INCARNATION": incarnations["helper"],
             "REPRO_LIVE_HOST_INCARNATION": incarnations["host"],
             "REPRO_LIVE_PROVIDER_INCARNATION": incarnations["provider"]})
        staged = Path(tempfile.mkdtemp(prefix="repro-ios-qualification-session-"))
        configuration = staged / "session.xctestrun"
        configuration.write_bytes(body)
        configuration.chmod(0o400)
        process = subprocess.Popen(
            ["/usr/bin/xcodebuild", "test-without-building", "-xctestrun", str(configuration),
             "-destination", "id=" + self._device.udid, "-resultBundlePath", str(staged / "result.xcresult"),
             "-parallel-testing-enabled", "NO", "-only-testing:" + CONTROL_TEST],
            env=dict(_ENV, HOME=os.environ.get("HOME", "/var/empty")), cwd=staged, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        session = PhysicalHelperSession(address, self.port, token, incarnations,
                                        hashlib.sha256(body).hexdigest(), self._device.wired, process, staged)
        try:
            yield session
        finally:
            session.close()


def _helper_executables(paths):
    return [path for path in paths if any(marker in path for marker in HELPER_EXECUTABLES)]


def _await_status(session, cancellation, deadline):
    """Poll ``/status`` until the helper reports the unactivated retirement state."""
    while time.monotonic() < deadline and not _external_cancelled(cancellation):
        try:
            code, value = session.request("GET", "/status")
            if code == 200 and value.get("retirementVersion") == 1 and value.get("ready") is False:
                return value
        except (OSError, http.client.HTTPException, ContractError):
            pass
        time.sleep(.3)
    raise IOSDeviceQualificationError("Helper status did not become available")


def _authority_fields(status, operation_id, sequence, payload):
    return {"protocolVersion": 2, "operationId": operation_id, "projectId": "ios-device-qualification",
              "sessionId": "ios-qualification-session", "controllerId": "ios-qualification-controller",
              "sequence": sequence, "ownershipGeneration": 1,
              "hostIncarnation": status["hostIncarnation"], "helperIncarnation": status["helperIncarnation"],
              "providerIncarnation": status["providerIncarnation"], "nativeIncarnation": status["nativeIncarnation"],
              "nativeClockId": "ios-mach-continuous", "nativeDeadlineMs": status["nativeTimeMs"] + 60000,
              "payloadDigest": digest(payload)}


def _probe_device_boundary(subject, expected_udid, cancellation, deadline):
    status = subject.device_status(cancellation=cancellation, deadline_monotonic=deadline)
    lock = subject.lock_state(cancellation=cancellation, deadline_monotonic=deadline)
    evidence = {"ready": status["ready"] is True, "paired": status["paired"] is True,
                "developerMode": status["developerMode"] is True, "wired": status["wired"] is True,
                "tunnelConnected": status["tunnelConnected"] is True,
                "selectedDevice": status["udid"] == expected_udid,
                "tunnelAddressPrivateULA": _is_private_ula(status["tunnelAddress"]),
                "unlocked": lock.get("passcodeRequired") is False}
    return all(evidence.values()), evidence


def _is_private_ula(address):
    try:
        validate_tunnel_address(address)
        return True
    except LiveError:
        return False


def _probe_network_boundary(session, status, cancellation, deadline):
    none_code, _ = session.request("GET", "/status", token=None)
    wrong_code, _ = session.request("GET", "/status", token="x" * 43)
    interfaces = status.get("networkInterfaces")
    # helper가 보고한 비루프백 주소 중 터널 주소 이외의 경로는 모두 닫혀 있어야 한다.
    alternates = sorted({item for item in interfaces if isinstance(item, str) and item != session.address}
                        ) if type(interfaces) is list else None
    reachable = 0
    scanned = True
    if alternates is not None:
        for address in alternates:
            if _external_cancelled(cancellation) or time.monotonic() >= deadline:
                # 잘린 스캔은 "도달 0"으로 기록하지 않는다 — 미측정은 실패다.
                scanned = False
                break
            try:
                with socket.create_connection((address, session.port),
                                              timeout=max(0.05, min(2, deadline - time.monotonic()))):
                    reachable += 1
            except OSError:
                pass
    evidence = {"transportWired": session.wired is True, "listenAddressPrivateULA": _is_private_ula(session.address),
                "missingTokenRejected": none_code == 401, "wrongTokenRejected": wrong_code == 401,
                "authenticatedStatus": status.get("ready") is False,
                "interfacesReported": alternates is not None,
                "nonTunnelInterfaceCount": len(alternates) if alternates is not None else None,
                "nonTunnelReachableCount": reachable,
                "nonTunnelScanComplete": scanned}
    passed = (evidence["transportWired"] and evidence["listenAddressPrivateULA"]
              and evidence["missingTokenRejected"] and evidence["wrongTokenRejected"]
              and evidence["authenticatedStatus"] and evidence["interfacesReported"]
              and scanned and reachable == 0)
    return passed, evidence


def _probe_backend_scope(session, status, cancellation, deadline):
    incarnations_match = all(status.get(key + "Incarnation") == value for key, value in session.incarnations.items())
    startup_payload = {"kind": "ios-device-qualification-startup", "configurationDigest": session.configuration_digest}
    fields = _authority_fields(status, "ios-qual-startup", 1, startup_payload)
    foreign_grant = {**fields, "providerIncarnation": "ios-qual-foreign-" + secrets.token_hex(4)}
    foreign_grant["operationFingerprint"] = digest({**foreign_grant, "slot": "startup"})
    activate_code, activate_body = session.request("POST", "/activate", {"authority": foreign_grant})
    fields = _authority_fields(status, "ios-qual-startup", 1, startup_payload)
    startup = {**fields, "operationFingerprint": digest({**fields, "slot": "startup"})}
    code, activated = session.request("POST", "/activate", {"authority": startup})
    foreign = {**fields, "operationId": "ios-qual-foreign", "sequence": 2,
               "providerIncarnation": "ios-qual-other-" + secrets.token_hex(4),
               "payloadDigest": digest({"action": "home", "payload": {}})}
    foreign["operationFingerprint"] = digest({**foreign, "slot": "command"})
    foreign_code, foreign_body = session.request("POST", "/command",
        {"id": "ios-qual-foreign", "action": "home", "payload": {}, "authority": foreign})
    retire = {**fields, "operationId": "ios-qual-retire", "sequence": 2,
              "payloadDigest": digest({"action": "authority_retire", "payload": {}})}
    retire["operationFingerprint"] = digest({**retire, "slot": "retire"})
    retire_code, accepted = session.request("POST", "/retire",
        {"id": "ios-qual-retire", "action": "authority_retire", "payload": {}, "authority": retire})
    ordinary_code, ordinary = session.request("POST", "/command",
        {"id": "ios-qual-ordinary", "action": "home", "payload": {},
         "authority": {**retire, "operationId": "ios-qual-ordinary", "sequence": 3,
                       "payloadDigest": digest({"action": "home", "payload": {}})}})
    evidence = {"incarnationsMatch": incarnations_match,
                "foreignActivationRejected": activate_code >= 400 or activate_body.get("activated") is not True,
                "activated": code == 200 and activated.get("activated") is True,
                "foreignIncarnationRejected": foreign_code >= 400 or foreign_body.get("accepted") is not True,
                "retirementAccepted": retire_code == 202 and accepted.get("accepted") is True,
                "ordinaryCommandRejectedAfterRetire": ordinary_code >= 400 or ordinary.get("accepted") is not True}
    return all(evidence.values()), evidence


def _probe_process_termination(subject, session, cancellation, deadline):
    result = session.wait(deadline_monotonic=deadline, cancellation=cancellation)
    summary = result["testSummary"] or {}
    remaining = _helper_executables(subject.process_executables(cancellation=cancellation,
                                                              deadline_monotonic=deadline))
    while remaining and time.monotonic() < deadline and not _external_cancelled(cancellation):
        time.sleep(min(2, max(0.05, deadline - time.monotonic())))
        remaining = _helper_executables(subject.process_executables(cancellation=cancellation,
                                                                    deadline_monotonic=deadline))
    evidence = {"xcodebuildExitZero": result["returncode"] == 0,
                "singleTestPassed": summary.get("passedTests") == 1 and summary.get("failedTests") == 0
                                    and summary.get("skippedTests") == 0 and summary.get("totalTestCount") == 1,
                "helperProcessesAbsent": not remaining,
                "observationComplete": not _external_cancelled(cancellation)
                                       and (not remaining or time.monotonic() < deadline)}
    return all(evidence.values()), evidence


def _probe_state_cleanup(subject, session, cancellation, deadline):
    for bundle in subject.installed_bundles(cancellation=cancellation, deadline_monotonic=deadline):
        if _external_cancelled(cancellation) or time.monotonic() >= deadline:
            break
        subject.uninstall(bundle, cancellation=cancellation, deadline_monotonic=deadline)
    bounded = not _external_cancelled(cancellation) and time.monotonic() < deadline
    evidence = {"helperBundlesAbsent": subject.installed_bundles(cancellation=cancellation,
                                                                 deadline_monotonic=deadline) == [],
                "stagedFilesRemoved": session.close() is True,
                "uninstallCompleted": bounded}
    return all(evidence.values()), evidence


def _measure(subject, expected_udid, cancellation, deadline):
    """Run the fixed probe order; stop at the first failure or cancellation."""
    probes = []

    def record(probe_id, passed, evidence):
        probes.append({"probeId": probe_id, "passed": passed and not _external_cancelled(cancellation),
                       "evidence": evidence})
        return probes[-1]["passed"]

    try:
        passed, evidence = _probe_device_boundary(subject, expected_udid, cancellation, deadline)
        if not record(PROBES[0], passed, evidence):
            return probes
        with subject.open_helper_session() as session:
            status = _await_status(session, cancellation, deadline)
            passed, evidence = _probe_network_boundary(session, status, cancellation, deadline)
            if not record(PROBES[1], passed, evidence):
                return probes
            passed, evidence = _probe_backend_scope(session, status, cancellation, deadline)
            if not record(PROBES[2], passed, evidence):
                return probes
            passed, evidence = _probe_process_termination(subject, session, cancellation, deadline)
            if not record(PROBES[3], passed, evidence):
                return probes
            passed, evidence = _probe_state_cleanup(subject, session, cancellation, deadline)
            record(PROBES[4], passed, evidence)
    except (IOSDeviceQualificationError, ContractError, LiveError, OSError, ValueError, KeyError, TypeError,
            subprocess.SubprocessError, http.client.HTTPException) as error:
        probes.append({"probeId": PROBES[len(probes)] if len(probes) < len(PROBES) else "unknown",
                       "passed": False, "evidence": {"errorType": type(error).__name__}})
    return probes


def qualify_ios_device(backend_id, authority, subject, *, environment_digest, signing_policy_id, expected_udid,
                       ttl_ms=3600000, timeout_seconds=300, cancellation=None):
    """Measure the selected iPhone and issue a ``mobile-device`` qualification on full success.

    Every probe is observed here; nothing in ``subject`` or on disk grants the
    capability. Any failure, cancellation or exception leaves the backend
    revoked and returns ``qualification=None``.
    """
    cancellation = _validate_cancellation(cancellation)
    if type(authority) is not QualificationAuthority:
        raise ExecutionDenied("Trusted iOS device qualification composition required")
    validate_id(backend_id)
    validate_id(signing_policy_id, "signing policy id")
    validate_digest(environment_digest, "environment digest")
    bounded_int(ttl_ms, "qualification lifetime", 1000, 24 * 60 * 60 * 1000)
    bounded_int(timeout_seconds, "qualification timeout", 30, 1800)
    require(type(expected_udid) is str and expected_udid, "Registered device UDID required")
    execution_class = "mobile-device"
    probes, qualification = [], None
    try:
        # 새 측정은 먼저 이전 qualification을 폐기한다. 실패해도 옛 권한이 남지 않는다.
        authority.revoke_backend(backend_id, execution_class, environment_digest)
        deadline = time.monotonic() + timeout_seconds
        probes = _measure(subject, expected_udid, cancellation, deadline)
        complete = ([item["probeId"] for item in probes] == list(PROBES) and all(item["passed"] for item in probes)
                    and not _external_cancelled(cancellation))
        if complete:
            now = int(time.time() * 1000)
            probe_ids = sorted(REQUIRED_PROBES[execution_class])
            receipts = [authority.record_probe(probe_id=probe_id, backend_id=backend_id, execution_class=execution_class,
                            environment_digest=environment_digest, outcome="pass",
                            evidence_digest=digest({"probeId": probe_id, "runs": probes}), observed_at_ms=now)
                        for probe_id in probe_ids if not _external_cancelled(cancellation)]
            if len(receipts) == len(probe_ids) and not _external_cancelled(cancellation):
                qualification = authority.issue_backend_qualification({"schemaVersion": 1,
                    "id": "qualified-" + uuid.uuid4().hex, "backendId": backend_id, "executionClass": execution_class,
                    "environmentDigest": environment_digest, "signingPolicyId": signing_policy_id,
                    "issuedAtMs": now, "expiresAtMs": now + ttl_ms, "probeIds": probe_ids}, receipts, evaluated_at_ms=now)
                if _external_cancelled(cancellation):
                    authority.revoke_backend(backend_id, execution_class, environment_digest)
                    qualification = None
    except (ContractError, ExecutionDenied, IOSDeviceQualificationError, OSError):
        qualification = None
    report = {"schemaVersion": 1, "status": "qualified" if qualification is not None else "blocked-unqualified",
              "backendId": backend_id, "executionClass": execution_class, "platform": "ios",
              "environmentDigest": environment_digest, "physicalDevice": True,
              "qualified": qualification is not None, "probes": probes,
              "authority": "process-local" if qualification is not None else "none"}
    return report, qualification
