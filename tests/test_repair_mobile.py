"""The qualified mobile scope encloses installation, checks, replay and cleanup."""
from dataclasses import replace
import threading
import unittest

from reproloop import contracts
from reproloop.execution.journal import RunStore
from reproloop.qualification import QualificationError
from reproloop.repair_execution import RepairExecutionError
from tests.g9_support import RepairEnvironment
from tests.g9_execution_support import SyntheticRepairExecution


class ProtectedMobileTests(unittest.TestCase):
    def setUp(self):
        self.env = RepairEnvironment(); self.addCleanup(self.env.close)
        self.runtime = SyntheticRepairExecution(self.env); self.addCleanup(self.runtime.close)
        self.signed = self.runtime.signed_build()
        self.progress = []

    def verify(self, mobile=None, cancellation=None, operation='mobile_candidate'):
        try:
            return (mobile or self.runtime.mobile()).verify(self.signed, self.env.approved,
                operation_id=operation, cancellation=cancellation or threading.Event(),
                boundary=lambda: None, progress=lambda phase, data: self.progress.append((phase, data)))
        except RepairExecutionError as error:
            error.add_note('Synthetic replay error codes: ' + repr(self.runtime.replay_errors))
            raise

    def test_full_scope_requires_all_three_unchanged_replays_and_final_cleanup(self):
        mobile = self.runtime.mobile(); result = self.verify(mobile)
        mobile.require_verified(result, self.signed, self.env.approved)
        document = result.public()
        self.assertEqual(len(document['attempts']), 3)
        self.assertEqual(len(self.runtime.installs), 1); self.assertEqual(len(self.runtime.cleanups), 1)
        self.assertTrue(document['cleanupConfirmed'])
        self.assertFalse(document['verified'])
        self.assertEqual({item['specificationDigest'] for item in document['attempts']},
                         {self.env.approved.specification_digest})
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['state'], 'succeeded')
        self.assertEqual(self.env.lab.devices['device']['capabilities']['applicationIdentity'], self.runtime.original_identity)
        for execution in self.runtime.executions:
            with self.assertRaises(QualificationError): self.env.registry.require_execution(execution)
        for forged in (document, replace(result)):
            with self.assertRaises(RepairExecutionError): mobile.require_verified(forged, self.signed, self.env.approved)

    def test_missing_or_revoked_mobile_qualification_prevents_install(self):
        with self.assertRaises(RepairExecutionError): self.verify(self.runtime.mobile(qualification=False))
        self.runtime.authority.revoke_backend('synthetic-device', 'mobile-device', self.runtime.mobile_route.environment_digest)
        with self.assertRaises(RepairExecutionError): self.verify()
        self.assertEqual(self.runtime.installs, [])

    def test_candidate_regression_report_cannot_replace_independent_checks(self):
        self.runtime.verification_result = 'json'
        with self.assertRaises(RepairExecutionError) as caught: self.verify()
        self.assertEqual(caught.exception.code, 'mobile_quarantined')
        self.assertTrue(any(data.get('validation', {}).get('status') == 'quarantined'
                            for _, data in self.progress))
        self.assertEqual(self.runtime.replays, [])
        self.assertEqual(len(self.runtime.cleanups), 1)
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['state'], 'quarantined')

    def test_a_copied_g4_result_cannot_claim_candidate_success(self):
        self.runtime.next_value = 'error'; self.runtime.forge_result = True
        with self.assertRaises(RepairExecutionError) as caught: self.verify()
        self.assertEqual(caught.exception.code, 'mobile_quarantined')
        self.assertEqual(self.progress[-1][1]['attempts'][0]['reason'], 'untrusted_replay')
        self.assertEqual(len(self.runtime.cleanups), 1)
        self.assertEqual(len(self.runtime.replays), 1)

    def test_irrelevant_patch_fails_after_fixed_repetitions_without_retrying(self):
        self.runtime.next_value = 'error'
        with self.assertRaises(RepairExecutionError) as caught: self.verify()
        self.assertEqual(caught.exception.code, 'candidate_mismatch',
            (self.runtime.replay_errors,
             [(r.cleanup, r.verdict, r.valid) for r in self.runtime.replays]))
        self.assertEqual(len(self.runtime.replays), 3)
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['state'], 'failed')
        self.assertEqual(len(self.progress[-2][1]['attempts']), 3)

    def test_cancellation_after_one_run_revokes_capability_and_cleans_up(self):
        cancelled = threading.Event(); self.runtime.cancel_after_replay = 1; self.runtime.cancel_target = cancelled
        with self.assertRaises(RepairExecutionError) as caught: self.verify(cancellation=cancelled)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertEqual(len(self.runtime.replays), 1); self.assertEqual(len(self.runtime.cleanups), 1)
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['state'], 'cancelled')
        with self.assertRaises(QualificationError): self.env.registry.require_execution(self.runtime.executions[0])

    def test_uncertain_sanitation_blocks_same_and_new_journal_reuse(self):
        self.runtime.fail_cleanup = True
        with self.assertRaises(RepairExecutionError) as caught: self.verify()
        self.assertEqual(caught.exception.code, 'mobile_quarantined')
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['state'], 'quarantined')
        other_store = RunStore(self.env.root / 'bypass-state',
            environment_digest=self.runtime.mobile_route.environment_digest, disk_limit=1)
        for store in (self.runtime.mobile_store, other_store):
            with self.assertRaises(RepairExecutionError):
                self.verify(self.runtime.mobile(store=store), operation='retry_mobile')
        self.assertEqual(len(self.runtime.installs), 1)

    def test_install_timeout_cannot_publish_a_late_pass(self):
        released = threading.Event(); self.runtime.install_wait = released
        try:
            with self.assertRaises(RepairExecutionError) as caught: self.verify(self.runtime.mobile(timeout=.03))
            self.assertEqual(caught.exception.code, 'mobile_quarantined')
        finally:
            released.set(); self.assertTrue(self.runtime.install_returned.wait(1))
        self.assertEqual(len(self.runtime.cleanups), 1)
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['state'], 'quarantined')


if __name__ == '__main__': unittest.main()
