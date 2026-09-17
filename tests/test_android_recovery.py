"""Fixed recovery executes real SDK/guardian processes against owned protocol data."""
from dataclasses import replace
import hashlib
import json
import subprocess
import threading
import time
import unittest

from reproloop.repair_android_operation import AndroidOperationStore
from tests import test_android_native_process as support


class AndroidDeviceRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AndroidNativeProcessTests.setUpClass()
        cls.addClassCleanup(support.AndroidNativeProcessTests.doClassCleanups)

    def setUp(self):
        self.f=support.AndroidNativeProcessTests(methodName='runTest')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        probe=self.f.f.root/'recovery-inspector'
        source=self.f.f.root/'recovery-inspector.c'
        version=self.f.config.original_profile.data['artifact']['versionCode']
        source.write_text('#include <stdio.h>\nint main(void){puts('+json.dumps(
            "package: name='"+self.f.config.package+"' versionCode='"+str(version)+"'")+');return 0;}\n')
        made=subprocess.run(['/usr/bin/clang','-Wall','-Wextra','-Werror',str(source),'-o',str(probe)],
            stdin=subprocess.DEVNULL,capture_output=True,timeout=20)
        self.assertEqual(made.returncode,0)
        self.config=replace(self.f.config,native_guardian=self.f.guardian(),tools=replace(self.f.config.tools,
            package_inspector=probe,package_inspector_digest=hashlib.sha256(probe.read_bytes()).hexdigest()))
        self.operations=AndroidOperationStore(self.f.fixture.runs,self.config,self.f.f.root/'recovery-operations')
        self.addCleanup(lambda:self.operations.close(deadline_monotonic=time.monotonic()+2))
        self.scope=self.f.f.lab.begin_retained_device_scope('device',self.config.owner,'recovery-owner-scope',
            self.f.f.registration,application_id=self.config.application_id,build_id=self.config.original_build_id)
        self.device=self.scope._reservation._authority_handle;self.addCleanup(self.device.close)
        self.grant=self.device._parent_grant
        with self.operations.admit(self.f.f.context,self.f.fixture.fixture.blobs) as operation:
            self.operation=operation
            binding=self.operations.bind_native(operation,self.f.f.context,
                ownership_generation=self.device.generation,host_incarnation=self.device._authority.host_incarnation,
                helper_incarnation=self.device.helper_incarnation,provider_incarnation='recovery_original_provider')
            with self.operations.phase(operation,self.f.f.context,binding,'install') as phase:
                with self.operations.borrow_native_descriptors(operation,self.f.f.context,binding,phase,self.device) as descriptors:
                    from reproloop.adb_endpoint import adb_client_sandbox
                    from reproloop.android_native_calls import prepare_call
                    command=('/usr/bin/sandbox-exec','-p',adb_client_sandbox(self.config.tools.adb,
                        operation.staging_root,self.f.fixture.server.path),str(self.config.tools.adb),'-L',
                        'localfilesystem:'+str(self.f.fixture.server.path),'-s',self.config.serial,'shell','echo abandoned')
                    if not getattr(self,'discard_before_recovery',None):
                        prepare_call(self.operations,descriptors,command)
                if getattr(self,'seed_fixture',False) or getattr(self,'discard_before_recovery',None):
                    self.operations.complete_phase(phase,'a'*64)
            if getattr(self,'seed_fixture',False):
                from reproloop import contracts
                from reproloop.live.issue_sessions import _operation, fixture_reservation_id
                self.issue_id='mobile_'+contracts.digest({'context':self.f.f.context.digest,'attempt':1})[:40]
                plan=self.config.preparations[0].plan
                with self.operations.phase(operation,self.f.f.context,binding,'replay',1) as phase:
                    mode=getattr(self,'reservation_mode',None)
                    self.allocation=None
                    if mode!='absent':
                        self.allocation=self.config.service.fixtures.reserve(plan,owner=self.config.owner,device_id=self.config.device_id,
                            allocation_id=fixture_reservation_id(self.issue_id,plan.fixture_id) if mode else None)
                        if mode!='unstarted':
                            self.config.service.fixtures.prepare(plan,self.allocation,payload={},
                                operation_id=_operation(self.issue_id,plan.fixture_id,'prepare'))
                        self.config.service.fixtures.retain_for_cleanup(self.allocation)
                    issue={'schemaVersion':1,'issueId':self.issue_id,'state':'quarantined',
                        'projectDigest':self.config.registration.project_digest,'applicationId':self.config.application_id,
                        'deviceId':self.config.device_id,'buildId':'candidate_'+contracts.digest(
                            {'operation':operation.operation_id,'request':operation.request_digest})[:32],
                        'fixtures':[] if mode else [self.config.service.fixtures.status(self.allocation)],'cleanup':[],'createdAtMs':1}
                    if mode:
                        issue.update(fixtureReservationVersion=1,fixtureReservations=[{'fixtureId':plan.fixture_id,
                            'allocationId':fixture_reservation_id(self.issue_id,plan.fixture_id)}])
                    self.config.service._persist(issue)
                    self.operations.complete_phase(phase,'a'*64)
            if getattr(self,'discard_before_recovery',None):
                with self.operations.phase(operation,self.f.f.context,binding,'cleanup') as phase:
                    if self.discard_before_recovery=='partial':
                        from unittest.mock import patch
                        remove=self.operations._remove_staged
                        def interrupted(directory,name):
                            remove(directory,name)
                            raise OSError('owned normal-cleanup interruption')
                        with patch.object(self.operations,'_remove_staged',side_effect=interrupted):
                            with self.assertRaises(OSError):
                                self.operations.discard_staged(operation,phase)
                    else:
                        self.operations.discard_staged(operation,phase)
        if getattr(self,'seed_authority_operations',False):
            from reproloop.live.authority import ProviderResult
            admitted=self.device.admit_operation(operation_id='owned_recovery_uncertain',
                payload_digest='a'*64,session_id='recovery_test_session',sequence=1)
            self.device.admit_operation(operation_id='owned_recovery_queued',
                payload_digest='b'*64,session_id='recovery_test_session',sequence=2)
            self.original_permit=self.device.prepare_dispatch(admitted,provider_incarnation='recovery_original_provider')
            self.device.confirm_operation(self.original_permit,ProviderResult('owned_unknown_receipt','unknown','c'*64))
        self.device.revoke_dispatches();self.snapshot=self.device.recovery_snapshot()
        self.commands=[];self.keep_process=False;self.late_process=False;self.snapshots=0
        self.installed_path='/data/app/owned/base.apk';self.clear_success=True
        def response(command):
            record=json.loads((self.operation.staging_root.parent/'recovery.json').read_bytes())
            self.assertIsNotNone(record['activeStep'])
            self.commands.append(command.decode())
            if command==b'ps -A -o NAME':
                self.snapshots+=1
                present=self.keep_process or self.late_process and self.snapshots>1
                return b'NAME\n'+(self.config.package.encode()+b'\n' if present else b'init\n')
            if command==('pm clear '+self.config.package).encode():
                return b'Success\n' if self.clear_success else b'Failed\n'
            if command==('pm path '+self.config.package).encode():return ('package:'+self.installed_path+'\n').encode()
            if command==b'sha256sum /data/app/owned/base.apk':
                return self.config.original_profile.data['artifact']['sha256'].encode()+b'  /data/app/owned/base.apk\n'
            return b''
        self.f.fixture.server.shell_response=response

    def recover(self,cancellation=None):
        from reproloop.android_recovery import recover_android_device
        with self.operations.native_recovery(self.operation.operation_id,self.operation.request_digest,
            device=self.device,snapshot=self.snapshot,parent_grant=self.grant) as descriptors:
            return recover_android_device(self.operations,descriptors,
                cancellation=cancellation or threading.Event(),deadline_monotonic=time.monotonic()+10)

    def test_original_is_restored_and_cleared_under_retained_ownership(self):
        result=self.recover()
        self.assertTrue(result.processes_stopped and result.original_restored and result.data_cleared,
            {'result':result,'record':json.loads((self.operation.staging_root.parent/'recovery.json').read_bytes()),
             'commands':self.commands})
        self.assertFalse(result.ownership_released)
        self.assertEqual(self.f.fixture.server.installed_bytes,self.config.original_apk.read_bytes())
        self.assertTrue(self.device.requires_reconciliation)
        self.assertGreater(self.f.fixture.runs.status(self.operation.operation_id)['reservedBytes'],0)
        record=json.loads((self.operation.staging_root.parent/'recovery.json').read_bytes())
        self.assertEqual(record['state'],'device-restored')
        self.assertIsNone(record['activeStep'])
        self.assertEqual(len(record['steps']),9)

    def test_legacy_prepared_recovery_record_remains_readable(self):
        from reproloop.execution.wire import canonical
        self.assertTrue(self.recover().original_restored)
        path=self.operation.staging_root.parent/'recovery.json'
        record=json.loads(path.read_bytes());record['schemaVersion']=1;record.pop('recoveryMode')
        original=canonical(record);path.write_bytes(original)
        with self.operations.recovery(self.operation.operation_id,self.operation.request_digest) as inspected:
            self.assertEqual(inspected.state,'recovery-device-restored')
        self.assertEqual(path.read_bytes(),original)

    def test_remaining_target_process_prevents_install_and_release(self):
        self.keep_process=True
        result=self.recover()
        self.assertFalse(result.original_restored or result.ownership_released)
        self.assertIsNone(self.f.fixture.server.installed_bytes)
        self.assertTrue(self.device.requires_reconciliation)
        self.assertGreater(self.f.fixture.runs.status(self.operation.operation_id)['reservedBytes'],0)

    def test_cancelled_recovery_has_no_remote_effect(self):
        cancelled=threading.Event();cancelled.set()
        result=self.recover(cancelled)
        self.assertFalse(result.original_restored or result.ownership_released)
        self.assertEqual(self.f.fixture.server.requests,[])

    def test_process_reappearing_after_restore_is_not_reported_stopped(self):
        self.late_process=True
        result=self.recover()
        self.assertFalse(result.processes_stopped)
        self.assertFalse(result.ownership_released)
        self.assertEqual(json.loads((self.operation.staging_root.parent/'recovery.json').read_bytes())['state'],'failed')

    def test_traversal_in_device_reported_apk_path_is_rejected_before_hash_command(self):
        self.installed_path='/data/app/../../private/base.apk'
        result=self.recover()
        self.assertFalse(result.original_restored)
        self.assertFalse(any(command.startswith('sha256sum') for command in self.commands))

    def test_failed_clear_preserves_quarantine_and_reservation(self):
        self.clear_success=False
        result=self.recover()
        self.assertFalse(result.data_cleared or result.ownership_released)
        self.assertTrue(self.device.requires_reconciliation)
        self.assertGreater(self.f.fixture.runs.status(self.operation.operation_id)['reservedBytes'],0)

    def test_failed_attempt_can_retry_under_the_same_original_recovery_binding(self):
        self.keep_process=True
        self.assertFalse(self.recover().original_restored)
        self.keep_process=False
        result=self.recover()
        self.assertTrue(result.processes_stopped and result.original_restored and result.data_cleared)
        record=json.loads((self.operation.staging_root.parent/'recovery.json').read_bytes())
        self.assertEqual(record['attempt'],2)
        self.assertNotEqual(record['historyDigest'],'0'*64)

    def test_recovery_dispatch_cannot_be_reused_for_an_arbitrary_sdk_command(self):
        from unittest.mock import patch
        from reproloop import android_recovery
        original=android_recovery.run_native_adb
        def changed(operations,descriptors,guardian,client,arguments,**kwargs):
            return original(operations,descriptors,guardian,client,('shell','echo unauthorized'),**kwargs)
        with patch.object(android_recovery,'run_native_adb',side_effect=changed):
            result=self.recover()
        self.assertFalse(result.processes_stopped or result.original_restored or result.ownership_released)
        self.assertEqual(self.f.fixture.server.requests,[])

    def test_attempt_limit_does_not_overwrite_the_last_valid_recovery_record(self):
        from reproloop.execution.wire import canonical
        self.assertTrue(self.recover().original_restored)
        path=self.operation.staging_root.parent/'recovery.json'
        record=json.loads(path.read_bytes());record['attempt']=32
        original=canonical(record);path.write_bytes(original)
        before=len(self.f.fixture.server.requests)
        self.assertFalse(self.recover().original_restored)
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(len(self.f.fixture.server.requests),before)

    def test_incomplete_step_record_cannot_claim_device_restoration(self):
        from reproloop.execution.wire import canonical
        self.assertTrue(self.recover().original_restored)
        path=self.operation.staging_root.parent/'recovery.json'
        record=json.loads(path.read_bytes());record['steps'].pop('confirm-cleared')
        path.write_bytes(canonical(record))
        self.assertEqual(self.operations.status(self.operation.operation_id)['state'],'record-invalid')
