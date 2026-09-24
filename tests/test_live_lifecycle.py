import tempfile
import unittest
from reproof.live.model import Lab,LiveError
from tests.test_live_model import Provider


class Clock:
    def __init__(self):self.now=100.0
    def __call__(self):return self.now
    def advance(self,seconds):self.now+=seconds


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.clock=Clock();self.providers=[]
        def factory():
            provider=Provider();self.providers.append(provider);return provider
        self.devices=[{'id':'test','name':'test','kind':'demo','capabilities':{'actions':['tap','reset'],'recovery':True},'factory':factory}]
        self.lab=Lab(self.devices,self.temp.name,idle_timeout=30,max_session_seconds=90,clock=self.clock)
    def tearDown(self):self.lab.close_all();self.temp.cleanup()
    def session(self):return self.lab.create_session('test','owner','browser')
    def test_abandoned_session_expires_and_releases_device(self):
        s=self.session();self.clock.advance(31)
        self.lab.reap_expired()
        self.assertEqual(self.lab.get_session(s['id'])['state'],'closed')
        self.assertEqual(self.lab.get_session(s['id'])['closeReason'],'idle_timeout')
        self.assertTrue(self.providers[0].closed)
        self.assertEqual(self.lab.list_devices()[0]['state'],'available')
    def test_heartbeat_renews_idle_but_not_absolute_lifetime(self):
        s=self.session();self.clock.advance(25);self.lab.heartbeat(s['id'],'owner','browser')
        self.clock.advance(20);self.lab.reap_expired()
        self.assertEqual(self.lab.get_session(s['id'])['state'],'active')
        for _ in range(2):
            self.lab.heartbeat(s['id'],'owner','browser');self.clock.advance(25)
        self.lab.reap_expired()
        self.assertEqual(self.lab.get_session(s['id'])['closeReason'],'session_timeout')
    def test_late_heartbeat_cannot_resurrect_expired_lease(self):
        s=self.session();self.clock.advance(31)
        with self.assertRaises(LiveError):self.lab.heartbeat(s['id'],'owner','browser')
        self.lab.reap_expired();self.assertEqual(self.lab.get_session(s['id'])['state'],'closed')
    def test_input_cannot_revive_expired_lease(self):
        s=self.session();f=self.lab.frame(s['id']);self.clock.advance(31)
        with self.assertRaises(LiveError):
            self.lab.input(s['id'],'owner',{'controllerId':'browser','epoch':1,'sequence':1,'commandId':'late','frameId':f['id'],'geometryVersion':f['geometryVersion'],'action':'tap','payload':{'x':.5,'y':.5}})
        self.assertEqual(self.providers[0].calls,[])
    def test_foreign_heartbeat_and_session_listing_are_isolated(self):
        s=self.session()
        with self.assertRaises(LiveError):self.lab.heartbeat(s['id'],'other','browser')
        self.assertEqual(self.lab.list_sessions('other'),[])
        self.assertEqual(self.lab.list_sessions('owner')[0]['id'],s['id'])
    def test_crash_marker_quarantines_device_after_restart(self):
        self.session();restarted=Lab(self.devices,self.temp.name)
        self.assertEqual(restarted.list_devices()[0]['state'],'quarantined')
        with self.assertRaises(LiveError):restarted.create_session('test','owner','next')
    def test_clean_close_survives_restart_as_available(self):
        s=self.session();self.lab.close_session(s['id'],'owner')
        restarted=Lab(self.devices,self.temp.name)
        self.assertEqual(restarted.list_devices()[0]['state'],'available')
    def test_lifecycle_events_do_not_record_input_payload(self):
        s=self.session();self.lab.heartbeat(s['id'],'owner','browser')
        self.lab.claim(s['id'],'owner','automation',s['epoch'],'automation')
        self.lab.close_session(s['id'],'owner')
        events=self.lab.session_events(s['id'],'owner')
        self.assertIn('controller_changed',[e['type'] for e in events])
        self.assertIn('session_closed',[e['type'] for e in events])
        self.assertTrue(all('payload' not in e for e in events))

if __name__=='__main__':unittest.main()

class RecoveryTests(unittest.TestCase):
    def test_inactive_device_marker_survives_other_device_server(self):
        from reproof.live.providers import demo_device
        with tempfile.TemporaryDirectory() as output:
            first=Lab([demo_device()],output);s=first.create_session('demo','owner','c')
            other=demo_device();other['id']='other'
            second=Lab([other],output);t=second.create_session('other','owner','d');second.close_session(t['id'],'owner')
            restarted=Lab([demo_device()],output)
            self.assertEqual(restarted.list_devices()[0]['state'],'quarantined')
            first.close_all();second.close_all()
    def test_demo_recovery_releases_only_orphan_quarantine(self):
        from reproof.live.providers import demo_device
        with tempfile.TemporaryDirectory() as output:
            first=Lab([demo_device()],output);s=first.create_session('demo','owner','c')
            restarted=Lab([demo_device()],output)
            self.assertEqual(restarted.recover_device('demo')['state'],'available')
            first.close_all();restarted.close_all()
