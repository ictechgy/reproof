"""Synthetic-only explicit unverified action playback tests."""
import copy
import tempfile
import unittest
from pathlib import Path
from reproof.core import digest
from reproof.live.model import Lab, LiveError
from tests.test_live_model import Provider

class ActionPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.provider=Provider()
        self.device={'id':'test','kind':'demo','factory':lambda:self.provider,
                     'capabilities':{'actions':['tap','text','reset','home'],'resetContract':'app-relaunch-only'}}
        self.lab=Lab([self.device],self.temp.name);self.addCleanup(self.lab.close_all)
        self.s=self.lab.create_session('test','owner','controller');self.sid=self.s['id']
    def command(self,seq=1,action='tap',payload=None):
        f=self.lab.frame(self.sid)
        return dict(controllerId='controller',epoch=1,sequence=seq,commandId=f'c{seq}',
                    frameId=f['id'],geometryVersion=f['geometryVersion'],action=action,
                    payload=({'x':.5,'y':.5} if payload is None else payload))
    def capture(self,action='tap',payload=None):
        self.lab.start_recording(self.sid,'owner','controller',1,reset=False)
        self.lab.input(self.sid,'owner',self.command(action=action,payload=payload))
        return self.lab.stop_recording(self.sid,'owner','controller',1)
    def plan(self,r,variables=None):
        f=self.lab.frame(self.sid)
        return self.lab.plan_playback(self.sid,'owner','controller',1,r['id'],variables,f['id'],f['geometryVersion'])
    def confirm(self,p,**changes):
        f=self.lab.frame(self.sid)
        args=dict(sid=self.sid,owner='owner',controller='controller',epoch=1,token=p['token'],
                  acknowledge_unknown_start=True,frame_id=f['id'],geometry_version=f['geometryVersion'])
        args.update(changes);return self.lab.confirm_playback(**args)
    def test_unknown_start_without_reset_or_verified_evidence(self):
        r=self.capture();self.assertFalse(r['replayable'])
        with self.assertRaises(LiveError):self.lab.start_replay(self.sid,'owner','controller',1,r['id'])
        p=self.plan(r);self.assertEqual(len(self.provider.calls),1)
        self.confirm(p);s=self.lab.sessions[self.sid];s['playbackThread'].join(2)
        self.assertEqual(s['playback']['state'],'actions_played_unverified')
        self.assertEqual([a for a,_ in self.provider.calls],['tap','tap'])
        self.assertFalse(s['knownStart']);self.assertNotIn('knownStartBinding',s)
        self.assertIsNone(s['replay']);self.assertFalse(s['playback']['verified']);self.assertFalse(s['playback']['reproduced'])
        self.assertFalse(self.lab.recording(r['id'],'owner')['replayable'])
        self.assertFalse((Path(self.temp.name)/'replays').exists())
    def test_confirmation_explicit_and_one_use(self):
        p=self.plan(self.capture())
        with self.assertRaises(LiveError):self.confirm(p,acknowledge_unknown_start=False)
        with self.assertRaises(LiveError):self.confirm(p)
        self.assertEqual(len(self.provider.calls),1)
    def test_forged_token(self):
        p=self.plan(self.capture())
        with self.assertRaises(LiveError):self.confirm(p,token='x'*43)
        with self.assertRaises(LiveError):self.confirm(p)
        self.assertEqual(len(self.provider.calls),1)
    def test_non_ascii_token_is_cleanly_rejected(self):
        p=self.plan(self.capture())
        with self.assertRaises(LiveError) as error:self.confirm(p,token='승인')
        self.assertEqual(error.exception.code,'playback_confirmation')
        self.assertEqual(len(self.provider.calls),1)
    def test_malformed_token_does_not_consume_plan_or_inject(self):
        p=self.plan(self.capture())
        for token in ('승인','\ud800','x'*42,'x'*44,'!'*43,[],{},1,True,None):
            with self.subTest(token=repr(token)):
                with self.assertRaises(LiveError) as error:self.confirm(p,token=token)
                self.assertEqual(error.exception.code,'playback_confirmation')
                self.assertEqual(self.lab.sessions[self.sid]['playbackPlan']['public']['token'],p['token'])
                self.assertEqual(len(self.provider.calls),1)
        self.confirm(p);s=self.lab.sessions[self.sid];s['playbackThread'].join(2)
        self.assertEqual(s['playback']['state'],'actions_played_unverified')
    def test_import_and_missing_process_origin_inert(self):
        r=self.capture();self.lab.recordings[r['id']].pop('actionOrigin')
        with self.assertRaises(LiveError):self.plan(r)
        r=copy.deepcopy(r);r['id']='imported';r.pop('digest');r['digest']=digest(r)
        imported=self.lab.import_recording(r,'owner')['recording']
        with self.assertRaises(LiveError):self.plan(imported)
    def test_capability_change(self):
        p=self.plan(self.capture());self.device['capabilities']['actions'].append('back')
        with self.assertRaises(LiveError):self.confirm(p)
        self.assertEqual(len(self.provider.calls),1)
    def test_concurrent_input_invalidates(self):
        p=self.plan(self.capture());self.lab.input(self.sid,'owner',self.command(2))
        with self.assertRaises(LiveError):self.confirm(p)
        self.assertEqual(len(self.provider.calls),2)
    def test_expiry(self):
        p=self.plan(self.capture());self.lab.sessions[self.sid]['playbackPlan']['deadline']=self.lab.clock()-1
        with self.assertRaises(LiveError):self.confirm(p)
    def test_geometry_change(self):
        p=self.plan(self.capture());self.lab.publish_frame(self.sid,b'<svg/>','image/svg+xml',800,400,'landscape')
        with self.assertRaises(LiveError):self.confirm(p)
    def test_current_displayed_frame_required(self):
        p=self.plan(self.capture())
        with self.assertRaises(LiveError):self.confirm(p,frame_id=-1)
    def test_prepared_scope_refused(self):
        r=self.capture();self.lab.sessions[self.sid]['releaseRegistration']=object()
        with self.assertRaises(LiveError):self.plan(r)
    def test_cancel_pending(self):
        p=self.plan(self.capture());self.lab.cancel_playback(self.sid,'owner')
        with self.assertRaises(LiveError):self.confirm(p)
    def test_text_preview_exact_but_not_persisted(self):
        p=self.plan(self.capture('text',{'value':'original-private'}),{'text_1':'reviewed-private'})
        self.assertEqual(p['actions'][0]['payload'],{'value':'reviewed-private'})
        self.confirm(p);self.lab.sessions[self.sid]['playbackThread'].join(2)
        for path in Path(self.temp.name).rglob('*.json'):
            self.assertNotIn('reviewed-private',path.read_text());self.assertNotIn(p['token'],path.read_text())
    def test_partial_pointer_capture_is_not_executable(self):
        self.device['capabilities'].update(actions=['pointer'],inputMode='continuous-pointer',maxPointers=5)
        down={'phase':'down','pointerId':0,'x':.5,'y':.5}
        self.lab.input(self.sid,'owner',self.command(1,'pointer',down))
        self.lab.start_recording(self.sid,'owner','controller',1,reset=False)
        self.lab.input(self.sid,'owner',self.command(2,'pointer',dict(down,phase='up')))
        r=self.lab.stop_recording(self.sid,'owner','controller',1)
        self.assertEqual(r['status'],'complete')
        with self.assertRaises(LiveError):self.plan(r)
        self.assertEqual(len(self.provider.calls),2)
    def test_balanced_pointer_capture_can_play_unverified(self):
        self.device['capabilities'].update(actions=['pointer'],inputMode='continuous-pointer',maxPointers=5)
        down={'phase':'down','pointerId':0,'x':.5,'y':.5}
        self.lab.start_recording(self.sid,'owner','controller',1,reset=False)
        self.lab.input(self.sid,'owner',self.command(1,'pointer',down))
        self.lab.input(self.sid,'owner',self.command(2,'pointer',dict(down,phase='up')))
        r=self.lab.stop_recording(self.sid,'owner','controller',1)
        self.confirm(self.plan(r));s=self.lab.sessions[self.sid];s['playbackThread'].join(2)
        self.assertEqual(s['playback']['state'],'actions_played_unverified')
        self.assertFalse(s['activePointers']);self.assertEqual(len(self.provider.calls),4)
    def test_home_forbidden(self):
        r=self.capture('home',{})
        with self.assertRaises(LiveError):self.plan(r)
    def test_revocation(self):
        p=self.plan(self.capture());self.lab.sessions[self.sid]['effectAuthorizer']=lambda kind:False
        with self.assertRaises(LiveError):self.confirm(p)
        self.assertEqual(len(self.provider.calls),1)
    def test_uncertain_quarantines_no_retry(self):
        p=self.plan(self.capture());self.provider.execute=lambda action,payload:{'ok':False}
        self.confirm(p);s=self.lab.sessions[self.sid];s['playbackThread'].join(2)
        self.assertEqual(s['playback']['state'],'failed');self.assertEqual(s['state'],'failed')
        self.assertEqual(self.lab.devices['test']['state'],'quarantined');self.assertEqual(s['playback']['index'],0)

if __name__=='__main__':unittest.main()
