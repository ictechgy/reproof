"""Real authority/OS locks over owned iOS metadata; no Apple device is contacted."""
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from reproof.execution.artifacts import BlobSet
from reproof.execution.journal import RunStore
from reproof.ios_mobile_operation import IOSMobileOperationError, IOSMobileOperationStore
from reproof.live.authority import HostAuthority, issue_local_parent_grant
from tests import test_ios_mobile_operation as preparation


def crash_native_owner(root,udid,mode):
    from reproof import ios_mobile_native as native
    root=Path(root);body=(root/'input.ipa').read_bytes()
    baselines=BlobSet((('original.ipa',body),));selected=preparation.definition(udid,baselines)
    runs=RunStore(root/'runs',environment_digest='9'*64,disk_limit=4*1024**3)
    operations=IOSMobileOperationStore(runs,selected,root/'operations')
    authority=HostAuthority(root/'authority/state.sqlite3',lease_directory=root/'device-leases')
    grant=issue_local_parent_grant(authority,lifetime_ns=600_000_000_000)
    device=authority.claim_device(device_kind='ios-physical',physical_id=udid,
        helper_incarnation='owned-crash-helper',parent_grant=grant)
    with operations.admit(preparation.context(selected,body),BlobSet((('candidate.ipa',body),)),baselines) as operation:
        for role in ('candidate','original'):
            operations.prepare(operation,role,cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10)
        if mode=='before-state':
            with patch.object(native,'_replace_at',side_effect=lambda *_:os._exit(73)):
                with operations.native_owner(operation,device):pass
        else:
            with operations.native_owner(operation,device) as owner,owner.borrow_descriptors():os._exit(74)
    os._exit(75)


