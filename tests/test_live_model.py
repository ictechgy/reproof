import tempfile
from pathlib import Path
import unittest
from reproof.live.model import Lab,LiveError


class Provider:
    def __init__(self):self.calls=[];self.closed=False
    def start(self,session,lab):
        self.session=session;self.lab=lab
        lab.publish_frame(session['id'],b'<svg/>','image/svg+xml',400,800,'portrait')
    def execute(self,action,payload):
        self.calls.append((action,payload))
        self.lab.publish_frame(self.session['id'],b'<svg/>','image/svg+xml',400,800,'portrait')
        return {'ok':True,'timing':'best-effort'}
    def close(self):self.closed=True


class LiveSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.provider=Provider()
        self.lab=Lab([{'id':'test','name':'Test','platform':'demo','kind':'demo','factory':lambda:self.provider,
                       'capabilities':{'actions':['tap','long_press','swipe','text','reset','home'],'inputMode':'gesture-batch','media':'demo-svg'}}],Path(self.temp.name))
    def tearDown(self):self.lab.close_all();self.temp.cleanup()
    def session(self):return self.lab.create_session('test','owner','browser-a')
    def command(self,s,n=1,**changes):
        frame=self.lab.frame(s['id']);value={'controllerId':s['controllerId'],'epoch':s['epoch'],'sequence':n,
            'commandId':f'command-{n}','frameId':frame['id'],'geometryVersion':frame['geometryVersion'],
            'action':'tap','payload':{'x':.5,'y':.5}}
        value.update(changes);return value
    def test_device_cannot_be_allocated_twice(self):
        self.session()
        with self.assertRaises(LiveError):self.lab.create_session('test','owner','browser-b')
    def test_same_command_is_not_injected_twice(self):
        s=self.session();command=self.command(s)
        first=self.lab.input(s['id'],'owner',command);second=self.lab.input(s['id'],'owner',command)
        self.assertEqual(first['id'],second['id']);self.assertEqual(len(self.provider.calls),1)
    def test_reusing_id_with_different_input_is_rejected(self):
        s=self.session();c=self.command(s);self.lab.input(s['id'],'owner',c)
        c['payload']={'x':.1,'y':.1}
        with self.assertRaises(LiveError):self.lab.input(s['id'],'owner',c)
    def test_handoff_fences_old_controller(self):
        s=self.session();old=self.command(s)
        new=self.lab.claim(s['id'],'owner','browser-b',s['epoch'])
        self.assertGreater(new['epoch'],s['epoch'])
        with self.assertRaises(LiveError):self.lab.input(s['id'],'owner',old)
    def test_foreign_owner_and_stale_geometry_rejected(self):
        s=self.session();c=self.command(s)
        with self.assertRaises(LiveError):self.lab.input(s['id'],'other-owner',c)
        self.lab.publish_frame(s['id'],b'<svg/>','image/svg+xml',800,400,'landscape')
        with self.assertRaises(LiveError):self.lab.input(s['id'],'owner',c)
    def test_recording_freezes_injected_input_and_redacts_text(self):
        s=self.session();r=self.lab.start_recording(s['id'],'owner',s['controllerId'],s['epoch'],reset=True)
        self.lab.input(s['id'],'owner',self.command(s))
        self.lab.input(s['id'],'owner',self.command(s,2,action='text',payload={'value':'synthetic-sensitive-input'}))
        record=self.lab.stop_recording(s['id'],'owner',s['controllerId'],s['epoch'])
        self.assertEqual(len(record['events']),2);self.assertTrue(record['replayable'])
        self.assertNotIn('synthetic-sensitive-input',str(record))
        self.assertEqual(record['variables'],['text_1'])
    def test_unknown_start_is_not_declared_replayable(self):
        s=self.session();self.lab.input(s['id'],'owner',self.command(s))
        self.lab.start_recording(s['id'],'owner',s['controllerId'],s['epoch'],reset=False)
        record=self.lab.stop_recording(s['id'],'owner',s['controllerId'],s['epoch'])
        self.assertFalse(record['replayable'])
    def test_close_releases_device_and_blocks_old_input(self):
        s=self.session();self.lab.close_session(s['id'],'owner',s['controllerId'],s['epoch'])
        self.assertTrue(self.provider.closed)
        with self.assertRaises(LiveError):self.lab.input(s['id'],'owner',self.command(s))
        self.assertEqual(self.lab.create_session('test','owner','next')['state'],'active')

if __name__=='__main__':unittest.main()

class LiveFailureTests(unittest.TestCase):
    setUp=LiveSessionTests.setUp
    tearDown=LiveSessionTests.tearDown
    session=LiveSessionTests.session
    command=LiveSessionTests.command
    def test_uncertain_reset_quarantines_device(self):
        s=self.session()
        self.provider.execute=lambda *args:{'ok':False}
        with self.assertRaises(LiveError):self.lab.start_recording(s['id'],'owner',s['controllerId'],s['epoch'],True)
        self.assertEqual(self.lab.get_session(s['id'])['state'],'failed')
        self.assertEqual(self.lab.list_devices()[0]['state'],'quarantined')
    def test_uncertain_input_quarantines_device(self):
        s=self.session()
        def unknown(*args):raise TimeoutError()
        self.provider.execute=unknown
        with self.assertRaises(LiveError):self.lab.input(s['id'],'owner',self.command(s))
        self.assertEqual(self.lab.get_session(s['id'])['state'],'failed')
        self.assertEqual(self.lab.list_devices()[0]['state'],'quarantined')
    def test_recording_geometry_change_is_invalidated_without_blocking_media(self):
        s=self.session();self.lab.start_recording(s['id'],'owner',s['controllerId'],s['epoch'],True)
        self.lab.publish_frame(s['id'],b'<svg/>','image/svg+xml',800,400,'landscape')
        r=self.lab.stop_recording(s['id'],'owner',s['controllerId'],s['epoch'])
        self.assertEqual(r['status'],'invalid');self.assertFalse(r['replayable'])
    def test_handoff_cancels_replay_before_delayed_input(self):
        import time
        s=self.session();self.lab.start_recording(s['id'],'owner',s['controllerId'],s['epoch'],True)
        self.lab.recordings[self.lab.get_session(s['id'])['recordingId']]['data']['_started']-=2
        self.lab.input(s['id'],'owner',self.command(s))
        r=self.lab.stop_recording(s['id'],'owner',s['controllerId'],s['epoch'])
        self.lab.start_replay(s['id'],'owner',s['controllerId'],s['epoch'],r['id'])
        current=self.lab.get_session(s['id']);self.lab.claim(s['id'],'owner','new-controller',current['epoch'])
        self.lab._session(s['id'])['replayThread'].join(timeout=2)
        current=self.lab.get_session(s['id']);self.assertEqual(current['controllerId'],'new-controller')
        self.assertEqual(current['replay']['state'],'cancelled')
        self.assertEqual(sum(action=='tap' for action,_ in self.provider.calls),1)
    def test_app_identity_change_blocks_original_replay(self):
        s=self.session();self.lab.start_recording(s['id'],'owner',s['controllerId'],s['epoch'],True)
        self.lab.input(s['id'],'owner',self.command(s));r=self.lab.stop_recording(s['id'],'owner',s['controllerId'],s['epoch'])
        self.lab.devices['test']['capabilities']['applicationIdentity']={'artifactDigest':'changed'}
        with self.assertRaises(LiveError):self.lab.start_replay(s['id'],'owner',s['controllerId'],s['epoch'],r['id'])
