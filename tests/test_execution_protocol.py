"""Strict wire contracts for the protected execution route."""
import copy
import unittest

from reproof.core import ContractError
from reproof.execution.protocol import (
    validate_environment_descriptor,
    validate_execution_route,
    validate_execution_request,
    validate_external_validation_plan,
    validate_guest_image_manifest,
    validate_signing_policy,
    validate_toolchain_manifest,
)


DIGEST = "1" * 64


def build_environment():
    return {
        "schemaVersion": 1,
        "id": "build-environment",
        "executionClass": "build-guest",
        "architecture": "arm64",
        "network": "none",
        "transport": "virtio-vsock",
        "guestImageId": "macos-image",
        "toolchainId": "offline-tools",
        "controls": [
            "network-deny",
            "immutable-input",
            "bounded-output",
            "process-termination",
            "overlay-cleanup",
        ],
        "resources": {
            "cpuCount": 2,
            "memoryMiB": 4096,
            "diskBytes": 20_000_000_000,
            "timeoutMs": 900_000,
        },
    }


def validation_plan():
    return {
        "schemaVersion": 1,
        "id": "repair-validation",
        "projectDigest": DIGEST,
        "checks": [
            {
                "id": "artifact-check",
                "recipeId": "artifact-validator",
                "kind": "trusted-runner",
                "evidenceSourceId": "host-artifact-validator",
            },
            {
                "id": "outcome-check",
                "recipeId": "scenario-oracle",
                "kind": "external-observation",
                "evidenceSourceId": "device-observer",
            },
        ],
        "candidateReports": "supplemental-only",
    }


def build_request():
    return {
        "protocolVersion": 1,
        "operationId": "repair-build-one",
        "backendId": "apple-vm",
        "executionClass": "build-guest",
        "projectDigest": DIGEST,
        "environmentDigest": "2" * 64,
        "inputKind": "sealed-source",
        "inputDigest": "3" * 64,
        "recipeId": "build-recipe",
        "artifactPolicyId": "bounded-artifacts",
        "requiredValidationIds": ["artifact-check", "outcome-check"],
        "cleanupPolicyId": "discard-overlay",
    }


def build_route():
    return {
        "schemaVersion": 1,
        "id": "protected-build-route",
        "projectDigest": DIGEST,
        "backendId": "apple-vm",
        "executionClass": "build-guest",
        "environmentDigest": "2" * 64,
        "inputKind": "sealed-source",
        "recipeId": "build-recipe",
        "artifactPolicyId": "bounded-artifacts",
        "validationPlanId": "repair-validation",
        "cleanupPolicyId": "discard-overlay",
    }


class ExecutionProtocolTests(unittest.TestCase):
    def test_registered_metadata_describes_resources_without_executable_fields(self):
        image = {
            "schemaVersion": 1,
            "id": "macos-image",
            "kind": "macos-vm-image",
            "architecture": "arm64",
            "artifactDigest": DIGEST,
            "sizeBytes": 1024,
        }
        tools = {
            "schemaVersion": 1,
            "id": "offline-tools",
            "kind": "offline-toolchain",
            "architecture": "arm64",
            "artifactDigest": "2" * 64,
            "sizeBytes": 2048,
            "tools": [
                {"id": "swift", "version": "6.2", "artifactDigest": "3" * 64}
            ],
        }
        self.assertEqual(validate_guest_image_manifest(image), image)
        self.assertEqual(validate_toolchain_manifest(tools), tools)
        self.assertEqual(validate_environment_descriptor(build_environment()), build_environment())

        for injected in (
            {**image, "path": "/private/guest.img"},
            {**tools, "downloadUrl": "https://example.invalid/tools"},
            {**build_environment(), "command": ["sh", "build.sh"]},
        ):
            with self.assertRaises(ContractError):
                if injected.get("kind") == "macos-vm-image":
                    validate_guest_image_manifest(injected)
                elif injected.get("kind") == "offline-toolchain":
                    validate_toolchain_manifest(injected)
                else:
                    validate_environment_descriptor(injected)

    def test_execution_class_controls_input_and_environment_shape(self):
        validate_execution_request(build_request())
        validate_execution_route(build_route())
        invalid = copy.deepcopy(build_request())
        invalid.update(
            executionClass="mobile-device",
            inputKind="validated-artifact",
            platform="ios",
            applicationId="checkout-app",
            signingPolicyId="company-ios-signing",
        )
        validate_execution_request(invalid)

        wrong_environment = build_environment()
        wrong_environment["deviceProfileId"] = "iphone-profile"
        with self.assertRaises(ContractError):
            validate_environment_descriptor(wrong_environment)

        missing_signing = copy.deepcopy(invalid)
        missing_signing.pop("signingPolicyId")
        with self.assertRaises(ContractError):
            validate_execution_request(missing_signing)

        changed_route = build_route()
        changed_route["command"] = "build-anything"
        with self.assertRaises(ContractError):
            validate_execution_route(changed_route)

    def test_validation_is_independent_and_candidate_reports_stay_supplemental(self):
        self.assertEqual(validate_external_validation_plan(validation_plan()), validation_plan())
        candidate_only = validation_plan()
        candidate_only["checks"][0]["kind"] = "candidate-report"
        with self.assertRaises(ContractError):
            validate_external_validation_plan(candidate_only)
        promoted = validation_plan()
        promoted["candidateReports"] = "authoritative"
        with self.assertRaises(ContractError):
            validate_external_validation_plan(promoted)

    def test_signing_policy_is_a_fixed_host_stage_not_a_candidate_command(self):
        policy = {
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
        self.assertEqual(validate_signing_policy(policy), policy)
        injected = copy.deepcopy(policy)
        injected["command"] = "codesign --identity anything"
        with self.assertRaises(ContractError):
            validate_signing_policy(injected)
        missing_profile = copy.deepcopy(policy)
        missing_profile.pop("provisioningReferenceId")
        with self.assertRaises(ContractError):
            validate_signing_policy(missing_profile)


if __name__ == "__main__":
    unittest.main()
