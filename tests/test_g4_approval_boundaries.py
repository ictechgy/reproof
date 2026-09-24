"""Approval binds actual immutable bytes and the G0 full build identity."""
import copy
import unittest

from reproof import contracts
from reproof.qualification import QualificationError
from tests.g4_support import G4Environment


class ApprovalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.env = G4Environment()
        self.original = self.env.record_original()
        self.approved = self.env.approve(self.original)

    def tearDown(self):
        self.env.close()

    def test_mutating_approved_specification_requires_a_new_approval(self):
        self.approved.specification["waits"] = [{"afterEventId": "event_1", "durationMs": 200}]
        with self.assertRaises(QualificationError):
            self.env.registry.require(self.approved)

    def test_nested_project_policy_and_budget_mutations_are_rejected(self):
        for change in (
            lambda item: item.project["builds"][0].update(artifactDigest="6" * 64),
            lambda item: item.runtime_policy["attestations"].append("changed_condition"),
            lambda item: item.qualification["attemptBudget"].update(original=4, total=7),
        ):
            with self.subTest(change=change.__code__.co_firstlineno):
                approved = self.env.approve(self.original)
                change(approved)
                with self.assertRaises(QualificationError):
                    self.env.registry.require(approved)

    def test_execution_has_an_independent_snapshot_of_approved_bytes(self):
        execution = self.env.registry.original_execution(self.approved)
        self.approved.specification["waits"] = [{"afterEventId": "event_1", "durationMs": 200}]
        self.assertEqual(contracts.digest(execution.approved.specification),
                         execution.approved.specification_digest)

    def candidate(self, build_digest):
        candidate = {"schemaVersion": 1, "qualificationDigest": self.approved.qualification_digest,
                     "sourceBuildId": "candidate", "candidateBuildDigest": build_digest,
                     "originalRecordingDigest": self.approved.recording_digest,
                     "specificationDigest": self.approved.specification_digest}
        approval = contracts.issue_substitution_approval(
            qualification_digest=self.approved.qualification_digest,
            recording_digest=self.approved.recording_digest,
            specification_digest=self.approved.specification_digest,
            candidate_build_id="candidate", candidate_build_digest=build_digest)
        return candidate, approval

    def test_candidate_approval_uses_the_full_build_manifest_digest(self):
        build = next(item for item in self.env.project["builds"] if item["id"] == "candidate")
        candidate, approval = self.candidate(contracts.digest(contracts.validate_build_identity(build)))
        execution = self.env.registry.candidate_execution(self.approved, candidate, approval)
        self.assertEqual(execution.build_id, "candidate")
        self.assertEqual(execution.build_digest, build["artifactDigest"])
        self.assertEqual(execution.approved.qualification_digest, self.approved.qualification_digest)

    def test_artifact_digest_alone_cannot_substitute_for_the_build_manifest(self):
        build = next(item for item in self.env.project["builds"] if item["id"] == "candidate")
        candidate, approval = self.candidate(build["artifactDigest"])
        with self.assertRaises(QualificationError):
            self.env.registry.candidate_execution(self.approved, candidate, approval)


if __name__ == "__main__":
    unittest.main()
