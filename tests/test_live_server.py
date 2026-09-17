import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from reproloop.live.model import Lab
from reproloop.live.providers import demo_device
from reproloop.live.server import LiveServer


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.lab=Lab([demo_device()],Path(self.temp.name))
        self.server=LiveServer(self.lab);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.cookie=None
    def tearDown(self):
        self.server.close_operations();self.lab.close_all();self.server.shutdown();self.thread.join();self.server.server_close();self.temp.cleanup()
    def request(self,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        values={'Content-Type':'application/json'}
        if self.cookie:values['Cookie']=self.cookie
        values.update(headers or {})
        connection.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,values)
        response=connection.getresponse();data=response.read()
        if response.getheader('Set-Cookie'):self.cookie=response.getheader('Set-Cookie').split(';')[0]
        status=response.status;mime=response.getheader('Content-Type');connection.close()
        return status,json.loads(data) if 'json' in mime else data
    def test_auth_host_origin_and_payload_guards(self):
        self.assertEqual(self.request('/api/devices')[0],401)
        self.assertEqual(self.request('/',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('/',headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/')[0],200)
        self.assertEqual(self.request('/api/devices',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.request('/api/sessions',{'deviceId':'demo','clientId':'c'},headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('/api/devices')[0],200)
    def test_record_export_replay_and_restart(self):
        self.request('/');status,value=self.request('/api/sessions',{'deviceId':'demo','clientId':'c'})
        self.assertEqual(status,201);s=value['session'];path='/api/sessions/'+s['id'];control={'controllerId':'c','epoch':s['epoch']}
        self.assertEqual(self.request(path+'/recordings/start',dict(control,reset=True))[0],200)
        f=self.request(path+'/frame')[1]
        command=dict(control,sequence=1,commandId='tap-1',frameId=f['id'],geometryVersion=f['geometryVersion'],action='tap',payload={'x':.5,'y':.5})
        self.assertEqual(self.request(path+'/input',command)[1]['receipt']['status'],'injected')
        r=self.request(path+'/recordings/stop',control)[1]['recording']
        exported=self.request('/api/recordings/'+r['id']+'/export')[1]
        self.assertEqual(r,exported);self.assertTrue(r['replayable'])
        code=self.request('/api/recordings/'+r['id']+'/script')[1].decode()
        compile(code,'replay.py','exec');self.assertIn(r['digest'],code)
        self.assertEqual(self.request(path+'/replay',dict(control,recordingId=r['id'],variables={}))[0],200)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            s=self.request(path)[1]['session']
            if s['replay']['state']!='running':break
            time.sleep(.01)
        self.assertEqual(s['replay']['state'],'actions_replayed')
        self.assertEqual(self.lab._session(s['id'])['provider'].count,1)
        self.assertEqual(s['controllerId'],'c');self.assertGreater(s['epoch'],control['epoch'])
        self.assertEqual(self.request(path+'/input',command)[0],409)
        self.assertEqual(self.request(path+'/close',{'controllerId':'c','epoch':s['epoch']})[0],200)
        self.assertEqual(self.request(path+'/frame')[0],503)
        restored=Lab([demo_device()],Path(self.temp.name));self.assertEqual(restored.recording(r['id'],'local-owner'),r)
        import subprocess,sys
        replay=subprocess.run([sys.executable,'-c',code,'--server',self.server.origin],capture_output=True,timeout=10)
        self.assertEqual(replay.returncode,0,replay.stderr.decode())
        self.assertEqual(self.lab.list_devices()[0]['state'],'available')
    def test_empty_recording_and_corrupt_export_rejected(self):
        self.request('/');s=self.request('/api/sessions',{'deviceId':'demo','clientId':'c'})[1]['session']
        path='/api/sessions/'+s['id'];control={'controllerId':'c','epoch':s['epoch']}
        self.request(path+'/recordings/start',dict(control,reset=True));r=self.request(path+'/recordings/stop',control)[1]['recording']
        self.assertFalse(r['replayable'])
        self.assertEqual(self.request(path+'/replay',dict(control,recordingId=r['id'],variables={}))[0],409)
        file=Path(self.temp.name)/'recordings'/f'{r["id"]}.json';value=json.loads(file.read_text());value['recording']['replayable']=True;file.write_text(json.dumps(value))
        self.assertEqual(self.request('/api/recordings/'+r['id'])[0],409)

if __name__=='__main__':unittest.main()
