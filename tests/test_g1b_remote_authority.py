"""Originating authority deadlines remain the worker/native deadline ceiling."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from reproloop.live.authority import HostAuthority, issue_local_parent_grant
from reproloop.live.clock_sync import ClockReading
from reproloop.live.model import Lab, LiveError
from reproloop.live.worker import WorkerClient, WorkerServer, remote_devices


class Clock:
    def __init__(self, name):
        self.name = name
        self.now = 1_000_000_000

    def read(self):
        return ClockReading(self.name, 'd' * 64, self.now, 0)


class FencedProvider:
    def __init__(self):
        self.effects = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def bind_authority(self, authority, provider_incarnation):
        self.authority = authority

    def start_authorized(self, session, lab, permit):
        self.authority.check_dispatch_permit(permit)
        self.session, self.lab = session, lab
        lab.publish_frame(session['id'], b'<svg/>', 'image/svg+xml', 400, 800)
        return {'ok': True}

    def execute_authorized(self, action, payload, permit, frame=None):
        self.authority.check_dispatch_permit(permit)
        self.entered.set()
        if self.block and not self.release.wait(5):
            raise TimeoutError('Synthetic worker effect did not receive its test release')
        self.authority.check_dispatch_permit(permit)
        self.effects.append(permit.operation_id)
        self.lab.publish_frame(self.session['id'], b'<svg/>', 'image/svg+xml', 400, 800)
        return {'ok': True, 'timing': 'best-effort'}

    def close_authorized(self, permit):
        self.authority.check_dispatch_permit(permit)
        return {'ok': True}


class G1bRemoteAuthorityTests(unittest.TestCase):
    def test_input_after_origin_deadline_is_rejected_before_worker_effect(self):
        self.exercise(inflight=False)

    def test_inflight_worker_cannot_fall_back_to_its_longer_local_grant(self):
        self.exercise(inflight=True)

    def exercise(self, *, inflight):
        with tempfile.TemporaryDirectory(prefix='g1b-remote-parent-') as directory:
            root = Path(directory)
            worker_clock = Clock('synthetic-worker-clock')
            parent_clock = Clock('synthetic-parent-clock')
            worker_authority = HostAuthority(
                root / 'worker.sqlite3', clock=worker_clock,
                lease_directory=root / 'worker-leases')
            parent_authority = HostAuthority(
                root / 'parent.sqlite3', clock=parent_clock,
                lease_directory=root / 'parent-leases')
            provider = FencedProvider()
            worker = Lab([{
                'id': 'worker-device', 'name': 'Synthetic remote executor',
                'platform': 'android', 'kind': 'android-live',
                '_authority': {'deviceKind': 'android', 'physicalId': 'remote-physical'},
                'capabilities': {'actions': ['tap'], 'inputMode': 'gesture-batch'},
                'factory': lambda: provider,
            }], root / 'worker-live', authority=worker_authority,
                parent_grant=issue_local_parent_grant(
                    worker_authority, lifetime_ns=60_000_000_000))
            server = WorkerServer(worker, 'remote-authority-test-token-123456789')
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            parent = None
            input_thread = None
            try:
                client = WorkerClient(server.origin, 'remote-authority-test-token-123456789')
                grant = issue_local_parent_grant(
                    parent_authority, lifetime_ns=5_000_000_000)
                parent = Lab(remote_devices(client, 'worker'), root / 'parent-live',
                             authority=parent_authority, parent_grant=grant)
                session = parent.create_session('worker--worker-device', 'owner', 'browser')
                deadline = time.monotonic() + 5
                while session['state'] == 'connecting' and time.monotonic() < deadline:
                    time.sleep(.03)
                    session = parent.get_session(session['id'], 'owner')
                self.assertEqual(session['state'], 'active')
                frame = parent.frame(session['id'])
                failures = []

                def invoke():
                    try:
                        parent.input(session['id'], 'owner', {
                            'controllerId': session['controllerId'], 'epoch': session['epoch'],
                            'sequence': 1, 'commandId': 'expired-parent-input',
                            'frameId': frame['id'], 'geometryVersion': frame['geometryVersion'],
                            'action': 'tap', 'payload': {'x': .5, 'y': .5},
                        })
                    except LiveError as error:
                        failures.append(error.code)

                if inflight:
                    provider.block = True
                    input_thread = threading.Thread(target=invoke, daemon=True)
                    input_thread.start()
                    self.assertTrue(provider.entered.wait(2))
                if inflight:
                    parent_clock.now = grant.local_deadline_ns + 1
                    worker_clock.now = parent_clock.now
                    provider.release.set()
                    input_thread.join(5)
                    self.assertFalse(input_thread.is_alive())
                else:
                    parent_clock.now = grant.local_deadline_ns + 1
                    invoke()
                self.assertEqual(provider.effects, [])
                self.assertTrue(failures)
            finally:
                provider.release.set()
                if input_thread is not None:
                    input_thread.join(5)
                if parent is not None:
                    parent.close_all()
                server.close_operations()
                server.shutdown()
                server.server_close()
                server_thread.join(5)
                worker_authority.close()
                parent_authority.close()


if __name__ == '__main__':
    unittest.main()
