"""Independent synthetic review of exact-plan and concurrent admission boundaries."""
import threading
import unittest
from tests import test_live_action_playback as fixtures
from reproof.live.model import LiveError


class PlaybackBoundaryReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ActionPlaybackTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_returned_preview_mutation_does_not_change_authorized_plan(self):
        c=self.case;p=c.plan(c.capture())
        p['actions'][0]['payload']['x']=.99
        c.confirm(p);c.lab.sessions[c.sid]['playbackThread'].join(2)
        self.assertEqual(c.provider.calls[-1],('tap',{'x':.5,'y':.5}))

    def test_derived_same_process_recording_cannot_inherit_action_origin(self):
        c=self.case;r=c.capture();derived=c.lab.derive_recording(r['id'],'owner')
        with self.assertRaises(LiveError):c.plan(derived)
        self.assertEqual(len(c.provider.calls),1)

    def test_cancel_at_input_admission_prevents_effect(self):
        c=self.case;p=c.plan(c.capture());original=c.lab.input
        def wrapped(*args,**kwargs):
            c.lab.cancel_playback(c.sid,'owner')
            return original(*args,**kwargs)
        c.lab.input=wrapped
        c.confirm(p);s=c.lab.sessions[c.sid];s['playbackThread'].join(2)
        self.assertEqual(len(c.provider.calls),1)
        self.assertEqual(s['playback']['state'],'cancelled')

    def test_binding_drift_at_input_admission_prevents_effect(self):
        c=self.case;p=c.plan(c.capture());original=c.lab.input
        def wrapped(*args,**kwargs):
            c.device['capabilities']['actions'].append('back')
            return original(*args,**kwargs)
        c.lab.input=wrapped
        c.confirm(p);s=c.lab.sessions[c.sid];s['playbackThread'].join(2)
        self.assertEqual(len(c.provider.calls),1)
        self.assertEqual(s['playback']['state'],'failed')

    def test_public_input_and_takeover_cannot_interleave_with_approved_run(self):
        c=self.case;p=c.plan(c.capture());original=c.lab.input
        reached=threading.Event();release=threading.Event()
        def wrapped(*args,**kwargs):
            reached.set()
            if not release.wait(3):raise AssertionError('test did not release worker')
            return original(*args,**kwargs)
        c.lab.input=wrapped
        try:
            c.confirm(p);self.assertTrue(reached.wait(2))
            s=c.lab.get_session(c.sid,'owner');f=c.lab.frame(c.sid)
            command=dict(controllerId=s['controllerId'],epoch=s['epoch'],sequence=99,
                commandId='external-reset',frameId=f['id'],geometryVersion=f['geometryVersion'],
                action='reset',payload={})
            with self.assertRaises(LiveError):original(c.sid,'owner',command)
            with self.assertRaises(LiveError):c.lab.claim(c.sid,'owner','other',s['epoch'])
            self.assertEqual(len(c.provider.calls),1)
            c.lab.cancel_playback(c.sid,'owner')
        finally:
            release.set()
            c.lab.sessions[c.sid]['playbackThread'].join(3)
        self.assertEqual(len(c.provider.calls),1)
        self.assertFalse(c.lab.sessions[c.sid]['knownStart'])

if __name__=='__main__':unittest.main()
