"""Qualification control tests with an explicitly substituted VM boundary."""
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

from reproloop.execution.artifacts import BlobSet, send_blobs
from reproloop.execution.backend import ExecutionDenied, QualificationAuthority, REQUIRED_PROBES
from reproloop.execution.journal import RunStore
from reproloop.execution.native import NativeError
from reproloop.execution.qualification import qualify_backend
from reproloop.execution.resources import provision
from reproloop.execution.wire import ProtocolError, accept_bootstrap, canonical
from reproloop.contracts.versions import digest
from tests.test_execution_resources import resource_inputs


class ProbeVMDouble:
    instances = []
    stop_confirmed = True
    network_denied = True
    bounded_output_denied = True
    external_cancel = None
    cancel_after_first_stop = False

    def __init__(self, bundle, directory, *, deadline, cancel):
        self.cancel = cancel
        self.channel, self.peer = socket.socketpair()
        self.modes = []
        self.stop_called = False
        self.worker_joined = False
        type(self).instances.append(self)
        def serve():
            try:
                channel = accept_bootstrap(self.peer, deadline=deadline)
                channel.send("ready", {"agentDigest": bundle.metadata["agentDigest"],
                                       "catalogDigest": digest(bundle.metadata["catalog"])})
                kind, payload = channel.receive()
                if kind != "probe":
                    raise AssertionError("qualification admitted candidate data")
                mode = payload["mode"]
                self.modes.append(mode)
                channel.send("probe-started", {"mode": mode})
                if mode == "hold":
                    try:
                        channel.receive()
                    except RuntimeError:
                        pass
                    return
                if mode == "oversize" and self.bounded_output_denied:
                    channel.send("error", {"code": "output-rejected"})
                    return
                report = ({"verified": True, "passed": True} if mode == "forged-report" else
                          {"schemaVersion": 1, "mode": "containment", "networkDenied": self.network_denied,
                           "agentWriteDenied": True, "toolchainWriteDenied": True, "detachedChildStarted": True})
                send_blobs(channel, BlobSet((("probe.json", canonical(report)),)), prefix="artifact")
                channel.send("result", {"exitCode": 0, "outputTruncated": False, "logDigest": "a" * 64})
            except ProtocolError:
                # The host deliberately closes a rejected/oversized stream.
                pass
        self.thread = threading.Thread(target=serve)
        self.thread.start()

    def wait_ready(self):
        pass

    @property
    def evidence(self):
        return {"configured": True, "started": True, "connected": True, "stopped": self.stop_confirmed}

    def stop(self, **_):
        self.stop_called = True
        try:
            self.channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.channel.close()
        self.thread.join(3)
        self.worker_joined = not self.thread.is_alive()
        self.peer.close()
        if (type(self).cancel_after_first_stop and type(self).external_cancel is not None
                and len(type(self).instances) == 1):
            type(self).external_cancel.set()
        return self.stop_confirmed


class BlockingVMDouble:
    """Explicit lifecycle double that blocks at readiness or channel receive."""
    phase = "wait-ready"
    instances = []
    created = threading.Event()
    wait_ready_called = threading.Event()
    channel_ready = threading.Event()
    stop_confirmed = True

    def __init__(self, bundle, directory, *, deadline, cancel):
        self.cancel = cancel
        self.channel, self.peer = socket.socketpair()
        self.stop_event = threading.Event()
        self.stop_called = False
        self.worker_joined = False
        type(self).instances.append(self)
        type(self).created.set()
        self.thread = threading.Thread(target=self._serve, args=(bundle, deadline), daemon=True)
        self.thread.start()

    def _serve(self, bundle, deadline):
        if type(self).phase == "wait-ready":
            while not self.stop_event.wait(.01):
                pass
            return
        try:
            channel = accept_bootstrap(self.peer, deadline=deadline)
            type(self).channel_ready.set()
            while not self.stop_event.wait(.01):
                pass
            channel.failed = True
        except (ProtocolError, OSError):
            pass

    def wait_ready(self):
        type(self).wait_ready_called.set()
        if type(self).phase == "wait-ready":
            while not self.cancel.is_set():
                threading.Event().wait(.01)
            raise NativeError("qualification cancelled")

    @property
    def evidence(self):
        return {"configured": True, "started": True,
                "connected": type(self).phase == "channel",
                "stopped": self.stop_confirmed and self.worker_joined}

    def stop(self, **_):
        self.stop_called = True
        self.stop_event.set()
        for channel in (self.channel, self.peer):
            try:
                channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                channel.close()
            except OSError:
                pass
        self.thread.join(3)
        self.worker_joined = not self.thread.is_alive()
        return self.stop_confirmed and self.worker_joined


class ExecutionQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        metadata, paths = resource_inputs(self.root)
        self.bundle = provision(self.root / "bundle", metadata=metadata, resources=paths)
        self.store = RunStore(self.root / "state", environment_digest=self.bundle.environment_digest, disk_limit=1024)
        self.authority = QualificationAuthority()
        ProbeVMDouble.stop_confirmed = True
        ProbeVMDouble.network_denied = True
        ProbeVMDouble.bounded_output_denied = True
        ProbeVMDouble.instances = []
        ProbeVMDouble.external_cancel = None
        ProbeVMDouble.cancel_after_first_stop = False
        patcher = mock.patch("reproloop.execution.qualification.NativeVM", ProbeVMDouble)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_fixed_probe_modes_and_host_shutdown_are_required(self):
        outcome = qualify_backend("apple-vm", self.authority, self.bundle, self.store)
        self.assertIsNotNone(outcome.qualification)
        self.assertEqual([item["mode"] for item in outcome.report["probes"]],
                         ["containment", "hold", "oversize", "forged-report"])
        self.assertTrue(all(item["passed"] and item["cleanupConfirmed"] for item in outcome.report["probes"]))
        self.assertEqual(outcome.qualification.execution_class, "build-guest")
        self.assertEqual(list((self.root / "state/runs").iterdir()), [])

    def test_missing_termination_cannot_issue_any_qualification(self):
        ProbeVMDouble.stop_confirmed = False
        outcome = qualify_backend("apple-vm", self.authority, self.bundle, self.store)
        self.assertIsNone(outcome.qualification)
        self.assertEqual(outcome.report["status"], "blocked-unqualified")
        self.assertEqual(outcome.report["probes"][0]["state"], "quarantined")

    def test_network_escape_and_accepted_oversize_output_fail_qualification(self):
        ProbeVMDouble.network_denied = False
        outcome = qualify_backend("apple-vm", self.authority, self.bundle, self.store)
        self.assertIsNone(outcome.qualification)
        ProbeVMDouble.network_denied = True
        ProbeVMDouble.bounded_output_denied = False
        outcome = qualify_backend("apple-vm", self.authority, self.bundle, self.store)
        self.assertIsNone(outcome.qualification)
        self.assertEqual(outcome.report["probes"][-1]["mode"], "oversize")

    def test_wire_boolean_cannot_replace_the_trusted_composition_root(self):
        with self.assertRaises(RuntimeError):
            qualify_backend("apple-vm", {"qualified": True}, self.bundle, self.store)

    def test_invalid_cancellation_is_rejected_before_native_dispatch(self):
        with self.assertRaises(ExecutionDenied):
            qualify_backend("apple-vm", self.authority, self.bundle, self.store,
                            cancellation=object())
        self.assertEqual(ProbeVMDouble.instances, [])

    def test_cancellation_before_native_dispatch_runs_no_vm(self):
        cancellation = threading.Event(); cancellation.set()
        outcome = qualify_backend("apple-vm", self.authority, self.bundle, self.store,
                                  cancellation=cancellation)
        self.assertIsNone(outcome.qualification)
        self.assertEqual(outcome.report["probes"], [])
        self.assertEqual(ProbeVMDouble.instances, [])

    def test_cancellation_between_probe_modes_stops_before_next_vm(self):
        cancellation = threading.Event()
        ProbeVMDouble.external_cancel = cancellation
        ProbeVMDouble.cancel_after_first_stop = True
        outcome = qualify_backend("apple-vm", self.authority, self.bundle, self.store,
                                  cancellation=cancellation)
        self.assertIsNone(outcome.qualification)
        self.assertEqual(ProbeVMDouble.instances[0].modes, ["containment"])
        self.assertEqual(len(ProbeVMDouble.instances), 1)
        self.assertTrue(ProbeVMDouble.instances[0].stop_called)
        self.assertTrue(ProbeVMDouble.instances[0].worker_joined)

    def test_cancellation_while_waiting_for_vm_readiness_stops_and_joins(self):
        cancellation = threading.Event()
        BlockingVMDouble.phase = "wait-ready"
        BlockingVMDouble.instances = []
        BlockingVMDouble.created.clear(); BlockingVMDouble.wait_ready_called.clear()
        patcher = mock.patch("reproloop.execution.qualification.NativeVM", BlockingVMDouble)
        patcher.start(); self.addCleanup(patcher.stop)
        holder = []
        worker = threading.Thread(target=lambda: holder.append(
            qualify_backend("apple-vm", self.authority, self.bundle, self.store,
                            cancellation=cancellation)))
        worker.start()
        self.assertTrue(BlockingVMDouble.created.wait(1))
        self.assertTrue(BlockingVMDouble.wait_ready_called.wait(1))
        cancellation.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(holder), 1)
        self.assertIsNone(holder[0].qualification)
        self.assertTrue(BlockingVMDouble.instances[0].stop_called)
        self.assertTrue(BlockingVMDouble.instances[0].worker_joined)

    def test_cancellation_while_waiting_on_channel_stops_and_joins(self):
        cancellation = threading.Event()
        BlockingVMDouble.phase = "channel"
        BlockingVMDouble.instances = []
        BlockingVMDouble.created.clear(); BlockingVMDouble.wait_ready_called.clear()
        BlockingVMDouble.channel_ready.clear()
        patcher = mock.patch("reproloop.execution.qualification.NativeVM", BlockingVMDouble)
        patcher.start(); self.addCleanup(patcher.stop)
        holder = []
        worker = threading.Thread(target=lambda: holder.append(
            qualify_backend("apple-vm", self.authority, self.bundle, self.store,
                            cancellation=cancellation)))
        worker.start()
        self.assertTrue(BlockingVMDouble.created.wait(1))
        self.assertTrue(BlockingVMDouble.channel_ready.wait(1))
        cancellation.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(holder), 1)
        self.assertIsNone(holder[0].qualification)
        self.assertTrue(BlockingVMDouble.instances[0].stop_called)
        self.assertTrue(BlockingVMDouble.instances[0].worker_joined)

    def test_cancellation_after_final_probe_prevents_qualification_issuance(self):
        cancellation = threading.Event()
        original_record_probe = self.authority.record_probe
        calls = []

        def record_probe(**kwargs):
            calls.append(kwargs["probe_id"])
            result = original_record_probe(**kwargs)
            if len(calls) == len(REQUIRED_PROBES["build-guest"]):
                cancellation.set()
            return result

        with mock.patch.object(self.authority, "record_probe", side_effect=record_probe), \
             mock.patch.object(self.authority, "issue_backend_qualification",
                               wraps=self.authority.issue_backend_qualification) as issue:
            outcome = qualify_backend("apple-vm", self.authority, self.bundle, self.store,
                                      cancellation=cancellation)
        self.assertIsNone(outcome.qualification)
        issue.assert_not_called()
        self.assertEqual(len(calls), len(REQUIRED_PROBES["build-guest"]))
