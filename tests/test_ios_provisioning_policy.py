from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from reproof.core import digest


TEAM = "TEAM123456"
BUNDLE = "com.example.inventory"
DEVICE = "owned-device-01"
CERTIFICATE = b"owned dummy developer certificate"
EVALUATED = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def expected_entitlements(*, team=TEAM, prefix=TEAM, bundle=BUNDLE):
    return {
        "application-identifier": prefix + "." + bundle,
        "com.apple.developer.team-identifier": team,
        "get-task-allow": True,
        "keychain-access-groups": [prefix + "." + bundle],
    }


def decoded_profile():
    return {
        "CreationDate": EVALUATED - timedelta(days=1),
        "ExpirationDate": EVALUATED + timedelta(days=30),
        "ApplicationIdentifierPrefix": [TEAM],
        "TeamIdentifier": [TEAM],
        "ProvisionedDevices": [DEVICE],
        "DeveloperCertificates": [CERTIFICATE],
        "Entitlements": {
            "application-identifier": TEAM + ".com.example.*",
            "com.apple.developer.team-identifier": TEAM,
            "get-task-allow": True,
            "keychain-access-groups": [TEAM + ".com.example.*"],
        },
        # Decoded profiles contain unrelated metadata. It must not become a
        # public assessment field or an implicit entitlement policy.
        "Name": "private dummy profile name",
    }


