import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from reproof.android_launch import (validate_android_launch, load_android_launch,
                                    require_android_launch)
from reproof.android_profile import validate_android_runtime_profile
from reproof.core import ContractError
from reproof.live.android_live import AndroidLiveProvider, android_live_device
from reproof.live.model import LiveError
from tests.test_worker_profiles import android_document


def fixture():
    document = android_document()
    document['capabilities']['observations'].remove('logs')
    document['capabilities']['logAdapter'] = None
    profile = validate_android_runtime_profile(document)
    config = {'schemaVersion': 1, 'kind': 'reproof-approved-android-launch',
              'referenceId': profile.data['approvedReferences']['launch'],
              'profileDigest': profile.digest,
              'extras': [{'key': 'QA_SERVER', 'type': 'string', 'value': 'https://qa.example.invalid/base'},
                         {'key': 'QA_MOCK_MODE', 'type': 'boolean', 'value': True},
                         {'key': 'QA_FIXTURE_VERSION', 'type': 'integer', 'value': 7}]}
    return profile, config


class AndroidLaunchTests(unittest.TestCase):
    def test_selection_is_immutable_and_bound_to_complete_profile(self):
        profile, config = fixture()
        selected = validate_android_launch(config, profile)
        config['extras'][0]['value'] = 'changed'
        self.assertNotEqual(selected.data, config)
        self.assertEqual(require_android_launch(selected, profile).digest, selected.digest)
        changed = profile.data;changed['buildId'] = 'candidate'
        with self.assertRaises(ContractError):
            require_android_launch(selected, validate_android_runtime_profile(changed))
        raw = base64.b64decode(selected.native()['configurationBase64'])
        self.assertEqual(raw, json.dumps(selected.data, sort_keys=True, separators=(',', ':')).encode())
        self.assertNotIn('qa.example', repr(selected))

    def test_canonical_golden_url_empty_and_integer_bounds(self):
        import hashlib
        profile, config = fixture()
        config['extras'] = [{'key':'QA_URL','type':'string','value':'https://qa.example.invalid/base'},
                            {'key':'QA_EMPTY','type':'string','value':''},
                            {'key':'QA_COUNT','type':'integer','value':-2147483648}]
        expected = ('{"extras":[{"key":"QA_URL","type":"string","value":"https://qa.example.invalid/base"},'
                    '{"key":"QA_EMPTY","type":"string","value":""},'
                    '{"key":"QA_COUNT","type":"integer","value":-2147483648}],'
                    '"kind":"reproof-approved-android-launch","profileDigest":"' + profile.digest + '",'
                    '"referenceId":"' + config['referenceId'] + '","schemaVersion":1}').encode('ascii')
        selected = validate_android_launch(config, profile)
        self.assertEqual(base64.b64decode(selected.native()['configurationBase64']), expected)
        self.assertEqual(selected.digest, hashlib.sha256(expected).hexdigest())
        self.assertNotIn(b'\\/', expected)

    def test_strict_shapes_types_limits_and_reserved_keys(self):
        profile, config = fixture()
        variants = []
        for key in ('referenceId', 'profileDigest', 'kind'):
            value = copy.deepcopy(config);value[key] = 'wrong';variants.append(value)
        value = copy.deepcopy(config);value['schemaVersion'] = True;variants.append(value)
        for key in ('PATH', 'repro_mode', 'QA_TOKEN', 'QA_API_KEY', 'QA_SESSION', 'android.intent.extra', 'QA_FOO;echo'):
            value = copy.deepcopy(config);value['extras'][0]['key'] = key;variants.append(value)
        for kind, payload in [('integer', True), ('integer', 2**31), ('boolean', 1),
                              ('string', '$(echo bad)'), ('string', 'abc\n'), ('string', '한글')]:
            value = copy.deepcopy(config);value['extras'][0].update(type=kind, value=payload);variants.append(value)
        value = copy.deepcopy(config);value['extras'] *= 6;variants.append(value)
        value = copy.deepcopy(config);value['extras'] = [];variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_android_launch(value, profile)

    def test_urls_reject_credentials_queries_fragments_and_unapproved_schemes(self):
        profile, config = fixture()
        for url in ('http://qa.example', 'https://user:pass@qa.example', 'https://qa.example/?token=x',
                    'https://qa.example/?', 'https://qa.example/#fragment', 'https://qa.example:1234/', 'https://qa.example/%40name',
                    'file:///tmp/x', 'https://qa.example bad'):
            config['extras'][0]['value'] = url
            with self.subTest(url=url), self.assertRaises(ContractError):
                validate_android_launch(config, profile)

    def test_bounded_loader_rejects_duplicate_fields_and_links(self):
        profile, config = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();path = root/'launch.json'
            path.write_text(json.dumps(config))
            self.assertEqual(load_android_launch(path, profile).data, config)
            path.write_text(json.dumps(config)[:-1] + ',"schemaVersion":1}')
            with self.assertRaises(ContractError):load_android_launch(path, profile)
            path.write_text('x'*8193)
            with self.assertRaises(ContractError):load_android_launch(path, profile)
            link = root/'link.json';link.symlink_to(path)
            with self.assertRaises(ContractError):load_android_launch(link, profile)

    def provider(self):
        profile, config = fixture()
        selected = validate_android_launch(config, profile)
        with patch('reproof.live.android_live.AdbDevice'), patch('reproof.live.android_live._general_android_observation', return_value=None):
            provider = AndroidLiveProvider('fake', Path('/tmp/helper.apk'), Path('/tmp/app.apk'),
                                           runtime_profile=profile, launch_config=selected)
        status = {'profileDigest': profile.digest, 'nativeDigest': profile.native_digest,
                  'targetPackage': profile.package, 'generalProfile': True,
                  'capabilities': {'actions': profile.data['capabilities']['actions'], 'launchConfigurationVersion': 1},
                  'launchConfigurationDigest': selected.digest, 'ready': True}
        return provider, status

    def test_native_capability_digest_gate_before_command(self):
        for version, digest in [(None, None), (True, 'wrong'), (1, 'wrong'), (2, 'wrong')]:
            provider, status = self.provider()
            status['capabilities']['launchConfigurationVersion'] = version
            status['launchConfigurationDigest'] = digest
            provider.transport = Mock();provider.transport.call.return_value = status
            with self.assertRaises(LiveError):provider._execute('launch', {'applicationId': 'android_app'})
            self.assertEqual([call.args[0] for call in provider.transport.call.call_args_list], ['/status'])
            self.assertFalse(provider.launch_configuration_verified)

    def test_launch_uses_same_selected_config_not_input_values(self):
        provider, status = self.provider();commands = []
        def call(path, body=None):
            if path == '/status':return copy.deepcopy(status)
            if path == '/command':commands.append(body);return {'accepted': True}
            return {'id': commands[-1]['id'], 'ok': True}
        provider.transport = Mock();provider.transport.call.side_effect = call
        for _ in range(2):
            result = provider._execute('launch', {'applicationId': 'android_app'})
            self.assertTrue(result['ok'])
        self.assertEqual([c['payload'] for c in commands], [{'applicationId': 'android_app'}]*2)
        self.assertNotIn('qa.example', json.dumps(commands))
        self.assertEqual(provider.launch_config.digest, status['launchConfigurationDigest'])

    def test_configured_descriptor_discloses_digest_not_values_and_no_reset(self):
        profile, config = fixture();selected = validate_android_launch(config, profile)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'app.apk';path.write_bytes(b'fake')
            with patch('reproof.live.android_live.AdbDevice'), patch('reproof.live.android_live._general_android_observation'):
                device = android_live_device('fake',path,path,runtime_profile=profile,launch_config=selected)
            self.assertIsNone(device['capabilities']['resetContract'])
            self.assertNotIn('reset', device['capabilities']['actions'])
            self.assertNotIn('qa.example', json.dumps(device['capabilities']))
            self.assertEqual(device['capabilities']['approvedLaunch']['configurationDigest'], selected.digest)


    def test_configuration_rebinding_rejects_before_adb_construction(self):
        profile, config = fixture();selected = validate_android_launch(config, profile)
        changed = profile.data;changed['buildId'] = 'candidate'
        with patch('reproof.live.android_live.AdbDevice') as adb, self.assertRaises(ContractError):
            AndroidLiveProvider('fake', '/tmp/helper.apk', '/tmp/app.apk',
                runtime_profile=validate_android_runtime_profile(changed), launch_config=selected)
        adb.assert_not_called()

    def test_trusted_worker_configuration_loads_selection_not_raw_values(self):
        from reproof.live.worker_cli import configured_local_devices
        profile, config = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'profile.json').write_text(json.dumps(profile.data))
            (root/'launch.json').write_text(json.dumps(config))
            value = {'schemaVersion': 1, 'devices': [{'platform': 'android', 'deviceId': 'test-device',
                'profile': 'profile.json', 'application': 'app.apk', 'helper': 'helper.apk', 'launchConfig': 'launch.json'}]}
            (root/'worker.json').write_text(json.dumps(value))
            with patch('reproof.live.android_live.android_live_device', return_value={'id': 'fake'}) as factory:
                configured_local_devices(root/'worker.json')
            self.assertEqual(factory.call_args.kwargs['launch_config'].data, config)
            self.assertEqual(factory.call_args.kwargs['authority_mode'], 'shared-v2')
            value['devices'][0]['platform'] = 'ios-physical'
            (root/'worker.json').write_text(json.dumps(value))
            with self.assertRaises(LiveError):configured_local_devices(root/'worker.json')

    def test_default_profile_does_not_require_new_helper_capability(self):
        provider, status = self.provider();provider.launch_config = None
        del status['capabilities']['launchConfigurationVersion']
        del status['launchConfigurationDigest']
        provider._validate_native_metadata(status)
        self.assertFalse(provider.launch_configuration_verified)

    def test_native_source_wires_config_parse_echo_and_fixed_typed_extras(self):
        root = Path(__file__).resolve().parents[1]
        helper = (root/'android/live/src/main/java/io/reproof/live/LiveInstrumentation.kt').read_text()
        parser = (root/'android/live/src/main/java/io/reproof/live/ApprovedLaunch.kt').read_text()
        self.assertIn('ApprovedLaunch.parse(', helper)
        self.assertIn('put("launchConfigurationVersion", 1)', helper)
        self.assertIn('put("launchConfigurationDigest", liveConfig?.approvedLaunch?.digest', helper)
        self.assertIn('$logArgument$approvedArguments', helper)
        self.assertIn('canonical(config) != text', parser)
        self.assertNotIn('JSONObject.quote', parser)
        self.assertIn('jsonString(value)', parser)
        for flag in ('--es', '--ez', '--ei'):self.assertIn(flag, parser)
        self.assertNotIn('Runtime.getRuntime', parser)
        host = (root/'reproof/live/android_live.py').read_text()
        self.assertIn('write_live_configuration(command,config_bytes)', host)
        self.assertIn('input=config_bytes', host)
        self.assertNotIn('json.dumps(config).encode()', host)

    def test_cli_rejects_option_without_android_profile_before_device_access(self):
        from reproof.live.cli import main
        with patch('reproof.live.cli.LiveServer'), self.assertRaises(SystemExit):
            main(['live-serve', '--android-launch-config', '/not/read.json'])


if __name__ == '__main__':unittest.main()
