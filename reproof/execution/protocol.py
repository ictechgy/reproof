"""Strict inert metadata for the protected execution route.

These validators accept registration metadata. They never qualify an
environment, authorize execution, or interpret commands from imported data.
"""
from __future__ import annotations

import copy
import re

from reproof.contracts.versions import (
    bounded_int,
    bounded_list,
    bounded_text,
    epoch_ms,
    exact,
    require,
    unique_ids,
    validate_digest,
    validate_id,
    validate_version,
)


EXECUTION_CLASSES = ("build-guest", "host-build", "desktop-guest", "mobile-device")
ARCHITECTURES = ("arm64", "x86_64")
MAX_RESOURCE_BYTES = 512 * 1024 * 1024 * 1024
MAX_QUALIFICATION_MS = 24 * 60 * 60 * 1000

GUEST_CONTROLS = frozenset(
    {
        "network-deny",
        "immutable-input",
        "bounded-output",
        "process-termination",
        "overlay-cleanup",
    }
)
HOST_CONTROLS = frozenset(
    {
        "toolchain-pinned",
        "process-termination",
        "cleanup",
    }
)
MOBILE_CONTROLS = frozenset(
    {
        "device-exclusive",
        "registered-backend-scope",
        "candidate-termination",
        "state-cleanup",
    }
)


def _version_label(value: object) -> str:
    bounded_text(value, "tool version", 80)
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}", value) is not None,
            "Invalid tool version")
    return value


def _ids(value: object, field: str, maximum: int, *, minimum: int = 0) -> list[str]:
    values = bounded_list(value, field, maximum, minimum=minimum)
    for item in values:
        validate_id(item, field[:-1] if field.endswith("s") else field)
    require(len(values) == len(set(values)), f"Duplicate {field}")
    return values


def validate_guest_image_manifest(value):
    exact(value, ("schemaVersion", "id", "kind", "architecture", "artifactDigest", "sizeBytes"))
    validate_version(value["schemaVersion"])
    validate_id(value["id"], "guest image id")
    require(value["kind"] == "macos-vm-image", "Invalid guest image kind")
    require(value["architecture"] in ARCHITECTURES, "Invalid guest image architecture")
    validate_digest(value["artifactDigest"], "guest image digest")
    bounded_int(value["sizeBytes"], "guest image size", 1, MAX_RESOURCE_BYTES)
    return copy.deepcopy(value)


def validate_toolchain_manifest(value):
    exact(value, ("schemaVersion", "id", "kind", "architecture", "artifactDigest", "sizeBytes", "tools"))
    validate_version(value["schemaVersion"])
    validate_id(value["id"], "toolchain id")
    require(value["kind"] == "offline-toolchain", "Invalid toolchain kind")
    require(value["architecture"] in ARCHITECTURES, "Invalid toolchain architecture")
    validate_digest(value["artifactDigest"], "toolchain digest")
    bounded_int(value["sizeBytes"], "toolchain size", 1, MAX_RESOURCE_BYTES)
    tools = bounded_list(value["tools"], "toolchain tools", 64, minimum=1)
    for tool in tools:
        exact(tool, ("id", "version", "artifactDigest"))
        validate_id(tool["id"], "tool id")
        _version_label(tool["version"])
        validate_digest(tool["artifactDigest"], "tool artifact digest")
    unique_ids(tools, "tool id")
    return copy.deepcopy(value)


def _resources(value):
    exact(value, ("cpuCount", "memoryMiB", "diskBytes", "timeoutMs"))
    bounded_int(value["cpuCount"], "CPU count", 1, 64)
    bounded_int(value["memoryMiB"], "memory limit", 512, 1024 * 1024)
    bounded_int(value["diskBytes"], "disk limit", 1024 * 1024, MAX_RESOURCE_BYTES)
    bounded_int(value["timeoutMs"], "execution timeout", 1000, 24 * 60 * 60 * 1000)


