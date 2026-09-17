"""A fresh authenticated helper is observed and collected under native recovery ownership."""
import json
import struct
import threading
import time
import unittest
from unittest.mock import patch

from reproloop import contracts
from reproloop.live.authority import NATIVE_PROTOCOL_VERSION,HELPER_VERSION
from reproloop.live.android_live import HELPER
from tests import test_android_recovery as support


class AndroidRecoveryHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AndroidDeviceRecoveryTests.setUpClass()
        cls.addClassCleanup(support.AndroidDeviceRecoveryTests.doClassCleanups)

    def setUp(self):
        self.f=support.AndroidDeviceRecoveryTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.server=self.f.f.fixture.server;self.server.concurrent=True
        self.configured=None;self.started=threading.Event();self.stopped=threading.Event()
        self.addCleanup(self.stopped.set);self.wrong_binding=False;self.pointer_active=False
        self.cancellation=threading.Event();self.cancel_on_status=False;self.clock_id='android-elapsed-realtime'
        original=self.server.shell_response
        def shell(connection,command):
            body=b''
            while True:
                kind,size=struct.unpack('<BI',self.server.read(connection,5))
                self.assertLessEqual(size,65536)
                chunk=self.server.read(connection,size)
                if kind==0:body+=chunk
                elif kind==4:break
                else:self.fail('Unexpected owned SDK stdin frame')
            if command.startswith(('run-as '+HELPER+' ').encode()):
                self.configured=json.loads(body);return b''
            if command==('am instrument -w -r '+HELPER+'/.LiveInstrumentation').encode():
                self.started.set();self.stopped.wait(4);return b'INSTRUMENTATION_CODE: -1\n'
            if command==('am force-stop '+HELPER).encode():
                if self.started.is_set():self.stopped.set()
                return b''
            if command==('pm path '+HELPER).encode():return b'package:/data/app/helper/base.apk\n'
            if command==b'sha256sum /data/app/helper/base.apk':
                return self.f.config.helper_digest.encode()+b'  /data/app/helper/base.apk\n'
            if command==('pm clear '+HELPER).encode():return b'Success\n'
            if self.configured is not None and command==b'ps -A -o NAME':return b'NAME\ninit\n'
            return original(command)
        def status(headers,body):
            value=self.configured
            self.assertIsNotNone(value)
            self.assertTrue(self.started.wait(2),'Owned helper did not start')
            self.assertIn(b'GET /status ',headers)
            self.assertTrue(('Authorization: Bearer '+value['token']).encode() in headers,
                            'Owned helper authentication mismatch')
            if self.cancel_on_status:self.cancellation.set()
            return {'ready':True,'stopped':False,'protocolVersion':NATIVE_PROTOCOL_VERSION,'helperVersion':HELPER_VERSION,
                'helperIncarnation':'wrong_helper' if self.wrong_binding else value['helperIncarnation'],
                'hostIncarnation':value['hostIncarnation'],'providerIncarnation':value['providerIncarnation'],
                'nativeIncarnation':'native_owned_recovery','nativeClockId':self.clock_id,'nativeTimeMs':1000000,
                'profileDigest':value['profileDigest'],'nativeDigest':contracts.digest(value['appProfile']),
                'targetPackage':value['targetPackage'],'generalProfile':True,
                'activePointerIds':[1] if self.pointer_active else []}
        self.server.shell_session=shell;self.server.helper_response=status

    def recover(self):
        from reproloop.android_recovery_helper import recover_android_helper
        f=self.f
        with f.operations.native_recovery(f.operation.operation_id,f.operation.request_digest,
            device=f.device,snapshot=f.snapshot,parent_grant=f.grant) as recovery:
            return recover_android_helper(f.operations,recovery,cancellation=self.cancellation,
                deadline_monotonic=time.monotonic()+10)

    def test_fresh_helper_identity_and_idle_input_are_observed_before_collection(self):
        from reproloop import android_recovery
        original=android_recovery.run_native_adb;failures=[]
        def capture(*args,**kwargs):
            result=original(*args,**kwargs)
            if result.returncode!=0:failures.append({'returncode':result.returncode,'bounded':result.bounded,
                'interrupted':result.interrupted,'stderr':result.stderr.decode(errors='replace')[:512]})
            return result
        with patch.object(android_recovery,'run_native_adb',side_effect=capture):result=self.recover()
        self.assertTrue(result.fresh_helper_verified and result.helper_collected,
            {'result':result,'failures':failures,'record':json.loads((self.f.operation.staging_root.parent/'recovery.json').read_bytes())})
        self.assertNotEqual(result.helper_incarnation,self.f.snapshot.prior_helper_incarnation)
        self.assertTrue(self.started.is_set() and self.stopped.is_set())
        self.assertTrue(self.f.device.requires_reconciliation)
        self.assertGreater(self.f.f.fixture.runs.status(self.f.operation.operation_id)['reservedBytes'],0)

    def test_wrong_helper_binding_does_not_verify_recovery(self):
        self.wrong_binding=True
        result=self.recover()
        self.assertFalse(result.fresh_helper_verified)
        self.assertTrue(result.helper_collected)
        self.assertTrue(self.f.device.requires_reconciliation)

    def test_active_pointer_state_does_not_verify_recovery(self):
        self.pointer_active=True
        result=self.recover()
        self.assertFalse(result.fresh_helper_verified)
        self.assertTrue(result.helper_collected)
        self.assertTrue(self.f.device.requires_reconciliation)

    def test_unqualified_native_clock_does_not_verify_recovery(self):
        self.clock_id='unsupported-clock'
        result=self.recover()
        self.assertFalse(result.fresh_helper_verified)
        self.assertTrue(result.helper_collected)

    def test_cancellation_during_status_keeps_recovery_unconfirmed(self):
        self.cancel_on_status=True
        result=self.recover()
        self.assertFalse(result.fresh_helper_verified)
        self.assertTrue(self.f.device.requires_reconciliation)
        self.assertGreater(self.f.f.fixture.runs.status(self.f.operation.operation_id)['reservedBytes'],0)

    def test_changed_configuration_body_is_rejected_before_helper_start(self):
        from reproloop import android_recovery_helper
        original=android_recovery_helper.run_native_adb
        def changed(*args,**kwargs):
            if kwargs.get('input_bytes'):
                kwargs['input_bytes']=b'{}'
            return original(*args,**kwargs)
        with patch.object(android_recovery_helper,'run_native_adb',side_effect=changed):result=self.recover()
        self.assertFalse(result.fresh_helper_verified)
        self.assertFalse(self.started.is_set())
        self.assertIsNone(self.configured)
