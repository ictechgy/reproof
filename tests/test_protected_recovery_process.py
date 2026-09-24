"""Complete live-serve processes recover the original journal across process loss."""
import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from reproof.live.access import AccessStore
from reproof.live.client import IssueClient
from tests import test_android_recovery as recovery_fixture
from tests import test_repair_android as device_fixture
from tests import test_protected_recovery_service as support


class ProtectedRecoveryProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.ProtectedRecoveryServiceTests.setUpClass()
        cls.addClassCleanup(support.ProtectedRecoveryServiceTests.doClassCleanups)

    def setUp(self):
        self.f=support.ProtectedRecoveryServiceTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups)
        with patch.object(device_fixture.AndroidAdapterTests,'process_recovery_fixture',True,create=True), \
                patch.object(recovery_fixture.AndroidDeviceRecoveryTests,'seed_fixture',True,create=True):
            self.f.setUp()
        self.root=self.f.root
        self.config=self.f.f.f.config
        self.operation=self.f.f.f.operation
        self.processes=[];self.streams=[]
        self.addCleanup(self.stop_all)
        self.coordinator=AccessStore(self.root/'coordinator-v2');self.addCleanup(self.coordinator.close)
        store=self.coordinator;store.bootstrap_administrator('admin')
        store.register_project('admin',self.config.registration.project)
        store.assign_device('admin',self.config.device_id,project_id=self.config.registration.project['id'])
        store.create_identity('admin','operator');store.grant_membership('admin',self.config.registration.project['id'],'operator','operator')
        self.token=store.issue_principal_credential('admin','operator',lifetime_seconds=600)['token']
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        self.origin=f'http://127.0.0.1:{port}'
        def write(name,value):
            path=self.root/name;path.write_text(json.dumps(value));path.chmod(0o600);return path
        project=write('process-project.json',self.config.registration.project)
        policy=write('process-collection.json',self.config.registration.collection_policy)
        self.shared=write('process-shared.json',{'schemaVersion':2,'kind':'reproof-shared-coordinator',
            'stateRoot':str(store.root),'listen':{'host':'127.0.0.1','port':port,'origin':self.origin,
                'tlsCertificateFile':None,'tlsPrivateKeyFile':None},
            'projects':[{'projectFile':str(project),'collectionPolicyFile':str(policy)}],'browserSessionSeconds':600})
        issue=copy.deepcopy(self.f.issue)
        declarations={item['id']:item for item in self.config.registration.project['fixtures']}
        issue['projects'][0]['fixtures']=[{'applicationId':item.plan.application_id,'fixtureId':item.plan.fixture_id,
            'endpointId':declarations[item.plan.cleanup_recipe_id]['endpointId'],
            'baseUrl':f'http://127.0.0.1:{self.f.f.f.f.f.remote.port}',
            'checkRecipeIds':list(item.plan.check_recipe_ids),'cleanupRecipeId':item.plan.cleanup_recipe_id,
            'payload':item.payload} for item in self.config.preparations]
        self.issue=write('process-issues.json',issue)
        self.protected=write('process-protected.json',self.f.document)
        self.output=self.f.lab.output
        self.f.recovery.close();self.f.bundle.close();self.f.lab.close_all();self.f.lab.authority.close()
        base=self.f.f.f.f.f
        base.fixtures.close();base.registry.close()
        binary=self.root/'process-bin';binary.mkdir()
        self.default_probe=self.root/'unexpected-default-adb'
        adb=binary/'adb';adb.write_text('#!/usr/bin/python3\nfrom pathlib import Path\nPath('+repr(str(self.default_probe))+').write_text("called")\nraise SystemExit(73)\n');adb.chmod(0o700)
        self.environment=os.environ.copy();self.environment['PATH']=str(binary)+os.pathsep+self.environment.get('PATH','')

    def start_server(self):
        number=len(self.processes)+1
        stdout=(self.root/f'process-server-{number}.out').open('x')
        stderr=(self.root/f'process-server-{number}.err').open('x')
        self.streams.extend((stdout,stderr))
        process=subprocess.Popen([sys.executable,'-m','reproof','live-serve',
            '--shared-config',str(self.shared),'--issue-config',str(self.issue),
            '--protected-recovery-config',str(self.protected),'--output',str(self.output)],
            stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,env=self.environment,
            cwd=Path(__file__).resolve().parent.parent,start_new_session=True)
        self.processes.append(process)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if process.poll() is not None:
                self.fail({'exitCode':process.returncode,'error':(self.root/f'process-server-{number}.err').read_text()[-1800:]})
            try:
                self.client=IssueClient(self.origin,self.token)
                return process
            except OSError:
                time.sleep(.05)
        self.fail('Owned live-serve process did not become available')

    def stop_all(self):
        for process in self.processes:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=5)
        for stream in self.streams:
            stream.close()
            body=Path(stream.name).read_text()
            self.assertTrue(self.token not in body,'Owned service output exposed a credential')
            self.assertTrue(self.config.serial not in body,'Owned service output exposed a device serial')
            self.assertNotIn('ResourceWarning',body)

    def start_recovery(self,request_id):
        return self.client.call('/api/protected-recovery/profiles/protected-android/operations/'+
            self.operation.operation_id+'/recover',{'requestId':request_id,
            'requestDigest':self.operation.request_digest,'timeoutSeconds':45})['job']

    def wait_job(self,job):
        deadline=time.monotonic()+50
        while time.monotonic()<deadline:
            value=self.client.call('/api/protected-recovery/jobs/'+job['id'])['job']
            if value['state'] in ('succeeded','failed','cancelled'):return value
            time.sleep(.05)
        self.fail('Owned process recovery did not finish')

    def check_original_locks(self, *, held):
        descriptors=[]
        try:
            for path in (self.operation.staging_root.parent/'producer.lock',self.f.f.device_path):
                descriptor=os.open(path,os.O_RDWR|os.O_NOFOLLOW);descriptors.append(descriptor)
                if held:
                    with self.assertRaises(BlockingIOError):fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                else:
                    fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:
            for descriptor in descriptors:os.close(descriptor)

    def wait_for_collected_locks(self):
        deadline=time.monotonic()+10
        while True:
            try:
                self.check_original_locks(held=False);return
            except BlockingIOError:
                if time.monotonic()>=deadline:self.fail('Original native ownership was not collected')
                time.sleep(.02)

    def test_complete_server_cli_starts_without_default_adb_and_recovers(self):
        self.start_server()
        self.assertFalse(self.default_probe.exists())
        self.assertEqual(self.f.f.helper.server.requests,[])
        result=self.wait_job(self.start_recovery('process-recovery'))
        self.assertEqual(result['state'],'succeeded',result)
        self.assertEqual(result['result']['reservedBytes'],0)
        record=json.loads((self.operation.staging_root.parent/'recovery.json').read_bytes())
        self.assertEqual(record['fixtureRecovery']['total'],1)
        self.assertEqual(record['fixtureRecovery']['completed'],1)
        devices=self.client.call('/api/devices')['devices']
        self.assertEqual(devices[0]['state'],'available')
        self.assertTrue(devices[0]['capabilities']['recoveryOnly'])
        self.assertNotIn('_recoveryOnly',devices[0])
        self.assertFalse(self.default_probe.exists())

    def test_sigkill_then_new_server_process_recovers_original_operation(self):
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        server=self.f.f.helper.server;original=server.shell_session
        def paused(connection,command):
            output=original(connection,command)
            if command==('am force-stop '+self.config.package).encode() and not release.is_set():
                entered.set();release.wait(15)
            return output
        server.shell_session=paused
        process=self.start_server();self.start_recovery('before-process-kill')
        self.assertTrue(entered.wait(10))
        self.check_original_locks(held=True)
        process.kill();self.assertEqual(process.wait(timeout=5),-signal.SIGKILL)
        release.set()
        self.wait_for_collected_locks()
        self.assertGreater(self.f.f.runs.status(self.operation.operation_id)['reservedBytes'],0)
        self.start_server()
        result=self.wait_job(self.start_recovery('after-process-kill'))
        self.assertEqual(result['state'],'succeeded',result)
        self.assertEqual(result['result']['reservedBytes'],0)
        self.assertFalse(self.default_probe.exists())

    def test_process_loss_after_staging_discard_resumes_without_device_replay(self):
        blocked=[];ready=threading.Event()
        server=self.f.f.helper.server;original=server.helper_response
        def pause_release(headers,body):
            value=original(headers,body)
            if not ready.is_set():
                descriptor=os.open(self.f.f.runs.root/'.control-lock',os.O_RDWR|os.O_NOFOLLOW)
                fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                blocked.append(descriptor);ready.set()
            return value
        server.helper_response=pause_release
        process=self.start_server()
        try:
            self.start_recovery('before-release-kill')
            self.assertTrue(ready.wait(10))
            path=self.operation.staging_root.parent/'finalization.json'
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                if path.exists() and json.loads(path.read_bytes()).get('state')=='sanitized':break
                time.sleep(.02)
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_bytes())['state'],'sanitized')
            self.assertEqual(list(self.operation.staging_root.iterdir()),[])
            self.assertGreater(self.f.f.runs.status(self.operation.operation_id)['reservedBytes'],0)
            self.check_original_locks(held=True)
            process.kill();self.assertEqual(process.wait(timeout=5),-signal.SIGKILL)
        finally:
            for descriptor in blocked:os.close(descriptor)
            server.helper_response=original
        self.wait_for_collected_locks()
        requests=len(server.requests)
        self.start_server()
        result=self.wait_job(self.start_recovery('after-release-kill'))
        self.assertEqual(result['state'],'succeeded',result)
        self.assertEqual(result['result']['reservedBytes'],0)
        self.assertEqual(len(server.requests),requests)

    def test_process_loss_after_run_budget_commit_finishes_device_release(self):
        blocked=[];ready=threading.Event()
        server=self.f.f.helper.server;original=server.helper_response
        def pause_release(headers,body):
            value=original(headers,body)
            if not ready.is_set():
                descriptor=os.open(self.f.f.runs.root/'.control-lock',os.O_RDWR|os.O_NOFOLLOW)
                fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                blocked.append(descriptor);ready.set()
            return value
        server.helper_response=pause_release
        process=self.start_server();authority=None
        try:
            self.start_recovery('before-device-release-kill')
            self.assertTrue(ready.wait(10))
            path=self.operation.staging_root.parent/'finalization.json';deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                if path.exists() and json.loads(path.read_bytes()).get('state')=='sanitized':break
                time.sleep(.01)
            self.assertEqual(json.loads(path.read_bytes())['state'],'sanitized')
            authority=sqlite3.connect(self.root/'host-authority-v1'/'authority.sqlite3',isolation_level=None)
            authority.execute('BEGIN IMMEDIATE')
            for descriptor in blocked:os.close(descriptor)
            blocked.clear()
            deadline=time.monotonic()+1
            while time.monotonic()<deadline and self.f.f.runs.status(self.operation.operation_id)['reservedBytes']:
                time.sleep(.005)
            self.assertEqual(self.f.f.runs.status(self.operation.operation_id)['reservedBytes'],0)
            self.check_original_locks(held=True)
            process.kill();self.assertEqual(process.wait(timeout=5),-signal.SIGKILL)
        finally:
            if authority is not None:authority.rollback();authority.close()
            for descriptor in blocked:os.close(descriptor)
            server.helper_response=original
        self.wait_for_collected_locks()
        requests=len(server.requests)
        self.start_server()
        result=self.wait_job(self.start_recovery('after-device-release-kill'))
        self.assertEqual(result['state'],'succeeded',result)
        self.assertTrue(result['result']['ownershipReleased'])
        self.assertEqual(len(server.requests),requests)
