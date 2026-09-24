"""Reject changed preparation before touching a backend or starting a device."""
import copy
import json
import unittest

from reproof import contracts
from reproof.fixtures import FixtureCoordinator
from reproof.live.issue_sessions import FixturePreparation, IssueSessionError
from tests.g4_support import G4Environment, qualification, runtime_policy, specification


class PreparationBindingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.env = G4Environment()
        self.original = self.env.record_original()
        self.approved = self.env.approve(self.original)

    def tearDown(self):
        self.env.close()

    def operations(self):
        return json.loads(self.env.remote.state.read_text())["operations"]

    def replay(self, preparations):
        return self.env.service.replay(
            self.env.registry.original_execution(self.approved), registration=self.env.registration,
            device_id="device", owner="owner", controller_id="binding", preparations=preparations)

    def test_changed_payload_is_rejected_before_remote_preparation(self):
        before = self.operations()
        with self.assertRaises(IssueSessionError):
            self.replay(self.env.preparations({"account": "different"}))
        self.assertEqual(self.operations(), before)
        self.assertEqual(self.env.lab.list_devices()[0]["state"], "available")

    def test_duplicate_fixture_is_rejected_before_any_remote_operation(self):
        before = self.operations()
        with self.assertRaises(IssueSessionError):
            self.replay(self.env.preparations() * 2)
        self.assertEqual(self.operations(), before)

    def test_changed_trusted_plan_cannot_skip_an_approved_check(self):
        self.env.fixtures.close()
        self.env.fixtures = FixtureCoordinator(self.env.root / "replacement-fixtures")
        self.env.service.fixtures = self.env.fixtures
        changed = self.env.fixtures.register_plan(
            self.env.registration, application_id="ios_app", fixture_id="seed_account",
            adapter=self.env.adapter, check_recipe_ids=(), cleanup_recipe_id="cleanup_account")
        self.assertNotEqual(changed.equivalence_digest, self.env.plan.equivalence_digest)
        before = self.operations()
        with self.assertRaises(IssueSessionError):
            self.replay([FixturePreparation(changed, {})])
        self.assertEqual(self.operations(), before)

    def approve_changed_original(self, original):
        original["recordingDigest"] = contracts.digest(original["original"])
        spec = specification(original, revision=2)
        return self.env.registry.register(
            self.env.registration, original, spec,
            qualification(self.env.project, original, spec, self.env.plan),
            runtime_policy(), fixture_plans=(self.env.plan,))

    def test_wrong_operation_receipt_does_not_establish_preparation(self):
        original = copy.deepcopy(self.original)
        original["original"]["preparation"][0]["operation"] = "check"
        approved = self.approve_changed_original(original)
        self.assertFalse(approved.preparation_known)

    def test_unknown_duplicate_receipt_does_not_become_known_from_a_complete_receipt(self):
        original = copy.deepcopy(self.original)
        receipt = copy.deepcopy(original["original"]["preparation"][0])
        receipt.update(receiptId="uncertain_preparation", status="unknown")
        original["original"]["preparation"].append(receipt)
        approved = self.approve_changed_original(original)
        self.assertFalse(approved.preparation_known)


if __name__ == "__main__":
    unittest.main()
