"""iOS mobile-device qualification control tests with a substituted device subject."""
from contextlib import contextmanager
import socket
import threading
import time
import unittest

from reproloop.execution.backend import ExecutionDenied, QualificationAuthority
from reproloop.ios_device_qualification import (
    IOSDeviceQualificationError, PROBES, _probe_network_boundary, qualify_ios_device,
)

UDID = "00008150-000000000000001C"
TUNNEL = "fd71:14f:92fb::1"
POLICY = "ios-signing-policy"


class HelperDouble:
    """Scripted helper HTTP surface; records every request for assertions."""

    def __init__(self, subject, *, reject_unauthenticated=True, exit_code=0, leave_running=False,
                 retire_status=202, interfaces=None, accept_foreign_activation=False, port=1,
                 cancel_in_wait=False):
        self.subject = subject
        self.reject_unauthenticated = reject_unauthenticated
        self.exit_code = exit_code
        self.leave_running = leave_running
        self.retire_status = retire_status
        self.accept_foreign_activation = accept_foreign_activation
        self.address = TUNNEL
        self.port = port
        self.wired = True
        self.interfaces = [TUNNEL] if interfaces is None else interfaces
        self.cancel_in_wait = cancel_in_wait
        self.incarnations = {"host": "h-1", "helper": "x-1", "provider": "p-1"}
        self.configuration_digest = "c" * 64
        self.retired = False
        self.requests = []
        self.closed = 0

    def request(self, method, path, body=None, *, token=""):
        self.requests.append((method, path, token))
        if token is None or (token != "" and token != "issued"):
            return (401, {"error": "unauthorized"}) if self.reject_unauthenticated else (200, {"ready": False})
        if path == "/status":
            value = {"retirementVersion": 1, "ready": False, "hostIncarnation": "h-1", "helperIncarnation": "x-1",
                     "providerIncarnation": "p-1", "nativeIncarnation": "n-1", "nativeTimeMs": 1000}
            if isinstance(self.interfaces, list):
                value["networkInterfaces"] = self.interfaces
            return 200, value
        if path == "/activate":
            if body["authority"]["providerIncarnation"] != "p-1" and not self.accept_foreign_activation:
                return 409, {"error": "authority_rejected"}
            return 200, {"activated": True}
        if path == "/retire":
            self.retired = True
            return self.retire_status, {"accepted": True}
        if path == "/command":
            authority = body["authority"]
            if authority["providerIncarnation"] != "p-1" or self.retired:
                return 409, {"error": "authority_rejected"}
            return 202, {"accepted": True}
        raise AssertionError(path)

    def wait(self, *, deadline_monotonic, cancellation=None):
        if self.cancel_in_wait and cancellation is not None:
            cancellation.set()
        if cancellation is not None and cancellation.is_set():
            self.subject.helper_running = False
            raise IOSDeviceQualificationError("Helper XCTest was cancelled")
        self.subject.helper_running = self.leave_running
        summary = {"passedTests": 1, "failedTests": 0, "skippedTests": 0, "totalTestCount": 1} if self.exit_code == 0 else None
        return {"returncode": self.exit_code, "testSummary": summary}

    def close(self):
        self.closed += 1
        return True


class SubjectDouble:
    """A paired-iPhone subject with adjustable observations and no device access."""

    def __init__(self, **helper_options):
        self.helper_options = helper_options
        self.ready = True
        self.locked = False
        self.udid = UDID
        self.helper_running = False
        self.installed = ["io.reproloop.live.host", "io.reproloop.live.tests.xctrunner"]
        self.uninstalled = []
        self.sessions = []

    def device_status(self, *, cancellation=None, deadline_monotonic=None):
        return {"ready": self.ready, "paired": True, "developerMode": True, "wired": True, "tunnelConnected": True,
                "udid": self.udid, "tunnelAddress": TUNNEL}

    def lock_state(self, *, cancellation=None, deadline_monotonic=None):
        return {"passcodeRequired": self.locked}

    def installed_bundles(self, *, cancellation=None, deadline_monotonic=None):
        return list(self.installed)

    def process_executables(self, *, cancellation=None, deadline_monotonic=None):
        return ["file:///sbin/launchd"] + (["file:///private/var/containers/Bundle/Application/X/ReproLiveTests-Runner.app/ReproLiveTests-Runner"]
                                            if self.helper_running else [])

    def uninstall(self, bundle, *, cancellation=None, deadline_monotonic=None):
        self.installed.remove(bundle)
        self.uninstalled.append(bundle)

    @contextmanager
    def open_helper_session(self):
        helper = HelperDouble(self, **self.helper_options)
        helper.token = "issued"
        self.sessions.append(helper)
        try:
            yield helper
        finally:
            helper.close()


