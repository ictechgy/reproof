"""Termination evidence and service-owned shutdown at the native boundary."""
from contextlib import nullcontext
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from reproloop.device import DeviceError
from reproloop.execution.artifacts import BlobSet
from reproloop.live.android_live import AndroidLiveProvider
from reproloop.live.model import LiveError
from reproloop.repair_composition import ProtectedRepairComposition
from reproloop.repair_mobile import MobileFailureObservation
from tests import test_repair_android as support


class Lease:
    def __init__(self): self.released = False
    def __exit__(self, *_): self.released = True


class ProviderTerminationTests(unittest.TestCase):
    def provider(self):
        provider = AndroidLiveProvider.__new__(AndroidLiveProvider)
        provider._managed_device = False
        provider.stop = threading.Event()
        provider.process = provider.port = provider.transport = provider.thread = None
        provider.token = 'owned-test-token'
        provider.lease = Lease(); provider.lease_held = True
        provider.automatic_app_logs = False
        return provider

    def test_a_live_frame_reader_cannot_release_native_lease(self):
        provider = self.provider()
        released, entered = threading.Event(), threading.Event()
        def reader(): entered.set(); released.wait(5)
        thread = threading.Thread(target=reader)
        thread.start(); self.assertTrue(entered.wait(1))
        provider.thread = thread
        lease = provider.lease
        try:
            with patch.object(thread, 'join', return_value=None):
                with self.assertRaises(LiveError): provider.close()
            self.assertFalse(lease.released)
            self.assertTrue(provider.lease_held)
        finally:
            released.set(); thread.join(2)
        self.assertEqual(provider.close(), {'ok': True})
        self.assertTrue(lease.released)

    def test_reaped_unmanaged_leader_is_never_signalled_after_wait_race(self):
        provider = self.provider()
        process = subprocess.Popen(['/usr/bin/true'], start_new_session=True)
        process.wait(timeout=5)
        provider.process = process
        sent = []
        def signal_group(pid, sig):
            if sig: sent.append((pid, sig))
            else: raise ProcessLookupError
        with patch.object(process, 'wait', side_effect=[subprocess.TimeoutExpired('owned', 8), 0]), \
                patch('os.killpg', side_effect=signal_group):
            with self.assertRaises(LiveError): provider.close()
        self.assertEqual(sent, [])


class AdapterShutdownTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.AndroidAdapterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.adapter = self.fixture.adapter

    def test_composition_closes_installed_candidate_and_fences_reuse(self):
        self.fixture.install()
        composition = ProtectedRepairComposition()
        composition.adopt_android_mobile(self.adapter)
        composition.close(timeout_seconds=10)
        self.assertEqual(self.fixture.lab.list_devices()[0]['state'], 'available')
        self.assertIsNone(self.adapter._temporary)
        with self.assertRaises(DeviceError): self.fixture.install()

    def test_close_waits_for_callback_and_blocks_a_late_install_receipt(self):
        entered, release = threading.Event(), threading.Event()
        stage = self.adapter._stage
        results = []
        def held_stage(*args):
            entered.set(); release.wait(3); return stage(*args)
        with patch.object(self.adapter, '_stage', held_stage):
            worker = threading.Thread(target=lambda: results.append(self.adapter.install(
                self.fixture.context, BlobSet((('candidate.apk', self.fixture.candidate),)),
                **self.fixture.bounds())))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                before = time.monotonic()
                self.assertFalse(self.adapter.close(deadline_monotonic=before+.03))
                self.assertLess(time.monotonic()-before, .5)
            finally:
                release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(results[0], MobileFailureObservation)
        self.assertTrue(self.adapter.close(deadline_monotonic=time.monotonic()+10))
        self.assertEqual(self.fixture.lab.list_devices()[0]['state'], 'available')

    def test_uncertain_cleanup_still_closes_owned_local_tool_resources(self):
        self.fixture.install()
        owner = self.adapter._devices[0]
        owner._uncertain = True
        result = self.adapter.cleanup(self.fixture.context, **self.fixture.bounds())
        self.assertFalse(result.ownership_released)
        self.assertTrue(owner._closed)
        self.assertTrue(self.adapter._candidate_path.is_file())
        # No native effect is uncertain in this explicitly injected state.
        owner._uncertain = False


if __name__ == '__main__': unittest.main()
