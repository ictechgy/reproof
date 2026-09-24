"""Measured native/host clock boundaries, without a native process."""
import copy

from reproof.core import ContractError
from reproof.live.authority import HELPER_VERSION, NATIVE_PROTOCOL_VERSION
from reproof.live.clock_sync import RecordingTimeAnchor
from reproof.live.model import LiveError
from tests.test_live_authority import AuthorityTestCase, DIGEST_A


class NativeFrameClockTests(AuthorityTestCase):
    def setUp(self):
        super().setUp()
        from reproof.live.native_frame_clock import NativeFrameClock
        self.clock.advance(10_000_000_000)
        self.device = self.claim(parent_grant=self.grant(lifetime_ns=180_000_000_000))
        admission = self.device.admit_operation(
            operation_id="frame-startup", payload_digest=DIGEST_A,
            session_id="frame-session", sequence=1)
        self.permit = self.device.prepare_dispatch(admission, provider_incarnation="provider-one")
        self.handshake = self.device.bind_native_handshake(
            self.permit, protocol_version=NATIVE_PROTOCOL_VERSION,
            helper_version=HELPER_VERSION,
            helper_incarnation=self.permit.helper_incarnation,
            provider_incarnation=self.permit.provider_incarnation,
            native_incarnation="native-one", native_clock_id="android-elapsed-realtime",
            native_time_ms=1000)
        self.sync = self.authority.clock_sync
        self.anchor = RecordingTimeAnchor(self.sync, wall_clock_ms=lambda: 1_000_000)
        self.timing = NativeFrameClock(self.sync, self.anchor, self.device.check_observation_authority)
        self.status = {
            "protocolVersion": NATIVE_PROTOCOL_VERSION, "helperVersion": HELPER_VERSION,
            "helperIncarnation": self.permit.helper_incarnation,
            "providerIncarnation": self.permit.provider_incarnation,
            "nativeClockId": "android-elapsed-realtime", "nativeIncarnation": "native-one",
            "nativeTimeMs": 1000, "capabilities": {"nativeFrameTimingVersion": 1},
        }

    def probe(self):
        sent = self.sync.sample()
        self.clock.advance(20_000_000)
        self.timing.accept_status(self.handshake, self.status, sent, self.sync.sample())

    def frame(self, start=1100, end=1300):
        return {"nativeFrameId": 1, "nativeTiming": {
            "version": 1, "nativeClockId": "android-elapsed-realtime",
            "nativeIncarnation": "native-one", "captureStartMs": start,
            "captureEndMs": end}}

    def test_delayed_frame_keeps_actual_capture_interval(self):
        self.probe()
        self.clock.advance(2_000_000_000)
        arguments = self.timing.frame_arguments(self.frame())
        stamp = self.anchor.stamp_provider(arguments["provider_clock_binding"],
            arguments["provider_monotonic_ns"],
            capture_start_ns=arguments["provider_capture_start_ns"])
        self.assertLessEqual(stamp.earliest_offset_ms, 100)
        self.assertGreaterEqual(stamp.latest_offset_ms, 320)
        self.assertLess(stamp.latest_offset_ms, 325)
        self.assertEqual(arguments["timing_source"], "provider-mapped")
        self.assertEqual(arguments["native_incarnation"], "native-one")

    def test_declared_timing_rejects_missing_invalid_or_different_incarnation(self):
        self.probe()
        for body in ({"nativeFrameId": 1}, self.frame(end=900)):
            with self.subTest(body=body), self.assertRaises((LiveError, ContractError)):
                self.timing.frame_arguments(body)
        for key, value in (("version", True), ("captureStartMs", 1100.0),
                           ("captureEndMs", True), ("nativeIncarnation", "native-two"),
                           ("nativeClockId", "wall-clock")):
            body = self.frame()
            body["nativeTiming"][key] = value
            with self.subTest(key=key), self.assertRaises((LiveError, ContractError)):
                self.timing.frame_arguments(body)

    def test_old_helper_stays_unmapped_and_cannot_upgrade_from_frame_wire(self):
        self.timing.configure(self.handshake, {})
        self.assertEqual(self.timing.frame_arguments(self.frame()),
                         {"timing_source": "native-unmapped"})
        with self.assertRaises((LiveError, ContractError)):
            self.timing.configure(self.handshake, {"nativeFrameTimingVersion": 1})

    def test_probe_requires_same_helper_and_clock_and_refreshes(self):
        self.probe()
        self.assertFalse(self.timing.refresh_due())
        self.clock.advance(31_000_000_000)
        self.assertTrue(self.timing.refresh_due())
        status = copy.deepcopy(self.status)
        status["nativeIncarnation"] = "native-two"
        with self.assertRaises((LiveError, ContractError)):
            self.timing.accept_status(self.handshake, status, self.sync.sample(), self.sync.sample())
        self.status["nativeTimeMs"] += 31_020
        self.probe()
        self.assertFalse(self.timing.refresh_due())

    def test_native_initiated_exchange_cannot_accept_wire_samples_or_reused_nonce(self):
        self.timing.configure(self.handshake, self.status["capabilities"])
        response = self.timing.begin_exchange({"nativeClockId": "android-elapsed-realtime",
            "nativeIncarnation": "native-one", "nativeSendMs": 1000})
        self.assertEqual(set(response), {"ok", "exchangeId"})
        self.clock.advance(20_000_000)
        body = {"exchangeId": response["exchangeId"], "nativeReceiveMs": 1020}
        self.assertEqual(self.timing.finish_exchange(body), {"ok": True})
        self.clock.advance(1_000_000_000)
        self.assertEqual(self.timing.frame_arguments(self.frame())["timing_source"], "provider-mapped")
        with self.assertRaises((LiveError, ContractError)):
            self.timing.finish_exchange(body)
        with self.assertRaises((LiveError, ContractError)):
            self.timing.begin_exchange({"nativeClockId": "android-elapsed-realtime",
                "nativeIncarnation": "native-one", "nativeSendMs": 1000,
                "hostReceivedNs": 1, "hostSentNs": 1})

    def test_expired_exchange_and_revoked_owner_cannot_bind_late_result(self):
        self.timing.configure(self.handshake, self.status["capabilities"])
        response = self.timing.begin_exchange({"nativeClockId": "android-elapsed-realtime",
            "nativeIncarnation": "native-one", "nativeSendMs": 1000})
        self.clock.advance(6_000_000_000)
        with self.assertRaises((LiveError, ContractError)):
            self.timing.finish_exchange({"exchangeId": response["exchangeId"],
                                         "nativeReceiveMs": 7000})
        self.clock.advance(180_000_000_000)
        with self.assertRaises((LiveError, ContractError)):
            self.probe()

    def test_capture_can_observe_inflight_effect_but_cannot_clear_quarantine_or_revocation(self):
        self.assertEqual(self.device.status, "quarantined")
        with self.assertRaises(ContractError):
            self.device.check_ownership()
        self.probe()
        self.assertEqual(self.device.status, "quarantined")
        self.device.revoke_dispatches()
        with self.assertRaises(ContractError):
            self.timing.frame_arguments(self.frame())

    def test_clock_refresh_cannot_hide_a_native_jump_or_regression(self):
        self.probe()
        self.clock.advance(31_000_000_000)
        self.status['nativeTimeMs'] = 32020
        self.probe()
        self.clock.advance(31_000_000_000)
        for value in (2000, 90000):
            self.status['nativeTimeMs'] = value
            with self.subTest(value=value), self.assertRaises((ContractError, LiveError)):
                self.probe()

    def test_observation_authority_rejects_changed_owner_and_unrelated_quarantine(self):
        self.device.check_observation_authority()
        for field, replacement in (('generation', self.device.generation + 1),
                                   ('helper_incarnation', 'different-helper')):
            before = getattr(self.device, field)
            setattr(self.device, field, replacement)
            try:
                with self.subTest(field=field), self.assertRaises(ContractError):
                    self.device.check_observation_authority()
            finally:
                setattr(self.device, field, before)
        self.authority.store.quarantine_clock(device_fingerprint=self.device._device_fingerprint,
            generation=self.device.generation, host_incarnation=self.authority.host_incarnation,
            now_ns=self.clock.nanoseconds)
        with self.assertRaises(ContractError):
            self.device.check_observation_authority()

    def test_closed_clock_rejects_frames_and_late_exchange(self):
        self.probe()
        pending = self.timing.begin_exchange({'nativeClockId': 'android-elapsed-realtime',
            'nativeIncarnation': 'native-one', 'nativeSendMs': 1020})
        self.timing.close()
        with self.assertRaises(LiveError):
            self.timing.frame_arguments(self.frame())
        with self.assertRaises(LiveError):
            self.timing.finish_exchange({'exchangeId': pending['exchangeId'], 'nativeReceiveMs': 1030})

    def test_slow_probe_refreshes_before_drift_exhausts_its_uncertainty_budget(self):
        sent = self.sync.sample()
        self.clock.advance(180_000_000)
        self.timing.accept_status(self.handshake, self.status, sent, self.sync.sample())
        self.assertFalse(self.timing.refresh_due())
        self.clock.advance(20_000_000_000)
        self.assertTrue(self.timing.refresh_due())
