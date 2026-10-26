"""General iOS action declarations are checked before startup authority advances.

Native transport, ownership, and installation are explicit doubles. These tests
do not launch helpers, compile native code, or access a Simulator or device.
"""
import copy
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from reproof.ios_profile import validate_ios_profile
from reproof.live.iphone import PhysicalIosProvider
from reproof.live.model import LiveError
from reproof.live.providers import IosProvider
from tests.test_worker_profiles import physical_ios_document


class IOSHelperCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.profile = validate_ios_profile(physical_ios_document())
        self.actions = self.profile.data['capabilities']['actions']

    def invalid_capabilities(self):
        values = {
            'missing_capabilities': None,
            'non_object_capabilities': [],
            'missing_actions': {},
            'null_actions': {'actions': None},
            'string_actions': {'actions': ','.join(self.actions)},
            'tuple_actions': {'actions': tuple(self.actions)},
            'mapping_actions': {'actions': dict.fromkeys(self.actions, True)},
            'empty_actions': {'actions': []},
            'missing_action': {'actions': self.actions[:-1]},
            'extra_action': {'actions': self.actions + ['private_native_action']},
            'legacy_reset': {'actions': ['reset']},
            'duplicate_action': {'actions': self.actions + self.actions[:1]},
            'integer_action': {'actions': self.actions + [1]},
            'boolean_action': {'actions': self.actions + [True]},
            'nested_array_action': {'actions': self.actions + [[]]},
            'nested_object_action': {'actions': self.actions + [{}]},
        }
        return {key: dict(value, targetBoundaryVersion=1) if type(value) is dict else value
                for key, value in values.items()}

    def test_missing_or_malformed_boundary_never_reaches_native_activation(self):
        variants = [{}, *[{'targetBoundaryVersion': value}
                         for value in (None, True, False, 0, 2, 1.0, '1', [], {})]]
        for boundary in variants:
            for general in (False, True):
                with self.subTest(boundary=boundary, general=general):
                    capabilities = dict(boundary, actions=self.actions)
                    provider = self.simulator(general=general)
                    with self.assertRaises(LiveError) as caught:
                        provider.bridge('ready', self.ready(capabilities))
                    self.assertEqual(caught.exception.code, 'native_profile_mismatch')
                    self.assertIn('Rebuild', str(caught.exception))
                    provider.device_authority.bind_native_handshake.assert_not_called()
                    provider.device_authority.native_grant.assert_not_called()
                    physical = self.physical_poll(capabilities, general=general)
                    physical.device_authority.bind_native_handshake.assert_not_called()
                    physical._receive_frame.assert_not_called()
                    self.assertFalse(physical.handshake_ready.is_set())
                    self.assertFalse(physical.startup_complete.is_set())

    def simulator(self, *, general=True):
        profile = self.profile if general else None
        provider = IosProvider('synthetic-simulator', Path('/tmp/synthetic-products'),
            self.profile.bundle, self.profile.application_identity,
            app=Path('/tmp/synthetic-app'), profile=profile)
        provider.device_authority = Mock()
        provider.device_authority.native_grant.return_value.wire.return_value = {'synthetic': True}
        provider._startup_permit = object()
        provider._startup_accepting = True
        provider._startup_deadline = time.monotonic() + 10
        provider._pending_install_identity = {'synthetic': 'selected-app'}
        provider._install_app = Path('/tmp/synthetic-app')
        provider.frame_clock = Mock()
        return provider

    def ready(self, capabilities):
        return {
            'capabilities': copy.deepcopy(capabilities),
            'protocolVersion': 2, 'helperVersion': 2,
            'helperIncarnation': 'synthetic_helper',
            'hostIncarnation': 'synthetic_host',
            'providerIncarnation': 'synthetic_provider',
            'nativeIncarnation': 'synthetic_native',
            'nativeClockId': 'ios-mach-continuous', 'nativeTimeMs': 1,
        }

    def test_simulator_rejects_invalid_actions_before_handshake_install_or_grant(self):
        for name, capabilities in self.invalid_capabilities().items():
            with self.subTest(name=name):
                provider = self.simulator()
                with patch('reproof.live.providers.subprocess.run', return_value=Mock(returncode=0)) as install, \
                        patch('reproof.live.providers.installed_identity',
                              return_value=provider._pending_install_identity):
                    with self.assertRaises(LiveError) as caught:
                        provider.bridge('ready', self.ready(capabilities))
                self.assertEqual((caught.exception.code, caught.exception.status),
                                 ('native_profile_mismatch', 400))
                self.assertNotIn('private_native_action', str(caught.exception))
                provider.device_authority.bind_native_handshake.assert_not_called()
                provider.device_authority.native_grant.assert_not_called()
                provider.frame_clock.configure.assert_not_called()
                install.assert_not_called()
                self.assertIsNone(provider.native_handshake)

    def test_simulator_accepts_exact_actions_in_any_order(self):
        for actions in (self.actions, list(reversed(self.actions))):
            with self.subTest(actions=actions):
                provider = self.simulator()
                with patch('reproof.live.providers.subprocess.run', return_value=Mock(returncode=0)) as install, \
                        patch('reproof.live.providers.installed_identity',
                              return_value=provider._pending_install_identity):
                    result = provider.bridge('ready', self.ready({'actions': actions, 'targetBoundaryVersion': 1}))
                self.assertEqual(result, {'ok': True, 'authority': {'synthetic': True}})
                provider.device_authority.bind_native_handshake.assert_called_once()
                provider.device_authority.native_grant.assert_called_once()
                install.assert_called_once()

    def test_general_legacy_transport_checks_actions_before_waiting_for_app_logs(self):
        provider = self.simulator()
        provider.device_authority = None
        provider.app_logs_only = True
        provider._wait_for_app_log_marker = Mock()
        with self.assertRaises(LiveError):
            provider.bridge('ready', {'capabilities': {'actions': []}})
        provider._wait_for_app_log_marker.assert_not_called()

    def test_legacy_profile_requires_boundary_but_not_general_actions(self):
        provider = self.simulator(general=False)
        provider._pending_install_identity = None
        self.assertEqual(provider.bridge('ready', self.ready({'targetBoundaryVersion': 1})),
                         {'ok': True, 'authority': {'synthetic': True}})
        provider.device_authority = None
        self.assertEqual(provider.bridge('ready', {'capabilities': {'targetBoundaryVersion': 1}}), {'ok': True})

    def physical_poll(self, capabilities, *, general=True):
        # Initialize transport-independent state only, never the physical
        # constructor or its device discovery and tunnel client.
        provider = object.__new__(PhysicalIosProvider)
        IosProvider.__init__(provider, 'synthetic-iphone', Path('/tmp/synthetic-products'),
            self.profile.bundle, self.profile.application_identity,
            app=Path('/tmp/synthetic-app'), profile=self.profile if general else None)
        provider.device_authority = Mock()
        provider._startup_permit = object()
        provider.handshake_ready = threading.Event()
        provider.startup_complete = threading.Event()
        provider.stop = Mock()
        provider.stop.wait.side_effect = [False, True]
        provider.process = Mock()
        provider.process.poll.return_value = None
        provider.lab = Mock()
        provider.sid = 'synthetic-session'
        provider.transport = Mock()
        provider.transport.call.return_value = dict(self.ready(capabilities), ready=True,
            targetBundle=self.profile.bundle, applicationProfileDigest=self.profile.digest)
        provider._validate_native_frame_buffer = Mock()
        provider._receive_frame = Mock()
        provider._poll()
        return provider

    def test_physical_invalid_actions_cannot_signal_startup_or_bind_handshake(self):
        for name, capabilities in self.invalid_capabilities().items():
            with self.subTest(name=name):
                provider = self.physical_poll(capabilities)
                provider.device_authority.bind_native_handshake.assert_not_called()
                provider.device_authority.native_grant.assert_not_called()
                provider._validate_native_frame_buffer.assert_not_called()
                provider._receive_frame.assert_not_called()
                self.assertFalse(provider.handshake_ready.is_set())
                self.assertFalse(provider.startup_complete.is_set())
                self.assertIsNone(provider.native_handshake)
                provider.transport.call.assert_called_once_with('/status', timeout=2)

    def test_physical_accepts_exact_actions_in_any_order(self):
        for actions in (self.actions, list(reversed(self.actions))):
            with self.subTest(actions=actions):
                provider = self.physical_poll({'actions': actions, 'targetBoundaryVersion': 1})
                provider.device_authority.bind_native_handshake.assert_called_once()
                self.assertTrue(provider.handshake_ready.is_set())
                self.assertTrue(provider.startup_complete.is_set())
                provider._receive_frame.assert_called_once()

    def test_physical_legacy_profile_requires_boundary_but_not_general_actions(self):
        provider = self.physical_poll({'targetBoundaryVersion': 1}, general=False)
        provider.device_authority.bind_native_handshake.assert_called_once()
        self.assertTrue(provider.handshake_ready.is_set())
        self.assertTrue(provider.startup_complete.is_set())


if __name__ == '__main__':
    unittest.main()
