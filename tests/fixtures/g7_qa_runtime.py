"""Owned process adapters for the executable G7 gate.

The app is a synthetic duplicate-counter. Hardware construction and presence
are the only worker substitutions; the real CLI, authority, inventory, HTTP,
recording storage and encoder run unchanged. No physical-device claim is made.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
from pathlib import Path
import signal
import struct
import sys
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from reproof import contracts
from reproof.live.model import Lab, check


def project_document():
    # Reuse a public contract fixture, never an existing application or account.
    from tests.test_fixture_allocations import project_document as registered
    project = registered()
    project["variables"] = []
    return project


def collection_policy():
    from tests.test_fixture_allocations import collection_policy as registered
    return registered()


def call_backend(origin, path, body=None):
    parsed = urlsplit(origin)
    check(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port,
          "synthetic_configuration", "Owned backend must be loopback")
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        encoded = json.dumps(body or {}, separators=(",", ":")).encode()
        connection.request("POST", path, body=encoded, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(128 * 1024 + 1)
        check(response.status == 200 and len(raw) <= 128 * 1024,
              "synthetic_backend", "Owned backend request failed")
        return json.loads(raw)
    finally:
        connection.close()


def public(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def backend_main(configuration):
    """Stateful app, independent observed values, and fenced fixture operations."""
    stop = threading.Event()
    lock = threading.Lock()
    state = {"count": 0, "prepared": False, "generation": 0,
             "operations": {}, "inputs": [], "observations": []}
    fields = ("operationId", "generation", "status", "completedAtMs", "retentionExpiresAtMs", "fence")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def reply(self, status, value):
            encoded = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try: self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError): pass

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 64 * 1024: raise ValueError()
                request = json.loads(self.rfile.read(size))
                with lock:
                    self.dispatch(request)
            except (KeyError, ValueError, TypeError, contracts.ContractError):
                self.reply(400, {"error": "invalid_synthetic_request"})

        def dispatch(self, request):
            now = int(time.time() * 1000)
            if self.path == "/snapshot":
                self.reply(200, state); return
            if self.path == "/input":
                if not state["prepared"] or request.get("action") != "tap":
                    self.reply(409, {}); return
                identifier = request["operationId"]
                existing = next((item for item in state["inputs"] if item["id"] == identifier), None)
                if existing is None:
                    state["count"] += 2  # The intentional application defect.
                    state["inputs"].append({"id": identifier, "action": "tap",
                        "atMs": now, "generation": state["generation"], "count": state["count"]})
                self.reply(200, {"ok": True, "count": state["count"]}); return
            if self.path == "/observations":
                if request["projectDigest"] != configuration["projectDigest"] or request["observationId"] != "screen":
                    self.reply(409, {}); return
                envelope = {"schemaVersion": 1, "id": "screen", "providerIncarnation": "owned_counter_backend",
                    "applicationId": request["applicationId"], "intervalMs": {"start": now, "end": now},
                    "clockUncertaintyMs": 0, "scope": "root", "targets": ["counter"], "properties": ["count"],
                    "limits": {"nodes": 1, "bytes": 4096, "depth": 1}, "truncated": False,
                    "errors": [], "completeness": "complete", "coverage": "snapshot"}
                state["observations"].append({"requestId": request["requestId"], "atMs": now, "count": state["count"]})
                self.reply(200, {"schemaVersion": 1, "requestId": request["requestId"], "envelope": envelope,
                    "values": {"count": state["count"]}, "absentProperties": []}); return
            identifier = request.get("operationId")
            old = state["operations"].get(identifier)
            if self.path == "/status":
                item = old or {"operationId": identifier, "generation": request["generation"], "status": "unknown",
                    "completedAtMs": None, "retentionExpiresAtMs": None, "fence": state["generation"]}
                self.reply(200, {key: item[key] for key in fields}); return
            if self.path != "/operations": self.reply(404, {}); return
            identity = {key: request[key] for key in ("generation", "payloadDigest", "idempotencyKey", "operation")}
            if old is not None:
                if old["identity"] != identity: self.reply(409, {}); return
                self.reply(200, {key: old[key] for key in fields}); return
            generation, operation = request["generation"], request["operation"]
            if generation < state["generation"]:
                self.reply(409, {}); return
            if operation == "prepare":
                if generation == state["generation"] and not state["prepared"]:
                    self.reply(409, {}); return
                state.update(count=0, prepared=True, generation=generation)
            elif operation == "check":
                if not state["prepared"] or generation != state["generation"]:
                    self.reply(409, {}); return
            elif operation == "cleanup":
                state.update(prepared=False, generation=generation)
            else: self.reply(400, {}); return
            item = {"operationId": identifier, "generation": generation, "status": "complete", "completedAtMs": now,
                "retentionExpiresAtMs": now + 600_000, "fence": generation, "identity": identity}
            state["operations"][identifier] = item
            self.reply(200, {key: item[key] for key in fields})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = .1
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    public({"backend": f"http://127.0.0.1:{server.server_port}", "pid": __import__("os").getpid()})
    try:
        while not stop.is_set(): server.handle_request()
    finally: server.server_close()


def frame_png(width, height, index, count):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    lines = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            bar = (index * 7) % max(1, width - 12) <= x < (index * 7) % max(1, width - 12) + 12
            counter = 12 <= y < 28 and 8 <= x < 8 + 12 * min(count, 8)
            row.extend((245, 92, 83) if counter else (71, 208, 165) if bar else (20, 33, 49))
        lines.append(b"\0" + row)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(lines))) + chunk(b"IEND", b"")


def worker_main(configuration):
    from reproof.live import worker_cli
    from reproof.ios_profile import validate_ios_profile
    from tests.fixtures.g6_worker_process import _profile
    from tests.fixtures.g6_worker_cli_process import PublicOutput

    document = _profile(configuration["project"]).data
    document["capabilities"]["geometry"] = {"maxWidth": 160, "maxHeight": 160,
        "orientations": ["portrait", "landscape"]}
    profile = validate_ios_profile(document)

    class Provider:
        def bind_authority(self, authority, provider_incarnation):
            self.authority = authority

        def start_authorized(self, session, lab, permit):
            self.authority.check_dispatch_permit(permit)
            self.session = session; self.lab = lab; self.stop = threading.Event()
            self.index = 0; self.rotation_at = None; self.count = 0
            self.frame_lock = threading.Lock(); self.render()
            self.thread = threading.Thread(target=self.capture, name="owned-synthetic-capture", daemon=True)
            self.thread.start()
            return {"ok": True, "qualification": "synthetic-protocol-only"}

        def render(self):
            with self.frame_lock:
                self.index += 1
                rotated = self.rotation_at is not None and time.monotonic() >= self.rotation_at
                width, height = (160, 96) if rotated else (96, 160)
                self.lab.publish_frame(self.session["id"], frame_png(width, height, self.index, self.count),
                    "image/png", width, height, "landscape" if rotated else "portrait")

        def capture(self):
            cadence = (.07, .19, .095, .26, .11)
            while not self.stop.wait(cadence[self.index % len(cadence)]):
                try: self.render()
                except Exception as error:
                    import traceback
                    (Path(configuration['root']) / 'capture-diagnostic.json').write_text(json.dumps({
                        'type': type(error).__name__, 'code': getattr(error, 'code', None),
                        'frameIndex': self.index, 'stopping': self.stop.is_set(),
                        'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno,
                                    'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)]}))
                    if not self.stop.is_set(): self.lab.fail(self.session["id"], "Owned capture failed")
                    return

        def execute_authorized(self, action, payload, permit, frame=None):
            self.authority.check_dispatch_permit(permit)
            result = call_backend(configuration["backend"], "/input",
                {"action": action, "operationId": permit.operation_id})
            self.count = result["count"]
            self.rotation_at = time.monotonic() + 1.6
            self.render()
            return {"ok": True, "timing": "best-effort"}

        def close_authorized(self, permit):
            self.authority.check_dispatch_permit(permit)
            self.stop.set(); self.thread.join(3)
            check(not self.thread.is_alive(), "cleanup_uncertain", "Owned capture did not stop")
            return {"ok": True}

    def synthetic_iphone(public_id, products, app, *, profile, authority_mode):
        check(public_id == configuration["deviceAlias"] and authority_mode == "shared-v2",
              "synthetic_configuration", "Owned worker configuration changed")
        return {"id": public_id, "name": "Synthetic duplicate counter", "platform": "ios", "kind": "ios-physical",
            "_authority": {"deviceKind": "ios-physical", "physicalId": configuration["physicalId"]},
            "capabilities": {"actions": ["tap"], "inputMode": "gesture-batch", "media": "png",
                "authorityMode": "shared-v2", "applicationIdentity": profile.application_identity,
                "applicationProfile": profile.data, "applicationProfileDigest": profile.digest,
                "identityEvidence": "synthetic-protocol-only", "locatorKinds": []}, "factory": Provider}

    root = Path(configuration["root"]); root.mkdir(mode=0o700)
    documents = {"profile.json": profile.data,
        "registration.json": {"project": configuration["project"], "collectionPolicy": configuration["collectionPolicy"]},
        "devices.json": {"schemaVersion": 1, "devices": [{"platform": "ios-physical", "deviceId": configuration["deviceAlias"],
            "profile": "profile.json", "products": "synthetic-products", "application": "synthetic.app"}]}}
    for name, value in documents.items():
        with (root / name).open("x") as stream: json.dump(value, stream)
    credentials = io.StringIO(json.dumps({key: configuration[key] for key in ("enrollmentToken", "transportToken")}))
    with patch("reproof.live.iphone.iphone_device", side_effect=synthetic_iphone), \
            patch.object(worker_cli, "connected_worker_devices", side_effect=lambda devices: {d["id"] for d in devices}), \
            patch.object(sys, "stdin", credentials), patch.object(sys, "stdout", PublicOutput(sys.stdout)):
        return worker_cli.main(["--port", "0", "--output", str(root / "output"), "--authority-root", str(root / "authority"),
            "--devices-config", str(root / "devices.json"), "--project-registration", str(root / "registration.json"),
            "--coordinator", configuration["coordinator"], "--enrollment-stdin", "--host-id", configuration["hostId"],
            "--host-incarnation", configuration["incarnation"], "--host-credential-output", str(root / "owned-host-credential.json")])


def coordinator_main(configuration):
    from reproof.live.access import AccessController, AccessStore
    from reproof.live.authority import HostAuthority
    from reproof.live.configuration import issue_bounded_project_grant
    from reproof.live.issue_configuration import compose_issue_workflow, load_issue_configuration
    from reproof.live.server import LiveServer
    from reproof.live.worker_cli import configured_remote_devices
    root = Path(configuration["root"])
    store = AccessStore(root / "coordinator-v2")
    authority = HostAuthority(root / "authority" / "authority.sqlite3")
    lab = Lab([], root / "lab", authority=authority,
        project_grant_provider=lambda registration: issue_bounded_project_grant(authority, registration.project["id"], lifetime_seconds=600))
    create_session = lab.create_release_session
    def observed_create(*args, **kwargs):
        try: return create_session(*args, **kwargs)
        except Exception as error:
            import traceback
            # Trusted setup diagnostics retain stack locations and error codes,
            # never local values, grants or supplied process configuration.
            (root / "startup-diagnostic.json").write_text(json.dumps({"type": type(error).__name__,
                "code": getattr(error, "code", None), "frames": [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                    for f in traceback.extract_tb(error.__traceback__)]}))
            raise
    lab.create_release_session = observed_create
    bundle = server = thread = None
    try:
        registration = lab.register_recording_project(
            configuration["project"], configuration["collectionPolicy"])
        access = AccessController(store); access.bind_project(registration)
        bundle = compose_issue_workflow(lab, access, load_issue_configuration(configuration["issueConfiguration"]),
            root=root / "issues", video_helper=configuration["videoHelper"], media_helper=configuration["mediaHelper"])
        for runtime in bundle.workflow.runtimes.values():
            replay = runtime.service.replay
            def observed_replay(*args, _call=replay, **kwargs):
                try:
                    return _call(*args, **kwargs)
                except Exception as error:
                    import traceback
                    causes = []
                    cause = error.__context__
                    while cause is not None and len(causes) < 4:
                        causes.append({'type': type(cause).__name__, 'code': getattr(cause, 'code', None),
                            'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno,
                                        'function': frame.name} for frame in traceback.extract_tb(cause.__traceback__)]})
                        cause = cause.__context__
                    (root / 'replay-diagnostic.json').write_text(json.dumps({
                        'type': type(error).__name__, 'code': getattr(error, 'code', None),
                        'causes': causes,
                        'issueStates': [{key: item.get(key) for key in ('state', 'reason', 'deviceCleanup')}
                                        for item in _call.__self__.list()[:8]],
                        'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno,
                                    'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)]}))
                    raise
            runtime.service.replay = observed_replay
        validator = bundle.workflow.packages.media_validator
        reports = []
        def observed_validation(body, mime):
            entry = {"digest": hashlib.sha256(body).hexdigest(), "mimeType": mime}
            try:
                report = validator(body, mime); entry["report"] = report
                return report
            except Exception as error:
                entry["error"] = getattr(error, "code", type(error).__name__)
                raise
            finally:
                if len(reports) < 256: reports.append(entry)
                (root / "media-validation.json").write_text(json.dumps(reports))
        bundle.workflow.packages.media_validator = observed_validation
        server = LiveServer(lab, port=configuration.get("port", 0), access=access, issue_workflow=bundle.workflow)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        public({"coordinator": server.origin, "pid": __import__("os").getpid()})
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("operation") == "stop": break
            check(set(request) == {"operation", "workers"} and request["operation"] == "install-workers",
                  "synthetic_configuration", "Unknown owned process control")
            devices = configured_remote_devices({"workers": request["workers"]}, access_store=store)
            with lab.lock:
                check(not lab.sessions and not lab.devices, "synthetic_configuration", "Owned setup is already sealed")
                for device in devices:
                    value = dict(device, state="available", sessionId=None)
                    previous = lab.device_history.get(device["id"], {})
                    if not isinstance(previous, dict) or previous.get("state", "available") != "available":
                        value.update(state="quarantined", quarantineReason="previous_session_unfinished")
                    lab.devices[device["id"]] = value
            public({"installed": [device["id"] for device in devices]})
    finally:
        if server is not None:
            server.shutdown()
            if thread is not None: thread.join(3)
            server.server_close()
        if bundle is not None: bundle.close()
        lab.close_all(); authority.close(); store.close()


def main():
    # A single bounded setup line permits further private coordinator commands.
    raw = sys.stdin.readline(128 * 1024 + 1)
    check(len(raw.encode()) <= 128 * 1024, "synthetic_configuration", "Owned setup is too large")
    configuration = json.loads(raw)
    selected = {"backend": backend_main, "worker": worker_main, "coordinator": coordinator_main}[sys.argv[1]]
    return selected(configuration)


if __name__ == "__main__":
    raise SystemExit(main())
