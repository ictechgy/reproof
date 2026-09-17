"""A measured candidate build substitutes only its identity, not the frozen QA contract."""
import copy
from dataclasses import replace
import time
import unittest

from reproloop import contracts
from reproloop.qualification import QualificationError
from tests.g9_support import RepairEnvironment


class RepairCandidateBindingTests(unittest.TestCase):
    def setUp(self):
        self.env = RepairEnvironment(); self.addCleanup(self.env.close)
        self.build = {'id': 'candidate_new_product', 'applicationId': 'ios_app', 'revision': 'patch_1',
            'sourceDigest': 'a' * 64, 'artifactDigest': 'b' * 64, 'provenance': 'trusted-build'}

    def approval(self):
        return contracts.issue_substitution_approval(qualification_digest=self.env.approved.qualification_digest,
            recording_digest=self.env.approved.recording_digest, specification_digest=self.env.approved.specification_digest,
            candidate_build_id=self.build['id'], candidate_build_digest=contracts.digest(self.build))

    def test_new_candidate_replays_same_spec_without_changing_registered_project(self):
        before = contracts.digest(self.env.registration.project)
        execution = self.env.registry.authorize_candidate_build(self.env.approved, self.build, self.approval())
        self.env.lab.devices['device']['capabilities']['applicationIdentity']['artifactDigest'] = self.build['artifactDigest']
        self.env.observations._adapters['screen'].value = 'success'
        result = self.env.service.replay(execution, registration=self.env.registration, device_id='device',
            owner='owner', controller_id='candidate_replay', preparations=self.env.preparations())
        self.assertEqual((result.defect, result.expected, result.cleanup), (False, True, 'complete'))
        self.assertEqual(result.build_id, self.build['id'])
        self.assertEqual(result.specification_digest, self.env.approved.specification_digest)
        self.assertEqual(contracts.digest(self.env.registration.project), before)
        self.assertNotIn(self.build, self.env.registration.project['builds'])

    def test_unapproved_changed_or_original_overwriting_build_is_rejected(self):
        for build, approval in ((self.build, {}), (dict(self.build, artifactDigest='f' * 64), self.approval()),
                                (dict(self.build, id='original'), self.approval())):
            with self.subTest(build=build), self.assertRaises(QualificationError):
                self.env.registry.authorize_candidate_build(self.env.approved, build, approval)

    def test_revocation_and_wrong_install_are_checked_before_fixture_effects(self):
        execution = self.env.registry.authorize_candidate_build(self.env.approved, self.build, self.approval())
        before = len(self.env.control['calls'])
        with self.assertRaises(Exception):
            self.env.service.replay(execution, registration=self.env.registration, device_id='device',
                owner='owner', controller_id='wrong_build', preparations=self.env.preparations())
        self.assertEqual(len(self.env.control['calls']), before)
        self.env.registry.revoke_candidate_build(execution.candidate_binding)
        with self.assertRaises(QualificationError): self.env.registry.require_execution(execution)
        self.assertEqual(self.env.lab.devices['device']['state'], 'available')

    def test_protected_verification_requires_this_runners_exact_final_result(self):
        from reproloop.scenario_runner import ScenarioError
        execution = self.env.registry.authorize_candidate_build(self.env.approved, self.build, self.approval())
        self.env.lab.devices['device']['capabilities']['applicationIdentity']['artifactDigest'] = self.build['artifactDigest']
        self.env.observations._adapters['screen'].value = 'success'
        started = time.monotonic_ns()
        result = self.env.service.replay(execution, registration=self.env.registration, device_id='device',
            owner='owner', controller_id='bound_result', preparations=self.env.preparations())
        self.assertIs(self.env.runner.require_finalized(result, execution, started_after_ns=started), result)
        for forged in (result.public(), replace(result), replace(result, defect=True)):
            with self.subTest(forged=type(forged)), self.assertRaises(ScenarioError):
                self.env.runner.require_finalized(forged, execution, started_after_ns=started)
        with self.assertRaises(ScenarioError):
            self.env.runner.require_finalized(result, execution, started_after_ns=time.monotonic_ns())
        with self.assertRaises(ScenarioError):
            self.env.runner.finalize(replace(result), cleanup='complete', attempt_recording_digest='f' * 64)

    def test_concurrent_approval_checks_preserve_the_same_immutable_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        def check(_):
            for _ in range(80):
                self.env.registry.require(self.env.approved)
        with ThreadPoolExecutor(max_workers=6) as workers:
            list(workers.map(check, range(6)))

    def test_active_binding_and_original_reads_share_one_recording_snapshot_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        handle = self.env.service.start_prepared_recording(device_id='device', owner='owner',
            controller_id='binding_reader', registration=self.env.registration, application_id='ios_app',
            build_id='original', preparations=self.env.preparations())
        self.addCleanup(lambda: self.env.service.stop(handle))
        def read(which):
            for _ in range(120):
                if which % 2:
                    binding = self.env.lab.release_binding(handle.session_id, 'owner')
                    self.assertEqual(binding['buildId'], 'original')
                else:
                    record = self.env.lab.release_recording(self.env.original['recordingId'], 'owner')
                    self.assertEqual(record['recordingDigest'], self.env.original['recordingDigest'])
        with ThreadPoolExecutor(max_workers=6) as workers:
            list(workers.map(read, range(6)))


if __name__ == '__main__': unittest.main()
