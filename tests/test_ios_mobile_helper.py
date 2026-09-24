"""Bounded iOS helper control over an owned XCTest session and HTTP double."""
from dataclasses import replace
import base64
import json
import os
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from reproof import contracts
from reproof import ios_mobile_helper as helper_module
from reproof.ios_device_tools import IOSDeviceToolError
from reproof.ios_mobile_helper import IOSHelperChannel, command_payload
from reproof.ios_mobile_identity import IOSInstalledIdentityObservation
from reproof.ios_mobile_runtime_identity import IOSRuntimeIdentityReadObservation
from reproof.live.authority import ProviderResult
from reproof.live.model import Lab
from tests import test_ios_mobile_runtime_identity as runtime_fixtures


class OwnedHTTPDouble:
    """Protocol-only endpoint double; no socket, phone, or external network."""

    def __init__(self, address, port, token):
        self.address, self.port, self.token = address, port, token
        self.launch = None
        self.release = None
        self.calls = []
        self.commands = {}
        self.activated = False
        self.ready = False
        self.bad_native_incarnation = False
        self.cleanup_evidence_mode = "valid"
        self.include_cleanup_evidence_for_all = False
        self.wrong_cleanup_authority = False
        self.stale_cleanup_authority = False
        self.cleanup_ack_failure = False
        self.network_evidence_mode = "emit"
        self.egress_delta = 0
        self._network_tick = 0
        self.frame = {
            "id": "native-1", "nativeFrameId": 1,
            "imageBase64": base64.b64encode(b"jpeg").decode("ascii"),
            "mime": "image/jpeg", "width": 100, "height": 200,
            "logicalWidth": 100, "logicalHeight": 200,
            "orientation": "portrait", "capturedAt": 1000,
            "nativeTiming": {
                "version": 1, "nativeClockId": "ios-mach-continuous",
                "nativeIncarnation": "native_helper_double",
                "captureStartMs": 100, "captureEndMs": 100,
            },
        }

    def _status(self):
        launch = self.launch
        return {
            "ready": self.ready,
            "stopped": False,
            "capabilities": {
                "actions": list(launch.payload["actions"]),
                "inputMode": "gesture-batch",
                "media": "sampled-jpeg",
                "nativeFrameBufferVersion": 1,
                "nativeFrameTimingVersion": 1,
            },
            "protocolVersion": 2,
            "helperVersion": 2,
            "helperIncarnation": launch._runner.native_owner.device.helper_incarnation,
            "hostIncarnation": launch._runner.native_owner.device._authority.host_incarnation,
            "providerIncarnation": launch.payload["providerIncarnation"],
            "nativeIncarnation": ("native_wrong" if self.bad_native_incarnation
                                   else "native_helper_double"),
            "nativeClockId": "ios-mach-continuous",
            "nativeTimeMs": 100,
            "targetBundle": launch._runner.query.bundle,
            "applicationProfileDigest": launch.payload["profileDigest"],
        }

    def _egress_bound(self):
        runtime = self.launch.payload.get("runtimeIdentity") if self.launch else None
        return type(runtime) is dict and runtime.get("egressPolicyDigest") is not None

    def _network_evidence(self):
        # egress 바인딩 세션은 샘플마다 카운터 증거를 싣는다.
        # 'missing'은 생략, 'malformed'은 호스트 fail-closed 검증을 유도한다.
        if not self._egress_bound() or self.network_evidence_mode == "missing":
            return None
        if self.network_evidence_mode == "malformed":
            return {"schema": "ios-network-counters", "version": 1, "sampledAtMs": 1,
                    "interfaces": {"Bad Iface": {"rxBytes": 1, "txBytes": 1}}}
        self._network_tick += 1
        offset = self._network_tick * self.egress_delta
        return {"schema": "ios-network-counters", "version": 1,
                "sampledAtMs": 100 + self._network_tick,
                "interfaces": {"en0": {"rxBytes": 1000 + offset, "txBytes": 500 + offset},
                               "pdp_ip0": {"rxBytes": 100 + offset, "txBytes": 50 + offset}}}

    def call(self, path, body=None, timeout=5, binary=False):
        self.calls.append((path, body))
        if path == "/status":
            return self._status()
        if path == "/activate":
            self.activated = True
            self.ready = True
            response = {"activated": True, "authority": body["authority"]}
            evidence = self._network_evidence()
            if evidence is not None:
                response["networkEvidence"] = evidence
            return response
        if path.startswith("/frames/after/"):
            cursor = int(path.rsplit("/", 1)[1])
            if self.frame["nativeFrameId"] <= cursor:
                raise RuntimeError("frame_unavailable")
            return json.dumps(self.frame, separators=(",", ":")).encode("utf-8")
        if path == "/command":
            self.commands[body["id"]] = body
            return {"accepted": True}
        if path.startswith("/ack/"):
            command_id = path[5:]
            body = self.commands[command_id]
            result = {"pending": False, "id": command_id,
                      "ok": not (body["action"] == "authority_cleanup" and self.cleanup_ack_failure),
                      "timing": "best-effort", "authority": body["authority"]}
            if not result["ok"]:
                result["error"] = "target_not_stopped"
            if self.wrong_cleanup_authority and body["action"] == "authority_cleanup":
                result["authority"] = dict(body["authority"], operationId="operation-cross")
            if self.stale_cleanup_authority and body["action"] == "authority_cleanup":
                result["authority"] = dict(body["authority"], sequence=body["authority"]["sequence"] - 1)
            if (result["ok"]
                    and (body["action"] == "authority_cleanup" or self.include_cleanup_evidence_for_all)
                    and self.cleanup_evidence_mode != "missing"):
                evidence = {
                    "bundleId": self.launch._runner.query.bundle,
                    "state": "not-running",
                    "observer": "xctest-application-state",
                }
                if self.cleanup_evidence_mode == "wrong_bundle":
                    evidence["bundleId"] = "com.example.foreign"
                elif self.cleanup_evidence_mode == "wrong_state":
                    evidence["state"] = "running"
                elif self.cleanup_evidence_mode == "wrong_observer":
                    evidence["observer"] = "helper-claim"
                result["cleanupEvidence"] = evidence
            if result["ok"] and body["action"] == "authority_cleanup":
                network = self._network_evidence()
                if network is not None:
                    result["networkEvidence"] = network
            return result
        if path == "/stop":
            if self.release is not None:
                self.release.write_text("stop")
            return {"stopped": True, "authority": body["authority"]}
        raise AssertionError(path)


