"""Restart recovery retains only the original iOS operation/device ownership."""

from dataclasses import replace
import fcntl
import json
import os
import threading
import time
import unittest

from reproloop.execution.wire import canonical
from reproloop.ios_mobile_operation import IOSMobileOperationError, IOSMobileOperationStore
from reproloop.ios_native_recovery import (
    IOSNativeRecoveryError,
    IOSNativeRecoveryContext,
    native_recovery,
    require_native_recovery,
)
from reproloop.live.authority import HostAuthority, issue_local_parent_grant
from tests import test_ios_mobile_operation as preparation


class IOSNativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = preparation.IOSMobileOperationTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.authority = HostAuthority(
            self.case.root / "authority" / "state.sqlite3",
            lease_directory=self.case.root / "device-leases",
        )
        self.addCleanup(self.authority.close)
        self.grant = issue_local_parent_grant(self.authority, lifetime_ns=600_000_000_000)
        self.device = self.authority.claim_device(
            device_kind="ios-physical",
            physical_id=self.case.selected.udid,
            helper_incarnation="owned-ios-recovery-helper",
            parent_grant=self.grant,
        )
        self.addCleanup(self.device.close)
        self.operation_id = self.case.context.operation_id
        self.request_digest = self.case.context.request_digest
        self.operation_root = self.case.operations.operations / self.operation_id

    def _seed_native_binding(self):
        with self.case.operations.admit(
            self.case.context, self.case.artifacts, self.case.baselines
        ) as operation:
            for role in ("candidate", "original"):
                self.case.prepare(operation, role)
            with self.case.operations.native_owner(operation, self.device):
                pass
        self.device.revoke_dispatches()
        return self.device.recovery_snapshot()

    def _reopened(self):
        reopened = IOSMobileOperationStore(
            self.case.runs,
            self.case.selected,
            self.case.operations.root,
            create=False,
        )
        self.addCleanup(reopened.close)
        return reopened

    def _borrow(self, operations=None, **changes):
        snapshot = changes.pop("snapshot", self.snapshot)
        values = dict(
            device=changes.pop("device", self.device),
            snapshot=snapshot,
            parent_grant=changes.pop("parent_grant", self.grant),
            cancellation=changes.pop("cancellation", threading.Event()),
            deadline_monotonic=changes.pop("deadline_monotonic", time.monotonic() + 10),
        )
        values.update(changes)
        return native_recovery(
            operations or self.operations,
            self.operation_id,
            self.request_digest,
            **values,
        )

    @property
    def operations(self):
        return self.case.operations_reopened

    def test_restart_reopens_exact_operation_and_live_device_lease(self):
        self.snapshot = self._seed_native_binding()
        self.case.operations_reopened = self._reopened()
        with self._borrow(self.case.operations_reopened) as context:
            self.assertIs(type(context), IOSNativeRecoveryContext)
            self.assertEqual(context.operation_id, self.operation_id)
            self.assertEqual(context.request_digest, self.request_digest)
            self.assertEqual(context.scope_digest, self.case.selected.scope_digest)
            self.assertEqual(context.native["bindingDigest"], context.binding_digest)
            self.assertEqual(context.grant_deadline_ns, self.grant.local_deadline_ns)
            self.assertGreater(context.deadline_monotonic, time.monotonic())
            self.assertEqual(
                os.fstat(context.producer).st_ino,
                os.stat(self.operation_root / "producer.lock").st_ino,
            )
            self.assertEqual(
                os.fstat(context.device_lease.descriptor).st_ino,
                os.fstat(self.device._lease.file.fileno()).st_ino,
            )
            self.assertIn("nativeBindingDigest", context.public)
            self.assertFalse(context.public["deviceCleanupConfirmed"])
            self.assertEqual(context.public["executionAuthority"], "none")
            self.assertEqual(json.loads(json.dumps(context.public)), dict(context.public))
            with self.assertRaises(TypeError):
                context.public["state"] = "sanitized"
            self.assertNotIn(str(self.case.selected.udid), repr(context.public))
            self.assertNotIn(str(self.case.operations.root), repr(context.public))

            for path in (
                self.operation_root / "producer.lock",
                self.authority.lease_directory
                / (self.case.selected.scope_digest + ".lock"),
            ):
                descriptor = os.open(path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)

            self.assertIs(require_native_recovery(context), context)
        with self.assertRaises(IOSNativeRecoveryError):
            require_native_recovery(context)
        self.assertGreater(self.case.runs.status(self.operation_id)["reservedBytes"], 0)
        self.assertTrue(self.device.requires_reconciliation)

    def test_wrong_request_device_and_root_are_rejected_without_authorizing_preparation(self):
        self.snapshot = self._seed_native_binding()
        reopened = self._reopened()
        with self.assertRaises(IOSNativeRecoveryError):
            with native_recovery(
                reopened,
                self.operation_id,
                "0" * 64,
                device=self.device,
                snapshot=self.snapshot,
                parent_grant=self.grant,
                cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 10,
            ):
                pass

        foreign = self.authority.claim_device(
            device_kind="ios-physical",
            physical_id=self.case.selected.udid + "-foreign",
            helper_incarnation="foreign-ios-recovery-helper",
            parent_grant=self.grant,
        )
        self.addCleanup(foreign.close)
        with self.assertRaises(IOSNativeRecoveryError):
            with native_recovery(
                reopened,
                self.operation_id,
                self.request_digest,
                device=foreign,
                snapshot=self.snapshot,
                parent_grant=self.grant,
                cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 10,
            ):
                pass

        other_root = self.case.root / "other-operations"
        other = IOSMobileOperationStore(
            self.case.runs, self.case.selected, other_root, create=True
        )
        self.addCleanup(other.close)
        with self.assertRaises(IOSNativeRecoveryError):
            with native_recovery(
                other,
                self.operation_id,
                self.request_digest,
                device=self.device,
                snapshot=self.snapshot,
                parent_grant=self.grant,
                cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 10,
            ):
                pass
        self.assertTrue((self.operation_root / "candidate" / "input.ipa").is_file())

    def test_live_owner_copy_thread_expiry_and_same_inode_reopen_are_rejected(self):
        self.snapshot = self._seed_native_binding()
        reopened = self._reopened()

        active_case = preparation.IOSMobileOperationTests(methodName="runTest")
        active_case.setUp()
        self.addCleanup(active_case.doCleanups)
        active_authority = HostAuthority(
            active_case.root / "authority" / "state.sqlite3",
            lease_directory=active_case.root / "device-leases",
        )
        self.addCleanup(active_authority.close)
        active_grant = issue_local_parent_grant(
            active_authority, lifetime_ns=600_000_000_000
        )
        active_device = active_authority.claim_device(
            device_kind="ios-physical",
            physical_id=active_case.selected.udid,
            helper_incarnation="active-ios-recovery-helper",
            parent_grant=active_grant,
        )
        self.addCleanup(active_device.close)
        with active_case.operations.admit(
            active_case.context, active_case.artifacts, active_case.baselines
        ) as active_operation:
            for role in ("candidate", "original"):
                active_case.prepare(active_operation, role)
            with active_case.operations.native_owner(active_operation, active_device):
                active_device.revoke_dispatches()
                with self.assertRaises(IOSNativeRecoveryError):
                    with native_recovery(
                        active_case.operations,
                        active_case.context.operation_id,
                        active_case.context.request_digest,
                        device=active_device,
                        snapshot=active_device.recovery_snapshot(),
                        parent_grant=active_grant,
                        cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic() + 10,
                    ):
                        pass

        with self.assertRaises(IOSNativeRecoveryError):
            with self._borrow(reopened, deadline_monotonic=time.monotonic() - 1):
                pass

        with self._borrow(reopened) as context:
            with self.assertRaises(IOSNativeRecoveryError):
                require_native_recovery(replace(context))

            replacement = os.open(self.operation_root / "producer.lock", os.O_RDWR)
            original = context.producer
            try:
                context.producer = replacement
                with self.assertRaises(IOSNativeRecoveryError):
                    require_native_recovery(context)
            finally:
                context.producer = original
                os.close(replacement)
            self.assertIs(require_native_recovery(context), context)

            native_path = self.operation_root / "native.json"
            native_bytes = native_path.read_bytes()
            try:
                native_record = json.loads(native_bytes)
                native_record["authorityRootDigest"] = "0" * 64
                native_path.write_bytes(canonical(native_record))
                with self.assertRaises(IOSNativeRecoveryError):
                    require_native_recovery(context)
            finally:
                native_path.write_bytes(native_bytes)

            rejected = []

            def check():
                try:
                    require_native_recovery(context)
                except IOSNativeRecoveryError:
                    rejected.append(True)

            worker = threading.Thread(target=check)
            worker.start()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(rejected, [True])

            cancelled = threading.Event()
            with self.assertRaises(IOSNativeRecoveryError):
                context._cancellation = cancelled
                require_native_recovery(context)
            context._cancellation = context._original_cancellation
            context._cancellation.set()
            with self.assertRaises(IOSNativeRecoveryError):
                require_native_recovery(context)

    def test_unknown_native_journal_is_rejected_and_preparation_recovery_cannot_authorize(self):
        self.snapshot = self._seed_native_binding()
        (self.operation_root / "unrecognized-journal").write_bytes(canonical({"bad": True}))
        with self.assertRaises(IOSNativeRecoveryError):
            with self._borrow(self._reopened()):
                pass
        (self.operation_root / "unrecognized-journal").unlink()
        reopened = self._reopened()
        with self.assertRaises(IOSMobileOperationError):
            with reopened.preparation_recovery(
                self.operation_id,
                self.request_digest,
                cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 10,
            ):
                pass


if __name__ == "__main__":
    unittest.main()
