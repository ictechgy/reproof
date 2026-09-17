"""Real supervisor composition with explicit VM and cryptographic-tool doubles."""
import hashlib
import copy
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

from reproloop import contracts
from reproloop.execution.artifacts import ArtifactValidationAuthority, BlobSet
from reproloop.execution.backend import QualificationAuthority
from reproloop.execution.guest import serve_one
from reproloop.execution.journal import RunStore
from reproloop.execution.resources import provision
from reproloop.execution.wire import accept_bootstrap
from reproloop.repair_android_signing import (
    AndroidApkInspector, AndroidApkSigner, AndroidSigningError, AndroidSigningIdentity,
    AndroidSigningMaterialResolver, AndroidSigningTools,
)
from reproloop.repair_composition import ProtectedRepairComposition
from reproloop.repair_execution import RepairExecutionError
from tests.test_execution_protocol import build_route, validation_plan
from tests.test_execution_qualification import ProbeVMDouble
from tests.test_execution_resources import resource_inputs
from tests.test_repair_android_signing import _aapt_script, _apksigner_script, _digest, _fake_apk, _write_executable


class AndroidSigningCompositionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(); self.apk = _fake_apk()
        certificate = hashlib.sha256(str(self.root).encode()).hexdigest()
        java = _write_executable(self.root / 'java', _apksigner_script(certificate))
        jar = self.root / 'apksigner.jar'; jar.write_bytes(b'explicit crypto-tool double')
        aapt = _write_executable(self.root / 'aapt2', _aapt_script())
        self.tools = AndroidSigningTools(java, _digest(java), jar, _digest(jar), aapt, _digest(aapt))
        self.identity = AndroidSigningIdentity('owned-signing', 'android_app', 'com.example.product',
                                               certificate, ('v2', 'v3'), ())
        self.policy = {'schemaVersion': 1, 'id': 'owned-android-signing', 'platform': 'android',
            'applicationId': 'android_app', 'identityReferenceId': self.identity.reference_id,
            'entitlementsDigest': self.identity.signing_configuration_digest, 'tool': 'host-apksigner-fixed',
            'candidateHooks': 'forbidden', 'artifactRelation': 'pre-post-digests'}
        material = self.root / 'owned-test.p12'; material.write_bytes(b'owned test material'); material.chmod(0o600)
        self.resolver = AndroidSigningMaterialResolver(); self.addCleanup(self.resolver.close)
        self.resolver.register(self.identity, keystore=material, key_alias='owned-test',
            store_password=b'store-secret', key_password=b'key-secret')
        self.composition = ProtectedRepairComposition(); self.addCleanup(self.composition.close)
        metadata, paths = resource_inputs(self.root)
        metadata['catalog'][0]['outputPaths'] = ['candidate.apk']
        bundle = provision(self.root / 'bundle', metadata=metadata, resources=paths)
        store = RunStore(self.root / 'vm-state', environment_digest=bundle.environment_digest, disk_limit=1024)
        route = build_route(); route.update(environmentDigest=bundle.environment_digest,
                                            recipeId='build', cleanupPolicyId='dispose-overlay')
        unsigned = ArtifactValidationAuthority()
        unsigned.register('bounded-artifacts', paths=('candidate.apk',), max_bytes=4096,
                          checker=lambda blobs: blobs.entries == (('candidate.apk', self.apk),))
        with mock.patch('reproloop.execution.qualification.NativeVM', ProbeVMDouble), \
                mock.patch.object(ProbeVMDouble, 'network_denied', True), \
                mock.patch.object(ProbeVMDouble, 'stop_confirmed', True), \
                mock.patch.object(ProbeVMDouble, 'bounded_output_denied', True):
            self.builder = self.composition.qualify_build(bundle=bundle, store=store, route=route,
                validation_plan=validation_plan(), artifact_authority=unsigned, application_id='android_app')
        self.signing_store = RunStore(self.root / 'signing-state',
            environment_digest=contracts.digest({'test': str(self.root)}), disk_limit=4096)

    def configure(self, **changes):
        builder = changes.pop('builder', self.builder)
        arguments = dict(tools=self.tools, material_resolver=self.resolver, identity=self.identity,
            policy_document=self.policy, store=self.signing_store, work_root=self.root / 'signing',
            artifact_policy_id='signed-apk', max_apk_bytes=4096, timeout_seconds=3)
        return self.composition.configure_android_signing(builder, **{**arguments, **changes})

    def build(self):
        body = self.apk
        class ApkVMDouble(ProbeVMDouble):
            def __init__(self, bundle, run, *, deadline, cancel):
                self.channel, self.peer = socket.socketpair()
                class ExecutorDouble:
                    def execute(self, source, recipe, cancelled):
                        return ({'exitCode': 0, 'outputTruncated': False, 'logDigest': 'c' * 64},
                                BlobSet((('candidate.apk', body),)))
                def serve():
                    channel = accept_bootstrap(self.peer, deadline=deadline)
                    serve_one(channel, catalog=bundle.metadata['catalog'], agent_digest=bundle.metadata['agentDigest'],
                              executor=ExecutorDouble())
                self.thread = threading.Thread(target=serve); self.thread.start()
        self.source = BlobSet((('src/example.kt', b'explicit VM output fixture'),))
        with mock.patch('reproloop.execution.runtime.NativeVM', ApkVMDouble):
            return self.builder.build(self.source, operation_id='owned_build', repair_plan_digest='b' * 64,
                                      cancellation=threading.Event())

    def test_fixed_factory_signs_a_bound_build_and_composition_owns_teardown(self):
        supervisor = self.configure(); built = self.build()
        self.assertEqual(self.composition.status()['cleanupPending'], 1)
        signed = supervisor.sign(built, operation_id='owned_sign', cancellation=threading.Event())
        self.assertIs(supervisor.require_signed(signed, source_digest=self.source.digest,
            repair_plan_digest='b' * 64), signed)
        self.assertNotEqual(signed.artifact_digest, built.artifact_digest)
        self.assertEqual(signed.public()['unsignedArtifactDigest'], built.artifact_digest)
        self.assertFalse(signed.public()['verified'])
        self.assertEqual(self.signing_store.status('owned_sign')['state'], 'succeeded')
        self.composition.close()
        with self.assertRaises(AndroidSigningError): self.resolver.open(self.identity)
        with self.assertRaises(RepairExecutionError):
            supervisor.require_signed(signed, source_digest=self.source.digest, repair_plan_digest='b' * 64)

    def test_identity_alias_cannot_move_the_same_signing_key_to_another_journal(self):
        first = self.configure(); first.ready()
        alias = AndroidSigningIdentity('other-reference', self.identity.application_id,
            self.identity.package_name, self.identity.certificate_sha256, ('v2', 'v3'), ())
        self.resolver.register(alias, keystore=self.root / 'owned-test.p12', key_alias='owned-test',
            store_password=b'store-secret', key_password=b'key-secret')
        policy = {**self.policy, 'id': 'other-policy', 'identityReferenceId': alias.reference_id}
        other_store = RunStore(self.root / 'other-state', environment_digest='e' * 64, disk_limit=4096)
        with self.assertRaises(RepairExecutionError):
            self.configure(identity=alias, policy_document=policy, store=other_store,
                           work_root=self.root / 'other-work')
        self.assertEqual(self.composition.status()['cleanupPending'], 1)

    def test_invalid_artifact_policy_closes_unadopted_pair_without_erasing_caller_material(self):
        sign_close, inspect_close = AndroidApkSigner.close, AndroidApkInspector.close
        with mock.patch.object(AndroidApkSigner, 'close', autospec=True, side_effect=sign_close) as close_signer, \
                mock.patch.object(AndroidApkInspector, 'close', autospec=True, side_effect=inspect_close) as close_inspector:
            with self.assertRaises(RepairExecutionError): self.configure(artifact_policy_id='INVALID')
            close_signer.assert_called_once(); close_inspector.assert_called_once()
        self.assertEqual(self.composition.status()['cleanupPending'], 0)
        opened = self.resolver.open(self.identity); opened.close()

    def test_foreign_builder_and_invalid_policy_cannot_allocate_owned_signing_resources(self):
        foreign = copy.copy(self.builder); foreign.backend = copy.copy(self.builder.backend)
        foreign.backend.authority = QualificationAuthority()
        with self.assertRaises(RepairExecutionError): self.configure(builder=foreign)
        with self.assertRaises(RepairExecutionError): self.configure(policy_document={**self.policy, 'qualified': True})
        self.assertEqual(self.composition.status()['cleanupPending'], 0)
        self.composition.close()
        with self.assertRaises(RepairExecutionError): self.configure()


if __name__ == '__main__': unittest.main()
