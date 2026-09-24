import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from reproof.android_profile import validate_app_profile
from reproof.live.android_live import AndroidLiveProvider, android_live_device
from reproof.live.model import LiveError


def profile_document():
    return {
        'schemaVersion': 1,
        'id': 'inventory',
        'package': 'io.reproof.inventory',
        'activity': '.MainActivity',
        'fixture': {'id': 'empty_inventory', 'version': 1, 'inputs': {}},
        'startState': {'screen': 'inventory', 'nodes': {'quantity': '0', 'label': ''}},
        'targets': {
            'tap': ['commit'], 'text': ['label'], 'numeric': ['quantity'],
            'scroll': {}, 'back': 'go_back', 'report': 'export_capture',
        },
        'oracle': {
            'bugCondition': {'target': 'quantity', 'text': '2'},
            'expectedCondition': {'target': 'quantity', 'text': '1'},
        },
        'build': {
            'task': ':app:assembleDebug',
            'apk': 'app/build/outputs/apk/debug/app-debug.apk',
            'regressionTask': ':app:testDebugUnitTest',
            'regressionResults': 'app/build/test-results/testDebugUnitTest',
        },
        'edit': {
            'kind': 'kotlin_numeric_expression_v1',
            'path': 'app/src/main/java/example/Stock.kt',
            'function': 'unitsPerItem',
        },
    }


class _Device:
    identity = 'device-id'


class AndroidLiveProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = validate_app_profile(profile_document())

    def test_explicit_profile_controls_identity_and_native_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / 'helper.apk'
            sample = Path(directory) / 'inventory.apk'
            helper.write_bytes(b'helper')
            sample.write_bytes(b'inventory')
            with patch('reproof.live.android_live.AdbDevice', return_value=_Device()):
                provider = AndroidLiveProvider('serial', helper, sample, app_profile=self.profile)
            self.assertEqual(provider.target_package, 'io.reproof.inventory')
            self.assertEqual(provider._component_name(), 'io.reproof.inventory/.MainActivity')
            self.assertEqual(provider.identity['appProfileDigest'], self.profile.digest)
            self.assertEqual(provider.fixture, 'inventory')
            self.assertEqual(provider.fixture_spec['id'], 'empty_inventory')

            provider.transport = Mock()
            provider.transport.call.return_value = {
                'ready': True, 'nodes': [{'id': 'quantity', 'text': '0'}],
                'profileDigest': self.profile.digest,
                'nativeDigest': self.profile.native_digest,
            }
            observation = provider.observe()
            self.assertEqual(observation['nodes'], [{'id': 'quantity', 'text': '0'}])

    def test_native_profile_mismatch_is_rejected(self):
        provider = object.__new__(AndroidLiveProvider)
        provider.app_profile = self.profile
        provider.explicit_profile = True
        with self.assertRaises(LiveError) as raised:
            provider._validate_native_metadata({
                'profileDigest': self.profile.digest,
                'nativeDigest': '0' * 64,
            })
        self.assertEqual(raised.exception.code, 'native_profile_mismatch')

    def test_default_device_keeps_sample_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / 'helper.apk'
            sample = Path(directory) / 'sample.apk'
            helper.write_bytes(b'helper')
            sample.write_bytes(b'sample')
            with patch('reproof.live.android_live.AdbDevice', return_value=_Device()):
                device = android_live_device('serial', helper, sample)
            self.assertEqual(device['capabilities']['resetContract'], 'sample-counter-fixture-v1')
            self.assertNotIn('appProfileDigest', device['capabilities']['applicationIdentity'])


if __name__ == '__main__':
    unittest.main()
