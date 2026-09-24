"""iOS egress policy, counter measurement, and fail-closed verdict tests."""
from dataclasses import replace
import json
import threading
import time
import unittest

from reproof import contracts
from reproof.core import ContractError
from reproof.ios_egress import (
    counter_delta, egress_policy, load_egress_policy, measurement_evidence,
    network_counters)
from reproof.repair_callbacks import invoke_fixed
from reproof.repair_ios import IOSTrustedMobileAdapter
from reproof.repair_mobile import (
    MobileFailureObservation, MobileInstallationObservation)
from tests.ios_service_support import IOSServiceFixture


def policy_document(**changes):
    document = {
        "kind": "ios-egress-policy", "version": 1, "mode": "deny-all",
        "interfaces": ["en0", "pdp_ip"], "noiseFloorBytes": 1048576,
        "allowlist": [], "capture": {"rvictl": "off"},
    }
    document.update(changes)
    return document


def counters(en0_rx=1000, en0_tx=500, pdp_rx=100, pdp_tx=50, **extra):
    interfaces = {
        "en0": {"rxBytes": en0_rx, "txBytes": en0_tx},
        "pdp_ip0": {"rxBytes": pdp_rx, "txBytes": pdp_tx},
    }
    interfaces.update(extra)
    return {"schema": "ios-network-counters", "version": 1,
            "sampledAtMs": 100, "interfaces": interfaces}


class IOSEgressPolicyTests(unittest.TestCase):

    def test_valid_policy_loads_with_stable_digest(self):
        policy = egress_policy(policy_document())
        self.assertEqual(policy.data["mode"], "deny-all")
        self.assertEqual(policy.interfaces, ("en0", "pdp_ip"))
        self.assertEqual(policy.noise_floor_bytes, 1048576)
        self.assertEqual(policy.capture_mode, "off")
        self.assertEqual(policy.digest, contracts.digest(policy.data))
        self.assertEqual(egress_policy(policy.data).digest, policy.digest)

    def test_load_policy_from_bytes(self):
        raw = json.dumps(policy_document()).encode()
        self.assertEqual(load_egress_policy(raw).data["mode"], "deny-all")
        for value in (b"{}", b"not-json", b"", "doc", None):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ContractError):
                    load_egress_policy(value)

    def test_policy_rejects_shape_and_mode(self):
        for value in (None, [], policy_document(kind="egress"),
                      policy_document(version=2), policy_document(mode="audit"),
                      {**policy_document(), "extra": 1}):
            with self.subTest(value=str(value)[:60]):
                with self.assertRaises(ContractError):
                    egress_policy(value)

    def test_policy_rejects_bad_interfaces(self):
        for interfaces in ([], ["En0"], ["-bad"], ["lo0"], ["utun0"],
                           ["awdl0"], ["en0", "en0"], ["x" * 17], [1],
                           ["en0"] * 2):
            with self.subTest(interfaces=interfaces):
                with self.assertRaises(ContractError):
                    egress_policy(policy_document(interfaces=interfaces))

    def test_policy_rejects_bad_noise_floor(self):
        for floor in (-1, True, 64 * 1024 * 1024 + 1, "1", 1.5):
            with self.subTest(floor=floor):
                with self.assertRaises(ContractError):
                    egress_policy(policy_document(noiseFloorBytes=floor))
        self.assertEqual(
            egress_policy(policy_document(noiseFloorBytes=0)).noise_floor_bytes, 0)

    def test_policy_rejects_bad_allowlist_and_capture(self):
        for allowlist in ([{"host": "a.com"}], [{"host": "a.com", "port": 0}],
                          [{"host": "a.com", "port": 65536}],
                          [{"host": "bad host", "port": 443}],
                          [{"host": "a.com", "port": 443}] * 17, "x"):
            with self.subTest(allowlist=str(allowlist)[:50]):
                with self.assertRaises(ContractError):
                    egress_policy(policy_document(allowlist=allowlist))
        for capture in ({"rvictl": "always"}, {"rvictl": "optional", "x": 1},
                        {}, "optional"):
            with self.subTest(capture=capture):
                with self.assertRaises(ContractError):
                    egress_policy(policy_document(capture=capture))
        self.assertEqual(
            egress_policy(policy_document(
                capture={"rvictl": "optional"})).capture_mode, "optional")


