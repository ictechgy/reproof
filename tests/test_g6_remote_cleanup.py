"""Real worker HTTP cleanup after origin expiry or enrolled-host revocation."""
from __future__ import annotations
from pathlib import Path
import tempfile
import threading
import time
import unittest

from reproloop.live.authority import HostAuthority, issue_local_parent_grant
from reproloop.live.model import Lab, LiveError
from reproloop.live.worker import WorkerClient, WorkerServer, remote_devices
from tests.test_g1b_remote_authority import Clock, FencedProvider


def probe(mode):
    with tempfile.TemporaryDirectory(prefix='g6-parent-cleanup-') as directory:
        root = Path(directory)
        worker_clock = Clock('parent-probe-worker-clock')
        origin_clock = Clock('parent-probe-origin-clock')
        worker_authority = HostAuthority(root / 'worker.sqlite3', clock=worker_clock,
                                         lease_directory=root / 'worker-leases')
        origin_authority = HostAuthority(root / 'origin.sqlite3', clock=origin_clock,
                                         lease_directory=root / 'origin-leases')
        provider = FencedProvider()
        allowed = [True]

        def authorize(_project_id=None):
            if not allowed[0]:
                raise LiveError('unauthorized', 'Owned host scope was revoked', 401)
            return True

        worker = Lab([{
            'id': 'parent-device', 'name': 'Owned synthetic fenced provider',
            'platform': 'android', 'kind': 'android-live',
            '_authority': {'deviceKind': 'android', 'physicalId': 'parent-synthetic-device'},
            'capabilities': {'actions': ['tap'], 'inputMode': 'gesture-batch'},
            'factory': lambda: provider,
        }], root / 'worker-lab', authority=worker_authority, parent_grant=None,
            delegated_authority_only=True)
        server = WorkerServer(worker, 'r' * 40, host_authorizer=authorize)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        parent = None
        try:
            client = WorkerClient(server.origin, 'r' * 40)
            grant = issue_local_parent_grant(origin_authority, lifetime_ns=60_000_000_000)
            parent = Lab(remote_devices(client, 'worker'), root / 'parent-lab',
                         authority=origin_authority, parent_grant=grant)
            session = parent.create_session('worker--parent-device', 'owner', 'controller')
            end = time.monotonic() + 5
            while session['state'] == 'connecting' and time.monotonic() < end:
                time.sleep(.02)
                session = parent.get_session(session['id'], 'owner')
            if session['state'] != 'active':
                raise RuntimeError('Owned remote provider did not become active')
            remote = parent.sessions[session['id']]['provider']
            remote_id = remote.remote_session_id
            remote_controller = remote.remote_controller
            remote_epoch = remote.remote_epoch
            if mode == 'origin-expired':
                origin_clock.now = grant.local_deadline_ns + 1
            else:
                allowed[0] = False
            result = {'mode': mode}
            try:
                closed = parent.close_session(session['id'], 'owner',
                                              session['controllerId'], session['epoch'])
                result['parentCloseState'] = closed['state']
            except LiveError as exc:
                result['parentCloseError'] = exc.code
            result['workerStateAfterParentClose'] = worker.peek_session(remote_id)['state']
            # Prove that the existing restricted cleanup endpoint itself can
            # finish the original controller's close using cached identifiers.
            direct = client.call(f'/v1/sessions/{remote_id}/close',
                                 {'controllerId': remote_controller, 'epoch': remote_epoch})
            result['directCleanupState'] = direct['session']['state']
            result['workerDeviceStateAfterDirectCleanup'] = worker.list_devices()[0]['state']
            result['providerInputEffects'] = len(provider.effects)
            result['passed'] = (result.get('parentCloseState') == 'closed'
                                and result['workerStateAfterParentClose'] == 'closed'
                                and result['directCleanupState'] == 'closed'
                                and result['providerInputEffects'] == 0)
            return result
        finally:
            provider.release.set()
            if parent is not None:
                parent.close_all()
            server.close_operations()
            server.shutdown()
            server.server_close()
            thread.join(5)
            worker_authority.close()
            origin_authority.close()



class RemoteCleanupTests(unittest.TestCase):
    def test_cleanup_survives_origin_grant_expiry(self):
        result = probe('origin-expired')
        self.assertTrue(result['passed'], result)

    def test_cleanup_survives_enrolled_host_revocation(self):
        result = probe('host-revoked')
        self.assertTrue(result['passed'], result)
