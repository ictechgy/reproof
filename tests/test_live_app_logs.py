import json
from pathlib import Path
import tempfile
import unittest
from reproof.live.model import Lab, LiveError
from reproof.live.providers import DemoProvider, demo_device
from tests.test_app_logs import snapshot


class LoggingProvider(DemoProvider):
    def collect_app_logs(self):return snapshot()


class LiveAppLogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        device=demo_device();device['factory']=LoggingProvider;device['capabilities']['automaticAppLogs']=True
        self.lab=Lab([device],self.root);self.session=self.lab.create_session('demo','owner','client')
    def tearDown(self):self.lab.close_all();self.temp.cleanup()

    def test_snapshot_is_saved_and_available_after_native_session_closes(self):
        sid=self.session['id'];value=self.lab.app_logs(sid,'owner')
        self.assertEqual(value,snapshot())
        self.lab.close_session(sid,'owner')
        self.lab._session(sid)['provider'].collect_app_logs=lambda:(_ for _ in ()).throw(AssertionError('closed provider must not be read'))
        self.assertEqual(self.lab.app_logs(sid,'owner'),value)
        stored=list((self.root/'app-logs'/sid).glob('*.json'))
        self.assertEqual(len(stored),1);self.assertEqual(json.loads(stored[0].read_text()),value)
        with self.assertRaises(LiveError):self.lab.app_logs(sid,'someone-else')

    def test_close_saves_logs_even_when_ui_never_requested_them(self):
        sid=self.session['id'];self.lab.close_session(sid,'owner')
        self.assertEqual(self.lab.app_logs(sid,'owner'),snapshot())

    def test_unavailable_logs_do_not_prevent_device_cleanup(self):
        sid=self.session['id']
        self.lab._session(sid)['provider'].collect_app_logs=lambda:(_ for _ in ()).throw(LiveError('app_log_unavailable','not ready'))
        self.assertEqual(self.lab.close_session(sid,'owner')['state'],'closed')
        self.assertEqual(self.lab.list_devices()[0]['state'],'available')
        with self.assertRaises(LiveError):self.lab.app_logs(sid,'owner')

    def test_original_session_retains_log_capability_after_candidate_reconfiguration(self):
        sid=self.session['id'];self.lab.app_logs(sid,'owner');self.lab.close_session(sid,'owner')
        self.lab.devices['demo']['capabilities']['automaticAppLogs']=False
        self.assertTrue(self.lab.get_session(sid)['capabilities']['automaticAppLogs'])
        self.assertEqual(self.lab.app_logs(sid,'owner'),snapshot())

if __name__=='__main__':unittest.main()
