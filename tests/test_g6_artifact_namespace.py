"""Transfer deletion owns its CAS, while aggregate storage remains shared."""
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest

from reproof.core import ContractError
from reproof.live.artifact_transfer import ArtifactTransferStore
from reproof.live.evidence_store import EvidenceStore
from reproof.live.model import Lab
from reproof.live.providers import demo_device
from reproof.live.worker import WorkerClient, WorkerServer
from tests.test_fixture_allocations import collection_policy, project_document


class ArtifactNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='g6-artifact-namespace-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lab = Lab([demo_device()], self.root / 'lab')
        self.addCleanup(self.lab.close_all)
        self.registration = self.lab.register_recording_project(
            project_document(), collection_policy(), capacity_bytes=16 * 1024 * 1024,
            journal_headroom_bytes=512 * 1024)

    def transfer(self, evidence):
        value = ArtifactTransferStore(
            self.root / 'transfers', self.lab._recording_budget, evidence,
            object_quota_bytes=2 * 1024 * 1024,
            project_quota_bytes=4 * 1024 * 1024,
            host_quota_bytes=8 * 1024 * 1024)
        self.addCleanup(value.close)
        return value

    def server(self, transfer):
        return WorkerServer(
            self.lab, 'n' * 40, host_authorizer=lambda _project=None: True,
            host_identity=('namespace_worker', 1, 'namespace_boot'),
            artifact_store=transfer, registered_projects=[self.registration])

    def test_worker_rejects_recording_cas_as_its_transfer_namespace(self):
        transfer = self.transfer(self.lab._evidence_store)
        server = None
        try:
            with self.assertRaisesRegex(ContractError, 'isolated'):
                server = self.server(transfer)
        finally:
            if server is not None:
                server.server_close()

    def test_transfer_tombstone_preserves_identical_original_recording_bytes(self):
        evidence = EvidenceStore(self.root / 'artifact-objects', self.lab._recording_budget)
        self.addCleanup(evidence.close)
        transfer = self.transfer(evidence)
        server = self.server(transfer)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        body = b'owned identical recording and transfer bytes'
        until = int(time.time() * 1000) + 60_000
        try:
            original = self.lab._evidence_store.put_bytes(
                body, owner='original_recording', retention_class='original', retain_until_ms=until)
            client = WorkerClient(server.origin, 'n' * 40)
            upload = client.upload_artifact_bytes(
                body, project_id='checkout', kind='manifest', metadata={},
                retention_class='original', retain_until_ms=until)
            self.assertEqual(upload['digest'], hashlib.sha256(body).hexdigest())
            self.assertEqual(client.download_artifact(upload['objectId']), body)
            client.tombstone_artifact(upload['objectId'])
            self.assertIsNone(evidence.lookup(original.digest))
            self.assertEqual(self.lab._evidence_store.read(original.digest), body)
            self.assertIs(evidence.budget, self.lab._recording_budget)
        finally:
            server.close_operations(); server.shutdown(); server.server_close(); thread.join(5)
