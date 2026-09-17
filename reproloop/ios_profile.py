"""Closed general iOS runtime profiles for trusted worker configuration.

The legacy sample app and its fixture SDK retain their original contracts.
This schema describes only a selected application/runtime and contains no
filesystem paths, commands, endpoints, credentials, or executable policy.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from .core import ContractError, digest, identifier, require


PROFILE_SCHEMA_VERSION = 2
PROFILE_KIND = "reproloop-runtime-application"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BUNDLE = re.compile(
    r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+\Z")
_PACKAGE = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
_ACTIVITY = re.compile(
    r"\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_ACTIONS = {
    "ios": frozenset({"tap", "long_press", "swipe", "text", "home",
                       "launch", "terminate"}),
    "android": frozenset({"tap", "long_press", "swipe", "text", "home",
                           "pointer", "launch", "terminate"}),
}
_OBSERVATIONS = frozenset({"pixels", "accessibility", "logs"})
_LOCATORS = {"ios": "xctest-accessibility", "android": "android-resource-id"}
_CAPTURE_ADAPTERS = {"ios": frozenset({"native-frame"}),
                     "android": frozenset({"native-frame"})}
_LOG_ADAPTERS = {"ios": frozenset({"repro-app-log"}),
                 "android": frozenset({"repro-app-log"})}
_IDENTITY_REQUIREMENTS = {"ios": "install-and-launch",
                          "android": "installed-sha256"}
_ARTIFACT_KINDS = {"ios": "ios-app", "android": "android-apk"}


def _exact(value: object, fields: set[str], message: str) -> dict[str, Any]:
    require(type(value) is dict and set(value) == fields, message)
    return value


def _id(value: object, message: str) -> str:
    try:
        return identifier(value)
    except ContractError:
        raise ContractError(message) from None


def _digest(value: object, message: str) -> str:
    require(type(value) is str and _DIGEST.fullmatch(value) is not None, message)
    return value


def _version(value: object, message: str) -> str:
    require(type(value) is str and _VERSION.fullmatch(value) is not None, message)
    return value


def _adapter(value: object, *, platform: str, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    _exact(value, {"id", "version"}, f"Invalid {label} adapter")
    supported = _CAPTURE_ADAPTERS[platform] if label == "capture" else _LOG_ADAPTERS[platform]
    require(type(value["id"]) is str and value["id"] in supported and type(value["version"]) is int
            and value["version"] == 1, f"Unsupported {label} adapter")
    return value


def _validate_runtime_document(document: object, platform: str) -> dict[str, Any]:
    platform_field = "bundle" if platform == "ios" else "package"
    fields = {
        "schemaVersion", "kind", "id", "projectId", "projectDigest",
        "applicationId", "buildId", "platform", platform_field,
        "launchTarget", "artifact", "helper", "capabilities",
        "approvedReferences", "identityRequirement",
    }
    _exact(document, fields, "Unsupported runtime application profile fields")
    require(type(document["schemaVersion"]) is int
            and document["schemaVersion"] == PROFILE_SCHEMA_VERSION
            and document["kind"] == PROFILE_KIND
            and document["platform"] == platform,
            "Unsupported runtime application profile version")
    for key in ("id", "projectId", "applicationId", "buildId"):
        _id(document[key], f"Invalid runtime profile {key}")
    _digest(document["projectDigest"], "Invalid runtime profile project digest")

    application_identifier = document[platform_field]
    pattern = _BUNDLE if platform == "ios" else _PACKAGE
    require(type(application_identifier) is str
            and len(application_identifier) <= 180
            and pattern.fullmatch(application_identifier) is not None
            and application_identifier not in {
                "io.reproloop.live", "io.reproloop.driver"},
            "Invalid runtime application identifier")
    launch = _exact(document["launchTarget"], {"kind", "value"},
                    "Invalid runtime launch target")
    if platform == "ios":
        require(launch == {"kind": "bundle", "value": application_identifier},
                "iOS launch target must bind the selected bundle")
    else:
        require(launch["kind"] == "activity" and type(launch["value"]) is str
                and len(launch["value"]) <= 240
                and _ACTIVITY.fullmatch(launch["value"]) is not None,
                "Invalid Android launch activity")

    artifact_fields = ({"kind", "sha256", "bytes", "provenanceDigest",
                        "bundleVersion", "bundleBuild"} if platform == "ios"
                       else {"kind", "sha256", "bytes", "provenanceDigest",
                             "versionCode"})
    artifact = _exact(document["artifact"], artifact_fields,
                      "Invalid application artifact declaration")
    require(artifact["kind"] == _ARTIFACT_KINDS[platform]
            or platform == 'ios' and artifact['kind'] == 'ios-ipa',
            "Invalid application artifact kind")
    _digest(artifact["sha256"], "Invalid application artifact digest")
    _digest(artifact["provenanceDigest"], "Invalid build provenance digest")
    require(type(artifact["bytes"]) is int and 1 <= artifact["bytes"] <= 10 * 1024 ** 3,
            "Invalid application artifact size")
    if artifact['kind']=='ios-ipa':
        require(artifact['bytes'] <= 64*1024*1024,'iOS IPA exceeds transfer limit')
    if platform == "ios":
        _version(artifact["bundleVersion"], "Invalid iOS bundle version")
        _version(artifact["bundleBuild"], "Invalid iOS bundle build")
    else:
        require(type(artifact["versionCode"]) is int
                and 1 <= artifact["versionCode"] <= 2 ** 31 - 1,
                "Invalid Android version code")

    helper = _exact(document["helper"], {"protocolVersion", "version"},
                    "Invalid native helper requirement")
    require(type(helper["protocolVersion"]) is int
            and type(helper["version"]) is int
            and helper["protocolVersion"] == 2 and helper["version"] == 2,
            "Unsupported native helper requirement")

    capabilities = _exact(
        document["capabilities"],
        {"actions", "locator", "observations", "geometry",
         "captureAdapter", "logAdapter"},
        "Invalid runtime capability declaration")
    actions = capabilities["actions"]
    require(type(actions) is list and 1 <= len(actions) <= 16
            and all(type(item) is str for item in actions)
            and len(set(actions)) == len(actions)
            and set(actions) <= _ACTIONS[platform],
            "Invalid or unsupported runtime actions")
    observations = capabilities["observations"]
    require(type(observations) is list and len(observations) <= len(_OBSERVATIONS)
            and all(type(item) is str for item in observations)
            and len(set(observations)) == len(observations)
            and set(observations) <= _OBSERVATIONS,
            "Invalid runtime observations")
    locator = capabilities["locator"]
    if locator is not None:
        _exact(locator, {"kind", "version", "targets"},
               "Invalid locator capability")
        targets = locator["targets"]
        require(locator["kind"] == _LOCATORS[platform]
                and type(locator["version"]) is int and locator["version"] == 1
                and type(targets) is list and 1 <= len(targets) <= 128
                and all(type(item) is str for item in targets)
                and len(set(targets)) == len(targets),
                "Unsupported locator capability")
        for target in targets:
            _id(target, "Invalid locator target")
        require("accessibility" in observations,
                "Locator support requires accessibility observation")
    geometry = _exact(capabilities["geometry"],
                      {"maxWidth", "maxHeight", "orientations"},
                      "Invalid runtime geometry capability")
    width_limit = 8192 if platform == "ios" else 960
    require(type(geometry["maxWidth"]) is int
            and 1 <= geometry["maxWidth"] <= width_limit
            and type(geometry["maxHeight"]) is int
            and 1 <= geometry["maxHeight"] <= 8192
            and type(geometry["orientations"]) is list
            and 1 <= len(geometry["orientations"]) <= 2
            and all(type(item) is str for item in geometry["orientations"])
            and len(set(geometry["orientations"])) == len(geometry["orientations"])
            and set(geometry["orientations"]) <= {"portrait", "landscape"},
            "Unsupported runtime geometry capability")
    capture = _adapter(capabilities["captureAdapter"], platform=platform,
                       label="capture")
    logs = _adapter(capabilities["logAdapter"], platform=platform, label="log")
    require((capture is None) == ("pixels" not in observations),
            "Pixel observation and capture adapter must be declared together")
    require((logs is None) == ("logs" not in observations),
            "Log observation and log adapter must be declared together")

    approved = _exact(document["approvedReferences"], {"launch", "preparations"},
                      "Invalid approved runtime references")
    _id(approved["launch"], "Invalid approved launch reference")
    preparations = approved["preparations"]
    require(type(preparations) is list and len(preparations) <= 32
            and all(type(item) is str for item in preparations)
            and len(set(preparations)) == len(preparations),
            "Invalid approved preparation references")
    for reference in preparations:
        _id(reference, "Invalid approved preparation reference")
    require(document["identityRequirement"] == _IDENTITY_REQUIREMENTS[platform],
            "Unsupported installed identity requirement")

    try:
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ContractError("Invalid runtime application profile") from None
    require(len(encoded.encode("utf-8")) <= 32 * 1024,
            "Runtime application profile exceeds its size limit")
    return json.loads(encoded)


@dataclass(frozen=True, slots=True)
class IosAppProfile:
    _json: str

    @property
    def data(self) -> dict[str, Any]:
        return json.loads(self._json)

    @property
    def digest(self) -> str:
        return digest(self.data)

    @property
    def bundle(self) -> str:
        return self.data["bundle"]

    @property
    def application_identity(self) -> dict[str, Any]:
        data = self.data
        artifact = data["artifact"]
        return {
            "bundle": data["bundle"],
            "artifactDigest": artifact["sha256"],
            "applicationProfileDigest": self.digest,
            "bundleVersion": artifact["bundleVersion"],
            "bundleBuild": artifact["bundleBuild"],
            "identityProof": "selected-signed-artifact",
            "installedDigestProof": "unavailable",
        }

    def supports_identity_requirement(self, requirement: str) -> bool:
        return requirement in {"selected-artifact-sha256", "signed-products",
                               "install-and-launch"}


def validate_ios_profile(document: object) -> IosAppProfile:
    value = _validate_runtime_document(document, "ios")
    return IosAppProfile(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False))


def load_ios_profile(path) -> IosAppProfile:
    from .storage import read_json
    return validate_ios_profile(read_json(path))


# Explicit aliases keep the public name discoverable without changing the
# legacy iOS instrumentation profiles.
IosApplicationProfile = IosAppProfile
validate_ios_application_profile = validate_ios_profile


__all__ = [
    "IosAppProfile", "IosApplicationProfile", "PROFILE_KIND",
    "PROFILE_SCHEMA_VERSION", "load_ios_profile", "validate_ios_profile",
    "validate_ios_application_profile",
]