class IOSNetworkCounterTests(unittest.TestCase):

    def test_valid_counters_canonicalize(self):
        document = counters(en0_rx=7)
        parsed = network_counters(document)
        self.assertEqual(parsed["en0"]["rxBytes"], 7)
        self.assertEqual(set(parsed), {"en0", "pdp_ip0"})

    def test_counters_reject_bad_shape(self):
        for value in (None, [], {"schema": "ios-network-counters"},
                      {**counters(), "extra": 1},
                      {**counters(), "version": 2},
                      {**counters(), "sampledAtMs": -1},
                      {**counters(), "sampledAtMs": "now"}):
            with self.subTest(value=str(value)[:60]):
                with self.assertRaises(ContractError):
                    network_counters(value)

    def test_counters_reject_bad_interface_rows(self):
        bad = (
            {"BAD IFACE": {"rxBytes": 1, "txBytes": 1}},
            {"en0": {"rxBytes": -1, "txBytes": 1}},
            {"en0": {"rxBytes": True, "txBytes": 1}},
            {"en0": {"rxBytes": 1}},
            {"en0": {"rxBytes": 1, "txBytes": 1, "extra": 0}},
            {"en0": {"rxBytes": 2 ** 63, "txBytes": 1}},
            {f"en{i}": {"rxBytes": 1, "txBytes": 1} for i in range(65)},
        )
        for interfaces in bad:
            with self.subTest(interfaces=str(interfaces)[:60]):
                with self.assertRaises(ContractError):
                    network_counters(counters() | {"interfaces": interfaces})

    def test_counter_delta_scopes_and_sums(self):
        start = network_counters(counters(en0_rx=1000, en0_tx=500,
                                          lo0={"rxBytes": 9, "txBytes": 9}))
        end = network_counters(counters(en0_rx=1600, en0_tx=900,
                                        lo0={"rxBytes": 99, "txBytes": 99}))
        delta, names = counter_delta(start, end, ("en0", "pdp_ip"))
        # en0: (1600-1000)+(900-500)=1000, pdp_ip0는 시작과 끝이 같아 0.
        # lo0는 정책 스코프 밖이라 제외된다.
        self.assertEqual(delta, 1000)
        self.assertEqual(names, ["en0", "pdp_ip0"])

    def test_counter_delta_is_fail_closed_on_reset_or_vanish(self):
        start = network_counters(counters(en0_rx=5000, en0_tx=4000))
        # 윈도우 중 카운터 리셋 — 관측 불가 구간은 큰 쪽(시작값)을 계상한다.
        end = network_counters(counters(en0_rx=10, en0_tx=10))
        delta, _ = counter_delta(start, end, ("en0",))
        self.assertEqual(delta, 5000 + 4000)
        # 윈도우 중 인터페이스 소멸 — 시작값 전체를 계상한다.
        end = network_counters(
            {"schema": "ios-network-counters", "version": 1,
             "sampledAtMs": 200, "interfaces": {}})
        delta, names = counter_delta(start, end, ("en0",))
        self.assertEqual(delta, 5000 + 4000)
        self.assertEqual(names, ["en0"])

    def test_measurement_evidence_pass_and_violation(self):
        policy = egress_policy(policy_document())
        start = counters()
        end = counters(en0_rx=1700, en0_tx=500, pdp_rx=100, pdp_tx=50)
        evidence = measurement_evidence(policy, start, end,
                                        capture={"mode": "off"})
        self.assertEqual(evidence["verdict"], "pass")
        self.assertEqual(evidence["deltaBytes"], 700)
        self.assertEqual(evidence["policyDigest"], policy.digest)
        self.assertEqual(evidence["scope"]["matched"], ["en0", "pdp_ip0"])
        big = counters(en0_rx=1000 + 3 * 1024 * 1024)
        evidence = measurement_evidence(policy, start, big,
                                        capture={"mode": "captured"})
        self.assertEqual(evidence["verdict"], "violation")
        self.assertGreater(evidence["deltaBytes"], policy.noise_floor_bytes)
        for capture in ({"mode": "recorded"}, {}, "off"):
            with self.subTest(capture=capture):
                with self.assertRaises(ContractError):
                    measurement_evidence(policy, start, end, capture=capture)
        for broken in (None, {"interfaces": {}}, "x"):
            with self.subTest(broken=str(broken)[:40]):
                with self.assertRaises(ContractError):
                    measurement_evidence(policy, broken, end,
                                         capture={"mode": "off"})


