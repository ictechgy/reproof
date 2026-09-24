"""End-to-end G9 composition uses explicit VM/device doubles and real G4 runs."""
import threading
import unittest

from reproof import contracts
from reproof.project_repair import ProjectRepair
from reproof.repair_journal import RepairJournal
from tests.g9_support import RepairEnvironment
from tests.g9_execution_support import SyntheticRepairExecution
from tests.test_project_repair import BEFORE, PRODUCT
from tests.test_project_repair_jobs import LocalProposalDouble
from tests.test_execution_runtime import VMDouble


class ProjectRepairVerificationTests(unittest.TestCase):
    def setUp(self):
        self.env = RepairEnvironment(); self.addCleanup(self.env.close)
        self.baseline = self.env.baseline()
        self.runtime = SyntheticRepairExecution(self.env); self.addCleanup(self.runtime.close)
        self.journal = RepairJournal(self.env.root / 'repairs', disk_limit=256*1024*1024)
        self.addCleanup(self.journal.close)
        self.agent = LocalProposalDouble()

    def repair(self, executor=None):
        return ProjectRepair(self.env.source, self.env.registry, self.env.engine, self.journal, self.agent,
            build_recipe_id='build_app', validation_recipe_ids=('regression_ui',),
            executor=executor or self.runtime.executor())

    def run_job(self, repair=None, *, authorize=lambda: True):
        self.active = repair or self.repair()
        job = self.active.create(self.env.approved, campaign_id=self.baseline['campaignId'], issue_id='issue',
            owner_id='owner', request_id='verify_request', mode='verify')
        self.job_id = job['id']
        return self.active.execute(job['id'], self.env.approved, authorize=authorize)

    def test_verified_job_has_protected_build_and_before_after_evidence(self):
        repair = self.repair()
        self.assertTrue(repair.availability()['verificationAvailable'])
        job = self.run_job(repair)
        self.assertEqual((job['status'], job['result']['verified']), ('verified', True), job['reason'])
        evidence = job['result']['afterEvidence']
        self.assertEqual(len(evidence['attempts']), 3)
        self.assertEqual(evidence['specificationDigest'], self.env.approved.specification_digest)
        self.assertEqual(evidence['candidateBuild']['sourceDigest'], job['result']['candidateSourceDigest'])
        self.assertEqual(job['result']['beforeEvidence']['digest'], contracts.digest(self.baseline))
        self.assertEqual(job['plan']['attemptBudget'], {'original': 3, 'candidate': 3, 'total': 6})
        self.assertTrue(evidence['cleanupConfirmed'])
        self.assertEqual(job['result']['build']['repairPlanDigest'], job['plan']['digest'])
        self.assertEqual(job['result']['signing']['unsignedArtifactDigest'], job['result']['build']['artifactDigest'])
        self.assertEqual((self.env.root / 'source' / PRODUCT).read_bytes(), BEFORE)
        self.assertFalse(repair.proposal(job['id'])['verified'])
        self.assertEqual(self.agent.calls, 1)
        self.assertEqual(repair.execute(job['id'], self.env.approved, authorize=lambda: True), job)
        self.assertEqual(len(VMDouble.instances), 1)

    def test_missing_mobile_qualification_blocks_before_ai_or_guest(self):
        executor = self.runtime.executor(); executor.mobile.qualification = None
        job = self.run_job(self.repair(executor))
        self.assertEqual(job['status'], 'blocked')
        self.assertFalse(job['result']['verified'])
        self.assertEqual(self.agent.calls, 0); self.assertEqual(VMDouble.instances, [])

    def test_revocation_during_proposal_prevents_build_and_install(self):
        self.agent.effect = lambda _: self.runtime.authority.revoke_backend('synthetic-device', 'mobile-device',
            self.runtime.mobile_route.environment_digest)
        job = self.run_job()
        self.assertNotEqual(job['status'], 'verified')
        self.assertEqual(len(VMDouble.instances), 0); self.assertEqual(self.runtime.installs, [])

    def test_unknown_guest_stop_quarantines_job_before_signing_or_mobile(self):
        VMDouble.stop_confirmed = False
        job = self.run_job()
        self.assertEqual(job['status'], 'quarantined')
        self.assertEqual(self.runtime.installs, [])
        self.assertFalse(job['result']['verified'])

    def test_invalid_signature_and_failed_regression_cannot_verify(self):
        self.runtime.inspection_valid = False
        job = self.run_job()
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['reason'], 'signature_invalid')
        self.assertEqual(self.runtime.installs, [])
        self.assertIsNone(job['result']['afterEvidence'])

    def test_three_failed_candidate_attempts_remain_visible(self):
        self.runtime.next_value = 'error'
        job = self.run_job()
        self.assertEqual((job['status'], job['reason']), ('failed', 'candidate_mismatch'))
        rows = job['attempts'][0]['execution']['attempts']
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['defect'] is True and row['expected'] is False for row in rows))
        self.assertIsNone(job['result']['afterEvidence'])

    def test_source_change_during_cleanup_denies_final_publication(self):
        cleanup = self.runtime.cleanup
        def change(context, **kwargs):
            observation = cleanup(context, **kwargs)
            (self.env.root / 'source' / PRODUCT).write_bytes(b'concurrent user edit')
            return observation
        self.runtime.cleanup = change
        job = self.run_job()
        self.assertNotEqual(job['status'], 'verified')
        self.assertFalse(job['result']['verified'])
        self.assertEqual(len(self.runtime.cleanups), 1)
        self.assertEqual((self.env.root / 'source' / PRODUCT).read_bytes(), b'concurrent user edit')

    def test_permission_loss_cancels_dispatch_and_unknown_cleanup_stays_quarantined(self):
        allowed = [True]; cleanup = self.runtime.cleanup
        def revoke(context, **kwargs):
            allowed[0] = False
            return cleanup(context, **kwargs)
        self.runtime.cleanup = revoke; self.runtime.fail_cleanup = True
        job = self.run_job(authorize=lambda: allowed[0])
        self.assertEqual(job['status'], 'quarantined')
        self.assertTrue(job['cancelRequested'])
        self.assertFalse(job['result']['verified'])

    def test_existing_mobile_quarantine_blocks_before_another_ai_proposal(self):
        self.runtime.fail_cleanup = True
        first = self.run_job()
        self.assertEqual(first['status'], 'quarantined')
        self.assertFalse(self.active.availability()['verificationAvailable'])
        second = self.active.create(self.env.approved, campaign_id=self.baseline['campaignId'], issue_id='issue',
            owner_id='owner', request_id='second_verify', mode='verify')
        completed = self.active.execute(second['id'], self.env.approved, authorize=lambda: True)
        self.assertNotEqual(completed['status'], 'verified')
        self.assertEqual(self.agent.calls, 1)
        self.assertEqual(len(VMDouble.instances), 1)

    def test_executor_for_a_different_application_is_denied_before_proposal(self):
        executor = self.runtime.executor()
        executor.builder.application_id = 'different_app'
        job = self.run_job(self.repair(executor))
        self.assertNotEqual(job['status'], 'verified')
        self.assertEqual(self.agent.calls, 0)
        self.assertEqual(VMDouble.instances, [])
        self.assertEqual(self.runtime.installs, [])


if __name__ == '__main__': unittest.main()
