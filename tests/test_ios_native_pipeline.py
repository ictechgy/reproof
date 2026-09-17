"""Actual owned CMS, memory-only app signing, independent inspection and journal cleanup."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

from reproloop import contracts
from reproloop.execution.artifacts import BlobSet
from reproloop.execution.journal import RunStore, TERMINAL
from reproloop.ios_provisioning_cms import IOSCmsTools, IOSCmsTrust
from reproloop.ios_provisioning_policy import decoded_profile_digest
from reproloop.ios_signing_inputs import (IOSSigningDefinition, IOSSigningIdentity, IOSSigningMaterialResolver,
    IOSSigningOwnerTools, IOSSigningProvisioning)
from reproloop.ios_signing_operation import IOSSigningOperationStore
from reproloop.repair_signing import SigningContext, SigningObservation, SignatureObservation, SigningFailureObservation
from reproloop.resources import read_resource
from tests import test_ios_provisioning_cms as cms_fixture


def sha(body): return hashlib.sha256(body).hexdigest()


class IOSNativePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cms_fixture.IOSCmsVerificationTests.setUpClass()
        cls.addClassCleanup(cms_fixture.IOSCmsVerificationTests.doClassCleanups)
        material = cms_fixture.IOSCmsVerificationTests
        temporary = tempfile.TemporaryDirectory(prefix='owned-ios-native-pipeline-')
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name).resolve()
        source_root = cls.root/'source'; source_root.mkdir(mode=0o700)
        for name in ('ios-signing-owner/main.c', 'ios-signing-owner/ownership.h',
                     'ios-process-guardian/main.c', 'ios-code-verifier/main.c'):
            path = source_root/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(read_resource('native/'+name))
        sdk = '/Applications/Xcode-27.0.0-beta.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX27.0.sdk'
        paths = {}
        for name, source in (('signer','ios-signing-owner/main.c'),('guardian','ios-process-guardian/main.c'),
                             ('verifier','ios-code-verifier/main.c')):
            target = cls.root/name
            result = subprocess.run(['/usr/bin/clang','-std=c11','-fblocks','-Wall','-Wextra','-Werror',
                '-mmacosx-version-min=15.0','-isysroot',sdk,str(source_root/source),'-framework','Security',
                '-framework','CoreFoundation','-o',str(target)],stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
            if result.returncode: raise RuntimeError(result.stderr.decode())
            paths[name] = target
        cls.identity = IOSSigningIdentity('owned-key','ios_app','OWNEDTEAM1',
            (material.certs['signer'], material.certs['root-a']))
        for index, body in enumerate(cls.identity.certificate_chain):
            (cls.root/('chain-'+str(index)+'.der')).write_bytes(body)
        cls.bundle = 'com.example.reproinventory'
        entitlements = {'application-identifier':'OWNEDTEAM1.'+cls.bundle,
                        'com.apple.developer.team-identifier':'OWNEDTEAM1','get-task-allow':False,
                        'keychain-access-groups':['OWNEDTEAM1.'+cls.bundle]}
        now = datetime.now(timezone.utc).replace(microsecond=0)
        profile = {'UUID':secrets.token_hex(16),'Name':'Owned Native Pipeline Profile',
            'CreationDate':(now-timedelta(hours=1)).replace(tzinfo=None),
            'ExpirationDate':(now+timedelta(hours=6)).replace(tzinfo=None),
            'TeamIdentifier':['OWNEDTEAM1'],'ApplicationIdentifierPrefix':['OWNEDTEAM1'],
            'DeveloperCertificates':[material.certs['signer']],'ProvisionedDevices':['OWNED-DEVICE'],
            'Entitlements':entitlements}
        plain = cls.root/'owned-profile.plist'; plain.write_bytes(plistlib.dumps(profile))
        signed = cls.root/'owned-profile.cms'
        material.native('cms','-sign','-binary','-nodetach','-md','sha256','-in',str(plain),
            '-signer',str(material.material_root/'signer.pem'),'-inkey',str(material.material_root/'signer.key'),
            '-certfile',str(material.material_root/'root-a.pem'),'-outform','DER','-out',str(signed))
        cls.definition = IOSSigningDefinition(cls.identity,'owned-profiles',
            {'.':{'bundleId':cls.bundle,'entitlements':entitlements}},
            {'.':{'cms':signed.read_bytes(),'profileDigest':decoded_profile_digest(profile)}})
        cls.provisioning = IOSSigningProvisioning(
            IOSCmsTools(Path('/usr/bin/openssl'),sha(Path('/usr/bin/openssl').read_bytes()),
                sha(Path('/usr/bin/sandbox-exec').read_bytes())),
            IOSCmsTrust('owned-issuer',material.certs['signer'],(material.certs['root-a'],)),
            'OWNEDTEAM1','OWNED-DEVICE')
        cls.tools = IOSSigningOwnerTools(paths['signer'],sha(paths['signer'].read_bytes()),
            paths['verifier'],sha(paths['verifier'].read_bytes()),sha(Path('/usr/bin/sandbox-exec').read_bytes()),
            paths['guardian'],sha(paths['guardian'].read_bytes()))
        cls.password = secrets.token_hex(24).encode()
        reader, writer = os.pipe(); os.write(writer,cls.password); os.close(writer)
        cls.pkcs12 = cls.root/'owned.p12'
        result = subprocess.run(['/usr/bin/openssl','pkcs12','-export','-inkey',str(material.material_root/'signer.key'),
            '-in',str(material.material_root/'signer.pem'),'-out',str(cls.pkcs12),'-passout','fd:'+str(reader)],
            pass_fds=(reader,),env=material.environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
        os.close(reader)
        if result.returncode: raise RuntimeError('owned pkcs12 setup failed')
        cls.pkcs12.chmod(0o600)
        cls.resolver = IOSSigningMaterialResolver()
        cls.resolver.register(cls.identity,pkcs12=cls.pkcs12,password=cls.password)
        cls.addClassCleanup(cls.resolver.close)
        source = Path(__file__).resolve().parents[1]/('artifacts/product-delivery/d1-uikit-r1/'
            'original-release/DerivedData/Build/Products/Release-iphonesimulator/Inventory.app')
        archive = cls.root/'unsigned.ipa'
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as output:
            for path in sorted(source.rglob('*')):
                output.write(path,'Payload/App.app/'+path.relative_to(source).as_posix())
        cls.blobs = BlobSet((('candidate.ipa',archive.read_bytes()),))
        cls.policy = {'schemaVersion':1,'id':'owned-signing','platform':'ios','applicationId':'ios_app',
            'identityReferenceId':'owned-key','entitlementsDigest':cls.definition.entitlements_digest,
            'tool':'host-codesign-fixed','candidateHooks':'forbidden','artifactRelation':'pre-post-digests',
            'provisioningReferenceId':'owned-profiles'}
        cls.store = RunStore(cls.root/'run-store',environment_digest='a'*64,disk_limit=4*1024**3)
        cls.operations = IOSSigningOperationStore(cls.store,cls.tools,cls.definition,cls.root/'operations')
        cls.addClassCleanup(cls.operations.close)
        cls.number = 0

    def setUp(self):
        type(self).number += 1
        self.identifier = 'native-'+str(type(self).number)
        self.request = contracts.digest(self.identifier)
        self.context = SigningContext(self.identifier,'a'*64,'b'*64,'ios_app','c'*64,
            sha(self.blobs.entries[0][1]),contracts.digest(self.policy),secrets.token_hex(24))

    def tearDown(self):
        try: row = self.store.status(self.identifier)
        except Exception: return
        if row['state'] not in TERMINAL:
            with self.operations.recovery(self.identifier,self.request) as capability:
                self.store.finish_signing_recovery(capability,authority=self.operations)

    def test_real_profile_signing_and_independent_inspection_share_one_recoverable_operation(self):
        with self.operations.admit(self.context,self.request) as operation:
            signed = self.operations.sign(operation,self.blobs,material_resolver=self.resolver,
                provisioning=self.provisioning,policy_document=self.policy,cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+60)
            self.assertIs(type(signed),SigningObservation)
            self.assertTrue(signed.cleanup_confirmed)
            self.assertNotEqual(signed.artifacts.digest,self.blobs.digest)
            context = replace(self.context,nonce=secrets.token_hex(24),
                signed_artifact_digest=sha(signed.artifacts.entries[0][1]))
            inspected = self.operations.inspect(operation,context,signed.artifacts,provisioning=self.provisioning,
                policy_document=self.policy,cancellation=threading.Event(),deadline_monotonic=time.monotonic()+60)
            self.assertIs(type(inspected),SignatureObservation)
            self.assertTrue(inspected.valid and inspected.cleanup_confirmed)
            operation.run.finish('succeeded',stopped=True)
        self.assertEqual(self.store.status(self.identifier)['reservedBytes'],0)

    def test_wrong_password_is_a_clean_failure_after_real_profile_verification(self):
        resolver = IOSSigningMaterialResolver()
        self.addCleanup(resolver.close)
        resolver.register(self.identity,pkcs12=self.pkcs12,password=b'owned incorrect password')
        with self.operations.admit(self.context,self.request) as operation:
            result = self.operations.sign(operation,self.blobs,material_resolver=resolver,
                provisioning=self.provisioning,policy_document=self.policy,cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+60)
            self.assertIs(type(result),SigningFailureObservation)
            self.assertTrue(result.termination_confirmed and result.cleanup_confirmed)
            self.assertEqual(result.code,'signing_failed')
            operation.run.finish('failed',stopped=True)
        self.assertEqual(self.store.status(self.identifier)['reservedBytes'],0)

    def test_prepared_app_change_after_profile_verification_cannot_reach_the_key(self):
        from reproloop import ios_signing_execution as execution
        original = execution._verify_profiles
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            path = args[1].root/'App.app'/'Info.plist'
            info = plistlib.loads(path.read_bytes()); info['CFBundleVersion'] = 'owned-unapproved-change'
            path.write_bytes(plistlib.dumps(info))
            return result
        with self.operations.admit(self.context,self.request) as operation:
            with mock.patch.object(execution,'_verify_profiles',side_effect=changed), \
                    mock.patch.object(self.resolver,'open',side_effect=AssertionError('key must remain unopened')) as opened:
                result = self.operations.sign(operation,self.blobs,material_resolver=self.resolver,
                    provisioning=self.provisioning,policy_document=self.policy,cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic()+60)
                self.assertIs(type(result),SigningFailureObservation)
                opened.assert_not_called()
            operation.run.finish('failed',stopped=True)

    def test_candidate_resource_rule_override_is_rejected_before_key_use(self):
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.blobs.entries[0][1]),'r') as source, \
                zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                body = source.read(info)
                if info.filename == 'Payload/App.app/Info.plist':
                    value = plistlib.loads(body); value['CFBundleResourceSpecification'] = 'OwnedRules.plist'
                    body = plistlib.dumps(value)
                target.writestr(info,body)
            info = zipfile.ZipInfo('Payload/App.app/OwnedRules.plist')
            info.create_system = 3; info.external_attr = (stat.S_IFREG | 0o600) << 16
            target.writestr(info,plistlib.dumps({'rules':{'.*':True}}))
        changed = BlobSet((('candidate.ipa',output.getvalue()),))
        self.context = replace(self.context,unsigned_artifact_digest=sha(changed.entries[0][1]))
        with self.operations.admit(self.context,self.request) as operation:
            with mock.patch.object(self.resolver,'open',side_effect=AssertionError('key must remain unopened')) as opened:
                result = self.operations.sign(operation,changed,material_resolver=self.resolver,
                    provisioning=self.provisioning,policy_document=self.policy,cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic()+60)
                self.assertIs(type(result),SigningFailureObservation)
                opened.assert_not_called()
            operation.run.finish('failed',stopped=True)

    def test_tampered_signed_container_is_not_an_independent_inspection_success(self):
        with self.operations.admit(self.context,self.request) as operation:
            signed = self.operations.sign(operation,self.blobs,material_resolver=self.resolver,
                provisioning=self.provisioning,policy_document=self.policy,cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+60)
            self.assertIs(type(signed),SigningObservation)
            context = replace(self.context,nonce=secrets.token_hex(24),signed_artifact_digest=sha(signed.artifacts.entries[0][1]))
            changed = BlobSet((('candidate.ipa',signed.artifacts.entries[0][1]+b'owned tamper'),))
            result = self.operations.inspect(operation,context,changed,provisioning=self.provisioning,
                policy_document=self.policy,cancellation=threading.Event(),deadline_monotonic=time.monotonic()+60)
            self.assertIs(type(result),SigningFailureObservation)
            self.assertTrue(result.cleanup_confirmed)
            operation.run.finish('failed',stopped=True)
        self.assertEqual(self.store.status(self.identifier)['reservedBytes'],0)

    def test_changed_provisioning_configuration_cannot_adopt_a_signed_phase(self):
        with self.operations.admit(self.context,self.request) as operation:
            signed = self.operations.sign(operation,self.blobs,material_resolver=self.resolver,
                provisioning=self.provisioning,policy_document=self.policy,cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+60)
            self.assertIs(type(signed),SigningObservation)
            context = replace(self.context,nonce=secrets.token_hex(24),signed_artifact_digest=sha(signed.artifacts.entries[0][1]))
            changed = replace(self.provisioning,selected_device='OTHER-OWNED-DEVICE')
            with self.assertRaises(Exception):
                self.operations.inspect(operation,context,signed.artifacts,provisioning=changed,
                    policy_document=self.policy,cancellation=threading.Event(),deadline_monotonic=time.monotonic()+60)

    def test_cancelled_before_key_dispatch_has_no_signature_and_preserves_cleanup(self):
        cancelled = threading.Event(); cancelled.set()
        with self.operations.admit(self.context,self.request) as operation:
            with self.assertRaises(Exception):
                self.operations.sign(operation,self.blobs,material_resolver=self.resolver,
                    provisioning=self.provisioning,policy_document=self.policy,cancellation=cancelled,
                    deadline_monotonic=time.monotonic()+60)
        with self.operations.recovery(self.identifier,self.request) as capability:
            result = self.store.finish_signing_recovery(capability,authority=self.operations)
        self.assertEqual(result['reservedBytes'],0)

    def test_service_composition_uses_the_fixed_signer_and_inspector_under_the_supervisor(self):
        from tests import test_repair_execution as build_fixture
        from reproloop.execution.artifacts import ArtifactValidationAuthority
        from reproloop.execution.wire import MAX_TRANSFER_BYTES
        from reproloop.repair_composition import ProtectedRepairComposition
        fixture = build_fixture.ProtectedRepairBuildTests(methodName='runTest')
        original_inputs = build_fixture.resource_inputs
        def resources(root):
            metadata, paths = original_inputs(root)
            metadata['catalog'][0].update(outputPaths=['candidate.ipa'], maxOutputBytes=MAX_TRANSFER_BYTES)
            return metadata, paths
        with mock.patch.object(build_fixture, 'resource_inputs', side_effect=resources):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.artifacts = ArtifactValidationAuthority()
        fixture.artifacts.register('bounded-artifacts', paths=('candidate.ipa',), max_bytes=MAX_TRANSFER_BYTES,
            checker=lambda value: value.digest == self.blobs.digest)
        builder = fixture.supervisor()
        # Only the VM/build transport is a declared double. Signing, CMS and
        # independent code inspection below use the real fixed native tools.
        with mock.patch('tests.test_execution_runtime.BlobSet', return_value=self.blobs):
            build = fixture.build(builder, operation='owned-native-build')
        resolver = IOSSigningMaterialResolver()
        resolver.register(self.identity, pkcs12=self.pkcs12, password=self.password)
        self.addCleanup(resolver.close)
        composition = ProtectedRepairComposition(authority=fixture.authority)
        self.addCleanup(composition.close)
        supervisor = composition.configure_ios_signing_owner(builder, tools=self.tools, material_resolver=resolver,
            definition=self.definition, provisioning=self.provisioning, policy_document=self.policy,
            store=self.store, work_root=self.root/'operations', timeout_seconds=60)
        try:
            result = supervisor.sign(build, operation_id=self.identifier, cancellation=threading.Event())
            self.assertEqual(result.public()['applicationId'], 'ios_app')
            self.assertFalse(result.public()['verified'])
            self.assertEqual(self.store.status(self.identifier)['state'], 'succeeded')
        finally:
            try: self.request = self.store.status(self.identifier)['requestDigest']
            except Exception: pass

    def test_interrupted_factory_closes_partial_ios_signing_owners(self):
        from tests import test_repair_execution as build_fixture
        from tests.signing_factory_support import check_interrupted_factory
        from reproloop.repair_composition import ProtectedRepairComposition
        from reproloop.repair_ios_signing_owner import IOSSigningOwnerSigner, IOSSigningOwnerInspector
        fixture = build_fixture.ProtectedRepairBuildTests(methodName='runTest')
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        builder = fixture.supervisor()
        resolver = IOSSigningMaterialResolver()
        self.addCleanup(resolver.close)
        resolver.register(self.identity, pkcs12=self.pkcs12, password=self.password)
        composition = ProtectedRepairComposition(authority=fixture.authority)
        self.addCleanup(composition.close)

        def configure():
            return composition.configure_ios_signing_owner(builder, tools=self.tools,
                material_resolver=resolver, definition=self.definition, provisioning=self.provisioning,
                policy_document=self.policy, store=self.store, work_root=self.root/'operations')

        check_interrupted_factory(self, configure, owner=composition,
            operations_type=IOSSigningOperationStore, signer_type=IOSSigningOwnerSigner,
            inspector_type=IOSSigningOwnerInspector, resolver=resolver)

    def test_real_parent_death_during_signing_recovers_in_a_new_python_process(self):
        command = [sys.executable,'-m','tests.fixtures.ios_native_pipeline_child',str(self.root),
                   self.identifier,self.request]
        reader, writer = os.pipe(); os.write(writer,self.password); os.close(writer)
        try:
            result = subprocess.run([*command,'crash-sign',str(reader)],pass_fds=(reader,),
                cwd=Path(__file__).resolve().parents[1],stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        finally: os.close(reader)
        self.assertEqual(result.returncode,73)
        self.assertGreater(self.store.status(self.identifier)['reservedBytes'],0)
        root = self.operations.operation_root(self.identifier)
        self.assertGreater((root/'start.json').stat().st_size,0)
        recovered = subprocess.run([*command,'recover'],cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
        self.assertEqual(recovered.returncode,0)
        self.assertEqual(json.loads(recovered.stdout),{'state':'failed','reservedBytes':0})
        self.assertFalse((root/'App.app').exists())
        self.assertEqual(list((root/'checks').iterdir()),[])

    def test_public_cli_recovers_native_parent_death_with_material_and_tools_unavailable(self):
        reference = self.root/(self.identifier+'-recovery.json')
        reference.write_text(json.dumps(self.operations.recovery_configuration())); reference.chmod(0o600)
        reader,writer = os.pipe(); os.write(writer,self.password); os.close(writer)
        checkout = Path(__file__).resolve().parents[1]
        try:
            result = subprocess.run([sys.executable,'-m','tests.fixtures.ios_native_pipeline_child',
                str(self.root),self.identifier,self.request,'crash-sign',str(reader)],pass_fds=(reader,),
                cwd=checkout,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        finally: os.close(reader)
        self.assertEqual(result.returncode,73)
        operation_root = self.operations.operation_root(self.identifier)
        self.assertGreater((operation_root/'start.json').stat().st_size,0)
        self.assertGreater(self.store.status(self.identifier)['reservedBytes'],0)
        unavailable = [self.root/name for name in ('owned.p12','owned-profile.plist','owned-profile.cms',
            'chain-0.der','chain-1.der','signer','guardian','verifier')]
        sandbox = '(version 1)(allow default)(deny file-read-data '+''.join(
            '(literal '+json.dumps(str(path))+')' for path in unavailable)+')'
        prefix = ['/usr/bin/sandbox-exec','-p',sandbox]
        probe = subprocess.run([*prefix,sys.executable,'-I','-c',
            'import sys\nfor path in sys.argv[1:]:\n'
            ' try:\n  with open(path,"rb") as stream: stream.read(1)\n'
            ' except PermissionError: continue\n'
            ' raise SystemExit(1)\n',*map(str,unavailable)],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        self.assertEqual((probe.returncode,probe.stdout,probe.stderr),(0,b'',b''))
        def command(action, *extra):
            ran = subprocess.run([*prefix,sys.executable,'-m','reproloop','ios-signing',action,
                '--config',str(reference),'--operation',self.identifier,*extra],cwd=checkout,
                stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
            self.assertEqual(ran.stderr,b'')
            return ran.returncode,json.loads(ran.stdout)
        code,observed = command('status')
        self.assertEqual(code,0); self.assertEqual(observed['status'],'observed')
        deadline = time.monotonic()+5
        while True:
            code,report = command('recover','--request-digest',self.request)
            if code == 0 or time.monotonic() >= deadline: break
            time.sleep(.02)
        self.assertEqual(code,0)
        self.assertEqual((report['status'],report['state'],report['reservedBytes']),('recovered','failed',0))
        code,report = command('recover','--request-digest',self.request)
        self.assertEqual(code,0); self.assertEqual(report['status'],'already-terminal')
        self.assertFalse((operation_root/'App.app').exists())
        self.assertEqual(list((operation_root/'checks').iterdir()),[])
