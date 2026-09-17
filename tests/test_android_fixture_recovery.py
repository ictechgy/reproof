"""Device recovery and the original remote fixture allocation are recovered together."""
import json
import threading
import time
import unittest
from contextlib import contextmanager

from tests import test_android_recovery as support


class AndroidFixtureRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AndroidDeviceRecoveryTests.setUpClass()
        cls.addClassCleanup(support.AndroidDeviceRecoveryTests.doClassCleanups)

    def setUp(self):
        self.f=support.AndroidDeviceRecoveryTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups)
        self.f.seed_fixture=True;self.f.setUp()

    def recover(self):
        from reproloop.android_recovery import recover_android_resources
        f=self.f
        with f.operations.native_recovery(f.operation.operation_id,f.operation.request_digest,
            device=f.device,snapshot=f.snapshot,parent_grant=f.grant) as recovery:
            return recover_android_resources(f.operations,recovery,cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+10)

    def test_original_fixture_is_reconciled_and_cleaned_without_releasing_device(self):
        result=self.recover()
        self.assertTrue(result.device_recovered and result.fixtures_clean)
        self.assertFalse(result.ownership_released)
        self.assertEqual(self.f.config.service.fixtures.status(self.f.allocation)['state'],'available')
        self.assertTrue(self.f.device.requires_reconciliation)
        self.assertGreater(self.f.f.fixture.runs.status(self.f.operation.operation_id)['reservedBytes'],0)

    def test_failed_remote_cleanup_preserves_fixture_and_device_quarantine(self):
        self.f.f.f.remote.control(fail_cleanup=True)
        result=self.recover()
        self.assertTrue(result.device_recovered)
        self.assertFalse(result.fixtures_clean or result.ownership_released)
        self.assertEqual(self.f.config.service.fixtures.status(self.f.allocation)['state'],'quarantined')
        self.assertTrue(self.f.device.requires_reconciliation)

    def test_changed_issue_device_binding_does_not_clean_the_fixture(self):
        service=self.f.config.service;record=service.get(self.f.issue_id)
        record['deviceId']='unrelated_device';service._persist(record)
        result=self.recover()
        self.assertFalse(result.fixtures_clean)
        self.assertEqual(service.fixtures.status(self.f.allocation)['state'],'quarantined')

    def test_retry_recognizes_prior_fixture_cleanup_without_touching_new_generation(self):
        self.assertTrue(self.recover().fixtures_clean)
        coordinator=self.f.config.service.fixtures;plan=self.f.config.preparations[0].plan
        current=coordinator.reserve(plan,owner='another-owner',device_id='another-device')
        coordinator.prepare(plan,current,payload={},operation_id='other_fixture_prepare')
        result=self.recover()
        self.assertTrue(result.fixtures_clean)
        self.assertEqual(coordinator.status(current)['state'],'ready')


class AndroidUnstartedFixtureRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AndroidDeviceRecoveryTests.setUpClass()
        cls.addClassCleanup(support.AndroidDeviceRecoveryTests.doClassCleanups)

    @contextmanager
    def fixture(self,mode):
        instance=support.AndroidDeviceRecoveryTests(methodName='runTest')
        instance.seed_fixture=True;instance.reservation_mode=mode
        try:
            instance.setUp();yield instance
        finally:instance.doCleanups()

    def recover(self,f):
        from reproloop.android_recovery import recover_android_resources
        started=time.monotonic()
        with f.operations.native_recovery(f.operation.operation_id,f.operation.request_digest,
            device=f.device,snapshot=f.snapshot,parent_grant=f.grant) as recovery:
            result=recover_android_resources(f.operations,recovery,cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+10)
        record_path=f.operation.staging_root.parent/'recovery.json'
        self.recovery_evidence={'elapsedSeconds':round(time.monotonic()-started,3),
            'deviceRecovered':result.device_recovered,
            'record':json.loads(record_path.read_bytes()) if record_path.exists() else None}
        return result

    def test_reserved_before_reference_write_is_recovered_without_remote_fixture_calls(self):
        with self.fixture('unstarted') as f:
            self.assertTrue(self.recover(f).fixtures_clean,self.recovery_evidence)
            self.assertEqual(f.config.service.fixtures.status(f.allocation)['state'],'available')
            from tests.test_fixture_allocations import _read_state
            self.assertEqual(_read_state(f.f.f.remote.state)['dispatches'],{})
            self.assertGreater(f.f.fixture.runs.status(f.operation.operation_id)['reservedBytes'],0)

    def test_intent_before_any_reservation_needs_no_remote_cleanup(self):
        with self.fixture('absent') as f:
            self.assertTrue(self.recover(f).fixtures_clean,self.recovery_evidence)
            from tests.test_fixture_allocations import _read_state
            self.assertEqual(_read_state(f.f.f.remote.state)['dispatches'],{})

    def test_missing_legacy_reference_cannot_be_treated_as_unstarted(self):
        with self.fixture('unstarted') as f:
            record=f.config.service.get(f.issue_id)
            record.pop('fixtureReservationVersion');record.pop('fixtureReservations')
            f.config.service._persist(record)
            self.assertFalse(self.recover(f).fixtures_clean)
            self.assertEqual(f.config.service.fixtures.status(f.allocation)['state'],'quarantined')

    def test_changed_planned_reservation_identity_is_rejected(self):
        with self.fixture('unstarted') as f:
            record=f.config.service.get(f.issue_id)
            record['fixtureReservations'][0]['allocationId']='allocation_unrelated'
            f.config.service._persist(record)
            self.assertFalse(self.recover(f).fixtures_clean)
            self.assertEqual(f.config.service.fixtures.status(f.allocation)['state'],'quarantined')
