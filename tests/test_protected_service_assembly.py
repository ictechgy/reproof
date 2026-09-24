"""Fixed factories assemble live chains; VM/mobile qualification are explicit doubles."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reproof import contracts
from reproof.execution.backend import QualificationAuthority, REQUIRED_PROBES
from reproof.execution.resources import provision
from reproof.execution.wire import canonical, MAX_TRANSFER_BYTES
from reproof.protected_build_signing_inputs import PreparedBuildSigningInputs, ProtectedBuildSigningProfile
from reproof.protected_signing_inputs import AndroidSigningDefinitionInputs
from reproof.protected_tool_inputs import ProtectedToolProfile
from reproof.protected_validation import ValidationSecretRegistry
from reproof.repair_android_signing import AndroidSigningIdentity, AndroidSigningMaterialResolver
from reproof.repair_composition import ProtectedRepairComposition
from reproof.repair_configuration import ProtectedServiceConfiguration
from reproof.repair_signing_recovery import SigningOwnerTools, MIN_OPERATION_BYTES
from tests import test_protected_mobile_inputs as support
from tests import test_android_signing_owner as native
from tests import test_repair_android as android_fixture
from tests import test_android_process_guardian as guardian_support
from tests.test_adb_endpoint import OwnedAdbServer, ADB
from tests.test_execution_qualification import ProbeVMDouble
from tests.test_execution_resources import resource_inputs


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ProtectedServiceAssemblyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        native.AndroidSigningOwnerTests.setUpClass()
        cls.addClassCleanup(native.AndroidSigningOwnerTests.tearDownClass)
        owner=native.AndroidSigningOwnerTests
        cls.tools=SigningOwnerTools(owner.java,sha(owner.java),owner.owner_jar,sha(owner.owner_jar),
            owner.native/'libreproof_signing_owner_fd.dylib',sha(owner.native/'libreproof_signing_owner_fd.dylib'),
            native.APKSIGNER_JAR,sha(native.APKSIGNER_JAR))
        guardian_support.AndroidProcessGuardianTests.setUpClass()
        cls.addClassCleanup(guardian_support.AndroidProcessGuardianTests.doClassCleanups)
        cls.guardian=guardian_support.AndroidProcessGuardianTests.guardian

    def setUp(self):
        self.f=support.ProtectedMobileInputsTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups)
        original_project=android_fixture.project_document
        def project_with_build():
            value=original_project()
            value['recipes'].append({'id':'build_app','kind':'build','productFile':'build/recipe.json','operation':'build'})
            value['executionClasses'].append('build-guest')
            return value
        with patch.object(android_fixture,'project_document',side_effect=project_with_build):self.f.setUp()
        self.root=self.f.root;row=self.f.document['profiles'][0]
        serial='assembly-'+hashlib.sha256(str(self.root).encode()).hexdigest()[:24]
        self.f.fixture.lab.devices['device']['_authority']['physicalId']=serial
        self.f.fixture.config=replace(self.f.fixture.config,serial=serial)
        self.f.definition['serial']=serial
        temporary=tempfile.TemporaryDirectory(prefix='assembly-adb-',dir='/private/tmp');self.addCleanup(temporary.cleanup)
        self.adb_server=OwnedAdbServer(Path(temporary.name).resolve(),serial=self.f.fixture.config.serial)
        self.addCleanup(self.adb_server.close)
        self.f.definition['tools'].update(adbPath=str(ADB),adbSha256=sha(ADB))
        self.f.definition.update(adbEndpoint={'socketPath':str(self.adb_server.path),'serverVersion':41,
            'sandboxSha256':sha('/usr/bin/sandbox-exec')},nativeGuardian={'path':str(self.guardian),'sha256':sha(self.guardian)})
        project=self.f.fixture.registration.project
        build_id=next(item['id'] for item in project['recipes'] if item['kind']=='build')
        inputs=self.root/'vm-resource-source';inputs.mkdir()
        metadata,paths=resource_inputs(inputs)
        recipe=metadata['catalog'][0]
        recipe.update(id=build_id,outputPaths=['candidate.apk'],maxOutputBytes=MAX_TRANSFER_BYTES)
        self.guest=provision(Path(row['build']['bundlePath']),metadata=metadata,resources=paths)
        row['build']['route'].update(environmentDigest=self.guest.environment_digest,recipeId=build_id,
            artifactPolicyId=recipe['artifactPolicyId'],cleanupPolicyId=recipe['cleanupPolicyId'])
        row['build']['journal']['environmentDigest']=self.guest.environment_digest
        row['signing']['journal']['diskBudgetBytes']=2*MIN_OPERATION_BYTES
        row['mobile']['journal']['diskBudgetBytes']=128*1024*1024
        self.identity=AndroidSigningIdentity('owned-assembly-key',row['applicationId'],self.f.fixture.config.package,
            contracts.digest(str(self.root)),('v2','v3'),())
        row['signing']['policy'].update(identityReferenceId=self.identity.reference_id,
            entitlementsDigest=self.identity.signing_configuration_digest)
        self.f.issue['projects'][0]['repair'].update(buildRecipeId=build_id,
            sourcePaths=sorted(set(project['editablePaths'])|{item['productFile'] for item in project['recipes']+project['fixtures']}),
            originalArtifactPaths=['candidate.apk'])
        self.f.issue['repairStorageBytes']=128*1024*1024
        self.config=self.f.selected()
        row=self.config.document['profiles'][0]
        self.build_inputs=PreparedBuildSigningInputs(self.config.definition_digest,(
            ProtectedBuildSigningProfile(ProtectedToolProfile(row['id'],canonical(row).decode(),self.guest,self.tools),
                AndroidSigningDefinitionInputs(self.identity,'a'*64)),))
        observer=Path(row['validation']['observers']['path'])
        observer.write_text(json.dumps({'schemaVersion':1,'kind':'unix-validation-observers-v1','observers':[{
            'sourceId':row['validation']['plan']['checks'][0]['evidenceSourceId'],'providerId':'owned-observer',
            'socketPath':str(self.root/'observer.sock'),'authenticationReferenceId':'owned-observer-key'}]}))
        observer.chmod(0o600)
        self.f.document['profiles'][0]['validation']['observers']['sha256']=sha(observer)
        self.config=ProtectedServiceConfiguration(self.f.document)
        row=self.config.document['profiles'][0]
        self.build_inputs=PreparedBuildSigningInputs(self.config.definition_digest,(
            ProtectedBuildSigningProfile(ProtectedToolProfile(row['id'],canonical(row).decode(),self.guest,self.tools),
                AndroidSigningDefinitionInputs(self.identity,'a'*64)),))
        # Keep the input-loader double current while all native factories remain real.
        self.enterContext(patch('reproof.protected_service.load_protected_build_signing_inputs',return_value=self.build_inputs))
        self.enterContext(patch('reproof.protected_build_signing_inputs.load_protected_build_signing_inputs',return_value=self.build_inputs))
        self.owner=ProtectedRepairComposition();self.addCleanup(self.owner.close)
        self.resolver=AndroidSigningMaterialResolver();self.addCleanup(self.resolver.close)
        key=self.root/'owned-assembly-material.p12';key.write_bytes(b'owned material placeholder');key.chmod(0o600)
        self.resolver.register(self.identity,keystore=key,key_alias='owned',store_password=b'owned-password')
        self.secrets=ValidationSecretRegistry();self.addCleanup(self.secrets.close)
        self.secrets.register('owned-observer-key',project_digest=row['projectDigest'],provider_id='owned-observer',secret=secrets.token_bytes(32))
        self.qualification=self.qualify(self.owner.authority)
        self.enterContext(patch('reproof.execution.qualification.NativeVM',ProbeVMDouble))
        for name in ('stop_confirmed','network_denied','bounded_output_denied'):
            self.enterContext(patch.object(ProbeVMDouble,name,True))

    def qualify(self,authority):
        row=self.config.document['profiles'][0];route=row['mobile']['route'];now=int(time.time()*1000)
        ids=sorted(REQUIRED_PROBES['mobile-device'])
        receipts=[authority.record_probe(probe_id=name,backend_id=route['backendId'],execution_class='mobile-device',
            environment_digest=route['environmentDigest'],outcome='pass',evidence_digest=contracts.digest('explicit mobile qualification double'),observed_at_ms=now)
            for name in ids]
        return authority.issue_backend_qualification({'schemaVersion':1,'id':'owned-mobile-qualification',
            'backendId':route['backendId'],'executionClass':'mobile-device','environmentDigest':route['environmentDigest'],
            'signingPolicyId':row['signing']['policy']['id'],'issuedAtMs':now,'expiresAtMs':now+600000,'probeIds':ids},
            receipts,evaluated_at_ms=now)

    def assemble(self,qualification=None,resolver=None):
        from reproof.protected_service import compose_android_protected_service
        return compose_android_protected_service(self.config,self.f.issue,self.f.bundle,owner=self.owner,
            signing_materials=self.resolver if resolver is None else resolver,validation_secrets=self.secrets,
            mobile_qualifications={'protected-android':self.qualification if qualification is None else qualification})

    def assert_no_owner_roots(self):
        row=self.config.document['profiles'][0]
        for name in ('build','signing','mobile'):
            self.assertFalse(Path(row[name]['journal']['root']).exists())
        for name in ('signing','mobile'):self.assertFalse(Path(row[name]['ownerRoot']).exists())

    def test_factories_attach_one_live_chain_to_the_exact_issue_runtime(self):
        from reproof import repair_composition,repair_mobile,protected_validation
        from reproof.execution import backend
        errors=[]
        def tracked(original):
            def require(value,*args,**kwargs):
                if not value:errors.append(args[0] if args else 'unspecified')
                return original(value,*args,**kwargs)
            return require
        with patch.object(repair_composition,'_require',side_effect=tracked(repair_composition._require)), \
                patch.object(repair_mobile,'_require',side_effect=tracked(repair_mobile._require)), \
                patch.object(protected_validation,'_require',side_effect=tracked(protected_validation._require)), \
                patch.object(backend,'_deny',side_effect=tracked(backend._deny)):
            try:result=self.assemble()
            except RuntimeError as error:self.fail({'stage':str(error),'contracts':errors})
        self.assertIs(result,self.owner)
        self.assertIs(self.f.bundle.protected_repairs,self.owner)
        self.assertIsNotNone(self.f.bundle.workflow.repairs)
        self.assertEqual(self.owner.status()['profiles'],['protected-android'])

    def test_json_copied_foreign_and_revoked_qualification_are_rejected_before_owners(self):
        from reproof.protected_service import ProtectedServiceAssemblyError
        for value in ({'qualified':True},replace(self.qualification),self.qualify(QualificationAuthority())):
            with self.subTest(kind=type(value).__name__),self.assertRaises(ProtectedServiceAssemblyError):self.assemble(value)
            self.assert_no_owner_roots()
        route=self.config.document['profiles'][0]['mobile']['route']
        self.owner.authority.revoke_backend(route['backendId'],'mobile-device',route['environmentDigest'])
        with self.assertRaises(ProtectedServiceAssemblyError):self.assemble()
        self.assert_no_owner_roots()

    def test_unregistered_material_is_rejected_without_opening_a_key_or_starting_vm(self):
        from reproof.protected_service import ProtectedServiceAssemblyError
        empty=AndroidSigningMaterialResolver();self.addCleanup(empty.close)
        with patch('reproof.execution.qualification.NativeVM',side_effect=AssertionError('VM started')):
            with self.assertRaises(ProtectedServiceAssemblyError):self.assemble(resolver=empty)
        self.assert_no_owner_roots()

    def test_observer_change_during_vm_qualification_cannot_be_attached(self):
        from reproof.protected_service import ProtectedServiceAssemblyError
        qualify=self.owner.qualify_build
        path=Path(self.config.document['profiles'][0]['validation']['observers']['path'])
        def changed(*args,**kwargs):
            result=qualify(*args,**kwargs);path.write_bytes(path.read_bytes()+b' ');return result
        with patch.object(self.owner,'qualify_build',side_effect=changed):
            with self.assertRaises(ProtectedServiceAssemblyError):self.assemble()
        self.assertIsNone(self.f.bundle.workflow.repairs)
        self.assertTrue(self.owner.status()['closed'])

    def test_recovery_only_inventory_cannot_become_a_normal_execution_chain(self):
        from reproof.protected_service import ProtectedServiceAssemblyError
        self.f.fixture.lab.devices['device']['_recoveryOnly']=True
        with self.assertRaises(ProtectedServiceAssemblyError):self.assemble()
        self.assert_no_owner_roots()

    def test_foreign_validation_secret_owner_is_rejected_before_vm_work(self):
        from reproof.protected_service import ProtectedServiceAssemblyError
        self.secrets.claim(object())
        with patch('reproof.execution.qualification.NativeVM',side_effect=AssertionError('VM started')):
            with self.assertRaises(ProtectedServiceAssemblyError):self.assemble()
        self.assert_no_owner_roots()
        self.assertFalse(self.owner.status()['closed'])

    def test_mobile_factory_failure_collects_prior_owners_without_attaching_jobs(self):
        from reproof.protected_service import ProtectedServiceAssemblyError
        with patch.object(self.owner,'configure_android_mobile',side_effect=RuntimeError('owned assembly failure')):
            with self.assertRaises(ProtectedServiceAssemblyError):self.assemble()
        self.assertIsNone(self.f.bundle.protected_repairs)
        self.assertIsNone(self.f.bundle.workflow.repairs)
        self.assertTrue(self.owner.status()['closed'])
        with self.assertRaises(RuntimeError):self.resolver.require_registered(self.identity)

    def test_startup_interrupt_collects_created_owners(self):
        with patch.object(self.owner,'configure_android_mobile',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.assemble()
        self.assertTrue(self.owner.status()['closed'])
        self.assertIsNone(self.f.bundle.protected_repairs)
        self.assertIsNone(self.f.bundle.workflow.repairs)
        with self.assertRaises(RuntimeError):self.resolver.require_registered(self.identity)

    def test_mobile_revocation_during_build_prevents_signing_owner_creation(self):
        from reproof.protected_service import ProtectedServiceAssemblyError
        qualify=self.owner.qualify_build
        route=self.config.document['profiles'][0]['mobile']['route']
        def revoked(*args,**kwargs):
            result=qualify(*args,**kwargs)
            self.owner.authority.revoke_backend(route['backendId'],'mobile-device',route['environmentDigest'])
            return result
        with patch.object(self.owner,'qualify_build',side_effect=revoked):
            with self.assertRaises(ProtectedServiceAssemblyError):self.assemble()
        self.assertFalse(Path(self.config.document['profiles'][0]['signing']['ownerRoot']).exists())
        self.assertIsNone(self.f.bundle.workflow.repairs)

    def test_preparation_is_inert_and_does_not_read_private_signing_material(self):
        from reproof.protected_service import load_protected_service_inputs
        with patch('subprocess.Popen',side_effect=AssertionError('native process started')), \
                patch('socket.socket',side_effect=AssertionError('network contacted')), \
                patch.object(self.resolver,'open',side_effect=AssertionError('private material opened')):
            prepared=load_protected_service_inputs(self.config,self.f.issue,self.f.bundle)
        self.assertEqual(prepared.public()['executionAuthority'],'none')
        self.assertNotIn(str(self.root),json.dumps(prepared.public()))
        self.assert_no_owner_roots()

    def test_fresh_workflow_is_composed_and_owned_with_the_registered_chain(self):
        from reproof.protected_service import compose_android_protected_workflow
        bundle=compose_android_protected_workflow(self.f.fixture.lab,self.f.bundle.workflow.access,
            self.config,self.f.issue,root=self.root/'complete-service',owner=self.owner,
            signing_materials=self.resolver,validation_secrets=self.secrets,
            mobile_qualifications={'protected-android':self.qualification})
        self.addCleanup(bundle.close)
        self.assertIs(bundle.protected_repairs,self.owner)
        self.assertIsNotNone(bundle.workflow.repairs)
        self.assertIsNone(self.f.bundle.workflow.repairs)
