"""Known native failures still require final cleanup and never issue a pass."""
import unittest

from reproloop.qualification import QualificationError
from reproloop.repair_execution import RepairExecutionError
from tests import test_repair_mobile as mobile_tests


class MobileFailureTests(unittest.TestCase):
    setUp = mobile_tests.ProtectedMobileTests.setUp
    verify = mobile_tests.ProtectedMobileTests.verify

    def install_failure(self, **changes):
        from reproloop.repair_mobile import MobileFailureObservation
        def failed(context, artifacts, **kwargs):
            values = dict(context_digest=context.digest, code='mobile_install_failed',
                          evidence_digest='a' * 64, effects_settled=True)
            return MobileFailureObservation(**{**values, **changes})
        self.runtime.install = failed

    def test_known_install_failure_releases_budget_only_after_final_cleanup(self):
        self.install_failure(); mobile = self.runtime.mobile()
        with self.assertRaises(RepairExecutionError) as caught: self.verify(mobile)
        self.assertEqual(caught.exception.code, 'mobile_install_failed')
        self.assertEqual(self.runtime.replays, [])
        self.assertEqual(len(self.runtime.cleanups), 1)
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['state'], 'failed')
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['reservedBytes'], 0)
        self.assertEqual(mobile._proofs, {})

    def test_known_replay_failure_revokes_candidate_and_requires_restore(self):
        from reproloop.repair_mobile import MobileFailureObservation
        replay = self.runtime.replay
        def fail_second(context, execution, number, **kwargs):
            if number == 2:
                return MobileFailureObservation(context.digest, 'mobile_replay_failed', 'a' * 64, True)
            return replay(context, execution, number, **kwargs)
        self.runtime.replay = fail_second; mobile = self.runtime.mobile()
        with self.assertRaises(RepairExecutionError) as caught: self.verify(mobile)
        self.assertEqual(caught.exception.code, 'mobile_replay_failed')
        self.assertEqual(len(self.runtime.replays), 1)
        self.assertEqual(len(self.runtime.cleanups), 1)
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['state'], 'failed')
        for execution in self.runtime.executions:
            with self.assertRaises(QualificationError): self.env.registry.require_execution(execution)
        self.assertEqual(mobile._proofs, {})

    def test_known_failure_with_unconfirmed_final_sanitation_keeps_quarantine(self):
        self.install_failure(); self.runtime.fail_cleanup = True
        with self.assertRaises(RepairExecutionError) as caught: self.verify()
        self.assertEqual(caught.exception.code, 'mobile_quarantined')
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['reservedBytes'], 1)

    def test_unknown_effects_cannot_become_clean_just_because_cleanup_returns(self):
        self.install_failure(effects_settled=False)
        with self.assertRaises(RepairExecutionError) as caught: self.verify()
        self.assertEqual(caught.exception.code, 'mobile_quarantined')
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['state'], 'quarantined')

    def test_wrong_context_and_wrong_stage_failure_cannot_release_the_scope(self):
        self.install_failure(context_digest='f' * 64, code='mobile_replay_failed')
        with self.assertRaises(RepairExecutionError) as caught: self.verify()
        self.assertEqual(caught.exception.code, 'mobile_quarantined')
        self.assertEqual(self.runtime.mobile_store.status('mobile_candidate')['state'], 'quarantined')


if __name__ == '__main__': unittest.main()
