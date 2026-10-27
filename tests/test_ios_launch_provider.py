"""Approved launch transport tests; no helper, Simulator or app is executed."""
import copy
import io
import plistlib
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from reproof.core import ContractError
from reproof.ios_launch import validate_ios_launch
from reproof.ios_profile import validate_ios_profile
from reproof.live.model import LiveError
from reproof.live.providers import IosProvider, ios_device
from tests import test_ios_helper_capabilities as support
from tests.test_worker_profiles import physical_ios_document


def selection(profile):
    return validate_ios_launch({
        'schemaVersion': 1, 'kind': 'reproof-approved-ios-launch',
        'referenceId': profile.data['approvedReferences']['launch'],
        'profileDigest': profile.digest,
        'artifactDigest': profile.data['artifact']['sha256'],
        'environment': [{'key': 'QA_SERVER', 'type': 'string', 'value': 'https://qa.example.test'}],
    }, profile)


class IosLaunchProviderTests(unittest.TestCase):
    def setUp(self):
        self.support = support.IOSHelperCapabilitiesTests(methodName='runTest')
        self.support.setUp()
        self.profile = self.support.profile
        self.selected = selection(self.profile)
        self.enterContext(patch('subprocess.Popen', side_effect=AssertionError('Native execution forbidden')))

    def provider(self):
        provider = self.support.simulator()
        provider.launch_config = self.selected
        return provider

    def ready(self):
        return dict(self.support.ready({'actions': self.support.actions, 'targetBoundaryVersion': 1}),
                    approvedLaunch={'version': 1, 'digest': self.selected.digest})

    def test_start_serializes_only_private_selection_and_never_argv(self):
        provider = self.provider()
        provider.lease = Mock()
        provider.lease.__enter__ = Mock()
        provider._start_runner = Mock()
        permit = Mock(helper_incarnation='helper', host_incarnation='host', provider_incarnation='provider')
        def template(products, name, destination):
            with destination.open('wb') as stream:
                plistlib.dump({'ReproLiveTests': {'EnvironmentVariables': {}}}, stream)
            return destination
        try:
            with patch('reproof.live.native_frame_clock.NativeFrameClock.for_session', return_value=Mock()), \
                    patch('reproof.live.providers._general_ios_simulator_identity'), \
                    patch('reproof.live.ios_artifact.stage_simulator_app', return_value=Path('/tmp/frozen.app')), \
                    patch('reproof.live.providers.prepare_xctestrun', side_effect=template), \
                    patch('reproof.live.providers.require_ios_helper_v2'), \
                    patch('reproof.live.providers._targets', side_effect=lambda doc: list(doc.items())), \
                    patch('reproof.live.providers.threading.Thread') as thread, \
                    patch('reproof.live.providers.subprocess.run') as install:
                provider.start({'id': 'session'}, Mock(base_url='http://localhost'), permit)
                install.assert_not_called()
                thread.return_value.start.assert_called_once()
            with (Path(provider.temp.name)/'live.xctestrun').open('rb') as stream:
                environment = plistlib.load(stream)['ReproLiveTests']['EnvironmentVariables']
            private = {key: value for key, value in environment.items() if key.startswith('REPRO_LIVE_APPROVED_LAUNCH')}
            self.assertEqual(private, self.selected.native())
            self.assertEqual(environment['REPRO_LIVE_GENERAL_PROFILE_DIGEST'], self.profile.digest)
            self.assertNotIn('QA_SERVER', environment)
            self.assertNotIn('qa.example', str(provider._start_runner.call_args))
            self.assertEqual(provider._pending_install_identity,
                             {'bundle': self.profile.bundle, 'artifactDigest': self.profile.data['artifact']['sha256']})
        finally:
            if provider.temp is not None: provider.temp.cleanup()

    def test_missing_wrong_stale_handshake_precedes_install_and_authority(self):
        bad = [None, {}, {'version': True, 'digest': self.selected.digest},
               {'version': 2, 'digest': self.selected.digest},
               {'version': 1, 'digest': 'f'*64},
               {'version': 1, 'digest': self.selected.digest, 'value': 'private'}]
        for value in bad:
            with self.subTest(value=value):
                provider = self.provider(); body = self.ready()
                if value is None: del body['approvedLaunch']
                else: body['approvedLaunch'] = value
                with patch('reproof.live.providers.subprocess.run') as install:
                    with self.assertRaises(LiveError) as raised: provider.bridge('ready', body)
                self.assertEqual(raised.exception.code, 'native_launch_mismatch')
                install.assert_not_called()
                provider.device_authority.bind_native_handshake.assert_not_called()
                provider.device_authority.native_grant.assert_not_called()
                self.assertFalse(provider._target_boundary_ready)
                self.assertNotIn('qa.example', str(raised.exception))

    def test_matching_handshake_keeps_existing_install_and_grant(self):
        provider = self.provider()
        with patch('reproof.live.providers.subprocess.run', return_value=Mock(returncode=0)) as install, \
                patch('reproof.live.providers.installed_identity', return_value=provider._pending_install_identity):
            self.assertEqual(provider.bridge('ready', self.ready()),
                             {'ok': True, 'authority': {'synthetic': True}})
        install.assert_called_once()
        provider.device_authority.native_grant.assert_called_once()
        self.assertTrue(provider._target_boundary_ready)

    def test_no_selection_rejects_unsolicited_helper_configuration(self):
        provider = self.support.simulator()
        with patch('reproof.live.providers.subprocess.run') as install, self.assertRaises(LiveError):
            provider.bridge('ready', self.ready())
        install.assert_not_called()
        self.assertFalse(provider._target_boundary_ready)

    def test_shared_authority_required_before_any_start_effect(self):
        provider = self.provider(); provider.device_authority = None
        with patch('reproof.live.providers.Lease') as lease, \
                patch('reproof.live.providers._general_ios_simulator_identity') as artifact:
            with self.assertRaises(LiveError): provider.start({'id': 'test'}, Mock())
        lease.assert_not_called(); artifact.assert_not_called()
        with self.assertRaises(LiveError): provider.bridge('ready', self.ready())

    def test_selection_requires_general_profile_and_shared_descriptor(self):
        with self.assertRaises(ContractError):
            IosProvider('synthetic', '/tmp/products', 'com.example.target', launch_config=self.selected)
        with patch('reproof.live.providers._general_ios_simulator_identity') as artifact:
            with self.assertRaises(LiveError):
                ios_device('synthetic', '/tmp/products', self.profile.bundle, app='/tmp/app',
                           profile=self.profile, launch_config=self.selected, authority_mode='legacy-offline-v1')
            artifact.assert_not_called()

    def test_public_descriptor_has_digest_only_and_no_reset_promotion(self):
        with patch('reproof.live.providers._general_ios_simulator_identity', return_value=self.profile.application_identity):
            descriptor = ios_device('synthetic', '/tmp/products', self.profile.bundle, app='/tmp/app',
                                    profile=self.profile, launch_config=self.selected)
        self.assertEqual(descriptor['capabilities']['approvedLaunch'], {
            'referenceId': self.selected.data['referenceId'], 'configurationDigest': self.selected.digest})
        self.assertIsNone(descriptor['capabilities']['resetContract'])
        self.assertNotIn('qa.example', str(descriptor['capabilities']))
        self.assertEqual(descriptor['factory']().launch_config.digest, self.selected.digest)

    def test_live_cli_forwards_operator_selection_without_reading_app(self):
        from reproof.live.cli import main
        class Configured(Exception): pass
        with patch('reproof.ios_runner.IosSimulator'), \
                patch('reproof.ios_profile.load_ios_profile', return_value=self.profile), \
                patch('reproof.ios_launch.load_ios_launch', return_value=self.selected) as loader, \
                patch('reproof.live.cli.ios_device', side_effect=Configured) as device:
            with self.assertRaises(Configured):
                main(['live-serve', '--simulator', 'synthetic', '--products', '/tmp', '--ios-app', '/tmp/selected.app',
                      '--ios-profile', '/tmp/profile.json', '--ios-launch-config', '/tmp/launch.json'])
        loader.assert_called_once_with(Path('/tmp/launch.json'), self.profile)
        self.assertEqual(device.call_args.kwargs['launch_config'], self.selected)
        self.assertEqual(device.call_args.kwargs['profile'], self.profile)
        self.assertEqual(device.call_args.kwargs['app'], Path('/tmp/selected.app'))
        self.assertEqual(device.call_args.kwargs['authority_mode'], 'shared-v2')

    def test_worker_forwards_general_simulator_operator_selection(self):
        from reproof.live.worker_cli import main
        class Configured(Exception): pass
        with patch('reproof.live.worker_cli.stdin_object', return_value={'token': 'synthetic-transport'}), \
                patch('reproof.ios_profile.load_ios_profile', return_value=self.profile), \
                patch('reproof.ios_launch.load_ios_launch', return_value=self.selected) as loader, \
                patch('reproof.live.worker_cli.ios_device', side_effect=Configured) as device:
            with self.assertRaises(Configured):
                main(['--output', '/tmp/not-written', '--token-stdin', '--simulator', 'synthetic',
                      '--ios-products', '/tmp/products', '--ios-profile', '/tmp/profile.json',
                      '--ios-app', '/tmp/selected.app', '--ios-launch-config', '/tmp/launch.json'])
        loader.assert_called_once_with(Path('/tmp/launch.json'), self.profile)
        self.assertEqual(device.call_args.kwargs['launch_config'], self.selected)
        self.assertEqual(device.call_args.kwargs['profile'], self.profile)
        self.assertEqual(device.call_args.kwargs['app'], Path('/tmp/selected.app'))
        self.assertEqual(device.call_args.kwargs['authority_mode'], 'shared-v2')
        self.assertEqual(device.call_args.args[2], self.profile.bundle)

    def test_live_cli_rejects_unsupported_options_without_device_access(self):
        from reproof.live.cli import main
        for arguments in ([], ['--iphone', 'physical'], ['--simulator', 'synthetic', '--authority-mode', 'legacy-offline-v1']):
            with self.subTest(arguments=arguments), patch('sys.stderr', new_callable=io.StringIO), \
                    patch('reproof.live.cli.ios_device') as device, self.assertRaises(SystemExit):
                main(['live-serve', '--ios-launch-config', '/not/read.json', *arguments])
            device.assert_not_called()

    def test_worker_rejects_unsupported_or_mixed_launch_options_before_credentials(self):
        from reproof.live.worker_cli import main
        variants = [[], ['--simulator', 'synthetic'],
                    ['--simulator', 'synthetic', '--ios-profile', 'profile', '--ios-app', 'app', '--iphone', 'physical'],
                    ['--simulator', 'synthetic', '--ios-profile', 'profile', '--ios-app', 'app', '--authority-mode', 'legacy-offline-v1'],
                    ['--devices-config', 'devices']]
        for arguments in variants:
            with self.subTest(arguments=arguments), patch('sys.stderr', new_callable=io.StringIO), \
                    patch('reproof.live.worker_cli.stdin_object') as credentials, self.assertRaises(SystemExit):
                main(['--output', '/not/written', '--ios-launch-config', '/not/read.json', *arguments])
            credentials.assert_not_called()


if __name__ == '__main__': unittest.main()
