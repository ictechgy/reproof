import copy
import http.client
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest

from reproof.live.access import AccessController, AccessError, AccessStore
from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.enrollment import EnrollmentClient
from reproof.live.jobs import JobQueue
from reproof.live.model import Lab, LiveError
from reproof.live.server import LiveServer
from reproof.core import ContractError
from tests.test_fixture_allocations import collection_policy, project_document
from tests.test_clock_sync import FakeClock


class ProjectProvider:
    def __init__(self, calls):
        self.calls = calls

    def start(self, session, lab):
        self.session = session
        self.lab = lab
        self.closed = False
        lab.publish_frame(session["id"], b"<svg>one</svg>", "image/svg+xml", 320, 640)

    def execute_operation(self, action, payload, *, operation_id, frame=None):
        self.calls.append((action, copy.deepcopy(payload)))
        self.lab.publish_frame(self.session["id"], b"<svg>two</svg>", "image/svg+xml", 320, 640)
        return {"ok": True, "timing": "best-effort"}

    def observe(self):
        self.calls.append(("observe", {}))
        return {"nodes": []}

    def close(self):
        self.closed = True


def device(calls):
    return {
        "id": "shared-device", "name": "Shared synthetic", "platform": "ios",
        "kind": "demo", "factory": lambda: ProjectProvider(calls),
        "capabilities": {
            "actions": ["tap"], "inputMode": "gesture-batch", "media": "demo-svg",
            "applicationIdentity": {"bundle": "com.example.app", "artifactDigest": "0" * 64},
        },
    }


class BlockingControl:
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False


class BlockingProvider:
    def __init__(self, control):
        self.control = control

    def start(self, session, lab):
        self.session = session
        self.lab = lab
        lab.publish_frame(session["id"], b"<svg/>", "image/svg+xml", 320, 640, "portrait")

    def execute_operation(self, action, payload, *, operation_id, frame=None):
        self.control.calls.append(action)
        if self.control.block and action == "tap":
            self.control.entered.set()
            self.control.release.wait(5)
        self.lab.publish_frame(
            self.session["id"], b"<svg/>", "image/svg+xml", 320, 640, "portrait")
        return {"ok": True, "timing": "best-effort"}

    def close(self):
        return None


def blocking_device(control):
    value = device([])
    value["factory"] = lambda: BlockingProvider(control)
    value["capabilities"]["actions"] = ["tap", "reset"]
    value["capabilities"]["resetContract"] = "synthetic-v1"
    return value


class SharedHttpFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = []
        self.store = AccessStore(self.root / "coordinator-v2")
        self.store.bootstrap_administrator("root-admin")
        self.store.create_identity("root-admin", "viewer")
        self.store.create_identity("root-admin", "operator")
        self.project = project_document()
        self.store.register_project("root-admin", self.project)
        self.store.grant_membership("root-admin", "checkout", "viewer", "viewer")
        self.store.grant_membership("root-admin", "checkout", "operator", "operator")
        self.store.assign_device("root-admin", "shared-device", project_id="checkout")
        self.viewer_token = self.store.issue_principal_credential(
            "root-admin", "viewer", lifetime_seconds=600)["token"]
        self.operator_token = self.store.issue_principal_credential(
            "root-admin", "operator", lifetime_seconds=600)["token"]
        self.lab = Lab([device(self.calls)], self.root / "lab",
                       recording_clock_sync=ClockSynchronizer(FakeClock()),
                       recording_wall_clock_ms=lambda: int(time.time() * 1000))
        self.registration = self.lab.register_recording_project(
            self.project, collection_policy(), capacity_bytes=64 * 1024 * 1024,
            journal_headroom_bytes=512 * 1024)
        self.access = AccessController(self.store)
        self.access.bind_project(self.registration)
        self.server = LiveServer(self.lab, access=self.access)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.close_operations()
        self.lab.close_all()
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, path, *, method="GET", body=None, token=None, cookie=None,
                csrf=None, headers=None, read=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        values = {"Host": f"127.0.0.1:{self.server.server_port}"}
        if body is not None:
            values["Content-Type"] = "application/json"
        if token:
            values["Authorization"] = "Bearer " + token
        if cookie:
            values["Cookie"] = cookie
        if csrf:
            values["X-Repro-CSRF"] = csrf
        values.update(headers or {})
        payload = None if body is None else json.dumps(body)
        connection.request(method, path, payload, values)
        response = connection.getresponse()
        data = response.read() if read else response
        if not read:
            return connection, response
        mime = response.getheader("Content-Type", "")
        result = json.loads(data) if "json" in mime and data else data
        return response.status, result, response.getheader("Set-Cookie")

    def browser_session(self, token):
        status, value, cookie = self.request(
            "/api/auth/session", method="POST", body={}, token=token)
        if status != 201:
            raise AssertionError(value)
        return cookie.split(";", 1)[0], value["csrfToken"]

    def create_session(self):
        cookie, csrf = self.browser_session(self.operator_token)
        status, value, _ = self.request(
            "/api/sessions", method="POST", cookie=cookie, csrf=csrf,
            body={"deviceId": "shared-device", "clientId": "browser",
                  "projectId": "checkout", "applicationId": "ios_app",
                  "buildId": "original"})
        if status != 201:
            raise AssertionError(value)
        return value["session"], cookie, csrf


class ProjectAccessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AccessStore(Path(self.temp.name) / "coordinator-v2")
        self.store.bootstrap_administrator("admin")
        self.store.create_identity("admin", "person")
        self.store.register_project("admin", project_document())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_roles_are_separate_and_membership_revocation_is_immediate(self):
        self.store.grant_membership("admin", "checkout", "person", "viewer")
        token = self.store.issue_principal_credential(
            "admin", "person", lifetime_seconds=300)["token"]
        principal = self.store.authenticate_principal(token)
        self.store.authorize(principal, "checkout", "session.read")
        with self.assertRaises(AccessError):
            self.store.authorize(principal, "checkout", "session.operate")
        self.store.revoke_membership("admin", "checkout", "person", "viewer")
        with self.assertRaises(AccessError):
            self.store.authorize(principal, "checkout", "session.read")

    def test_administrator_role_does_not_implicitly_operate_projects(self):
        token = self.store.issue_principal_credential(
            "admin", "admin", lifetime_seconds=300)["token"]
        principal = self.store.authenticate_principal(token)
        self.store.authorize(principal, None, "membership.manage")
        with self.assertRaises(AccessError):
            self.store.authorize(principal, "checkout", "device.operate")
        with self.assertRaises(ContractError):
            self.store.grant_membership(
                "admin", "checkout", "person", "administrator")

    def test_capability_matrix_separates_all_four_roles(self):
        expected = {
            "viewer": ("session.read", "device.operate"),
            "operator": ("device.operate", "export.read"),
            "maintainer": ("project.maintain", "device.operate"),
        }
        for role, (allowed, denied) in expected.items():
            identity = "person-" + role
            self.store.create_identity("admin", identity)
            self.store.grant_membership("admin", "checkout", identity, role)
            token = self.store.issue_principal_credential(
                "admin", identity, lifetime_seconds=300)["token"]
            principal = self.store.authenticate_principal(token)
            self.store.authorize(principal, "checkout", allowed)
            with self.assertRaises(AccessError):
                self.store.authorize(principal, "checkout", denied)

        administrator = self.store.authenticate_principal(
            self.store.issue_principal_credential(
                "admin", "admin", lifetime_seconds=300)["token"])
        self.store.authorize(administrator, None, "host.manage")
        with self.assertRaises(AccessError):
            self.store.authorize(administrator, "checkout", "session.read")

    def test_bootstrap_closes_after_first_identity(self):
        with self.assertRaises(AccessError) as raised:
            self.store.bootstrap_administrator("replacement")
        self.assertEqual(raised.exception.code, "bootstrap_denied")

    def test_browser_session_expires_and_tracks_credential_revocation(self):
        clock = [1_000]
        other = AccessStore(
            Path(self.temp.name) / "expiring" / "coordinator-v2",
            clock=lambda: clock[0])
        try:
            other.bootstrap_administrator("root")
            issued = other.issue_principal_credential(
                "root", "root", lifetime_seconds=60)
            controller = AccessController(other, browser_session_seconds=60)
            session = controller.create_browser_session(issued["token"])
            controller.authenticate_browser(session["cookie"])
            other.revoke_credential("root", issued["credentialId"])
            with self.assertRaises(AccessError):
                controller.authenticate_browser(session["cookie"])

            replacement = other.issue_principal_credential(
                "root", "root", lifetime_seconds=60)
            expiring = controller.create_browser_session(replacement["token"])
            clock[0] += 61
            with self.assertRaises(AccessError):
                controller.authenticate_browser(expiring["cookie"])
        finally:
            other.close()

    def test_legacy_execution_requires_adoption_and_digest_promotion(self):
        self.store.create_identity("admin", "maintainer")
        self.store.grant_membership("admin", "checkout", "maintainer", "maintainer")
        principal = self.store.authenticate_principal(
            self.store.issue_principal_credential(
                "admin", "maintainer", lifetime_seconds=300)["token"])
        project = self.store.project("checkout")
        self.store.bind_resource(
            "recording", "legacy-recording", "checkout",
            project["projectDigest"], "maintainer", meaning="legacy-inert")
        with self.assertRaises(AccessError):
            self.store.authorize_resource(
                principal, "recording", "legacy-recording",
                "recording.read", executable=True)

        source = Path(self.temp.name) / "recording.json"
        source.write_bytes(b'{"kind":"legacy-recording"}\n')
        self.store.adopt_legacy(
            "adopted-recording", source, original_format="recording-v1",
            meaning="legacy-recording-only")
        digest = "a" * 64
        promoted = self.store.authorize_legacy_resource(
            "admin", "recording", "legacy-recording",
            adoption_id="adopted-recording", resource_digest=digest)
        self.assertEqual(promoted.meaning, "legacy-authorized")
        self.assertEqual(promoted.authorized_digest, digest)
        self.store.authorize_resource(
            principal, "recording", "legacy-recording",
            "recording.read", executable=True)
        rebound = self.store.bind_resource(
            "recording", "legacy-recording", "checkout",
            project["projectDigest"], "maintainer", meaning="legacy-inert")
        self.assertEqual(rebound.meaning, "legacy-authorized")


class ProjectAccessHttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SharedHttpFixture()

    def tearDown(self):
        self.fixture.close()

    def test_shared_console_does_not_issue_legacy_owner_cookie_and_requires_auth(self):
        status, _, cookie = self.fixture.request("/")
        self.assertEqual(status, 200)
        self.assertIsNone(cookie)
        self.assertEqual(self.fixture.request("/api/health")[0], 401)
        self.assertEqual(
            self.fixture.request("/api/health", token=self.fixture.viewer_token)[0], 200)
        self.assertEqual(self.fixture.request(
            "/api/admin/projects", token=self.fixture.operator_token)[0], 403)
        administrator_token = self.fixture.store.issue_principal_credential(
            "root-admin", "root-admin", lifetime_seconds=300)["token"]
        self.assertEqual(
            self.fixture.request("/api/health", token=administrator_token)[0], 403)
        for path in ("/api/hosts/enroll", "/api/hosts/authenticate",
                     "/api/hosts/authorize", "/api/auth/session"):
            with self.subTest(path=path):
                self.assertEqual(self.fixture.request(
                    path, method="POST", body={})[0], 401)

    def test_viewer_read_does_not_renew_or_collect_and_operator_can_input(self):
        session, _, _ = self.fixture.create_session()
        raw = self.fixture.lab._session(session["id"])
        before = raw["lastActivity"]
        viewer_cookie, _ = self.fixture.browser_session(self.fixture.viewer_token)
        self.assertEqual(self.fixture.request(
            f"/api/sessions/{session['id']}", cookie=viewer_cookie)[0], 200)
        self.assertEqual(raw["lastActivity"], before)
        self.assertEqual(self.fixture.request(
            f"/api/sessions/{session['id']}/observe", cookie=viewer_cookie)[0], 403)
        self.assertEqual(self.fixture.calls, [])

        operator_cookie, operator_csrf = self.fixture.browser_session(self.fixture.operator_token)
        frame = self.fixture.request(
            f"/api/sessions/{session['id']}/frame", cookie=operator_cookie)[1]
        command = {
            "controllerId": "browser", "epoch": session["epoch"], "sequence": 1,
            "commandId": "input-one", "frameId": frame["id"],
            "geometryVersion": frame["geometryVersion"], "action": "tap",
            "payload": {"x": .5, "y": .5},
        }
        self.assertEqual(self.fixture.request(
            f"/api/sessions/{session['id']}/input", method="POST", body=command,
            cookie=operator_cookie, csrf=operator_csrf)[0], 200)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_csrf_and_cross_project_ids_fail_before_provider_effects(self):
        session, cookie, csrf = self.fixture.create_session()
        frame = self.fixture.request(f"/api/sessions/{session['id']}/frame", cookie=cookie)[1]
        command = {
            "controllerId": "browser", "epoch": session["epoch"], "sequence": 1,
            "commandId": "csrf-blocked", "frameId": frame["id"],
            "geometryVersion": frame["geometryVersion"], "action": "tap",
            "payload": {"x": .5, "y": .5},
        }
        self.assertEqual(self.fixture.request(
            f"/api/sessions/{session['id']}/input", method="POST", body=command,
            cookie=cookie)[0], 403)
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual(self.fixture.request(
            f"/api/sessions/{session['id']}/input", method="POST", body=command,
            cookie=cookie, csrf=csrf, headers={"Host": "attacker.invalid"})[0], 403)
        self.assertEqual(self.fixture.request(
            f"/api/sessions/{session['id']}/input", method="POST", body=command,
            cookie=cookie, csrf=csrf,
            headers={"Origin": "https://attacker.invalid"})[0], 403)
        self.assertEqual(self.fixture.calls, [])

        other = copy.deepcopy(project_document())
        other.update(id="other", revision="r2", trustGroup="other-group")
        self.fixture.store.register_project("root-admin", other)
        other_registration = self.fixture.lab.register_recording_project(
            other, collection_policy())
        self.fixture.access.bind_project(other_registration)
        self.fixture.store.grant_membership("root-admin", "other", "operator", "operator")
        self.assertEqual(self.fixture.request(
            "/api/sessions", method="POST", body={"deviceId": "shared-device",
            "clientId": "other", "projectId": "other", "applicationId": "ios_app",
            "buildId": "original"}, cookie=cookie, csrf=csrf)[0], 403)
        self.assertEqual(self.fixture.calls, [])

    def test_unauthorized_range_export_and_report_do_not_leak_objects(self):
        session, _, _ = self.fixture.create_session()
        recording_id = session["releaseRecordingId"]
        headers = {"Range": "bytes=0-10"}
        self.assertEqual(self.fixture.request(
            f"/api/recordings/{recording_id}/export", headers=headers)[0], 401)
        self.assertEqual(self.fixture.request(
            "/api/jobs/not-visible/report", headers=headers)[0], 401)

    def test_cross_project_object_and_list_are_filtered_before_lookup(self):
        session, _, _ = self.fixture.create_session()
        other = copy.deepcopy(project_document())
        other.update(id="other", revision="r2", trustGroup="other-group")
        self.fixture.store.register_project("root-admin", other)
        self.fixture.store.create_identity("root-admin", "other-viewer")
        self.fixture.store.grant_membership(
            "root-admin", "other", "other-viewer", "viewer")
        self.fixture.access.bind_project(self.fixture.lab.register_recording_project(
            other, collection_policy()))
        token = self.fixture.store.issue_principal_credential(
            "root-admin", "other-viewer", lifetime_seconds=300)["token"]
        cookie, _ = self.fixture.browser_session(token)

        status, value, _ = self.fixture.request("/api/sessions", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(value["sessions"], [])
        self.assertEqual(self.fixture.request(
            f"/api/sessions/{session['id']}", cookie=cookie)[0], 404)

    def test_membership_revocation_stops_an_active_stream_before_next_frame(self):
        session, _, _ = self.fixture.create_session()
        viewer_cookie, _ = self.fixture.browser_session(self.fixture.viewer_token)
        connection, response = self.fixture.request(
            f"/api/sessions/{session['id']}/stream", cookie=viewer_cookie, read=False)
        self.assertEqual(response.status, 200)
        header = response.read(8)
        metadata_size, image_size = struct.unpack(">II", header)
        first = json.loads(response.read(metadata_size))
        response.read(image_size)
        self.assertEqual(first["type"], "frame")

        self.fixture.store.revoke_membership(
            "root-admin", "checkout", "viewer", "viewer")
        self.fixture.lab.publish_frame(
            session["id"], b"<svg>revoked</svg>", "image/svg+xml", 320, 640)
        response.fp.raw._sock.settimeout(2)
        remainder = response.read()
        response.close()
        connection.close()
        self.assertEqual(remainder, b"")

    def test_host_enrollment_http_is_single_use_and_revocable(self):
        issued = self.fixture.store.create_host_enrollment(
            "root-admin", host_id="worker-one", project_ids=["checkout"],
            trust_groups=[], lifetime_seconds=300,
            credential_lifetime_seconds=600)
        client = EnrollmentClient(
            f"http://127.0.0.1:{self.fixture.server.server_port}")
        enrolled = client.enroll(
            issued["token"], host_id="worker-one", incarnation="boot-one")
        current = client.authenticate(enrolled["credential"])
        self.assertEqual(current["generation"], 1)
        self.assertEqual(current["projectIds"], ["checkout"])
        self.assertTrue(client.authorize(
            enrolled["credential"], project_id="checkout")["authorized"])
        with self.assertRaises(AccessError):
            client.authorize(enrolled["credential"], project_id="unknown")
        with self.assertRaises(AccessError):
            client.enroll(
                issued["token"], host_id="worker-one", incarnation="boot-one")
        self.fixture.store.revoke_host("root-admin", "worker-one")
        with self.assertRaises(AccessError):
            client.authenticate(enrolled["credential"])

    def test_remote_session_refuses_to_substitute_for_g6_reservation(self):
        self.fixture.lab.devices["shared-device"]["_remoteAuthority"] = True
        cookie, csrf = self.fixture.browser_session(self.fixture.operator_token)
        before = self.fixture.store.list_resource_bindings()
        status, value, _ = self.fixture.request(
            "/api/sessions", method="POST", cookie=cookie, csrf=csrf,
            body={"deviceId": "shared-device", "clientId": "remote-browser",
                  "projectId": "checkout", "applicationId": "ios_app",
                  "buildId": "original"})
        self.assertEqual(status, 409)
        self.assertEqual(value["error"]["code"], "authority_unavailable")
        self.assertEqual(self.fixture.store.list_resource_bindings(), before)
        self.assertEqual(self.fixture.calls, [])

    def test_invalid_release_selection_fails_before_resource_binding(self):
        cookie, csrf = self.fixture.browser_session(self.fixture.operator_token)
        before = self.fixture.store.list_resource_bindings()
        status, _, _ = self.fixture.request(
            "/api/sessions", method="POST", cookie=cookie, csrf=csrf,
            body={"deviceId": "shared-device", "clientId": "invalid-build",
                  "projectId": "checkout", "applicationId": "ios_app",
                  "buildId": "unknown-build"})
        self.assertEqual(status, 400)
        self.assertEqual(self.fixture.store.list_resource_bindings(), before)
        self.assertEqual(self.fixture.calls, [])

    def test_noncanonical_paths_do_not_alias_scoped_resources(self):
        session, cookie, _ = self.fixture.create_session()
        before = self.fixture.lab._session(session["id"])["lastActivity"]
        for path in (
            f"/api//sessions/{session['id']}",
            f"/api/sessions/{session['id']}/",
            f"/api/sessions/{session['id']}?projectId=foreign",
            f"/api/sessions/%2F{session['id']}",
            f"/api/sessions/../sessions/{session['id']}",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.fixture.request(path, cookie=cookie)[0], 400)
        with socket.create_connection(
                ("127.0.0.1", self.fixture.server.server_port), timeout=5) as connection:
            request = (
                f"GET //api/sessions/{session['id']} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.fixture.server.server_port}\r\n"
                f"Cookie: {cookie}\r\nConnection: close\r\n\r\n").encode()
            connection.sendall(request)
            status_line = connection.recv(256).split(b"\r\n", 1)[0]
        self.assertIn(b" 400 ", status_line)
        self.assertEqual(self.fixture.lab._session(session["id"])["lastActivity"], before)
        self.assertEqual(self.fixture.calls, [])


class AsynchronousCollectionRevocationTests(unittest.TestCase):
    def test_revocation_after_provider_callback_starts_blocks_later_result_use(self):
        temporary = tempfile.TemporaryDirectory()
        entered = threading.Event()
        release = threading.Event()
        current = [True]
        provider_calls = []

        class Provider(ProjectProvider):
            def observe(self):
                provider_calls.append("observe")
                entered.set()
                release.wait(3)
                return {"nodes": []}

        selected = device([])
        selected["factory"] = lambda: Provider([])

        def authorize(_kind):
            if not current[0]:
                raise LiveError("authorization_revoked", "Authorization revoked", 403)
            return True

        lab = Lab([selected], Path(temporary.name) / "lab")
        result = []
        try:
            session = lab.create_session(
                "shared-device", "operator", "browser", _effect_authorizer=authorize)

            def collect():
                try:
                    result.append(lab.observe(session["id"], "operator"))
                except Exception as error:
                    result.append(error)

            thread = threading.Thread(target=collect)
            thread.start()
            self.assertTrue(entered.wait(2))
            current[0] = False
            release.set()
            thread.join(timeout=3)
            self.assertEqual(provider_calls, ["observe"])
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], LiveError)
            self.assertEqual(result[0].code, "authorization_revoked")
        finally:
            release.set()
            lab.close_all()
            temporary.cleanup()