def _issued_request(helper):
    return [path for _, path, token in helper.requests if token == ""]


class IOSDeviceQualificationTests(unittest.TestCase):
    def _qualify(self, subject, authority=None, **options):
        authority = authority or QualificationAuthority()
        report, qualification = qualify_ios_device("ios-device-backend", authority, subject,
            environment_digest="e" * 64, signing_policy_id=POLICY, expected_udid=UDID, **options)
        return authority, report, qualification

    def test_full_measurement_issues_current_mobile_qualification(self):
        subject = SubjectDouble()
        authority, report, qualification = self._qualify(subject)
        self.assertTrue(report["qualified"])
        self.assertEqual([item["probeId"] for item in report["probes"]], list(PROBES))
        self.assertTrue(all(item["passed"] for item in report["probes"]))
        self.assertEqual(report["authority"], "process-local")
        self.assertIs(authority.require_qualification(qualification, backend_id="ios-device-backend",
            execution_class="mobile-device", environment_digest="e" * 64, signing_policy_id=POLICY,
            evaluated_at_ms=int(time.time() * 1000)), qualification)
        self.assertEqual(subject.uninstalled, ["io.reproloop.live.host", "io.reproloop.live.tests.xctrunner"])
        helper = subject.sessions[0]
        self.assertTrue(helper.retired)
        self.assertGreaterEqual(helper.closed, 1)
        network = report["probes"][1]["evidence"]
        self.assertEqual(network["nonTunnelInterfaceCount"], 0)
        self.assertEqual(network["nonTunnelReachableCount"], 0)
        self.assertTrue(report["probes"][2]["evidence"]["foreignActivationRejected"])
        self.assertNotIn(TUNNEL, str(report))
        self.assertNotIn(UDID, str(report))

    def test_reachable_non_tunnel_interface_fails_network_boundary(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            subject = SubjectDouble(interfaces=[TUNNEL, "127.0.0.1"], port=listener.getsockname()[1])
            _, report, qualification = self._qualify(subject)
        finally:
            listener.close()
        self.assertIsNone(qualification)
        evidence = report["probes"][-1]["evidence"]
        self.assertEqual(evidence["nonTunnelInterfaceCount"], 1)
        self.assertEqual(evidence["nonTunnelReachableCount"], 1)
        self.assertFalse(report["probes"][-1]["passed"])

    def test_missing_interface_report_fails_network_boundary(self):
        subject = SubjectDouble(interfaces="absent")
        _, report, qualification = self._qualify(subject)
        self.assertIsNone(qualification)
        self.assertFalse(report["probes"][-1]["evidence"]["interfacesReported"])

    def test_helper_accepting_foreign_activation_fails_backend_scope(self):
        subject = SubjectDouble(accept_foreign_activation=True)
        _, report, qualification = self._qualify(subject)
        self.assertIsNone(qualification)
        self.assertFalse(report["probes"][-1]["evidence"]["foreignActivationRejected"])

    def test_locked_device_blocks_before_any_helper_session(self):
        subject = SubjectDouble()
        subject.locked = True
        _, report, qualification = self._qualify(subject)
        self.assertIsNone(qualification)
        self.assertEqual(report["status"], "blocked-unqualified")
        self.assertEqual([item["probeId"] for item in report["probes"]], ["device-boundary"])
        self.assertFalse(report["probes"][0]["evidence"]["unlocked"])
        self.assertEqual(subject.sessions, [])

    def test_wrong_device_is_not_qualified(self):
        subject = SubjectDouble()
        subject.udid = "00008150-0000000000000FFF"
        _, report, qualification = self._qualify(subject)
        self.assertIsNone(qualification)
        self.assertFalse(report["probes"][0]["evidence"]["selectedDevice"])

    def test_helper_accepting_unauthenticated_status_fails_network_boundary(self):
        subject = SubjectDouble(reject_unauthenticated=False)
        _, report, qualification = self._qualify(subject)
        self.assertIsNone(qualification)
        self.assertEqual(report["probes"][-1]["probeId"], "network-boundary")
        self.assertFalse(report["probes"][-1]["evidence"]["missingTokenRejected"])
        self.assertNotIn("/activate", _issued_request(subject.sessions[0]))

    def test_helper_left_running_fails_process_termination(self):
        subject = SubjectDouble(leave_running=True)
        _, report, qualification = self._qualify(subject, timeout_seconds=30)
        self.assertIsNone(qualification)
        self.assertEqual(report["probes"][-1]["probeId"], "process-termination")
        self.assertFalse(report["probes"][-1]["evidence"]["helperProcessesAbsent"])
        self.assertEqual(subject.uninstalled, [])

    def test_failed_xctest_exit_is_not_qualified(self):
        subject = SubjectDouble(exit_code=65)
        _, report, qualification = self._qualify(subject)
        self.assertIsNone(qualification)
        self.assertFalse(report["probes"][-1]["evidence"]["xcodebuildExitZero"])

    def test_rejected_retirement_fails_backend_scope(self):
        subject = SubjectDouble(retire_status=409)
        _, report, qualification = self._qualify(subject)
        self.assertIsNone(qualification)
        self.assertEqual(report["probes"][-1]["probeId"], "backend-scope")
        self.assertFalse(report["probes"][-1]["evidence"]["retirementAccepted"])

    def test_cancellation_during_measurement_stops_at_probe_boundary(self):
        subject = SubjectDouble()
        cancellation = threading.Event()
        original = subject.device_status
        def cancelling(**kwargs):
            cancellation.set()
            return original(**kwargs)
        subject.device_status = cancelling
        _, report, qualification = self._qualify(subject, cancellation=cancellation)
        self.assertIsNone(qualification)
        self.assertEqual([item["probeId"] for item in report["probes"]], ["device-boundary"])
        self.assertFalse(report["probes"][0]["passed"])

    def test_cancellation_during_helper_wait_fails_termination(self):
        subject = SubjectDouble(cancel_in_wait=True)
        cancellation = threading.Event()
        _, report, qualification = self._qualify(subject, cancellation=cancellation)
        self.assertIsNone(qualification)
        self.assertEqual([item["probeId"] for item in report["probes"]],
                         ["device-boundary", "network-boundary", "backend-scope", "process-termination"])
        self.assertFalse(report["probes"][-1]["passed"])
        self.assertEqual(subject.sessions[-1].closed, 1)

    def test_truncated_interface_scan_is_not_evidence_of_closure(self):
        subject = SubjectDouble()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            helper = HelperDouble(subject, interfaces=[TUNNEL, "127.0.0.1"], port=listener.getsockname()[1])
            status = {"ready": False, "networkInterfaces": [TUNNEL, "127.0.0.1"]}
            passed, evidence = _probe_network_boundary(helper, status, None, time.monotonic() - 1)
            self.assertFalse(passed)
            self.assertFalse(evidence["nonTunnelScanComplete"])
            self.assertEqual(evidence["nonTunnelReachableCount"], 0)
            passed, evidence = _probe_network_boundary(helper, status, None, time.monotonic() + 30)
            self.assertFalse(passed)
            self.assertTrue(evidence["nonTunnelScanComplete"])
            self.assertEqual(evidence["nonTunnelReachableCount"], 1)
        finally:
            listener.close()

    def test_subject_calls_receive_measurement_deadline(self):
        subject = SubjectDouble()
        observed = []
        original = subject.lock_state
        def recording(**kwargs):
            observed.append(kwargs)
            return original(**kwargs)
        subject.lock_state = recording
        self._qualify(subject)
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0]["deadline_monotonic"], time.monotonic())
        self.assertIn("cancellation", observed[0])

    def test_cancellation_leaves_backend_unqualified(self):
        subject = SubjectDouble()
        cancellation = threading.Event()
        cancellation.set()
        _, report, qualification = self._qualify(subject, cancellation=cancellation)
        self.assertIsNone(qualification)
        self.assertFalse(report["qualified"])

    def test_new_measurement_revokes_previous_qualification(self):
        authority = QualificationAuthority()
        _, _, first = self._qualify(SubjectDouble(), authority)
        subject = SubjectDouble()
        subject.locked = True
        _, _, second = self._qualify(subject, authority)
        self.assertIsNone(second)
        with self.assertRaises(ExecutionDenied):
            authority.require_qualification(first, backend_id="ios-device-backend", execution_class="mobile-device",
                environment_digest="e" * 64, signing_policy_id=POLICY, evaluated_at_ms=int(time.time() * 1000))

    def test_report_cannot_be_replayed_as_a_capability(self):
        authority, report, _ = self._qualify(SubjectDouble())
        with self.assertRaises(ExecutionDenied):
            authority.require_qualification(report, backend_id="ios-device-backend", execution_class="mobile-device",
                environment_digest="e" * 64, signing_policy_id=POLICY, evaluated_at_ms=int(time.time() * 1000))

    def test_foreign_authority_object_is_rejected(self):
        with self.assertRaises(ExecutionDenied):
            qualify_ios_device("ios-device-backend", object(), SubjectDouble(), environment_digest="e" * 64,
                               signing_policy_id=POLICY, expected_udid=UDID)


if __name__ == "__main__":
    unittest.main()