def egress_fixture_policy():
    return egress_policy(policy_document(capture={"rvictl": "optional"}))


class IOSEgressAdapterTests(unittest.TestCase):
    """egress 정책이 묶인 실서비스 fixture에서 카운터 윈도우 판정을 검증한다."""

    def setUp(self):
        self.fixture = IOSServiceFixture(egress=egress_fixture_policy())
        self.adapter = None
        self._manager = None

    def tearDown(self):
        manager = getattr(self, "_manager", None)
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass
        if self.adapter is not None:
            try:
                self.adapter.close(deadline_monotonic=time.monotonic() + 20)
            except Exception:
                pass
        self.fixture.close()

    def bounds(self):
        return {"cancellation": threading.Event(),
                "deadline_monotonic": time.monotonic() + 120}

    def admitted(self):
        manager = self.fixture.admit()
        operation = manager.__enter__()
        context = replace(self.fixture.context, _operation_binding=operation)
        self._manager = manager
        return context

    def fixed(self, callback, *args):
        value, returned = invoke_fixed(
            callback, *args, cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 120)
        self.assertTrue(returned)
        return value

    def _install(self):
        self.adapter = IOSTrustedMobileAdapter(
            self.fixture.config, operations=self.fixture.operations)
        context = self.admitted()
        installed = self.fixed(
            self.adapter.install, context, self.fixture.candidate)
        self.assertIsInstance(installed, MobileInstallationObservation)
        return context

    def test_replay_below_noise_floor_passes(self):
        context = self._install()
        result = self.fixed(
            self.adapter.replay, context, self.fixture.execution, 1)
        self.assertNotIsInstance(result, MobileFailureObservation)
        cleaned = self.fixed(self.adapter.cleanup, context)
        self.assertTrue(cleaned.ownership_released, cleaned)

    def test_counter_delta_above_floor_is_egress_violation(self):
        # 샘플 사이에 인터페이스당 egress_delta만큼 카운터가 오른다.
        self.fixture.egress_delta = 4 * 1024 * 1024
        context = self._install()
        result = self.fixed(
            self.adapter.replay, context, self.fixture.execution, 1)
        self.assertIsInstance(result, MobileFailureObservation)
        self.assertEqual(result.code, "egress_violation")
        cleaned = self.fixed(self.adapter.cleanup, context)
        self.assertTrue(cleaned.termination_confirmed, cleaned)

    def _quarantined_replay(self):
        """증거 부재/파손은 raise(미확정) 또는 실패 관측 중 하나로만 끝난다."""
        context = self._install()
        try:
            result = self.adapter.replay(
                context, self.fixture.execution, 1, **self.bounds())
        except Exception:
            return context, None
        self.assertIsInstance(result, MobileFailureObservation)
        self.assertIn(result.code,
                      ("mobile_replay_failed", "mobile_quarantined"))
        return context, result

    def test_missing_counter_evidence_never_passes(self):
        # 기기가 카운터 증거를 생략하면 activation 단계에서 fail-closed.
        self.fixture.network_evidence_mode = "missing"
        context, result = self._quarantined_replay()
        cleaned = self.adapter.cleanup(context, **self.bounds())
        self.assertFalse(cleaned.ownership_released)
        self.assertEqual(
            self.fixture.env.lab.list_devices()[0]["state"], "quarantined")

    def test_malformed_counter_evidence_never_passes(self):
        self.fixture.network_evidence_mode = "malformed"
        context, result = self._quarantined_replay()
        cleaned = self.adapter.cleanup(context, **self.bounds())
        self.assertFalse(cleaned.ownership_released)
        self.assertEqual(
            self.fixture.env.lab.list_devices()[0]["state"], "quarantined")


if __name__ == "__main__":
    unittest.main()
