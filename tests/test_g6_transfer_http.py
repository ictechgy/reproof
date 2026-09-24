"""Actual worker HTTP transfer tests for expiry, denial, revocation, and pin cleanup."""
from __future__ import annotations
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from reproof.live.artifact_transfer import ArtifactRead, ArtifactTransferStore
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore
from reproof.live.model import Lab, LiveError
from reproof.live.providers import demo_device
from reproof.live.worker import WorkerClient, WorkerServer, _WorkerHandler
from tests.test_fixture_allocations import collection_policy, project_document


class TransferHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='g6-parent-http-')
        root = Path(self.temporary.name)
        self.now = 1000
        self.allowed = {'project_a', 'project_b'}
        self.budget = DiskBudget(root / 'budget', capacity_bytes=64 * 1024 * 1024,
                                 journal_headroom_bytes=1024 * 1024)
        self.evidence = EvidenceStore(root / 'evidence', self.budget,
                                      max_object_bytes=8 * 1024 * 1024)
        self.transfer = ArtifactTransferStore(
            root / 'transfers', self.budget, self.evidence,
            object_quota_bytes=8 * 1024 * 1024,
            project_quota_bytes=32 * 1024 * 1024,
            host_quota_bytes=32 * 1024 * 1024, clock_ms=lambda: self.now)

        def authorize(project_id=None):
            if project_id is not None and project_id not in self.allowed:
                raise LiveError('unauthorized', 'Owned project scope was revoked', 401)
            return True

        self.lab = Lab([demo_device()], root / 'lab')
        registrations = []
        for project_id in ('project_a', 'project_b'):
            project = project_document();project['id'] = project_id
            registrations.append(self.lab.register_recording_project(
                project, collection_policy(), capacity_bytes=8 * 1024 * 1024,
                journal_headroom_bytes=512 * 1024))
        self.server = WorkerServer(self.lab, 'h' * 40,
                                   host_authorizer=authorize,
                                   host_identity=('parent_mac', 1, 'parent_incarnation'),
                                   artifact_store=self.transfer,
                                   registered_projects=registrations)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.client = WorkerClient(self.server.origin, 'h' * 40)

    def tearDown(self):
        self.server.close_operations()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.transfer.close()
        self.evidence.close()
        self.budget.close()
        self.temporary.cleanup()

    def publish(self, body=b'owned HTTP transfer', *, project='project_a', expires=2000):
        upload = self.client.allocate_artifact(
            project_id=project, kind='manifest', size=len(body),
            digest=hashlib.sha256(body).hexdigest(), metadata={'format': 'json'},
            retention_class='original', retain_until_ms=expires)
        for offset in range(0, len(body), 1024 * 1024):
            self.client.upload_artifact_chunk(upload['objectId'], upload['uploadGeneration'],
                                              offset, body[offset:offset + 1024 * 1024])
        self.client.finalize_artifact(upload['objectId'], upload['uploadGeneration'])
        return upload

    def pin_count(self):
        return self.evidence._connection.execute('SELECT COUNT(*) FROM pins').fetchone()[0]

    def assert_pins_released(self, message=None):
        # Content-Length completion at the client does not synchronize the
        # worker's final revalidation and ArtifactRead.__exit__ on its thread.
        deadline = time.monotonic()+2
        while self.pin_count() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.pin_count(), 0, message)

    def test_oversized_full_download_rejection_does_not_leave_a_pin(self):
        upload = self.publish(b'x' * (4 * 1024 * 1024 + 1))
        try:
            self.client.download_artifact(upload['objectId'])
        except LiveError:
            pass
        self.assert_pins_released('A rejected full download left a permanent evidence pin')

    def test_expired_association_is_denied_by_the_http_download_route(self):
        upload = self.publish(expires=1001)
        self.now = 1002
        with self.assertRaises(LiveError):
            self.client.download_artifact(upload['objectId'])
        self.assert_pins_released()

    def test_revocation_while_disk_read_is_held_denies_bytes_and_closes_pin(self):
        upload = self.publish()
        entered = threading.Event()
        proceed = threading.Event()
        result = {}
        original = self.evidence.read

        def held(digest):
            entered.set()
            if not proceed.wait(4):
                raise RuntimeError('Owned read probe timed out')
            return original(digest)

        def download():
            try:
                result['body'] = self.client.download_artifact(upload['objectId'])
            except LiveError as exc:
                result['errorCode'] = exc.code

        downloading = threading.Thread(target=download, daemon=True)
        with mock.patch.object(self.evidence, 'read', held):
            try:
                downloading.start()
                self.assertTrue(entered.wait(3))
                self.allowed.remove('project_a')
            finally:
                proceed.set()
                downloading.join(5)
        self.assertFalse(downloading.is_alive())
        self.assertNotIn('body', result)
        self.assertEqual(result.get('errorCode'), 'unauthorized')
        self.assert_pins_released()

    def test_response_header_write_failure_closes_the_download_pin(self):
        upload = self.publish()
        original = _WorkerHandler.end_headers

        def failed_headers(handler):
            if any(b'application/octet-stream' in line
                   for line in getattr(handler, '_headers_buffer', [])):
                raise BrokenPipeError('owned response header write fault')
            return original(handler)

        with mock.patch.object(_WorkerHandler, 'end_headers', failed_headers):
            with self.assertRaises(LiveError):
                self.client.download_artifact(upload['objectId'])
        self.assert_pins_released('A failed response header write left a permanent evidence pin')

    def test_identical_content_in_two_projects_keeps_per_association_authorization(self):
        first = self.publish(project='project_a')
        second = self.publish(project='project_b')
        self.assertEqual(first['digest'], second['digest'])
        self.allowed.remove('project_a')
        with self.assertRaises(LiveError):
            self.client.download_artifact(first['objectId'])
        self.assertEqual(self.client.download_artifact(second['objectId']), b'owned HTTP transfer')
        self.assert_pins_released()

    def test_client_body_completion_can_precede_the_worker_pin_release(self):
        upload = self.publish()
        entered, proceed = threading.Event(), threading.Event()
        original = ArtifactRead.close
        def held(value):
            entered.set()
            if not proceed.wait(3): raise RuntimeError('Owned close probe timed out')
            original(value)
        with mock.patch.object(ArtifactRead, 'close', held):
            try:
                self.assertEqual(self.client.download_artifact(upload['objectId']), b'owned HTTP transfer')
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.pin_count(), 1)
            finally:
                proceed.set()
            self.assert_pins_released()
