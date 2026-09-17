"""Independent shared HTTP, credential, and original-evidence regressions."""
import copy
import http.client
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

from reproloop.live.access import AccessError, AccessStore
from reproloop.core import ContractError
from tests.test_fixture_allocations import project_document


class ParentSharedHttpTests(unittest.TestCase):
    def setUp(self):
        from tests.test_project_access import SharedHttpFixture
        self.fixture = SharedHttpFixture()

    def tearDown(self):
        self.fixture.close()

    def foreign_member(self):
        store = self.fixture.store
        other = copy.deepcopy(project_document())
        other.update(id="foreign-project", revision="r2", trustGroup="foreign-group")
        store.register_project("root-admin", other)
        store.create_identity("root-admin", "foreign-viewer")
        store.grant_membership("root-admin", "foreign-project", "foreign-viewer", "viewer")
        return store.issue_principal_credential(
            "root-admin", "foreign-viewer", lifetime_seconds=300)["token"]

    def test_every_previous_api_route_refuses_anonymous_before_effects(self):
        session, _, _ = self.fixture.create_session()
        substitutions = {"session": session["id"],
                         "recording": session["releaseRecordingId"],
                         "device": "shared-device", "job": "unknown-job",
                         "repair": "unknown-repair"}
        matrix = json.loads((ROOT / "tests/fixtures/release/g5-parent-route-matrix.json").read_text())
        before = self.fixture.lab._session(session["id"])["lastActivity"]
        for route in matrix["routes"]:
            path = route["path"].format(**substitutions)
            with self.subTest(method=route["method"], path=route["path"]):
                status, _, _ = self.fixture.request(
                    path, method=route["method"],
                    body={} if route["method"] == "POST" else None)
                self.assertIn(status, (401, 403, 404, 405))
                self.assertEqual(self.fixture.calls, [])
                self.assertEqual(self.fixture.lab._session(session["id"])["lastActivity"], before)

    def test_foreign_project_cannot_read_session_recording_or_export_aliases(self):
        session, _, _ = self.fixture.create_session()
        token = self.foreign_member()
        paths = [f"/api/sessions/{session['id']}" + suffix for suffix in (
            "", "/events", "/observe", "/frame", "/stream", "/app-logs", "/app-logs/export")]
        paths += [f"/api/recordings/{session['releaseRecordingId']}" + suffix for suffix in (
            "", "/export", "/script")]
        before = self.fixture.lab._session(session["id"])["lastActivity"]
        for path in paths:
            with self.subTest(route=path.rsplit("/", 1)[-1]):
                status, value, _ = self.fixture.request(
                    path, token=token, headers={"Range": "bytes=0-10"})
                self.assertIn(status, (401, 403, 404))
                if isinstance(value, dict):
                    self.assertNotIn("session", value)
                    self.assertNotIn("recording", value)
                self.assertEqual(self.fixture.calls, [])
                self.assertEqual(self.fixture.lab._session(session["id"])["lastActivity"], before)

    def test_foreign_lists_do_not_disclose_session_or_device_ids(self):
        session, _, _ = self.fixture.create_session()
        token = self.foreign_member()
        for path in ("/api/sessions", "/api/devices", "/api/recordings", "/api/jobs", "/api/repairs"):
            with self.subTest(path=path):
                status, value, _ = self.fixture.request(path, token=token)
                self.assertIn(status, (200, 403, 404))
                encoded = json.dumps(value, sort_keys=True)
                self.assertNotIn(session["id"], encoded)
                self.assertNotIn("shared-device", encoded)
                self.assertNotIn(session["releaseRecordingId"], encoded)

    def test_cookie_from_local_owner_does_not_authenticate_shared_routes(self):
        value = getattr(self.fixture.server, "browser_token", None) or "legacy-placeholder"
        status, _, _ = self.fixture.request("/api/health", cookie="repro_live=" + value)
        self.assertEqual(status, 401)

    def test_duplicate_security_headers_are_rejected(self):
        session, cookie, csrf = self.fixture.create_session()
        payload = json.dumps({"clientId": "browser"}).encode()
        endpoint = f"/api/sessions/{session['id']}/heartbeat"
        authority = f"127.0.0.1:{self.fixture.server.server_port}"
        for duplicate in ("Host", "Authorization", "Content-Length", "Origin", "X-Repro-CSRF"):
            with self.subTest(header=duplicate):
                headers = {"Host": authority, "X-Repro-CSRF": csrf,
                           "Origin": self.fixture.server.origin,
                           "Content-Type": "application/json", "Content-Length": str(len(payload))}
                if duplicate == "Authorization":
                    headers["Authorization"] = "Bearer " + self.fixture.operator_token
                else:
                    headers["Cookie"] = cookie
                baseline = http.client.HTTPConnection(
                    "127.0.0.1", self.fixture.server.server_port, timeout=5)
                try:
                    baseline.request("POST", endpoint, body=payload, headers=headers)
                    response = baseline.getresponse()
                    response.read()
                    self.assertEqual(response.status, 200)
                finally:
                    baseline.close()
                before = self.fixture.lab._session(session["id"])["lastActivity"]
                connection = http.client.HTTPConnection(
                    "127.0.0.1", self.fixture.server.server_port, timeout=5)
                try:
                    connection.putrequest("POST", endpoint, skip_host=True, skip_accept_encoding=True)
                    for name, value in headers.items():
                        connection.putheader(name, value)
                        if name == duplicate:
                            connection.putheader(name, value)
                    connection.endheaders(payload)
                    response = connection.getresponse()
                    response.read()
                    self.assertIn(response.status, (400, 401, 403, 411))
                    self.assertEqual(self.fixture.lab._session(session["id"])["lastActivity"], before)
                finally:
                    connection.close()

    def test_browser_session_cannot_keep_revoked_membership(self):
        session, _, _ = self.fixture.create_session()
        cookie, _ = self.fixture.browser_session(self.fixture.viewer_token)
        self.fixture.store.revoke_membership("root-admin", "checkout", "viewer", "viewer")
        path = f"/api/sessions/{session['id']}"
        self.assertIn(self.fixture.request(path, cookie=cookie)[0], (401, 403, 404))
        self.assertEqual(self.fixture.calls, [])

    def test_get_cannot_dispatch_the_post_only_replay_cancel_action(self):
        session, cookie, _ = self.fixture.create_session()
        calls = []
        original = self.fixture.lab.cancel_replay

        def counted(*args, **kwargs):
            calls.append("cancel_replay")
            return original(*args, **kwargs)

        self.fixture.lab.cancel_replay = counted
        status, _, _ = self.fixture.request(
            f"/api/sessions/{session['id']}/replay/cancel", cookie=cookie)
        self.assertEqual(calls, [])
        self.assertIn(status, (404, 405))

    def test_logout_closes_an_already_open_browser_stream(self):
        self.check_inactive_browser_stream(expired=False)

    def test_expired_browser_session_cannot_emit_another_frame(self):
        self.check_inactive_browser_stream(expired=True)

    def check_inactive_browser_stream(self, *, expired):
        session, _, _ = self.fixture.create_session()
        if expired:
            self.fixture.access.browser_session_seconds = 60
        cookie, csrf = self.fixture.browser_session(self.fixture.viewer_token)
        connection, response = self.fixture.request(
            f"/api/sessions/{session['id']}/stream", cookie=cookie, read=False)
        try:
            self.assertEqual(response.status, 200)
            lengths = response.read(8)
            self.assertEqual(len(lengths), 8)
            metadata_size, image_size = struct.unpack(">II", lengths)
            first = json.loads(response.read(metadata_size))
            response.read(image_size)
            self.assertEqual(first["type"], "frame")
            if expired:
                later = self.fixture.store._now() + 61
                self.fixture.store._clock = lambda: later
            else:
                status, _, _ = self.fixture.request(
                    "/api/auth/logout", method="POST", body={}, cookie=cookie, csrf=csrf)
                self.assertEqual(status, 200)
            self.fixture.lab.publish_frame(
                session["id"], b"<svg>after-logout</svg>", "image/svg+xml", 320, 640)
            response.fp.raw._sock.settimeout(2)
            self.assertEqual(response.read(8), b"")
        finally:
            response.close()
            connection.close()

    def test_project_revision_keeps_original_recording_readable_and_unchanged(self):
        session, cookie, csrf = self.fixture.create_session()
        status, _, _ = self.fixture.request(
            f"/api/sessions/{session['id']}/close", method="POST",
            cookie=cookie, csrf=csrf,
            body={"controllerId": session["controllerId"], "epoch": session["epoch"]})
        self.assertEqual(status, 200)
        path = f"/api/recordings/{session['releaseRecordingId']}/export"
        status, original, _ = self.fixture.request(path, token=self.fixture.viewer_token)
        self.assertEqual(status, 200)
        replacement = copy.deepcopy(self.fixture.project)
        replacement["revision"] = "revision-two"
        self.fixture.store.register_project("root-admin", replacement)
        status, historical, _ = self.fixture.request(path, token=self.fixture.viewer_token)
        self.assertEqual(status, 200, "A project revision made its immutable original unavailable")
        self.assertEqual(historical, original)

    def test_checked_principal_keeps_its_browser_authority(self):
        cookie, _ = self.fixture.browser_session(self.fixture.viewer_token)
        principal = self.fixture.access.authenticate_browser(cookie.split("=", 1)[1])
        checked = self.fixture.store.authorize(principal, "checkout", "session.read")
        self.fixture.access.close_browser_session(cookie.split("=", 1)[1])
        with self.assertRaises(AccessError):
            self.fixture.store.authorize(checked, "checkout", "session.read")

    def test_historical_session_cannot_renew_under_a_new_project_revision(self):
        session, cookie, csrf = self.fixture.create_session()
        path = f"/api/sessions/{session['id']}/heartbeat"
        status, _, _ = self.fixture.request(
            path, method="POST", cookie=cookie, csrf=csrf, body={"clientId": "browser"})
        self.assertEqual(status, 200)
        raw = self.fixture.lab._session(session["id"])
        before = raw["lastActivity"]
        effect_authorizer = raw["effectAuthorizer"]
        replacement = copy.deepcopy(self.fixture.project)
        replacement["revision"] = "revision-two"
        self.fixture.store.register_project("root-admin", replacement)
        from tests.test_fixture_allocations import collection_policy
        current = self.fixture.lab.register_recording_project(replacement, collection_policy())
        self.fixture.access.bind_project(current)
        status, _, _ = self.fixture.request(
            path, method="POST", cookie=cookie, csrf=csrf, body={"clientId": "browser"})
        self.assertIn(status, (403, 409))
        self.assertEqual(raw["lastActivity"], before)
        with self.assertRaises(AccessError):
            effect_authorizer("input")
        status, _, _ = self.fixture.request(
            f"/api/sessions/{session['id']}/close", method="POST", cookie=cookie, csrf=csrf,
            body={"controllerId": session["controllerId"], "epoch": session["epoch"]})
        self.assertEqual(status, 200, "Historical session cleanup must remain available")

    def test_logged_out_stream_does_not_emit_a_terminal_record(self):
        session, _, _ = self.fixture.create_session()
        cookie, csrf = self.fixture.browser_session(self.fixture.viewer_token)
        connection, response = self.fixture.request(
            f"/api/sessions/{session['id']}/stream", cookie=cookie, read=False)
        try:
            self.assertEqual(response.status, 200)
            metadata_size, image_size = struct.unpack(">II", response.read(8))
            response.read(metadata_size + image_size)
            status, _, _ = self.fixture.request(
                "/api/auth/logout", method="POST", body={}, cookie=cookie, csrf=csrf)
            self.assertEqual(status, 200)
            self.fixture.lab.close_session(session["id"], "operator")
            response.fp.raw._sock.settimeout(2)
            self.assertEqual(response.read(8), b"")
        finally:
            response.close()
            connection.close()


class ParentCredentialBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = [1_000]
        self.store = AccessStore(Path(self.temp.name) / "coordinator-v2",
                                 clock=lambda: self.clock[0])
        self.store.bootstrap_administrator("admin")
        self.store.register_project("admin", project_document())
        self.store.create_identity("admin", "person")
        self.store.grant_membership("admin", "checkout", "person", "operator")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enroll(self):
        issued = self.store.create_host_enrollment(
            "admin", host_id="mac-one", project_ids=["checkout"], trust_groups=["qa"],
            lifetime_seconds=60, credential_lifetime_seconds=60)
        enrolled = self.store.consume_host_enrollment(
            issued["token"], host_id="mac-one", incarnation="boot-one")
        return issued, enrolled

    def test_cached_principal_cannot_operate_after_credential_expiry(self):
        issued = self.store.issue_principal_credential(
            "admin", "person", lifetime_seconds=60)
        principal = self.store.authenticate_principal(issued["token"])
        self.store.authorize(principal, "checkout", "session.operate")
        self.clock[0] += 61
        with self.assertRaises(AccessError):
            self.store.authorize(principal, "checkout", "session.operate")

    def test_cached_host_cannot_operate_after_credential_expiry(self):
        _, enrolled = self.enroll()
        host = self.store.authenticate_host(enrolled["credential"])
        self.store.authorize_host(host, project_id="checkout", trust_group="qa")
        self.clock[0] += 61
        with self.assertRaises(AccessError):
            self.store.authorize_host(host, project_id="checkout", trust_group="qa")

    def test_cached_host_cannot_operate_after_committed_revocation(self):
        _, enrolled = self.enroll()
        host = self.store.authenticate_host(enrolled["credential"])
        self.store.authorize_host(host, project_id="checkout", trust_group="qa")
        self.store.revoke_host("admin", "mac-one")
        with self.assertRaises(AccessError):
            self.store.authorize_host(host, project_id="checkout", trust_group="qa")

    def test_enrollment_host_and_principal_credentials_are_separate(self):
        issued, enrolled = self.enroll()
        principal = self.store.issue_principal_credential(
            "admin", "person", lifetime_seconds=60)["token"]
        for token in (issued["token"], enrolled["credential"]):
            with self.subTest(boundary="principal"):
                with self.assertRaises(AccessError):
                    self.store.authenticate_principal(token)
        for token in (issued["token"], principal):
            with self.subTest(boundary="host"):
                with self.assertRaises(AccessError):
                    self.store.authenticate_host(token)

    def pending_enrollment(self):
        return self.store.create_host_enrollment(
            "admin", host_id="mac-one", project_ids=["checkout"], trust_groups=["qa"],
            lifetime_seconds=60, credential_lifetime_seconds=60)

    def test_duplicate_pending_enrollment_cannot_replace_the_original(self):
        first = self.pending_enrollment()
        with self.assertRaises(ContractError):
            self.pending_enrollment()
        enrolled = self.store.consume_host_enrollment(
            first["token"], host_id="mac-one", incarnation="boot-one")
        self.assertEqual(enrolled["generation"], first["generation"])
        host = self.store.authenticate_host(enrolled["credential"])
        self.store.authorize_host(host, project_id="checkout", trust_group="qa")

    def test_expired_unused_enrollment_can_be_reissued_as_a_later_generation(self):
        first = self.pending_enrollment()
        self.clock[0] += 61
        with self.assertRaises(AccessError):
            self.store.consume_host_enrollment(
                first["token"], host_id="mac-one", incarnation="boot-one")
        replacement = self.pending_enrollment()
        enrolled = self.store.consume_host_enrollment(
            replacement["token"], host_id="mac-one", incarnation="boot-two")
        self.assertGreater(enrolled["generation"], first["generation"])
        host = self.store.authenticate_host(enrolled["credential"])
        self.store.authorize_host(host, project_id="checkout", trust_group="qa")

    def test_host_revocation_invalidates_an_already_pending_replacement(self):
        first = self.pending_enrollment()
        original = self.store.consume_host_enrollment(
            first["token"], host_id="mac-one", incarnation="boot-one")
        self.clock[0] += 61
        replacement = self.pending_enrollment()
        self.store.revoke_host("admin", "mac-one")
        with self.assertRaises(AccessError):
            self.store.consume_host_enrollment(
                replacement["token"], host_id="mac-one", incarnation="boot-two")
        self.assertEqual(self.store.list_hosts()[0]["generation"], original["generation"])
        self.assertTrue(self.store.list_hosts()[0]["revoked"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
