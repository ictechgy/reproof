"""Synthetic-only imported-data plans: new authorization, never recording trust."""
import copy
import json
import unittest
from pathlib import Path
from reproof.core import digest
from reproof.live.model import Lab, LiveError
from tests import test_live_action_playback as fixtures


class ReviewedActionPlanTests(unittest.TestCase):
    def setUp(self):
        self.c = fixtures.ActionPlaybackTests(); self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.c.device['capabilities']['applicationIdentity'] = {'bundle': 'reproof.synthetic.demo'}

    def imported(self, transform=None):
        r = self.c.capture()
        r['id'] = 'imported'; r['sessionId'] = 'historic-session'
        if transform: transform(r)
        r.pop('digest'); r['digest'] = digest(r)
        return self.c.lab.import_recording(r, 'owner')['recording']

    def plan(self, r, variables=None):
        c = self.c; f = c.lab.frame(c.sid)
        return c.lab.plan_reviewed_actions(c.sid, 'owner', 'controller', 1,
            r['id'], variables, f['id'], f['geometryVersion'])

    def confirm(self, p, **changes):
        return self.c.confirm(p, acknowledge_untrusted_source=changes.pop('acknowledge_untrusted_source', True), **changes)

    def finish(self):
        s = self.c.lab.sessions[self.c.sid]; s['playbackThread'].join(2)
        self.assertFalse(s['playbackThread'].is_alive()); return s

    def rewrite(self, rid, transform):
        """A fully re-digested, structurally valid replacement still invalidates approval."""
        c = self.c; e = c.lab.recordings[rid]; transform(e['data'])
        e['data'].pop('digest'); e['data']['digest'] = digest(e['data'])
        path = Path(c.temp.name)/'recordings'/f'{rid}.json'
        stored = json.loads(path.read_text()); stored['recording'] = e['data']
        path.write_text(json.dumps(stored))

    def test_import_creates_new_plan_but_never_trust(self):
        c = self.c; r = self.imported(); before = copy.deepcopy(r)
        with self.assertRaises(LiveError): c.plan(r)
        with self.assertRaises(LiveError): c.lab.start_replay(c.sid, 'owner', 'controller', 1, r['id'])
        p = self.plan(r)
        self.assertEqual(p['kind'], 'reviewed-untrusted-action-plan')
        self.assertEqual(p['sourceTrust'], 'unverified')
        self.assertEqual(p['currentTarget']['applicationIdentity'], r['applicationIdentity'])
        self.assertIn('REAL DATA', p['warning']); self.assertEqual(len(c.provider.calls), 1)
        self.confirm(p); s = self.finish()
        self.assertEqual(s['playback']['state'], 'actions_played_unverified')
        self.assertEqual(s['playback']['planId'], p['planId'])
        self.assertFalse(s['knownStart']); self.assertIsNone(s['replay'])
        self.assertFalse(s['playback']['verified']); self.assertFalse(s['playback']['reproduced'])
        self.assertEqual(c.lab.recording(r['id'], 'owner'), before)
        self.assertNotIn('actionOrigin', c.lab.recordings[r['id']])
        self.assertNotIn('replayBinding', c.lab.recordings[r['id']])
        with self.assertRaises(LiveError): c.lab.start_replay(c.sid, 'owner', 'controller', s['epoch'], r['id'])

    def test_source_ack_required_consumes_token(self):
        p = self.plan(self.imported())
        with self.assertRaises(LiveError): self.confirm(p, acknowledge_untrusted_source=False)
        with self.assertRaises(LiveError): self.confirm(p)
        self.assertEqual(len(self.c.provider.calls), 1)

    def test_public_preview_not_authoritative(self):
        p = self.plan(self.imported()); p['actions'][0]['payload']['x'] = .99
        p['currentTarget']['applicationIdentity']['bundle'] = 'another.app'
        p['sourceTrust'] = 'trusted'
        self.confirm(p); self.finish()
        self.assertEqual(self.c.provider.calls[-1], ('tap', {'x': .5, 'y': .5}))

    def test_claims_do_not_mint_replay_authority(self):
        def forge(r):
            r.update(replayable=True, startingState={'kind': 'provider-reset'}); r.pop('reason', None)
        r = self.imported(forge)
        self.assertFalse(r['replayable']); self.assertEqual(r['startingState']['kind'], 'unknown')
        self.plan(r)
        with self.assertRaises(LiveError): self.c.lab.start_replay(self.c.sid, 'owner', 'controller', 1, r['id'])

    def test_missing_actual_identity_rejected_even_demo(self):
        r = self.imported(); self.c.device['capabilities'].pop('applicationIdentity')
        with self.assertRaises(LiveError) as e: self.plan(r)
        self.assertEqual(e.exception.code, 'application_identity_required')

    def test_source_claimed_scope_mismatch_no_remapping(self):
        r = self.imported()
        for key, value in [('applicationIdentity', {'bundle': 'wrong.app'}), ('providerKind', 'ios-simulator'),
                           ('deviceId', 'other-device'), ('geometry', {'width': 10, 'height': 20, 'orientation': 'portrait'})]:
            with self.subTest(key=key):
                original = copy.deepcopy(self.c.lab.recordings[r['id']]['data'])
                self.rewrite(r['id'], lambda item: item.update({key: value}))
                with self.assertRaises(LiveError): self.plan(r)
                self.rewrite(r['id'], lambda item: (item.clear(), item.update(copy.deepcopy(original))))
        self.assertEqual(len(self.c.provider.calls), 1)

    def test_source_changed_after_review_consumes_token(self):
        r = self.imported(); p = self.plan(r)
        self.rewrite(r['id'], lambda item: item['events'][0]['payload'].update(x=.9))
        with self.assertRaises(LiveError): self.confirm(p)
        with self.assertRaises(LiveError): self.confirm(p)
        self.assertEqual(len(self.c.provider.calls), 1)
        self.assertNotEqual(self.plan(r)['token'], p['token'])

    def test_deleted_source_rejected_cleanly(self):
        r = self.imported(); p = self.plan(r)
        (Path(self.c.temp.name)/'recordings'/f'{r["id"]}.json').unlink()
        with self.assertRaises(LiveError): self.confirm(p)
        self.assertNotIn('playbackPlan', self.c.lab.sessions[self.c.sid])
        self.assertEqual(len(self.c.provider.calls), 1)

    def test_owner_change_rejected(self):
        r = self.imported(); p = self.plan(r); self.c.lab.recordings[r['id']]['owner'] = 'other'
        with self.assertRaises(LiveError): self.confirm(p)
        self.assertEqual(len(self.c.provider.calls), 1)

    def test_source_change_at_input_admission_stops_before_effect(self):
        c = self.c; r = self.imported(); p = self.plan(r); original = c.lab.input
        def wrapped(*args, **kwargs):
            self.rewrite(r['id'], lambda item: item['events'][0]['payload'].update(x=.9))
            return original(*args, **kwargs)
        c.lab.input = wrapped
        self.confirm(p); s = self.finish()
        self.assertEqual(s['playback']['state'], 'failed')
        self.assertEqual(len(c.provider.calls), 1)

    def test_deleted_source_at_input_admission_stops_before_effect(self):
        c = self.c; r = self.imported(); p = self.plan(r); original = c.lab.input
        def wrapped(*args, **kwargs):
            (Path(c.temp.name)/'recordings'/f'{r["id"]}.json').unlink()
            return original(*args, **kwargs)
        c.lab.input = wrapped
        self.confirm(p); s = self.finish()
        self.assertEqual(s['playback']['state'], 'failed')
        self.assertEqual(len(c.provider.calls), 1)

    def test_source_owner_at_input_admission_stops_before_effect(self):
        c = self.c; r = self.imported(); p = self.plan(r); original = c.lab.input
        def wrapped(*args, **kwargs):
            c.lab.recordings[r['id']]['owner'] = 'other'
            return original(*args, **kwargs)
        c.lab.input = wrapped
        self.confirm(p); s = self.finish()
        self.assertEqual(s['playback']['state'], 'failed')
        self.assertEqual(len(c.provider.calls), 1)

    def test_null_empty_and_missing_native_identity_rejected(self):
        r = self.imported(); c = self.c
        c.device['kind'] = 'ios-simulator'
        for identity in (None, {}, {'bundle': ''}, {'bundleId': ''}):
            with self.subTest(identity=identity):
                c.device['capabilities']['applicationIdentity'] = identity
                with self.assertRaises(LiveError) as e: self.plan(r)
                self.assertEqual(e.exception.code, 'application_identity_required')

    def test_source_bounds_revalidated_and_new_plan_invalidates_old(self):
        r = self.imported(); original = copy.deepcopy(self.c.lab.recordings[r['id']]['data'])
        for mutate in (lambda item: item['events'][0].update(offsetMs=600001),
                       lambda item: item.update(events=[dict(copy.deepcopy(item['events'][0]), id=f'e{i}') for i in range(501)]),
                       lambda item: item['events'][0].update(action='text', payload={'value': 'unapproved raw text'})):
            with self.subTest(mutate=mutate):
                p = self.plan(r)
                self.rewrite(r['id'], mutate)
                with self.assertRaises(LiveError): self.plan(r)
                self.assertNotIn('playbackPlan', self.c.lab.sessions[self.c.sid])
                self.rewrite(r['id'], lambda item: (item.clear(), item.update(copy.deepcopy(original))))
                with self.assertRaises(LiveError): self.confirm(p)
        self.assertEqual(len(self.c.provider.calls), 1)

    def test_restarted_recording_requires_new_plan_not_restored_token(self):
        c = self.c; r = self.imported(); p = self.plan(r)
        c.lab.close_all(); restored = Lab([c.device], c.temp.name); self.addCleanup(restored.close_all)
        new = restored.create_session('test', 'owner', 'new-controller'); f = restored.frame(new['id'])
        with self.assertRaises(LiveError):
            restored.confirm_playback(new['id'], 'owner', 'new-controller', 1, p['token'], True,
                                      f['id'], f['geometryVersion'], True)
        np = restored.plan_reviewed_actions(new['id'], 'owner', 'new-controller', 1, r['id'], {}, f['id'], f['geometryVersion'])
        self.assertNotEqual(np['token'], p['token'])
        self.assertFalse(restored.recording(r['id'], 'owner')['replayable'])

    def test_unbalanced_pointer_import_rejected_independent_of_replayable(self):
        self.c.device['capabilities']['actions'].append('pointer')
        def pointer(r):
            r['events'][0].update(action='pointer', payload={'phase':'down','pointerId':0,'x':.5,'y':.5})
        r = self.imported(pointer)
        self.assertFalse(r['replayable'])
        with self.assertRaises(LiveError): self.plan(r)

    def test_pointer_capability_preflight_before_any_effect(self):
        c = self.c; c.device['capabilities']['actions'].append('pointer')
        def pointers(r):
            template = copy.deepcopy(r['events'][0])
            for i, phase in enumerate(('down', 'up'), 1):
                e = copy.deepcopy(template); e.update(id=f'pointer{i}', action='pointer',
                    payload={'phase': phase, 'pointerId': 0, 'x': .5, 'y': .5})
                r['events'].append(e)
        r = self.imported(pointers)
        for mode, maximum in ((None, None), ('continuous-pointer', 4), ('wrong', 5)):
            with self.subTest(mode=mode, maximum=maximum):
                c.device['capabilities'].update(inputMode=mode, maxPointers=maximum)
                with self.assertRaises(LiveError): self.plan(r)
        self.assertEqual(len(c.provider.calls), 1)
        c.device['capabilities'].update(inputMode='continuous-pointer', maxPointers=5)
        self.assertEqual(len(self.plan(r)['actions']), 3)

    def test_lifecycle_action_rejected(self):
        r = self.imported(lambda item: item['events'][0].update(action='home', payload={}))
        with self.assertRaises(LiveError): self.plan(r)

    def test_prepared_and_durable_scopes_still_rejected(self):
        r = self.imported(); s = self.c.lab.sessions[self.c.sid]
        for key in ('releaseRecorder', 'releaseRecordingId', 'releaseRegistration', '_startupBinding', '_retainedDeviceScope', '_candidateIdentity'):
            with self.subTest(key=key):
                from types import SimpleNamespace
                s[key] = SimpleNamespace(duration_reached=lambda: False) if key == 'releaseRecorder' else object()
                try:
                    with self.assertRaises(LiveError): self.plan(r)
                finally:
                    s[key] = None

    def test_cancel_and_expiry_require_new_review(self):
        r = self.imported(); p = self.plan(r)
        self.c.lab.cancel_playback(self.c.sid, 'owner')
        with self.assertRaises(LiveError): self.confirm(p)
        p = self.plan(r); self.c.lab.sessions[self.c.sid]['playbackPlan']['deadline'] = self.c.lab.clock()-1
        with self.assertRaises(LiveError): self.confirm(p)
        self.assertEqual(len(self.c.provider.calls), 1)

if __name__ == '__main__': unittest.main()
