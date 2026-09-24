"""Real writer process exits exercise metadata publication and orphan cleanup."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reproof.repair_android_operation import AndroidOperationError, _read_json_at, _write_new_at
from tests import test_android_recovery_finalization as support


WRITER = r'''
import json,os,signal,sys
from reproof import repair_android_operation as journal
directory=int(sys.argv[1]);name=sys.argv[2];mode=sys.argv[3]
value=json.loads(sys.argv[4])
write,link,replace=os.write,os.link,os.replace
if mode in ('partial','sigkill-partial'):
    def interrupted(descriptor,body):
        write(descriptor,body[:max(1,len(body)//2)]);os.fsync(descriptor)
        if mode=='sigkill-partial':os.kill(os.getpid(),signal.SIGKILL)
        os._exit(71)
    journal.os.write=interrupted
elif mode=='before-link':
    def interrupted(*args,**kwargs):os._exit(72)
    journal.os.link=interrupted
elif mode=='after-link':
    def interrupted(*args,**kwargs):
        link(*args,**kwargs);os._exit(73)
    journal.os.link=interrupted
elif mode=='before-replace':
    def interrupted(*args,**kwargs):os._exit(74)
    journal.os.replace=interrupted
if mode=='before-replace':journal._replace_at(directory,name,value)
else:journal._write_new_at(directory,name,value)
os._exit(99)
'''


def stop_writer(directory, name, mode, value):
    descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        return subprocess.run([sys.executable,'-c',WRITER,str(descriptor),name,mode,json.dumps(value)],
            pass_fds=(descriptor,),cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL,capture_output=True,timeout=10)
    finally:os.close(descriptor)


class AndroidMetadataWriterCrashTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def read(self,name):
        descriptor=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
        try:return _read_json_at(descriptor,name)
        finally:os.close(descriptor)

    def test_process_exit_during_new_record_write_never_publishes_partial_json(self):
        result=stop_writer(self.root,'recovery.json','partial',{'state':'prepared','contextDigest':'a'*64})
        self.assertEqual(result.returncode,71,result.stderr.decode())
        self.assertFalse((self.root/'recovery.json').exists())
        self.assertEqual(len(list(self.root.glob('.record-*'))),1)

    def test_process_exit_before_link_leaves_only_unpublished_scratch(self):
        result=stop_writer(self.root,'recovery.json','before-link',{'state':'prepared'})
        self.assertEqual(result.returncode,72,result.stderr.decode())
        self.assertFalse((self.root/'recovery.json').exists())
        self.assertEqual(len(list(self.root.glob('.record-*'))),1)

    def test_sigkill_during_new_record_write_leaves_no_partial_target(self):
        result=stop_writer(self.root,'recovery.json','sigkill-partial',{'state':'prepared','contextDigest':'a'*64})
        self.assertEqual(result.returncode,-9,result.stderr.decode())
        self.assertFalse((self.root/'recovery.json').exists())
        self.assertEqual(len(list(self.root.glob('.record-*'))),1)

    def test_process_exit_after_link_keeps_a_readable_complete_record(self):
        value={'state':'prepared','contextDigest':'a'*64}
        result=stop_writer(self.root,'recovery.json','after-link',value)
        self.assertEqual(result.returncode,73,result.stderr.decode())
        self.assertEqual((self.root/'recovery.json').stat().st_nlink,2)
        self.assertEqual(self.read('recovery.json'),value)

    def test_atomic_new_record_does_not_replace_an_existing_record(self):
        descriptor=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
        try:
            _write_new_at(descriptor,'state.json',{'state':'original'})
            before=(self.root/'state.json').read_bytes()
            with self.assertRaises((FileExistsError,AndroidOperationError)):
                _write_new_at(descriptor,'state.json',{'state':'replacement'})
            self.assertEqual((self.root/'state.json').read_bytes(),before)
            self.assertEqual(list(self.root.glob('.record-*')),[])
        finally:os.close(descriptor)

    def test_external_hardlink_is_not_an_acceptable_pending_metadata_link(self):
        descriptor=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
        try:_write_new_at(descriptor,'state.json',{'state':'original'})
        finally:os.close(descriptor)
        os.link(self.root/'state.json',self.root/'unrelated.json')
        with self.assertRaises(AndroidOperationError):self.read('state.json')


class AndroidMetadataRecoveryCrashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AndroidRecoveryFinalizationTests.setUpClass()
        cls.addClassCleanup(support.AndroidRecoveryFinalizationTests.doClassCleanups)

    def setUp(self):
        self.f=support.AndroidRecoveryFinalizationTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups);self.f.setUp()

    def clean_scratch(self):
        f=self.f
        with f.operations._admission(),f.operations._recovery_files(
            f.f.operation.operation_id,f.f.operation.request_digest,retire_metadata=True):
            pass

    def test_dead_writer_scratch_is_removed_without_adopting_its_proposed_state(self):
        path=self.f.directory/'state.json';original=path.read_bytes()
        proposed=json.loads(original);proposed['stage']='discarded'
        result=stop_writer(self.f.directory,'state.json','before-replace',proposed)
        self.assertEqual(result.returncode,74,result.stderr.decode())
        scratch=list(self.f.directory.glob('.record-*'));self.assertEqual(len(scratch),1)
        with self.assertRaises(AndroidOperationError):
            with self.f.operations.recovery(self.f.f.operation.operation_id,self.f.f.operation.request_digest):pass
        self.assertTrue(scratch[0].exists())
        self.clean_scratch()
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(list(self.f.directory.glob('.record-*')),[])
        self.assertGreater(self.f.runs.status(self.f.f.operation.operation_id)['reservedBytes'],0)
        self.assertTrue(self.f.finalize().ownership_released)

    def test_full_recovery_retires_unpublished_initial_recovery_record(self):
        result=stop_writer(self.f.directory,'recovery.json','partial',{'state':'device-restored'})
        self.assertEqual(result.returncode,71,result.stderr.decode())
        self.assertTrue(self.f.finalize().ownership_released)
        self.assertEqual(list(self.f.directory.glob('.record-*')),[])

    def test_scratch_link_to_unrelated_file_is_preserved_and_rejected(self):
        target=self.f.directory/'notes.txt';target.write_bytes(b'preserved owned notes');target.chmod(0o600)
        scratch=self.f.directory/('.record-'+'a'*32);os.link(target,scratch)
        with self.assertRaises(AndroidOperationError):self.clean_scratch()
        self.assertTrue(scratch.exists())
        self.assertEqual(target.read_bytes(),b'preserved owned notes')

    def test_wrong_request_cannot_retire_metadata_scratch(self):
        scratch=self.f.directory/('.record-'+'a'*32);scratch.write_bytes(b'partial');scratch.chmod(0o600)
        with self.assertRaises(AndroidOperationError):
            with self.f.operations._recovery_files(self.f.f.operation.operation_id,'0'*64,retire_metadata=True):pass
        self.assertTrue(scratch.exists())

    def test_published_state_with_pending_link_is_normalized_after_binding_checks(self):
        path=self.f.directory/'state.json';original=path.read_bytes();value=json.loads(original)
        path.unlink()
        result=stop_writer(self.f.directory,'state.json','after-link',value)
        self.assertEqual(result.returncode,73,result.stderr.decode())
        self.assertEqual(path.stat().st_nlink,2)
        self.clean_scratch()
        self.assertEqual(path.stat().st_nlink,1)
        self.assertEqual(path.read_bytes(),original)
        self.assertTrue(self.f.finalize().ownership_released)

    def test_native_workspace_scratch_does_not_replace_committed_slot_state(self):
        directory=self.f.directory/'native-calls';path=directory/'state.json';original=path.read_bytes()
        result=stop_writer(directory,'state.json','before-replace',{'state':'discarded'})
        self.assertEqual(result.returncode,74,result.stderr.decode())
        self.clean_scratch()
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(list(directory.glob('.record-*')),[])

    def test_active_producer_prevents_scratch_retirement(self):
        f=self.f
        with f.operations.native_recovery(f.f.operation.operation_id,f.f.operation.request_digest,
            device=f.device,snapshot=f.device.recovery_snapshot(),parent_grant=f.grant()):
            scratch=f.directory/('.record-'+'a'*32);scratch.write_bytes(b'partial');scratch.chmod(0o600)
            with self.assertRaises(AndroidOperationError):self.clean_scratch()
            self.assertTrue(scratch.exists())

    def test_symlink_and_oversized_scratch_are_preserved_and_rejected(self):
        target=self.f.directory/'intent.json';original=target.read_bytes()
        scratch=self.f.directory/('.record-'+'a'*32);scratch.symlink_to(target)
        with self.assertRaises(AndroidOperationError):self.clean_scratch()
        self.assertTrue(scratch.is_symlink());self.assertEqual(target.read_bytes(),original)
        scratch.unlink()
        descriptor=os.open(scratch,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:os.ftruncate(descriptor,512*1024+1)
        finally:os.close(descriptor)
        with self.assertRaises(AndroidOperationError):self.clean_scratch()
        self.assertEqual(scratch.stat().st_size,512*1024+1)

    def test_legacy_partial_published_record_remains_untrusted_and_preserved(self):
        path=self.f.directory/'recovery.json';body=b'{"schemaVersion":1,'
        path.write_bytes(body);path.chmod(0o600)
        with self.assertRaises(AndroidOperationError):self.f.finalize()
        self.assertEqual(path.read_bytes(),body)
        self.assertEqual(self.f.helper.server.requests,[])
        self.assertGreater(self.f.runs.status(self.f.f.operation.operation_id)['reservedBytes'],0)
