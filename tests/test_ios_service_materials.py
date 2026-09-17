"""Explicit iOS protected-service material binding and dispatch tests."""
import base64
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from reproloop.execution.wire import canonical
from reproloop.ios_signing_inputs import IOSSigningMaterialResolver, IOSSigningIdentity
from reproloop.protected_signing_inputs import IOSSigningDefinitionInputs
from reproloop.protected_service import PreparedProtectedServiceInputs
from reproloop.protected_service_materials import (
    ProtectedServiceMaterials, ProtectedServiceMaterialsError, bind_service_materials,
    compose_service_from_material_stream,
)
from reproloop.protected_validation import ValidationSecretRegistry
from reproloop.repair_composition import ProtectedRepairComposition


class IOSServiceMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ios-service-materials-")
        self.root = Path(self.temp.name).resolve()
        self.material = self.root / "owned-signing.p12"
        self.material.write_bytes(b"synthetic-pkcs12-placeholder")
        self.material.chmod(0o600)
        self.identity = IOSSigningIdentity(
            "ios-signing-key", "ios_app", "TEAM123",
            (b"\x30\x02\x05\x00",))
        definition = SimpleNamespace(identity=self.identity,
                                     definition_digest="a" * 64)
        provisioning = SimpleNamespace(definition_digest="b" * 64)
        signing = IOSSigningDefinitionInputs(definition, provisioning, "c" * 64)
        build_profile = SimpleNamespace(profile_id="ios-profile", signing=signing)
        mobile_profile = SimpleNamespace(
            profile_id="ios-profile",
            config=SimpleNamespace(registration=SimpleNamespace(project_digest="d" * 64)))
        observers = SimpleNamespace(authentication_references=lambda: (("auth-ref", "observer"),),
                                    definition_digest="f" * 64)
        self.prepared = PreparedProtectedServiceInputs(
            "e" * 64,
            SimpleNamespace(configuration_digest="e" * 64, _profiles=(build_profile,),
                             public=lambda: {"kind": "synthetic-build"}),
            SimpleNamespace(configuration_digest="e" * 64, _profiles=(mobile_profile,),
                             public=lambda: {"kind": "synthetic-mobile"}),
            (("ios-profile", observers),),
        )

    def tearDown(self):
        self.temp.cleanup()

    def document(self, *, signing=None, profiles=None):
        return {
            "schemaVersion": 1,
            "kind": "protected-service-materials",
            "configurationDigest": self.prepared.configuration_digest,
            "signing": signing if signing is not None else [{
                "profileId": "ios-profile", "pkcs12Path": str(self.material),
                "passwordB64": base64.b64encode(b"owned-password").decode(),
            }],
            "validation": [{
                "profileId": "ios-profile", "authenticationReferenceId": "auth-ref",
                "providerId": "observer", "secretB64": base64.b64encode(b"s" * 32).decode(),
            }],
        }

    def bind(self, document=None):
        buffer = bytearray(canonical(self.document() if document is None else document))
        result = bind_service_materials(self.prepared, buffer)
        self.assertEqual(buffer, bytearray(len(buffer)))
        return result

    def test_ios_binding_uses_ios_resolver_and_clears_stream_buffer(self):
        materials = self.bind()
        self.addCleanup(materials.close)
        self.assertIs(type(materials.signing), IOSSigningMaterialResolver)
        self.assertEqual(materials.signing_profiles, 1)
        self.assertEqual(materials.validation_references, 1)
        opened = materials.signing.open(self.identity)
        opened.close()
        self.assertEqual(materials.public()["executionAuthority"], "none")

    def test_android_material_row_is_rejected_for_ios_definition_before_registration(self):
        document = self.document(signing=[{
            "profileId": "ios-profile", "keystorePath": str(self.material),
            "keyAlias": "owned", "storePasswordB64": base64.b64encode(b"password").decode(),
        }])
        with patch.object(IOSSigningMaterialResolver, "register",
                          side_effect=AssertionError("registered invalid cross-platform row")):
            with self.assertRaises(ProtectedServiceMaterialsError):
                self.bind(document)

    def test_invalid_ios_password_clears_input_without_registering_material(self):
        document = self.document(signing=[{
            "profileId": "ios-profile", "pkcs12Path": str(self.material),
            "passwordB64": base64.b64encode(b"line\nbreak").decode(),
        }])
        buffer = bytearray(canonical(document))
        with patch.object(IOSSigningMaterialResolver, "register",
                          side_effect=AssertionError("registered invalid password")):
            with self.assertRaises(ProtectedServiceMaterialsError):
                bind_service_materials(self.prepared, buffer)
        self.assertEqual(buffer, bytearray(len(buffer)))

    def test_material_stream_composition_dispatches_to_ios_and_rejects_mixed_platforms(self):
        owner = ProtectedRepairComposition()
        self.addCleanup(owner.close)
        resolver = IOSSigningMaterialResolver()
        validation = ValidationSecretRegistry()
        bindings = ProtectedServiceMaterials(self.prepared.configuration_digest,
            resolver, validation, 1, 1)
        self.addCleanup(bindings.close)
        configuration = SimpleNamespace(
            definition_digest=self.prepared.configuration_digest,
            document={"profiles": [{"id": "ios-profile", "platform": "ios",
                                    "mobile": {"route": {"backendId": "ios", "environmentDigest": "1" * 64}},
                                    "signing": {"policy": {"id": "ios-policy"}}}]})
        with patch("reproloop.protected_service.load_protected_service_inputs",
                   return_value=self.prepared), \
             patch("reproloop.protected_service_materials.read_service_materials",
                   return_value=bindings), \
             patch("reproloop.protected_service.compose_ios_protected_service",
                   return_value="ios-composed") as compose, \
             patch.object(owner.authority, "require_qualification"):
            result = compose_service_from_material_stream(
                configuration, {}, SimpleNamespace(), owner=owner,
                mobile_qualifications={"ios-profile": object()}, stream=object())
        self.assertEqual(result, "ios-composed")
        compose.assert_called_once()

        mixed = SimpleNamespace(
            definition_digest=self.prepared.configuration_digest,
            document={"profiles": [
                {"id": "ios-profile", "platform": "ios", "mobile": {"route": {"backendId": "ios"}},
                 "signing": {"policy": {"id": "ios-policy"}}},
                {"id": "android-profile", "platform": "android", "mobile": {"route": {"backendId": "android"}},
                 "signing": {"policy": {"id": "android-policy"}}},
            ]})
        with patch("reproloop.protected_service.load_protected_service_inputs",
                   return_value=self.prepared):
            with self.assertRaises(ProtectedServiceMaterialsError):
                compose_service_from_material_stream(
                    mixed, {}, SimpleNamespace(), owner=ProtectedRepairComposition(),
                    mobile_qualifications={"ios-profile": object(), "android-profile": object()},
                    stream=object())


if __name__ == "__main__":
    unittest.main()
