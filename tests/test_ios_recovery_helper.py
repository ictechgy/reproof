"""Prior XCTest-helper retirement under the live iOS recovery execution."""
from dataclasses import replace
import json
import plistlib
import threading
import time
import unittest
from unittest.mock import patch

from reproof.ios_device_tools import PinnedDeviceCtlClient
from reproof.ios_native_recovery import IOSNativeRecoveryError
from reproof.ios_recovery_helper import (
    recover_prior_helpers, require_ios_recovery_helper_retirement,
)
from reproof.ios_recovery_helper import _helper_executables
from tests.ios_service_support import SanitationHTTPDouble
from tests import test_ios_recovery_execution as recovery_fixture
from reproof.ios_xctest_template import _target_location


class _RetirementDouble(SanitationHTTPDouble):
    def __init__(self, *args, cancel_after_retire=None, bad_provider=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancel_after_retire = cancel_after_retire
        self.bad_provider = bad_provider

    def _status(self):
        value = super()._status()
        _, target = _target_location(plistlib.loads(self.launch._configuration))
        environment = target["EnvironmentVariables"]
        value.update(
            helperIncarnation=environment["REPRO_LIVE_HELPER_INCARNATION"],
            hostIncarnation=environment["REPRO_LIVE_HOST_INCARNATION"],
            providerIncarnation=environment["REPRO_LIVE_PROVIDER_INCARNATION"],
        )
        if self.bad_provider:
            value["providerIncarnation"] = "ios-stale-provider"
        value.update(retirementVersion=1, authoritySequence=0)
        return value

    def call(self, path, body=None, timeout=5, binary=False):
        if path == "/retire":
            self.calls.append((path, body))
            if self.cancel_after_retire is not None:
                self.cancel_after_retire.set()
            return {"accepted": True}
        return super().call(path, body=body, timeout=timeout, binary=binary)


class IOSRecoveryHelperTests(unittest.TestCase):
    def setUp(self):
        self.base = recovery_fixture.IOSRecoveryExecutionTests(methodName="runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        for name in ("fixture", "old_authority", "device", "authority", "grant", "operations"):
            setattr(self, name, getattr(self.base, name))

    def _recovery(self):
        return self.base._recovery()

    def _seed_saved_launch(self):
        from reproof.ios_recovery_execution import IOSRecoveryExecution
        with self._recovery() as recovery:
            execution = IOSRecoveryExecution(recovery, self.fixture.config)
            with execution:
                original_status = SanitationHTTPDouble._status
                def recovery_status(double):
                    value = original_status(double)
                    value["helperIncarnation"] = double.launch._runner.native_owner.helper_incarnation
                    return value
                with patch.object(SanitationHTTPDouble, "_status", recovery_status):
                    execution.run_original_sanitation(
                        cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic() + 60)

    def _retirement_transport(self, *, process_rows, address="fd00::2", cancel_after_retire=None,
                              bad_provider=False):
        launches = self.fixture._launches
        doubles = []
        original_query = PinnedDeviceCtlClient.query
        calls = {"processes": 0}

        def query(client, kind, *, cancellation, deadline_monotonic):
            result = original_query(client, kind, cancellation=cancellation,
                                    deadline_monotonic=deadline_monotonic)
            if kind == "details":
                value = result.data
                value["connectionProperties"]["tunnelIPAddress"] = address
                return replace(result, _document=json.dumps(value, separators=(",", ":")))
            if kind == "processes":
                calls["processes"] += 1
                rows = process_rows(calls["processes"])
                return replace(result, _document=json.dumps({"runningProcesses": rows}, separators=(",", ":")))
            return result

        def tunnel(address_value, port, token):
            launch = next(item for item in launches if item._token == token)
            double = _RetirementDouble(
                address_value, port, token,
                stage_path=self.fixture.stage_path, mode_path=self.fixture.mode_path,
                cancel_event=None, cancel_after_retire=cancel_after_retire,
                bad_provider=bad_provider)
            double.launch = launch
            doubles.append(double)
            return double
        return query, tunnel, doubles

    def test_stale_saved_provider_identity_refuses_retirement(self):
        self._seed_saved_launch()
        names = _helper_executables(self.fixture.config)
        query, tunnel, doubles = self._retirement_transport(
            process_rows=lambda _count: [{"processIdentifier": 4321,
                                          "executable": "/usr/bin/" + name}
                                         for name in sorted(names)],
            bad_provider=True)
        with self._recovery() as recovery:
            from reproof.ios_recovery_execution import IOSRecoveryExecution
            with IOSRecoveryExecution(recovery, self.fixture.config):
                with patch.object(PinnedDeviceCtlClient, "query", query), \
                     patch("reproof.ios_recovery_helper.TunnelClient", side_effect=tunnel):
                    with self.assertRaises(IOSNativeRecoveryError):
                        recover_prior_helpers(
                            recovery, self.fixture.config,
                            cancellation=threading.Event(),
                            deadline_monotonic=time.monotonic() + 30)
        self.assertTrue(doubles)
        self.assertFalse(any(path == "/retire" for path, _ in doubles[0].calls))

    def test_dispatched_prior_helper_uses_current_tunnel_and_matching_retire_grant(self):
        self._seed_saved_launch()
        names = _helper_executables(self.fixture.config)
        def rows(count):
            return ([{"processIdentifier": 4321, "executable": "/usr/bin/" + name}
                     for name in sorted(names)] if count == 1 else [])
        query, tunnel, doubles = self._retirement_transport(process_rows=rows)
        cache = {}
        class Connection:
            def __init__(self, address, port, timeout):
                self.address, self.port, self.timeout = address, port, timeout
            def request(self, method, path, body=None, headers=None):
                token = headers["Authorization"].removeprefix("Bearer ")
                if token not in cache:
                    cache[token] = tunnel(self.address, self.port, token)
                self.payload = cache[token].call(path, None if body is None else json.loads(body), self.timeout)
                self.status = 202 if method == "POST" else 200
            def getresponse(self):
                return self
            def read(self, maximum):
                return json.dumps(self.payload).encode()[:maximum]
            def close(self):
                pass
        with self._recovery() as recovery:
            from reproof.ios_recovery_execution import IOSRecoveryExecution
            with IOSRecoveryExecution(recovery, self.fixture.config) as execution:
                with patch.object(PinnedDeviceCtlClient, "query", query), \
                     patch("http.client.HTTPConnection", Connection):
                    proof = recover_prior_helpers(
                        recovery, self.fixture.config,
                        cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic() + 30)
                self.assertTrue(proof.public()["priorHelperAbsent"])
                self.assertEqual(len(doubles), 1)
                self.assertEqual(doubles[0].address, "fd00::2")
                self.assertTrue(any(path == "/retire" for path, _ in doubles[0].calls))

    def test_helper_still_running_after_retire_cannot_publish_proof(self):
        self._seed_saved_launch()
        names = _helper_executables(self.fixture.config)
        cancellation = threading.Event()
        def rows(_count):
            return [{"processIdentifier": 4321, "executable": "/usr/bin/" + name}
                    for name in sorted(names)]
        query, tunnel, doubles = self._retirement_transport(
            process_rows=rows, cancel_after_retire=cancellation)
        with self._recovery() as recovery:
            from reproof.ios_recovery_execution import IOSRecoveryExecution
            with IOSRecoveryExecution(recovery, self.fixture.config):
                with patch.object(PinnedDeviceCtlClient, "query", query), \
                     patch("reproof.ios_recovery_helper.TunnelClient", side_effect=tunnel):
                    with self.assertRaises(IOSNativeRecoveryError):
                        recover_prior_helpers(
                            recovery, self.fixture.config,
                            cancellation=cancellation,
                            deadline_monotonic=time.monotonic() + 30)
        self.assertTrue(doubles and any(path == "/retire" for path, _ in doubles[0].calls))
        self.assertTrue(self.device.requires_reconciliation)
        self.assertGreater(self.fixture.runs.status(self.fixture.context.operation_id)["reservedBytes"], 0)

    def test_prior_helpers_absent_produces_live_retirement_proof(self):
        with self._recovery() as recovery:
            from reproof.ios_recovery_execution import IOSRecoveryExecution
            with IOSRecoveryExecution(recovery, self.fixture.config) as execution:
                proof = recover_prior_helpers(
                    recovery, self.fixture.config,
                    cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 20)
                self.assertTrue(proof.public()["priorHelperAbsent"])
                self.assertIs(require_ios_recovery_helper_retirement(proof, recovery), proof)

            with self.assertRaises(Exception):
                require_ios_recovery_helper_retirement(replace(proof), recovery)
            copied = replace(recovery)
            with self.assertRaises(Exception):
                require_ios_recovery_helper_retirement(proof, copied)

    def test_cancelled_helper_retirement_does_not_publish_proof(self):
        cancellation = threading.Event()
        cancellation.set()
        with self._recovery() as recovery:
            from reproof.ios_recovery_execution import IOSRecoveryExecution
            with IOSRecoveryExecution(recovery, self.fixture.config):
                with self.assertRaises(IOSNativeRecoveryError):
                    recover_prior_helpers(
                        recovery, self.fixture.config,
                        cancellation=cancellation,
                        deadline_monotonic=time.monotonic() + 20)

    def test_malformed_process_row_fails_closed_after_pinned_query(self):
        original = PinnedDeviceCtlClient.query

        def malformed(client, kind, *, cancellation, deadline_monotonic):
            result = original(client, kind, cancellation=cancellation,
                              deadline_monotonic=deadline_monotonic)
            if kind == "processes":
                return replace(result, _document=json.dumps({
                    "runningProcesses": [{"processIdentifier": "bad", "executable": 7}]
                }, separators=(",", ":")))
            return result

        with self._recovery() as recovery:
            from reproof.ios_recovery_execution import IOSRecoveryExecution
            with IOSRecoveryExecution(recovery, self.fixture.config):
                with patch.object(PinnedDeviceCtlClient, "query", malformed):
                    with self.assertRaises(IOSNativeRecoveryError):
                        recover_prior_helpers(
                            recovery, self.fixture.config,
                            cancellation=threading.Event(),
                            deadline_monotonic=time.monotonic() + 20)


if __name__ == "__main__":
    unittest.main()
