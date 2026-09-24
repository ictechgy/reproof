"""Exercise the physical iPhone receipt path with real host authority."""

import copy
from pathlib import Path
import tempfile
import unittest

from reproof.live.authority import HostAuthority, ProviderResult, issue_local_parent_grant
from reproof.live.clock_sync import ClockReading
from reproof.live.iphone import PhysicalIosProvider
from reproof.live.model import LiveError
from reproof.live.providers import IosProvider
from reproof.core import ContractError, digest
from reproof.storage import Lease


class Clock:
    def read(self):
        return ClockReading('receipt-test-clock', 'd' * 64, 1_000_000_000, 0)


class Transport:
    def __init__(self, mutation=None):
        self.mutation = mutation
        self.command = None

    def call(self, path, body=None):
        if path == '/command':
            self.command = copy.deepcopy(body)
            return {'accepted': True}
        if path == '/ack/' + self.command['id']:
            authority = copy.deepcopy(self.command['authority'])
            if self.mutation is not None:
                self.mutation(authority)
            return {'pending': False, 'id': self.command['id'], 'ok': True,
                    'timing': 'best-effort', 'authority': authority}
        raise AssertionError('Unexpected synthetic native route')


class NativeReceiptBindings(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='g1b-native-receipt-')
        root = Path(self.temporary.name)
        self.authority = HostAuthority(root / 'authority.sqlite3', clock=Clock(),
                                       lease_directory=root / 'leases')
        grant = issue_local_parent_grant(self.authority, lifetime_ns=60_000_000_000)
        self.owner = self.authority.claim_device(
            device_kind='ios-physical', physical_id='synthetic-iphone',
            helper_incarnation='helper-one', parent_grant=grant)
        startup = self.owner.admit_operation(
            operation_id='startup-one', payload_digest='a' * 64,
            session_id='session-one', sequence=1)
        startup_permit = self.owner.prepare_dispatch(startup, provider_incarnation='provider-one')
        handshake = self.owner.bind_native_handshake(
            startup_permit, protocol_version=2, helper_version=2,
            helper_incarnation='helper-one', provider_incarnation='provider-one',
            native_incarnation='native-one', native_clock_id='ios-mach-continuous',
            native_time_ms=1000)
        self.owner.confirm_operation(startup_permit, ProviderResult('startup-receipt', 'succeeded', 'b' * 64))
        admission = self.owner.admit_operation(
            operation_id='input-one', payload_digest='c' * 64,
            session_id='session-one', sequence=2)
        self.permit = self.owner.prepare_dispatch(admission, provider_incarnation='provider-one')
        # Initialize only the transport-independent base; no paired-device
        # discovery, signing metadata or native installation is accessed.
        self.provider = object.__new__(PhysicalIosProvider)
        IosProvider.__init__(self.provider, 'synthetic-iphone', root, 'io.reproof.synthetic')
        self.provider.bind_authority(self.owner, 'provider-one')
        self.provider.native_handshake = handshake
        self.provider._receive_frame = lambda: None

    def tearDown(self):
        self.authority.close()
        self.temporary.cleanup()

    def execute(self, mutation=None):
        self.provider.transport = Transport(mutation)
        return self.provider.execute_authorized('tap', {'x': .5, 'y': .5}, self.permit)

    def test_valid_full_authority_receipt_is_accepted(self):
        self.assertEqual(self.execute(), {'ok': True, 'timing': 'best-effort'})

    def test_receipt_from_another_generation_is_rejected(self):
        with self.assertRaises(LiveError):
            self.execute(lambda value: value.update(ownershipGeneration=2))

    def test_float_protocol_receipt_is_rejected(self):
        with self.assertRaises(LiveError):
            self.execute(lambda value: value.update(protocolVersion=2.0))

    def test_host_close_without_native_cleanup_does_not_release_the_device(self):
        self.owner.confirm_operation(self.permit, ProviderResult('input-receipt', 'succeeded', 'e' * 64))
        self.authority.close()
        with self.assertRaises(ContractError):
            with Lease('ios-device:synthetic-iphone', Path(self.temporary.name) / 'leases'):
                self.fail('Host close released a helper without confirmed termination')

    def test_only_a_terminal_cleanup_operation_allows_native_release(self):
        self.owner.confirm_operation(self.permit, ProviderResult('input-receipt', 'succeeded', 'e' * 64))
        with self.assertRaises(ContractError):
            self.owner.confirm_native_cleanup(self.permit)
        admission = self.owner.admit_operation(
            operation_id='cleanup-one',
            payload_digest=digest({'kind': 'cleanup', 'payload': {'scope': 'native-helper-and-pointers'}}),
            session_id='session-one', sequence=3)
        permit = self.owner.prepare_dispatch(admission, provider_incarnation='provider-one')
        with self.assertRaises(ContractError):
            self.owner.confirm_native_cleanup(permit)
        self.owner.confirm_operation(permit, ProviderResult('cleanup-receipt', 'succeeded', 'f' * 64))
        self.owner.confirm_native_cleanup(permit)
        self.authority.close()
        with Lease('ios-device:synthetic-iphone', Path(self.temporary.name) / 'leases'):
            pass


if __name__ == '__main__':
    unittest.main()
