"""Actual JVM signing/recovery through a supervisor with an explicit VM double."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from reproof import contracts
from reproof.execution.artifacts import ArtifactValidationAuthority, BlobSet
from reproof.execution.guest import serve_one
from reproof.execution.journal import RunDenied, RunStore
from reproof.execution.resources import provision
from reproof.execution.wire import MAX_TRANSFER_BYTES, accept_bootstrap
from reproof.repair_android_signing import AndroidSigningIdentity, AndroidSigningMaterialResolver
from reproof.repair_composition import ProtectedRepairComposition
from reproof.repair_execution import RepairExecutionError
from reproof.repair_signing import SigningContext
from reproof.repair_signing_recovery import MIN_OPERATION_BYTES, SigningOperationStore, SigningOwnerTools
from tests import test_android_signing_owner as native
from tests.test_execution_protocol import build_route, validation_plan
from tests.test_execution_qualification import ProbeVMDouble
from tests.test_execution_resources import resource_inputs


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class AndroidSigningOwnerCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        native.AndroidSigningOwnerTests.setUpClass()
        cls.native = native.AndroidSigningOwnerTests
        cls.tools = SigningOwnerTools(cls.native.java, sha(cls.native.java),
            cls.native.owner_jar, sha(cls.native.owner_jar),
            cls.native.native/'libreproof_signing_owner_fd.dylib',
            sha(cls.native.native/'libreproof_signing_owner_fd.dylib'),
            native.APKSIGNER_JAR, sha(native.APKSIGNER_JAR))

    @classmethod
    def tearDownClass(cls): native.AndroidSigningOwnerTests.tearDownClass()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.apk = native.PUBLIC_APK.read_bytes()
        self.composition = ProtectedRepairComposition(); self.addCleanup(self.composition.close)
        metadata, paths = resource_inputs(self.root)
        metadata['catalog'][0].update(outputPaths=['candidate.apk'], maxOutputBytes=MAX_TRANSFER_BYTES)
        bundle = provision(self.root/'bundle', metadata=metadata, resources=paths)
        store = RunStore(self.root/'vm-state', environment_digest=bundle.environment_digest, disk_limit=1024)
        route = build_route(); route.update(environmentDigest=bundle.environment_digest,
                                            recipeId='build', cleanupPolicyId='dispose-overlay')
        unsigned = ArtifactValidationAuthority()
        unsigned.register('bounded-artifacts', paths=('candidate.apk',), max_bytes=MAX_TRANSFER_BYTES,
                          checker=lambda blobs: blobs.entries == (('candidate.apk', self.apk),))
        with mock.patch('reproof.execution.qualification.NativeVM', ProbeVMDouble), \
                mock.patch.object(ProbeVMDouble, 'network_denied', True), \
                mock.patch.object(ProbeVMDouble, 'stop_confirmed', True), \
                mock.patch.object(ProbeVMDouble, 'bounded_output_denied', True):
            self.builder = self.composition.qualify_build(bundle=bundle, store=store, route=route,
                validation_plan=validation_plan(), artifact_authority=unsigned, application_id='inventory_app')
        # Each test owns a fresh key/certificate so a canonical key scope never
        # moves to a different RunStore root between tests.
        password = secrets.token_hex(18).encode()
        key = self.root/'owned-test.p12'
        variable = 'REPROOF_D4_COMPOSITION_TEST_PASSWORD'
        env = {'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C',variable:password.decode()}
        result = subprocess.run([str(self.native.keytool), '-genkeypair', '-storetype', 'PKCS12',
            '-keystore',str(key),'-storepass:env',variable,'-keypass:env',variable,'-alias','owned-test',
            '-keyalg','RSA','-keysize','2048','-validity','1','-dname','CN=Owned Composition Test'],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        self.assertEqual(result.returncode,0); key.chmod(0o600)
        result = subprocess.run([str(self.native.keytool),'-exportcert','-keystore',str(key),
            '-storepass:env',variable,'-alias','owned-test'], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        self.assertEqual(result.returncode,0)
        self.identity = AndroidSigningIdentity('owned_signing','inventory_app','com.example.reproinventory',
            hashlib.sha256(result.stdout).hexdigest(),('v2','v3'),())
        self.policy = {'schemaVersion':1,'id':'owned_android_signing','platform':'android',
            'applicationId':'inventory_app','identityReferenceId':self.identity.reference_id,
            'entitlementsDigest':self.identity.signing_configuration_digest,'tool':'host-apksigner-fixed',
            'candidateHooks':'forbidden','artifactRelation':'pre-post-digests'}
        self.resolver = AndroidSigningMaterialResolver(); self.addCleanup(self.resolver.close)
        self.resolver.register(self.identity,keystore=key,key_alias='owned-test',
                               store_password=password,key_password=password)
        self.store = RunStore(self.root/'signing-state',environment_digest=contracts.digest(str(self.root)),
                              disk_limit=2*MIN_OPERATION_BYTES)

    def configure(self, **changes):
        values = dict(tools=self.tools, material_resolver=self.resolver, identity=self.identity,
            policy_document=self.policy, store=self.store, work_root=self.root/'signing', timeout_seconds=30)
        return self.composition.configure_android_signing_owner(self.builder, **{**values,**changes})

    def build(self):
        body = self.apk
        class ApkVMDouble(ProbeVMDouble):
            def __init__(self, bundle, run, *, deadline, cancel):
                self.channel,self.peer=socket.socketpair()
                class ExecutorDouble:
                    def execute(self,source,recipe,cancelled):
                        return ({'exitCode':0,'outputTruncated':False,'logDigest':'c'*64},
                                BlobSet((('candidate.apk',body),)))
                def serve():
                    channel=accept_bootstrap(self.peer,deadline=deadline)
                    serve_one(channel,catalog=bundle.metadata['catalog'],agent_digest=bundle.metadata['agentDigest'],
                              executor=ExecutorDouble())
                self.thread=threading.Thread(target=serve);self.thread.start()
        self.source=BlobSet((('src/example.kt',b'explicit VM boundary fixture'),))
        with mock.patch('reproof.execution.runtime.NativeVM',ApkVMDouble):
            return self.builder.build(self.source,operation_id='owned_build',repair_plan_digest='b'*64,
                                      cancellation=threading.Event())

    def test_actual_owner_sign_and_independent_inspect_use_supervisor_callback_threads(self):
        supervisor=self.configure(); build=self.build()
        signed=supervisor.sign(build,operation_id='owned_sign',cancellation=threading.Event())
        self.assertIs(supervisor.require_signed(signed,source_digest=self.source.digest,repair_plan_digest='b'*64),signed)
        self.assertFalse(signed.public()['verified'])
        self.assertNotEqual(signed.artifact_digest,build.artifact_digest)
        self.assertEqual(self.store.status('owned_sign')['state'],'succeeded')
        self.assertEqual(self.store.status('owned_sign')['reservedBytes'],0)
        states=supervisor.operations._state('owned_sign')['phases']
        self.assertEqual(set(states),{'sign','inspect'})
        self.assertTrue(all(phase['state']=='cleaned' for phase in states.values()))
        self.assertEqual(states['sign']['outputDigest'],states['inspect']['inputDigest'])
        self.composition.close()
        self.assertEqual(supervisor.operations.active_processes,0)
        with self.assertRaises(Exception):self.resolver.open(self.identity)

    def test_same_certificate_cannot_move_to_a_new_journal_or_alias(self):
        self.configure()
        alias=replace(self.identity,reference_id='other_signing')
        other=RunStore(self.root/'other-state',environment_digest='e'*64,disk_limit=2*MIN_OPERATION_BYTES)
        with self.assertRaises(RepairExecutionError):
            self.configure(identity=alias,policy_document={**self.policy,'id':'other_policy',
                'identityReferenceId':'other_signing'},store=other,work_root=self.root/'other-signing')
        self.assertEqual(self.composition.status()['cleanupPending'],1)

    def test_disk_reservation_covers_native_input_output_and_journals(self):
        supervisor=self.configure(); build=self.build()
        seen=[]
        admit=self.store.admit
        def capture(*args,**kwargs):
            seen.append(kwargs['disk_bytes']);return admit(*args,**kwargs)
        with mock.patch.object(self.store,'admit',side_effect=capture):
            supervisor.sign(build,operation_id='owned_sign',cancellation=threading.Event())
        self.assertEqual(seen,[MIN_OPERATION_BYTES])

    def test_interrupted_factory_closes_partial_android_signing_owners(self):
        from tests.signing_factory_support import check_interrupted_factory
        from reproof.repair_android_signing_owner import AndroidSigningOwnerSigner, AndroidSigningOwnerInspector
        check_interrupted_factory(self, self.configure, owner=self.composition,
            operations_type=SigningOperationStore, signer_type=AndroidSigningOwnerSigner,
            inspector_type=AndroidSigningOwnerInspector, resolver=self.resolver)

    def test_context_runtime_binding_is_not_serialized_or_hashed(self):
        context=SigningContext('op','a'*64,'b'*64,'inventory_app','c'*64,'d'*64,'e'*64,'nonce')
        bound=replace(context,_operation_binding=object())
        self.assertEqual(context.digest,bound.digest)
        self.assertNotIn('_operation_binding',repr(bound))

    def test_signer_rejects_an_unbound_context_before_opening_key_material(self):
        supervisor=self.configure()
        context=SigningContext('op','a'*64,self.builder.route.project_digest,'inventory_app',
            'c'*64,sha(native.PUBLIC_APK),supervisor.policy.definition_digest,'nonce')
        with mock.patch.object(self.resolver,'open',side_effect=AssertionError('must not open a key')):
            with self.assertRaises(RepairExecutionError):
                supervisor.signer(context,BlobSet((('candidate.apk',self.apk),)),
                    cancellation=threading.Event(),deadline_monotonic=__import__('time').monotonic()+10)

    def test_composition_retains_material_until_a_pre_dispatch_callback_returns(self):
        supervisor=self.configure()
        context=SigningContext('held_sign','a'*64,self.builder.route.project_digest,'inventory_app',
            'c'*64,sha(native.PUBLIC_APK),supervisor.policy.definition_digest,'nonce')
        request=contracts.digest({'context':context.digest})
        entered,release=threading.Event(),threading.Event()
        opened=[]; failures=[]
        original_open=self.resolver.open
        def held_open(identity):
            material=original_open(identity)
            opened.append(material); entered.set(); release.wait(3)
            return material
        with supervisor.operations.admit(context,request,MIN_OPERATION_BYTES) as operation:
            bound=replace(context,_operation_binding=operation)
            def sign():
                try:
                    supervisor.signer(bound,BlobSet((('candidate.apk',self.apk),)),
                        cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10)
                except Exception as error: failures.append(type(error).__name__)
            with mock.patch.object(self.resolver,'open',side_effect=held_open):
                worker=threading.Thread(target=sign);worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    with self.assertRaises(RepairExecutionError): self.composition.close(timeout_seconds=.03)
                    self.assertEqual(self.composition.status()['cleanupPending'],1)
                    self.assertIsNotNone(opened[0].descriptor)
                    still_registered=original_open(self.identity);still_registered.close()
                finally:
                    release.set();worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(failures),1)
            self.assertIsNone(opened[0].descriptor)
        with supervisor.operations.recovery(context.operation_id,request) as capability:
            self.store.finish_signing_recovery(capability,authority=supervisor.operations)
        self.composition.close()
        self.assertEqual(self.composition.status()['cleanupPending'],0)
        with self.assertRaises(Exception): original_open(self.identity)


if __name__=='__main__':unittest.main()
