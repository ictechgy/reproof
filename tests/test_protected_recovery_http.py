"""The shared HTTP and CLI surfaces drive the registered recovery owner."""
import http.client
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest

from reproloop.live.server import LiveServer
from tests import test_protected_recovery_service as support


class ProtectedRecoveryHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.ProtectedRecoveryServiceTests.setUpClass()
        cls.addClassCleanup(support.ProtectedRecoveryServiceTests.doClassCleanups)

    def setUp(self):
        self.f=support.ProtectedRecoveryServiceTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.server=LiveServer(self.f.lab,access=self.f.access,issue_workflow=self.f.bundle.workflow,
                              protected_recovery=self.f.recovery)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.close_operations();self.server.shutdown();self.thread.join(3);self.server.server_close()

    def request(self,path,body=None,role='operator'):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        try:
            headers={'Authorization':'Bearer '+self.f.tokens[role],'Origin':self.server.origin,'Content-Type':'application/json'}
            connection.request('GET' if body is None else 'POST',path,
                               body=None if body is None else json.dumps(body),headers=headers)
            response=connection.getresponse();return response.status,json.loads(response.read(65536))
        finally:connection.close()

    def cli(self,*args,role='operator'):
        result=subprocess.run([sys.executable,'-m','reproloop','protected-service',*args,
            '--server',self.server.origin,'--credential-stdin'],input=self.f.tokens[role]+'\n',
            text=True,capture_output=True,timeout=45,cwd=Path(__file__).resolve().parent.parent)
        self.assertNotIn(self.f.tokens[role],result.stdout+result.stderr)
        self.assertNotIn(self.f.f.f.config.serial,result.stdout+result.stderr)
        return result

    def test_authenticated_http_lists_bound_operations_without_device_effects(self):
        status,result=self.request('/api/protected-recovery/profiles/protected-android/operations',role='viewer')
        self.assertEqual(status,200,result)
        self.assertEqual(result['operations'][0]['operationId'],self.f.f.f.operation.operation_id)
        self.assertEqual(self.f.f.helper.server.requests,[])

    def test_generic_device_recovery_cannot_bypass_the_registered_operation(self):
        status,result=self.request('/api/devices/device/recover',{'projectId':self.f.f.f.config.registration.project['id']})
        self.assertEqual(status,409,result)
        self.assertEqual(result['error']['code'],'protected_operation_required')
        self.assertEqual(self.f.f.helper.server.requests,[])

    def test_cli_status_and_recover_use_actual_authenticated_service(self):
        operation=self.f.f.f.operation
        status=self.cli('android-status','--profile','protected-android','--operation',operation.operation_id)
        self.assertEqual(status.returncode,0,status.stderr+status.stdout)
        self.assertGreater(json.loads(status.stdout)['reservedBytes'],0)
        recovered=self.cli('android-recover','--profile','protected-android','--operation',operation.operation_id,
            '--request-digest',operation.request_digest,'--request-id','cli-recovery','--timeout-seconds','30','--wait','--wait-timeout','40')
        self.assertEqual(recovered.returncode,0,recovered.stderr+recovered.stdout)
        report=json.loads(recovered.stdout)
        self.assertEqual(report['job']['state'],'succeeded')
        self.assertEqual(report['job']['result']['reservedBytes'],0)

    def test_cli_viewer_cannot_start_recovery(self):
        operation=self.f.f.f.operation
        result=self.cli('android-recover','--profile','protected-android','--operation',operation.operation_id,
            '--request-digest',operation.request_digest,role='viewer')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.f.f.helper.server.requests,[])