class IOSMobileHelperTests(unittest.TestCase):
    def setUp(self):
        # This is the real XCTest/owner fixture, with the automatic runtime
        # identity embedded in the prepared candidate artifact.  Only the
        # helper HTTP boundary is replaced below.
        self.fixture = runtime_fixtures.RealIOSRuntimeIdentityTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.case = self.fixture.case

    def _dispatch(self, owner, payload, operation_id, sequence):
        admission = owner.device.admit_operation(
            operation_id=operation_id,
            payload_digest=contracts.digest(payload),
            session_id="owned-session",
            sequence=sequence,
        )
        return owner.device.prepare_dispatch(
            admission, provider_incarnation=self.launch.payload["providerIncarnation"]
        )

    def _confirm(self, owner, permit, kind):
        result_digest = contracts.digest({"kind": kind, "status": "succeeded"})
        return owner.device.confirm_operation(
            permit, ProviderResult("receipt_" + permit.operation_id, "succeeded", result_digest))

    def _running(self):
        context = self.case.owned()
        owner, runner = context.__enter__()
        self.addCleanup(lambda: context.__exit__(None, None, None))
        self.launch = self.case.prepare(runner)
        startup = self.case.permit(self.launch)
        session = self.case.start(runner, self.launch, startup, deadline=time.monotonic() + 12)
        self.case.g.wait_for(self.case.marker.exists)
        runtime = self.launch.payload["runtimeIdentity"]
        identity = IOSInstalledIdentityObservation(
            "install-candidate", owner.binding_digest, owner.operation.context.digest,
            "candidate", self.launch.payload["appDigests"]["candidate"],
            runtime["bundleId"], "1.0", "27", "e" * 64,
        )
        owner._identity_results["install-candidate"] = identity
        runtime_observation = IOSRuntimeIdentityReadObservation(
            owner.operation.context.digest, owner.binding_digest,
            contracts.digest(self.launch.payload), "f" * 64,
            runtime["bundleId"], runtime["buildId"], runtime["profileDigest"], runtime["runId"], 1,
        )
        owner._runtime_results[contracts.digest(self.launch.payload)] = runtime_observation
        double = OwnedHTTPDouble(self.launch._endpoint, runner.tools.port, self.launch._token)
        double.launch = self.launch
        double.release = self.case.release
        with patch("reproof.ios_mobile_helper.TunnelClient", return_value=double):
            channel = IOSHelperChannel(session)
        return owner, runner, session, startup, identity, channel, double

    def _cleanup_ready(self):
        owner, runner, session, startup, identity, channel, double = self._running()
        channel.handshake(startup, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        channel.activate(startup, identity, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        self._confirm(owner, startup, "startup")
        cleanup = self._dispatch(
            owner, command_payload("cleanup", {}), "command-cleanup-proof", 2)
        return owner, runner, session, cleanup, channel, double

    def test_command_payload_matches_lab_text_and_fixes_cleanup_scope(self):
        self.assertEqual(command_payload("text", {"value": "private"}),
                         {"kind": "text", "payload": {"variable": "live-text"}})
        self.assertEqual(
            contracts.digest(command_payload("text", {"value": "private"})),
            Lab._parameterized_digest("text", {"value": "private"}),
        )
        self.assertEqual(command_payload("cleanup", {}),
                         {"kind": "cleanup", "payload": {"scope": "native-helper-and-pointers"}})
        with self.assertRaises(IOSDeviceToolError):
            command_payload("text", {"value": "x", "extra": "private"})

    def test_real_bound_session_handshake_activation_command_and_shutdown(self):
        owner, _runner, _session, startup, identity, channel, double = self._running()
        handshake = channel.handshake(
            startup, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        self.assertEqual(handshake.native_clock_id, "ios-mach-continuous")
        activated = channel.activate(
            startup, identity, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        self.assertTrue(activated["activated"] and activated["ready"])
        self._confirm(owner, startup, "startup")

        new_run_id = str(uuid.uuid4())
        launch_payload = {"applicationId": self.launch.payload["applicationId"],
                          "autoRunId": new_run_id}
        launch_permit = self._dispatch(
            owner, command_payload("launch", launch_payload), "command-launch", 2)
        with self.assertRaises(IOSDeviceToolError):
            channel.command(
                "launch", launch_payload, launch_permit,
                cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        self.assertFalse(any(path == "/command" for path, _ in double.calls))
        result = channel.command(
            "launch", launch_payload, launch_permit,
            cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8,
            expected_auto_run_id=new_run_id)
        self.assertTrue(result["ok"])
        command_posts = [path for path, _ in double.calls if path == "/command"]
        duplicate = channel.command(
            "launch", launch_payload, launch_permit,
            cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8,
            expected_auto_run_id=new_run_id)
        self.assertEqual(duplicate, result)
        self.assertEqual(len([path for path, _ in double.calls if path == "/command"]), len(command_posts))
        self._confirm(owner, launch_permit, "launch")

        cleanup_permit = self._dispatch(
            owner, command_payload("cleanup", {}), "command-cleanup", 3)
        shutdown = channel.shutdown(
            cleanup_permit, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        self.assertTrue(shutdown["host"]["terminated"])
        self.assertTrue(shutdown["helper"]["terminationConfirmed"])
        self.assertTrue(shutdown["target"]["terminationConfirmed"])
        self.assertFalse(owner.device._native_cleanup_confirmed)
        records = b"".join(path.read_bytes() for path in (self.launch._work / "helper-control").rglob("*.json"))
        self.assertNotIn(self.launch._token.encode(), records)

    def test_wrong_grant_and_cancellation_are_rejected_before_http(self):
        owner, _runner, _session, startup, _identity, channel, double = self._running()
        wrong = replace(startup, provider_incarnation="ios-xctest-wrong")
        with self.assertRaises(IOSDeviceToolError):
            channel.handshake(wrong, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 5)
        self.assertEqual(double.calls, [])
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(IOSDeviceToolError):
            channel.handshake(startup, cancellation=cancelled, deadline_monotonic=time.monotonic() + 5)
        self.assertEqual(double.calls, [])

    def test_status_and_frame_after_are_read_only_bounded_observations(self):
        _owner, _runner, _session, startup, _identity, channel, double = self._running()
        channel.handshake(startup, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        status = channel.status(cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 5)
        self.assertEqual(status["nativeClockId"], "ios-mach-continuous")
        frame = channel.frame_after(0, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 5)
        self.assertEqual(frame["nativeFrameId"], 1)
        self.assertEqual(frame["imageBase64"], double.frame["imageBase64"])
        with self.assertRaises(IOSDeviceToolError):
            channel.frame_after(0, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 5)
        cancelled = threading.Event(); cancelled.set()
        calls = len(double.calls)
        with self.assertRaises(IOSDeviceToolError):
            channel.status(cancellation=cancelled, deadline_monotonic=time.monotonic() + 5)
        self.assertEqual(len(double.calls), calls)

    def test_status_and_frame_identity_fail_closed(self):
        _owner, _runner, _session, startup, _identity, channel, double = self._running()
        channel.handshake(startup, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        double.bad_native_incarnation = True
        with self.assertRaises(IOSDeviceToolError):
            channel.status(cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 5)
        double.bad_native_incarnation = False
        double.frame["orientation"] = "landscape"
        with self.assertRaises(IOSDeviceToolError):
            channel.frame_after(0, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 5)

    def test_journal_capacity_blocks_new_effect_before_post(self):
        owner, _runner, _session, startup, identity, channel, double = self._running()
        channel.handshake(startup, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        channel.activate(startup, identity, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        self._confirm(owner, startup, "startup")
        control = channel._open_control()
        try:
            for name in ("op-seed-a", "op-seed-b"):
                os.mkdir(name, 0o700, dir_fd=control)
            os.fsync(control)
        finally:
            os.close(control)
        run_id = str(uuid.uuid4())
        payload = {"applicationId": self.launch.payload["applicationId"], "autoRunId": run_id}
        permit = self._dispatch(owner, command_payload("launch", payload), "command-capacity", 2)
        with patch.object(helper_module, "_MAX_JOURNAL_OPERATIONS", 4):
            with self.assertRaises(IOSDeviceToolError):
                channel.command("launch", payload, permit,
                                cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8,
                                expected_auto_run_id=run_id)
        self.assertFalse(any(path == "/command" for path, _ in double.calls))

    def _assert_cleanup_rejected(self, *, mode=None, wrong_authority=False, stale_authority=False):
        _owner, _runner, _session, cleanup, channel, double = self._cleanup_ready()
        if mode is not None:
            double.cleanup_evidence_mode = mode
        double.wrong_cleanup_authority = wrong_authority
        double.stale_cleanup_authority = stale_authority
        with self.assertRaises(IOSDeviceToolError):
            channel.shutdown(
                cleanup, cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 8)
        self.assertFalse(any(path == "/stop" for path, _ in double.calls))

    def test_cleanup_missing_observation_is_rejected(self):
        self._assert_cleanup_rejected(mode="missing")

    def test_cleanup_wrong_bundle_observation_is_rejected(self):
        self._assert_cleanup_rejected(mode="wrong_bundle")

    def test_cleanup_wrong_state_is_rejected(self):
        self._assert_cleanup_rejected(mode="wrong_state")

    def test_cleanup_wrong_observer_is_rejected(self):
        self._assert_cleanup_rejected(mode="wrong_observer")

    def test_cleanup_cross_operation_or_stale_grant_is_rejected(self):
        self._assert_cleanup_rejected(wrong_authority=True)

    def test_cleanup_stale_grant_is_rejected(self):
        self._assert_cleanup_rejected(stale_authority=True)

    def test_rejected_cleanup_ack_does_not_require_or_reconstruct_proof(self):
        _owner, _runner, _session, cleanup, channel, double = self._cleanup_ready()
        double.cleanup_ack_failure = True
        with self.assertRaises(IOSDeviceToolError):
            channel.shutdown(
                cleanup, cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 8)
        self.assertFalse(any(path == "/stop" for path, _ in double.calls))

    def test_cleanup_observation_is_cached_without_repeating_posts(self):
        _owner, _runner, _session, cleanup, channel, double = self._cleanup_ready()
        result = channel.shutdown(
            cleanup, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        self.assertTrue(result["target"]["terminationConfirmed"])
        posts = [path for path, _ in double.calls if path in {"/command", "/stop"}]
        duplicate = channel.shutdown(
            cleanup, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        self.assertEqual(duplicate, result)
        self.assertEqual([path for path, _ in double.calls if path in {"/command", "/stop"}], posts)
        with self.assertRaises(IOSDeviceToolError):
            channel.shutdown(
                replace(cleanup, operation_id="command-other"),
                cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)

    def test_ordinary_command_cannot_carry_cleanup_observation(self):
        owner, _runner, _session, startup, identity, channel, double = self._running()
        channel.handshake(startup, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        channel.activate(startup, identity, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        self._confirm(owner, startup, "startup")
        run_id = str(uuid.uuid4())
        payload = {"applicationId": self.launch.payload["applicationId"], "autoRunId": run_id}
        permit = self._dispatch(owner, command_payload("launch", payload), "command-evidence", 2)
        double.include_cleanup_evidence_for_all = True
        with self.assertRaises(IOSDeviceToolError):
            channel.command(
                "launch", payload, permit,
                cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8,
                expected_auto_run_id=run_id)
        self.assertEqual(len([path for path, _ in double.calls if path == "/command"]), 1)

    def test_stale_permit_is_rejected_without_command_post(self):
        owner, _runner, _session, startup, identity, channel, double = self._running()
        channel.handshake(startup, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        channel.activate(startup, identity, cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 8)
        self._confirm(owner, startup, "startup")
        new_run_id = str(uuid.uuid4())
        payload = {"applicationId": self.launch.payload["applicationId"],
                   "autoRunId": new_run_id}
        permit = self._dispatch(owner, command_payload("launch", payload), "command-stale", 2)
        stale = replace(permit, sequence=1)
        with self.assertRaises(IOSDeviceToolError):
            channel.command("launch", payload, stale, cancellation=threading.Event(),
                            deadline_monotonic=time.monotonic() + 8,
                            expected_auto_run_id=new_run_id)
        self.assertFalse(any(path == "/command" for path, _ in double.calls))

    def _running_egress(self):
        # egress 정책이 묶인 정의로 스토어를 교체한다 — prepare가
        # runtimeIdentity.egressPolicyDigest와 브리지 env를 주입하게 된다.
        from reproof.ios_mobile_operation import IOSMobileOperationStore
        selected = replace(self.case.operations.definition,
                           egress_policy_digest="e" * 64)
        self.case.operations = IOSMobileOperationStore(
            self.case.g.c.runs, selected, self.case.root / "egress-operations")
        self.addCleanup(self.case.operations.close)
        return self._running()

    def test_egress_bound_session_carries_counter_evidence(self):
        owner, _runner, _session, startup, identity, channel, double = \
            self._running_egress()
        channel.handshake(startup, cancellation=threading.Event(),
                          deadline_monotonic=time.monotonic() + 8)
        activated = channel.activate(
            startup, identity, cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 8)
        evidence = activated["networkEvidence"]
        self.assertEqual(evidence["schema"], "ios-network-counters")
        self.assertEqual(set(evidence["interfaces"]), {"en0", "pdp_ip0"})
        self._confirm(owner, startup, "startup")
        cleanup = self._dispatch(
            owner, command_payload("cleanup", {}), "command-cleanup", 2)
        shutdown = channel.shutdown(
            cleanup, cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 8)
        end = shutdown["networkEvidence"]
        self.assertEqual(end["schema"], "ios-network-counters")
        self.assertEqual(set(end["interfaces"]), {"en0", "pdp_ip0"})
        # 두 번째 shutdown은 저널된 durable 결과를 재생한다.
        duplicate = channel.shutdown(
            cleanup, cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 8)
        self.assertEqual(duplicate["networkEvidence"], end)

    def test_egress_bound_activate_requires_counter_evidence(self):
        _owner, _runner, _session, startup, identity, channel, double = \
            self._running_egress()
        channel.handshake(startup, cancellation=threading.Event(),
                          deadline_monotonic=time.monotonic() + 8)
        double.network_evidence_mode = "missing"
        with self.assertRaises(IOSDeviceToolError):
            channel.activate(startup, identity, cancellation=threading.Event(),
                             deadline_monotonic=time.monotonic() + 8)

    def test_egress_bound_malformed_counter_evidence_fails(self):
        _owner, _runner, _session, startup, identity, channel, double = \
            self._running_egress()
        channel.handshake(startup, cancellation=threading.Event(),
                          deadline_monotonic=time.monotonic() + 8)
        double.network_evidence_mode = "malformed"
        with self.assertRaises(IOSDeviceToolError):
            channel.activate(startup, identity, cancellation=threading.Event(),
                             deadline_monotonic=time.monotonic() + 8)

    def test_egress_bound_cleanup_requires_counter_evidence(self):
        _owner, _runner, _session, cleanup, channel, double = \
            self._cleanup_ready_egress()
        double.network_evidence_mode = "missing"
        with self.assertRaises(IOSDeviceToolError):
            channel.shutdown(cleanup, cancellation=threading.Event(),
                             deadline_monotonic=time.monotonic() + 8)

    def _cleanup_ready_egress(self):
        owner, runner, session, startup, identity, channel, double = \
            self._running_egress()
        channel.handshake(startup, cancellation=threading.Event(),
                          deadline_monotonic=time.monotonic() + 8)
        channel.activate(startup, identity, cancellation=threading.Event(),
                         deadline_monotonic=time.monotonic() + 8)
        self._confirm(owner, startup, "startup")
        cleanup = self._dispatch(
            owner, command_payload("cleanup", {}), "command-cleanup-proof", 2)
        return owner, runner, session, cleanup, channel, double


if __name__ == "__main__":
    unittest.main()
