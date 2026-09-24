"""General repair build provenance comes from the protected G8 supervisor."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from reproof.contracts import digest
from reproof.execution.artifacts import ArtifactValidationAuthority, BlobSet
from reproof.execution.backend import QualificationAuthority, REQUIRED_PROBES
from reproof.execution.journal import RunStore
from reproof.execution.resources import provision
from reproof.execution.runtime import MacOSVirtualizationBackend
from tests.test_execution_resources import resource_inputs
from tests.test_execution_runtime import VMDouble
from tests.test_execution_protocol import build_route, validation_plan


class ProtectedRepairBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        metadata, paths = resource_inputs(self.root)
        self.bundle = provision(self.root / 'bundle', metadata=metadata, resources=paths)
        self.authority = QualificationAuthority()
        self.store = RunStore(self.root / 'vm-state', environment_digest=self.bundle.environment_digest, disk_limit=1024)
        self.backend = MacOSVirtualizationBackend('apple-vm', self.authority, self.bundle, self.store)
        self.plan = self.authority.register_validation_plan(validation_plan())
        route = build_route(); route.update(environmentDigest=self.bundle.environment_digest,
                                            recipeId='build', cleanupPolicyId='dispose-overlay')
        self.route = self.authority.register_execution_route(route)
        now = int(time.time() * 1000)
        probes = sorted(REQUIRED_PROBES['build-guest'])
        receipts = [self.authority.record_probe(probe_id=probe, backend_id='apple-vm', execution_class='build-guest',
            environment_digest=self.bundle.environment_digest, outcome='pass', evidence_digest='a' * 64,
            observed_at_ms=now) for probe in probes]
        self.qualification = self.authority.issue_backend_qualification({'schemaVersion': 1, 'id': 'explicit-test-double',
            'backendId': 'apple-vm', 'executionClass': 'build-guest', 'environmentDigest': self.bundle.environment_digest,
            'issuedAtMs': now, 'expiresAtMs': now + 60000, 'probeIds': probes}, receipts, evaluated_at_ms=now)
        self.artifacts = ArtifactValidationAuthority()
        self.artifacts.register('bounded-artifacts', paths=('product.bin',), max_bytes=4096,
                                checker=lambda blobs: blobs.entries[0][1] == b'candidate artifact')
        self.source = BlobSet((('src/Checkout.swift', b'candidate product logic'),))
        VMDouble.instances = []; VMDouble.stop_confirmed = True; VMDouble.wait_for_cancel = False
        VMDouble.started_recipe = threading.Event()
        patch = mock.patch('reproof.execution.runtime.NativeVM', VMDouble)
        patch.start(); self.addCleanup(patch.stop)

    def supervisor(self, qualification='configured'):
        from reproof.repair_execution import ProtectedBuildSupervisor
        return ProtectedBuildSupervisor(self.backend, qualification=self.qualification if qualification == 'configured' else None,
            route=self.route, validation_plan=self.plan, artifact_authority=self.artifacts,
            application_id='ios_app', artifact_identity='file-sha256')

    def build(self, supervisor, cancellation=None, operation='candidate_build'):
        return supervisor.build(self.source, operation_id=operation, repair_plan_digest='b' * 64,
                                cancellation=cancellation or threading.Event())

    def test_completed_guest_build_issues_bound_provenance_but_not_verified(self):
        from reproof.repair_execution import RepairExecutionError
        supervisor = self.supervisor(); proof = self.build(supervisor)
        self.assertFalse(proof.public()['verified'])
        self.assertEqual(proof.public()['sourceDigest'], self.source.digest)
        self.assertTrue(proof.public()['cleanupConfirmed'])
        supervisor.require_build(proof, source_digest=self.source.digest, repair_plan_digest='b' * 64)
        self.assertEqual(self.store.status('candidate_build')['state'], 'succeeded')
        with self.assertRaises(RepairExecutionError):
            supervisor.require_build(replace(proof, source_digest='f' * 64),
                source_digest='f' * 64, repair_plan_digest='b' * 64)
        with self.assertRaises(RepairExecutionError):
            supervisor.require_build(proof.public(), source_digest=self.source.digest, repair_plan_digest='b' * 64)

    def test_missing_qualification_and_revocation_prevent_guest_start(self):
        from reproof.repair_execution import RepairExecutionError
        with self.assertRaises(RepairExecutionError): self.build(self.supervisor(None))
        self.assertEqual(VMDouble.instances, [])
        self.authority.revoke_backend('apple-vm', 'build-guest', self.bundle.environment_digest)
        with self.assertRaises(RepairExecutionError): self.build(self.supervisor())
        self.assertEqual(VMDouble.instances, [])

    def test_unconfirmed_vm_stop_hides_artifacts_and_retains_quarantine(self):
        from reproof.repair_execution import RepairExecutionError
        VMDouble.stop_confirmed = False
        with self.assertRaises(RepairExecutionError) as caught: self.build(self.supervisor())
        self.assertEqual(caught.exception.code, 'build_quarantined')
        self.assertEqual(self.store.status('candidate_build')['state'], 'quarantined')

    def test_job_cancellation_reaches_the_guest_journal_and_discards_outputs(self):
        from reproof.repair_execution import RepairExecutionError
        VMDouble.wait_for_cancel = True
        cancelled = threading.Event(); errors = []
        def build():
            try: self.build(self.supervisor(), cancelled)
            except RepairExecutionError as error: errors.append(error.code)
        thread = threading.Thread(target=build); thread.start()
        self.assertTrue(VMDouble.started_recipe.wait(2))
        cancelled.set(); thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['cancelled'])
        self.assertEqual(self.store.status('candidate_build')['state'], 'cancelled')


if __name__ == '__main__': unittest.main()
