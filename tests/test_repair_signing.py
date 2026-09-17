"""Fixed signing and independent inspection never trust a candidate receipt."""
from dataclasses import replace
import threading
import unittest

from reproloop import contracts
from reproloop.execution.artifacts import ArtifactValidationAuthority, BlobSet
from reproloop.execution.journal import RunStore
from tests import test_repair_execution as build_tests


class ProtectedSigningTests(unittest.TestCase):
    setUp = build_tests.ProtectedRepairBuildTests.setUp

    def signing(self, *, signer=None, inspector=None, timeout=.5, store=None):
        from reproloop.repair_signing import (TrustedSigningSupervisor, SigningObservation,
                                             SignatureObservation)
        self.policy_document = {'schemaVersion': 1, 'id': 'test-signing', 'platform': 'ios',
            'applicationId': 'ios_app', 'identityReferenceId': 'synthetic-identity',
            'entitlementsDigest': 'e' * 64, 'provisioningReferenceId': 'synthetic-profile',
            'tool': 'host-codesign-fixed', 'candidateHooks': 'forbidden',
            'artifactRelation': 'pre-post-digests'}
        policy = self.authority.register_signing_policy(self.policy_document)
        self.signed_blobs = BlobSet((('product.bin', b'candidate artifact signed'),))
        def sign(context, artifacts, *, cancellation, deadline_monotonic):
            return SigningObservation(context.digest, self.signed_blobs,
                                      contracts.digest('test signature'), True, True)
        def inspect(context, artifacts, *, cancellation, deadline_monotonic):
            return SignatureObservation(context.digest, artifacts == self.signed_blobs, 'a' * 64, True, True)
        artifact_authority = ArtifactValidationAuthority()
        artifact_authority.register('bounded-artifacts', paths=('product.bin',), max_bytes=4096,
                                    checker=lambda blobs: blobs == self.signed_blobs)
        scope = contracts.digest({'testSigningScope': str(self.root)})
        self.signing_store = store or RunStore(self.root / 'signing-state',
            environment_digest=scope, disk_limit=1)
        if not hasattr(self, 'builder'):
            self.builder = build_tests.ProtectedRepairBuildTests.supervisor(self)
        return TrustedSigningSupervisor(self.builder, authority=self.authority, policy=policy,
            policy_document=self.policy_document, signer=signer or sign, inspector=inspector or inspect,
            artifact_authority=artifact_authority, artifact_policy_id='bounded-artifacts',
            store=self.signing_store, scope_digest=scope, timeout_seconds=timeout)

    def signed(self, supervisor, *, cancellation=None, operation='candidate_sign'):
        proof = build_tests.ProtectedRepairBuildTests.build(self, self.builder)
        self.build_proof = proof
        return supervisor.sign(proof, operation_id=operation,
            cancellation=cancellation or threading.Event())

    def test_signed_proof_binds_both_artifacts_and_independent_inspection(self):
        from reproloop.repair_execution import RepairExecutionError
        signer = self.signing(); signed = self.signed(signer)
        result = signer.require_signed(signed, source_digest=self.source.digest, repair_plan_digest='b' * 64)
        self.assertFalse(result.public()['verified'])
        self.assertNotEqual(result.artifact_digest, self.build_proof.artifact_digest)
        self.assertEqual(result.public()['unsignedArtifactDigest'], self.build_proof.artifact_digest)
        self.assertEqual(self.signing_store.status('candidate_sign')['state'], 'succeeded')
        for forged in (result.public(), replace(result), replace(result, artifact_digest='f' * 64)):
            with self.subTest(forged=type(forged)), self.assertRaises(RepairExecutionError):
                signer.require_signed(forged, source_digest=self.source.digest, repair_plan_digest='b' * 64)

    def test_false_or_json_signature_cannot_authorize_installation(self):
        from reproloop.repair_signing import SignatureObservation
        from reproloop.repair_execution import RepairExecutionError
        signer = self.signing(inspector=lambda context, artifacts, **kw:
            SignatureObservation(context.digest, False, 'a' * 64, True, True))
        with self.assertRaises(RepairExecutionError) as caught: self.signed(signer)
        self.assertEqual(caught.exception.code, 'signature_invalid')
        self.assertEqual(self.signing_store.status('candidate_sign')['state'], 'failed')

    def test_foreign_policy_and_untrusted_build_are_rejected_before_signing(self):
        from reproloop.repair_execution import RepairExecutionError
        signer = self.signing(signer=lambda *args, **kw: self.fail('untrusted build was dispatched'))
        with self.assertRaises(RepairExecutionError):
            signer.sign({'verified': True}, operation_id='forged_build', cancellation=threading.Event())
        self.assertFalse((self.signing_store.root / 'runs' / 'forged_build').exists())

    def test_unknown_signer_cleanup_quarantines_across_restart(self):
        from reproloop.repair_signing import SigningObservation
        from reproloop.repair_execution import RepairExecutionError
        signer = self.signing(signer=lambda context, artifacts, **kw:
            SigningObservation(context.digest, artifacts, 'a' * 64, True, False))
        with self.assertRaises(RepairExecutionError) as caught: self.signed(signer)
        self.assertEqual(caught.exception.code, 'signing_quarantined')
        restarted = RunStore(self.signing_store.root, environment_digest=self.signing_store.environment_digest, disk_limit=1)
        self.assertEqual(restarted.status('candidate_sign')['state'], 'quarantined')
        replacement = self.signing(store=restarted)
        with self.assertRaises(RepairExecutionError):
            replacement.sign(self.build_proof, operation_id='retry_sign', cancellation=threading.Event())

    def test_timeout_rejects_late_signature_and_keeps_scope_quarantined(self):
        from reproloop.repair_signing import SigningObservation
        from reproloop.repair_execution import RepairExecutionError
        released = threading.Event(); returned = threading.Event()
        def slow(context, artifacts, **kw):
            released.wait(2); returned.set()
            return SigningObservation(context.digest, self.signed_blobs, 'a' * 64, True, True)
        signer = self.signing(signer=slow, timeout=.03)
        try:
            with self.assertRaises(RepairExecutionError) as caught: self.signed(signer)
            self.assertEqual(caught.exception.code, 'signing_quarantined')
        finally:
            released.set(); self.assertTrue(returned.wait(1))
        self.assertEqual(self.signing_store.status('candidate_sign')['state'], 'quarantined')

    def test_cancelled_return_still_requires_cleanup_and_cannot_publish(self):
        from reproloop.repair_signing import SigningObservation
        from reproloop.repair_execution import RepairExecutionError
        cancelled = threading.Event()
        def cancel(context, artifacts, **kw):
            cancelled.set()
            return SigningObservation(context.digest, self.signed_blobs, 'a' * 64, True, False)
        signer = self.signing(signer=cancel)
        with self.assertRaises(RepairExecutionError) as caught: self.signed(signer, cancellation=cancelled)
        self.assertEqual(caught.exception.code, 'signing_quarantined')
        self.assertEqual(self.signing_store.status('candidate_sign')['state'], 'quarantined')

    def test_durable_scope_cancellation_stops_before_signature_inspection(self):
        from reproloop.repair_signing import SigningObservation
        from reproloop.repair_execution import RepairExecutionError
        def cancel(context, artifacts, **kwargs):
            state = self.signing_store.status(context.operation_id)
            self.signing_store.cancel(context.operation_id, state['requestDigest'])
            return SigningObservation(context.digest, self.signed_blobs, 'a' * 64, True, True)
        signer = self.signing(signer=cancel,
            inspector=lambda *args, **kwargs: self.fail('cancelled signing reached inspection'))
        with self.assertRaises(RepairExecutionError) as caught: self.signed(signer)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertEqual(self.signing_store.status('candidate_sign')['state'], 'cancelled')

    def test_revoked_build_cannot_enter_signing_even_with_completed_bytes(self):
        from reproloop.repair_execution import RepairExecutionError
        signer = self.signing(signer=lambda *args, **kwargs: self.fail('revoked build was signed'))
        built = build_tests.ProtectedRepairBuildTests.build(self, self.builder)
        self.authority.revoke_backend('apple-vm', 'build-guest', self.bundle.environment_digest)
        with self.assertRaises(RepairExecutionError) as caught:
            signer.sign(built, operation_id='revoked_sign', cancellation=threading.Event())
        self.assertEqual(caught.exception.code, 'build_unqualified')

    def test_known_clean_signing_failure_cannot_publish_and_releases_only_its_reservation(self):
        from reproloop.repair_signing import SigningFailureObservation
        from reproloop.repair_execution import RepairExecutionError
        signer = self.signing(signer=lambda context, artifacts, **kwargs:
            SigningFailureObservation(context.digest, 'signing_failed', 'a' * 64, True, True),
            inspector=lambda *args, **kwargs: self.fail('failed signing reached inspection'))
        with self.assertRaises(RepairExecutionError) as caught: self.signed(signer)
        self.assertEqual(caught.exception.code, 'signing_failed')
        self.assertEqual(self.signing_store.status('candidate_sign')['state'], 'failed')
        self.assertEqual(self.signing_store.status('candidate_sign')['reservedBytes'], 0)
        self.assertEqual(signer._proofs, {})

    def test_known_clean_inspection_failure_and_cancellation_never_authorize_installation(self):
        from reproloop.repair_signing import SigningFailureObservation
        from reproloop.repair_execution import RepairExecutionError
        cancelled = threading.Event()
        def fail(context, artifacts, **kwargs):
            cancelled.set()
            return SigningFailureObservation(context.digest, 'cancelled', 'a' * 64, True, True)
        signer = self.signing(inspector=fail)
        with self.assertRaises(RepairExecutionError) as caught: self.signed(signer, cancellation=cancelled)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertEqual(self.signing_store.status('candidate_sign')['state'], 'cancelled')
        self.assertEqual(signer._proofs, {})

    def test_wrong_failure_context_retains_quarantine(self):
        from reproloop.repair_signing import SigningFailureObservation
        from reproloop.repair_execution import RepairExecutionError
        def fail(context, artifacts, **kwargs):
            return SigningFailureObservation('f' * 64, 'signing_failed', 'a' * 64, True, True)
        signer = self.signing(signer=fail)
        with self.assertRaises(RepairExecutionError) as caught: self.signed(signer)
        self.assertEqual(caught.exception.code, 'signing_quarantined')
        self.assertEqual(self.signing_store.status('candidate_sign')['state'], 'quarantined')

    def test_known_failure_with_unconfirmed_cleanup_retains_quarantine(self):
        from reproloop.repair_signing import SigningFailureObservation
        from reproloop.repair_execution import RepairExecutionError
        signer = self.signing(signer=lambda context, artifacts, **kwargs:
            SigningFailureObservation(context.digest, 'signing_failed', 'a' * 64, True, False))
        with self.assertRaises(RepairExecutionError) as caught: self.signed(signer)
        self.assertEqual(caught.exception.code, 'signing_quarantined')
        self.assertEqual(self.signing_store.status('candidate_sign')['state'], 'quarantined')

    def test_unknown_failure_code_cannot_escape_cleanup_quarantine(self):
        from reproloop.repair_signing import SigningFailureObservation
        from reproloop.repair_execution import RepairExecutionError
        signer = self.signing(signer=lambda context, artifacts, **kwargs:
            SigningFailureObservation(context.digest, 'untrusted tool output', 'a' * 64, True, True))
        with self.assertRaises(RepairExecutionError) as caught: self.signed(signer)
        self.assertEqual(caught.exception.code, 'signing_quarantined')
        self.assertEqual(self.signing_store.status('candidate_sign')['state'], 'quarantined')


if __name__ == '__main__': unittest.main()
