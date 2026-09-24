"""Registered iOS input binding with owned inert IPA data and no device effects."""
import copy
import hashlib
import json
import plistlib
from pathlib import Path
import secrets
import threading
import time
import unittest
from unittest.mock import patch

from reproof import contracts
from reproof.execution.artifacts import BlobSet
from reproof.ios_profile import validate_ios_profile
from reproof.ios_storage import tree_manifest
from reproof.live.access import AccessController,AccessStore
from reproof.live.issue_configuration import compose_issue_workflow
from reproof.repair_configuration import ProtectedServiceConfiguration
from reproof.repair_mobile import MobileContext
from tests import g4_support as workflow_fixture
from tests import test_ios_artifact_transfer as artifacts
from tests.test_protected_service_configuration import configuration,issue_configuration
from tests.test_worker_profiles import physical_ios_document


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def reference(path):return {'path':str(path),'sha256':sha(path)}


class IOSMobileInputsTests(unittest.TestCase):
    def setUp(self):
        self.files=artifacts.IOSArtifactTransferTests(methodName='runTest')
        self.addCleanup(self.files.doCleanups);self.files.setUp()
        self.app=self.files.make_flat_app()
        info=plistlib.loads((self.app/'Info.plist').read_bytes());info['CFBundlePackageType']='APPL'
        (self.app/'Info.plist').write_bytes(plistlib.dumps(info))
        self.ipa=self.files.make_ipa(self.app,self.files.root/'original.ipa');self.ipa.chmod(0o600)
        manifest=tree_manifest(self.app);self.tree_digest=contracts.digest(manifest)
        self.app_bytes=sum((self.app/name).stat().st_size for name in manifest)
        project=workflow_fixture.project_document()
        project['applications'][0]['bundle']='com.example.flat'
        project['builds'][0]['artifactDigest']=self.tree_digest
        policy=workflow_fixture.collection_policy();policy['captureMode']='test-data'
        with patch.object(workflow_fixture,'project_document',return_value=project), \
                patch.object(workflow_fixture,'collection_policy',return_value=policy):
            self.env=workflow_fixture.G4Environment()
        self.addCleanup(self.env.close);self.root=self.env.root.resolve()
        data=physical_ios_document(self.tree_digest)
        data.update(projectDigest=self.env.registration.project_digest,bundle='com.example.flat',
                    launchTarget={'kind':'bundle','value':'com.example.flat'})
        data['artifact'].update(bytes=self.app_bytes,bundleVersion='1.0',bundleBuild='27')
        self.profile=validate_ios_profile(data)
        self.udid='owned-'+secrets.token_hex(16)
        device=self.env.lab.devices['device']
        self.env.lab.project_grant_provider=lambda **_:None  # No grant is issued by metadata preflight.
        device.update(kind='ios-physical',_authority={'deviceKind':'ios-physical','physicalId':self.udid})
        device['capabilities'].update(applicationProfile=data,applicationProfileDigest=self.profile.digest,
                                      applicationIdentity=self.profile.application_identity)
        self.access_store=AccessStore(self.root/'input-access');self.addCleanup(self.access_store.close)
        self.access_store.bootstrap_administrator('admin');self.access_store.register_project('admin',self.env.project)
        self.access_store.assign_device('admin','device',project_id='checkout')
        access=AccessController(self.access_store);access.bind_project(self.env.registration)
        self.document=configuration(self.root);row=self.document['profiles'][0]
        row.update(projectDigest=self.env.registration.project_digest,deviceId='device',
                   runtimePolicyDigest=contracts.digest(workflow_fixture.runtime_policy()))
        for phase in ('build','mobile'):row[phase]['route']['projectDigest']=row['projectDigest']
        row['validation']['plan']['projectDigest']=row['projectDigest']
        self.issue=issue_configuration(row,workflow_fixture.runtime_policy())
        self.bundle=compose_issue_workflow(self.env.lab,access,self.issue,root=self.root/'configured-issues',defer_repairs=True)
        self.addCleanup(self.bundle.close)
        self.profile_path=self.root/'runtime-profile.json'
        self.profile_path.write_text(json.dumps(data));self.profile_path.chmod(0o600)
        native=Path('/Library/Developer/PrivateFrameworks/CoreDevice.framework/Versions/A/Resources/bin/devicectl').resolve()
        self.definition={'schemaVersion':1,'kind':'ios-mobile-definition-v1','owner':'repair',
            'udid':self.udid,'coreDeviceIdentifier':'11111111-2222-3333-4444-555555555555',
            'runtimeProfile':reference(self.profile_path),
            'query':{'devicectlPath':str(native),'devicectlSha256':sha(native),'workRoot':str(self.root/'query-owner')},
            'baselines':[{'role':'original','bundleId':'com.example.flat',
                'archive':{**reference(self.ipa),'bytes':self.ipa.stat().st_size}}],
            'preparations':[]}

    def selected(self,definition=None):
        path=Path(self.document['profiles'][0]['mobile']['definition']['path'])
        path.write_text(json.dumps(self.definition if definition is None else definition));path.chmod(0o600)
        self.document['profiles'][0]['mobile']['definition']['sha256']=sha(path)
        return ProtectedServiceConfiguration(self.document)

    def load(self,definition=None):
        from reproof.protected_mobile_inputs import load_protected_mobile_inputs
        config=self.selected(definition)
        return config,load_protected_mobile_inputs(config,self.issue,self.bundle)

    def assert_unstarted(self):
        self.assertEqual(self.env.control['calls'],[])
        row=self.document['profiles'][0]
        for path in (row['mobile']['ownerRoot'],row['mobile']['journal']['root'],self.definition['query']['workRoot']):
            self.assertFalse(Path(path).exists())

    def test_load_binds_real_registration_profile_and_ipa_without_execution_or_extraction(self):
        with (patch('subprocess.Popen',side_effect=AssertionError('Loader started a tool')),
                patch('socket.socket',side_effect=AssertionError('Loader contacted a network')),
                patch('reproof.ios_artifact_transfer._opened_ipa_contents',side_effect=AssertionError('Loader extracted an app'))):
            config,loaded=self.load()
        selected=loaded.profile('protected-ios')
        self.assertIs(selected.config.registration,self.env.registration)
        self.assertIs(selected.config.service,self.bundle.workflow.runtimes['checkout'].service)
        self.assertEqual(selected.config.definition.original_profile_digest,self.profile.digest)
        self.assertEqual(loaded.public()['executionAuthority'],'none')
        self.assertTrue(self.udid not in repr(loaded.public()) and str(self.root) not in repr(loaded.public()))
        self.assert_unstarted()

    def test_fixed_xctest_tools_and_both_helpers_are_bound_without_starting_them(self):
        from tests.test_ios_device_guardian import build_owned_guardian
        from tests.test_ios_device_tools import PinnedDeviceCtlTests
        tool_fixture=PinnedDeviceCtlTests(methodName='runTest');tool_fixture.setUp()
        self.addCleanup(tool_fixture.doCleanups)
        guardian=build_owned_guardian(self,self.root)
        developer=self.root/'Developer';(developer/'usr/bin').mkdir(parents=True)
        binary=developer/'usr/bin/xcodebuild';binary.write_bytes(tool_fixture.tool.read_bytes());binary.chmod(0o700)
        value=copy.deepcopy(self.definition)
        value['query']['nativeGuardian']={'path':str(guardian.path),'sha256':guardian.sha256}
        template=(Path(__file__).parent/'fixtures/ios-xctest-template/ReproLive_iphoneos.xctestrun').resolve()
        value['xctest']={'xcodebuildPath':str(binary),'xcodebuildSha256':sha(binary),'developerRoot':str(developer),
            'template':reference(template)}
        for role,bundle in (('helper-host','com.example.helper.host'),('helper-runner','io.reproof.live.tests.xctrunner')):
            app=self.files.make_flat_app()
            info=plistlib.loads((app/'Info.plist').read_bytes())
            info.update(CFBundleIdentifier=bundle,CFBundlePackageType='APPL')
            (app/'Info.plist').write_bytes(plistlib.dumps(info))
            ipa=self.files.make_ipa(app,self.files.root/(role+'.ipa'));ipa.chmod(0o600)
            value['baselines'].append({'role':role,'bundleId':bundle,'archive':{**reference(ipa),'bytes':ipa.stat().st_size}})
        with (patch('subprocess.Popen',side_effect=AssertionError('Loader launched a tool')),
                patch('socket.socket',side_effect=AssertionError('Loader contacted a device'))):
            _,loaded=self.load(value)
        config=loaded.profile('protected-ios').config
        self.assertEqual(config.definition.xctest_definition_digest,config.xctest.definition_digest)
        self.assertEqual(config.definition.xctest_reserved_bytes,6*64*1024*1024)
        self.assert_unstarted()
        from reproof.protected_mobile_inputs import ProtectedMobileInputsError
        for changed in (dict(value,xctest={**value['xctest'],'command':'untrusted'}),
                        dict(value,baselines=value['baselines'][:1])):
            with self.assertRaises(ProtectedMobileInputsError):self.load(changed)

    def test_changed_ipa_cannot_become_original_just_by_updating_its_reference(self):
        from reproof.protected_mobile_inputs import ProtectedMobileInputsError
        extra=self.app/'changed.txt';extra.write_bytes(b'owned change');extra.chmod(0o600)
        self.files.make_ipa(self.app,self.ipa)
        changed=copy.deepcopy(self.definition)
        changed['baselines'][0]['archive']={**reference(self.ipa),'bytes':self.ipa.stat().st_size}
        with self.assertRaises(ProtectedMobileInputsError):self.load(changed)
        self.assert_unstarted()

    def test_foreign_device_remote_device_and_unassigned_registration_are_rejected(self):
        from reproof.protected_mobile_inputs import ProtectedMobileInputsError
        changed=copy.deepcopy(self.definition);changed['udid']='unselected-device'
        with self.assertRaises(ProtectedMobileInputsError):self.load(changed)
        self.env.lab.devices['device']['_remoteAuthority']=True
        with self.assertRaises(ProtectedMobileInputsError):self.load()
        self.assert_unstarted()

    def test_commands_credentials_and_overlapping_query_work_are_rejected(self):
        from reproof.protected_mobile_inputs import ProtectedMobileInputsError
        for alter in (lambda d:d.update(command='untrusted'),lambda d:d['query'].update(password='owned-invalid'),
                      lambda d:d['query'].update(workRoot=self.document['profiles'][0]['mobile']['ownerRoot'])):
            value=copy.deepcopy(self.definition);alter(value)
            with self.subTest(alter=alter),self.assertRaises(ProtectedMobileInputsError):self.load(value)
        self.assert_unstarted()

    def test_loaded_inputs_recheck_current_source_and_assignment_before_opening_store(self):
        from reproof.protected_mobile_inputs import ProtectedMobileInputsError
        config,loaded=self.load()
        self.profile_path.write_bytes(self.profile_path.read_bytes()+b' ')
        with self.assertRaises(ProtectedMobileInputsError):
            loaded.open_ios_preparation('protected-ios',config,self.issue,self.bundle)
        self.assert_unstarted()

    def test_assignment_loss_before_factory_prevents_any_journal_creation(self):
        from reproof.protected_mobile_inputs import ProtectedMobileInputsError
        config,loaded=self.load()
        with patch.object(self.access_store,'assignment_project_ids',return_value=()):
            with self.assertRaises(ProtectedMobileInputsError):
                loaded.open_ios_preparation('protected-ios',config,self.issue,self.bundle)
        self.assert_unstarted()

    def test_helper_pair_is_bound_by_role_bundle_and_container_digest(self):
        value=copy.deepcopy(self.definition)
        for role,bundle in (('helper-host','com.example.host'),('helper-runner','com.example.runner')):
            app=self.files.make_flat_app();info=plistlib.loads((app/'Info.plist').read_bytes())
            info.update(CFBundlePackageType='APPL',CFBundleIdentifier=bundle)
            (app/'Info.plist').write_bytes(plistlib.dumps(info))
            archive=self.files.make_ipa(app,self.files.root/(role+'.ipa'));archive.chmod(0o600)
            value['baselines'].append({'role':role,'bundleId':bundle,
                'archive':{**reference(archive),'bytes':archive.stat().st_size}})
        _,loaded=self.load(value);config=loaded.profile('protected-ios').config
        self.assertEqual(set(dict(config.definition.helper_bundles)),{'helper-host','helper-runner'})
        self.assertEqual(config.read_baselines().digest,config.definition.baseline_digest)
        self.assert_unstarted()

    def test_source_change_after_zip_preflight_cannot_be_used(self):
        from reproof import ios_mobile_inputs as inputs
        from reproof.protected_mobile_inputs import ProtectedMobileInputsError
        original=inputs._zip_preflight
        def changed(*args,**kwargs):
            result=original(*args,**kwargs);self.ipa.write_bytes(self.ipa.read_bytes()+b'owned-change');return result
        with patch.object(inputs,'_zip_preflight',side_effect=changed):
            with self.assertRaises(ProtectedMobileInputsError):self.load()
        self.assert_unstarted()

    def test_registered_inputs_drive_actual_preparation_and_recovery(self):
        config,loaded=self.load();selected=loaded.profile('protected-ios')
        operations=loaded.open_ios_preparation('protected-ios',config,self.issue,self.bundle)
        self.addCleanup(operations.close)
        blobs=BlobSet((('candidate.ipa',self.ipa.read_bytes()),));definition=selected.config.definition
        context=MobileContext('from-inputs','1'*64,'2'*64,definition.project_digest,definition.application_id,
            '3'*64,sha(self.ipa),definition.scope_digest,definition.runtime_policy_digest,'owned-input-nonce')
        with operations.admit(context,blobs,selected.config.read_baselines()) as operation:
            prepared=operations.prepare(operation,'candidate',cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10)
            self.assertEqual(prepared.manifest['applicationId'],'com.example.flat')
        with operations.preparation_recovery(context.operation_id,context.request_digest,
                cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10) as capability:
            result=operations.run_store.finish_ios_preparation_recovery(capability,authority=operations)
        self.assertEqual(result['reservedBytes'],0)
        self.assertFalse(Path(self.definition['query']['workRoot']).exists())
        self.assertEqual(self.env.control['calls'],[])

    def test_guardian_reference_is_inert_pinned_and_outside_the_query_workspace(self):
        from reproof.protected_mobile_inputs import ProtectedMobileInputsError
        from tests import test_ios_device_guardian as guardian_fixtures
        directory=self.root/'owned-guardian-tools';directory.mkdir(mode=0o700)
        guardian=guardian_fixtures.build_owned_guardian(self,directory)
        definition=copy.deepcopy(self.definition)
        definition['query']['nativeGuardian']={'path':str(guardian.path),'sha256':guardian.sha256}
        with patch('subprocess.Popen',side_effect=AssertionError('Input loading started a process')):
            config,loaded=self.load(definition)
            query=loaded.profile('protected-ios').config.query
            self.assertEqual(query.native_guardian.definition_digest,guardian.definition_digest)
            self.assert_unstarted()
        overlap=copy.deepcopy(definition);overlap['query']['workRoot']=str(directory)
        with self.assertRaises(ProtectedMobileInputsError):self.load(overlap)
        self.selected(definition)
        guardian.path.write_bytes(guardian.path.read_bytes()+b'owned alteration')
        with self.assertRaises(ProtectedMobileInputsError):loaded.verify(config,self.issue,self.bundle)
        self.assert_unstarted()
