"""Mobile input loading with real registries and explicit native bridge doubles."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
import time
from unittest.mock import patch

from reproloop import contracts
from reproloop.live.access import AccessController,AccessStore
from reproloop.live.issue_configuration import compose_issue_workflow
from reproloop.repair_configuration import ProtectedServiceConfiguration
from tests import test_repair_android as support
from tests.test_protected_service_configuration import configuration,issue_configuration
from tests.g4_support import runtime_policy


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def blob(path):return {'path':str(path),'sha256':sha(path),'bytes':path.stat().st_size}


class ProtectedMobileInputsTests(unittest.TestCase):
    def setUp(self):
        self.fixture=support.AndroidAdapterTests(methodName='runTest')
        self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        f=self.fixture;self.root=f.root
        store=AccessStore(self.root/'public-access');self.addCleanup(store.close)
        store.bootstrap_administrator('admin');store.register_project('admin',f.registration.project)
        store.assign_device('admin','device',project_id=f.registration.project['id'])
        access=AccessController(store);access.bind_project(f.registration)
        self.document=configuration(self.root);row=self.document['profiles'][0]
        row.update(id='protected-android',projectId=f.registration.project['id'],projectDigest=f.registration.project_digest,
            platform='android',deviceId='device',runtimePolicyDigest=f.config.runtime_policy_digest)
        for phase in ('build','mobile'):row[phase]['route']['projectDigest']=row['projectDigest']
        row['mobile']['route'].update(platform='android',artifactPolicyId='signed-apk')
        row['signing']['policy'].update(platform='android',tool='host-apksigner-fixed')
        row['signing']['policy'].pop('provisioningReferenceId')
        row['validation']['plan']['projectDigest']=row['projectDigest']
        self.issue=issue_configuration(row,runtime_policy());project=self.issue['projects'][0]
        project['fixtures']=[{'applicationId':'ios_app','fixtureId':'seed_account','endpointId':'fixture_service',
            'baseUrl':f'http://127.0.0.1:{f.remote.port}','checkRecipeIds':['check_account'],
            'cleanupRecipeId':'cleanup_account','payload':{}}]
        project['variables']=[{'variableId':'secret_text','environment':'OWNED_MOBILE_INPUT_VALUE'}]
        project['observations']=[{'observationId':'screen','providerIncarnation':'owned-observer',
            'baseUrl':'http://127.0.0.1:12345','coverage':['snapshot']}]
        self.bundle=compose_issue_workflow(f.lab,access,self.issue,root=self.root/'public-issues',defer_repairs=True)
        self.addCleanup(self.bundle.close)
        profile_path=self.root/'runtime-profile.json';profile_path.write_text(json.dumps(f.profile.data));profile_path.chmod(0o600)
        self.definition={'schemaVersion':1,'kind':'android-mobile-definition-v1','owner':'repair','serial':'android-test',
            'runtimeProfile':{'path':str(profile_path),'sha256':sha(profile_path)},
            'originalApk':blob(f.original),'helperApk':blob(f.helper),
            'tools':{'adbPath':str(f.tools.adb),'adbSha256':f.tools.adb_digest,
                'packageInspectorPath':str(f.tools.package_inspector),'packageInspectorSha256':f.tools.package_inspector_digest},
            'preparations':[{'fixtureId':'seed_account','payloadDigest':contracts.digest({})}]}

    def selected(self, definition=None):
        row=self.document['profiles'][0];path=Path(row['mobile']['definition']['path'])
        path.write_text(json.dumps(self.definition if definition is None else definition));path.chmod(0o600)
        row['mobile']['definition']['sha256']=sha(path)
        return ProtectedServiceConfiguration(self.document)

    def load(self, definition=None):
        from reproloop.protected_mobile_inputs import load_protected_mobile_inputs
        return load_protected_mobile_inputs(self.selected(definition),self.issue,self.bundle)

    def assert_unstarted(self):
        self.assertEqual(json.loads(self.fixture.state_path.read_bytes())['commands'],[])
        row=self.document['profiles'][0]
        self.assertFalse(Path(row['mobile']['journal']['root']).exists())
        self.assertFalse(Path(row['mobile']['ownerRoot']).exists())

    def test_loader_uses_exact_runtime_and_files_without_starting_adb_or_resolving_variables(self):
        from reproloop.repair_android import AndroidMobileAdapterConfig
        with (patch('subprocess.Popen',side_effect=AssertionError('ADB started')),
              patch('socket.socket',side_effect=AssertionError('network contacted'))):loaded=self.load()
        profile=loaded.profile('protected-android')
        self.assertIs(type(profile.config),AndroidMobileAdapterConfig)
        self.assertIs(profile.config.service,self.bundle.workflow.runtimes['integration_project'].service)
        self.assertEqual(profile.config.original_profile,self.fixture.profile)
        self.assertEqual(profile.config.preparations[0].payload,{})
        self.assertEqual(loaded.public()['executionAuthority'],'none')
        self.assertNotIn(str(self.root),str(loaded.public()));self.assertNotIn('android-test',str(loaded.public()))
        self.assert_unstarted()

    def test_mismatched_serial_profile_and_fixture_payload_are_rejected(self):
        from reproloop.protected_mobile_inputs import ProtectedMobileInputsError
        for change in (lambda d:d.update(serial='other-device'),
                       lambda d:d['runtimeProfile'].update(sha256='0'*64),
                       lambda d:d['preparations'][0].update(payloadDigest='0'*64),
                       lambda d:d['preparations'][0].update(fixtureId='unknown-fixture')):
            document=copy.deepcopy(self.definition);change(document)
            with self.subTest(change=change),self.assertRaises(ProtectedMobileInputsError):self.load(document)
        self.assert_unstarted()

    def test_changed_apk_helper_and_tools_cannot_become_prepared_inputs(self):
        from reproloop.protected_mobile_inputs import ProtectedMobileInputsError
        for section,field,value in (('originalApk','sha256','0'*64),('originalApk','bytes',1),
                                    ('helperApk','sha256','0'*64),('helperApk','bytes',1),
                                    ('tools','adbSha256','0'*64)):
            document=copy.deepcopy(self.definition);document[section][field]=value
            with self.subTest(section=section,field=field),self.assertRaises(ProtectedMobileInputsError):self.load(document)
        self.assert_unstarted()

    def test_credential_commands_and_imported_qualification_are_rejected(self):
        from reproloop.protected_mobile_inputs import ProtectedMobileInputsError
        for name,value in (('password','OwnedCredentialCanary'),('command','OwnedCommandCanary'),('qualified',True)):
            document=dict(self.definition,**{name:value})
            with self.subTest(name=name),self.assertRaises(ProtectedMobileInputsError) as error:self.load(document)
            self.assertNotIn('Canary',str(error.exception))
        self.assert_unstarted()

    def test_changed_loaded_payload_or_current_runtime_cannot_be_revalidated(self):
        from reproloop.protected_mobile_inputs import ProtectedMobileInputsError
        selected=self.selected();loaded=self.load()
        loaded.profile('protected-android').config.preparations[0].payload['unapproved']='value'
        with self.assertRaises(ProtectedMobileInputsError):loaded.verify(selected,self.issue,self.bundle)
        current=self.load();self.bundle.workflow.runtimes['integration_project'].preparations[0].payload['changed']='value'
        with self.assertRaises(ProtectedMobileInputsError):current.verify(selected,self.issue,self.bundle)
        self.assert_unstarted()

    def test_loaded_config_drives_fixed_adapter_installation_and_original_restoration(self):
        from reproloop.execution.artifacts import BlobSet
        from reproloop.repair_android import AndroidTrustedMobileAdapter
        from reproloop.repair_mobile import MobileInstallationObservation
        import time
        loaded=self.load();adapter=AndroidTrustedMobileAdapter(loaded.profile('protected-android').config)
        self.addCleanup(lambda:adapter.close(deadline_monotonic=time.monotonic()+5))
        result=adapter.install(self.fixture.context,BlobSet((('candidate.apk',self.fixture.candidate),)),**self.fixture.bounds())
        self.assertIs(type(result),MobileInstallationObservation)
        cleaned=adapter.cleanup(self.fixture.context,**self.fixture.bounds())
        self.assertTrue(cleaned.ownership_released and cleaned.sanitation_confirmed)
        self.assertEqual(json.loads(self.fixture.state_path.read_bytes())['installed'],self.fixture.original_sha)

    def test_validation_definitions_bind_only_registered_external_sources_and_scoped_credentials(self):
        from reproloop.protected_validation_inputs import load_android_validation_inputs
        from reproloop.protected_validation import ValidationSecretRegistry
        from reproloop.repair_android import AndroidTrustedMobileAdapter
        from reproloop.validation import ValidationError
        loaded=self.load().profile('protected-android');row=self.document['profiles'][0]
        document={'schemaVersion':1,'kind':'unix-validation-observers-v1','observers':[{
            'sourceId':'registered-observer','providerId':'owned-observer','socketPath':str(self.root/'observer.sock'),
            'authenticationReferenceId':'owned-auth'}]}
        path=Path(row['validation']['observers']['path']);path.write_text(json.dumps(document));path.chmod(0o600)
        reference={'path':str(path),'sha256':sha(path)}
        inputs=load_android_validation_inputs(reference,plan=row['validation']['plan'],mobile_inputs=loaded)
        secrets=ValidationSecretRegistry();self.addCleanup(secrets.close)
        adapter=AndroidTrustedMobileAdapter(loaded.config)
        import time
        self.addCleanup(lambda:adapter.close(deadline_monotonic=time.monotonic()+5))
        with self.assertRaises(ValidationError):inputs.bind(adapter,secrets)
        secrets.register('owned-auth',project_digest=loaded.config.registration.project_digest,
            provider_id='owned-observer',secret=b'o'*32)
        with (patch('socket.socket',side_effect=AssertionError('binding contacted observer')),
              patch('subprocess.Popen',side_effect=AssertionError('binding started native work'))):
            authority=inputs.bind(adapter,secrets)
        self.assertEqual(authority.ready(),contracts.digest(row['validation']['plan']))
        wrong=copy.deepcopy(document);wrong['observers'][0]['sourceId']='unregistered'
        path.write_text(json.dumps(wrong));reference['sha256']=sha(path)
        with self.assertRaises(ValidationError):
            load_android_validation_inputs(reference,plan=row['validation']['plan'],mobile_inputs=loaded)
        self.assert_unstarted()

    def test_remote_device_cannot_be_loaded_as_local_before_or_after_file_reads(self):
        from reproloop import protected_mobile_inputs as inputs
        device=self.fixture.lab.devices['device']
        try:
            device['_remoteAuthority']=True
            with patch.object(inputs,'_json',wraps=inputs._json) as read:
                with self.assertRaises(inputs.ProtectedMobileInputsError):self.load()
                read.assert_not_called()
            device.pop('_remoteAuthority')
            original=inputs._load_profile
            def changed(*args,**kwargs):
                result=original(*args,**kwargs);device['_remoteAuthority']=True;return result
            with patch.object(inputs,'_load_profile',side_effect=changed):
                with self.assertRaises(inputs.ProtectedMobileInputsError):self.load()
        finally:device.pop('_remoteAuthority',None)
        self.assert_unstarted()

    def test_explicit_adb_endpoint_is_loaded_and_bound_to_the_operation_journal(self):
        from dataclasses import replace
        from reproloop.execution.journal import RunStore
        from reproloop.execution.artifacts import BlobSet
        from reproloop.repair_android_operation import AndroidOperationStore,AndroidOperationError
        from reproloop.live.authority import canonical_device_fingerprint
        from tests.test_adb_endpoint import OwnedAdbServer
        server=OwnedAdbServer(self.root);self.addCleanup(server.close)
        serial='owned-endpoint-'+hashlib.sha256(str(self.root).encode()).hexdigest()[:24]
        self.fixture.lab.devices['device']['_authority']['physicalId']=serial
        self.definition['serial']=serial
        context=replace(self.fixture.context,scope_digest=canonical_device_fingerprint('android',serial))
        self.definition['adbEndpoint']={'socketPath':str(server.path),'serverVersion':41,
            'sandboxSha256':sha('/usr/bin/sandbox-exec')}
        config=self.load().profile('protected-android').config
        self.assertEqual(config.adb_endpoint.socket_path,server.path)
        store=RunStore(self.root/'endpoint-journal',environment_digest='e'*64,disk_limit=4*1024**3)
        operations=AndroidOperationStore(store,config,self.root/'endpoint-operations')
        self.addCleanup(lambda:operations.close(deadline_monotonic=time.monotonic()+5))
        with operations.admit(context,BlobSet((('candidate.apk',self.fixture.candidate),))):pass
        intent=json.loads((operations.operations/context.operation_id/'intent.json').read_bytes())
        self.assertEqual(intent['configuration']['adbEndpointDigest'],config.adb_endpoint.definition_digest)
        downgraded=AndroidOperationStore(store,replace(config,adb_endpoint=None),self.root/'endpoint-operations',create=False)
        self.addCleanup(lambda:downgraded.close(deadline_monotonic=time.monotonic()+5))
        with self.assertRaises(AndroidOperationError):
            with downgraded.recovery(context.operation_id,context.request_digest):pass
        self.assertGreater(store.status(context.operation_id)['reservedBytes'],0)
        self.assertEqual(server.requests,[]);self.assert_unstarted()

    def test_guardian_definition_is_pinned_without_starting_a_process(self):
        from tests.test_adb_endpoint import OwnedAdbServer
        from reproloop.protected_mobile_inputs import ProtectedMobileInputsError
        server=OwnedAdbServer(self.root);self.addCleanup(server.close)
        path=self.root/'owned-guardian'
        self.assertFalse(path.exists())
        path.write_bytes(self.fixture.tools.adb.read_bytes());path.chmod(0o700)
        self.definition['adbEndpoint']={'socketPath':str(server.path),'serverVersion':41,
            'sandboxSha256':sha('/usr/bin/sandbox-exec')}
        self.definition['nativeGuardian']={'path':str(path),'sha256':sha(path)}
        with patch('subprocess.Popen',side_effect=AssertionError('Native owner started during loading')):
            config=self.load().profile('protected-android').config
        self.assertEqual(config.native_guardian.path,path)
        self.assertEqual(config.native_guardian.sha256,sha(path))
        self.definition['nativeGuardian']['sha256']='0'*64
        with self.assertRaises(ProtectedMobileInputsError):self.load()
        self.assertEqual(server.requests,[]);self.assert_unstarted()
