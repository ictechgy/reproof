"""Native guardian and owned Mach-O CoreDevice protocol double; no phone/service."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import fcntl
import multiprocessing
import os
from pathlib import Path
import subprocess
import signal
import threading
import time
import unittest
from unittest.mock import patch

from reproof.ios_device_tools import IOSDeviceQueryDefinition,IOSDeviceTools,IOSDeviceToolError
from reproof.ios_mobile_operation import IOSMobileOperationStore
from tests import test_ios_device_tools as queries
from tests import test_ios_mobile_native as native


def build_owned_guardian(test,root):
    from reproof.ios_device_guardian import IOSDeviceGuardianTools
    from reproof.resources import read_resource
    path=root/'ios-device-guardian';source=root/'ios-device-guardian.c'
    source.write_bytes(read_resource('native/ios-device-guardian/main.c'))
    result=subprocess.run(['/usr/bin/clang','-std=c11','-Wall','-Wextra','-Werror',
        '-Wno-deprecated-declarations',str(source),'-o',str(path)],capture_output=True,timeout=20)
    test.assertEqual(result.returncode,0,'Owned guardian compile failed: '+result.stderr.decode()[:1000])
    path.chmod(0o700)
    return IOSDeviceGuardianTools(path,hashlib.sha256(path.read_bytes()).hexdigest())


def guarded_query_child(root,body,udid,tool,guardian_path):
    from reproof.execution.artifacts import BlobSet
    from reproof.execution.journal import RunStore
    from reproof.ios_device_guardian import IOSDeviceGuardianTools
    from reproof.live.authority import HostAuthority,issue_local_parent_grant
    root=Path(root);work=root/'queries';work.mkdir(mode=0o700)
    guardian=IOSDeviceGuardianTools(Path(guardian_path),hashlib.sha256(Path(guardian_path).read_bytes()).hexdigest())
    definition=IOSDeviceQueryDefinition(IOSDeviceTools(Path(tool),hashlib.sha256(Path(tool).read_bytes()).hexdigest()),
        queries.IDENTIFIER,udid,'com.example.flat',work,guardian)
    baselines=BlobSet((('original.ipa',body),))
    selected=replace(native.preparation.definition(udid,baselines),query_definition_digest=definition.definition_digest)
    runs=RunStore(root/'runs',environment_digest='9'*64,disk_limit=4*1024**3)
    operations=IOSMobileOperationStore(runs,selected,root/'operations')
    authority=HostAuthority(root/'authority/state.sqlite3',lease_directory=root/'device-leases')
    grant=issue_local_parent_grant(authority,lifetime_ns=600_000_000_000)
    device=authority.claim_device(device_kind='ios-physical',physical_id=udid,
        helper_incarnation='owned-query-helper',parent_grant=grant)
    with operations.admit(native.preparation.context(selected,body),BlobSet((('candidate.ipa',body),)),baselines) as operation:
        for role in ('candidate','original'):operations.prepare(operation,role,
            cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10)
        with operations.native_owner(operation,device) as owner:
            client=definition.open_client(native_owner=owner,guardian=guardian)
            client.query('details',cancellation=threading.Event(),deadline_monotonic=time.monotonic()+30)
    os._exit(76)


class IOSDeviceGuardianTests(unittest.TestCase):
    def setUp(self):
        self.n=native.IOSMobileNativeTests(methodName='runTest')
        self.addCleanup(self.n.doCleanups);self.n.setUp()
        self.q=queries.PinnedDeviceCtlTests(methodName='runTest')
        self.addCleanup(self.q.doCleanups);self.q.setUp()
        self.c=self.n.c
        self.write_tool()
        self.pinned_guardian=self.guardian()
        declared=self.definition()
        selected=replace(self.c.selected,query_definition_digest=declared.definition_digest)
        self.operations=IOSMobileOperationStore(self.c.runs,selected,self.c.root/'guarded-operations')
        self.addCleanup(self.operations.close)

    def write_tool(self, extra='pass',udid=None):
        with patch.object(queries,'UDID',udid or self.c.selected.udid),patch.object(queries,'BUNDLE',self.c.selected.bundle_id):
            self.q.write_tool(extra)

    def definition(self):
        return IOSDeviceQueryDefinition(IOSDeviceTools(self.q.tool,hashlib.sha256(self.q.tool.read_bytes()).hexdigest()),
            queries.IDENTIFIER,self.c.selected.udid,self.c.selected.bundle_id,self.q.work,self.pinned_guardian)

    def guardian(self):
        if hasattr(self,'pinned_guardian'):return self.pinned_guardian
        return build_owned_guardian(self,self.q.root)

    @contextmanager
    def owned(self):
        with self.operations.admit(self.c.context,self.c.artifacts,self.c.baselines) as operation:
            for role in ('candidate','original'):self.operations.prepare(operation,role,
                cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10)
            with self.operations.native_owner(operation,self.n.device) as owner:yield owner

    def client(self,owner):
        client=self.definition().open_client(native_owner=owner,guardian=self.guardian())
        self.addCleanup(client.close)
        return client

    def test_fixed_query_runs_under_original_native_locks(self):
        with self.owned() as owner:
            client=self.client(owner)
            result=client.query('apps',cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10)
            self.assertTrue(result.data['apps'][0]['bundleIdentifier']==self.c.selected.bundle_id)
            self.assertEqual(client.active_processes,0)
            self.assertEqual(list(self.q.work.iterdir()),[])
            self.assertEqual(result.public()['nativeBindingDigest'],owner.binding_digest)
            self.assertFalse(result.public()['deviceCleanupConfirmed'])
            self.assertEqual(result.public()['executionAuthority'],'none')

    def test_expired_owner_and_different_query_definition_cannot_start_a_client(self):
        with self.owned() as owner:
            foreign=replace(self.definition(),identifier='aaaaaaaa-2222-3333-4444-555555555555')
            with self.assertRaises(IOSDeviceToolError):foreign.open_client(native_owner=owner,guardian=self.guardian())
        with self.assertRaises(IOSDeviceToolError):self.client(owner)
        self.assertFalse(self.q.requests.exists())

    def test_cancelled_query_collects_both_native_processes_and_preserves_operation(self):
        self.write_tool('time.sleep(30)')
        # The exact query definition includes the changed owned protocol tool.
        selected=replace(self.c.selected,query_definition_digest=self.definition().definition_digest)
        self.operations=IOSMobileOperationStore(self.c.runs,selected,self.c.root/'cancelled-operations')
        self.addCleanup(self.operations.close)
        with self.owned() as owner:
            client=self.client(owner);cancel=threading.Event()
            timer=threading.Timer(.2,cancel.set);timer.start();self.addCleanup(timer.join)
            with self.assertRaises(IOSDeviceToolError):
                client.query('details',cancellation=cancel,deadline_monotonic=time.monotonic()+5)
            self.assertEqual(client.active_processes,0)
            self.assertGreater(self.c.runs.status(self.c.context.operation_id)['reservedBytes'],0)
            self.assertEqual(list(self.q.work.iterdir()),[])

    def test_changed_guardian_is_rejected_before_any_coredevice_query(self):
        with self.owned() as owner:
            client=self.client(owner)
            path=self.q.root/'ios-device-guardian';path.write_bytes(path.read_bytes()+b'changed')
            with self.assertRaises(IOSDeviceToolError):
                client.query('details',cancellation=threading.Event(),deadline_monotonic=time.monotonic()+5)
            self.assertFalse(self.q.requests.exists())

    def test_another_pinned_guardian_cannot_replace_the_declared_native_tool(self):
        from reproof.ios_device_guardian import IOSDeviceGuardianTools
        foreign=IOSDeviceGuardianTools(self.q.tool,hashlib.sha256(self.q.tool.read_bytes()).hexdigest())
        with self.owned() as owner:
            with self.assertRaises(IOSDeviceToolError):
                self.definition().open_client(native_owner=owner,guardian=foreign)
        self.assertFalse(self.q.requests.exists())

    @staticmethod
    def lock_available(path):
        descriptor=os.open(path,os.O_RDWR)
        try:
            try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return False
            return True
        finally:os.close(descriptor)

    def wait_for(self,predicate):
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.01)
        self.fail('Owned native process did not reach the expected bounded state')

    def exercise_death(self,kind):
        root=self.c.root/('process-'+kind);root.mkdir(mode=0o700)
        marker=self.q.root/'live.json';release=self.q.root/'release'
        sentinel=self.q.root/'child.lock'
        udid=self.c.selected.udid+'-'+kind
        extra=("import fcntl\n"
            f"sentinel=open({str(sentinel)!r},'w');fcntl.flock(sentinel.fileno(),fcntl.LOCK_EX)\n"
            f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{'guardian':os.getppid(),'child':os.getpid()}}))\n"
            f"while not pathlib.Path({str(release)!r}).exists():time.sleep(.01)")
        self.write_tool(extra,udid=udid);guardian=self.guardian()
        worker=multiprocessing.get_context('spawn').Process(target=guarded_query_child,
            args=(root,self.c.body,udid,self.q.tool,guardian.path))
        try:
            worker.start();self.wait_for(lambda:marker.exists() and marker.stat().st_size>0)
            self.assertFalse(self.lock_available(sentinel))
            from reproof.live.authority import canonical_device_fingerprint
            locks=(root/'operations/operations/mobile-one/producer.lock',
                   root/'device-leases'/(canonical_device_fingerprint('ios-physical',udid)+'.lock'))
            self.assertTrue(all(not self.lock_available(path) for path in locks))
            if kind=='guardian':
                observed=json.loads(marker.read_bytes())
                os.kill(observed['guardian'],signal.SIGKILL)
            worker.kill();worker.join(3);self.assertFalse(worker.is_alive())
            if kind=='guardian':
                # Only the SDK double retains the original descriptions now.
                time.sleep(.1)
                self.assertFalse(self.lock_available(sentinel))
                self.assertTrue(all(not self.lock_available(path) for path in locks))
                release.write_text('finish owned child')
            self.wait_for(lambda:all(self.lock_available(path) for path in (*locks,sentinel)))
            self.assertTrue((root/'operations/operations/mobile-one/native.json').is_file())
            self.assertTrue((root/'operations/operations/mobile-one/original/App.app').is_dir())
        finally:
            release.write_text('finish owned child')
            if worker.is_alive():worker.kill();worker.join(5)
            worker.close()

    def test_parent_sigkill_makes_native_guardian_reap_the_sdk_before_lock_release(self):
        self.exercise_death('parent')

    def test_guardian_sigkill_leaves_original_locks_in_the_sdk_until_it_exits(self):
        self.exercise_death('guardian')

    def test_native_guardian_rejects_management_commands_and_reopened_locks(self):
        guardian=self.guardian()
        with self.owned() as owner,owner.borrow_descriptors() as borrowed:
            reader,writer=os.pipe()
            foreign=os.open(self.operations.operations/self.c.context.operation_id/'producer.lock',os.O_RDWR)
            try:
                for kind,producer in (('install',borrowed.producer_fd),('details',foreign)):
                    descriptors=(producer,borrowed.operation_directory_fd,borrowed.device_fd,
                                 borrowed.device_directory_fd,reader)
                    command=(str(guardian.path),*map(str,descriptors),self.c.selected.scope_digest,
                        str(self.q.tool),self.definition().tools.sha256,kind,queries.IDENTIFIER,
                        self.c.selected.bundle_id,str(self.q.work))
                    result=subprocess.run(command,pass_fds=descriptors,capture_output=True,timeout=3)
                    self.assertEqual(result.returncode,64)
                self.assertFalse(self.q.requests.exists())
            finally:
                os.close(reader);os.close(writer);os.close(foreign)
