"""Pure constraints for an already-decoded iOS provisioning profile.

The assessor intentionally does not parse CMS, validate a certificate chain,
open a Keychain, invoke ``codesign``, or issue a signing/qualification
capability.  A later fixed native adapter must perform those operations and
bind its result to this static assessment.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from .core import ContractError, digest


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TEAM = re.compile(r"[A-Z0-9]{1,64}\Z")
_BUNDLE = re.compile(
    r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+\Z")
_DEVICE = re.compile(r"[A-Za-z0-9._:-]{1,256}\Z")
_ENTITLEMENT_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

_PROFILE_FIELDS = (
    "CreationDate", "ExpirationDate", "ApplicationIdentifierPrefix",
    "TeamIdentifier", "ProvisionedDevices", "DeveloperCertificates",
    "Entitlements",
)
_MAX_PROFILE_FIELDS = 64
_REQUIRED_ENTITLEMENTS = frozenset({
    "application-identifier",
    "com.apple.developer.team-identifier",
    "get-task-allow",
    "keychain-access-groups",
})
_ERROR_CODES = frozenset({
    "profile_invalid", "unsupported_entitlement", "unsupported_pattern",
    "policy_invalid",
})
# Apple system keychain-access groups granted by standard team provisioning
# profiles. They are not team-prefixed, so the team-scope pattern check cannot
# reason about them; they are enumerated here instead of widening the pattern.
_APPLE_SYSTEM_KEYCHAIN_GROUPS = frozenset({"com.apple.token"})


class ProvisioningPolicyError(ValueError):
    """A decoded profile or static policy cannot satisfy this contract."""

    def __init__(self, code: str):
        if code not in _ERROR_CODES:
            code = "profile_invalid"
        self.code = code
        # Only a fixed code is exposed; profile/device/team values never enter
        # exception text or tracebacks produced by this module.
        super().__init__(code)


def _fail(code: str):
    raise ProvisioningPolicyError(code)


def _digest(value: object, code: str = "policy_invalid") -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(code)
    return value


def _text(value: object, *, pattern: re.Pattern[str] | None = None,
          code: str = "profile_invalid", maximum: int = 1024) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        _fail(code)
    try:
        if len(value.encode("utf-8")) > maximum:
            _fail(code)
    except UnicodeError:
        _fail(code)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _fail(code)
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail(code)
    return value


def _date(value: object) -> datetime:
    if type(value) is not datetime:
        _fail("profile_invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        _fail("profile_invalid")


def _date_wire(value: object) -> str:
    return _date(value).isoformat(timespec="microseconds")


def _terminal_pattern(value: object, *, scope: str):
    """Validate and return an exact or terminal-star pattern.

    The only wildcard accepted is one final ``*``.  The scope must be an
    exact team-prefixed namespace, so a bare ``*`` can never authorize data.
    """
    value = _text(value, maximum=512)
    _check_terminal_syntax(value)
    if "*" in value:
        prefix = value[:-1]
    else:
        prefix = value
    if not prefix.startswith(scope + "."):
        _fail("unsupported_pattern")
    return value


def _check_terminal_syntax(value: str):
    if any(character in value for character in "?[]"):
        _fail("unsupported_pattern")
    if "*" in value:
        if value.count("*") != 1 or not value.endswith("*"):
            _fail("unsupported_pattern")


def _terminal_matches(pattern: str, expected: str) -> bool:
    if "*" not in pattern:
        return pattern == expected
    # The complete prefix, including a separator immediately before the
    # terminal wildcard, must be present in the concrete value.  In
    # particular, ``TEAM.bundle.*`` cannot authorize ``TEAM.bundle``.
    return expected.startswith(pattern[:-1])


def _entitlement_value(value: object):
    if type(value) in (str, bool, int):
        if type(value) is str:
            _text(value, maximum=2048)
        elif type(value) is int and not -(2 ** 53) <= value <= 2 ** 53:
            _fail("unsupported_entitlement")
        return value
    if type(value) is list:
        if not 0 <= len(value) <= 256:
            _fail("unsupported_entitlement")
        for item in value:
            _text(item, code="unsupported_entitlement", maximum=2048)
        return list(value)
    _fail("unsupported_entitlement")


def _entitlements(value: object) -> dict[str, Any]:
    if type(value) is not dict or len(value) > 128:
        _fail("unsupported_entitlement")
    result: dict[str, Any] = {}
    for key, item in value.items():
        _text(key, pattern=_ENTITLEMENT_KEY, code="unsupported_entitlement",
              maximum=128)
        checked = _entitlement_value(item)
        wildcard_allowed = key in {"application-identifier", "keychain-access-groups"}
        if type(checked) is str and "*" in checked:
            # Syntax is checked here; namespace checks for the two permitted
            # wildcard fields happen after the expected team is known.
            if not wildcard_allowed:
                _fail("unsupported_pattern")
            _check_terminal_syntax(checked)
        elif type(checked) is list:
            for element in checked:
                if "*" in element:
                    if not wildcard_allowed:
                        _fail("unsupported_pattern")
                    _check_terminal_syntax(element)
        result[key] = checked
    return result


def _certificate_hashes(value: object) -> list[str]:
    if type(value) is not list or not 0 < len(value) <= 32:
        _fail("profile_invalid")
    result = []
    total = 0
    for certificate in value:
        if type(certificate) is not bytes or not 0 < len(certificate) <= 4 * 1024 * 1024:
            _fail("profile_invalid")
        total += len(certificate)
        if total > 8 * 1024 * 1024:
            _fail("profile_invalid")
        result.append(hashlib.sha256(certificate).hexdigest())
    if len(result) != len(set(result)):
        _fail("profile_invalid")
    return result


def _profile_projection(profile: object) -> tuple[dict[str, Any], list[str]]:
    if type(profile) is not dict:
        _fail("profile_invalid")
    if len(profile) > _MAX_PROFILE_FIELDS:
        _fail("profile_invalid")
    if any(field not in profile for field in _PROFILE_FIELDS):
        _fail("profile_invalid")
    creation = _date_wire(profile["CreationDate"])
    expiration = _date_wire(profile["ExpirationDate"])
    prefixes = profile["ApplicationIdentifierPrefix"]
    teams = profile["TeamIdentifier"]
    if type(prefixes) is not list or len(prefixes) != 1:
        _fail("profile_invalid")
    if type(teams) is not list or len(teams) != 1:
        _fail("profile_invalid")
    prefixes = [_text(prefixes[0], pattern=_TEAM)]
    teams = [_text(teams[0], pattern=_TEAM)]
    devices = profile["ProvisionedDevices"]
    if type(devices) is not list or not 0 < len(devices) <= 1024:
        _fail("profile_invalid")
    devices = [_text(item, pattern=_DEVICE, maximum=256) for item in devices]
    if len(devices) != len(set(devices)):
        _fail("profile_invalid")
    certificates = _certificate_hashes(profile["DeveloperCertificates"])
    entitlements = _entitlements(profile["Entitlements"])
    projection = {
        "CreationDate": creation,
        "ExpirationDate": expiration,
        "ApplicationIdentifierPrefix": prefixes,
        "TeamIdentifier": teams,
        "ProvisionedDevices": devices,
        "DeveloperCertificatesSha256": certificates,
        "Entitlements": entitlements,
    }
    return projection, certificates


def decoded_profile_digest(profile: object) -> str:
    """Digest the bounded decoded fields without retaining certificate bytes."""
    projection, _ = _profile_projection(profile)
    return digest(projection)


@dataclass(frozen=True, slots=True)
class ProvisioningPolicyAssessment:
    """Safe static evidence; this object is not a signing capability."""

    valid: bool
    profile_digest: str
    certificate_match: bool
    entitlements_digest: str
    reason_code: str | None

    def public(self):
        return {
            "valid": self.valid,
            "profileDigest": self.profile_digest,
            "certificateMatch": self.certificate_match,
            "entitlementsDigest": self.entitlements_digest,
            "reasonCode": self.reason_code,
        }


def _assessment(profile_digest: str, certificate_match: bool,
                entitlements_digest: str, reason: str | None):
    return ProvisioningPolicyAssessment(
        reason is None, profile_digest, certificate_match,
        entitlements_digest, reason)


def _policy_identity(bundle_id, team_id, prefix, selected_device, evaluated_at):
    _text(bundle_id, pattern=_BUNDLE, maximum=180)
    _text(team_id, pattern=_TEAM, maximum=64)
    _text(prefix, pattern=_TEAM, maximum=64)
    _text(selected_device, pattern=_DEVICE, maximum=256)
    if type(evaluated_at) is not datetime:
        _fail("policy_invalid")
    return _date(evaluated_at)


def _validate_expected_entitlements(value: object, *, team_id: str,
                                    application_identifier_prefix: str,
                                    bundle_id: str):
    entitlements = _entitlements(value)
    if not _REQUIRED_ENTITLEMENTS <= set(entitlements):
        _fail("policy_invalid")
    expected_application = application_identifier_prefix + "." + bundle_id
    if entitlements["application-identifier"] != expected_application:
        _fail("policy_invalid")
    if entitlements["com.apple.developer.team-identifier"] != team_id:
        _fail("policy_invalid")
    if type(entitlements["get-task-allow"]) is not bool:
        _fail("policy_invalid")
    groups = entitlements["keychain-access-groups"]
    if type(groups) is not list or not groups:
        _fail("policy_invalid")
    for group in groups:
        if "*" in group or not group.startswith(application_identifier_prefix + "."):
            _fail("policy_invalid")
    return entitlements


def _profile_entitlements_allowed(profile_entitlements, expected,
                                  *, team_id: str,
                                  application_identifier_prefix: str,
                                  bundle_id: str):
    if not _REQUIRED_ENTITLEMENTS <= set(profile_entitlements):
        return "entitlement_not_allowed"
    expected_application = application_identifier_prefix + "." + bundle_id
    profile_application = profile_entitlements["application-identifier"]
    _terminal_pattern(profile_application,
                      scope=application_identifier_prefix)
    if not _terminal_matches(profile_application, expected_application):
        return "entitlement_not_allowed"
    profile_team = profile_entitlements["com.apple.developer.team-identifier"]
    if type(profile_team) is not str or profile_team != team_id:
        return "entitlement_not_allowed"
    if type(profile_entitlements["get-task-allow"]) is not bool:
        return "entitlement_not_allowed"
    if profile_entitlements["get-task-allow"] != expected["get-task-allow"]:
        return "entitlement_not_allowed"

    profile_groups = profile_entitlements["keychain-access-groups"]
    expected_groups = expected["keychain-access-groups"]
    if type(profile_groups) is not list or not profile_groups:
        return "entitlement_not_allowed"
    for group in profile_groups:
        if group in _APPLE_SYSTEM_KEYCHAIN_GROUPS:
            continue
        _terminal_pattern(group, scope=application_identifier_prefix)
    for group in expected_groups:
        if not any(_terminal_matches(pattern, group) for pattern in profile_groups):
            return "entitlement_not_allowed"

    for key, expected_value in expected.items():
        if key in _REQUIRED_ENTITLEMENTS:
            continue
        profile_value = profile_entitlements.get(key)
        if profile_value is None:
            return "entitlement_not_allowed"
        if type(expected_value) is list:
            if type(profile_value) is not list or not set(expected_value) <= set(profile_value):
                return "entitlement_not_allowed"
        elif (type(profile_value) is not type(expected_value)
              or profile_value != expected_value):
            return "entitlement_not_allowed"
    return None


def assess_decoded_profile(profile: object, *, expected_profile_digest: str,
                           expected_certificate_sha256: str, bundle_id: str,
                           team_id: str, application_identifier_prefix: str,
                           selected_device: str, evaluated_at: datetime,
                           expected_entitlements: dict[str, Any],
                           expected_entitlements_digest: str):
    """Return a bounded static assessment for one injected decoded profile."""
    evaluated = _policy_identity(bundle_id, team_id,
                                 application_identifier_prefix,
                                 selected_device, evaluated_at)
    expected_profile_digest = _digest(expected_profile_digest)
    expected_certificate_sha256 = _digest(expected_certificate_sha256)
    expected_entitlements_digest = _digest(expected_entitlements_digest)
    expected = _validate_expected_entitlements(
        expected_entitlements, team_id=team_id,
        application_identifier_prefix=application_identifier_prefix,
        bundle_id=bundle_id)
    calculated_entitlements_digest = digest(expected)
    projection, certificate_hashes = _profile_projection(profile)
    actual_profile_digest = digest(projection)
    certificate_match = expected_certificate_sha256 in certificate_hashes

    if projection["ApplicationIdentifierPrefix"][0] != application_identifier_prefix:
        return _assessment(actual_profile_digest, certificate_match,
                           calculated_entitlements_digest, "prefix_mismatch")
    if projection["TeamIdentifier"][0] != team_id:
        return _assessment(actual_profile_digest, certificate_match,
                           calculated_entitlements_digest, "team_mismatch")
    if selected_device not in projection["ProvisionedDevices"]:
        return _assessment(actual_profile_digest, certificate_match,
                           calculated_entitlements_digest, "device_not_provisioned")

    creation = _date(profile["CreationDate"])
    expiration = _date(profile["ExpirationDate"])
    if expiration <= evaluated:
        return _assessment(actual_profile_digest, certificate_match,
                           calculated_entitlements_digest, "profile_expired")
    if creation > evaluated:
        return _assessment(actual_profile_digest, certificate_match,
                           calculated_entitlements_digest, "profile_not_yet_valid")

    entitlement_reason = _profile_entitlements_allowed(
        projection["Entitlements"], expected,
        team_id=team_id,
        application_identifier_prefix=application_identifier_prefix,
        bundle_id=bundle_id)
    if entitlement_reason is not None:
        return _assessment(actual_profile_digest, certificate_match,
                           calculated_entitlements_digest, entitlement_reason)
    if not certificate_match:
        return _assessment(actual_profile_digest, False,
                           calculated_entitlements_digest, "certificate_mismatch")
    if calculated_entitlements_digest != expected_entitlements_digest:
        return _assessment(actual_profile_digest, True,
                           calculated_entitlements_digest, "entitlement_digest_mismatch")
    if actual_profile_digest != expected_profile_digest:
        return _assessment(actual_profile_digest, True,
                           calculated_entitlements_digest, "profile_digest_mismatch")
    return _assessment(actual_profile_digest, True,
                       calculated_entitlements_digest, None)


__all__ = [
    "ProvisioningPolicyAssessment",
    "ProvisioningPolicyError",
    "assess_decoded_profile",
    "decoded_profile_digest",
]
