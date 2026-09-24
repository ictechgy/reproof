"""Trusted project policy at the actual worker artifact HTTP boundary."""
import hashlib
import unittest

from reproof import contracts
from reproof.live.model import LiveError
from tests import test_worker_artifacts as artifact_tests


class ArtifactPolicyTests(unittest.TestCase):
    def setUp(self):
        artifact_tests.WorkerArtifactHttpTests.setUp(self)
        self.transfer._clock_ms = lambda: 1000
        self.registration = next(value for (project_id, _), value
                                 in self.server.registered_projects.items()
                                 if project_id == "checkout")
        self.server.registered_projects = {
            ("checkout", self.registration.project_digest): self.registration}

    tearDown = artifact_tests.WorkerArtifactHttpTests.tearDown

    def policy_upload(self, *, kind="manifest", retain_until=2000):
        body = b"owned policy body"
        return self.client.allocate_artifact(
            project_id="checkout", kind=kind, size=len(body),
            digest=hashlib.sha256(body).hexdigest(), metadata={},
            retention_class="original", retain_until_ms=retain_until)

    def test_disabled_log_collection_is_denied_before_staging(self):
        with self.assertRaises(LiveError) as rejected:
            self.policy_upload(kind="app-log")
        self.assertEqual(rejected.exception.code, "capture_suppressed")
        self.assertEqual(list(self.transfer.staging.glob("*.part")), [])

    def test_wire_retention_cannot_exceed_registered_policy(self):
        with self.assertRaises(LiveError) as rejected:
            self.policy_upload(retain_until=1000 + 86_400_000 + 1)
        self.assertEqual(rejected.exception.code, "invalid_retention")

    def test_host_membership_does_not_replace_project_collection_registration(self):
        self.server.registered_projects.clear()
        with self.assertRaises(LiveError) as rejected:
            self.policy_upload()
        self.assertEqual(rejected.exception.code, "artifact_policy_required")

    def test_admission_persists_the_actual_project_and_collection_policy_digests(self):
        value = self.policy_upload()
        self.assertEqual(value.get("projectDigest"), self.registration.project_digest)
        self.assertEqual(value.get("collectionPolicyDigest"),
                         contracts.digest(self.registration.collection_policy))

    def test_local_unbound_blob_is_inert_at_the_network_boundary(self):
        body = b"local-only incomplete policy binding"
        digest = hashlib.sha256(body).hexdigest()
        upload = self.transfer.allocate(
            project_id="checkout", host_identity=self.server.host_identity,
            kind="manifest", size=len(body), digest=digest, metadata={},
            retention_class="original", retain_until_ms=2000)
        self.transfer.put_chunk(upload["objectId"], upload["uploadGeneration"],
                                0, body, digest, host_identity=self.server.host_identity)
        self.transfer.finalize(upload["objectId"], upload["uploadGeneration"],
                               host_identity=self.server.host_identity)
        with self.assertRaises(LiveError) as rejected:
            self.client.download_artifact(upload["objectId"], project_id="checkout")
        self.assertEqual(rejected.exception.code, "artifact_policy_required")
