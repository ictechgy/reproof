"""Fresh-host recovery retains original locks without reviving device effects."""
from dataclasses import replace
import fcntl
import os
import subprocess
import sys
import unittest

from reproloop.core import ContractError
from reproloop.live.authority import HostAuthority
from tests import test_live_authority as support


class NativeRecoveryLeaseTests(unittest.TestCase):
    def setUp(self):
        self.f = support.AuthorityTestCase(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        device = self.f.claim()
        admission = device.admit_operation(operation_id='uncertain-native-operation',
            payload_digest='a'*64, session_id='native-session', sequence=1)
        with self.assertRaises(ContractError):
            device.dispatch_operation(admission, provider_incarnation='native-provider',
                callback=lambda _: (_ for _ in ()).throw(RuntimeError('owned failure')))
        self.prior_host = self.f.authority.host_incarnation
        self.prior_generation = device.generation
        self.prior_inode = os.fstat(device._lease.file.fileno()).st_ino
        self.f.authority.close()
        self.f.authority = HostAuthority(self.f.state_path, clock=self.f.clock,
            lease_directory=self.f.lease_directory)
        self.grant = self.f.grant(grant_id='fresh-recovery-grant')
        self.device = self.f.claim(parent_grant=self.grant, helper_incarnation='fresh-candidate-helper')
        self.snapshot = self.device.recovery_snapshot()

    def test_fresh_host_can_borrow_only_the_quarantined_original_lease(self):
        self.assertTrue(self.device.requires_reconciliation)
        self.assertNotEqual(self.f.authority.host_incarnation, self.prior_host)
        with self.assertRaises(ContractError):
            with self.device.borrow_native_lease():
                pass
        with self.device.borrow_native_recovery_lease(self.snapshot, parent_grant=self.grant) as borrowed:
            self.assertIs(self.device.require_native_recovery_lease(borrowed), borrowed)
            self.assertEqual(os.fstat(borrowed.descriptor).st_ino, self.prior_inode)
            self.assertEqual(borrowed.prior_generation, self.prior_generation)
            self.assertEqual(borrowed.prior_host_incarnation, self.prior_host)
            with self.assertRaises(ContractError):
                self.device.require_native_recovery_lease(replace(borrowed))
            with self.assertRaises(ContractError):
                self.device.check_ownership()
            self.assertTrue(self.device.requires_reconciliation)
        with self.assertRaises(ContractError):
            self.device.require_native_recovery_lease(borrowed)

    def test_expired_grant_or_changed_snapshot_is_rejected(self):
        wrong = replace(self.snapshot, prior_generation=self.snapshot.prior_generation+1)
        with self.assertRaises(ContractError):
            with self.device.borrow_native_recovery_lease(wrong, parent_grant=self.grant):
                pass
        with self.device.borrow_native_recovery_lease(self.snapshot, parent_grant=self.grant) as borrowed:
            self.f.clock.advance(2_000_000)
            with self.assertRaises(ContractError):
                self.device.require_native_recovery_lease(borrowed)
        with self.assertRaises(ContractError):
            with self.device.borrow_native_recovery_lease(self.snapshot, parent_grant=self.grant):
                pass
        self.assertTrue(self.device.requires_reconciliation)

    def test_generation_cannot_change_while_recovery_descriptors_are_borrowed(self):
        authority = self.f.authority
        disposition = authority.record_operation_disposition(self.snapshot,
            operation_id='uncertain-native-operation', terminal_status='rejected',
            result_digest='a'*64, evidence_digest='b'*64)
        reconciliation = authority.record_reconciliation(self.snapshot, dispositions=[disposition],
            prior_helper_exit_digest='a'*64, pointer_cleanup_digest='b'*64,
            fresh_helper_incarnation='reconciled-helper', fresh_handshake_digest='c'*64)
        with self.device.borrow_native_recovery_lease(self.snapshot, parent_grant=self.grant):
            with self.assertRaises(ContractError):
                self.device.reconcile(reconciliation, parent_grant=self.grant)
            self.assertTrue(self.device.requires_reconciliation)
        self.device.reconcile(reconciliation, parent_grant=self.grant)
        self.assertEqual(self.device.status, 'owned')
        self.assertEqual(self.device.generation, self.prior_generation+1)

    def test_child_retains_recovery_lock_after_parent_handle_closes(self):
        reader, writer = os.pipe()
        process = None
        try:
            with self.device.borrow_native_recovery_lease(self.snapshot, parent_grant=self.grant) as borrowed:
                path = self.device._lease.directory / self.device._lease.key
                path = path.with_suffix('.lock')
                process = subprocess.Popen([sys.executable, '-I', '-c',
                    'import os,sys;os.fstat(int(sys.argv[1]));print("ready",flush=True);os.read(int(sys.argv[2]),1)',
                    str(borrowed.descriptor), str(reader)], pass_fds=(borrowed.descriptor, reader),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(process.stdout.readline(), b'ready\n')
                self.assertFalse(self.device.close())
            probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(writer, b'x')
                self.assertEqual(process.wait(timeout=3), 0)
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
        finally:
            os.close(reader)
            os.close(writer)
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                process.stdout.close()
                process.stderr.close()
