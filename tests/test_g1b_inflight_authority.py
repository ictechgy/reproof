"""Probe revocation between admission and the next device effect."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from reproloop.core import ContractError
from reproloop.live.authority import HostAuthority, ProviderResult, issue_local_parent_grant
from reproloop.live.clock_sync import ClockReading
from reproloop.live.model import Lab, LiveError
from reproloop.storage import Lease


class Clock:
    def read(self):
        return ClockReading('parent-probe-clock', 'd' * 64, 1_000_000_000, 0)


class EffectBoundaryProvider:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.effects = []

    def bind_authority(self, device_authority, provider_incarnation):
        self.authority = device_authority

    def start_authorized(self, session, lab, permit):
        self.authority.check_dispatch_permit(permit)
        lab.publish_frame(session['id'], b'<svg/>', 'image/svg+xml', 400, 800)
        return {'ok': True}

    def execute_authorized(self, action, payload, permit, frame=None):
        self.authority.check_dispatch_permit(permit)
        self.last_permit = permit
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError('Synthetic effect boundary deadline')
        self.authority.check_dispatch_permit(permit)
        self.effects.append(permit.operation_id)
        return {'ok': True, 'timing': 'best-effort'}

    def close_authorized(self, permit):
        self.authority.check_dispatch_permit(permit)
        return {'ok': True}

    def close(self):
        self.release.set()


class InflightAuthorityBoundaries(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='g1b-inflight-probe-')
        root = Path(self.temporary.name)
        self.authority = HostAuthority(
            root / 'authority.sqlite3', clock=Clock(), lease_directory=root / 'leases')
        self.provider = EffectBoundaryProvider()
        descriptor = {
            'id': 'device-alias', 'name': 'Synthetic effect boundary',
            'platform': 'android', 'kind': 'android-live',
            '_authority': {'deviceKind': 'android', 'physicalId': 'parent-effect-probe'},
            'capabilities': {'actions': ['tap'], 'inputMode': 'gesture-batch'},
            'factory': lambda: self.provider,
        }
        self.lab = Lab(
            [descriptor], root / 'live', authority=self.authority,
            parent_grant=issue_local_parent_grant(self.authority, lifetime_ns=60_000_000_000))
        self.session = self.lab.create_session('device-alias', 'owner', 'old-controller')
        self.assertEqual(self.session['state'], 'active')
        self.thread = None
        self.errors = []

    def tearDown(self):
        self.provider.release.set()
        if self.thread is not None:
            self.thread.join(3)
        try:
            self.lab.close_all()
        finally:
            self.authority.close()
            self.temporary.cleanup()

    def _start_input(self):
        frame = self.lab.frame(self.session['id'])
        command = {
            'controllerId': self.session['controllerId'], 'epoch': self.session['epoch'],
            'sequence': 1, 'commandId': 'operation-before-handoff',
            'frameId': frame['id'], 'geometryVersion': frame['geometryVersion'],
            'action': 'tap', 'payload': {'x': .5, 'y': .5},
        }

        def invoke():
            try:
                self.lab.input(self.session['id'], 'owner', command)
            except Exception as error:
                self.errors.append(type(error).__name__)

        self.thread = threading.Thread(target=invoke, daemon=True)
        self.thread.start()
        self.assertTrue(self.provider.entered.wait(2), f'Effect boundary not reached: {self.errors}')

    def _finish_input(self):
        self.provider.release.set()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive(), 'Synthetic callback leaked')

    def test_live_permit_reaches_the_effect(self):
        self._start_input()
        self._finish_input()
        self.assertEqual(len(self.provider.effects), 1)
        self.assertEqual(self.errors, [])

    def test_failure_revokes_the_already_issued_permit_before_next_effect(self):
        self._start_input()
        finished = threading.Event()

        def revoke():
            try:
                self.lab.fail(self.session['id'], 'Synthetic transport failure')
            finally:
                finished.set()

        thread = threading.Thread(target=revoke, daemon=True)
        thread.start()
        returned = finished.wait(.6)
        self._finish_input()
        thread.join(3)
        self.assertTrue(returned, 'Host revocation waited for the provider')
        self.assertEqual(self.provider.effects, [], 'Revoked permit allowed a later effect')
        self.assertEqual(self.lab.list_devices()[0]['state'], 'quarantined')

    def test_handoff_cannot_activate_another_controller_during_uncertain_input(self):
        self._start_input()
        result = None
        try:
            result = self.lab.claim(
                self.session['id'], 'owner', 'new-controller', self.session['epoch'])
        except LiveError:
            pass
        finally:
            self._finish_input()
        if result is not None:
            self.assertNotEqual(
                result['state'], 'active',
                'A new controller became active while the old effect was still unresolved')

    def test_late_success_cannot_clear_revocation_or_enable_legacy_control(self):
        self._start_input()
        permit = self.provider.last_permit
        self.lab.fail(self.session['id'], 'Synthetic transport failure')
        self._finish_input()
        receipt = self.authority.record_provider_result(
            operation_id=permit.operation_id,
            generation=permit.ownership_generation,
            host_incarnation=permit.host_incarnation,
            provider_incarnation=permit.provider_incarnation,
            result=ProviderResult('late-probe-receipt', 'succeeded', 'e' * 64),
        )
        self.assertEqual(receipt['binding'], 'late')
        self.assertTrue(self.provider.authority.requires_reconciliation,
                        'Late success cleared a revoked device owner')
        self.provider.authority.close()
        with self.assertRaises(ContractError):
            with Lease('parent-effect-probe', Path(self.temporary.name) / 'leases'):
                self.fail('Legacy control bypassed an unresolved revoked owner')

    def test_handoff_rechecks_input_admitted_during_pointer_cleanup(self):
        cleanup_entered = threading.Event()
        cleanup_release = threading.Event()
        results = []
        errors = []

        def cleanup(_session):
            cleanup_entered.set()
            if not cleanup_release.wait(3):
                raise TimeoutError('Synthetic handoff cleanup deadline')

        def handoff():
            try:
                results.append(self.lab.claim(
                    self.session['id'], 'owner', 'new-controller', self.session['epoch']))
            except LiveError as error:
                errors.append(error.code)

        with patch.object(self.lab, '_cancel_active_pointers', side_effect=cleanup):
            thread = threading.Thread(target=handoff, daemon=True)
            thread.start()
            try:
                self.assertTrue(cleanup_entered.wait(1))
                self._start_input()
                cleanup_release.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertFalse(any(result['state'] == 'active' for result in results),
                                 'Handoff activated while a newly admitted effect was unresolved')
                self.assertTrue(errors)
            finally:
                cleanup_release.set()
                self.provider.release.set()
                thread.join(3)

    def test_clock_failure_does_not_prevent_durable_revocation(self):
        self._start_input()
        permit = self.provider.last_permit
        with patch.object(self.authority.clock_sync._clock, 'read',
                          side_effect=OSError('Synthetic clock read failure')):
            self.lab.fail(self.session['id'], 'Synthetic transport failure')
        receipt = self.authority.record_provider_result(
            operation_id=permit.operation_id,
            generation=permit.ownership_generation,
            host_incarnation=permit.host_incarnation,
            provider_incarnation=permit.provider_incarnation,
            result=ProviderResult('late-after-clock-failure', 'succeeded', 'f' * 64),
        )
        self.assertEqual(receipt['binding'], 'late',
                         'Clock read failure let a revoked operation become current again')
        self.assertTrue(self.provider.authority.requires_reconciliation)


if __name__ == '__main__':
    unittest.main()
