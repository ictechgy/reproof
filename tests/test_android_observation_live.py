import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from reproof.android_observation import validate_observation_profile
from reproof.android_profile import validate_android_runtime_profile
from reproof.live.android_live import AndroidLiveProvider, android_live_device
from reproof.live.model import LiveError
from reproof.storage import sha_file
from tests.test_android_observation import ordinary_views_project
from tests.test_app_logs import snapshot, RUN
from tests.test_worker_profiles import android_document


class AndroidObservationLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        document = ordinary_views_project(self.root / 'original')
        document['instrumentation'] = {'kind': 'android_asm_v1', 'sites': [{
            'id': 's123abc', 'path': 'app/src/main/java/example/MainActivity.kt',
            'line': 12, 'target': 'save', 'kind': 'tap'}]}
        self.observation = validate_observation_profile(document)
        self.app = self.root / 'inventory.apk'
        self.helper = self.root / 'helper.apk'
        self.helper.write_bytes(b'helper')
        self.write_apk(document)

    def write_apk(self, document):
        with zipfile.ZipFile(self.app, 'w') as archive:
            archive.writestr('classes.dex', b'host contract test')
            if document is not None:
                archive.writestr('assets/reproof-observation.json', json.dumps(document))
        value = android_document(sha_file(self.app))
        value['artifact']['bytes'] = self.app.stat().st_size
        value['package'] = 'com.example.inventory'
        self.runtime = validate_android_runtime_profile(value)

    def provider(self):
        with patch('reproof.live.android_live.AdbDevice'):
            return AndroidLiveProvider('owned-test', self.helper, self.app, runtime_profile=self.runtime)

    def log(self, provider):
        value = snapshot()
        value.update(applicationId=self.observation.data['package'], profileDigest=self.observation.digest,
                     runId=provider.app_log_run_id)
        for event in value['events']:
            if event['type'] == 'click':
                event['target'] = 'save'
            if event['type'] == 'screen':
                event['target'] = 'inventory'
        return value

    def test_adapter_declaration_requires_matching_embedded_apk_configuration(self):
        self.assertEqual(self.provider().observation_profile.digest, self.observation.digest)
        for document in (None, dict(self.observation.data, package='com.example.other')):
            self.write_apk(document)
            with self.assertRaises(LiveError):
                self.provider()
            with patch('reproof.live.android_live.AdbDevice'), self.assertRaises(LiveError):
                android_live_device('owned-test', self.helper, self.app, runtime_profile=self.runtime)

    def test_collector_binds_embedded_profile_targets_and_original_marker(self):
        from reproof.app_logs import IDENTITY_KEYS
        provider = self.provider()
        provider.app_log_run_id = RUN
        value = self.log(provider)
        marker = {key: value[key] for key in IDENTITY_KEYS}
        provider._read_sdk_json = Mock(side_effect=lambda path, **kwargs:
            copy.deepcopy(marker if path.endswith('app-log-session.json') else value))
        self.assertNotEqual(self.runtime.digest, self.observation.digest)
        self.assertEqual(provider.collect_app_logs(), value)
        self.assertEqual(provider.app_log_marker, marker)
        value['events'][2]['target'] = 'checkout_button'
        with self.assertRaises(LiveError):
            provider.collect_app_logs()
        value['events'][2]['target'] = 'save'
        marker['sessionId'] = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        with self.assertRaises(LiveError):
            provider.collect_app_logs()

    def test_launch_rotates_and_binds_logs_before_acknowledging_success(self):
        provider = self.provider()
        provider.transport = Mock()
        commands = []
        def call(path, body=None):
            if path == '/command':
                commands.append(body)
                return {'accepted': True}
            return {'id': commands[-1]['id'], 'ok': True}
        provider.transport.call.side_effect = call
        provider._wait_for_app_log_marker = Mock()
        payload = {'applicationId': self.runtime.data['applicationId']}
        provider.execute('launch', payload)
        first = provider.app_log_run_id
        self.assertEqual(commands[-1]['payload']['appLogProfileDigest'], self.observation.digest)
        self.assertEqual(commands[-1]['payload']['appLogRunId'], first)
        provider.execute('launch', payload)
        self.assertNotEqual(provider.app_log_run_id, first)
        self.assertEqual(provider._wait_for_app_log_marker.call_count, 2)
        self.assertEqual(payload, {'applicationId': self.runtime.data['applicationId']})
        provider._wait_for_app_log_marker.side_effect = LiveError('app_log_unavailable', 'test')
        with self.assertRaises(LiveError):
            provider.execute('launch', payload)

    def test_changed_artifact_is_rejected_before_any_device_mutation(self):
        provider = self.provider()
        self.app.write_bytes(b'changed-after-registration')
        provider._install = Mock(side_effect=AssertionError('Installation must not run'))
        with self.assertRaises(LiveError):
            provider.start({'id': 'owned-session'}, Mock())
        provider._install.assert_not_called()
        provider.device.lease.assert_not_called()
        self.assertIsNone(provider.artifact_temp)

    def test_log_wait_does_not_accept_old_runs_or_hide_revoked_authority(self):
        provider = self.provider()
        provider.app_log_run_id = RUN
        provider.collect_app_logs = Mock(side_effect=LiveError('app_log_unavailable', 'old-run'))
        with self.assertRaises(LiveError):
            provider._wait_for_app_log_marker(timeout=.01)
        provider._check_permit = Mock(side_effect=LiveError('authority_expired', 'revoked'))
        provider.collect_app_logs.reset_mock()
        with self.assertRaises(LiveError) as raised:
            provider._wait_for_app_log_marker(timeout=.01, permit=object())
        self.assertEqual(raised.exception.code, 'authority_expired')
        provider.collect_app_logs.assert_not_called()

    def test_observation_launch_requires_the_native_capability_before_install(self):
        provider = self.provider()
        metadata = {'profileDigest': self.runtime.digest, 'nativeDigest': self.runtime.native_digest,
            'targetPackage': self.runtime.package, 'generalProfile': True,
            'capabilities': {'actions': self.runtime.data['capabilities']['actions']}}
        with self.assertRaises(LiveError):
            provider._validate_native_metadata(metadata)
        metadata['capabilities']['viewsObservationLaunchVersion'] = 2
        self.assertEqual(provider._validate_native_metadata(metadata), metadata)
        metadata['capabilities']['actions'] = str(metadata['capabilities']['actions'])
        with self.assertRaises(LiveError):
            provider._validate_native_metadata(metadata)


if __name__ == '__main__':
    unittest.main()
