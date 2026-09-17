import json
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest

from reproloop.android_profile import validate_app_profile
from reproloop.core import ContractError, compile_capture
from reproloop.device import AdbDevice
from reproloop.replay import replay_suite
from reproloop.storage import create_bundle, load_bundle
try:
    from tests.test_android_profile import profile_document
except ModuleNotFoundError:
    from tests.test_android_profile import profile_document


def profile_capture(profile):
    return {
        'schemaVersion': 1,
        'sessionId': 'profile-replay',
        'fixture': profile.data['fixture'],
        'startState': profile.data['startState'],
        'events': [{'id': 'e1', 'seq': 1, 'action': 'replace', 'target': 'label',
                    'parameters': {'value': 'QA'}},
                   {'id': 'e2', 'seq': 2, 'action': 'tap', 'target': 'commit',
                    'parameters': {}}],
        'truncated': False,
        'lostEvents': False,
        'endSequence': 2,
    }


class ProfileReplayDevice:
    def __init__(self, profile, outcomes):
        self.app_profile = profile
        self.outcomes = iter(outcomes)
        self.quantity = '0'
        self.label = ''
        self.resets = 0
        self.last_profile_receipt = None

    def prepare(self, apk, fixture):
        self.resets += 1
        self.quantity = '0'
        self.label = ''
        self.outcome = next(self.outcomes)
        return {'installedVerified': True, 'fixtureVerified': True, 'apkSha256': 'test',
                'appProfileDigest': self.app_profile.digest,
                'nativeDigest': self.app_profile.native_digest}

    def observe(self):
        self.last_profile_receipt = {'profileDigest': self.app_profile.digest,
                                     'nativeDigest': self.app_profile.native_digest,
                                     'operation': 'observe'}
        return {'label': self.label, 'quantity': self.quantity}

    def execute(self, step):
        if step['action'] == 'replace':
            self.label = step['parameters']['value']
        elif step['action'] == 'tap':
            self.quantity = self.outcome

    def stop(self):
        pass


class ProfileReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = validate_app_profile(profile_document())
        self.capture = profile_capture(self.profile)
        self.apk = self.root / 'original.apk'
        self.apk.write_bytes(b'profile-apk')

    def tearDown(self):
        self.temp.cleanup()

    def test_compile_uses_profile_policy_and_cannot_be_widened(self):
        scenario = compile_capture(self.capture, self.profile.oracle(), app_profile=self.profile)
        self.assertEqual(scenario['appProfileDigest'], self.profile.digest)
        self.assertEqual(scenario['editPolicy'], self.profile.data['edit'])
        bad = json.loads(json.dumps(self.capture))
        bad['events'][0]['target'] = 'quantity'
        with self.assertRaises(ContractError):
            compile_capture(bad, self.profile.oracle(), app_profile=self.profile)
        with self.assertRaises(ContractError):
            compile_capture(self.capture, self.profile.oracle(), tap_targets={'anything'},
                            app_profile=self.profile)

    def test_generic_bundle_requires_the_matching_explicit_profile(self):
        bundle = create_bundle(self.capture, self.profile.oracle(), self.apk,
                               self.root / 'bundle', app_profile=self.profile)
        self.assertEqual(bundle['manifest']['schemaVersion'], 2)
        self.assertEqual(bundle['manifest']['package'], self.profile.data['package'])
        self.assertEqual(bundle['manifest']['appProfileDigest'], self.profile.digest)
        self.assertIn('app-profile.json', bundle['manifest']['files'])
        self.assertIs(bundle['app_profile'], self.profile)
        with self.assertRaises(ContractError):
            load_bundle(bundle['path'])
        other = dict(profile_document())
        other['oracle'] = dict(other['oracle'])
        other['oracle']['expectedCondition'] = {'target': 'quantity', 'text': '3'}
        with self.assertRaises(ContractError):
            load_bundle(bundle['path'], validate_app_profile(other))

    def test_profile_replay_retains_three_runs_and_digests(self):
        bundle = create_bundle(self.capture, self.profile.oracle(), self.apk,
                               self.root / 'bundle', app_profile=self.profile)
        result = replay_suite(ProfileReplayDevice(self.profile, ['2'] * 3), bundle,
                              self.apk, self.root / 'runs')
        self.assertEqual(result['status'], 'reproduced')
        self.assertEqual(len(result['runs']), 3)
        self.assertTrue(all(run['appProfileDigest'] == self.profile.digest
                            and run['nativeDigest'] == self.profile.native_digest
                            and run['nativeProfileProof']['profileDigest'] == self.profile.digest
                            and run['nativeProfileProof']['nativeDigest'] == self.profile.native_digest
                            for run in result['runs']))
        self.assertEqual(result['appProfileDigest'], self.profile.digest)
        self.assertEqual(len(list((self.root / 'runs').glob('run-*.json'))), 3)

    def test_profile_mismatch_blocks_device_work(self):
        bundle = create_bundle(self.capture, self.profile.oracle(), self.apk,
                               self.root / 'bundle', app_profile=self.profile)
        other = dict(profile_document())
        other['oracle'] = dict(other['oracle'])
        other['oracle']['expectedCondition'] = {'target': 'quantity', 'text': '3'}
        wrong = validate_app_profile(other)
        device = ProfileReplayDevice(wrong, ['2'] * 3)
        result = replay_suite(device, bundle, self.apk, self.root / 'runs')
        self.assertEqual(result['status'], 'environment_blocked')
        self.assertEqual(device.resets, 0)

    def test_driver_sends_profile_and_rejects_digest_or_target_mismatch(self):
        device = object.__new__(AdbDevice)
        device.app_profile = self.profile
        device.package = self.profile.data['package']
        device.authority_lease = nullcontext()
        calls = []
        response = {'ok': True, 'profileDigest': self.profile.digest,
                    'nativeDigest': self.profile.native_digest}
        device.shell = lambda *args, **kwargs: calls.append((args, kwargs)) or (
            'INSTRUMENTATION_RESULT: result=' + json.dumps(response) + '\n')
        self.assertTrue(device.driver('tap', target='commit')['ok'])
        command = calls[-1][0]
        self.assertEqual(command[command.index('package') + 1], self.profile.data['package'])
        self.assertIn('app_profile', command)
        self.assertIn('profile_digest', command)
        with self.assertRaises(ContractError):
            device.driver('tap', target='unconfigured')
        response['nativeDigest'] = '0' * 64
        with self.assertRaises(ContractError):
            device.driver('tap', target='commit')


if __name__ == '__main__':
    unittest.main()
