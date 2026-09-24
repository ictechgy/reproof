"""Bounded operator definitions; file loading grants no signing authority."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof import contracts
from reproof.ios_signing_inputs import IOSSigningDefinition,IOSSigningIdentity,IOSSigningProvisioning
from reproof.repair_android_signing import AndroidSigningIdentity


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def blob(path):
    return {'path':str(path),'sha256':sha(path),'bytes':path.stat().st_size}


def ios_document(root, *, policies, profile_digest, team='OWNEDTEAM1'):
    return {'schemaVersion':1,'kind':'ios-signing-definition-v1',
        'identity':{'referenceId':'owned-key','applicationId':'ios_app','teamId':team,
                    'certificateChain':[blob(root/'chain-0.der'),blob(root/'chain-1.der')]},
        'provisioningReferenceId':'owned-profiles','bundlePolicies':policies,
        'profiles':{'.':{'cms':blob(root/'owned-profile.cms'),'profileDigest':profile_digest}},
        'provisioning':{'tools':{'opensslPath':'/usr/bin/openssl','opensslSha256':sha('/usr/bin/openssl'),
            'sandboxSha256':sha('/usr/bin/sandbox-exec')},
            'trust':{'referenceId':'owned-issuer','signer':blob(root/'chain-0.der'),
                     'anchors':[blob(root/'chain-1.der')]},
            'applicationIdentifierPrefix':team,'selectedDevice':'OWNED-DEVICE'}}


class ProtectedSigningDefinitionTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='owned-signing-definition-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        for name,body in (('chain-0.der',b'\x30\x0a\x04\x08leafxxxx'),
                          ('chain-1.der',b'\x30\x0a\x04\x08rootxxxx'),('owned-profile.cms',b'owned unverified profile')):
            path=self.root/name;path.write_bytes(body);path.chmod(0o600)
        self.policies={'.':{'bundleId':'com.example.app','entitlements':{}}}
        self.document=ios_document(self.root,policies=self.policies,profile_digest='a'*64)
        identity=IOSSigningIdentity('owned-key','ios_app','OWNEDTEAM1',
            ((self.root/'chain-0.der').read_bytes(),(self.root/'chain-1.der').read_bytes()))
        definition=IOSSigningDefinition(identity,'owned-profiles',self.policies,
            {'.':{'cms':(self.root/'owned-profile.cms').read_bytes(),'profileDigest':'a'*64}})
        self.policy={'schemaVersion':1,'id':'owned-signing','platform':'ios','applicationId':'ios_app',
            'identityReferenceId':'owned-key','entitlementsDigest':definition.entitlements_digest,
            'tool':'host-codesign-fixed','candidateHooks':'forbidden','artifactRelation':'pre-post-digests',
            'provisioningReferenceId':'owned-profiles'}
        self.application={'id':'ios_app','platform':'ios','bundle':'com.example.app'}

    def reference(self, document=None):
        path=self.root/'definition.json';path.write_text(json.dumps(self.document if document is None else document));path.chmod(0o600)
        return {'path':str(path),'sha256':sha(path)}

    def load(self, document=None, **changes):
        from reproof.protected_signing_inputs import load_signing_definition
        return load_signing_definition(self.reference(document),policy_document=changes.get('policy',self.policy),
            application=changes.get('application',self.application))

    def test_ios_definition_loads_immutable_profiles_without_opening_a_key_or_running_tools(self):
        with (patch('subprocess.Popen',side_effect=AssertionError('definition loading ran a process')),
              patch('reproof.ios_signing_inputs.IOSSigningMaterialResolver.open',side_effect=AssertionError('key opened'))):
            loaded=self.load()
        self.assertIs(type(loaded.definition),IOSSigningDefinition)
        self.assertIs(type(loaded.provisioning),IOSSigningProvisioning)
        self.assertEqual(loaded.definition.identity.application_id,'ios_app')
        (self.root/'owned-profile.cms').write_bytes(b'changed profile')
        self.assertEqual(loaded.definition.profile_bytes('.'),b'owned unverified profile')
        for text in ('OWNED-DEVICE','owned unverified profile',str(self.root),'entitlements'):
            self.assertNotIn(text,str(loaded.public()))
        self.assertEqual(loaded.public()['executionAuthority'],'none')

    def test_android_definition_matches_exact_package_identity_and_permissions_policy(self):
        identity=AndroidSigningIdentity('owned-key','android_app','com.example.android','a'*64,('v2',),())
        document={'schemaVersion':1,'kind':'android-signing-definition-v1','identity':{
            'referenceId':identity.reference_id,'applicationId':identity.application_id,'packageName':identity.package_name,
            'certificateSha256':identity.certificate_sha256,'signatureSchemes':['v2'],'permissions':[]}}
        policy=dict(self.policy,platform='android',applicationId='android_app',tool='host-apksigner-fixed',
            entitlementsDigest=identity.signing_configuration_digest)
        policy.pop('provisioningReferenceId')
        application={'id':'android_app','platform':'android','bundle':'com.example.android'}
        loaded=self.load(document,policy=policy,application=application)
        self.assertIs(type(loaded.identity),AndroidSigningIdentity);self.assertEqual(loaded.identity,identity)
        from reproof.protected_signing_inputs import ProtectedSigningInputsError
        with self.assertRaises(ProtectedSigningInputsError):
            self.load(document,policy=dict(policy,entitlementsDigest='f'*64),application=application)

    def test_top_digest_blob_size_hash_symlink_and_private_key_roles_are_rejected(self):
        from reproof.protected_signing_inputs import load_signing_definition,ProtectedSigningInputsError
        reference=self.reference();reference['sha256']='0'*64
        with self.assertRaises(ProtectedSigningInputsError):
            load_signing_definition(reference,policy_document=self.policy,application=self.application)
        for field,value in (('bytes',1),('sha256','0'*64),('path',str(self.root/'owned.p12'))):
            changed=copy.deepcopy(self.document);changed['identity']['certificateChain'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ProtectedSigningInputsError):self.load(changed)
        link=self.root/'link.der';link.symlink_to(self.root/'chain-0.der')
        changed=copy.deepcopy(self.document);changed['identity']['certificateChain'][0]['path']=str(link)
        with self.assertRaises(ProtectedSigningInputsError):self.load(changed)

    def test_application_policy_and_unknown_credential_fields_cannot_be_loaded(self):
        from reproof.protected_signing_inputs import ProtectedSigningInputsError
        for changed in (dict(self.document,password='OwnedCredentialCanary'),dict(self.document,schemaVersion=True)):
            with self.assertRaises(ProtectedSigningInputsError) as error:self.load(changed)
            self.assertNotIn('OwnedCredentialCanary',str(error.exception))
        with self.assertRaises(ProtectedSigningInputsError):self.load(application=dict(self.application,bundle='com.other.app'))
        with self.assertRaises(ProtectedSigningInputsError):self.load(policy=dict(self.policy,identityReferenceId='other-key'))

    def test_der_certificate_with_conventional_cer_suffix_is_accepted(self):
        path=self.root/'leaf.cer';path.write_bytes((self.root/'chain-0.der').read_bytes());path.chmod(0o600)
        changed=copy.deepcopy(self.document);changed['identity']['certificateChain'][0]=blob(path)
        loaded=self.load(changed)
        self.assertEqual(loaded.identity.certificate_sha256,sha(path))

    def test_declared_profile_total_is_bounded_before_certificate_or_profile_reads(self):
        from reproof.protected_signing_inputs import ProtectedSigningInputsError
        changed=copy.deepcopy(self.document)
        for index in range(17):
            changed['profiles'][f'PlugIns/Owned{index}.appex']={'cms':dict(blob(self.root/'owned-profile.cms'),bytes=4*1024**2),
                                                             'profileDigest':'a'*64}
        with patch('reproof.protected_signing_inputs._read_blob',side_effect=AssertionError('oversized set read a blob')) as read:
            with self.assertRaises(ProtectedSigningInputsError):self.load(changed)
            read.assert_not_called()

    def test_remaining_service_profile_budget_is_checked_before_blob_reads(self):
        from reproof.protected_signing_inputs import load_signing_definition,ProtectedSigningInputsError
        reference=self.reference()
        with patch('reproof.protected_signing_inputs._read_blob') as read:
            with self.assertRaises(ProtectedSigningInputsError):
                load_signing_definition(reference,policy_document=self.policy,application=self.application,profile_bytes_limit=0)
            read.assert_not_called()


class LoadedNativeIOSDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import test_ios_native_pipeline as native
        cls.native=native.IOSNativePipelineTests
        cls.native.setUpClass();cls.addClassCleanup(cls.native.doClassCleanups)

    def test_loaded_definition_drives_real_owned_signing_and_fresh_independent_inspection(self):
        from dataclasses import replace
        import secrets
        import threading
        import time
        from reproof.ios_signing_operation import IOSSigningOperationStore
        from reproof.protected_signing_inputs import load_signing_definition
        from reproof.repair_signing import SigningObservation,SignatureObservation
        case=self.native(methodName='runTest');case.setUp()
        self.addCleanup(case.tearDown)
        document=ios_document(case.root,policies=case.definition.bundle_policies,
            profile_digest=case.definition.provisioning_policies['.']['profileDigest'])
        path=case.root/'loaded-definition.json';path.write_text(json.dumps(document));path.chmod(0o600)
        loaded=load_signing_definition({'path':str(path),'sha256':sha(path)},policy_document=case.policy,
            application={'id':'ios_app','platform':'ios','bundle':case.bundle})
        self.assertEqual(loaded.definition.definition_digest,case.definition.definition_digest)
        self.assertEqual(loaded.provisioning.definition_digest,case.provisioning.definition_digest)
        operations=IOSSigningOperationStore(case.store,case.tools,loaded.definition,case.root/'operations',create=False)
        self.addCleanup(operations.close)
        with operations.admit(case.context,case.request) as operation:
            signed=operations.sign(operation,case.blobs,material_resolver=case.resolver,
                provisioning=loaded.provisioning,policy_document=case.policy,
                cancellation=threading.Event(),deadline_monotonic=time.monotonic()+60)
            self.assertIs(type(signed),SigningObservation)
            context=replace(case.context,nonce=secrets.token_hex(24),
                signed_artifact_digest=hashlib.sha256(signed.artifacts.entries[0][1]).hexdigest())
            inspected=operations.inspect(operation,context,signed.artifacts,provisioning=loaded.provisioning,
                policy_document=case.policy,cancellation=threading.Event(),deadline_monotonic=time.monotonic()+60)
            self.assertIs(type(inspected),SignatureObservation)
            self.assertTrue(inspected.valid and inspected.cleanup_confirmed)
            operation.run.finish('succeeded',stopped=True)
        self.assertEqual(case.store.status(case.identifier)['reservedBytes'],0)
