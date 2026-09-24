"""Original operation and device descriptors for a bounded recovery owner."""
from dataclasses import replace
import fcntl
import os
import subprocess
import sys
import threading
import time
import unittest

from reproof.repair_android_operation import AndroidOperationError
from tests import test_android_mobile_operation_integration as support


class AndroidRecoveryDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.PersistentAndroidAdapterTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f, self.operations = self.fixture.fixture, self.fixture.operations
        self.scope = self.f.lab.begin_retained_device_scope('device', self.f.config.owner,
            'android-recovery-scope', self.f.registration, application_id=self.f.config.application_id,
            build_id=self.f.config.original_build_id)
        self.device = self.scope._reservation._authority_handle
        self.addCleanup(self.device.close)
        self.grant = self.device._parent_grant
        with self.operations.admit(self.f.context, self.fixture.blobs) as operation:
            self.operation = operation
            binding = self.operations.bind_native(operation, self.f.context,
                ownership_generation=self.device.generation,
                host_incarnation=self.device._authority.host_incarnation,
                helper_incarnation=self.device.helper_incarnation,
                provider_incarnation='android_recovery_provider')
            with self.operations.phase(operation, self.f.context, binding, 'install') as phase:
                self.operations.complete_phase(phase, 'a'*64)
        self.device.revoke_dispatches()
        self.snapshot = self.device.recovery_snapshot()

    def borrow(self, **changes):
        values = dict(device=self.device, snapshot=self.snapshot, parent_grant=self.grant)
        values.update(changes)
        return self.operations.native_recovery(self.operation.operation_id,
            self.operation.request_digest, **values)

    def test_recovery_export_is_live_bound_and_does_not_release_reservation(self):
        with self.borrow() as borrowed:
            self.assertIs(self.operations.require_recovery_descriptors(borrowed), borrowed)
            self.assertEqual(borrowed.scope_digest, self.f.config.scope_digest)
            self.assertEqual(borrowed.prior_generation, self.snapshot.prior_generation)
            self.assertEqual(os.fstat(borrowed.device_fd).st_ino,
                             os.fstat(self.device._lease.file.fileno()).st_ino)
            with self.assertRaises(AndroidOperationError):
                self.operations.require_recovery_descriptors(replace(borrowed))
            with self.assertRaises(AndroidOperationError):
                self.operations.require_native_descriptors(borrowed)
            self.assertTrue(self.device.requires_reconciliation)
        with self.assertRaises(AndroidOperationError):
            self.operations.require_recovery_descriptors(borrowed)
        self.assertGreater(self.fixture.run_store.status(self.operation.operation_id)['reservedBytes'], 0)

    def test_mismatched_prior_generation_is_rejected(self):
        with self.assertRaises(AndroidOperationError):
            with self.borrow(snapshot=replace(self.snapshot, prior_generation=self.snapshot.prior_generation+1)):
                pass
        self.assertTrue(self.device.requires_reconciliation)

    def test_valid_grant_for_a_different_project_cannot_borrow_operation_locks(self):
        authority = self.device._authority
        grant = authority.issue_parent_grant(self.grant._mapping,
            grant_id='wrong-project-recovery', project_id='unrelated_project',
            controller_id=self.grant.controller_id, renewal_sequence=self.grant.renewal_sequence+1,
            coordinator_deadline_ns=self.grant.coordinator_deadline_ns)
        with self.assertRaises(AndroidOperationError):
            with self.borrow(parent_grant=grant):
                pass
        self.assertTrue(self.device.requires_reconciliation)

    def test_close_waits_for_the_recovery_scope_and_revokes_its_token(self):
        values = []
        with self.borrow() as borrowed:
            thread = threading.Thread(target=lambda: values.append(
                self.operations.close(deadline_monotonic=time.monotonic()+.05)))
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(values, [False])
            with self.assertRaises(AndroidOperationError):
                self.operations.require_recovery_descriptors(borrowed)
        self.assertTrue(self.operations.close(deadline_monotonic=time.monotonic()+1))
        self.assertGreater(self.fixture.run_store.status(self.operation.operation_id)['reservedBytes'], 0)

    def test_child_keeps_both_recovery_locks_after_parent_context_and_device_close(self):
        reader, writer = os.pipe()
        child = None
        producer = self.operation.staging_root.parent/'producer.lock'
        device_path = self.device._lease.directory/(self.device._lease.key+'.lock')
        try:
            with self.borrow() as borrowed:
                child = subprocess.Popen([sys.executable, '-I', '-c',
                    'import os,sys;[os.fstat(int(fd)) for fd in sys.argv[1:3]];print("ready",flush=True);os.read(int(sys.argv[3]),1)',
                    str(borrowed.producer_fd), str(borrowed.device_fd), str(reader)],
                    pass_fds=(borrowed.producer_fd, borrowed.device_fd, reader),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(child.stdout.readline(), b'ready\n')
            self.device.close()
            probes = [os.open(path, os.O_RDWR|os.O_NOFOLLOW) for path in (producer, device_path)]
            try:
                for descriptor in probes:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(AndroidOperationError):
                    with self.operations.recovery(self.operation.operation_id, self.operation.request_digest):
                        pass
                os.write(writer, b'x')
                self.assertEqual(child.wait(timeout=3), 0)
                for descriptor in probes:
                    fcntl.flock(descriptor, fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:
                for descriptor in probes:
                    os.close(descriptor)
        finally:
            os.close(reader)
            os.close(writer)
            if child is not None:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
                child.stdout.close()
                child.stderr.close()
