"""Simulator cleanup ACK contracts, with inert transport and no native execution.

Exercise the real provider queue/ACK path and real Lab/HostAuthority ownership.
Only startup and transport are synthetic; Popen is forbidden in every test.
"""
import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from reproof.core import ContractError
from reproof.live.authority import HELPER_VERSION, NATIVE_PROTOCOL_VERSION, HostAuthority
from reproof.live.model import Lab, LiveError
from reproof.live.providers import IosProvider
from reproof.storage import Lease
from tests.test_live_authority_integration import Clock, parent_grant


BUNDLE = 'example.cleanup-target'
EVIDENCE = {'bundleId': BUNDLE, 'state': 'not-running',
            'observer': 'xctest-application-state'}


class InlineBridge:
    """Deliver an inert helper ACK synchronously, retaining real pending state."""
    def __init__(self, provider, reply=None, after_ack=None):
        self.provider = provider
        self.reply = reply
        self.after_ack = after_ack
        self.commands = []
        self.pending = None
        self.body = None

    def put_nowait(self, command):
        self.commands.append(copy.deepcopy(command))
        self.pending = self.provider.pending[command['id']]
        self.body = {'id': command['id'], 'ok': True, 'timing': 'best-effort'}
        if 'authority' in command:
            self.body['authority'] = copy.deepcopy(command['authority'])
        if command['action'] == 'authority_cleanup':
            self.body['cleanupEvidence'] = copy.deepcopy(EVIDENCE)
        if self.reply is not None:
            self.reply(self.body, command)
        self.provider.bridge('ack', self.body)
        if self.after_ack is not None:
            self.after_ack()


def invalid_evidence():
    values = {'null': None, 'list': [], 'string': 'not-running', 'empty': {}}
    for key in EVIDENCE:
        missing = dict(EVIDENCE)
        del missing[key]
        values['missing_' + key] = missing
        for wrong in (None, True, 1, ['not-running']):
            values[key + '_' + repr(wrong)] = dict(EVIDENCE, **{key: wrong})
    values.update({
        'wrong_bundle': dict(EVIDENCE, bundleId='example.other-app'),
        'running': dict(EVIDENCE, state='running'),
        'wrong_observer': dict(EVIDENCE, observer='host-process-exit'),
        'extra_key': dict(EVIDENCE, extra=True),
    })
    return values


class IOSCleanupAckTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('subprocess.Popen', side_effect=AssertionError('Native execution forbidden')))
        self.provider = IosProvider('synthetic', '/tmp/inert-products', BUNDLE)
        self.provider.bridge('ready', {'capabilities': {'targetBoundaryVersion': 1}})
        self.provider.device_authority = Mock()
        self.provider.native_handshake = object()
        self.grant = {'operationId': 'cleanup-command', 'ownershipGeneration': 1,
                      'protocolVersion': 2, 'nativeDeadlineMs': 9000,
                      'hostIncarnation': 'host-one', 'providerIncarnation': 'provider-one'}
        self.provider.device_authority.native_grant.return_value.wire.return_value = self.grant
        self.permit = SimpleNamespace(operation_id='cleanup-command')

    def execute(self, *, action='authority_cleanup', reply=None):
        self.bridge = InlineBridge(self.provider, reply)
        self.provider.commands = self.bridge
        return self.provider.execute_authorized(action, {}, self.permit)

    def rejected(self, reply, *, action='authority_cleanup'):
        with self.assertRaises(LiveError) as caught:
            self.execute(action=action, reply=reply)
        self.assertEqual(caught.exception.code, 'authority_rejected')
        self.assertIsNone(self.bridge.pending['result'])
        self.assertFalse(self.bridge.pending['event'].is_set())
        self.assertEqual(self.provider.pending, {})

    def test_success_requires_exact_target_termination_evidence(self):
        self.assertEqual(self.execute(), {'ok': True, 'timing': 'best-effort'})
        self.assertEqual(self.bridge.pending['action'], 'authority_cleanup')
        self.assertTrue(self.bridge.pending['event'].is_set())
        self.assertEqual(self.provider.pending, {})

    def test_missing_cleanup_evidence_cannot_acknowledge_success(self):
        self.rejected(lambda body, _: body.pop('cleanupEvidence'))

    def test_malformed_or_unbound_cleanup_evidence_is_rejected(self):
        for name, evidence in invalid_evidence().items():
            with self.subTest(name=name):
                self.rejected(lambda body, _, value=evidence: body.update(cleanupEvidence=value))

    def test_ordinary_actions_cannot_carry_cleanup_evidence(self):
        for action in ('tap', 'home', 'launch'):
            with self.subTest(action=action):
                self.rejected(lambda body, _: body.update(cleanupEvidence=dict(EVIDENCE)), action=action)

    def test_transport_cannot_change_the_trusted_pending_action(self):
        def forged(body, command):
            command['action'] = 'authority_cleanup'
            body['cleanupEvidence'] = dict(EVIDENCE)
        self.rejected(forged, action='tap')
        self.assertEqual(self.bridge.pending['action'], 'tap')

    def test_failed_cleanup_cannot_include_termination_evidence(self):
        self.rejected(lambda body, _: body.update(ok=False, error='cleanup_failed'))

    def test_failed_cleanup_without_evidence_remains_rejected_result(self):
        def failed(body, _):
            body.pop('cleanupEvidence')
            body.update(ok=False, error='cleanup_failed')
        self.assertEqual(self.execute(reply=failed), {
            'ok': False, 'timing': 'best-effort', 'outcome': 'rejected', 'code': 'cleanup_failed'})

    def test_typed_success_and_timing_remain_required(self):
        for value in (1, 0, 'true', None):
            with self.subTest(ok=value):
                self.rejected(lambda body, _, value=value: body.update(ok=value))
        self.rejected(lambda body, _: body.update(timing='provider'))

    def test_authority_staleness_types_and_exact_keys_remain_checked(self):
        variants = []
        for key, value in self.grant.items():
            variants.append(dict(self.grant, **{key: value + 1 if type(value) is int else value + '-stale'}))
            missing = dict(self.grant)
            del missing[key]
            variants.append(missing)
        variants.extend([None, [], dict(self.grant, extra=True),
                         dict(self.grant, ownershipGeneration=True),
                         dict(self.grant, ownershipGeneration=1.0)])
        for authority in variants:
            with self.subTest(authority=authority):
                self.rejected(lambda body, _, value=authority: body.update(authority=value))

    def test_unknown_ack_keys_are_not_admitted_by_cleanup(self):
        for key in ('action', 'networkEvidence', 'unexpected'):
            with self.subTest(key=key):
                self.rejected(lambda body, _, key=key: body.update({key: 'authority_cleanup'}))

    def test_ordinary_and_legacy_ack_contracts_are_unchanged(self):
        self.assertEqual(self.execute(action='tap'), {'ok': True, 'timing': 'best-effort'})
        self.provider.device_authority = None
        self.provider.commands = InlineBridge(self.provider)
        self.assertEqual(self.provider.execute('tap', {}), {'ok': True, 'timing': 'best-effort'})
        self.provider.commands = InlineBridge(self.provider,
            lambda body, _: body.update(cleanupEvidence=dict(EVIDENCE)))
        with self.assertRaises(LiveError):
            self.provider.execute('tap', {})

    def test_unknown_command_cannot_acknowledge_pending_cleanup(self):
        with self.assertRaises(LiveError) as caught:
            self.execute(reply=lambda body, _: body.update(id='stale-command'))
        self.assertEqual(caught.exception.code, 'unknown_command')
        self.assertIsNone(self.bridge.pending['result'])
        self.assertFalse(self.bridge.pending['event'].is_set())


class IOSCleanupAckOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('subprocess.Popen', side_effect=AssertionError('Native execution forbidden')))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = Clock()
        self.authority = HostAuthority(self.root / 'authority.sqlite3', clock=self.clock,
                                       lease_directory=self.root / 'leases')
        self.addCleanup(self.authority.close)
        self.provider = IosProvider('synthetic-cleanup', self.root / 'products', BUNDLE)
        self.provider.start_authorized = self.start
        descriptor = {'id': 'sim', 'name': 'Inert Simulator', 'platform': 'ios',
            'kind': 'ios-simulator', 'capabilities': {'actions': ['tap']},
            'factory': lambda: self.provider,
            '_authority': {'deviceKind': 'ios-simulator', 'physicalId': 'synthetic-cleanup'}}
        self.lab = Lab([descriptor], self.root / 'live', authority=self.authority,
                       parent_grant=parent_grant(self.authority, self.clock))
        self.session = self.lab.create_session('sim', 'owner', 'browser')
        self.handle = self.provider.device_authority
        self.assertEqual(self.session['state'], 'active')

    def start(self, session, lab, permit):
        self.provider._check_permit(permit)
        self.provider.native_handshake = self.provider.device_authority.bind_native_handshake(
            permit, protocol_version=NATIVE_PROTOCOL_VERSION, helper_version=HELPER_VERSION,
            helper_incarnation=permit.helper_incarnation, provider_incarnation=permit.provider_incarnation,
            native_incarnation='inert-native', native_clock_id='ios-mach-continuous', native_time_ms=1000)
        lab.publish_frame(session['id'], b'<svg/>', 'image/svg+xml', 400, 800)
        return {'ok': True}

    def close(self, reply=None, after_ack=None):
        self.bridge = InlineBridge(self.provider, reply, after_ack)
        self.provider.commands = self.bridge
        return self.lab.close_session(self.session['id'], 'owner')

    def ownership(self):
        return self.authority.inventory_ownership(device_kind='ios-simulator',
                                                 physical_id='synthetic-cleanup')

    def assert_quarantined(self):
        self.assertEqual(self.lab.list_devices()[0]['state'], 'quarantined')
        self.assert_no_release()

    def assert_no_release(self):
        ownership = self.ownership()
        self.assertEqual(ownership['status'], 'quarantined')
        self.assertIsNone(ownership['releaseReceiptDigest'])
        with self.assertRaises(ContractError):
            with Lease('ios-simulator:synthetic-cleanup', self.root / 'leases'):
                self.fail('Unconfirmed cleanup released legacy device access')

    def test_valid_cleanup_releases_real_authority_and_legacy_lease(self):
        self.assertEqual(self.close()['state'], 'closed')
        self.assertEqual(self.lab.list_devices()[0]['state'], 'available')
        self.assertEqual(self.ownership()['status'], 'released')
        self.assertIsNotNone(self.ownership()['releaseReceiptDigest'])
        with Lease('ios-simulator:synthetic-cleanup', self.root / 'leases'):
            pass
        self.assertEqual([command['action'] for command in self.bridge.commands], ['authority_cleanup'])

    def test_missing_cleanup_evidence_retains_quarantine_and_lease(self):
        self.assertEqual(self.close(lambda body, _: body.pop('cleanupEvidence'))['state'], 'failed')
        self.assert_quarantined()

    def test_wrong_bundle_retains_quarantine_and_lease(self):
        self.assertEqual(self.close(lambda body, _: body['cleanupEvidence'].update(
            bundleId='example.other-app'))['state'], 'failed')
        self.assert_quarantined()

    def test_stale_generation_retains_quarantine_and_lease(self):
        self.assertEqual(self.close(lambda body, _: body['authority'].update(
            ownershipGeneration=body['authority']['ownershipGeneration'] + 1))['state'], 'failed')
        self.assert_quarantined()

    def test_failed_cleanup_retains_quarantine_and_lease(self):
        def failed(body, _):
            body.pop('cleanupEvidence')
            body.update(ok=False, error='cleanup_failed')
        self.assertEqual(self.close(failed)['state'], 'failed')
        self.assert_quarantined()

    def test_missing_ack_retains_quarantine_and_lease(self):
        event = threading.Event()
        self.provider.commands = Mock()
        with patch.object(event, 'wait', return_value=False), \
                patch('reproof.live.providers.threading.Event', return_value=event):
            result = self.lab.close_session(self.session['id'], 'owner')
        self.assertEqual(result['state'], 'failed')
        self.assert_quarantined()
        self.assertEqual(self.provider.pending, {})

    def test_revocation_before_cleanup_never_dispatches_or_releases(self):
        self.lab.fail(self.session['id'], 'Synthetic authority revocation')
        self.assertEqual(self.close()['state'], 'failed')
        self.assertEqual(self.bridge.commands, [])
        self.assert_quarantined()

    def test_revocation_after_valid_ack_cannot_release_ownership(self):
        def revoke():
            self.handle.revoke_dispatches()
        # Lab's existing terminal confirmation rejects revocation; do not
        # weaken it or change its exception contract to accept a late ACK.
        with self.assertRaises(ContractError):
            self.close(after_ack=revoke)
        self.assert_no_release()

    def test_expiry_after_valid_ack_preserves_terminal_confirmation(self):
        def expire():
            self.clock.now += 120_000_000_000
        self.assertEqual(self.close(after_ack=expire)['state'], 'closed')
        self.assertEqual(self.ownership()['status'], 'released')
        with Lease('ios-simulator:synthetic-cleanup', self.root / 'leases'):
            pass


if __name__ == '__main__':
    unittest.main()