class IOSProvisioningPolicyTests(unittest.TestCase):
    def setUp(self):
        from reproof.ios_provisioning_policy import decoded_profile_digest

        self.profile = decoded_profile()
        self.profile_digest = decoded_profile_digest(self.profile)
        self.entitlements = expected_entitlements()
        self.entitlements_digest = digest(self.entitlements)
        self.certificate_digest = hashlib.sha256(CERTIFICATE).hexdigest()

    def assess(self, profile=None, **overrides):
        from reproof.ios_provisioning_policy import assess_decoded_profile

        args = dict(
            expected_profile_digest=self.profile_digest,
            expected_certificate_sha256=self.certificate_digest,
            bundle_id=BUNDLE,
            team_id=TEAM,
            application_identifier_prefix=TEAM,
            selected_device=DEVICE,
            evaluated_at=EVALUATED,
            expected_entitlements=self.entitlements,
            expected_entitlements_digest=self.entitlements_digest,
        )
        args.update(overrides)
        return assess_decoded_profile(self.profile if profile is None else profile, **args)

    def test_valid_profile_returns_static_safe_assessment(self):
        from reproof.ios_provisioning_policy import ProvisioningPolicyAssessment

        assessment = self.assess()
        self.assertIs(type(assessment), ProvisioningPolicyAssessment)
        self.assertTrue(assessment.valid)
        self.assertTrue(assessment.certificate_match)
        self.assertEqual(assessment.profile_digest, self.profile_digest)
        self.assertEqual(assessment.entitlements_digest, self.entitlements_digest)
        self.assertIsNone(assessment.reason_code)
        self.assertEqual(set(assessment.public()), {
            "valid", "profileDigest", "certificateMatch", "entitlementsDigest", "reasonCode"
        })

    def test_profile_digest_and_certificate_are_fixed_inputs(self):
        assessment = self.assess(expected_profile_digest="0" * 64)
        self.assertFalse(assessment.valid)
        self.assertEqual(assessment.reason_code, "profile_digest_mismatch")

        assessment = self.assess(expected_certificate_sha256="0" * 64)
        self.assertFalse(assessment.valid)
        self.assertFalse(assessment.certificate_match)
        self.assertEqual(assessment.reason_code, "certificate_mismatch")

    def test_time_team_prefix_and_device_constraints_fail_closed(self):
        from reproof.ios_provisioning_policy import ProvisioningPolicyError

        cases = [
            ("expired", {"ExpirationDate": EVALUATED}, "profile_expired"),
            ("not_yet_created", {"CreationDate": EVALUATED + timedelta(seconds=1)},
             "profile_not_yet_valid"),
            ("wrong_device", {}, "device_not_provisioned"),
            ("wrong_team", {}, "team_mismatch"),
            ("wrong_prefix", {}, "prefix_mismatch"),
        ]
        for name, changes, reason in cases:
            with self.subTest(case=name):
                profile = deepcopy(self.profile)
                if name == "wrong_device":
                    profile["ProvisionedDevices"] = ["other-device"]
                elif name == "wrong_team":
                    profile["TeamIdentifier"] = ["OTHER12345"]
                elif name == "wrong_prefix":
                    profile["ApplicationIdentifierPrefix"] = ["OTHER12345"]
                else:
                    profile.update(changes)
                try:
                    assessment = self.assess(profile)
                except ProvisioningPolicyError as error:
                    self.fail(f"policy mismatch should return safe assessment: {error}")
                self.assertFalse(assessment.valid)
                self.assertEqual(assessment.reason_code, reason)

    def test_apple_system_keychain_group_grants_are_enumerated_not_scoped(self):
        from reproof.ios_provisioning_policy import (ProvisioningPolicyError,
            decoded_profile_digest)

        profile = deepcopy(self.profile)
        profile["Entitlements"]["keychain-access-groups"] = [
            TEAM + ".com.example.*", "com.apple.token"]
        assessment = self.assess(
            profile, expected_profile_digest=decoded_profile_digest(profile))
        self.assertTrue(assessment.valid)

        profile = deepcopy(self.profile)
        profile["Entitlements"]["keychain-access-groups"] = [
            TEAM + ".com.example.*", "com.apple.unlisted"]
        with self.assertRaises(ProvisioningPolicyError) as raised:
            self.assess(profile)
        self.assertEqual(raised.exception.code, "unsupported_pattern")

    def test_application_identifier_and_keychain_wildcards_are_terminal_and_scoped(self):
        profile = deepcopy(self.profile)
        profile["Entitlements"]["application-identifier"] = TEAM + ".*." + BUNDLE
        with self.assertRaisesRegex(Exception, "unsupported_pattern"):
            self.assess(profile)

        profile = deepcopy(self.profile)
        profile["Entitlements"]["keychain-access-groups"] = ["*"]
        with self.assertRaisesRegex(Exception, "unsupported_pattern"):
            self.assess(profile)

        profile = deepcopy(self.profile)
        profile["Entitlements"]["keychain-access-groups"] = ["OTHER12345.*"]
        with self.assertRaisesRegex(Exception, "unsupported_pattern"):
            self.assess(profile)

        profile = deepcopy(self.profile)
        profile["Entitlements"]["unapproved-string"] = "value*"
        with self.assertRaisesRegex(Exception, "unsupported_pattern"):
            self.assess(profile)

    def test_entitlements_are_allowlisted_and_bound_to_the_expected_digest(self):
        profile = deepcopy(self.profile)
        profile["Entitlements"]["get-task-allow"] = False
        assessment = self.assess(profile)
        self.assertFalse(assessment.valid)
        self.assertEqual(assessment.reason_code, "entitlement_not_allowed")

        profile = deepcopy(self.profile)
        profile["Entitlements"]["application-identifier"] = TEAM + ".com.example.other.*"
        assessment = self.assess(profile)
        self.assertFalse(assessment.valid)
        self.assertEqual(assessment.reason_code, "entitlement_not_allowed")

        assessment = self.assess(expected_entitlements_digest="0" * 64)
        self.assertFalse(assessment.valid)
        self.assertEqual(assessment.reason_code, "entitlement_digest_mismatch")

    def test_terminal_wildcard_does_not_authorize_its_bare_prefix(self):
        profile = deepcopy(self.profile)
        profile["Entitlements"]["application-identifier"] = (
            TEAM + "." + BUNDLE + ".*")
        assessment = self.assess(profile)
        self.assertFalse(assessment.valid)
        self.assertEqual(assessment.reason_code, "entitlement_not_allowed")

    def test_application_prefix_is_independent_from_team_identifier(self):
        from reproof.ios_provisioning_policy import decoded_profile_digest

        prefix = "PREFIX123"
        profile = deepcopy(self.profile)
        profile["ApplicationIdentifierPrefix"] = [prefix]
        profile["Entitlements"]["application-identifier"] = prefix + ".com.example.*"
        profile["Entitlements"]["keychain-access-groups"] = [prefix + ".com.example.*"]
        expected = expected_entitlements(prefix=prefix)
        assessment = self.assess(
            profile,
            expected_profile_digest=decoded_profile_digest(profile),
            application_identifier_prefix=prefix,
            expected_entitlements=expected,
            expected_entitlements_digest=digest(expected),
        )
        self.assertTrue(assessment.valid)

    def test_scalar_entitlements_require_exact_python_types(self):
        profile = deepcopy(self.profile)
        profile["Entitlements"]["com.example.mode"] = True
        expected = expected_entitlements()
        expected["com.example.mode"] = 1
        assessment = self.assess(
            profile,
            expected_entitlements=expected,
            expected_entitlements_digest=digest(expected),
        )
        self.assertFalse(assessment.valid)
        self.assertEqual(assessment.reason_code, "entitlement_not_allowed")

    def test_unsupported_nested_entitlement_values_are_rejected(self):
        from reproof.ios_provisioning_policy import ProvisioningPolicyError

        profile = deepcopy(self.profile)
        profile["Entitlements"]["nested"] = {"private": "value"}
        with self.assertRaisesRegex(ProvisioningPolicyError, "unsupported_entitlement"):
            self.assess(profile)

        profile = deepcopy(self.profile)
        profile["Entitlements"]["nested"] = ["ok", ["nested"]]
        with self.assertRaisesRegex(ProvisioningPolicyError, "unsupported_entitlement"):
            self.assess(profile)

    def test_single_team_prefix_and_certificate_shape_are_required(self):
        from reproof.ios_provisioning_policy import ProvisioningPolicyError

        for field in ("TeamIdentifier", "ApplicationIdentifierPrefix"):
            profile = deepcopy(self.profile)
            profile[field] = [TEAM, TEAM]
            with self.subTest(field=field), self.assertRaisesRegex(
                    ProvisioningPolicyError, "profile_invalid"):
                self.assess(profile)

        profile = deepcopy(self.profile)
        profile["DeveloperCertificates"] = [CERTIFICATE, CERTIFICATE]
        with self.assertRaisesRegex(ProvisioningPolicyError, "profile_invalid"):
            self.assess(profile)

    def test_top_level_profile_fields_are_bounded_before_projection(self):
        from reproof.ios_provisioning_policy import ProvisioningPolicyError

        profile = deepcopy(self.profile)
        profile.update({f"extra-{index}": index for index in range(58)})
        with self.assertRaisesRegex(ProvisioningPolicyError, "profile_invalid"):
            self.assess(profile)

    def test_raw_profile_identity_values_never_appear_in_public_results_or_errors(self):
        from reproof.ios_provisioning_policy import ProvisioningPolicyError

        assessment = self.assess()
        encoded = json.dumps(assessment.public(), sort_keys=True)
        for value in (TEAM, BUNDLE, DEVICE, "private dummy profile name"):
            self.assertNotIn(value, encoded)

        profile = deepcopy(self.profile)
        profile["Entitlements"]["application-identifier"] = "bad.*.pattern"
        with self.assertRaises(ProvisioningPolicyError) as raised:
            self.assess(profile)
        message = str(raised.exception)
        for value in (TEAM, BUNDLE, DEVICE, "private dummy profile name"):
            self.assertNotIn(value, message)

    def test_assessment_is_not_a_capability_or_signing_claim(self):
        assessment = self.assess()
        self.assertFalse(hasattr(assessment, "_issuer"))
        self.assertFalse(hasattr(assessment, "sign"))
        self.assertFalse(hasattr(assessment, "verify_signature"))


if __name__ == "__main__":
    unittest.main()
