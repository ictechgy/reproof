"""Object-capability checks for backend and trusted-validation authority."""
import copy
from dataclasses import replace
import json
import unittest

from reproloop.execution.backend import (
    DisabledExecutionBackend,
    ExecutionDenied,
    QualificationAuthority,
)
from tests.test_execution_protocol import DIGEST, build_request, build_route, validation_plan


REQUIRED_BUILD_PROBES = (
    "boot",
    "network-boundary",
    "filesystem-boundary",
    "process-termination",
    "cleanup",
)


def qualification_record():
    return {
        "schemaVersion": 1,
        "id": "apple-vm-current",
        "backendId": "apple-vm",
        "executionClass": "build-guest",
        "environmentDigest": "2" * 64,
        "issuedAtMs": 1100,
        "expiresAtMs": 2000,
        "probeIds": list(REQUIRED_BUILD_PROBES),
    }


class ExecutionBackendTests(unittest.TestCase):
    def setUp(self):
        self.authority = QualificationAuthority()
        self.receipts = [
            self.authority.record_probe(
                probe_id=probe_id,
                backend_id="apple-vm",
                execution_class="build-guest",
                environment_digest="2" * 64,
                outcome="pass",
                evidence_digest=str(index + 4) * 64,
                observed_at_ms=1000,
            )
            for index, probe_id in enumerate(REQUIRED_BUILD_PROBES)
        ]
        self.qualification = self.authority.issue_backend_qualification(
            qualification_record(), self.receipts, evaluated_at_ms=1200
        )
        self.plan = self.authority.register_validation_plan(validation_plan())
        self.route = self.authority.register_execution_route(build_route())

    def test_matching_local_capabilities_authorize_but_disabled_backend_stays_closed(self):
        authorization = self.authority.authorize(
            build_request(),
            qualification=self.qualification,
            validation_plan=self.plan,
            execution_route=self.route,
            evaluated_at_ms=1200,
        )
        self.assertEqual(authorization.execution_class, "build-guest")
        with self.assertRaisesRegex(ExecutionDenied, "backend implementation is unavailable"):
            DisabledExecutionBackend("apple-vm", "build-guest").execute(authorization)

    def test_revoked_qualification_invalidates_existing_and_new_authorization(self):
        authorization = self.authority.authorize(build_request(), qualification=self.qualification,
            validation_plan=self.plan, execution_route=self.route, evaluated_at_ms=1200)
        self.authority.revoke_backend("apple-vm", "build-guest", "2" * 64)
        with self.assertRaises(ExecutionDenied):
            self.authority.check_authorization(authorization, build_request(), evaluated_at_ms=1200)
        with self.assertRaises(ExecutionDenied):
            self.authority.authorize(build_request(), qualification=self.qualification,
                validation_plan=self.plan, execution_route=self.route, evaluated_at_ms=1200)

    def test_qualification_copy_cannot_extend_or_change_issued_authority(self):
        for qualification in (replace(self.qualification),replace(self.qualification,expires_at_ms=9000)):
            with self.subTest(extended=qualification.expires_at_ms>2000),self.assertRaises(ExecutionDenied):
                self.authority.authorize(build_request(),qualification=qualification,
                    validation_plan=self.plan,execution_route=self.route,evaluated_at_ms=1200)

    def test_json_or_a_different_authority_cannot_forge_qualification(self):
        forged = json.loads(json.dumps(qualification_record()))
        with self.assertRaisesRegex(ExecutionDenied, "Trusted backend qualification required"):
            self.authority.authorize(
                build_request(),
                qualification=forged,
                validation_plan=self.plan,
                execution_route=self.route,
                evaluated_at_ms=1200,
            )
        other = QualificationAuthority()
        with self.assertRaisesRegex(ExecutionDenied, "Trusted backend qualification required"):
            other.authorize(
                build_request(),
                qualification=self.qualification,
                validation_plan=other.register_validation_plan(validation_plan()),
                execution_route=other.register_execution_route(build_route()),
                evaluated_at_ms=1200,
            )

    def test_missing_independent_check_and_stale_qualification_are_denied(self):
        incomplete = build_request()
        incomplete["requiredValidationIds"] = ["artifact-check"]
        with self.assertRaisesRegex(ExecutionDenied, "Trusted validation plan mismatch"):
            self.authority.authorize(
                incomplete,
                qualification=self.qualification,
                validation_plan=self.plan,
                execution_route=self.route,
                evaluated_at_ms=1200,
            )
        with self.assertRaisesRegex(ExecutionDenied, "Backend qualification is not current"):
            self.authority.authorize(
                build_request(),
                qualification=self.qualification,
                validation_plan=self.plan,
                execution_route=self.route,
                evaluated_at_ms=2001,
            )

    def test_build_qualification_cannot_authorize_mobile_execution(self):
        mobile = copy.deepcopy(build_request())
        mobile.update(
            executionClass="mobile-device",
            inputKind="validated-artifact",
            platform="ios",
            applicationId="checkout-app",
            signingPolicyId="company-ios-signing",
        )
        signing = self.authority.register_signing_policy(
            {
                "schemaVersion": 1,
                "id": "company-ios-signing",
                "platform": "ios",
                "applicationId": "checkout-app",
                "identityReferenceId": "approved-ios-identity",
                "entitlementsDigest": DIGEST,
                "provisioningReferenceId": "approved-provisioning",
                "tool": "host-codesign-fixed",
                "candidateHooks": "forbidden",
                "artifactRelation": "pre-post-digests",
            }
        )
        with self.assertRaisesRegex(ExecutionDenied, "Backend qualification mismatch"):
            self.authority.authorize(
                mobile,
                qualification=self.qualification,
                validation_plan=self.plan,
                execution_route=self.route,
                signing_policy=signing,
                evaluated_at_ms=1200,
            )

    def test_request_cannot_expand_registered_recipe_or_cleanup_policy(self):
        for field, replacement in (
            ("recipeId", "candidate-recipe"),
            ("cleanupPolicyId", "skip-cleanup"),
        ):
            changed = build_request()
            changed[field] = replacement
            with self.assertRaisesRegex(ExecutionDenied, "Trusted execution route mismatch"):
                self.authority.authorize(
                    changed,
                    qualification=self.qualification,
                    validation_plan=self.plan,
                    execution_route=self.route,
                    evaluated_at_ms=1200,
                )
        changed_route = build_route()
        changed_route["cleanupPolicyId"] = "skip-cleanup"
        with self.assertRaisesRegex(ExecutionDenied, "Trusted execution route identity conflict"):
            self.authority.register_execution_route(changed_route)

    def test_failed_probe_cannot_be_turned_into_a_qualification(self):
        receipts = list(self.receipts)
        receipts[-1] = self.authority.record_probe(
            probe_id="cleanup",
            backend_id="apple-vm",
            execution_class="build-guest",
            environment_digest="2" * 64,
            outcome="fail",
            evidence_digest="9" * 64,
            observed_at_ms=1000,
        )
        with self.assertRaisesRegex(ExecutionDenied, "Backend qualification probes did not pass"):
            self.authority.issue_backend_qualification(
                qualification_record(), receipts, evaluated_at_ms=1200
            )

    def test_mobile_authorization_binds_the_fixed_signer_and_application(self):
        authority = QualificationAuthority()
        probe_ids = (
            "device-boundary", "network-boundary", "backend-scope",
            "process-termination", "state-cleanup",
        )
        receipts = [
            authority.record_probe(
                probe_id=probe_id,
                backend_id="iphone-provider",
                execution_class="mobile-device",
                environment_digest="4" * 64,
                outcome="pass",
                evidence_digest=str(index + 4) * 64,
                observed_at_ms=1000,
            )
            for index, probe_id in enumerate(probe_ids)
        ]
        qualification = authority.issue_backend_qualification({
            "schemaVersion": 1,
            "id": "iphone-environment-current",
            "backendId": "iphone-provider",
            "executionClass": "mobile-device",
            "environmentDigest": "4" * 64,
            "issuedAtMs": 1100,
            "expiresAtMs": 2000,
            "probeIds": list(probe_ids),
            "signingPolicyId": "company-ios-signing",
        }, receipts, evaluated_at_ms=1200)
        plan = authority.register_validation_plan(validation_plan())
        route = authority.register_execution_route({
            "schemaVersion": 1,
            "id": "protected-iphone-route",
            "projectDigest": DIGEST,
            "backendId": "iphone-provider",
            "executionClass": "mobile-device",
            "environmentDigest": "4" * 64,
            "inputKind": "validated-artifact",
            "recipeId": "scenario-replay",
            "artifactPolicyId": "bounded-artifacts",
            "validationPlanId": "repair-validation",
            "cleanupPolicyId": "sanitize-device",
            "platform": "ios",
            "applicationId": "checkout-app",
            "signingPolicyId": "company-ios-signing",
        })
        request = {
            "protocolVersion": 1,
            "operationId": "candidate-replay-one",
            "backendId": "iphone-provider",
            "executionClass": "mobile-device",
            "projectDigest": DIGEST,
            "environmentDigest": "4" * 64,
            "inputKind": "validated-artifact",
            "inputDigest": "5" * 64,
            "recipeId": "scenario-replay",
            "artifactPolicyId": "bounded-artifacts",
            "requiredValidationIds": ["artifact-check", "outcome-check"],
            "cleanupPolicyId": "sanitize-device",
            "platform": "ios",
            "applicationId": "checkout-app",
            "signingPolicyId": "company-ios-signing",
        }
        signer_wire = {
            "schemaVersion": 1,
            "id": "company-ios-signing",
            "platform": "ios",
            "applicationId": "checkout-app",
            "identityReferenceId": "approved-ios-identity",
            "entitlementsDigest": DIGEST,
            "provisioningReferenceId": "approved-provisioning",
            "tool": "host-codesign-fixed",
            "candidateHooks": "forbidden",
            "artifactRelation": "pre-post-digests",
        }
        signer = authority.register_signing_policy(signer_wire)
        authorization = authority.authorize(
            request,
            qualification=qualification,
            validation_plan=plan,
            execution_route=route,
            signing_policy=signer,
            evaluated_at_ms=1200,
        )
        self.assertEqual(authorization.execution_class, "mobile-device")

        wrong_signer = copy.deepcopy(signer_wire)
        wrong_signer["applicationId"] = "other-app"
        with self.assertRaisesRegex(ExecutionDenied, "Trusted signing policy identity conflict"):
            authority.authorize(
                request,
                qualification=qualification,
                validation_plan=plan,
                execution_route=route,
                signing_policy=authority.register_signing_policy(wrong_signer),
                evaluated_at_ms=1200,
            )


if __name__ == "__main__":
    unittest.main()
