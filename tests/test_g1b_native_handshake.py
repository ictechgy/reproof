"""Strict native handshake boundary probes; no native process is launched."""

from reproloop.core import ContractError
from reproloop.live.authority import HELPER_VERSION, NATIVE_PROTOCOL_VERSION
from tests.test_live_authority import AuthorityTestCase, DIGEST_A


class NativeHandshakeBoundaries(AuthorityTestCase):
    def setUp(self):
        super().setUp()
        self.device = self.claim(parent_grant=self.grant(lifetime_ns=60_000_000_000))
        admission = self.device.admit_operation(
            operation_id='native-startup', payload_digest=DIGEST_A,
            session_id='native-session', sequence=1)
        self.permit = self.device.prepare_dispatch(
            admission, provider_incarnation='provider-one')

    def handshake(self, **overrides):
        fields = dict(
            protocol_version=NATIVE_PROTOCOL_VERSION,
            helper_version=HELPER_VERSION,
            helper_incarnation=self.permit.helper_incarnation,
            provider_incarnation=self.permit.provider_incarnation,
            native_incarnation='native-one',
            native_clock_id='android-elapsed-realtime',
            native_time_ms=100_000)
        fields.update(overrides)
        return self.device.bind_native_handshake(self.permit, **fields)

    def test_supported_native_clock_preserves_operation_identity(self):
        grant = self.device.native_grant(self.permit, self.handshake())
        self.assertEqual(grant.operation_id, self.permit.operation_id)
        self.assertEqual(grant.payload_digest, self.permit.payload_digest)
        self.assertEqual(grant.sequence, self.permit.sequence)
        self.assertEqual(grant.ownership_generation, self.permit.ownership_generation)

    def test_float_protocol_version_is_rejected(self):
        with self.assertRaises(ContractError):
            self.handshake(protocol_version=float(NATIVE_PROTOCOL_VERSION))

    def test_float_helper_version_is_rejected(self):
        with self.assertRaises(ContractError):
            self.handshake(helper_version=float(HELPER_VERSION))

    def test_unqualified_clock_cannot_receive_an_effect_deadline(self):
        with self.assertRaises(ContractError):
            handshake = self.handshake(native_clock_id='wall-clock-local')
            self.device.native_grant(self.permit, handshake)

    def test_wrong_platform_clock_cannot_receive_an_effect_deadline(self):
        with self.assertRaises(ContractError):
            handshake = self.handshake(native_clock_id='ios-mach-continuous')
            self.device.native_grant(self.permit, handshake)
