"""Compile/run the actual fixed native tools; deliberately do not boot a VM."""
import json
from pathlib import Path
import platform
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from reproloop.execution.native import NativeError, NativeVM
from reproloop.execution.resources import provision
from reproloop.execution.journal import RunStore
from tests.test_execution_resources import resource_inputs

ROOT = Path(__file__).resolve().parents[1]


class NativeControlTests(unittest.TestCase):
    def control(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.setblocking(False)
        owner = NativeVM.__new__(NativeVM)
        owner.process = SimpleNamespace(stdin=left, poll=lambda: None)
        owner.cancel = threading.Event()
        return owner, right

    def test_cancellation_after_writable_notification_prevents_control_write(self):
        owner, peer = self.control()
        original_select = select.select
        def cancelled(*args):
            result = original_select(*args)
            owner.cancel.set()
            return result
        with mock.patch("reproloop.execution.native.select.select", side_effect=cancelled):
            with self.assertRaises(NativeError):
                owner._write(b"configuration\n", time.monotonic() + 1)
        with self.assertRaises(BlockingIOError):
            peer.recv(64)

    def test_expired_control_is_not_written_after_a_delayed_notification(self):
        owner, peer = self.control()
        moment = [10.0]
        original_select = select.select
        def delayed(*args):
            result = original_select(*args)
            moment[0] = 12.0
            return result
        with mock.patch("reproloop.execution.native.time.monotonic", side_effect=lambda: moment[0]), \
                mock.patch("reproloop.execution.native.select.select", side_effect=delayed):
            with self.assertRaises(NativeError):
                owner._write(b"configuration\n", 11.0)
        with self.assertRaises(BlockingIOError):
            peer.recv(64)

    def test_stop_control_is_allowed_after_cancellation(self):
        owner, peer = self.control()
        owner.cancel.set()
        owner._write(b"stop\n", time.monotonic() + 1, allow_cancelled=True)
        self.assertEqual(peer.recv(64), b"stop\n")


@unittest.skipUnless(platform.system() == "Darwin", "Apple SDK native compile requires macOS")
class ExecutionNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.binary = cls.root / "native"
        result = subprocess.run([sys.executable, str(ROOT / "scripts/build-macos-execution.py"),
                                 "--output-new", str(cls.binary)], capture_output=True, timeout=120)
        if result.returncode != 0:
            cls.temporary.cleanup()
            raise RuntimeError("Current native bridge compile failed")
        cls.build_report = json.loads(result.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_current_swift_and_c_tools_compile_without_claiming_vm_acceptance(self):
        self.assertEqual(self.build_report, {"status": "compiled", "actualVM": False, "qualified": False})
        result = subprocess.run([str(self.binary / "vm-helper"), "--preflight"], capture_output=True, timeout=10)
        self.assertIn(json.loads(result.stdout), ({"event": "runtime-supported"}, {"event": "runtime-unsupported"}))
        self.assertIn(result.returncode, (0, 2))

    def test_guest_only_launchers_refuse_this_host(self):
        for name in ("guest-connect", "guest-run"):
            with self.subTest(tool=name):
                result = subprocess.run([str(self.binary / name), "--preflight"], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout.strip(), b"guest-scope-required")
        result = subprocess.run([sys.executable, "-I", str(ROOT / "guest/reproloop_agent/probe.py"),
                                 "containment"], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), b"guest-probe-rejected")

    def test_real_native_owner_rejects_invalid_resources_before_starting_vm(self):
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            root = Path(temporary)
            metadata, paths = resource_inputs(root)
            paths["helper"] = self.binary / "vm-helper"
            bundle = provision(root / "bundle", metadata=metadata, resources=paths)
            store = RunStore(root / "state", environment_digest=bundle.environment_digest, disk_limit=1024)
            with store.machine_lease(bundle.machine_digest), store.admit(
                    "native-denial", "a" * 64, disk_bytes=bundle.overlay_bytes) as run:
                bundle.create_overlays(run.directory)
                vm = NativeVM(bundle, run, deadline=time.monotonic() + 5, cancel=run)
                try:
                    with self.assertRaises(NativeError):
                        vm.wait_ready()
                finally:
                    self.assertTrue(vm.stop(timeout=3))
                # Simulate loss of the parent acknowledgement. Only the actual
                # native helper's fsynced, run-bound record permits recovery.
                run.finish("failed", stopped=False)
            self.assertEqual(vm.evidence, {"configured": False, "started": False,
                                           "connected": False, "stopped": False})
            self.assertEqual(vm.process.returncode, 2)
            self.assertEqual(store.status("native-denial")["state"], "quarantined")
            with store.machine_lease(bundle.machine_digest):
                self.assertEqual(store.reconcile("native-denial", "a" * 64)["state"], "failed")
            self.assertFalse(run.directory.exists())
