"""Original kernel-lock transfer; no device commands or user material are used."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace

from reproloop.core import ContractError
from reproloop.storage import Lease


class NativeLeaseRetentionTests(unittest.TestCase):
    def test_parent_close_does_not_unlock_a_duplicate_open_file_description(self):
        with tempfile.TemporaryDirectory(prefix='owned-lease-retention-') as temporary:
            directory=Path(temporary).resolve();lease=Lease('owned-descriptor',directory)
            with lease:duplicate=os.dup(lease.file.fileno())
            try:
                with self.assertRaises(ContractError):
                    with Lease('owned-descriptor',directory):pass
            finally:os.close(duplicate)
            with Lease('owned-descriptor',directory):pass

    def test_child_keeps_the_original_lock_after_parent_scope_exits(self):
        with tempfile.TemporaryDirectory(prefix='owned-child-lease-') as temporary:
            directory=Path(temporary).resolve();reader,writer=os.pipe();child=None
            try:
                with Lease('owned-child',directory) as lease:
                    descriptor=lease.file.fileno()
                    child=subprocess.Popen([sys.executable,'-I','-c',
                        'import os,sys\nfd=int(sys.argv[1]);pipe=int(sys.argv[2]);os.fstat(fd);print("ready",flush=True);os.read(pipe,1)\n',
                        str(descriptor),str(reader)],pass_fds=(descriptor,reader),stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    self.assertEqual(child.stdout.readline(),b'ready\n')
                with self.assertRaises(ContractError):
                    with Lease('owned-child',directory):pass
                os.write(writer,b'x');self.assertEqual(child.wait(timeout=3),0)
                with Lease('owned-child',directory):pass
            finally:
                os.close(reader);os.close(writer)
                if child is not None:
                    if child.poll() is None:child.kill();child.wait(timeout=3)
                    child.stdout.close();child.stderr.close()


class AndroidNativeDescriptorTests(unittest.TestCase):
    def setUp(self):
        from tests import test_android_mobile_operation_integration as support
        self.fixture=support.PersistentAndroidAdapterTests(methodName='runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.operations=self.fixture.operations;self.f=self.fixture.fixture

    def test_export_requires_exact_phase_and_original_device_generation(self):
        from reproloop.repair_android_operation import AndroidOperationError
        context=self.f.context
        with self.operations.admit(context,self.fixture.blobs) as operation:
            scope=self.f.lab.begin_retained_device_scope('device',self.f.config.owner,'owned-native-descriptors',
                self.f.registration,application_id=self.f.config.application_id,build_id=self.f.config.original_build_id)
            try:
                handle=scope._reservation._authority_handle
                binding=self.operations.bind_native(operation,context,ownership_generation=handle.generation,
                    host_incarnation=self.f.authority.host_incarnation,helper_incarnation=handle.helper_incarnation,
                    provider_incarnation='owned-native-provider')
                with self.operations.phase(operation,context,binding,'install') as phase:
                    with self.operations.borrow_native_descriptors(operation,context,binding,phase,handle) as borrowed:
                        self.operations.require_native_descriptors(borrowed)
                        self.assertEqual(os.fstat(borrowed.producer_fd).st_ino,os.fstat(phase._producer_fd).st_ino)
                        self.assertEqual(os.fstat(borrowed.device_fd).st_ino,os.fstat(handle._lease.file.fileno()).st_ino)
                        self.assertEqual(borrowed.ownership_generation,handle.generation)
                        with self.assertRaises(AndroidOperationError):
                            self.operations.require_native_descriptors(replace(borrowed))
                    with self.assertRaises(AndroidOperationError):self.operations.require_native_descriptors(borrowed)
                    with self.assertRaises(AndroidOperationError):
                        with self.operations.borrow_native_descriptors(operation,context,binding,replace(phase),handle):pass
                    self.operations.complete_phase(phase,'a'*64)
            finally:self.f.lab.release_retained_device_scope(scope)

    def test_exported_producer_stays_locked_after_phase_context_exits(self):
        context=self.f.context
        with self.operations.admit(context,self.fixture.blobs) as operation:
            scope=self.f.lab.begin_retained_device_scope('device',self.f.config.owner,'owned-phase-descriptors',
                self.f.registration,application_id=self.f.config.application_id,build_id=self.f.config.original_build_id)
            duplicate=None
            try:
                handle=scope._reservation._authority_handle
                binding=self.operations.bind_native(operation,context,ownership_generation=handle.generation,
                    host_incarnation=self.f.authority.host_incarnation,helper_incarnation=handle.helper_incarnation,
                    provider_incarnation='owned-native-provider')
                with self.operations.phase(operation,context,binding,'install') as phase:
                    with self.operations.borrow_native_descriptors(operation,context,binding,phase,handle) as borrowed:
                        duplicate=os.dup(borrowed.producer_fd)
                    self.operations.complete_phase(phase,'a'*64)
                path=self.operations.operations/context.operation_id/'producer.lock'
                probe=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
                try:
                    with self.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    os.close(duplicate);duplicate=None
                    fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                finally:os.close(probe)
            finally:
                if duplicate is not None:os.close(duplicate)
                self.f.lab.release_retained_device_scope(scope)
