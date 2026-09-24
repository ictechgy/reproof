"""Recovery reconciliation keeps quarantine until every owned file is retired."""

import os
import threading
import time
import unittest
from unittest.mock import patch

from reproof.ios_recovery_finalization import (
    IOSRecoveryFinalizationError,
    IOSRecoveryFinalizationObservation,
    finalize_recovery,
)
from reproof.ios_mobile_finalization import IOSNativeFinalizationError
from reproof.ios_mobile_operation import IOSMobileOperationStore
from reproof.live.authority import HostAuthority
from reproof.live.authority import issue_local_parent_grant
from reproof.ios_native_recovery import native_recovery
from reproof.execution.wire import canonical
from tests import test_ios_recovery_execution as support
from tests.ios_service_support import SanitationHTTPDouble
from tests.ios_service_support import IOSServiceFixture


class IOSRecoveryFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.case = support.IOSRecoveryExecutionTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def finalize(self):
        original_status = SanitationHTTPDouble._status
        def recovery_status(double):
            value = original_status(double)
            value["helperIncarnation"] = (
                double.launch._runner.native_owner.helper_incarnation
            )
            return value
        with patch.object(SanitationHTTPDouble, "_status", recovery_status):
            return finalize_recovery(
                self.case.operations,
                self.case.fixture.context.operation_id,
                self.case.fixture.context.request_digest,
                config=self.case.fixture.config,
                device=self.case.device,
                parent_grant=self.case.grant,
                cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 60,
            )

    def test_measured_recovery_discards_staging_and_releases_accounting(self):
        observed = self.finalize()
        self.assertIs(type(observed), IOSRecoveryFinalizationObservation)
        self.assertTrue(observed.ownership_released)
        self.assertTrue(observed.reservation_released)
        row = self.case.fixture.runs.status(self.case.fixture.context.operation_id)
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["reservedBytes"], 0)
        operation = (self.case.operations.operations
                     / self.case.fixture.context.operation_id)
        for role in self.case.operations._roles:
            self.assertFalse((operation / role / "input.ipa").exists())
            self.assertFalse((operation / role / "App.app").exists())
        self.assertEqual(list((operation / "native-recovery" / "archives").iterdir()), [])
        self.assertEqual(
            self.case.authority.store.device(self.case.fixture.config.scope_digest)["status"],
            "released",
        )
        lease = self.case.authority.lease_directory / (
            self.case.fixture.config.scope_digest + ".lock"
        )
        descriptor = os.open(lease, os.O_RDWR | os.O_NOFOLLOW)
        try:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

    def test_interrupted_disposal_resumes_without_repeating_device_reconciliation(self):
        from reproof import ios_recovery_finalization as finalization
        dispose = finalization.discard_recovery_native_staged

        def interrupted(*args, **kwargs):
            dispose(*args, **kwargs)
            raise OSError("owned post-disposal interruption")

        with patch.object(finalization, "discard_recovery_native_staged",
                          side_effect=interrupted):
            with self.assertRaises(IOSRecoveryFinalizationError):
                self.finalize()
        first_generation = self.case.device.generation
        self.assertEqual(first_generation, 2)
        self.assertTrue(self.case.device.requires_reconciliation)
        self.assertGreater(
            self.case.fixture.runs.status(self.case.fixture.context.operation_id)["reservedBytes"],
            0,
        )
        old_authority = self.case.authority
        self.assertTrue(self.case.operations.close())
        old_authority.close()
        authority = HostAuthority(
            old_authority.store.path,
            lease_directory=old_authority.lease_directory,
        )
        self.addCleanup(authority.close)
        received = authority.clock_sync.sample()
        sent = authority.clock_sync.sample()
        mapping = authority.clock_sync.record_exchange(
            coordinator_clock_id="ios-finalization-restart",
            coordinator_send_ns=received.nanoseconds,
            host_received=received,
            host_sent=sent,
            coordinator_receive_ns=sent.nanoseconds,
            max_drift_ppm=0,
        )
        grant = authority.issue_parent_grant(
            mapping, grant_id="ios_finalization_restart_grant",
            project_id=self.case.fixture.registration.project["id"],
            controller_id="ios_finalization_restart_controller",
            renewal_sequence=1,
            coordinator_deadline_ns=sent.nanoseconds + 60_000_000_000,
        )
        device = authority.claim_device(
            device_kind="ios-physical", physical_id=self.case.fixture.udid,
            helper_incarnation="ignored_restart_helper", parent_grant=grant,
        )
        self.addCleanup(device.close)
        operations = IOSMobileOperationStore(
            self.case.fixture.runs, self.case.fixture.config.definition,
            self.case.operations.root, create=False,
        )
        self.addCleanup(operations.close)
        observed = finalize_recovery(
            operations, self.case.fixture.context.operation_id,
            self.case.fixture.context.request_digest,
            config=self.case.fixture.config, device=device, parent_grant=grant,
            cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 30,
        )
        self.assertTrue(observed.ownership_released)
        self.assertEqual(device.generation, first_generation)
        self.assertEqual(
            self.case.fixture.runs.status(self.case.fixture.context.operation_id)["reservedBytes"],
            0,
        )

    def test_wrong_request_and_reintroduced_staged_file_cannot_report_release(self):
        from reproof import ios_recovery_finalization as finalization
        dispose = finalization.discard_recovery_native_staged
        def interrupted(*args, **kwargs):
            dispose(*args, **kwargs)
            raise OSError("owned post-disposal interruption")
        with patch.object(finalization, "discard_recovery_native_staged",
                          side_effect=interrupted):
            with self.assertRaises(IOSRecoveryFinalizationError):
                self.finalize()
        operation = (self.case.operations.operations
                     / self.case.fixture.context.operation_id)
        injected = operation / "original" / "input.ipa"
        injected.write_bytes(b"reintroduced")
        injected.chmod(0o600)
        with self.assertRaises(IOSRecoveryFinalizationError):
            self.finalize()
        self.assertGreater(
            self.case.fixture.runs.status(self.case.fixture.context.operation_id)["reservedBytes"],
            0,
        )
        with self.assertRaises(IOSRecoveryFinalizationError):
            finalize_recovery(
                self.case.operations, self.case.fixture.context.operation_id,
                "0" * 64, config=self.case.fixture.config,
                device=self.case.device, parent_grant=self.case.grant,
                cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 10,
            )

    def test_interruption_before_reconcile_repeats_measured_attempt_before_cleanup(self):
        with patch.object(self.case.device, "reconcile",
                          side_effect=OSError("owned pre-reconcile interruption")):
            with self.assertRaises(IOSRecoveryFinalizationError):
                self.finalize()
        self.assertEqual(self.case.device.generation, 1)
        self.assertGreater(
            self.case.fixture.runs.status(self.case.fixture.context.operation_id)["reservedBytes"],
            0,
        )
        observed = self.finalize()
        self.assertTrue(observed.ownership_released)
        self.assertEqual(self.case.device.generation, 2)
        operation = (self.case.operations.operations
                     / self.case.fixture.context.operation_id / "native-recovery")
        self.assertTrue((operation / "attempt-001").is_dir())
        self.assertTrue((operation / "attempt-002").is_dir())

    def test_completed_record_write_failure_closes_exact_device_before_retry(self):
        from reproof import ios_recovery_finalization as finalization
        save = finalization._Session.save
        def interrupted(session, state, **kwargs):
            if state == "completed":
                raise OSError("owned completion record interruption")
            return save(session, state, **kwargs)
        with patch.object(finalization._Session, "save", new=interrupted):
            with self.assertRaises(IOSRecoveryFinalizationError):
                self.finalize()
        self.assertTrue(self.case.device._closed)
        lease = self.case.authority.lease_directory / (
            self.case.fixture.config.scope_digest + ".lock"
        )
        descriptor = os.open(lease, os.O_RDWR | os.O_NOFOLLOW)
        try:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        observed = self.finalize()
        self.assertTrue(observed.ownership_released)
        self.assertTrue(observed.reservation_released)

    def test_post_accounting_reintroduced_file_blocks_device_release(self):
        finish = self.case.fixture.runs.finish_ios_native_recovery
        operation = (self.case.operations.operations
                     / self.case.fixture.context.operation_id)
        def interrupted(capability, *, authority):
            result = finish(capability, authority=authority)
            injected = operation / "original" / "input.ipa"
            injected.write_bytes(b"post-accounting reintroduction")
            injected.chmod(0o600)
            raise OSError("owned post-accounting interruption")
        with patch.object(self.case.fixture.runs, "finish_ios_native_recovery",
                          side_effect=interrupted):
            with self.assertRaises(IOSRecoveryFinalizationError):
                self.finalize()
        row = self.case.fixture.runs.status(self.case.fixture.context.operation_id)
        self.assertEqual(row["reservedBytes"], 0)
        self.assertIn(row["state"], {"failed", "cancelled"})
        with self.assertRaises(IOSRecoveryFinalizationError):
            self.finalize()
        device = self.case.authority.store.device(self.case.fixture.config.scope_digest)
        self.assertEqual(device["status"], "quarantined")
        self.assertEqual(device["quarantine_reason"], "recovery-cleanup-pending")

    def test_normal_finalizer_recovers_same_owner_post_publish_interruption(self):
        from reproof import ios_mobile_finalization as finalization
        from reproof.ios_mobile_callbacks import IOSNativeCallbackCoordinator
        from tests.test_ios_mobile_finalization import IOSMobileFinalizationTests

        normal = IOSMobileFinalizationTests(methodName="runTest")
        normal.setUp()
        self.addCleanup(normal.doCleanups)
        with normal.owned() as (operation, owner):
            coordinator = IOSNativeCallbackCoordinator(owner)
            publish = finalization._record_write_new
            interrupted = {"done": False}

            def post_publish(parent, name, value):
                publish(parent, name, value)
                if name == "intent.json" and not interrupted["done"]:
                    interrupted["done"] = True
                    raise OSError("owned post-link interruption")

            with patch.object(finalization, "_record_write_new", side_effect=post_publish):
                status, error = normal._cleanup_once(
                    coordinator, operation, owner, install_failure=True
                )
            self.assertEqual(status, "error")
            self.assertIsInstance(error, IOSNativeFinalizationError)
            status, evidence = normal._cleanup_once(coordinator, operation, owner)
            self.assertEqual(status, "ok")
            self.assertRegex(evidence, r"^[0-9a-f]{64}$")

    def test_normal_finalizer_recovers_empty_record_directory_and_mid_role_stop(self):
        from reproof import ios_mobile_finalization as finalization
        from reproof.ios_mobile_callbacks import IOSNativeCallbackCoordinator
        from tests.test_ios_mobile_finalization import IOSMobileFinalizationTests

        for interruption in ("after-mkdir-fsync", "before-intent", "after-candidate"):
            with self.subTest(interruption=interruption):
                normal = IOSMobileFinalizationTests(methodName="runTest")
                normal.setUp()
                self.addCleanup(normal.doCleanups)
                with normal.owned() as (operation, owner):
                    coordinator = IOSNativeCallbackCoordinator(owner)
                    root = normal.case.operations.operations / operation.context.operation_id
                    if interruption == "after-mkdir-fsync":
                        sync = finalization.os.fsync
                        stopped = {"done": False}
                        def fail_after_mkdir(descriptor):
                            sync(descriptor)
                            final = root / finalization.FINALIZATION_DIRECTORY
                            if (not stopped["done"] and final.is_dir()
                                    and not any(final.iterdir())):
                                stopped["done"] = True
                                raise OSError("owned post-mkdir fsync interruption")
                        selected = patch.object(finalization.os, "fsync",
                                                side_effect=fail_after_mkdir)
                    elif interruption == "before-intent":
                        publish = finalization._record_write_new
                        stopped = {"done": False}
                        def fail_before(parent, name, value):
                            if name == "intent.json" and not stopped["done"]:
                                stopped["done"] = True
                                raise OSError("owned pre-link interruption")
                            return publish(parent, name, value)
                        selected = patch.object(finalization, "_record_write_new",
                                                side_effect=fail_before)
                    else:
                        dispose = finalization._dispose_role
                        stopped = {"done": False}
                        def fail_after(owner_arg, role, record, session):
                            result = dispose(owner_arg, role, record, session)
                            if role == "candidate" and not stopped["done"]:
                                stopped["done"] = True
                                raise OSError("owned mid-role interruption")
                            return result
                        selected = patch.object(finalization, "_dispose_role",
                                                side_effect=fail_after)
                    with selected:
                        status, error = normal._cleanup_once(
                            coordinator, operation, owner, install_failure=True
                        )
                    self.assertEqual(status, "error")
                    self.assertIsInstance(error, IOSNativeFinalizationError)
                    if interruption in {"after-mkdir-fsync", "before-intent"}:
                        self.assertEqual(
                            list((root / finalization.FINALIZATION_DIRECTORY).iterdir()), []
                        )
                    else:
                        self.assertFalse((root / "candidate" / "input.ipa").exists())
                        self.assertTrue((root / "original" / "input.ipa").exists())
                    status, evidence = normal._cleanup_once(coordinator, operation, owner)
                    self.assertEqual(status, "ok", evidence)
                    self.assertRegex(evidence, r"^[0-9a-f]{64}$")

    def test_native_restart_inspects_empty_and_intent_only_finalization_publish(self):
        from reproof import ios_mobile_finalization as finalization
        from reproof.ios_mobile_callbacks import IOSNativeCallbackCoordinator
        from tests.test_ios_mobile_finalization import IOSMobileFinalizationTests

        for interruption in ("empty", "intent-only", "linked-temp"):
            with self.subTest(interruption=interruption):
                normal = IOSMobileFinalizationTests(methodName="runTest")
                normal.setUp()
                self.addCleanup(normal.doCleanups)
                with normal.owned() as (operation, owner):
                    coordinator = IOSNativeCallbackCoordinator(owner)
                    root = normal.case.operations.operations / operation.context.operation_id
                    if interruption == "empty":
                        sync = finalization.os.fsync
                        stopped = {"done": False}
                        def interrupted_sync(descriptor):
                            sync(descriptor)
                            directory = root / finalization.FINALIZATION_DIRECTORY
                            if (not stopped["done"] and directory.is_dir()
                                    and not any(directory.iterdir())):
                                stopped["done"] = True
                                raise OSError("owned empty finalization interruption")
                        selected = patch.object(finalization.os, "fsync",
                                                side_effect=interrupted_sync)
                    elif interruption == "intent-only":
                        publish = finalization._record_write_new
                        stopped = {"done": False}
                        def interrupted_publish(parent, name, value):
                            publish(parent, name, value)
                            if name == "intent.json" and not stopped["done"]:
                                stopped["done"] = True
                                raise OSError("owned intent-only interruption")
                        selected = patch.object(finalization, "_record_write_new",
                                                side_effect=interrupted_publish)
                    else:
                        stopped = {"done": False}
                        def orphaned_publish(parent, name, value):
                            if name != "intent.json" or stopped["done"]:
                                return finalization._record_write_new(parent, name, value)
                            stopped["done"] = True
                            body = canonical(value)
                            temporary = ".native-record-" + "a" * 32
                            descriptor = os.open(
                                temporary,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=parent,
                            )
                            try:
                                os.write(descriptor, body)
                                os.fsync(descriptor)
                            finally:
                                os.close(descriptor)
                            os.link(temporary, name, src_dir_fd=parent,
                                    dst_dir_fd=parent, follow_symlinks=False)
                            os.fsync(parent)
                            raise OSError("owned linked temp interruption")
                        selected = patch.object(finalization, "_record_write_new",
                                                side_effect=orphaned_publish)
                    with selected:
                        status, error = normal._cleanup_once(
                            coordinator, operation, owner, install_failure=True
                        )
                    self.assertEqual(status, "error")
                    self.assertIsInstance(error, IOSNativeFinalizationError)
                    owner.device.revoke_dispatches()
                    snapshot = owner.device.recovery_snapshot()
                self.assertTrue(normal.case.operations.close())
                reopened = IOSMobileOperationStore(
                    normal.case.runs, normal.case.selected,
                    normal.case.operations.root, create=False,
                )
                self.addCleanup(reopened.close)
                grant = issue_local_parent_grant(
                    normal.authority, lifetime_ns=60_000_000_000
                )
                with native_recovery(
                    reopened, operation.context.operation_id,
                    operation.context.request_digest, device=normal.device,
                    snapshot=snapshot, parent_grant=grant,
                    cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 10,
                ) as context:
                    self.assertEqual(context.operation_id, operation.context.operation_id)

    def test_intent_only_restart_runs_fresh_recovery_and_consumes_reservation(self):
        from reproof import ios_mobile_finalization as mobile_finalization
        from reproof.ios_mobile_callbacks import IOSNativeCallbackCoordinator

        fixture = IOSServiceFixture()
        self.addCleanup(fixture.close)
        old_authority = fixture.authority
        device = old_authority.claim_device(
            device_kind="ios-physical", physical_id=fixture.udid,
            helper_incarnation="partial_publish_helper",
            parent_grant=fixture.grant,
        )
        result = []
        with fixture.admit() as operation:
            for role in fixture.operations._roles:
                fixture.operations.prepare(
                    operation, role, cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 30,
                )
            with fixture.operations.native_owner(operation, device) as owner:
                coordinator = IOSNativeCallbackCoordinator(owner)
                publish = mobile_finalization._record_write_new
                stopped = {"done": False}
                def intent_only(parent, name, value):
                    publish(parent, name, value)
                    if name == "intent.json" and not stopped["done"]:
                        stopped["done"] = True
                        raise OSError("owned intent-only restart")
                def callback():
                    try:
                        with coordinator.callback(operation, "install") as token:
                            token.fail()
                        with coordinator.callback(operation, "cleanup") as token:
                            with patch.object(mobile_finalization, "_record_write_new",
                                              side_effect=intent_only):
                                mobile_finalization.discard_native_staged(
                                    owner, token, cancellation=threading.Event(),
                                    deadline_monotonic=time.monotonic() + 20,
                                )
                    except BaseException as error:
                        result.append(error)
                worker = threading.Thread(target=callback)
                worker.start(); worker.join(25)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(result), 1)
                self.assertIsInstance(result[0], IOSNativeFinalizationError)
                coordinator.close()
                device.revoke_dispatches()
        self.assertFalse(device.close())
        old_authority.close()
        self.assertTrue(fixture.operations.close())

        authority = HostAuthority(
            fixture.root / "authority.sqlite3", lease_directory=fixture.root / "leases"
        )
        self.addCleanup(authority.close)
        received = authority.clock_sync.sample()
        sent = authority.clock_sync.sample()
        mapping = authority.clock_sync.record_exchange(
            coordinator_clock_id="intent-only-restart",
            coordinator_send_ns=received.nanoseconds,
            host_received=received, host_sent=sent,
            coordinator_receive_ns=sent.nanoseconds, max_drift_ppm=0,
        )
        grant = authority.issue_parent_grant(
            mapping, grant_id="intent_only_restart_grant",
            project_id=fixture.registration.project["id"],
            controller_id="intent_only_restart_controller", renewal_sequence=1,
            coordinator_deadline_ns=sent.nanoseconds + 60_000_000_000,
        )
        device = authority.claim_device(
            device_kind="ios-physical", physical_id=fixture.udid,
            helper_incarnation="ignored_intent_only_restart", parent_grant=grant,
        )
        self.addCleanup(device.close)
        operations = IOSMobileOperationStore(
            fixture.runs, fixture.config.definition, fixture.operations.root, create=False
        )
        self.addCleanup(operations.close)
        observed = finalize_recovery(
            operations, fixture.context.operation_id, fixture.context.request_digest,
            config=fixture.config, device=device, parent_grant=grant,
            cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 60,
        )
        self.assertTrue(observed.ownership_released and observed.reservation_released)
        self.assertEqual(fixture.runs.status(fixture.context.operation_id)["reservedBytes"], 0)


if __name__ == "__main__":
    unittest.main()
