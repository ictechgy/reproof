import copy
from pathlib import Path
import tempfile
import time
import unittest
from reproloop.core import digest
from reproloop.live.model import Lab
from reproloop.live.providers import demo_device
from tests import test_live_server as http_helpers
from tests.test_live_recordings import recording


class OperationsHttpTests(unittest.TestCase):
    setUp=http_helpers.HttpTests.setUp
    tearDown=http_helpers.HttpTests.tearDown
    request=http_helpers.HttpTests.request
    def imported(self):
        value=recording(deviceId='demo',variables=[],events=[recording()['events'][0]],applicationIdentity=None)
        status,response=self.request('/api/recordings/import',{'recording':value})
        self.assertEqual(status,201,response);return response['recording']
    def test_import_is_idempotent_and_rejects_modified_payload(self):
        self.request('/');r=self.imported()
        status,value=self.request('/api/recordings/import',{'recording':r})
        self.assertEqual(status,200);self.assertFalse(value['imported'])
        changed=copy.deepcopy(r);changed['events'][0]['payload']['x']=.4
        self.assertEqual(self.request('/api/recordings/import',{'recording':changed})[0],400)
        changed['digest']=digest({k:v for k,v in changed.items() if k!='digest'})
        self.assertEqual(self.request('/api/recordings/import',{'recording':changed})[0],409)
        self.assertEqual(self.request('/api/recordings')[1]['recordings'][0],r)
    def test_derived_copy_keeps_source_unchanged(self):
        self.request('/');r=self.imported()
        status,response=self.request('/api/recordings/'+r['id']+'/derive',{'speed':2})
        self.assertEqual(status,201,response);derived=response['recording']
        self.assertNotEqual(r['id'],derived['id']);self.assertTrue(derived['replayable'])
        self.assertEqual(derived['events'][0]['offsetMs'],50)
        self.assertEqual(self.request('/api/recordings/'+r['id'])[1]['recording'],r)
    def test_two_servers_cannot_operate_one_output_store(self):
        from reproloop.live.server import LiveServer
        from reproloop.core import ContractError
        other=Lab([demo_device()],self.temp.name)
        with self.assertRaises(ContractError):LiveServer(other)
    def test_sessions_heartbeat_events_and_health(self):
        self.request('/');s=self.request('/api/sessions',{'deviceId':'demo','clientId':'c'})[1]['session']
        self.assertEqual(self.request('/api/sessions')[1]['sessions'][0]['id'],s['id'])
        self.assertEqual(self.request('/api/sessions/'+s['id']+'/heartbeat',{'clientId':'c'})[0],200)
        self.assertEqual(self.request('/api/sessions/'+s['id']+'/events')[1]['events'][0]['type'],'session_allocated')
        health=self.request('/api/health')[1];self.assertEqual(health['devices']['busy'],1)
    def test_job_waits_for_manual_session_and_runs_after_release(self):
        self.request('/');r=self.imported()
        s=self.request('/api/sessions',{'deviceId':'demo','clientId':'manual'})[1]['session']
        status,value=self.request('/api/jobs',{'recordingId':r['id'],'variables':{},'requestId':'queue-1','repeats':2,'timeoutSeconds':20})
        self.assertEqual(status,202,value);job=value['job'];self.assertEqual(job['state'],'queued')
        self.assertEqual(self.request('/api/jobs/'+job['id']+'/report')[0],200)
        self.request('/api/sessions/'+s['id']+'/close',{'controllerId':'manual','epoch':s['epoch']})
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            job=self.request('/api/jobs/'+job['id'])[1]['job']
            if job['state'] in {'succeeded','failed','cancelled'}:break
            time.sleep(.05)
        self.assertEqual(job['state'],'succeeded',job);self.assertEqual(job['completedRuns'],2)
        self.assertEqual(self.lab.list_devices()[0]['state'],'available')

if __name__=='__main__':unittest.main()