def validate_environment_descriptor(value):
    common = (
        "schemaVersion", "id", "executionClass", "architecture", "network",
        "transport", "controls", "resources",
    )
    require(type(value) is dict, "Contract must be an object")
    execution_class = value.get("executionClass")
    require(execution_class in EXECUTION_CLASSES, "Invalid execution class")
    if execution_class in ("build-guest", "desktop-guest"):
        exact(value, common + ("guestImageId", "toolchainId"))
        require(value["network"] == "none", "Guest network must be denied")
        require(value["transport"] == "virtio-vsock", "Invalid guest transport")
        validate_id(value["guestImageId"], "guest image id")
        validate_id(value["toolchainId"], "toolchain id")
        required_controls = GUEST_CONTROLS
    elif execution_class == "host-build":
        # 호스트 빌드는 격리가 아니라 고정 toolchain·종료·수거만 증명한다.
        exact(value, common)
        require(value["network"] == "unrestricted", "Host build network is not isolated")
        require(value["transport"] == "host-process", "Invalid host build transport")
        required_controls = HOST_CONTROLS
    else:
        exact(value, common + (
            "platform", "deviceProfileId", "trustGroup", "backendScopeId",
            "accountPolicyId", "signingPolicyId",
        ))
        require(value["network"] in ("none", "registered-policy"),
                "Invalid mobile network policy")
        require(value["transport"] == "device-provider", "Invalid mobile transport")
        require(value["platform"] in ("android", "ios"), "Invalid mobile platform")
        for field, label in (
            ("deviceProfileId", "device profile id"),
            ("trustGroup", "trust group"),
            ("backendScopeId", "backend scope id"),
            ("accountPolicyId", "account policy id"),
            ("signingPolicyId", "signing policy id"),
        ):
            validate_id(value[field], label)
        required_controls = MOBILE_CONTROLS
    validate_version(value["schemaVersion"])
    validate_id(value["id"], "environment id")
    require(value["architecture"] in ARCHITECTURES, "Invalid environment architecture")
    controls = _ids(value["controls"], "controls", 64, minimum=1)
    require(required_controls <= set(controls), "Missing environment control")
    _resources(value["resources"])
    return copy.deepcopy(value)


def validate_signing_policy(value):
    common = (
        "schemaVersion", "id", "platform", "applicationId", "identityReferenceId",
        "entitlementsDigest", "tool", "candidateHooks", "artifactRelation",
    )
    require(type(value) is dict, "Contract must be an object")
    platform = value.get("platform")
    require(platform in ("android", "ios"), "Invalid signing platform")
    if platform == "ios":
        exact(value, common + ("provisioningReferenceId",))
    else:
        exact(value, common)
    validate_version(value["schemaVersion"])
    validate_id(value["id"], "signing policy id")
    validate_id(value["applicationId"], "application id")
    validate_id(value["identityReferenceId"], "signing identity reference id")
    validate_digest(value["entitlementsDigest"], "entitlements digest")
    if platform == "ios":
        validate_id(value["provisioningReferenceId"], "provisioning reference id")
    required_tool = "host-codesign-fixed" if platform == "ios" else "host-apksigner-fixed"
    require(value["tool"] == required_tool, "Invalid fixed signing tool")
    require(value["candidateHooks"] == "forbidden", "Candidate signing hooks are forbidden")
    require(value["artifactRelation"] == "pre-post-digests", "Signing artifact relation is required")
    return copy.deepcopy(value)


def validate_external_validation_plan(value):
    exact(value, ("schemaVersion", "id", "projectDigest", "checks", "candidateReports"))
    validate_version(value["schemaVersion"])
    validate_id(value["id"], "validation plan id")
    validate_digest(value["projectDigest"], "project digest")
    checks = bounded_list(value["checks"], "validation checks", 128, minimum=1)
    for check in checks:
        exact(check, ("id", "recipeId", "kind", "evidenceSourceId"))
        validate_id(check["id"], "validation check id")
        validate_id(check["recipeId"], "validation recipe id")
        require(check["kind"] in ("trusted-runner", "external-observation"),
                "Validation evidence must be independent")
        validate_id(check["evidenceSourceId"], "validation evidence source id")
    unique_ids(checks, "validation check id")
    require(value["candidateReports"] == "supplemental-only",
            "Candidate reports cannot satisfy validation")
    return copy.deepcopy(value)


