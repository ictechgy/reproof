import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from reproloop.live.model import Lab
from reproloop.live.providers import demo_device
from reproloop.live.server import LiveServer
from reproloop.live.commands import main
from tests.test_live_recordings import recording


class CommandsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.lab=Lab([demo_device()],self.temp.name)
        self.server=LiveServer(self.lab);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.close_operations();self.lab.close_all();self.server.shutdown();self.thread.join();self.server.server_close();self.temp.cleanup()
    def run_command(self,args):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):status=main(args+['--server',self.server.origin])
        return status,json.loads(output.getvalue())
    def test_import_derive_export_and_queued_execution(self):
        r=recording(deviceId='demo',variables=[],events=[recording()['events'][0]])
        source=Path(self.temp.name)/'input.json';source.write_text(json.dumps(r))
        status,result=self.run_command(['live-recordings','import',str(source)])
        self.assertEqual(status,0);self.assertTrue(result['imported'])
        status,result=self.run_command(['live-recordings','derive',r['id'],'--speed','2'])
        self.assertEqual(status,0);derived=result['recording']
        target=Path(self.temp.name)/'derived.json'
        status,_=self.run_command(['live-recordings','export',derived['id'],'--output',str(target)])
        self.assertEqual(status,0);self.assertEqual(json.loads(target.read_text()),derived)
        status,job=self.run_command(['live-jobs','submit',derived['id'],'--request-id','cli-request','--repeats','2','--wait'])
        self.assertEqual(status,0,job);self.assertEqual(job['job']['state'],'succeeded');self.assertEqual(job['job']['completedRuns'],2)
    def test_existing_export_is_not_overwritten(self):
        r=recording(deviceId='demo',variables=[],events=[recording()['events'][0]])
        self.lab.import_recording(r,'local-owner');target=Path(self.temp.name)/'keep.json';target.write_text('keep')
        status,result=self.run_command(['live-recordings','export',r['id'],'--output',str(target)])
        self.assertEqual(status,2);self.assertEqual(target.read_text(),'keep')

if __name__=='__main__':unittest.main()
