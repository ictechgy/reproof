"""Synthetic trust-boundary tests; never start a native device or helper."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reproof.core import digest
from reproof.live.model import Lab, LiveError
from reproof.live.recordings import validate_recording, untrusted_recording
from reproof.storage import write_json
from tests.test_live_model import Provider
from tests.test_live_recordings import recording


class RecordingTrustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = Provider()
        self.device = {'id': 'demo', 'kind': 'demo', 'factory': lambda: self.provider,
                       'capabilities': {'actions': ['tap', 'reset'],
                                        'resetContract': 'sample-counter-fixture-v1'}}
        self.lab = Lab([self.device], self.temp.name)
        self.addCleanup(self.lab.close_all)
        self.s = self.lab.create_session('demo', 'owner', 'controller')

    def command(self, action='tap', sequence=1):
        frame = self.lab.frame(self.s['id'])
        return {'controllerId': self.s['controllerId'], 'epoch': self.s['epoch'],
                'sequence': sequence, 'commandId': f'command-{sequence}',
                'frameId': frame['id'], 'geometryVersion': frame['geometryVersion'],
                'action': action, 'payload': {} if action == 'reset' else {'x': .5, 'y': .5}}

    def capture(self):
        self.lab.start_recording(self.s['id'], 'owner', self.s['controllerId'], self.s['epoch'], reset=True)
        self.lab.input(self.s['id'], 'owner', self.command())
        return self.lab.stop_recording(self.s['id'], 'owner', self.s['controllerId'], self.s['epoch'])

    def forged(self, **changes):
        return recording(id='external', deviceId='demo', providerKind='demo',
                         variables=[], events=[recording()['events'][0]], **changes)

    def assert_rejected_without_effects(self, rid):
        before = list(self.provider.calls)
        controller = self.lab.get_session(self.s['id'])['controllerId']
        with self.assertRaises(LiveError) as error:
            self.lab.start_replay(self.s['id'], 'owner', self.s['controllerId'], self.s['epoch'], rid)
        self.assertEqual(error.exception.code, 'not_replayable')
        self.assertEqual(self.provider.calls, before)
        self.assertEqual(self.lab.get_session(self.s['id'])['controllerId'], controller)
        self.assertNotIn('replayThread', self.lab.sessions[self.s['id']])

    def test_unsigned_recomputed_digest_cannot_claim_known_start(self):
        source = self.forged()
        self.assertTrue(validate_recording(source)['replayable'])  # Syntax is not authority.
        imported = self.lab.import_recording(source, 'owner')['recording']
        self.assertFalse(imported['replayable'])
        self.assertEqual(imported['startingState'], {'kind': 'unknown'})
        self.assertEqual(imported['reason'], 'untrusted_starting_state')
        self.assertNotEqual(source['digest'], imported['digest'])
        self.assertEqual(validate_recording(imported), imported)
        self.assertTrue(source['replayable'])  # Caller document is untouched.
        self.assert_rejected_without_effects(imported['id'])

    def test_unsigned_import_is_idempotent_for_original_or_sanitized_document(self):
        source = self.forged()
        normalized = self.lab.import_recording(source, 'owner')['recording']
        for value in (source, normalized):
            result = self.lab.import_recording(value, 'owner')
            self.assertFalse(result['imported'])
            self.assertEqual(result['recording'], normalized)

    def test_forged_source_session_and_reset_flags_do_not_mint_binding(self):
        local = self.capture()
        for number, changes in enumerate(({'sessionId': local['sessionId']},
                                          {'provenance': {'kind': 'derived', 'sourceRecordingId': local['id'],
                                           'sourceDigest': local['digest'], 'transform': {'eventIds': ['e1'], 'speed': 1}}})):
            external = self.forged(**changes)
            external['id'] += str(number)
            external['digest'] = digest({k: v for k, v in external.items() if k != 'digest'})
            imported = self.lab.import_recording(external, 'owner')['recording']
            self.assert_rejected_without_effects(imported['id'])

    def test_unknown_capture_modified_and_redigested_is_still_untrusted(self):
        self.lab.start_recording(self.s['id'], 'owner', 'controller', self.s['epoch'])
        self.lab.input(self.s['id'], 'owner', self.command())
        source = self.lab.stop_recording(self.s['id'], 'owner', 'controller', self.s['epoch'])
        source.update(id='modified', replayable=True, startingState={'kind': 'provider-reset'})
        source.pop('reason', None)
        source['digest'] = digest({k: v for k, v in source.items() if k != 'digest'})
        value = self.lab.import_recording(source, 'owner')['recording']
        self.assert_rejected_without_effects(value['id'])

    def test_process_local_capture_and_full_derivation_replay(self):
        local = self.capture()
        derived = self.lab.derive_recording(local['id'], 'owner', speed=2)
        self.assertTrue(derived['replayable'])
        self.assertFalse(self.lab.import_recording(local, 'owner')['imported'])
        self.assertFalse(self.lab.import_recording(derived, 'owner')['imported'])
        self.lab.start_replay(self.s['id'], 'owner', 'controller', self.s['epoch'], derived['id'])
        self.lab.sessions[self.s['id']]['replayThread'].join(2)
        self.assertEqual(self.lab.get_session(self.s['id'])['replay']['state'], 'actions_replayed')

    def test_imported_derivation_cannot_launder_authority(self):
        value = self.lab.import_recording(self.forged(), 'owner')['recording']
        first = self.lab.derive_recording(value['id'], 'owner', speed=2)
        second = self.lab.derive_recording(first['id'], 'owner')
        for result in (first, second):
            self.assertFalse(result['replayable'])
            self.assert_rejected_without_effects(result['id'])

    def test_trusted_subset_and_export_to_another_lab_are_not_trusted(self):
        local = self.capture()
        subset = self.lab.derive_recording(local['id'], 'owner', event_ids=[])
        self.assertFalse(subset['replayable'])
        self.assert_rejected_without_effects(subset['id'])
        with tempfile.TemporaryDirectory() as other:
            lab = Lab([self.device], other)
            try:
                imported = lab.import_recording(local, 'owner')['recording']
                self.assertFalse(imported['replayable'])
                self.assertNotIn('replayBinding', lab.recordings[local['id']])
            finally:
                lab.close_all()

    def test_restart_demotes_history_without_rewriting_disk_or_laundering_owner(self):
        local = self.capture()
        self.lab.close_session(self.s['id'], 'owner')
        path = Path(self.temp.name) / 'recordings' / (local['id'] + '.json')
        before = path.read_bytes()
        restarted = Lab([self.device], self.temp.name)
        self.addCleanup(restarted.close_all)
        loaded = restarted.recording(local['id'], 'owner')
        self.assertFalse(loaded['replayable'])
        self.assertEqual(loaded['startingState'], {'kind': 'unknown'})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(restarted.import_recording(local, 'owner')['recording']['replayable'])
        with self.assertRaises(LiveError):
            restarted.recording(local['id'], 'other-owner')
        session = restarted.create_session('demo', 'owner', 'next')
        before_calls = list(self.provider.calls)
        with self.assertRaises(LiveError):
            restarted.start_replay(session['id'], 'owner', 'next', session['epoch'], local['id'])
        self.assertEqual(self.provider.calls, before_calls)

    def test_legacy_disk_forgery_is_view_only_and_later_disk_mutation_is_rejected(self):
        self.lab.close_session(self.s['id'], 'owner')
        source = self.forged()
        path = Path(self.temp.name) / 'recordings' / 'external.json'
        write_json(path, {'owner': 'owner', 'recording': source})
        restarted = Lab([self.device], self.temp.name)
        self.addCleanup(restarted.close_all)
        self.assertFalse(restarted.recording('external', 'owner')['replayable'])
        source['events'][0]['payload']['x'] = .9
        source['digest'] = digest({k: v for k, v in source.items() if k != 'digest'})
        write_json(path, {'owner': 'owner', 'recording': source})
        with self.assertRaises(LiveError) as error:
            restarted.recording('external', 'owner')
        self.assertEqual(error.exception.code, 'recording_changed')

    def test_unknown_reset_contract_does_not_establish_known_start(self):
        for contract in (None, '', 'none', 'app-relaunch-only', 'invented-fixture-v1',
                         'sample-counter-fixture-v2', 'sample-counter-fixture-v1-extra', True):
            with self.subTest(contract=contract):
                self.lab.devices['demo']['capabilities']['resetContract'] = contract
                self.lab._reset(self.lab.sessions[self.s['id']])
                self.assertFalse(self.lab.sessions[self.s['id']]['knownStart'])

    def test_direct_reset_with_null_contract_cannot_authorize_capture(self):
        self.lab.devices['demo']['capabilities']['resetContract'] = None
        self.lab.input(self.s['id'], 'owner', self.command('reset'))
        result = self.lab.start_recording(self.s['id'], 'owner', 'controller', self.s['epoch'])
        self.assertFalse(result['replayable'])
        self.assertEqual(result['startingState'], {'kind': 'unknown'})

    def test_direct_reset_ack_without_new_frame_cannot_authorize_capture(self):
        self.provider.execute = lambda action, payload: {'ok': True}
        self.lab.input(self.s['id'], 'owner', self.command('reset'))
        value = self.lab.start_recording(self.s['id'], 'owner', 'controller', self.s['epoch'])
        self.assertFalse(value['replayable'])

    def test_changed_target_between_reset_and_capture_is_not_rebound(self):
        internal = self.lab.sessions[self.s['id']]
        self.lab._reset(internal)
        self.assertTrue(internal['knownStart'])
        self.lab.devices['demo']['capabilities']['applicationIdentity'] = {'bundle': 'example.changed'}
        result = self.lab.start_recording(self.s['id'], 'owner', 'controller', self.s['epoch'])
        self.assertFalse(result['replayable'])

    def test_changed_target_during_reset_never_establishes_known_start(self):
        execute = self.provider.execute
        def changed(action, payload):
            self.lab.devices['demo']['capabilities']['applicationIdentity'] = {'bundle': 'example.changed'}
            return execute(action, payload)
        self.provider.execute = changed
        self.lab._reset(self.lab.sessions[self.s['id']])
        self.assertFalse(self.lab.sessions[self.s['id']]['knownStart'])

    def test_changed_contract_prevents_reset_and_inputs_on_replay(self):
        local = self.capture()
        self.lab.devices['demo']['capabilities']['resetContract'] = None
        self.assert_rejected_without_effects(local['id'])

    def test_changed_application_during_replay_reset_prevents_event_inputs(self):
        local = self.capture()
        execute = self.provider.execute
        def changed(action, payload):
            if action == 'reset':
                self.lab.devices['demo']['capabilities']['applicationIdentity'] = {'bundle': 'example.changed'}
            return execute(action, payload)
        self.provider.execute = changed
        before = len(self.provider.calls)
        self.lab.start_replay(self.s['id'], 'owner', 'controller', self.s['epoch'], local['id'])
        self.lab.sessions[self.s['id']]['replayThread'].join(2)
        self.assertEqual(self.provider.calls[before:], [('reset', {})])
        self.assertEqual(self.lab.get_session(self.s['id'])['replay']['state'], 'failed')

    def test_reset_contract_is_bound_to_provider_and_v1_profile_identity(self):
        device = self.lab.devices['demo']
        caps = device['capabilities']
        device['kind'] = 'android-live'
        profile_digest = 'a' * 64
        caps.update(resetContract=f'app-profile-{profile_digest}-fixture-test_v1-v1',
                    fixture='test_v1', applicationIdentity={'bundle': 'example.app', 'appProfileDigest': profile_digest})
        internal = self.lab.sessions[self.s['id']]
        self.assertIsNotNone(self.lab._supported_reset_contract(internal))
        for key, value in (('fixture', 'other'), ('applicationIdentity', {'bundle': 'example.app', 'appProfileDigest': 'b' * 64})):
            original = caps[key]
            caps[key] = value
            self.assertIsNone(self.lab._supported_reset_contract(internal))
            caps[key] = original
        device['kind'] = 'ios-simulator'
        self.assertIsNone(self.lab._supported_reset_contract(internal))

    def test_realistic_v1_profile_capture_derivation_keeps_trusted_identity(self):
        device = self.lab.devices['demo']
        device['kind'] = 'android-live'
        profile_digest = 'a' * 64
        device['capabilities'].update(
            resetContract=f'app-profile-{profile_digest}-fixture-checkout-v1', fixture='checkout',
            applicationIdentity={'bundle': 'example.app', 'artifactDigest': 'b' * 64,
                                 'appProfileDigest': profile_digest})
        local = self.capture()
        self.assertTrue(local['replayable'])
        derived = self.lab.derive_recording(local['id'], 'owner', speed=2)
        self.assertTrue(derived['replayable'])
        self.assertEqual(derived['applicationIdentity'], local['applicationIdentity'])
        self.lab.start_replay(self.s['id'], 'owner', 'controller', self.s['epoch'], derived['id'])
        self.lab.sessions[self.s['id']]['replayThread'].join(2)
        self.assertEqual(self.lab.get_session(self.s['id'])['replay']['state'], 'actions_replayed')

    def test_recognized_contract_change_between_reset_and_capture_is_untrusted(self):
        device = self.lab.devices['demo']
        device['kind'] = 'ios-simulator'
        device['capabilities'].update(resetContract='sample-counter-fixture-v1',
                                      applicationIdentity={'bundle': 'io.reproof.sample.ios'})
        self.lab._reset(self.lab.sessions[self.s['id']])
        device['capabilities']['resetContract'] = 'sample-reset-fixture-v1'
        self.assertIsNotNone(self.lab._supported_reset_contract(self.lab.sessions[self.s['id']]))
        value = self.lab.start_recording(self.s['id'], 'owner', 'controller', self.s['epoch'])
        self.assertFalse(value['replayable'])

    def test_private_server_binding_cannot_be_supplied_in_document(self):
        source = self.forged()
        source['replayBinding'] = ['owner', source['digest'], 'sample-counter-fixture-v1']
        source['digest'] = digest({k: v for k, v in source.items() if k != 'digest'})
        with self.assertRaises(LiveError) as error:
            self.lab.import_recording(source, 'owner')
        self.assertEqual(error.exception.code, 'invalid_recording')

    def test_derived_identifier_collision_never_overwrites_existing_trust(self):
        local = self.capture()
        external = self.lab.import_recording(self.forged(), 'owner')['recording']
        for source in (local, external):
            target = external if source is local else local
            with patch('reproof.live.recordings.uuid.uuid4', return_value=SimpleNamespace(hex=target['id'])):
                with self.assertRaises(LiveError) as error:
                    self.lab.derive_recording(source['id'], 'owner')
                self.assertEqual(error.exception.code, 'recording_conflict')
            self.assertEqual(self.lab.recording(target['id'], 'owner'), target)

    def test_demotion_is_deterministic_and_preserves_input_privacy_validation(self):
        source = self.forged()
        once = untrusted_recording(source)
        self.assertEqual(untrusted_recording(once), once)
        source['events'][0]['action'] = 'text'
        source['events'][0]['payload'] = {'value': 'not-allowed'}
        source['digest'] = digest({k: v for k, v in source.items() if k != 'digest'})
        with self.assertRaises(LiveError):
            self.lab.import_recording(source, 'owner')


if __name__ == '__main__':
    unittest.main()
