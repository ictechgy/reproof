import hashlib
import os
from pathlib import Path
import tempfile
import unittest


class IOSSigningInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='owned-ios-signing-inputs-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def identity(self, **changes):
        from reproloop.ios_signing_inputs import IOSSigningIdentity
        # Structurally bounded DER for input-contract tests; crypto acceptance
        # remains the responsibility of the fixed native signer/inspector.
        return IOSSigningIdentity(**{**dict(reference_id='owned-key', application_id='ios_app',
            team_id='OWNEDTEAM1', certificate_chain=(b'\x30\x03\x02\x01\x01',)), **changes})

    def test_definition_freezes_policy_profiles_and_orders_nested_code_before_root(self):
        from reproloop.ios_signing_inputs import IOSSigningDefinition
        policy = {'.': {'bundleId': 'com.example.app', 'entitlements': {'get-task-allow': False}},
            'Frameworks/Example.framework': {'bundleId': 'com.example.library', 'entitlements': {}},
            'PlugIns/Example.xctest': {'bundleId': 'com.example.tests', 'entitlements': {}}}
        profiles = {'.': {'cms': b'owned profile fixture', 'profileDigest': 'a'*64}}
        definition = IOSSigningDefinition(self.identity(), 'owned-profiles', policy, profiles)
        fingerprint = definition.definition_digest
        policy['.']['entitlements']['get-task-allow'] = True
        profiles['.']['cms'] = b'changed'
        self.assertEqual(definition.definition_digest, fingerprint)
        self.assertEqual(definition.code_objects[-1]['bundlePath'], '.')
        self.assertEqual(definition.code_objects[0]['bundlePath'], 'PlugIns/Example.xctest')
        self.assertEqual(definition.profile_bytes('.'), b'owned profile fixture')
        self.assertFalse(definition.bundle_policies['.']['entitlements']['get-task-allow'])
        self.assertNotIn('owned profile fixture', repr(definition))

    def test_canonical_certificate_scope_is_shared_across_reference_names(self):
        first = self.identity(); second = self.identity(reference_id='another-reference')
        self.assertEqual(first.scope_digest, second.scope_digest)
        self.assertNotEqual(first.definition_digest, second.definition_digest)

    def test_missing_profile_unknown_bundle_and_wrong_signing_policy_are_rejected(self):
        from reproloop.ios_signing_inputs import IOSSigningDefinition, IOSSigningInputError
        policy = {'.': {'bundleId': 'com.example.app', 'entitlements': {}}}
        with self.assertRaises(IOSSigningInputError):
            IOSSigningDefinition(self.identity(), 'profiles', policy, {})
        with self.assertRaises(IOSSigningInputError):
            IOSSigningDefinition(self.identity(), 'profiles', policy,
                {'Foreign.appex': {'cms': b'owned', 'profileDigest': 'a'*64}})
        definition = IOSSigningDefinition(self.identity(), 'profiles', policy,
            {'.': {'cms': b'owned', 'profileDigest': 'a'*64}})
        document = {'schemaVersion': 1, 'id': 'signing-policy', 'platform': 'ios', 'applicationId': 'ios_app',
            'identityReferenceId': 'owned-key', 'entitlementsDigest': definition.entitlements_digest,
            'tool': 'host-codesign-fixed', 'candidateHooks': 'forbidden', 'artifactRelation': 'pre-post-digests',
            'provisioningReferenceId': 'profiles'}
        self.assertEqual(definition.validate_policy(document), document)
        for field, value in (('identityReferenceId', 'other'), ('provisioningReferenceId', 'other'),
                             ('entitlementsDigest', 'f'*64), ('applicationId', 'other')):
            with self.assertRaises(IOSSigningInputError):
                definition.validate_policy({**document, field: value})

    def test_material_registry_rejects_aliases_and_changed_files_and_revokes_new_opens(self):
        from reproloop.ios_signing_inputs import IOSSigningMaterialResolver, IOSSigningInputError
        identity = self.identity()
        path = self.root/'owned.p12'; path.write_bytes(b'owned bounded ciphertext fixture'); path.chmod(0o600)
        resolver = IOSSigningMaterialResolver(); self.addCleanup(resolver.close)
        resolver.register(identity, pkcs12=path, password=b'owned fixture password')
        with resolver.open(identity) as material:
            self.assertEqual(os.read(material.descriptor, 64), path.read_bytes())
            self.assertNotIn('owned fixture password', repr(material))
            retained_password = material.password
        self.assertEqual(bytes(retained_password), b'\0'*len(retained_password))
        with self.assertRaises(IOSSigningInputError):
            resolver.open(self.identity(reference_id='other-key'))
        path.write_bytes(b'changed ciphertext')
        with self.assertRaises(IOSSigningInputError):
            resolver.open(identity)
        resolver.close()
        with self.assertRaises(IOSSigningInputError):
            resolver.open(identity)
        alias = self.root/'alias.p12'; alias.symlink_to(path)
        other = IOSSigningMaterialResolver(); self.addCleanup(other.close)
        with self.assertRaises(IOSSigningInputError):
            other.register(identity, pkcs12=alias, password=b'owned')

    def test_identity_certificate_and_request_size_bounds_are_enforced(self):
        from reproloop.ios_signing_inputs import IOSSigningDefinition, IOSSigningInputError
        with self.assertRaises(IOSSigningInputError): self.identity(certificate_chain=(b'not DER',))
        with self.assertRaises(IOSSigningInputError): self.identity(certificate_chain=(b'\x30\x00',)*9)
        with self.assertRaises(IOSSigningInputError): self.identity(team_id='wrong team')
        with self.assertRaises(IOSSigningInputError):
            IOSSigningDefinition(self.identity(), 'profiles', {'.': {'bundleId': 'com.example.app',
                'entitlements': {'group': 'x'*3000}}}, {'.': {'cms': b'owned', 'profileDigest': 'a'*64}})