class ActiveJobRevocationTests(unittest.TestCase):
    def test_revocation_during_job_prevents_the_next_provider_effect(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        control = BlockingControl()
        store = AccessStore(root / "coordinator-v2")
        lab = Lab(
            [blocking_device(control)], root / "lab",
            recording_clock_sync=ClockSynchronizer(FakeClock()),
            recording_wall_clock_ms=lambda: int(time.time() * 1000))
        queue = None
        try:
            store.bootstrap_administrator("admin")
            store.create_identity("admin", "operator")
            store.register_project("admin", project_document())
            store.grant_membership("admin", "checkout", "operator", "operator")
            store.assign_device("admin", "shared-device", project_id="checkout")
            issued = store.issue_principal_credential(
                "admin", "operator", lifetime_seconds=300)
            principal = store.authenticate_principal(issued["token"])

            session = lab.create_session("shared-device", "operator", "recorder")
            lab.start_recording(
                session["id"], "operator", session["controllerId"], session["epoch"], reset=True)
            frame = lab.frame(session["id"])
            for sequence in (1, 2):
                lab.input(session["id"], "operator", {
                    "controllerId": session["controllerId"], "epoch": session["epoch"],
                    "sequence": sequence, "commandId": f"record-{sequence}",
                    "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
                    "action": "tap", "payload": {"x": .5, "y": .5},
                })
                if sequence == 1:
                    time.sleep(.05)
            recording = lab.stop_recording(
                session["id"], "operator", session["controllerId"], session["epoch"])
            lab.close_session(session["id"], "operator")
            control.calls.clear()
            control.block = True

            access = AccessController(store)
            registration = lab.register_recording_project(
                project_document(), collection_policy())
            access.bind_project(registration)
            access.bind_resource(
                "recording", recording["id"], "checkout", "operator", meaning="approved")

            def authorize(principal_id, credential_id, project_id, device_id, job_id, kind,
                          authorization_id=None):
                self.assertIsNone(authorization_id)
                current = store._principal_by_id(credential_id)
                self.assertEqual(current.principal_id, principal_id)
                access.authorize_resource(current, "job", job_id, "job.manage", executable=True)
                access.authorize_device(current, device_id, project_id, "device.operate")
                return True

            queue = JobQueue(
                lab, poll_interval=.01, effect_authorizer=authorize,
                resource_binder=lambda job_id, project_id, owner: access.bind_resource(
                    "job", job_id, project_id, owner, meaning="release"))
            job = queue.submit(
                "operator", {"recordingId": recording["id"], "variables": {},
                             "requestId": "revoked-active-job"},
                project_id="checkout", principal_id="operator",
                credential_id=principal.credential_id)
            queue.start()
            self.assertTrue(control.entered.wait(2))
            store.revoke_membership("admin", "checkout", "operator", "operator")
            control.release.set()

            deadline = time.monotonic() + 4
            result = queue.get(job["id"], "operator")
            while result["state"] not in {"failed", "cancelled", "succeeded"} \
                    and time.monotonic() < deadline:
                time.sleep(.01)
                result = queue.get(job["id"], "operator")
            self.assertEqual(result["state"], "failed")
            self.assertEqual(control.calls.count("tap"), 1)
        finally:
            control.release.set()
            if queue is not None:
                queue.close()
            lab.close_all()
            store.close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