class IOSMobileNativeTests(unittest.TestCase):
    def setUp(self):
        self.c = preparation.IOSMobileOperationTests(methodName='runTest')
        self.addCleanup(self.c.doCleanups); self.c.setUp()
        self.authority = HostAuthority(self.c.root/'authority/state.sqlite3',
                                       lease_directory=self.c.root/'device-leases')
        self.addCleanup(self.authority.close)
        self.grant = issue_local_parent_grant(self.authority, lifetime_ns=600_000_000_000)
        self.device = self.claim(self.c.selected.udid)
        self.root = self.c.operations.operations/self.c.context.operation_id

    def claim(self, udid, kind='ios-physical'):
        return self.authority.claim_device(device_kind=kind, physical_id=udid,
            helper_incarnation='owned-ios-helper', parent_grant=self.grant)

    @contextmanager
    def admitted(self):
        with self.c.operations.admit(self.c.context,self.c.artifacts,self.c.baselines) as operation:
            for role in ('candidate','original'):self.c.prepare(operation,role)
            yield operation

    def owner(self, operation, device=None):
        return self.c.operations.native_owner(operation,device or self.device)

    def assert_held(self, path):
        probe = os.open(path,os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:os.close(probe)

    def test_exact_live_device_owns_all_prepared_roles_and_original_descriptors(self):
        with self.admitted() as operation:
            with self.owner(operation) as owner:
                with owner.borrow_descriptors() as borrowed:
                    self.assertIs(owner.require_descriptors(borrowed),borrowed)
                    self.assertEqual(os.fstat(borrowed.producer_fd).st_ino,
                                     os.fstat(operation._producer_fd).st_ino)
                    self.assertEqual(os.fstat(borrowed.device_fd).st_ino,
                                     os.fstat(self.device._lease.file.fileno()).st_ino)
                    self.assert_held(self.root/'producer.lock')
                    self.assert_held(self.authority.lease_directory/(self.c.selected.scope_digest+'.lock'))
                public = self.c.operations.status(self.c.context.operation_id)
                self.assertEqual(public['nativeOwnership']['state'],'bound')
                self.assertFalse(public['deviceCleanupConfirmed'])
                self.assertNotIn(self.c.selected.udid,json.dumps(public))
                self.assertNotIn(self.c.selected.udid,repr(owner))
            with self.assertRaises(IOSMobileOperationError):
                with owner.borrow_descriptors():pass
        self.assertGreater(self.c.runs.status(self.c.context.operation_id)['reservedBytes'],0)

    def test_wrong_device_and_nonissued_handle_cannot_create_native_records(self):
        foreign=self.claim('owned-foreign-phone')
        from reproof.live.authority import DeviceAuthority
        forged=DeviceAuthority(authority=self.authority,lease=self.device._lease,
            device_kind='ios-physical',device_fingerprint=self.device._device_fingerprint,
            generation=self.device.generation,helper_incarnation=self.device.helper_incarnation,
            parent_grant=self.grant)
        with self.admitted() as operation:
            for device in (foreign,forged):
                with self.subTest(kind='foreign' if device is foreign else 'unissued'):
                    with self.assertRaises(IOSMobileOperationError):
                        with self.owner(operation,device):pass
            self.assertFalse((self.root/'native.json').exists())

    def test_copied_cross_thread_and_expired_exports_cannot_authorize_dispatch(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            with owner.borrow_descriptors() as borrowed:
                with self.assertRaises(IOSMobileOperationError):owner.require_descriptors(replace(borrowed))
                rejected=[]
                def check():
                    try:owner.require_descriptors(borrowed)
                    except IOSMobileOperationError:rejected.append(True)
                worker=threading.Thread(target=check);worker.start();worker.join(3)
                self.assertFalse(worker.is_alive());self.assertEqual(rejected,[True])
            with self.assertRaises(IOSMobileOperationError):owner.require_descriptors(borrowed)

    def test_reentry_and_rewritten_binding_are_rejected(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            with self.assertRaises(IOSMobileOperationError):
                with self.owner(operation):pass
            native=json.loads((self.root/'native.json').read_bytes())
            native['ownershipGeneration']+=1
            (self.root/'native.json').write_text(json.dumps(native))
            with self.assertRaises(IOSMobileOperationError):
                with owner.borrow_descriptors():pass

    def test_revoked_authority_and_finished_admission_block_new_exports(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            self.device.revoke_dispatches()
            with self.assertRaises(IOSMobileOperationError):
                with owner.borrow_descriptors():pass
        with self.assertRaises(IOSMobileOperationError):
            with self.owner(operation):pass

    def test_native_entry_cannot_be_recovered_as_file_preparation(self):
        with self.admitted() as operation, self.owner(operation):pass
        fresh=IOSMobileOperationStore(self.c.runs,self.c.selected,self.c.operations.root,create=False)
        self.addCleanup(fresh.close)
        with self.assertRaises(IOSMobileOperationError):
            with fresh.preparation_recovery(self.c.context.operation_id,self.c.context.request_digest,
                    cancellation=threading.Event(),deadline_monotonic=time.monotonic()+3):pass
        self.assertTrue((self.root/'candidate/input.ipa').is_file())
        self.assertGreater(self.c.runs.status(self.c.context.operation_id)['reservedBytes'],0)
        self.assertEqual(fresh.status(self.c.context.operation_id)['nativeOwnership']['state'],'bound')

    def test_child_keeps_original_locks_after_python_owners_exit(self):
        child=None
        try:
            with self.admitted() as operation, self.owner(operation) as owner:
                with owner.borrow_descriptors() as borrowed:
                    child=subprocess.Popen([sys.executable,'-I','-c',
                        'import os,sys;os.write(1,b"ready\\n");os.read(0,1)'],
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                        pass_fds=(borrowed.producer_fd,borrowed.device_fd))
                    self.assertEqual(child.stdout.readline(),b'ready\n')
            self.device.close()
            for path in (self.root/'producer.lock',
                         self.authority.lease_directory/(self.c.selected.scope_digest+'.lock')):
                descriptor=os.open(path,os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                finally:os.close(descriptor)
        finally:
            if child is not None:
                child.communicate(b'x',timeout=5)
                self.assertEqual(child.returncode,0)

    def test_shutdown_waits_for_export_and_then_allows_it_to_close(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            with owner.borrow_descriptors() as borrowed:
                self.assertFalse(self.c.operations.close(deadline_monotonic=time.monotonic()))
                with self.assertRaises(IOSMobileOperationError):owner.require_descriptors(borrowed)
        self.assertTrue(self.c.operations.close(deadline_monotonic=time.monotonic()+1))

    def test_unprepared_original_blocks_native_entry(self):
        with self.c.operations.admit(self.c.context,self.c.artifacts,self.c.baselines) as operation:
            self.c.prepare(operation)
            with self.assertRaises(IOSMobileOperationError):
                with self.owner(operation):pass
            self.assertFalse((self.root/'native.json').exists())

    def test_a_reopened_matching_inode_is_not_an_original_borrowed_lock(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            with owner.borrow_descriptors() as borrowed:
                original=borrowed.producer_fd
                replacement=os.open(self.root/'producer.lock',os.O_RDWR)
                try:
                    borrowed.producer_fd=replacement
                    with self.assertRaises(IOSMobileOperationError):owner.require_descriptors(borrowed)
                finally:
                    borrowed.producer_fd=original;os.close(replacement)
                self.assertIs(owner.require_descriptors(borrowed),borrowed)

    def test_process_death_during_binding_or_export_preserves_native_recovery_boundary(self):
        for mode,code in (('before-state',73),('exported',74)):
            with self.subTest(mode=mode):
                root=self.c.root/mode;root.mkdir(mode=0o700)
                (root/'input.ipa').write_bytes(self.c.body)
                udid=self.c.selected.udid+'-'+mode
                child=multiprocessing.get_context('spawn').Process(target=crash_native_owner,args=(root,udid,mode))
                try:
                    child.start();child.join(10)
                    self.assertFalse(child.is_alive());self.assertEqual(child.exitcode,code)
                finally:
                    if child.is_alive():child.kill();child.join(5)
                    child.close()
                selected=preparation.definition(udid,self.c.baselines)
                runs=RunStore(root/'runs',environment_digest='9'*64,disk_limit=4*1024**3)
                owner=IOSMobileOperationStore(runs,selected,root/'operations',create=False)
                self.addCleanup(owner.close);context=preparation.context(selected,self.c.body)
                self.assertEqual(owner.status(context.operation_id)['nativeOwnership']['state'],'bound')
                with self.assertRaises(IOSMobileOperationError):
                    with owner.preparation_recovery(context.operation_id,context.request_digest,
                            cancellation=threading.Event(),deadline_monotonic=time.monotonic()+3):pass
                self.assertGreater(runs.status(context.operation_id)['reservedBytes'],0)
                self.assertTrue((owner.operations/context.operation_id/'original/App.app').is_dir())

    def test_either_native_record_keeps_preparation_recovery_closed(self):
        with self.admitted() as operation,self.owner(operation):pass
        native=(self.root/'native.json').read_bytes()
        (self.root/'native.json').unlink()
        fresh=IOSMobileOperationStore(self.c.runs,self.c.selected,self.c.operations.root,create=False)
        self.addCleanup(fresh.close)
        for mode in ('state-only','native-only'):
            if mode=='native-only':
                (self.root/'native.json').write_bytes(native);(self.root/'native.json').chmod(0o600)
                state=json.loads((self.root/'state.json').read_bytes());state.pop('nativeBindingDigest')
                (self.root/'state.json').write_text(json.dumps(state))
            with self.subTest(mode=mode),self.assertRaises(IOSMobileOperationError):
                with fresh.preparation_recovery(self.c.context.operation_id,self.c.context.request_digest,
                        cancellation=threading.Event(),deadline_monotonic=time.monotonic()+3):pass
            self.assertTrue((self.root/'candidate/input.ipa').is_file())
