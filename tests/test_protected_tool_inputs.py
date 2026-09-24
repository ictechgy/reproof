"""Real fixed tool loading and synthetic VM resources; no VM or device is started."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reproof.execution.resources import GuestBundle, provision
from reproof.ios_signing_inputs import IOSSigningOwnerTools
from reproof.ios_signing_tools import IOSSigningBuildTools, build_ios_signing_owner
from reproof.repair_configuration import ProtectedServiceConfiguration
from tests import test_protected_service_configuration as support
from tests.test_execution_resources import resource_inputs
from tests.test_ios_signing_tools import SDK, sha


class ProtectedToolInputsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary=tempfile.TemporaryDirectory(prefix='owned-protected-tool-inputs-')
        cls.addClassCleanup(temporary.cleanup)
        cls.tools_root=Path(temporary.name).resolve()/'tools'
        cls.built=build_ios_signing_owner(cls.tools_root,
            IOSSigningBuildTools(Path('/usr/bin/clang'),sha('/usr/bin/clang'),SDK,sha(SDK/'SDKSettings.json')),
            cancellation=threading.Event(),deadline_monotonic=time.monotonic()+45)

    def setUp(self):
        self.fixture=support.ProtectedRuntimeConfigurationTests(methodName='runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.env.root.resolve()
        self.document=copy.deepcopy(self.fixture.value); row=self.document['profiles'][0]
        source=self.root/'resource-source'; source.mkdir()
        self.metadata,self.resources=resource_inputs(source)
        recipe=self.metadata['catalog'][0]; recipe['outputPaths']=['candidate.ipa']
        self.bundle=provision(Path(row['build']['bundlePath']),metadata=self.metadata,resources=self.resources)
        row['build']['route'].update(environmentDigest=self.bundle.environment_digest,
            recipeId=recipe['id'],artifactPolicyId=recipe['artifactPolicyId'],cleanupPolicyId=recipe['cleanupPolicyId'])
        row['build']['journal']['environmentDigest']=self.bundle.environment_digest
        row['signing'].update(toolsPath=str(self.tools_root),toolsManifestSha256=self.built.manifest_digest)
        self.issue=support.issue_configuration(row,self.fixture.bundle.workflow.runtimes['checkout'].runtime_policy)

    def load(self, document=None):
        from reproof.protected_tool_inputs import load_protected_tool_inputs
        return load_protected_tool_inputs(ProtectedServiceConfiguration(document or self.document),
            self.issue,self.fixture.bundle)

    def assert_no_operation_roots(self):
        row=self.document['profiles'][0]
        for phase in ('build','signing','mobile'):
            self.assertFalse(Path(row[phase]['journal']['root']).exists())
        for phase in ('signing','mobile'): self.assertFalse(Path(row[phase]['ownerRoot']).exists())

    def test_actual_tools_and_bundle_are_loaded_without_creating_execution_owners(self):
        with (patch('subprocess.Popen',side_effect=AssertionError('input loading started a process')),
              patch('socket.socket',side_effect=AssertionError('input loading contacted a service'))):
            prepared=self.load()
        selected=prepared.profile('protected-ios')
        self.assertIs(type(selected.build_bundle),GuestBundle)
        self.assertIs(type(selected.signing_tools),IOSSigningOwnerTools)
        self.assertEqual(selected.build_bundle.environment_digest,self.bundle.environment_digest)
        self.assertEqual(selected.signing_tools.definition_digest,self.built.tools.definition_digest)
        self.assertEqual(prepared.public()['executionAuthority'],'none')
        self.assertNotIn(str(self.root),str(prepared.public()))
        self.assert_no_operation_roots()

    def test_runtime_mismatch_is_rejected_before_any_referenced_file_is_loaded(self):
        from reproof.protected_tool_inputs import ProtectedToolInputsError
        changed=copy.deepcopy(self.document); changed['profiles'][0]['deviceId']='missing-device'
        with (patch('reproof.protected_tool_inputs.GuestBundle.load') as bundle,
              patch('reproof.protected_tool_inputs.load_ios_signing_owner') as tools):
            with self.assertRaises(ProtectedToolInputsError): self.load(changed)
            bundle.assert_not_called(); tools.assert_not_called()
        self.assert_no_operation_roots()

    def test_environment_recipe_output_and_cleanup_cannot_drift_from_the_bundle(self):
        from reproof.protected_tool_inputs import ProtectedToolInputsError
        for field,value in (('environmentDigest','9'*64),('artifactPolicyId','other-policy'),
                            ('cleanupPolicyId','other-cleanup')):
            changed=copy.deepcopy(self.document); changed['profiles'][0]['build']['route'][field]=value
            if field=='environmentDigest': changed['profiles'][0]['build']['journal'][field]=value
            with self.subTest(field=field),self.assertRaises(ProtectedToolInputsError): self.load(changed)
        metadata=copy.deepcopy(self.metadata); metadata['catalog'][0]['outputPaths']=['unsupported.bin']
        other=provision(self.root/'other-bundle',metadata=metadata,resources=self.resources)
        changed=copy.deepcopy(self.document); build=changed['profiles'][0]['build']
        build['bundlePath']=str(other.root); build['route']['environmentDigest']=other.environment_digest
        build['journal']['environmentDigest']=other.environment_digest
        with self.assertRaises(ProtectedToolInputsError): self.load(changed)
        self.assert_no_operation_roots()

    def test_changed_manifest_binary_and_vm_resource_invalidate_prepared_inputs(self):
        from reproof.protected_tool_inputs import ProtectedToolInputsError
        tools=self.root/'copied-tools'; shutil.copytree(self.tools_root,tools)
        changed=copy.deepcopy(self.document); changed['profiles'][0]['signing']['toolsPath']=str(tools)
        configuration=ProtectedServiceConfiguration(changed)
        prepared=self.load(changed)
        manifest=tools/'tools-manifest.json'; body=manifest.read_bytes()
        manifest.write_bytes(body+b' ')
        with self.assertRaises(ProtectedToolInputsError): prepared.verify(configuration,self.issue,self.fixture.bundle)
        manifest.write_bytes(body)
        signer=tools/'ios-signing-owner'; executable=signer.read_bytes(); signer.write_bytes(b'changed owned tool')
        with self.assertRaises(ProtectedToolInputsError): prepared.verify(configuration,self.issue,self.fixture.bundle)
        signer.write_bytes(executable)
        disk=self.bundle.path('disk'); disk.chmod(0o600); disk.write_bytes(b'changed owned VM fixture')
        with self.assertRaises(ProtectedToolInputsError): prepared.verify(configuration,self.issue,self.fixture.bundle)
        self.assert_no_operation_roots()

    def test_runtime_is_rechecked_after_io_and_failed_loading_returns_no_partial_owner(self):
        from reproof import protected_tool_inputs as inputs
        original=inputs.load_ios_signing_owner
        def changed(*args,**kwargs):
            result=original(*args,**kwargs)
            self.fixture.bundle.workflow.lab.devices['device']['capabilities']['applicationIdentity']['artifactDigest']='9'*64
            return result
        with patch.object(inputs,'load_ios_signing_owner',side_effect=changed):
            with self.assertRaises(inputs.ProtectedToolInputsError): self.load()
        self.assert_no_operation_roots()

    def test_android_branch_loads_real_pinned_jvm_tools_for_a_single_apk_recipe(self):
        from reproof.android_signing_tools import build_android_signing_owner
        from reproof.protected_tool_inputs import _load_profile
        from reproof.repair_signing_recovery import SigningOwnerTools
        from tests.test_android_signing_tools import _actual_tools
        output=self.root/'android-tools'
        built=build_android_signing_owner(output,_actual_tools(),cancellation=threading.Event(),
            deadline_monotonic=time.monotonic()+45)
        metadata=copy.deepcopy(self.metadata);metadata['catalog'][0]['outputPaths']=['candidate.apk']
        bundle=provision(self.root/'android-bundle',metadata=metadata,resources=self.resources)
        document=copy.deepcopy(self.document);row=document['profiles'][0]
        row['platform']='android';row['mobile']['route']['platform']='android'
        row['build']['bundlePath']=str(bundle.root)
        row['build']['route']['environmentDigest']=bundle.environment_digest
        row['build']['journal']['environmentDigest']=bundle.environment_digest
        row['signing'].update(toolsPath=str(output),toolsManifestSha256=built.manifest_digest)
        row['signing']['policy'].update(platform='android',tool='host-apksigner-fixed')
        row['signing']['policy'].pop('provisioningReferenceId')
        checked=ProtectedServiceConfiguration(document).document['profiles'][0]
        with patch('subprocess.Popen',side_effect=AssertionError('tool loading ran JVM')):
            loaded=_load_profile(checked)
        self.assertIs(type(loaded.signing_tools),SigningOwnerTools)
        self.assertEqual(loaded.signing_tools.definition_digest,built.tools.definition_digest)
        self.assert_no_operation_roots()

    def signing_definition(self):
        from reproof import contracts
        from tests.test_protected_signing_inputs import ios_document
        for name,body in (('chain-0.der',b'\x30\x0a\x04\x08leafxxxx'),
                          ('chain-1.der',b'\x30\x0a\x04\x08rootxxxx'),('owned-profile.cms',b'owned unverified profile')):
            path=self.root/name;path.write_bytes(body);path.chmod(0o600)
        policies={'.':{'bundleId':'com.example.app','entitlements':{}}}
        document=ios_document(self.root,policies=policies,profile_digest='a'*64)
        row=self.document['profiles'][0]
        row['signing']['policy'].update(identityReferenceId='owned-key',provisioningReferenceId='owned-profiles',
                                        entitlementsDigest=contracts.digest(policies))
        path=Path(row['signing']['definition']['path']);path.write_text(json.dumps(document));path.chmod(0o600)
        row['signing']['definition']['sha256']=sha(path)
        return ProtectedServiceConfiguration(self.document)

    def test_combined_input_loading_retains_exact_definitions_without_creating_journals(self):
        from reproof.protected_build_signing_inputs import load_protected_build_signing_inputs,ProtectedBuildSigningInputsError
        configuration=self.signing_definition()
        prepared=load_protected_build_signing_inputs(configuration,self.issue,self.fixture.bundle)
        self.assertEqual(prepared.profile('protected-ios').signing.identity.reference_id,'owned-key')
        self.assertEqual(prepared.public()['executionAuthority'],'none')
        self.assertNotIn(str(self.root),str(prepared.public()))
        self.assert_no_operation_roots()
        (self.root/'owned-profile.cms').write_bytes(b'changed')
        with self.assertRaises(ProtectedBuildSigningInputsError):prepared.verify(configuration,self.issue,self.fixture.bundle)

    def test_service_profile_budget_rejects_before_reading_profile_or_certificate_bytes(self):
        from reproof import protected_build_signing_inputs as inputs
        configuration=self.signing_definition()
        with (patch.object(inputs,'MAX_RETAINED_PROFILE_BYTES',0),
              patch('reproof.protected_signing_inputs._read_blob') as read):
            with self.assertRaises(inputs.ProtectedBuildSigningInputsError):
                inputs.load_protected_build_signing_inputs(configuration,self.issue,self.fixture.bundle)
            read.assert_not_called()
        self.assert_no_operation_roots()

    def test_combined_loading_rechecks_original_after_signing_definition_capture(self):
        from reproof import protected_build_signing_inputs as inputs
        configuration=self.signing_definition();original=inputs.load_signing_definition
        def changed(*args,**kwargs):
            result=original(*args,**kwargs)
            self.fixture.bundle.workflow.lab.devices['device']['capabilities']['applicationIdentity']['artifactDigest']='9'*64
            return result
        with patch.object(inputs,'load_signing_definition',side_effect=changed):
            with self.assertRaises(inputs.ProtectedBuildSigningInputsError):
                inputs.load_protected_build_signing_inputs(configuration,self.issue,self.fixture.bundle)
        self.assert_no_operation_roots()
