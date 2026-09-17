"""Focused iOS protected-service ownership and inert IPA admission tests."""
import io
import plistlib
import zipfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
from unittest.mock import patch

from reproloop import contracts
from reproloop.execution.backend import REQUIRED_PROBES
from reproloop.execution.journal import RunStore
from reproloop.ios_signing_inputs import IOSSigningIdentity, IOSSigningMaterialResolver
from reproloop.protected_service import _structural_ipa
from reproloop.protected_service import ProtectedServiceAssemblyError, compose_ios_protected_service
from reproloop.protected_signing_inputs import IOSSigningDefinitionInputs
from reproloop.protected_validation import ValidationSecretRegistry
from reproloop.repair_composition import ProtectedRepairComposition
from reproloop.repair_execution import RepairExecutionError
from reproloop.repair_ios import IOSTrustedMobileAdapter
from tests.ios_service_support import IOSServiceFixture


class IOSServiceCompositionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = IOSServiceFixture()
        self.addCleanup(self.fixture.close)

    def test_unsigned_ipa_checker_is_bounded_and_structural(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Payload/App.app/Info.plist", plistlib.dumps({
                "CFBundlePackageType": "APPL", "CFBundleExecutable": "App"}))
            archive.writestr("Payload/App.app/App", b"Mach-O placeholder")
        body = stream.getvalue()
        self.assertTrue(_structural_ipa(body, len(body)))
        self.assertFalse(_structural_ipa(body[:16], len(body)))

    def test_exact_ios_adapter_is_owned_and_closed_with_composition(self):
        owner = ProtectedRepairComposition()
        adapter = IOSTrustedMobileAdapter(self.fixture.config, operations=self.fixture.operations)
        owner.adopt_ios_mobile(adapter)
        self.assertEqual(owner.status()["cleanupPending"], 1)
        owner.close(timeout_seconds=20)
        self.assertTrue(owner.status()["closed"])

    def test_foreign_adapter_cannot_enter_ios_lifetime(self):
        owner = ProtectedRepairComposition()
        self.addCleanup(owner.close)
        with self.assertRaises(RepairExecutionError):
            owner.adopt_ios_mobile(object())

    def _materials(self, owner):
        identity = IOSSigningIdentity("factory-ios-key", "ios_app", "TEAM123",
                                      (b"\x30\x02\x05\x00",))
        resolver = IOSSigningMaterialResolver()
        root = Path(self.fixture.root) / "factory-material.p12"
        root.write_bytes(b"owned synthetic p12")
        root.chmod(0o600)
        resolver.register(identity, pkcs12=root, password=b"owned-password")
        self.addCleanup(resolver.close)
        definition = SimpleNamespace(identity=identity, definition_digest="a" * 64)
        signing = IOSSigningDefinitionInputs(definition, SimpleNamespace(definition_digest="b" * 64), "c" * 64)
        secrets = ValidationSecretRegistry()
        self.addCleanup(secrets.close)
        return resolver, secrets, signing

    def _prepared(self, signing):
        validator = SimpleNamespace(
            definition_digest="f" * 64,
            require_secrets=lambda *_: None,
        )
        build_profile = SimpleNamespace(
            profile_id="ios-profile", signing=signing,
            tools=SimpleNamespace(
                build_bundle=SimpleNamespace(recipe=lambda _name: {"maxOutputBytes": 1024}),
                signing_tools=object()),
        )
        mobile_profile = SimpleNamespace(profile_id="ios-profile", config=self.fixture.config)
        prepared = SimpleNamespace(
            configuration_digest="e" * 64,
            build_signing=SimpleNamespace(configuration_digest="e" * 64,
                _profiles=(build_profile,), verify=lambda *_: None,
                profile=lambda identifier: build_profile),
            mobile=SimpleNamespace(configuration_digest="e" * 64,
                _profiles=(mobile_profile,), verify=lambda *_: None,
                profile=lambda identifier: mobile_profile),
            validation=(("ios-profile", validator),),
        )
        return prepared

    def _configuration(self, *, platform="ios"):
        policy = {"schemaVersion": 1, "id": "ios-policy", "platform": "ios",
                  "applicationId": "ios_app", "identityReferenceId": "factory-ios-key",
                  "entitlementsDigest": "1" * 64, "provisioningReferenceId": "factory-profile",
                  "tool": "fixed-ios-signing", "candidateHooks": "forbidden",
                  "artifactRelation": "pre-post-digests"}
        route = {"schemaVersion": 1, "id": "ios-factory-route", "projectDigest": self.fixture.registration.project_digest,
                 "backendId": "ios-factory", "executionClass": "mobile-device",
                 "environmentDigest": "2" * 64, "inputKind": "validated-artifact",
                 "recipeId": "ios-replay", "artifactPolicyId": "signed-ios-ipa",
                 "validationPlanId": "factory-plan", "cleanupPolicyId": "ios-cleanup",
                 "platform": platform, "applicationId": "ios_app", "signingPolicyId": "ios-policy"}
        row = {"id": "ios-profile", "platform": platform, "projectDigest": self.fixture.registration.project_digest,
               "applicationId": "ios_app", "runtimePolicyDigest": self.fixture.config.runtime_policy_digest,
               "signing": {"policy": policy}, "mobile": {"route": route},
               "validation": {"plan": {"schemaVersion": 1, "id": "factory-plan",
                   "projectDigest": self.fixture.registration.project_digest,
                   "candidateReports": "supplemental-only", "checks": [{"id": "check", "recipeId": "ios-replay",
                       "kind": "external-observation", "evidenceSourceId": "factory-observer"}]},
                   "observers": {}},
               "build": {"route": {"recipeId": "ios-replay", "artifactPolicyId": "unsigned-ios",
                                      "environmentDigest": "2" * 64},
                         "journal": {"root": str(self.fixture.root / "factory-build-journal"),
                                     "environmentDigest": "2" * 64, "diskBudgetBytes": 4096}},
               "signing": {"policy": policy, "journal": {"root": str(self.fixture.root / "factory-sign-journal"),
                                     "environmentDigest": "2" * 64, "diskBudgetBytes": 4096},
                           "ownerRoot": str(self.fixture.root / "factory-sign-owner")},
               "mobile": {"route": route, "journal": {"root": str(self.fixture.root / "factory-mobile-journal"),
                                     "environmentDigest": "2" * 64, "diskBudgetBytes": 4096},
                           "ownerRoot": str(self.fixture.root / "factory-mobile-owner")},
               "runtimePolicyDigest": self.fixture.config.runtime_policy_digest}
        return SimpleNamespace(definition_digest="e" * 64, document={"profiles": [row]},
                               validate_issue_configuration=lambda *_: None,
                               validate_runtime=lambda *_: None)

    def _qualification(self, owner, route):
        now = int(time.time() * 1000)
        probes = sorted(REQUIRED_PROBES["mobile-device"])
        receipts = [owner.authority.record_probe(
            probe_id=item, backend_id=route["backendId"], execution_class="mobile-device",
            environment_digest=route["environmentDigest"], outcome="pass",
            evidence_digest=contracts.digest("ios factory qualification double"), observed_at_ms=now)
                    for item in probes]
        return owner.authority.issue_backend_qualification({
            "schemaVersion": 1, "id": "ios-factory-qualification",
            "backendId": route["backendId"], "executionClass": "mobile-device",
            "environmentDigest": route["environmentDigest"], "signingPolicyId": route["signingPolicyId"],
            "issuedAtMs": now, "expiresAtMs": now + 600000, "probeIds": probes},
            receipts, evaluated_at_ms=now)

    def test_factory_rejects_stale_qualification_before_build_or_journal(self):
        owner = ProtectedRepairComposition()
        self.addCleanup(owner.close)
        resolver, secrets, signing = self._materials(owner)
        prepared = self._prepared(signing)
        configuration = self._configuration()
        with patch("reproloop.protected_service.load_protected_service_inputs", return_value=prepared), \
             patch.object(owner, "qualify_build", side_effect=AssertionError("build started")), \
             patch.object(owner.authority, "require_qualification", side_effect=RuntimeError("stale")):
            with self.assertRaises(ProtectedServiceAssemblyError):
                compose_ios_protected_service(configuration, {}, SimpleNamespace(), owner=owner,
                    signing_materials=resolver, validation_secrets=secrets,
                    mobile_qualifications={"ios-profile": object()})
        self.assertEqual(owner.status()["cleanupPending"], 0)

    def test_factory_dispatches_ios_chain_and_attaches_after_recheck(self):
        owner = ProtectedRepairComposition()
        self.addCleanup(owner.close)
        resolver, secrets, signing = self._materials(owner)
        prepared = self._prepared(signing)
        configuration = self._configuration()
        route = configuration.document["profiles"][0]["mobile"]["route"]
        qualification = self._qualification(owner, route)
        events = []
        builder = SimpleNamespace(application_id="ios_app")
        signer = SimpleNamespace(policy=SimpleNamespace(platform="ios"), builder=builder)
        supervisor = object()
        executor = SimpleNamespace(ready=lambda **_: events.append("ready"))
        runtime = SimpleNamespace(protected_repairs=None, workflow=SimpleNamespace(repairs=None))
        with patch("reproloop.protected_service.load_protected_service_inputs", return_value=prepared), \
             patch.object(owner, "qualify_build", side_effect=lambda **_: events.append("build") or builder), \
             patch.object(owner, "configure_ios_signing_owner", side_effect=lambda *a, **k: events.append("sign") or signer), \
             patch.object(owner, "configure_ios_mobile", side_effect=lambda **k: events.append("mobile") or supervisor), \
             patch.object(owner.authority, "require_qualification", side_effect=lambda *a, **k: events.append("qualification")), \
             patch("reproloop.protected_service._journal", return_value=object()), \
             patch("reproloop.protected_service.load_ios_validation_inputs",
                   return_value=prepared.validation[0][1]), \
             patch("reproloop.protected_service.ProtectedRepairExecutor", return_value=executor), \
             patch.object(owner, "register", side_effect=lambda *a: events.append("register")), \
             patch("reproloop.live.issue_configuration.compose_issue_repairs",
                   side_effect=lambda *a, **k: (setattr(runtime, "protected_repairs", owner),
                                                 setattr(runtime.workflow, "repairs", object()))):
            try:
                result = compose_ios_protected_service(configuration, {}, runtime, owner=owner,
                    signing_materials=resolver, validation_secrets=secrets,
                    mobile_qualifications={"ios-profile": qualification})
            except Exception:
                raise
        self.assertIs(result, owner)
        self.assertEqual([event for event in events if event != "qualification"],
                         ["build", "sign", "mobile", "ready", "register"])
        self.assertGreaterEqual(events.count("qualification"), 1)
        self.assertIs(runtime.protected_repairs, owner)
        self.assertIsNotNone(runtime.workflow.repairs)


if __name__ == "__main__":
    unittest.main()
