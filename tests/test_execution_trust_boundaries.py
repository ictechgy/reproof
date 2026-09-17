"""Root-owned acceptance tests; goal workers must not weaken these checks."""
import copy
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest import mock

from reproloop.core import ContractError
from reproloop.execution import backend as backend_module
from reproloop.execution.backend import ExecutionDenied, QualificationAuthority
from tests.test_execution_backend import REQUIRED_BUILD_PROBES, qualification_record
from tests.test_execution_protocol import build_request, build_route, validation_plan


class ExecutionTrustBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.authority = QualificationAuthority()

    def receipts(self, **changes):
        values = []
        for index, probe in enumerate(REQUIRED_BUILD_PROBES):
            fields = dict(probe_id=probe, backend_id="apple-vm", execution_class="build-guest",
                          environment_digest="2" * 64, outcome="pass",
                          evidence_digest=str(index + 4) * 64, observed_at_ms=1000)
            fields.update(changes)
            values.append(self.authority.record_probe(**fields))
        return values

    def test_declared_probe_list_cannot_remove_required_isolation_checks(self):
        record = qualification_record()
        record["probeIds"] = ["boot"]
        with self.assertRaises((ExecutionDenied, ContractError)):
            self.authority.issue_backend_qualification(record, self.receipts()[:1], evaluated_at_ms=1200)

    def test_receipts_from_another_environment_or_backend_cannot_be_reused(self):
        for change in ({"environment_digest": "a" * 64}, {"backend_id": "another-backend"}):
            with self.subTest(change=tuple(change)):
                with self.assertRaises((ExecutionDenied, ContractError)):
                    self.authority.issue_backend_qualification(
                        qualification_record(), self.receipts(**change), evaluated_at_ms=1200)

    def test_duplicate_probe_receipts_do_not_replace_a_missing_check(self):
        receipts = self.receipts()
        receipts[-1] = receipts[0]
        with self.assertRaises((ExecutionDenied, ContractError)):
            self.authority.issue_backend_qualification(qualification_record(), receipts, evaluated_at_ms=1200)

    def test_future_probe_receipts_cannot_authorize_present_execution(self):
        with self.assertRaises((ExecutionDenied, ContractError)):
            self.authority.issue_backend_qualification(
                qualification_record(), self.receipts(observed_at_ms=3000), evaluated_at_ms=1200)

    def test_a_validation_plan_for_another_project_does_not_transfer_authority(self):
        qualification = self.authority.issue_backend_qualification(
            qualification_record(), self.receipts(), evaluated_at_ms=1200)
        good = self.authority.register_validation_plan(validation_plan())
        route = self.authority.register_execution_route(build_route())
        self.authority.authorize(build_request(), qualification=qualification, validation_plan=good,
                                 execution_route=route, evaluated_at_ms=1200)
        plan = copy.deepcopy(validation_plan())
        plan["id"] = "another-project-plan"
        plan["projectDigest"] = "b" * 64
        trusted_plan = self.authority.register_validation_plan(plan)
        route_value = build_route()
        route_value.update(id="cross-project-route", validationPlanId=plan["id"])
        route = self.authority.register_execution_route(route_value)
        with self.assertRaises((ExecutionDenied, ContractError)):
            self.authority.authorize(build_request(), qualification=qualification,
                                     validation_plan=trusted_plan, execution_route=route,
                                     evaluated_at_ms=1200)

    def test_registered_validation_recipe_cannot_change_under_the_same_identity(self):
        plan = validation_plan()
        self.authority.register_validation_plan(plan)
        self.authority.register_validation_plan(copy.deepcopy(plan))
        plan["checks"][0]["recipeId"] = "a-different-recipe"
        with self.assertRaises((ExecutionDenied, ContractError)):
            self.authority.register_validation_plan(plan)

    def test_signing_identity_cannot_change_under_an_existing_policy_id(self):
        policy = {
            "schemaVersion": 1, "id": "approved-signing", "platform": "ios",
            "applicationId": "checkout-app", "identityReferenceId": "approved-identity",
            "entitlementsDigest": "1" * 64, "provisioningReferenceId": "approved-provision",
            "tool": "host-codesign-fixed", "candidateHooks": "forbidden",
            "artifactRelation": "pre-post-digests",
        }
        self.authority.register_signing_policy(policy)
        self.authority.register_signing_policy(copy.deepcopy(policy))
        policy["identityReferenceId"] = "a-different-identity"
        with self.assertRaises((ExecutionDenied, ContractError)):
            self.authority.register_signing_policy(policy)

    def test_concurrent_registration_cannot_create_two_definitions_for_one_id(self):
        constructor = backend_module.TrustedValidationPlan
        ready = threading.Barrier(2)

        def slow_constructor(*args, **kwargs):
            time.sleep(0.02)
            return constructor(*args, **kwargs)

        def register(recipe):
            plan = validation_plan()
            plan["checks"][0]["recipeId"] = recipe
            ready.wait(timeout=2)
            try:
                self.authority.register_validation_plan(plan)
                return "accepted"
            except ExecutionDenied:
                return "rejected"

        with mock.patch.object(backend_module, "TrustedValidationPlan", side_effect=slow_constructor):
            with ThreadPoolExecutor(max_workers=2) as workers:
                outcomes = list(workers.map(register, ("first-recipe", "second-recipe")))
        self.assertEqual(sorted(outcomes), ["accepted", "rejected"])


if __name__ == "__main__":
    unittest.main()
