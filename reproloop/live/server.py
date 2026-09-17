"""Loopback legacy console and separately authenticated shared coordinator."""
from __future__ import annotations

import copy
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import secrets
import ssl
import time
import uuid
from urllib.parse import urlsplit

from .model import LiveError, check
from ..core import ContractError
from ..resources import resource_root
from ..storage import _unique_object, Lease

WEB = resource_root() / "live-web"


def _loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LiveServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, lab, port=0, *, max_jobs_running=2, repair_project=None,
                 access=None, host=None, ssl_context=None, origin=None,
                 issue_service=None, public_configuration=None, inventory=None, issue_workflow=None,
                 protected_recovery=None):
        self.lab = lab
        self.access = access
        self.issue_service = issue_service
        self.issue_workflow = issue_workflow
        if protected_recovery is not None:
            from .protected_recovery import ProtectedRecoveryService
            check(type(protected_recovery) is ProtectedRecoveryService and access is not None
                and protected_recovery.lab is lab and protected_recovery.access is access
                and protected_recovery.bundle.workflow is issue_workflow,
                'invalid_configuration', 'Protected recovery belongs to another service', 400)
        self.protected_recovery = protected_recovery
        self.public_configuration = public_configuration or {}
        self._owns_inventory = access is not None and inventory is None
        if self._owns_inventory:
            from .inventory import InventoryRegistry
            check(not (access.store.root / "inventory-v1").exists(),
                  "migration_required", "Earlier inventory requires offline migration", 409)
            inventory = InventoryRegistry(access.store.root / "inventory-v2")
        self.inventory = inventory
        lab.remote_inventory = inventory
        self.browser_token = secrets.token_urlsafe(32) if access is None else None
        selected_host = host or "127.0.0.1"
        if access is None:
            check(_loopback(selected_host) and ssl_context is None,
                  "invalid_configuration", "Legacy console is loopback HTTP only", 400)
        elif not _loopback(selected_host):
            check(isinstance(ssl_context, ssl.SSLContext), "invalid_tls",
                  "Non-loopback shared coordination requires TLS", 400)
        try:
            super().__init__((selected_host, port), Handler)
        except Exception:
            if self._owns_inventory and self.inventory is not None:
                self.inventory.close()
            raise
        try:
            if ssl_context is not None:
                self.socket = ssl_context.wrap_socket(self.socket, server_side=True)
            scheme = "https" if ssl_context is not None else "http"
            default_host = "127.0.0.1" if selected_host in {"0.0.0.0", "::"} else selected_host
            derived = f"{scheme}://{default_host}:{self.server_port}"
            self.origin = (origin or derived).rstrip("/")
            parsed = urlsplit(self.origin)
            check(parsed.scheme == scheme and parsed.hostname is not None and parsed.port is not None
                  and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
                  "invalid_configuration", "Invalid coordinator origin", 400)
            if port != 0:
                check(parsed.port == self.server_port, "invalid_configuration",
                      "Coordinator origin port does not match listener", 400)
            self.origin_netloc = parsed.netloc
            lab.base_url = self.origin
        except Exception:
            super().server_close()
            if self._owns_inventory and self.inventory is not None:
                self.inventory.close()
            raise
        from .jobs import JobQueue
        self.output_lease = Lease("live-output:" + str(lab.output.resolve()))
        self.jobs = None
        self.repairs = None
        try:
            self.output_lease.__enter__()
            if access is not None:
                access.restore_lab_bindings(lab)
            self.jobs = JobQueue(
                lab, max_running=max_jobs_running,
                effect_authorizer=self._authorize_job_effect if access is not None else None,
                resource_binder=self._bind_job if access is not None else None).start()
            if repair_project is not None and access is None:
                from .repair_jobs import LiveRepairJobs
                self.repairs = LiveRepairJobs(
                    lab, repair_project["source"], repair_project["build"],
                    platform=repair_project.get("platform", "ios"),
                    app_profile=repair_project.get("app_profile"))
            lab.start_maintenance()
        except Exception:
            if self.jobs is not None:
                self.jobs.close()
            self.output_lease.__exit__(None, None, None)
            super().server_close()
            if self._owns_inventory and self.inventory is not None:
                self.inventory.close()
            raise

    def _bind_job(self, job_id, project_id, principal_id):
        return self.access.bind_resource(
            "job", job_id, project_id, principal_id, meaning="release")

    def _authorize_job_effect(self, principal_id, credential_id, project_id,
                              device_id, job_id, kind, authorization_id=None):
        principal = self.access.operation_principal(credential_id, authorization_id)
        if principal.principal_id != principal_id:
            raise LiveError("authorization_revoked", "Job authorization was revoked", 403)
        self.access.authorize_resource(
            principal, "job", job_id, "job.manage", executable=True)
        self.access.authorize_device(principal, device_id, project_id, "device.operate")
        return True

    def close_operations(self):
        if self.protected_recovery is not None:
            self.protected_recovery.close(deadline_monotonic=time.monotonic()+10)
        if self.issue_workflow is not None:
            self.issue_workflow.close()
        if self.repairs is not None:
            self.repairs.close()
        if self.jobs is not None:
            self.jobs.close()
        self.lab.stop_maintenance()

    def server_close(self):
        self.close_operations()
        try:
            self.output_lease.__exit__(None, None, None)
            super().server_close()
        finally:
            if self._owns_inventory and self.inventory is not None:
                self.inventory.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "ReproLive"
    sys_version = ""

    def log_message(self, *args):
        pass

    do_GET = lambda self: self.dispatch()
    do_POST = lambda self: self.dispatch()
    do_OPTIONS = lambda self: self.dispatch()
    do_HEAD = lambda self: self.dispatch()
    do_PUT = lambda self: self.dispatch()
    do_PATCH = lambda self: self.dispatch()
    do_DELETE = lambda self: self.dispatch()

    def parse_request(self):
        fields = self.raw_requestline.split()
        raw_target = (fields[1].decode("iso-8859-1", "strict")
                      if len(fields) >= 2 else None)
        parsed = super().parse_request()
        if parsed:
            self._raw_request_target = raw_target
        return parsed

    def respond(self, status, value, mime="application/json", cookie=None, download=False):
        data = value if isinstance(value, bytes) else json.dumps(
            value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        for key, value in (
            ("Content-Type", mime), ("Content-Length", str(len(data))),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
        ):
            self.send_header(key, value)
        if cookie is True:
            self.send_header("Set-Cookie",
                             f"repro_live={self.server.browser_token}; HttpOnly; SameSite=Strict; Path=/")
        elif isinstance(cookie, str):
            secure = "; Secure" if self.server.origin.startswith("https://") else ""
            self.send_header("Set-Cookie",
                             f"repro_shared={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200{secure}")
        elif cookie is False:
            secure = "; Secure" if self.server.origin.startswith("https://") else ""
            self.send_header("Set-Cookie",
                             f"repro_shared=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0{secure}")
        if download:
            filename = download if isinstance(download, str) else "recording.json"
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def body(self):
        check(self.headers.get("Content-Type", "").split(";")[0] == "application/json",
              "invalid_content_type", "Expected application/json", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise LiveError("invalid_body", "Invalid content length", 400) from None
        check(0 < length <= 5 * 1024 * 1024, "invalid_body",
              "Request body is empty or too large", 413)
        deadline = time.monotonic() + 10
        remaining = length
        blocks = []
        try:
            while remaining:
                available = deadline - time.monotonic()
                check(available > 0, "request_timeout",
                      "Request body deadline elapsed", 408)
                self.connection.settimeout(available)
                block = self.rfile.read1(min(64 * 1024, remaining))
                check(bool(block), "invalid_body", "Request body is truncated", 400)
                blocks.append(block);remaining -= len(block)
            value = json.loads(b"".join(blocks), object_pairs_hook=_unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        except LiveError:
            raise
        except (ValueError, TimeoutError, ContractError, RecursionError, OSError):
            raise LiveError("invalid_body", "Invalid JSON body", 400) from None
        check(isinstance(value, dict), "invalid_body", "Expected an object", 400)
        return value

    def _reject_ambiguous_headers(self):
        # BaseHTTPRequestHandler preserves duplicate field lines.  Never let a
        # proxy/server disagreement select which security value is effective.
        for name in ("Host", "Authorization", "Content-Length", "Content-Type",
                     "Cookie", "Origin", "Sec-Fetch-Site", "X-Repro-CSRF", "Range", "X-Repro-Content-SHA256"):
            if len(self.headers.get_all(name, [])) > 1:
                raise LiveError("ambiguous_header", "Ambiguous request headers", 400)
        if self.headers.get_all("Transfer-Encoding", []):
            raise LiveError("ambiguous_header", "Unsupported request framing", 400)

    def _cookies(self):
        cookies = SimpleCookie()
        raw = self.headers.get("Cookie", "")
        names = [item.split("=", 1)[0].strip() for item in raw.split(";") if "=" in item]
        if any(names.count(name) > 1 for name in {"repro_live", "repro_shared"}):
            raise LiveError("ambiguous_header", "Ambiguous request headers", 400)
        try:
            cookies.load(raw)
        except Exception:
            pass
        return cookies

    def _origin(self):
        origin = self.headers.get("Origin")
        fetch = self.headers.get("Sec-Fetch-Site")
        check(origin in (None, self.server.origin) and fetch not in {"cross-site", "same-site"},
              "cross_origin", "Cross-origin access denied", 403)

    def _shared_principal(self):
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return self.server.access.store.authenticate_principal(authorization[7:])
        supplied = self._cookies().get("repro_shared")
        check(supplied is not None, "unauthorized", "Authentication required", 401)
        return self.server.access.authenticate_browser(
            supplied.value, csrf=self.headers.get("X-Repro-CSRF"),
            require_csrf=self.command not in {"GET", "HEAD"})

    def _static(self, path):
        if self.command != "GET" or path not in {
                "/", "/index.html", "/styles.css", "/app.js", "/stream.js", "/pointer.js",
                "/boot.js", "/issue.js", "/video.js"}:
            return False
        name = "index.html" if path == "/" else path[1:]
        mime = {"index.html": "text/html; charset=utf-8", "styles.css": "text/css",
                "app.js": "text/javascript", "stream.js": "text/javascript",
                "boot.js": "text/javascript", "issue.js": "text/javascript", "video.js": "text/javascript",
                "pointer.js": "text/javascript"}[name]
        self.respond(200, (WEB / name).read_bytes(), mime,
                     cookie=(name == "index.html") if self.server.access is None else None)
        return True

    def _bridge(self, parts, lab):
        if len(parts) != 3 or parts[0] != "bridge":
            return False
        check(not self.headers.get("Origin") and not self.headers.get("Sec-Fetch-Site"),
              "forbidden", "Native bridge only", 403)
        try:
            if self.server.access is not None:
                binding = self.server.access.store.resource("session", parts[1])
                self.server.access.registration(
                    binding.project_id, project_digest=binding.project_digest)
            session = lab._session(parts[1])
            provider = session["provider"]
            token = getattr(provider, "token", "")
        except Exception:
            raise LiveError("unauthorized", "Native authentication required", 401) from None
        check(bool(token) and secrets.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + token),
            "unauthorized", "Native authentication required", 401)
        check((parts[2] == "next" and self.command == "GET")
              or (parts[2] in {"ready", "started", "ack", "frame", "clock-start", "clock-end"}
                  and self.command == "POST"),
              "method_not_allowed", "Invalid bridge method", 405)
        self.respond(200, provider.bridge(parts[2], self.body() if self.command == "POST" else {}))
        return True

    def dispatch(self):
        try:
            self._reject_ambiguous_headers()
            check(self.headers.get("Host") == self.server.origin_netloc,
                  "invalid_host", "Use the configured coordinator origin", 403)
            target = urlsplit(getattr(self, "_raw_request_target", None) or self.path)
            path = target.path
            check(not target.scheme and not target.netloc
                  and not target.query and not target.fragment and path.startswith("/")
                  and (path == "/" or not path.endswith("/"))
                  and "//" not in path and "%" not in path
                  and all(segment not in {".", ".."} for segment in path.split("/")),
                  "invalid_path", "Use the canonical coordinator path", 400)
            parts = [item for item in path.split("/") if item]
            lab = self.server.lab
            self._origin()
            if self._bridge(parts, lab) or self._static(path):
                return
            if parts==['api','mode'] and self.command=='GET':
                return self.respond(200,{'mode':'shared' if self.server.access is not None else 'legacy',
                                        'issueWorkflow':self.server.issue_workflow is not None})
            if self.server.access is None:
                return self._dispatch_legacy(parts, lab)
            return self._dispatch_shared(parts, lab)
        except LiveError as error:
            self.respond(error.status, {"error": {"code": error.code, "message": str(error)}})
        except Exception as error:
            from .access import AccessError
            if isinstance(error, AccessError):
                self.respond(error.status, {"error": {"code": error.code, "message": str(error)}})
            elif isinstance(error, (ContractError, TypeError, KeyError, ValueError)):
                self.respond(400, {"error": {"code": "invalid_argument",
                                              "message": "Invalid request arguments"}})
            else:
                self.respond(500, {"error": {"code": "internal_error",
                                              "message": "Coordinator operation failed"}})

    def _dispatch_shared(self, parts, lab):
        access = self.server.access
        if self.command not in {"GET", "POST"}:
            self._shared_principal()
            raise LiveError("method_not_allowed", "Method is not allowed", 405)
        if parts == ["api", "hosts", "enroll"] and self.command == "POST":
            authorization = self.headers.get("Authorization", "")
            check(authorization.startswith("Bearer "), "unauthorized",
                  "Enrollment authentication required", 401)
            body = self.body()
            check(set(body) == {"hostId", "incarnation"}, "invalid_argument",
                  "Expected host identity and incarnation", 400)
            return self.respond(201, access.store.consume_host_enrollment(
                authorization[7:], host_id=body["hostId"], incarnation=body["incarnation"]))
        if parts == ["api", "hosts", "authenticate"] and self.command == "POST":
            authorization = self.headers.get("Authorization", "")
            check(authorization.startswith("Bearer "), "unauthorized", "Host authentication required", 401)
            body = self.body();check(not body, "invalid_argument", "Host authentication body must be empty", 400)
            host = access.store.authenticate_host(authorization[7:])
            return self.respond(200, {"hostId": host.host_id, "generation": host.generation,
                "incarnation": host.incarnation, "credentialId": host.credential_id,
                "expiresAt": host.expires_at, "projectIds": list(host.project_ids),
                "trustGroups": list(host.trust_groups)})
        if parts == ["api", "hosts", "authorize"] and self.command == "POST":
            authorization = self.headers.get("Authorization", "")
            check(authorization.startswith("Bearer "), "unauthorized",
                  "Host authentication required", 401)
            body = self.body()
            check(set(body) == {"projectId"}, "invalid_argument",
                  "Host authorization requires a project", 400)
            host = access.store.authenticate_host(authorization[7:])
            project = access.store.project(body["projectId"])
            access.store.authorize_host(
                host, project_id=project["projectId"], trust_group=project["trustGroup"])
            return self.respond(200, {
                "hostId": host.host_id, "generation": host.generation,
                "incarnation": host.incarnation, "projectId": project["projectId"],
                "authorized": True})
        if parts == ["api", "hosts", "inventory"] and self.command == "POST":
            authorization = self.headers.get("Authorization", "")
            check(authorization.startswith("Bearer "), "unauthorized",
                  "Host authentication required", 401)
            host = access.store.authenticate_host(authorization[7:])
            document = self.body()
            current = access.store.authenticate_host(authorization[7:])
            check((current.host_id, current.generation, current.incarnation)
                  == (host.host_id, host.generation, host.incarnation),
                  "stale_host", "Inventory host identity changed", 409)
            check(self.server.inventory is not None, "unsupported_operation",
                  "Host inventory is unavailable", 503)
            return self.respond(200, self.server.inventory.refresh(current, document))
        if parts == ["api", "auth", "session"] and self.command == "POST":
            authorization = self.headers.get("Authorization", "")
            check(authorization.startswith("Bearer "), "unauthorized", "Bearer credential required", 401)
            body = self.body();check(not body, "invalid_argument", "Session login body must be empty", 400)
            session = access.create_browser_session(authorization[7:])
            public = {key: value for key, value in session.items() if key != "cookie"}
            return self.respond(201, public, cookie=session["cookie"])
        principal = self._shared_principal()
        if parts[:2] == ['api', 'protected-recovery']:
            from .protected_recovery import dispatch
            return dispatch(self, principal, parts[2:])
        if parts[:2]==['api','release']:
            from .issue_http import dispatch
            return dispatch(self,principal,parts)
        body = self.body() if self.command == "POST" else {}
        if parts == ["api", "auth", "logout"] and self.command == "POST":
            check(not body, "invalid_argument", "Logout body must be empty", 400)
            supplied = self._cookies().get("repro_shared")
            if supplied is not None:
                browser_principal = access.authenticate_browser(
                    supplied.value, csrf=self.headers.get("X-Repro-CSRF"),
                    require_csrf=True)
                check(browser_principal.principal_id == principal.principal_id,
                      "csrf", "Logout authorization does not match the browser session", 403)
                access.close_browser_session(supplied.value)
            return self.respond(200, {"loggedOut": True}, cookie=False)
        if len(parts) >= 2 and parts[:2] == ["api", "admin"]:
            return self._dispatch_admin(parts[2:], principal, body)
        if parts == ["api", "configuration"] and self.command == "GET":
            access.store.authorize(principal, None, "administration.read")
            return self.respond(200, copy.deepcopy(self.server.public_configuration))
        if parts == ["api", "health"] and self.command == "GET":
            check(bool(access.store.authorized_project_ids(principal, "health.read")),
                  "forbidden", "Health access is not granted", 403)
            visible = set(access.visible_device_ids(principal))
            devices = [item for item in lab.list_devices() if item["id"] in visible]
            jobs = self._visible_jobs(principal)
            return self.respond(200, {"status": "ok",
                "devices": {state: sum(item["state"] == state for item in devices)
                            for state in ("available", "busy", "quarantined", "recovering", "repairing")},
                "jobs": {state: sum(item["state"] == state for item in jobs)
                         for state in ("queued", "starting", "running", "cleaning", "succeeded", "failed", "cancelled", "interrupted")},
                "idleTimeoutSeconds": lab.idle_timeout,
                "maxSessionSeconds": lab.max_session_seconds})
        if parts == ["api", "devices"] and self.command == "GET":
            visible = set(access.visible_device_ids(principal))
            return self.respond(200, {"devices": [item for item in lab.list_devices() if item["id"] in visible]})
        if len(parts) == 4 and parts[:2] == ["api", "devices"] and parts[3] == "recover" and self.command == "POST":
            check(set(body) == {"projectId"}, "invalid_argument", "Recovery requires a project", 400)
            access.authorize_device(principal, parts[2], body["projectId"], "device.operate")
            check(self.server.protected_recovery is None or not self.server.protected_recovery.configured_device(parts[2]),
                  'protected_operation_required', 'Select the original protected operation for recovery', 409)
            return self.respond(200, {"device": lab.recover_device(parts[2])})
        if parts == ["api", "sessions"]:
            if self.command == "GET":
                return self.respond(200, {"sessions": self._visible_sessions(principal)})
            return self._create_shared_session(principal, body)
        if parts == ["api", "recordings", "import"] and self.command == "POST":
            check(set(body) == {"projectId", "recording"}, "invalid_argument",
                  "Expected a project and recording document", 400)
            registration = access.registration(body["projectId"])
            access.store.authorize(principal, body["projectId"], "recording.import")
            result = lab.import_recording(body["recording"], principal.principal_id)
            access.bind_resource("recording", result["recording"]["id"],
                                 registration.project["id"], principal.principal_id,
                                 meaning="legacy-inert")
            return self.respond(201 if result["imported"] else 200, result)
        if parts == ["api", "recordings"] and self.command == "GET":
            return self.respond(200, {"recordings": self._visible_recordings(principal)})
        if len(parts) == 4 and parts[:2] == ["api", "recordings"] and parts[3] == "derive" and self.command == "POST":
            check(set(body) <= {"eventIds", "speed"}, "invalid_argument", "Unknown recording transform", 400)
            binding = access.authorize_resource(principal, "recording", parts[2], "recording.derive")
            self._owned(binding, principal)
            result = lab.derive_recording(parts[2], binding.owner_id,
                event_ids=body.get("eventIds"), speed=body.get("speed", 1))
            access.bind_resource("recording", result["id"], binding.project_id,
                                 binding.owner_id, meaning=binding.meaning)
            return self.respond(201, {"recording": result})
        if len(parts) in {3, 4} and parts[:2] == ["api", "recordings"] and self.command == "GET":
            capability = "export.read" if len(parts) == 4 else "recording.read"
            binding = access.authorize_resource(principal, "recording", parts[2], capability,
                executable=len(parts) == 4 and parts[3] == "script")
            recording = self._recording(binding)
            if len(parts) == 3:
                return self.respond(200, {"recording": recording})
            if parts[3] == "export":
                return self.respond(200, recording, download=True)
            if parts[3] == "script":
                access.store.authorize(principal, binding.project_id, "replay.execute")
                from .client import export_script
                return self.respond(200, export_script(recording),
                                    "text/x-python; charset=utf-8", download="replay.py")
        if parts == ["api", "jobs"]:
            if self.command == "GET":
                return self.respond(200, {"jobs": self._visible_jobs(principal)})
            source = access.authorize_resource(principal, "recording", body.get("recordingId"),
                                               "job.manage", executable=True)
            self._owned(source, principal)
            self._recording(source)
            job = self.server.jobs.submit(source.owner_id, body, project_id=source.project_id,
                principal_id=principal.principal_id, credential_id=principal.credential_id,
                authorization_id=principal.authorization_id)
            return self.respond(202, {"job": job})
        if len(parts) in {3, 4} and parts[:2] == ["api", "jobs"]:
            capability = "job.manage" if self.command == "POST" else "job.read"
            cleanup = len(parts) == 4 and parts[3] == "cancel" and self.command == "POST"
            binding = access.authorize_resource(principal, "job", parts[2], capability,
                                                executable=self.command == "POST" and not cleanup)
            if len(parts) == 3 and self.command == "GET":
                return self.respond(200, {"job": self.server.jobs.get(parts[2], binding.owner_id)})
            if len(parts) == 4 and parts[3] == "cancel" and self.command == "POST":
                check(not body, "invalid_argument", "Cancel does not accept parameters", 400)
                self._owned(binding, principal)
                return self.respond(200, {"job": self.server.jobs.cancel(parts[2], binding.owner_id)})
            if len(parts) == 4 and parts[3] == "report" and self.command == "GET":
                access.store.authorize(principal, binding.project_id, "export.read")
                return self.respond(200, {"schemaVersion": 1, "kind": "live-replay-job-report",
                    "job": self.server.jobs.get(parts[2], binding.owner_id)}, download="job-report.json")
        if parts == ["api", "repairs"] and self.command == "GET":
            access.store.authorize(principal, None, "administration.read")
            return self.respond(200, {"enabled": False, "jobs": []})
        if len(parts) >= 2 and parts[:2] == ["api", "repairs"]:
            raise LiveError("unsupported_operation", "Shared repair requires protected project policy", 403)
        if parts == ["api", "issues"] and self.command == "GET":
            return self.respond(200, {"issues": self._visible_issues(principal)})
        if len(parts) == 3 and parts[:2] == ["api", "issues"] and self.command == "GET":
            binding = access.authorize_resource(principal, "issue", parts[2], "issue.read")
            check(self.server.issue_service is not None, "not_found", "Issue not found", 404)
            return self.respond(200, {"issue": self.server.issue_service.get(binding.resource_id)})
        if len(parts) >= 3 and parts[:2] == ["api", "sessions"]:
            return self._shared_session(parts, principal, body)
        raise LiveError("not_found", "Resource not found", 404)

    def _dispatch_admin(self, parts, principal, body):
        store = self.server.access.store
        store.authorize(principal, None, "administration.read")
        if self.command == "GET" and len(parts) == 1:
            methods = {
                "identities": store.list_identities, "projects": store.list_projects,
                "memberships": store.list_memberships, "credentials": store.list_credentials,
                "enrollments": store.list_host_enrollments,
                "hosts": store.list_hosts, "devices": store.list_device_assignments,
                "adoptions": store.list_legacy_adoptions,
            }
            if parts[0] in methods:
                return self.respond(200, {parts[0]: methods[parts[0]]()})
        actor = principal.principal_id
        if parts == ["identities"] and self.command == "POST":
            check(set(body) == {"identityId", "administrator"}
                  and type(body["administrator"]) is bool,
                  "invalid_argument", "Invalid identity request", 400)
            return self.respond(201, {"identity": store.create_identity(
                actor, body["identityId"], administrator=body["administrator"])})
        if parts == ["memberships"] and self.command == "POST":
            check(set(body) == {"projectId", "identityId", "role", "active"}
                  and type(body["active"]) is bool,
                  "invalid_argument", "Invalid membership request", 400)
            if body["active"]:
                result = store.grant_membership(
                    actor, body["projectId"], body["identityId"], body["role"])
            else:
                store.revoke_membership(
                    actor, body["projectId"], body["identityId"], body["role"])
                result = dict(body)
            return self.respond(200, {"membership": result})
        if len(parts) == 3 and parts[0] == "credentials" and parts[2] == "revoke" \
                and self.command == "POST":
            check(not body, "invalid_argument", "Revocation body must be empty", 400)
            store.revoke_credential(actor, parts[1])
            return self.respond(200, {"credentialId": parts[1], "revoked": True})
        if len(parts) == 3 and parts[0] == "enrollments" and parts[2] == "revoke" \
                and self.command == "POST":
            check(not body, "invalid_argument", "Revocation body must be empty", 400)
            store.revoke_host_enrollment(actor, parts[1])
            return self.respond(200, {"enrollmentId": parts[1], "revoked": True})
        if len(parts) == 3 and parts[0] == "hosts" and parts[2] == "revoke" \
                and self.command == "POST":
            check(not body, "invalid_argument", "Revocation body must be empty", 400)
            store.revoke_host(actor, parts[1])
            return self.respond(200, {"hostId": parts[1], "revoked": True})
        if parts == ["devices", "assign"] and self.command == "POST":
            expected = {"deviceId", "projectId", "trustGroup", "hostId", "sanitationReceipt"}
            check(set(body) == expected, "invalid_argument", "Invalid device assignment", 400)
            device = self.server.lab.devices.get(body["deviceId"])
            check(device is not None and device["state"] == "available",
                  "device_busy", "Device must be available before assignment", 409)
            result = store.assign_device(
                actor, body["deviceId"], project_id=body["projectId"],
                trust_group=body["trustGroup"], host_id=body["hostId"],
                sanitation_receipt=body["sanitationReceipt"])
            return self.respond(200, {"assignment": result})
        if parts == ["projects"] and self.command == "POST":
            raise LiveError("trusted_registration_required",
                            "Project registration requires the local administrator CLI", 403)
        raise LiveError("not_found", "Administration route not found", 404)

    def _create_shared_session(self, principal, body):
        expected = {"deviceId", "clientId", "projectId", "applicationId", "buildId"}
        check(set(body) == expected, "invalid_argument", "Invalid shared session request", 400)
        access = self.server.access
        registration = access.registration(body["projectId"])
        access.authorize_device(principal, body["deviceId"], body["projectId"], "session.create")
        device = self.server.lab.devices.get(body["deviceId"])
        check(device is not None, "not_found", "Device not found", 404)
        self.server.lab._validate_release_selection(
            body["deviceId"], registration, body["applicationId"], body["buildId"])
        check(device["state"] == "available", "device_busy",
              "Device is already allocated", 409)
        session_id = uuid.uuid4().hex
        recording_id = "recording_" + uuid.uuid4().hex
        authorizer = access.effect_authorizer(
            principal, project_id=body["projectId"], device_id=body["deviceId"],
            project_digest=registration.project_digest)
        reservation = None
        try:
            if device.get("_remoteAuthority") is True:
                authorizer("physical-reservation")
                reservation = self.server.lab.reserve_release_device(
                    body["deviceId"], principal.principal_id,
                    "reservation_" + uuid.uuid4().hex, registration,
                    application_id=body["applicationId"], build_id=body["buildId"])
                authorizer("physical-reservation")
            access.bind_resource("session", session_id, body["projectId"],
                                 principal.principal_id, meaning="release")
            access.bind_resource("recording", recording_id, body["projectId"],
                                 principal.principal_id, meaning="release")
            session = self.server.lab.create_release_session(
                body["deviceId"], principal.principal_id, body["clientId"], registration,
                application_id=body["applicationId"], build_id=body["buildId"],
                preparation_receipts=[], session_id=session_id,
                recording_id=recording_id, effect_authorizer=authorizer,
                device_reservation=reservation)
        except Exception:
            if reservation is not None \
                    and reservation.reservation_id in self.server.lab._device_reservations:
                try:self.server.lab.release_device_reservation(reservation)
                except Exception:pass
            raise
        return self.respond(201, {"session": session})

    @staticmethod
    def _owned(binding, principal):
        if binding.owner_id != principal.principal_id:
            raise LiveError("owner_forbidden", "Only the resource owner may mutate it", 403)

    def _shared_session(self, parts, principal, body):
        access = self.server.access
        session_id = parts[2]
        action = "/".join(parts[3:])
        read_capability = "media.read" if action in {"frame", "stream"} else "session.read"
        capability = read_capability if self.command == "GET" else (
            "replay.execute" if action.startswith("replay") else
            "recording.create" if action.startswith("recordings/") else "session.operate")
        if action in {"observe", "app-logs", "app-logs/export"}:
            capability = "evidence.collect"
        cleanup = self.command == "POST" and action in {"close", "replay/cancel"}
        binding = access.authorize_resource(
            principal, "session", session_id, capability,
            executable=(self.command == "POST" and not cleanup) or capability == "evidence.collect")
        owner = binding.owner_id
        if capability == "evidence.collect":
            self._owned(binding, principal)
        if self.command == "GET":
            if action == "":
                return self.respond(200, {"session": self.server.lab.peek_session(session_id, owner)})
            if action == "events":
                return self.respond(200, {"events": self.server.lab.session_events(session_id, owner)})
            if action == "frame":
                return self.respond(200, self.server.lab.frame(session_id, owner))
            if action == "observe":
                return self.respond(200, {"observation": self.server.lab.observe(session_id, owner)})
            if action in {"app-logs", "app-logs/export"}:
                value = self.server.lab.app_logs(session_id, owner)
                if action.endswith("/export"):
                    return self.respond(200, value, download="app-log.json")
                return self.respond(200, {"appLog": value,
                    "collectionFailed": bool(self.server.lab._session(
                        session_id, owner).get("appLogError"))})
            if action == "stream":
                from .media import serve_frames

                def revalidate():
                    access.authorize_resource(principal, "session", session_id, "media.read")
                    return True

                return serve_frames(self, self.server.lab, session_id, owner,
                                    authorization=revalidate, renew=False)
            raise LiveError("method_not_allowed", "Session operation requires POST", 405)
        self._owned(binding, principal)
        controller = body.get("controllerId")
        epoch = body.get("epoch")
        if action == "heartbeat":
            check(set(body) == {"clientId"}, "invalid_argument",
                  "Expected a heartbeat client ID", 400)
            result = {"session": self.server.lab.heartbeat(session_id, owner, body["clientId"])}
        elif action == "control":
            result = {"session": self.server.lab.claim(
                session_id, owner, body.get("clientId"), body.get("expectedEpoch"),
                body.get("mode", "manual"))}
        elif action == "input":
            recording_input = body.pop("recordingInput", None)
            command = body
            if recording_input is None:
                recording_input = self._recording_input(session_id, owner, command)
            result = {"receipt": self.server.lab.input(
                          session_id, owner, command,
                          recording_input=recording_input),
                      "session": self.server.lab.peek_session(session_id, owner)}
        elif action == "close":
            check(isinstance(controller, str) and type(epoch) is int,
                  "invalid_argument", "Controller and epoch required", 400)
            result = {"session": self.server.lab.close_session(
                session_id, owner, controller, epoch)}
        elif action == "recordings/stop":
            result = {"recording": self.server.lab.stop_release_recording(
                session_id, owner, controller, epoch)}
        elif action == "recordings/start":
            raise LiveError("unsupported_operation",
                            "Shared sessions start durable recording at creation", 409)
        elif action == "replay":
            recording_id = body.get("recordingId")
            source = access.authorize_resource(
                principal, "recording", recording_id, "replay.execute", executable=True)
            check(source.project_id == binding.project_id and source.owner_id == owner,
                  "forbidden", "Replay resource scope does not match the session", 403)
            self._recording(source)
            result = {"replay": self.server.lab.start_replay(
                session_id, owner, controller, epoch, recording_id, body.get("variables")),
                "session": self.server.lab.peek_session(session_id, owner)}
        elif action == "replay/cancel":
            result = {"replay": self.server.lab.cancel_replay(session_id, owner),
                      "session": self.server.lab.peek_session(session_id, owner)}
        elif action == "repair":
            raise LiveError("unsupported_operation", "Shared repair requires protected project policy", 403)
        else:
            raise LiveError("not_found", "Operation not found", 404)
        return self.respond(200, result)

    def _recording_input(self, session_id, owner, command):
        action = command.get("action")
        payload = command.get("payload")
        check(isinstance(payload, dict), "invalid_argument",
              "Shared input requires a recording input", 400)
        if action == "text":
            raise LiveError("recording_input_required",
                            "Shared text input requires a registered variable reference", 400)
        if action in {"tap", "long_press", "swipe", "pointer"}:
            if action == "pointer" and payload.get("phase") == "cancel":
                geometry = None
            else:
                frame = self.server.lab.frame(session_id, owner)
                geometry = {"width": frame["width"], "height": frame["height"],
                            "rotation": 0 if frame["orientation"] == "portrait" else 90,
                            "version": frame["geometryVersion"]}
                if isinstance(frame.get("objectDigest"), str):
                    geometry["frameDigest"] = frame["objectDigest"]
            translated = {"action": {"long_press": "long-press"}.get(action, action)}
            if action == "tap":
                parameters = {"x": payload.get("x"), "y": payload.get("y")}
            elif action == "long_press":
                parameters = {"x": payload.get("x"), "y": payload.get("y"),
                              "durationMs": payload.get("durationMs")}
            elif action == "swipe":
                parameters = {"x": payload.get("fromX"), "y": payload.get("fromY"),
                              "x2": payload.get("toX"), "y2": payload.get("toY"),
                              "durationMs": payload.get("durationMs")}
            else:
                parameters = {key: payload.get(key) for key in
                              ("phase", "pointerId", "x", "y")}
            translated["parameters"] = parameters
            if geometry is not None:
                translated["geometry"] = geometry
            return translated
        if action in {"back", "home"}:
            return {"action": action, "parameters": {}}
        if action == "rotate":
            return {"action": action, "parameters": {"orientation": payload.get("orientation")}}
        if action in {"launch", "terminate"}:
            return {"action": action,
                    "parameters": {"applicationId": payload.get("applicationId")}}
        raise LiveError("recording_input_required",
                        "Shared input has no durable recording form", 400)

    def _recording(self, binding):
        if binding.meaning == "release":
            return self.server.lab.release_recording(binding.resource_id, binding.owner_id)
        recording = self.server.lab.recording(binding.resource_id, binding.owner_id)
        if binding.meaning == "legacy-authorized":
            check(recording.get("digest") == binding.authorized_digest,
                  "resource_changed", "Authorized legacy recording has changed", 409)
        return recording

    def _visible_sessions(self, principal):
        result = []
        for binding in self.server.access.visible_bindings(principal, "session", "session.read"):
            try:
                result.append(self.server.lab.peek_session(binding.resource_id, binding.owner_id))
            except LiveError:
                continue
        return sorted(result, key=lambda item: item["createdAt"], reverse=True)

    def _visible_recordings(self, principal):
        result = []
        for binding in self.server.access.visible_bindings(principal, "recording", "recording.read"):
            try:
                result.append(self._recording(binding))
            except LiveError:
                continue
        return sorted(result, key=lambda item: item.get("endedAt", item.get("startedAt", 0)),
                      reverse=True)

    def _visible_jobs(self, principal):
        result = []
        for binding in self.server.access.visible_bindings(principal, "job", "job.read"):
            try:
                result.append(self.server.jobs.get(binding.resource_id, binding.owner_id))
            except LiveError:
                continue
        return sorted(result, key=lambda item: item["createdAt"], reverse=True)

    def _visible_issues(self, principal):
        if self.server.issue_service is None:
            return []
        result = []
        for binding in self.server.access.visible_bindings(principal, "issue", "issue.read"):
            try:
                result.append(self.server.issue_service.get(binding.resource_id))
            except Exception:
                continue
        return result

    def _dispatch_legacy(self, parts, lab):
        if self.command == "OPTIONS":
            return self.respond(405, {"error": {"code": "method_not_allowed",
                                                "message": "Use same-origin requests"}})
        if self.command not in {"GET", "POST"}:
            raise LiveError("method_not_allowed", "Method is not allowed", 405)
        supplied = self._cookies().get("repro_live")
        check(supplied is not None and secrets.compare_digest(
            supplied.value, self.server.browser_token),
            "unauthorized", "Open the console first", 401)
        owner = "local-owner"
        body = self.body() if self.command == "POST" else {}
        if parts == ["api", "health"] and self.command == "GET":
            devices = lab.list_devices();jobs = self.server.jobs.list(owner)
            return self.respond(200, {"status": "ok", "devices": {
                state: sum(item["state"] == state for item in devices)
                for state in ("available", "busy", "quarantined", "recovering", "repairing")},
                "jobs": {state: sum(item["state"] == state for item in jobs)
                         for state in ("queued", "starting", "running", "cleaning", "succeeded",
                                       "failed", "cancelled", "interrupted")},
                "recordingLoadErrors": len(lab.recording_load_errors),
                "idleTimeoutSeconds": lab.idle_timeout,
                "maxSessionSeconds": lab.max_session_seconds})
        if parts == ["api", "sessions"] and self.command == "GET":
            return self.respond(200, {"sessions": lab.list_sessions(owner)})
        if len(parts) == 4 and parts[:2] == ["api", "devices"] and parts[3] == "recover" and self.command == "POST":
            check(not body, "invalid_argument", "Recovery does not accept parameters", 400)
            return self.respond(200, {"device": lab.recover_device(parts[2])})
        if parts == ["api", "recordings", "import"] and self.command == "POST":
            check(set(body) == {"recording"}, "invalid_argument", "Expected a recording document", 400)
            result = lab.import_recording(body["recording"], owner)
            return self.respond(201 if result["imported"] else 200, result)
        if len(parts) == 4 and parts[:2] == ["api", "recordings"] and parts[3] == "derive" and self.command == "POST":
            check(set(body) <= {"eventIds", "speed"}, "invalid_argument", "Unknown recording transform", 400)
            return self.respond(201, {"recording": lab.derive_recording(
                parts[2], owner, event_ids=body.get("eventIds"), speed=body.get("speed", 1))})
        if parts == ["api", "jobs"]:
            if self.command == "GET":
                return self.respond(200, {"jobs": self.server.jobs.list(owner)})
            return self.respond(202, {"job": self.server.jobs.submit(owner, body)})
        if parts == ["api", "repairs"] and self.command == "GET":
            return self.respond(200, {"enabled": self.server.repairs is not None,
                "jobs": self.server.repairs.list(owner) if self.server.repairs else []})
        if len(parts) in {3, 4} and parts[:2] == ["api", "repairs"]:
            check(self.server.repairs is not None, "unsupported_operation",
                  "Repair is not configured on this server", 400)
            repairs = self.server.repairs
            if len(parts) == 3 and self.command == "GET":
                return self.respond(200, {"job": repairs.get(parts[2], owner)})
            if len(parts) == 4 and parts[3] == "cancel" and self.command == "POST":
                check(not body, "invalid_argument", "Cancel does not accept parameters", 400)
                return self.respond(200, {"job": repairs.cancel(parts[2], owner)})
            if len(parts) == 4 and parts[3] == "resume" and self.command == "POST":
                check(set(body) == {"clientId"}, "invalid_argument", "Expected a client ID", 400)
                return self.respond(201, {"session": repairs.resume(parts[2], owner, body["clientId"])})
            if len(parts) == 4 and parts[3] == "report" and self.command == "GET":
                return self.respond(200, repairs.report(parts[2], owner), "text/html; charset=utf-8")
        if len(parts) in {3, 4} and parts[:2] == ["api", "jobs"]:
            if len(parts) == 3 and self.command == "GET":
                return self.respond(200, {"job": self.server.jobs.get(parts[2], owner)})
            if len(parts) == 4 and parts[3] == "cancel" and self.command == "POST":
                check(not body, "invalid_argument", "Cancel does not accept parameters", 400)
                return self.respond(200, {"job": self.server.jobs.cancel(parts[2], owner)})
            if len(parts) == 4 and parts[3] == "report" and self.command == "GET":
                return self.respond(200, {"schemaVersion": 1, "kind": "live-replay-job-report",
                    "job": self.server.jobs.get(parts[2], owner)}, download="job-report.json")
        if parts == ["api", "devices"] and self.command == "GET":
            return self.respond(200, {"devices": lab.list_devices()})
        if parts == ["api", "recordings"] and self.command == "GET":
            return self.respond(200, {"recordings": lab.list_recordings(owner)})
        if parts == ["api", "sessions"] and self.command == "POST":
            return self.respond(201, {"session": lab.create_session(
                body.get("deviceId"), owner, body.get("clientId"))})
        if len(parts) in {3, 4} and parts[:2] == ["api", "recordings"] and self.command == "GET":
            recording = lab.recording(parts[2], owner)
            if len(parts) == 3:
                return self.respond(200, {"recording": recording})
            if parts[3] == "export":
                return self.respond(200, recording, download=True)
            if parts[3] == "script":
                from .client import export_script
                return self.respond(200, export_script(recording),
                                    "text/x-python; charset=utf-8", download="replay.py")
        if len(parts) >= 3 and parts[:2] == ["api", "sessions"]:
            sid = parts[2];action = "/".join(parts[3:])
            controller = body.get("controllerId");epoch = body.get("epoch")
            if self.command == "GET":
                if action == "":
                    return self.respond(200, {"session": lab.get_session(sid, owner)})
                if action == "observe":
                    return self.respond(200, {"observation": lab.observe(sid, owner)})
                if action == "events":
                    return self.respond(200, {"events": lab.session_events(sid, owner)})
                if action in {"app-logs", "app-logs/export"}:
                    value = lab.app_logs(sid, owner)
                    if action.endswith("/export"):
                        return self.respond(200, value, download="app-log.json")
                    return self.respond(200, {"appLog": value,
                        "collectionFailed": bool(lab._session(sid, owner).get("appLogError"))})
                if action == "stream":
                    from .media import serve_frames
                    return serve_frames(self, lab, sid, owner)
                if action == "frame":
                    return self.respond(200, lab.frame(sid, owner))
            if self.command == "POST":
                if action == "heartbeat":
                    check(set(body) == {"clientId"}, "invalid_argument", "Expected a heartbeat client ID", 400)
                    result = {"session": lab.heartbeat(sid, owner, body["clientId"])}
                elif action == "control":
                    result = {"session": lab.claim(sid, owner, body.get("clientId"),
                                                    body.get("expectedEpoch"), body.get("mode", "manual"))}
                elif action == "input":
                    result = {"receipt": lab.input(sid, owner, body),
                              "session": lab.get_session(sid, owner)}
                elif action == "close":
                    check(isinstance(controller, str) and type(epoch) is int,
                          "invalid_argument", "Controller and epoch required", 400)
                    result = {"session": lab.close_session(sid, owner, controller, epoch)}
                elif action == "recordings/start":
                    result = {"recording": lab.start_recording(
                        sid, owner, controller, epoch, body.get("reset") is True)}
                elif action == "recordings/stop":
                    result = {"recording": lab.stop_recording(sid, owner, controller, epoch)}
                elif action == "repair":
                    check(self.server.repairs is not None, "unsupported_operation",
                          "Repair is not configured on this server", 400)
                    check(set(body) == {"controllerId", "epoch", "recordingId", "requestId"},
                          "invalid_argument", "Invalid repair request", 400)
                    result = {"job": self.server.repairs.submit(
                        sid, owner, controller, epoch, body["recordingId"], body["requestId"])}
                elif action == "replay":
                    result = {"replay": lab.start_replay(
                        sid, owner, controller, epoch, body.get("recordingId"), body.get("variables")),
                        "session": lab.get_session(sid, owner)}
                elif action == "replay/cancel":
                    result = {"replay": lab.cancel_replay(sid, owner),
                              "session": lab.get_session(sid, owner)}
                else:
                    raise LiveError("not_found", "Operation not found", 404)
                return self.respond(200, result)
        raise LiveError("not_found", "Resource not found", 404)


__all__ = ["Handler", "LiveServer"]