def validate_execution_route(value):
    common = (
        "schemaVersion", "id", "projectDigest", "backendId", "executionClass",
        "environmentDigest", "inputKind", "recipeId", "artifactPolicyId",
        "validationPlanId", "cleanupPolicyId",
    )
    require(type(value) is dict, "Contract must be an object")
    execution_class = value.get("executionClass")
    require(execution_class in EXECUTION_CLASSES, "Invalid execution class")
    if execution_class == "mobile-device":
        exact(value, common + ("platform", "applicationId", "signingPolicyId"))
        require(value["platform"] in ("android", "ios"), "Invalid mobile platform")
        validate_id(value["applicationId"], "application id")
        validate_id(value["signingPolicyId"], "signing policy id")
        require(value["inputKind"] == "validated-artifact", "Mobile input must be a validated artifact")
    else:
        exact(value, common)
        expected = "sealed-source" if execution_class in ("build-guest", "host-build") else "validated-artifact"
        require(value["inputKind"] == expected, "Invalid execution input kind")
    validate_version(value["schemaVersion"])
    validate_id(value["id"], "execution route id")
    validate_id(value["backendId"], "backend id")
    validate_digest(value["projectDigest"], "project digest")
    validate_digest(value["environmentDigest"], "environment digest")
    for field, label in (
        ("recipeId", "recipe id"),
        ("artifactPolicyId", "artifact policy id"),
        ("validationPlanId", "validation plan id"),
        ("cleanupPolicyId", "cleanup policy id"),
    ):
        validate_id(value[field], label)
    return copy.deepcopy(value)


def validate_execution_request(value):
    common = (
        "protocolVersion", "operationId", "backendId", "executionClass",
        "projectDigest", "environmentDigest", "inputKind", "inputDigest",
        "recipeId", "artifactPolicyId", "requiredValidationIds", "cleanupPolicyId",
    )
    require(type(value) is dict, "Contract must be an object")
    execution_class = value.get("executionClass")
    require(execution_class in EXECUTION_CLASSES, "Invalid execution class")
    if execution_class == "mobile-device":
        exact(value, common + ("platform", "applicationId", "signingPolicyId"))
        require(value["platform"] in ("android", "ios"), "Invalid mobile platform")
        validate_id(value["applicationId"], "application id")
        validate_id(value["signingPolicyId"], "signing policy id")
        require(value["inputKind"] == "validated-artifact", "Mobile input must be a validated artifact")
    else:
        exact(value, common)
        expected = "sealed-source" if execution_class in ("build-guest", "host-build") else "validated-artifact"
        require(value["inputKind"] == expected, "Invalid execution input kind")
    validate_version(value["protocolVersion"], "protocolVersion")
    validate_id(value["operationId"], "operation id")
    validate_id(value["backendId"], "backend id")
    validate_digest(value["projectDigest"], "project digest")
    validate_digest(value["environmentDigest"], "environment digest")
    validate_digest(value["inputDigest"], "input digest")
    for field, label in (
        ("recipeId", "recipe id"),
        ("artifactPolicyId", "artifact policy id"),
        ("cleanupPolicyId", "cleanup policy id"),
    ):
        validate_id(value[field], label)
    _ids(value["requiredValidationIds"], "required validation ids", 128, minimum=1)
    return copy.deepcopy(value)


def validate_backend_qualification_record(value):
    required = (
        "schemaVersion", "id", "backendId", "executionClass", "environmentDigest",
        "issuedAtMs", "expiresAtMs", "probeIds",
    )
    require(type(value) is dict, "Contract must be an object")
    execution_class = value.get("executionClass")
    require(execution_class in EXECUTION_CLASSES, "Invalid execution class")
    optional = ("signingPolicyId",) if execution_class == "mobile-device" else ()
    exact(value, required, optional)
    validate_version(value["schemaVersion"])
    validate_id(value["id"], "qualification id")
    validate_id(value["backendId"], "backend id")
    validate_digest(value["environmentDigest"], "environment digest")
    issued = epoch_ms(value["issuedAtMs"], "qualification issue time")
    expires = epoch_ms(value["expiresAtMs"], "qualification expiry time")
    require(issued < expires <= issued + MAX_QUALIFICATION_MS, "Invalid qualification lifetime")
    _ids(value["probeIds"], "probe ids", 64, minimum=1)
    if execution_class == "mobile-device":
        require("signingPolicyId" in value, "Mobile qualification requires signing policy")
        validate_id(value["signingPolicyId"], "signing policy id")
    return copy.deepcopy(value)
