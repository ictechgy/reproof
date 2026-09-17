"""Explicit bounded material input registers scoped secrets without execution."""
import base64
import copy
import io
import json
import unittest
from unittest.mock import patch

from reproloop.protected_service import load_protected_service_inputs
from reproloop.repair_android_signing import AndroidSigningMaterialResolver
from tests import test_protected_service_assembly as support


def encoded(value):return base64.b64encode(value).decode('ascii')


class ProtectedServiceMaterialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.ProtectedServiceAssemblyTests.setUpClass()
        cls.addClassCleanup(support.ProtectedServiceAssemblyTests.doClassCleanups)

    def setUp(self):
        self.f=support.ProtectedServiceAssemblyTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.prepared=load_protected_service_inputs(self.f.config,self.f.f.issue,self.f.f.bundle)
        self.password=b'owned-bootstrap-password'
        self.validation_key=b'owned-validation-key-material-32b'
        self.document={'schemaVersion':1,'kind':'protected-service-materials',
            'configurationDigest':self.prepared.configuration_digest,
            'signing':[{'profileId':'protected-android','keystorePath':str(self.f.root/'owned-assembly-material.p12'),
                'keyAlias':'owned','storePasswordB64':encoded(self.password)}],
            'validation':[{'profileId':'protected-android','authenticationReferenceId':'owned-observer-key',
                'providerId':'owned-observer','secretB64':encoded(self.validation_key)}]}

    def body(self,value=None):
        return bytearray(json.dumps(self.document if value is None else value).encode())

    def bind(self,body=None):
        from reproloop.protected_service_materials import bind_service_materials
        result=bind_service_materials(self.prepared,self.body() if body is None else body)
        self.addCleanup(result.close)
        return result

    def test_explicit_materials_bind_without_private_key_reads_or_native_effects(self):
        body=self.body()
        with patch('subprocess.Popen',side_effect=AssertionError('process started')), \
                patch('socket.socket',side_effect=AssertionError('network contacted')), \
                patch.object(AndroidSigningMaterialResolver,'open',side_effect=AssertionError('private key read')):
            result=self.bind(body)
        self.assertFalse(any(body))
        result.signing.require_registered(self.f.identity)
        self.assertTrue(result.signing._materials[self.f.identity.reference_id].store_password==self.password,
                        'Registered signing material changed when scratch input was cleared')
        with result.validation.material('owned-observer-key',self.f.f.fixture.registration.project_digest,'owned-observer') as secret:
            self.assertTrue(secret==self.validation_key,'Registered validation material changed')
        public=json.dumps(result.public())+repr(result)
        self.assertTrue(self.password.decode() not in public and encoded(self.password) not in public,
                        'Public material metadata exposed a password')
        self.assertNotIn(str(self.f.root),public)
        self.assertEqual(result.public()['executionAuthority'],'none')
        self.f.assert_no_owner_roots()

    def test_profile_binding_duplicates_and_extra_commands_are_rejected_before_registration(self):
        from reproloop.protected_service_materials import ProtectedServiceMaterialsError
        for change in (lambda d:d.update(configurationDigest='0'*64),
                       lambda d:d['signing'][0].update(profileId='unregistered-profile'),
                       lambda d:d['validation'][0].update(providerId='unregistered-provider'),
                       lambda d:d['signing'].append(copy.deepcopy(d['signing'][0])),
                       lambda d:d.update(command='untrusted-command')):
            document=copy.deepcopy(self.document);change(document);body=self.body(document)
            with self.subTest(change=change),patch.object(AndroidSigningMaterialResolver,'register') as register:
                with self.assertRaises(ProtectedServiceMaterialsError):self.bind(body)
                register.assert_not_called()
            self.assertFalse(any(body))

    def test_failure_after_partial_registration_closes_and_erases_created_registries(self):
        from reproloop import protected_service_materials as materials
        created=[]
        original=materials.AndroidSigningMaterialResolver
        def resolver():
            value=original();created.append(value);return value
        with patch.object(materials,'AndroidSigningMaterialResolver',side_effect=resolver), \
                patch.object(materials.ValidationSecretRegistry,'register',side_effect=RuntimeError('owned failure')):
            body=self.body()
            with self.assertRaises(materials.ProtectedServiceMaterialsError):self.bind(body)
        self.assertFalse(any(body))
        self.assertEqual(len(created),1)
        self.assertTrue(created[0]._closed)
        self.assertEqual(created[0]._materials,{})

    def test_closed_bindings_cannot_supply_materials(self):
        result=self.bind();result.close()
        with self.assertRaises(RuntimeError):result.signing.require_registered(self.f.identity)
        with self.assertRaises(RuntimeError):result.validation.require_reference(
            'owned-observer-key',self.f.f.fixture.registration.project_digest,'owned-observer')

    def test_duplicate_json_keys_and_oversized_input_are_rejected_and_buffer_cleared(self):
        from reproloop.protected_service_materials import ProtectedServiceMaterialsError, MAX_MATERIAL_INPUT_BYTES
        for body in (bytearray(b'{"schemaVersion":1,"schemaVersion":1}'),bytearray(b'x'*(MAX_MATERIAL_INPUT_BYTES+1))):
            with self.assertRaises(ProtectedServiceMaterialsError):self.bind(body)
            self.assertFalse(any(body))

    def test_bounded_stream_reader_uses_no_environment_or_secret_file_discovery(self):
        from reproloop.protected_service_materials import read_service_materials, MAX_MATERIAL_INPUT_BYTES
        class Stream(io.BytesIO):
            def read(self,size=-1):
                self.requested=size
                return super().read(size)
        stream=Stream(bytes(self.body()))
        result=read_service_materials(self.prepared,stream);self.addCleanup(result.close)
        self.assertEqual(stream.requested,MAX_MATERIAL_INPUT_BYTES+1)
        self.assertEqual(result.public()['signingProfiles'],1)

    def test_material_stream_connects_to_the_current_qualified_service_chain(self):
        from reproloop.protected_service_materials import compose_service_from_material_stream
        result=compose_service_from_material_stream(self.f.config,self.f.f.issue,self.f.f.bundle,
            owner=self.f.owner,mobile_qualifications={'protected-android':self.f.qualification},
            stream=io.BytesIO(bytes(self.body())))
        self.assertIs(result,self.f.owner)
        self.assertIs(self.f.f.bundle.protected_repairs,self.f.owner)
        self.assertIsNotNone(self.f.f.bundle.workflow.repairs)

    def test_unqualified_start_does_not_consume_the_secret_stream(self):
        from reproloop.protected_service_materials import compose_service_from_material_stream,ProtectedServiceMaterialsError
        stream=io.BytesIO(bytes(self.body()))
        with patch.object(stream,'read',side_effect=AssertionError('unqualified startup read secrets')) as read:
            with self.assertRaises(ProtectedServiceMaterialsError):
                compose_service_from_material_stream(self.f.config,self.f.f.issue,self.f.f.bundle,
                    owner=self.f.owner,mobile_qualifications={'protected-android':{'qualified':True}},stream=stream)
            read.assert_not_called()
        self.f.assert_no_owner_roots()

    def test_closed_startup_owner_does_not_consume_the_secret_stream(self):
        from reproloop.protected_service_materials import compose_service_from_material_stream,ProtectedServiceMaterialsError
        self.f.owner.close()
        stream=io.BytesIO(bytes(self.body()))
        with patch.object(stream,'read',side_effect=AssertionError('closed owner read secrets')) as read:
            with self.assertRaises(ProtectedServiceMaterialsError):
                compose_service_from_material_stream(self.f.config,self.f.f.issue,self.f.f.bundle,
                    owner=self.f.owner,mobile_qualifications={'protected-android':self.f.qualification},stream=stream)
            read.assert_not_called()

    def test_revocation_while_reading_materials_cannot_start_vm_work(self):
        from reproloop.protected_service_materials import compose_service_from_material_stream,ProtectedServiceMaterialsError
        owner=self.f.owner;route=self.f.config.document['profiles'][0]['mobile']['route']
        class RevokingStream(io.BytesIO):
            def read(stream,size=-1):
                owner.authority.revoke_backend(route['backendId'],'mobile-device',route['environmentDigest'])
                return super().read(size)
        with patch('reproloop.execution.qualification.NativeVM',side_effect=AssertionError('VM started')):
            with self.assertRaises(ProtectedServiceMaterialsError):
                compose_service_from_material_stream(self.f.config,self.f.f.issue,self.f.f.bundle,
                    owner=owner,mobile_qualifications={'protected-android':self.f.qualification},
                    stream=RevokingStream(bytes(self.body())))
        self.f.assert_no_owner_roots()

    def test_malformed_secret_and_ambiguous_private_path_are_rejected_before_registration(self):
        from reproloop.protected_service_materials import ProtectedServiceMaterialsError
        for change in (lambda d:d['signing'][0].update(storePasswordB64='invalid-base64!'),
                       lambda d:d['signing'][0].update(storePasswordB64=encoded(b'line\nbreak')),
                       lambda d:d['signing'][0].update(keystorePath=str(self.f.root)+'/../owned.p12')):
            document=copy.deepcopy(self.document);change(document);body=self.body(document)
            with patch.object(AndroidSigningMaterialResolver,'register') as register:
                with self.assertRaises(ProtectedServiceMaterialsError):self.bind(body)
                register.assert_not_called()
            self.assertFalse(any(body))
