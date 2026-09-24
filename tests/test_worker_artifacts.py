import hashlib
import http.client
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from reproof.live.access import AccessError
from reproof.live.artifact_transfer import ArtifactTransferStore
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore
from reproof.live.model import Lab, LiveError
from reproof.live.providers import demo_device
from reproof.live.enrollment import EnrollmentClient
from reproof.live.worker import WorkerClient, WorkerServer
from tests.test_fixture_allocations import collection_policy, project_document


TOKEN = "g6-artifact-transport-token-0123456789"


class _DripHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        pass

    def _respond(self):
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
            except (ValueError, OSError):
                return
        body = b"{" + b" " * 100 + b"}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        for byte in body:
            try:
                self.wfile.write(bytes([byte]));self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                break
            time.sleep(self.server.drip_delay)

    do_GET = _respond
    do_POST = _respond


class _DripServer:
    def __init__(self, delay):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _DripHandler)
        self.server.drip_delay = delay
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *_args):
        self.server.shutdown();self.server.server_close()
        self.thread.join(timeout=3)


class WorkerArtifactHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="g6-artifacts-")
        root = Path(self.temporary.name)
        self.budget = DiskBudget(root / "budget", capacity_bytes=8 * 1024 * 1024,
                                 journal_headroom_bytes=512 * 1024)
        self.evidence = EvidenceStore(root / "evidence", self.budget,
                                      max_object_bytes=2 * 1024 * 1024)
        self.transfer = ArtifactTransferStore(
            root / "transfer", self.budget, self.evidence,
            object_quota_bytes=2 * 1024 * 1024,
            project_quota_bytes=2 * 1024 * 1024,
            host_quota_bytes=4 * 1024 * 1024)
        self.allowed = {"checkout"}

        def authorize(project_id=None):
            if project_id is not None and project_id not in self.allowed:
                raise LiveError("unauthorized", "Host scope is stale", 401)
            return True

        self.lab = Lab([demo_device()], root / "lab")
        registrations = []
        for project_id in ("checkout", "other"):
            project = project_document();project["id"] = project_id
            registrations.append(self.lab.register_recording_project(
                project, collection_policy(), capacity_bytes=8 * 1024 * 1024,
                journal_headroom_bytes=512 * 1024))
        self.server = WorkerServer(
            self.lab, TOKEN, host_authorizer=authorize,
            host_identity=("mac-one", 3, "boot-three"),
            artifact_store=self.transfer, registered_projects=registrations)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = WorkerClient(self.server.origin, TOKEN)

    def tearDown(self):
        self.server.close_operations()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.transfer.close()
        self.evidence.close()
        self.budget.close()
        self.temporary.cleanup()

    def allocate(self, body, *, project="checkout"):
        return self.client.allocate_artifact(
            project_id=project, kind="manifest", size=len(body),
            digest=hashlib.sha256(body).hexdigest(), metadata={"format": "json"},
            retention_class="original", retain_until_ms=self.transfer._now() + 60_000)

    def test_actual_http_upload_repeat_finalize_and_range_download(self):
        body = b'{"sameMacProtocol":true}'
        upload = self.allocate(body)
        first = self.client.upload_artifact_chunk(
            upload["objectId"], upload["uploadGeneration"], 0, body)
        self.assertEqual(first["receivedBytes"], len(body))
        repeated = self.client.upload_artifact_chunk(
            upload["objectId"], upload["uploadGeneration"], 0, body)
        self.assertTrue(repeated["repeated"])
        published = self.client.finalize_artifact(
            upload["objectId"], upload["uploadGeneration"])
        self.assertEqual(published["state"], "published")
        self.assertEqual(self.client.download_artifact(
            upload["objectId"], start=2, end=10), body[2:10])

    def test_changed_overlap_hole_corruption_and_cross_project_are_denied(self):
        body = b"abcdefgh"
        upload = self.allocate(body)
        self.client.upload_artifact_chunk(
            upload["objectId"], upload["uploadGeneration"], 0, body[:4])
        with self.assertRaises(LiveError) as overlap:
            self.client.upload_artifact_chunk(
                upload["objectId"], upload["uploadGeneration"], 0, b"ABCD")
        self.assertEqual(overlap.exception.code, "chunk_conflict")
        with self.assertRaises(LiveError) as hole:
            self.client.finalize_artifact(
                upload["objectId"], upload["uploadGeneration"])
        self.assertEqual(hole.exception.code, "upload_incomplete")
        with self.assertRaises(LiveError):
            self.allocate(body, project="foreign")

        corrupted = self.allocate(b"abcdefgh2")
        self.client.upload_artifact_chunk(
            corrupted["objectId"], corrupted["uploadGeneration"], 0, b"abcdefgh2")
        self.transfer._stage_path(corrupted["objectId"]).write_bytes(b"xxxxxxxxx")
        with self.assertRaises(LiveError) as mismatch:
            self.client.finalize_artifact(
                corrupted["objectId"], corrupted["uploadGeneration"])
        self.assertEqual(mismatch.exception.code, "digest_mismatch")

    def test_restart_reconciles_received_chunks_and_rejects_stale_host_identity(self):
        body = b"restart-resume"
        upload = self.allocate(body)
        self.client.upload_artifact_chunk(
            upload["objectId"], upload["uploadGeneration"], 0, body[:7])
        self.transfer.close()
        self.transfer = ArtifactTransferStore(
            Path(self.temporary.name) / "transfer", self.budget, self.evidence,
            object_quota_bytes=2 * 1024 * 1024,
            project_quota_bytes=2 * 1024 * 1024,
            host_quota_bytes=4 * 1024 * 1024)
        self.server.artifact_store = self.transfer
        status = self.client.artifact_status(upload["objectId"])
        self.assertEqual(status["receivedRanges"], [[0, 7]])
        self.client.upload_artifact_chunk(
            upload["objectId"], upload["uploadGeneration"], 7, body[7:])
        self.assertEqual(self.client.finalize_artifact(
            upload["objectId"], upload["uploadGeneration"])["state"], "published")
        self.server.host_identity = ("mac-one", 4, "boot-four")
        with self.assertRaises(LiveError) as stale:
            self.client.artifact_status(upload["objectId"])
        self.assertEqual(stale.exception.code, "stale_upload")

    def test_revocation_after_chunk_io_prevents_acknowledgement(self):
        body = b"revoked"
        upload = self.allocate(body)
        self.allowed.clear()
        with self.assertRaises(LiveError) as denied:
            self.client.upload_artifact_chunk(
                upload["objectId"], upload["uploadGeneration"], 0, body)
        self.assertEqual(denied.exception.code, "unauthorized")

    def test_same_digest_keeps_project_associations_and_tombstone_pins(self):
        self.allowed.add("other")
        body = b"shared-bytes-distinct-authority"
        first = self.allocate(body)
        second = self.allocate(body, project="other")
        for project, upload in (("checkout", first), ("other", second)):
            self.client.upload_artifact_chunk(
                upload["objectId"], upload["uploadGeneration"], 0, body,
                project_id=project)
            self.client.finalize_artifact(
                upload["objectId"], upload["uploadGeneration"],
                project_id=project)
        with self.assertRaises(LiveError) as crossed:
            self.client.download_artifact(first["objectId"], project_id="other")
        self.assertEqual(crossed.exception.code, "not_found")
        self.client.tombstone_artifact(first["objectId"], project_id="checkout")
        self.assertEqual(self.client.download_artifact(
            second["objectId"], project_id="other"), body)
        active = self.transfer.open_read(
            second["objectId"], host_identity=("mac-one", 3, "boot-three"),
            expected_project_id="other",
            authorizer=self.server.authorize_enrolled_host)
        try:
            with self.assertRaises(LiveError) as pinned:
                self.client.tombstone_artifact(second["objectId"], project_id="other")
            self.assertEqual(pinned.exception.code, "storage_failure")
        finally:
            active.close()
        self.assertEqual(self.client.download_artifact(
            second["objectId"], project_id="other"), body)

    def test_lost_chunk_ack_resumes_through_http_status(self):
        body = b"resumable-over-real-http"
        original = self.client.upload_artifact_chunk
        lost = {"value": False}

        def lose_once(*args, **kwargs):
            value = original(*args, **kwargs)
            if not lost["value"]:
                lost["value"] = True
                raise LiveError("transport_unavailable", "Synthetic lost response", 503)
            return value

        with patch.object(self.client, "upload_artifact_chunk", side_effect=lose_once):
            result = self.client.upload_artifact_bytes(
                body, project_id="checkout", kind="manifest",
                metadata={"format": "json"}, retention_class="original",
                retain_until_ms=self.transfer._now() + 60_000, chunk_bytes=7)
        self.assertTrue(lost["value"])
        self.assertEqual(result["state"], "published")
        self.assertEqual(self.client.download_artifact(result["objectId"]), body)

    def test_concurrent_identical_chunk_waits_and_remains_idempotent(self):
        body = b"same-concurrent-chunk" * 1024
        upload = self.allocate(body)
        entered = threading.Event();release = threading.Event()
        original = os.pwrite

        def held_write(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)

        results = []
        def send():
            client = WorkerClient(self.server.origin, TOKEN)
            try:
                results.append(client.upload_artifact_chunk(
                    upload["objectId"], upload["uploadGeneration"], 0, body,
                    project_id="checkout"))
            except Exception as error:
                results.append(error)

        with patch("reproof.live.artifact_transfer.os.pwrite",
                   side_effect=held_write):
            first = threading.Thread(target=send);first.start()
            self.assertTrue(entered.wait(5))
            second = threading.Thread(target=send);second.start()
            time.sleep(0.1)
            self.assertTrue(second.is_alive())
            release.set();first.join(5);second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(value, dict) for value in results),
                        repr(results))
        self.assertEqual(sum(value["repeated"] for value in results), 1)
        self.assertEqual(self.client.finalize_artifact(
            upload["objectId"], upload["uploadGeneration"])["state"],
            "published")

    def test_concurrent_project_quota_race_admits_only_one_object(self):
        body = b"q" * (1536 * 1024)
        barrier = threading.Barrier(2)
        original = self.budget.reserve

        def held_reserve(*args, **kwargs):
            reservation = original(*args, **kwargs)
            barrier.wait(timeout=5)
            return reservation

        results = []
        def allocate():
            client = WorkerClient(self.server.origin, TOKEN)
            try:
                results.append(("ok", client.allocate_artifact(
                    project_id="checkout", kind="manifest", size=len(body),
                    digest=hashlib.sha256(body).hexdigest(), metadata={},
                    retention_class="original", retain_until_ms=self.transfer._now() + 60_000)))
            except LiveError as error:
                results.append((error.code, None))

        with patch.object(self.budget, "reserve", side_effect=held_reserve):
            threads = [threading.Thread(target=allocate) for _ in range(2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([item[0] for item in results].count("ok"), 1)
        self.assertEqual([item[0] for item in results].count("quota_exceeded"), 1)

    def test_truncated_body_and_failed_write_do_not_ack_or_drop_charge(self):
        body = b"write-failure"
        upload = self.allocate(body)
        parsed = urlsplit(self.server.origin)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        path = f"/v2/artifacts/uploads/{upload['objectId']}/chunks/0"
        try:
            connection.putrequest("PUT", path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", parsed.netloc)
            connection.putheader("Authorization", "Bearer " + TOKEN)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(len(body) + 3))
            connection.putheader("X-Repro-Project-Id", "checkout")
            connection.putheader("X-Repro-Upload-Generation", str(upload["uploadGeneration"]))
            connection.putheader("X-Repro-Chunk-SHA256", hashlib.sha256(body).hexdigest())
            connection.endheaders(body)
            connection.sock.shutdown(1)
            response = connection.getresponse();response.read()
            self.assertEqual(response.status, 400)
        finally:
            connection.close()
        self.assertEqual(self.client.artifact_status(upload["objectId"])["receivedBytes"], 0)
        before = self.budget.snapshot()["chargedBytes"]
        with patch("reproof.live.artifact_transfer.os.pwrite", side_effect=OSError("synthetic")):
            with self.assertRaises(LiveError) as failed:
                self.client.upload_artifact_chunk(
                    upload["objectId"], upload["uploadGeneration"], 0, body)
        self.assertEqual(failed.exception.code, "storage_failure")
        self.assertEqual(self.client.artifact_status(upload["objectId"])["state"], "quarantined")
        self.assertEqual(self.budget.snapshot()["chargedBytes"], before)


class AbsoluteTransportDeadlineTests(unittest.TestCase):
    def test_worker_and_enrollment_clients_abort_drip_fed_responses(self):
        with _DripServer(0.08) as origin:
            worker = WorkerClient(origin, TOKEN)
            started = time.monotonic()
            with self.assertRaises(LiveError) as timed_out:
                worker.call("/v1/devices", timeout=5)
            self.assertEqual(timed_out.exception.code, "transport_unavailable")
            self.assertLess(time.monotonic() - started, 6.5)

        with _DripServer(0.02) as origin:
            enrollment = EnrollmentClient(origin, request_timeout=0.25)
            started = time.monotonic()
            with self.assertRaises(AccessError) as timed_out:
                enrollment.authenticate("rph.synthetic." + "a" * 32)
            self.assertEqual(timed_out.exception.code, "transport_unavailable")
            self.assertLess(time.monotonic() - started, 1.5)


if __name__ == "__main__":
    unittest.main()
