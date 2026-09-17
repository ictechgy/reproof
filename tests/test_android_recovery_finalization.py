"""Measured Android recovery retains its locks through durable final cleanup."""
from dataclasses import replace
import fcntl
import json
import os
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from reproloop.core import ContractError
from reproloop.execution.journal import RunDenied
from reproloop.live.authority import HostAuthority, ProviderResult
from reproloop.repair_android_operation import AndroidOperationError
from tests import test_android_recovery_helper as support


class AndroidRecoveryFinalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AndroidRecoveryHelperTests.setUpClass()
        cls.addClassCleanup(support.AndroidRecoveryHelperTests.doClassCleanups)

    def setUp(self):
        self.helper = support.AndroidRecoveryHelperTests(methodName="runTest")
        self.addCleanup(self.helper.doCleanups)
        with patch.object(support.support.AndroidDeviceRecoveryTests, "seed_authority_operations", True, create=True):
            self.helper.setUp()
        self.f = self.helper.f
        self.operations = self.f.operations
        self.runs = self.operations.run_store
        self.device = self.f.device
        self.directory = self.f.operation.staging_root.parent
        self.authority = self.device._authority
        self.device_path = self.device._lease.directory / (self.device._lease.key + ".lock")

    def grant(self):
        received = self.authority.clock_sync.sample()
        sent = self.authority.clock_sync.sample()
        mapping = self.authority.clock_sync.record_exchange(
            coordinator_clock_id="recovery-test-clock", coordinator_send_ns=received.nanoseconds-10000,
            host_received=received, host_sent=sent, coordinator_receive_ns=sent.nanoseconds+10000,
            max_drift_ppm=100,
        )
        return self.authority.issue_parent_grant(
            mapping, grant_id="cleanup_"+uuid.uuid4().hex,
            project_id=self.f.config.registration.project["id"],
            controller_id=self.f.grant.controller_id, renewal_sequence=1,
            coordinator_deadline_ns=sent.nanoseconds+60_000_000_000,
        )

    def finalize(self):
        try:
            return self.operations.finalize_recovery(
                self.f.operation.operation_id, self.f.operation.request_digest,
                device=self.device, parent_grant=self.grant(), cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+15,
            )
        except AndroidOperationError as error:
            if error.code == "android_recovery_helper_unconfirmed" and not self.helper.wrong_binding:
                record = json.loads((self.directory/"recovery.json").read_bytes())
                self.fail({"code": error.code, "recovery": record})
            raise

    def assert_locked_and_quarantined(self):
        for path in (self.directory / "producer.lock", self.device_path):
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
        with self.assertRaises(ContractError):
            self.device.check_ownership()

    def test_observed_recovery_reconciles_discards_and_releases_under_original_locks(self):
        remove = self.operations._remove_staged
        checked = []
        def observed(directory, name):
            self.assert_locked_and_quarantined()
            self.assertGreater(self.runs.status(self.f.operation.operation_id)["reservedBytes"], 0)
            checked.append(name)
            remove(directory, name)
        with patch.object(self.operations, "_remove_staged", side_effect=observed):
            result = self.finalize()
        self.assertTrue(result.ownership_released and result.reservation_released)
        self.assertEqual(set(checked), {"candidate.apk", "original.apk", "helper.apk"})
        self.assertEqual(list(self.f.operation.staging_root.iterdir()), [])
        self.assertEqual(self.runs.status(self.f.operation.operation_id)["reservedBytes"], 0)
        self.assertEqual(self.runs.status(self.f.operation.operation_id)["state"], "failed")
        self.assertEqual(self.authority.store.device(self.f.config.scope_digest)["status"], "released")
        self.assertEqual(self.device.generation, 2)
        self.assertEqual(self.authority.store.operation("owned_recovery_uncertain")["status"], "recovered")
        self.assertIsNone(self.authority.store.operation("owned_recovery_uncertain")["result_digest"])
        self.assertEqual(self.authority.store.operation("owned_recovery_queued")["status"], "rejected")
        permit = self.f.original_permit
        receipt = self.authority.record_provider_result(operation_id=permit.operation_id,
            generation=permit.ownership_generation, host_incarnation=permit.host_incarnation,
            provider_incarnation=permit.provider_incarnation,
            result=ProviderResult("late_original_result", "succeeded", "d"*64))
        self.assertEqual(receipt["binding"], "late")
        self.assertEqual(self.authority.store.operation("owned_recovery_uncertain")["status"], "recovered")
        self.assertEqual(self.operations.status(self.f.operation.operation_id)["state"], "finalization-completed")
        descriptor = os.open(self.device_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

    def test_wrong_helper_observation_cannot_reconcile_or_discard(self):
        self.helper.wrong_binding = True
        with self.assertRaises(AndroidOperationError):
            self.finalize()
        self.assertEqual(self.device.generation, 1)
        self.assertTrue(self.device.requires_reconciliation)
        self.assertEqual(len(list(self.f.operation.staging_root.iterdir())), 3)
        self.assertGreater(self.runs.status(self.f.operation.operation_id)["reservedBytes"], 0)

    def test_interrupted_discard_resumes_without_repeating_device_effects(self):
        remove = self.operations._remove_staged
        def interrupted(directory, name):
            remove(directory, name)
            raise OSError("owned discard interruption")
        with patch.object(self.operations, "_remove_staged", side_effect=interrupted):
            with self.assertRaisesRegex(AndroidOperationError, "android_operation_recovery_unavailable"):
                self.finalize()
        self.assertEqual(self.device.generation, 2)
        self.assertTrue(self.device.requires_reconciliation)
        self.assertEqual(len(list(self.f.operation.staging_root.iterdir())), 2)
        requests = len(self.helper.server.requests)
        self.assertTrue(self.finalize().ownership_released)
        self.assertEqual(len(self.helper.server.requests), requests)

    def test_restart_after_reconciliation_keeps_original_generation_cleanup_bound(self):
        with patch.object(self.operations, "_remove_staged", side_effect=OSError("owned interruption")):
            with self.assertRaisesRegex(AndroidOperationError, "android_operation_recovery_unavailable"):
                self.finalize()
        requests = len(self.helper.server.requests)
        old = self.authority
        old.close()
        self.authority = HostAuthority(old.store.path, lease_directory=old.lease_directory)
        self.addCleanup(self.authority.close)
        self.device = self.authority.claim_device(
            device_kind="android", physical_id=self.f.config.serial,
            helper_incarnation="helper-restart", parent_grant=self.grant(),
        )
        self.assertTrue(self.device.requires_reconciliation)
        self.assertTrue(self.finalize().ownership_released)
        self.assertEqual(len(self.helper.server.requests), requests)

    def test_run_budget_write_interruption_resumes_after_committed_release(self):
        write = self.runs._write
        def interrupted(value):
            write(value)
            if value["runs"][self.f.operation.operation_id]["reservedBytes"] == 0:
                raise OSError("owned post-commit interruption")
        with patch.object(self.runs, "_write", side_effect=interrupted):
            with self.assertRaisesRegex(AndroidOperationError, "android_operation_recovery_unavailable"):
                self.finalize()
        self.assertEqual(self.runs.status(self.f.operation.operation_id)["reservedBytes"], 0)
        self.assertTrue(self.device.requires_reconciliation)
        requests = len(self.helper.server.requests)
        self.assertTrue(self.finalize().ownership_released)
        self.assertEqual(len(self.helper.server.requests), requests)

    def test_forged_cleanup_data_cannot_release_run_budget(self):
        with self.assertRaises(RunDenied):
            self.runs.finish_mobile_recovery({"state": "sanitized"}, authority=self.operations)
        self.assertGreater(self.runs.status(self.f.operation.operation_id)["reservedBytes"], 0)

    def test_cleanup_capability_is_live_single_use_and_cannot_be_copied(self):
        consume = self.runs.finish_mobile_recovery
        captured = []
        def checked(capability, *, authority):
            captured.append(capability)
            with self.assertRaises(RunDenied):
                consume(replace(capability), authority=authority)
            result = consume(capability, authority=authority)
            with self.assertRaises(RunDenied):
                consume(capability, authority=authority)
            return result
        with patch.object(self.runs, "finish_mobile_recovery", side_effect=checked):
            self.assertTrue(self.finalize().ownership_released)
        with self.assertRaises(RunDenied):
            consume(captured[0], authority=self.operations)

    def test_reconciliation_commit_survives_interrupted_private_record_update(self):
        from reproloop import android_recovery_finalization
        write = android_recovery_finalization._replace_at
        def interrupted(directory, name, value):
            if name == "finalization.json" and value["state"] == "reconciled":
                raise OSError("owned post-reconciliation interruption")
            return write(directory, name, value)
        with patch.object(android_recovery_finalization, "_replace_at", side_effect=interrupted):
            with self.assertRaisesRegex(AndroidOperationError, "android_operation_recovery_unavailable"):
                self.finalize()
        self.assertEqual(self.device.generation, 2)
        self.assertEqual(json.loads((self.directory/"finalization.json").read_bytes())["state"], "prepared")
        self.assertTrue(self.device.requires_reconciliation)
        requests = len(self.helper.server.requests)
        self.assertTrue(self.finalize().ownership_released)
        self.assertEqual(len(self.helper.server.requests), requests)

    def test_old_finalization_after_release_cannot_touch_a_new_owner(self):
        from reproloop import android_recovery_finalization
        write = android_recovery_finalization._replace_at
        def interrupted(directory, name, value):
            if name == "finalization.json" and value["state"] == "completed":
                raise OSError("owned post-device-release interruption")
            return write(directory, name, value)
        with patch.object(android_recovery_finalization, "_replace_at", side_effect=interrupted):
            with self.assertRaisesRegex(AndroidOperationError, "android_operation_recovery_unavailable"):
                self.finalize()
        self.assertEqual(self.runs.status(self.f.operation.operation_id)["reservedBytes"], 0)
        self.assertTrue(self.device.close())
        self.device = self.authority.claim_device(device_kind="android", physical_id=self.f.config.serial,
            helper_incarnation="helper-next-owner", parent_grant=self.grant())
        self.addCleanup(self.device.close)
        self.assertEqual(self.device.generation, 3)
        before = self.authority.store.device(self.f.config.scope_digest)
        requests = len(self.helper.server.requests)
        self.assertTrue(self.finalize().ownership_released)
        self.assertEqual(self.authority.store.device(self.f.config.scope_digest), before)
        self.assertEqual(self.device.status, "owned")
        self.assertEqual(len(self.helper.server.requests), requests)

    def test_changed_saved_reconciliation_cannot_discard_or_release(self):
        with patch.object(self.operations, "_remove_staged", side_effect=OSError("owned interruption")):
            with self.assertRaisesRegex(AndroidOperationError, "android_operation_recovery_unavailable"):
                self.finalize()
        path = self.directory/"finalization.json"
        record = json.loads(path.read_bytes())
        record["reconciliationFingerprint"] = "a"*64
        path.write_text(json.dumps(record))
        requests = len(self.helper.server.requests)
        with self.assertRaises(AndroidOperationError):
            self.finalize()
        self.assertTrue(self.device.requires_reconciliation)
        self.assertEqual(len(list(self.f.operation.staging_root.iterdir())), 3)
        self.assertGreater(self.runs.status(self.f.operation.operation_id)["reservedBytes"], 0)
        self.assertEqual(len(self.helper.server.requests), requests)

    def test_old_parent_grant_is_rejected_before_any_device_effect(self):
        with self.assertRaisesRegex(AndroidOperationError, "android_recovery_fresh_grant_required"):
            self.operations.finalize_recovery(self.f.operation.operation_id, self.f.operation.request_digest,
                device=self.device, parent_grant=self.f.grant, cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+10)
        self.assertEqual(self.helper.server.requests, [])
        self.assertEqual(self.device.generation, 1)
