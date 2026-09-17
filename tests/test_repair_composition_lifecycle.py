"""Shutdown retains uncertain owners while fencing all new native dispatch."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from reproloop.live.issue_configuration import IssueRuntimeBundle
from reproloop.repair_android_signing import (
    AndroidApkInspector, AndroidApkSigner, AndroidSigningError,
    AndroidSigningIdentity, AndroidSigningMaterialResolver, AndroidSigningTools,
    _ProcessOwner,
)
from reproloop.repair_composition import ProtectedRepairComposition
from reproloop.repair_execution import RepairExecutionError
from tests.test_repair_android_signing import (
    APPLICATION, CERTIFICATE, CONFIGURATION, PACKAGE, PERMISSIONS, SCHEMES,
    _aapt_script, _apksigner_script, _digest, _write_executable,
)


class CompositionLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        java = _write_executable(self.root / 'java', _apksigner_script())
        jar = self.root / 'apksigner.jar'; jar.write_bytes(b'fixed test jar')
        aapt = _write_executable(self.root / 'aapt2', _aapt_script())
        self.tools = AndroidSigningTools(java, _digest(java), jar, _digest(jar), aapt, _digest(aapt))
        self.identity = AndroidSigningIdentity('owned-signing', APPLICATION, PACKAGE, CERTIFICATE,
                                               SCHEMES, PERMISSIONS)
        self.policy = {'schemaVersion': 1, 'id': 'android-signing', 'platform': 'android',
            'applicationId': APPLICATION, 'identityReferenceId': self.identity.reference_id,
            'entitlementsDigest': CONFIGURATION, 'tool': 'host-apksigner-fixed',
            'candidateHooks': 'forbidden', 'artifactRelation': 'pre-post-digests'}
        material = self.root / 'owned-test.p12'; material.write_bytes(b'owned test material'); material.chmod(0o600)
        self.resolver = AndroidSigningMaterialResolver(); self.addCleanup(self.resolver.close)
        self.resolver.register(self.identity, keystore=material, key_alias='owned-test',
            store_password=b'store-secret', key_password=b'key-secret')
        self.signer = AndroidApkSigner(self.tools, self.resolver, self.identity, self.policy, self.root / 'sign')
        self.inspector = AndroidApkInspector(self.tools, self.identity, self.policy, self.root / 'inspect')
        self.addCleanup(self.signer.close); self.addCleanup(self.inspector.close)
        self.composition = ProtectedRepairComposition(); self.addCleanup(self.composition.close)

    def test_exact_signing_pair_is_owned_until_processes_and_material_are_closed(self):
        self.composition.adopt_android_signing(self.signer, self.inspector)
        self.assertEqual(self.composition.status()['cleanupPending'], 1)
        self.composition.close()
        self.assertEqual(self.composition.status()['cleanupPending'], 0)
        with self.assertRaises(AndroidSigningError): self.resolver.open(self.identity)
        with self.assertRaises(RepairExecutionError):
            self.composition.adopt_android_signing(self.signer, self.inspector)
        self.composition.close()

    def test_foreign_identity_duplicate_and_untyped_resources_are_not_adopted(self):
        foreign = AndroidSigningIdentity(self.identity.reference_id, APPLICATION, PACKAGE,
                                         'f' * 64, SCHEMES, PERMISSIONS)
        inspector = AndroidApkInspector(self.tools, foreign, self.policy, self.root / 'foreign')
        self.addCleanup(inspector.close)
        for pair in ((self.signer, inspector), (self.signer, {'qualified': True})):
            with self.assertRaises(RepairExecutionError): self.composition.adopt_android_signing(*pair)
        self.composition.adopt_android_signing(self.signer, self.inspector)
        with self.assertRaises(RepairExecutionError):
            self.composition.adopt_android_signing(self.signer, self.inspector)

    def test_unknown_process_keeps_material_and_owner_for_a_later_close_attempt(self):
        self.composition.adopt_android_signing(self.signer, self.inspector)
        with mock.patch.object(AndroidApkSigner, 'active_processes', new_callable=mock.PropertyMock, return_value=1):
            with self.assertRaises(RepairExecutionError): self.composition.close()
        self.assertTrue(self.composition.status()['closed'])
        self.assertEqual(self.composition.status()['cleanupPending'], 1)
        opened = self.resolver.open(self.identity); opened.close()
        self.composition.close()
        self.assertEqual(self.composition.status()['cleanupPending'], 0)
        with self.assertRaises(AndroidSigningError): self.resolver.open(self.identity)

    def test_busy_workflow_revokes_authority_but_retains_resources_until_joined(self):
        self.composition.adopt_android_signing(self.signer, self.inspector)
        bundle = IssueRuntimeBundle(self.root / 'bundle')
        workflow = mock.Mock(); workflow.close.side_effect = RuntimeError('owned job still active')
        bundle.workflow = workflow; bundle.protected_repairs = self.composition
        registry = mock.Mock(); bundle.registries.append(registry)
        with self.assertRaises(RuntimeError): bundle.close()
        self.assertTrue(self.composition.status()['closed'])
        self.assertIs(bundle.workflow, workflow)
        self.assertIs(bundle.protected_repairs, self.composition)
        registry.close.assert_not_called()
        opened = self.resolver.open(self.identity); opened.close()
        workflow.close.side_effect = None
        bundle.close()
        self.assertIsNone(bundle.workflow); self.assertIsNone(bundle.protected_repairs)
        registry.close.assert_called_once()
        with self.assertRaises(AndroidSigningError): self.resolver.open(self.identity)

    def test_closed_process_owner_never_dispatches_a_late_callback(self):
        owner = _ProcessOwner(); owner.close()
        with mock.patch('reproloop.repair_android_signing.subprocess.Popen',
                        side_effect=AssertionError('closed owner dispatched')) as spawn:
            with self.assertRaises(AndroidSigningError):
                owner.run(('/usr/bin/true',), work=self.root, input_bytes=b'', pass_fds=(),
                    cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 2)
        spawn.assert_not_called()

    def test_close_waits_for_a_spawn_already_at_the_dispatch_boundary(self):
        import subprocess
        owner = _ProcessOwner(); self.addCleanup(owner.close)
        entering = threading.Event(); release = threading.Event(); closed = threading.Event()
        actual_spawn = subprocess.Popen
        results = []
        def gated_spawn(*args, **kwargs):
            entering.set()
            if not release.wait(3): raise RuntimeError('test barrier timed out')
            return actual_spawn(*args, **kwargs)
        def run():
            try:
                results.append(owner.run(('/bin/sleep', '10'), work=self.root,
                    input_bytes=b'', pass_fds=(), cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 4))
            except Exception as error: results.append(error)
        def close(): owner.close(); closed.set()
        with mock.patch('reproloop.repair_android_signing.subprocess.Popen', side_effect=gated_spawn):
            worker = threading.Thread(target=run); worker.start()
            self.assertTrue(entering.wait(2))
            closer = threading.Thread(target=close); closer.start()
            try: self.assertFalse(closed.wait(.1), 'close escaped an in-flight native dispatch')
            finally:
                release.set(); closer.join(5); worker.join(5)
        self.assertFalse(closer.is_alive()); self.assertFalse(worker.is_alive())
        self.assertEqual(owner.active_processes, 0)
        self.assertTrue(closed.is_set())

    def test_unreturned_native_dispatch_keeps_a_retryable_owner_after_close_deadline(self):
        import subprocess
        owner = _ProcessOwner(); self.addCleanup(owner.close)
        entered = threading.Event(); release = threading.Event(); finished = threading.Event()
        actual_spawn = subprocess.Popen
        def gated_spawn(*args, **kwargs):
            entered.set()
            if not release.wait(3): raise RuntimeError('test barrier timed out')
            return actual_spawn(*args, **kwargs)
        def run():
            try:
                owner.run(('/bin/sleep', '10'), work=self.root, input_bytes=b'', pass_fds=(),
                    cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 4)
            finally: finished.set()
        with mock.patch('reproloop.repair_android_signing.subprocess.Popen', side_effect=gated_spawn):
            worker = threading.Thread(target=run); worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(owner.close(deadline_monotonic=time.monotonic() + .05))
                self.assertFalse(finished.is_set())
            finally:
                release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(owner.close(deadline_monotonic=time.monotonic() + 1))
        self.assertEqual(owner.active_processes, 0)

    def test_close_never_signals_a_process_group_after_its_owned_leader_was_reaped(self):
        owner = _ProcessOwner()
        reaped = mock.Mock(); reaped.poll.return_value = 0
        owner._processes.add(reaped)
        try:
            with mock.patch.object(owner, '_group_empty', return_value=False), \
                    mock.patch.object(owner, '_terminate', side_effect=AssertionError('unowned PID signal')) as terminate:
                self.assertFalse(owner.close())
                terminate.assert_not_called()
            self.assertEqual(owner.active_processes, 1)
        finally:
            owner._processes.clear()

    def test_returned_child_with_unknown_group_stays_uncertain_without_a_pid_signal(self):
        import io
        owner = _ProcessOwner()
        reaped = mock.Mock(pid=123456, returncode=0)
        reaped.poll.return_value = 0; reaped.wait.return_value = 0
        reaped.stdin = io.BytesIO(); reaped.stdout = io.BytesIO(); reaped.stderr = io.BytesIO()
        try:
            with mock.patch('reproloop.repair_android_signing.subprocess.Popen', return_value=reaped), \
                    mock.patch.object(_ProcessOwner, '_group_empty', return_value=False), \
                    mock.patch('reproloop.repair_android_signing.os.killpg',
                               side_effect=AssertionError('unowned PID signal')) as signal_group:
                result = owner.run(('/usr/bin/true',), work=self.root, input_bytes=b'', pass_fds=(),
                    cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 1)
                self.assertFalse(result.terminated)
                signal_group.assert_not_called()
            self.assertEqual(owner.active_processes, 1)
        finally:
            owner._processes.clear()


if __name__ == '__main__': unittest.main()
