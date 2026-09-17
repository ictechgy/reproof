"""Fixed original-app sanitation while the native iOS recovery lease is live."""

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reproloop import contracts
from reproloop.ios_mobile_native import IOSMobileNativeOwner
from reproloop.ios_mobile_operation import IOSMobileOperationStore
from reproloop.ios_native_recovery import IOSNativeRecoveryError, native_recovery
from reproloop.ios_recovery_execution import (
    IOSRecoveryDispatch,
    IOSRecoveryExecution,
    IOSRecoveryExecutionObservation,
)
from reproloop.live.authority import HostAuthority
from tests.ios_service_support import IOSServiceFixture, SanitationHTTPDouble


_CRASH_EXIT = 73


class IOSRecoveryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = IOSServiceFixture()
        self.addCleanup(self.fixture.close)
        self.old_authority = self.fixture.authority
        self.device = self.old_authority.claim_device(
            device_kind="ios-physical",
            physical_id=self.fixture.udid,
            helper_incarnation="interrupted_ios_helper",
            parent_grant=self.fixture.grant,
        )
        with self.fixture.admit() as operation:
            for role in self.fixture.operations._roles:
                self.fixture.operations.prepare(
                    operation, role, cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 60,
                )
            with self.fixture.operations.native_owner(operation, self.device):
                admission = self.device.admit_operation(
                    operation_id="interrupted_ios_dispatch",
                    payload_digest=contracts.digest("interrupted-ios-payload"),
                    session_id="interrupted_ios_session",
                    sequence=1,
                )
                self.device.prepare_dispatch(
                    admission, provider_incarnation="interrupted_ios_provider"
                )
        self.assertFalse(self.device.close())
        self.old_authority.close()
        self.assertTrue(self.fixture.operations.close())

        self.authority = HostAuthority(
            self.fixture.root / "authority.sqlite3",
            lease_directory=self.fixture.root / "leases",
        )
        self.addCleanup(self.authority.close)
        received = self.authority.clock_sync.sample()
        sent = self.authority.clock_sync.sample()
        mapping = self.authority.clock_sync.record_exchange(
            coordinator_clock_id="ios-recovery-coordinator",
            coordinator_send_ns=received.nanoseconds,
            host_received=received,
            host_sent=sent,
            coordinator_receive_ns=sent.nanoseconds,
            max_drift_ppm=0,
        )
        self.grant = self.authority.issue_parent_grant(
            mapping,
            grant_id="ios_recovery_grant",
            project_id=self.fixture.registration.project["id"],
            controller_id="ios_recovery_controller",
            renewal_sequence=1,
            coordinator_deadline_ns=sent.nanoseconds + 600_000_000_000,
        )
        self.device = self.authority.claim_device(
            device_kind="ios-physical",
            physical_id=self.fixture.udid,
            helper_incarnation="ignored_recovery_claim_helper",
            parent_grant=self.grant,
        )
        self.addCleanup(self.device.close)
        self.operations = IOSMobileOperationStore(
            self.fixture.runs,
            self.fixture.config.definition,
            self.fixture.operations.root,
            create=False,
        )
        self.addCleanup(self.operations.close)

    def _recovery(self):
        return native_recovery(
            self.operations,
            self.fixture.context.operation_id,
            self.fixture.context.request_digest,
            device=self.device,
            snapshot=self.device.recovery_snapshot(),
            parent_grant=self.grant,
            cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 60,
        )

    def _restart_host(self):
        self.device.close()
        self.authority.close()
        self.assertTrue(self.operations.close())
        self.authority = HostAuthority(
            self.fixture.root / "authority.sqlite3",
            lease_directory=self.fixture.root / "leases",
        )
        self.addCleanup(self.authority.close)
        received = self.authority.clock_sync.sample()
        sent = self.authority.clock_sync.sample()
        mapping = self.authority.clock_sync.record_exchange(
            coordinator_clock_id="ios-recovery-coordinator",
            coordinator_send_ns=received.nanoseconds,
            host_received=received,
            host_sent=sent,
            coordinator_receive_ns=sent.nanoseconds,
            max_drift_ppm=0,
        )
        self.grant = self.authority.issue_parent_grant(
            mapping,
            grant_id="ios_recovery_restart_" + self.authority.host_incarnation[-12:],
            project_id=self.fixture.registration.project["id"],
            controller_id="ios_recovery_controller",
            renewal_sequence=1,
            coordinator_deadline_ns=sent.nanoseconds + 600_000_000_000,
        )
        self.device = self.authority.claim_device(
            device_kind="ios-physical",
            physical_id=self.fixture.udid,
            helper_incarnation="ignored_restarted_recovery_helper",
            parent_grant=self.grant,
        )
        self.addCleanup(self.device.close)
        self.operations = IOSMobileOperationStore(
            self.fixture.runs,
            self.fixture.config.definition,
            self.fixture.operations.root,
            create=False,
        )
        self.addCleanup(self.operations.close)

    def _crash_execution(self, point):
        temporary = Path(tempfile.gettempdir())
        self._ipa_temps_before = {
            (path.name, path.lstat().st_dev, path.lstat().st_ino)
            for path in temporary.glob("repro-ios-ipa-*")
        }
        pid = os.fork()
        if pid == 0:
            try:
                from reproloop import ios_recovery_execution as module

                def die():
                    os._exit(_CRASH_EXIT)

                with self._recovery() as recovery:
                    execution = IOSRecoveryExecution(recovery, self.fixture.config)
                    if point == "recovery-mkdir":
                        original = module._open_child_directory
                        def opened(parent, name, *args, **kwargs):
                            descriptor = original(parent, name, *args, **kwargs)
                            if name == "native-recovery":
                                os.close(descriptor)
                                die()
                            return descriptor
                        with patch.object(module, "_open_child_directory", opened):
                            execution.__enter__()
                    elif point in {"archives-mkdir", "root-intent", "root-state",
                                   "attempt-intent", "attempt-state"}:
                        original = module._write_new_at
                        def published(parent, name, value, *args, **kwargs):
                            kind = value.get("kind") if type(value) is dict else None
                            before = (
                                (point == "archives-mkdir" and kind == "ios-native-recovery-materials-v1")
                                or (point == "attempt-mkdir" and kind == "ios-native-recovery-attempt-v1")
                            )
                            if before:
                                die()
                            result = original(parent, name, value, *args, **kwargs)
                            after = (
                                (point == "root-intent" and kind == "ios-native-recovery-materials-v1")
                                or (point == "root-state" and kind == "ios-native-recovery-materials-state-v1")
                                or (point == "attempt-intent" and kind == "ios-native-recovery-attempt-v1")
                                or (point == "attempt-state" and kind == "ios-native-recovery-attempt-state-v1")
                            )
                            if after:
                                die()
                            return result
                        with patch.object(module, "_write_new_at", published):
                            execution.__enter__()
                    elif point == "first-archive":
                        original = module._write_bytes
                        calls = [0]
                        def written(descriptor, body):
                            original(descriptor, body)
                            calls[0] += 1
                            if calls[0] == 1:
                                die()
                        with patch.object(module, "_write_bytes", written):
                            execution.__enter__()
                    elif point == "attempt-mkdir":
                        original = module._write_new_at
                        def publishing(parent, name, value, *args, **kwargs):
                            if name == "intent.json" and value.get("kind") == "ios-native-recovery-attempt-v1":
                                die()
                            return original(parent, name, value, *args, **kwargs)
                        with patch.object(module, "_write_new_at", publishing):
                            execution.__enter__()
                    elif point == "role-mkdir":
                        original = module._replace_at
                        def replaced(parent, name, value, *args, **kwargs):
                            result = original(parent, name, value, *args, **kwargs)
                            roles = value.get("roles", {}) if type(value) is dict else {}
                            if (value.get("kind") == "ios-native-recovery-attempt-state-v1"
                                    and roles.get("original", {}).get("state") == "preparing"):
                                die()
                            return result
                        with patch.object(module, "_replace_at", replaced):
                            execution.__enter__()
                    elif point == "app-move":
                        original = module._extract_registered_app
                        def moved(body, destination):
                            original(body, destination)
                            die()
                        with patch.object(module, "_extract_registered_app", moved):
                            execution.__enter__()
                    elif point == "mid-extraction":
                        original = module._create_relative_file
                        calls = [0]
                        def created(parent, relative):
                            descriptor = original(parent, relative)
                            calls[0] += 1
                            if calls[0] == 1:
                                os.close(descriptor)
                                die()
                            return descriptor
                        with patch.object(module, "_create_relative_file", created):
                            execution.__enter__()
                    else:
                        execution.__enter__()
                        if point == "retiring-write":
                            original = module._replace_at
                            def retiring(parent, name, value, *args, **kwargs):
                                result = original(parent, name, value, *args, **kwargs)
                                if (value.get("kind") == "ios-native-recovery-attempt-state-v1"
                                        and value.get("materialState") == "retiring"
                                        and all(row.get("state") == "ready"
                                                for row in value["roles"].values())):
                                    die()
                                return result
                            with patch.object(module, "_replace_at", retiring):
                                execution.__exit__(None, None, None)
                        elif point == "role-rmdir":
                            original = module.os.rmdir
                            def removed(name, *args, **kwargs):
                                result = original(name, *args, **kwargs)
                                if name == "original":
                                    die()
                                return result
                            with patch.object(module.os, "rmdir", removed):
                                execution.__exit__(None, None, None)
                        elif point == "mid-app-unlink":
                            from reproloop import ios_mobile_recovery as cleanup_module
                            original = cleanup_module._unlink_file
                            calls = [0]
                            def unlinked(parent, name, info, session):
                                result = original(parent, name, info, session)
                                calls[0] += 1
                                if calls[0] == 1:
                                    die()
                                return result
                            with patch.object(cleanup_module, "_unlink_file", unlinked):
                                execution.__exit__(None, None, None)
                        else:
                            os._exit(75)
                os._exit(76)
            except BaseException:
                os._exit(77)
        waited, status = os.waitpid(pid, 0)
        self.assertEqual(waited, pid)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), _CRASH_EXIT)
        self._restart_host()

    def _assert_crash_resumes(self, point):
        self._crash_execution(point)
        with self._recovery() as recovery:
            execution = IOSRecoveryExecution(recovery, self.fixture.config)
            with execution:
                attempts = execution.inspect_attempts()
                self.assertLessEqual(attempts[-1]["attempt"], 3)
                self.assertEqual(attempts[-1]["materialState"], "ready")
            attempts = execution.inspect_attempts()
            self.assertTrue(all(row["materialState"] == "retired" for row in attempts))
        temporary = Path(tempfile.gettempdir())
        self.assertEqual({
            (path.name, path.lstat().st_dev, path.lstat().st_ino)
            for path in temporary.glob("repro-ios-ipa-*")
        }, self._ipa_temps_before)
        self.assertGreater(
            self.fixture.runs.status(self.fixture.context.operation_id)["reservedBytes"], 0
        )

    def test_restart_resumes_recovery_root_publication(self):
        self._assert_crash_resumes("recovery-mkdir")

    def test_restart_resumes_first_archive_publication(self):
        self._assert_crash_resumes("first-archive")

    def test_restart_resumes_archives_directory_before_intent(self):
        self._assert_crash_resumes("archives-mkdir")

    def test_restart_resumes_root_intent_before_state(self):
        self._assert_crash_resumes("root-intent")

    def test_restart_resumes_root_state_before_archives(self):
        self._assert_crash_resumes("root-state")

    def test_restart_resumes_empty_attempt_directory(self):
        self._assert_crash_resumes("attempt-mkdir")

    def test_restart_retires_attempt_intent_before_state(self):
        self._assert_crash_resumes("attempt-intent")

    def test_restart_retires_attempt_state_before_roles(self):
        self._assert_crash_resumes("attempt-state")

    def test_restart_retires_role_directory_created_before_state_completion(self):
        self._assert_crash_resumes("role-mkdir")

    def test_restart_retires_app_moved_before_ready_state(self):
        self._assert_crash_resumes("app-move")

    def test_restart_retires_journaled_mid_extraction_tree(self):
        self._assert_crash_resumes("mid-extraction")

    def test_restart_finishes_persisted_retiring_transition(self):
        self._assert_crash_resumes("retiring-write")

    def test_restart_finishes_role_rmdir_before_retired_state(self):
        self._assert_crash_resumes("role-rmdir")

    def test_restart_finishes_mid_app_tree_unlink(self):
        self._assert_crash_resumes("mid-app-unlink")

    def test_replaced_partial_archive_inode_fails_closed(self):
        self._crash_execution("first-archive")
        archive = (
            self.operations._operation_root(self.fixture.context.operation_id)
            / "native-recovery" / "archives" / "original.ipa"
        )
        body = archive.read_bytes()
        archive.unlink()
        archive.write_bytes(body)
        archive.chmod(0o600)
        with self.assertRaises(IOSNativeRecoveryError):
            with self._recovery():
                pass
        self.assertTrue(archive.is_file())
        self.assertGreater(
            self.fixture.runs.status(self.fixture.context.operation_id)["reservedBytes"], 0
        )

    def test_unknown_partial_extraction_node_fails_closed_without_deletion(self):
        self._crash_execution("mid-extraction")
        app = (
            self.operations._operation_root(self.fixture.context.operation_id)
            / "native-recovery" / "attempt-001" / "original" / "App.app"
        )
        unknown = app / "unexpected-recovery-node"
        unknown.write_bytes(b"unknown")
        unknown.chmod(0o600)
        with self.assertRaises(IOSNativeRecoveryError):
            with self._recovery():
                pass
        self.assertEqual(unknown.read_bytes(), b"unknown")
        self.assertGreater(
            self.fixture.runs.status(self.fixture.context.operation_id)["reservedBytes"], 0
        )

    def test_changed_journaled_app_content_fails_closed_without_deletion(self):
        self._crash_execution("app-move")
        app = (
            self.operations._operation_root(self.fixture.context.operation_id)
            / "native-recovery" / "attempt-001" / "original" / "App.app"
        )
        selected = app / "Info.plist"
        body = bytearray(selected.read_bytes())
        body[-1] ^= 1
        selected.write_bytes(body)
        with self.assertRaises(IOSNativeRecoveryError):
            with self._recovery():
                pass
        self.assertEqual(selected.read_bytes(), bytes(body))
        self.assertGreater(
            self.fixture.runs.status(self.fixture.context.operation_id)["reservedBytes"], 0
        )

    def test_fixed_original_sanitation_uses_fresh_recovery_authority(self):
        cancellation = threading.Event()
        deadline = time.monotonic() + 60
        with self._recovery() as recovery:
            execution = IOSRecoveryExecution(recovery, self.fixture.config)
            with execution as owner:
                self.assertIs(type(owner), IOSMobileNativeOwner)
                self.assertIs(owner._recovery_context, execution)
                self.assertNotEqual(
                    owner.helper_incarnation, recovery.native["helperIncarnation"]
                )
                self.assertEqual(
                    owner.device._authority.host_incarnation,
                    self.authority.host_incarnation,
                )
                original_status = SanitationHTTPDouble._status
                def recovery_status(double):
                    value = original_status(double)
                    value["helperIncarnation"] = (
                        double.launch._runner.native_owner.helper_incarnation
                    )
                    return value
                with patch.object(SanitationHTTPDouble, "_status", recovery_status):
                    observation = execution.run_original_sanitation(
                        cancellation=cancellation, deadline_monotonic=deadline
                    )
                self.assertIs(type(observation), IOSRecoveryExecutionObservation)
                self.assertTrue(observation.public()["originalSanitationConfirmed"])
                self.assertFalse(observation.public()["ownershipReleased"])
                self.assertFalse(observation.public()["deviceReconciled"])
                attempts = execution.inspect_attempts()
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0]["attempt"], 1)
                self.assertEqual(
                    {name: row["state"] for name, row in attempts[0]["slots"].items()},
                    {
                        "restore-original": "settled",
                        "start-original": "settled",
                        "cleanup-original": "settled",
                    },
                )
                token = execution._issued_by_slot["cleanup-original"]
                self.assertIs(type(token), IOSRecoveryDispatch)
                with self.assertRaises(Exception):
                    execution.require_dispatch(
                        owner, replace(token), token.payload_digest
                    )
                with self.assertRaises(Exception):
                    execution.require_dispatch(owner, token, token.payload_digest)
            self.assertNotIn(id(owner), self.operations._native_owners)
            self.assertFalse(self.operations._native_clients)
            self.assertIs(execution.require_observation(observation), observation)
            self.assertTrue(observation.public()["materialsDisposed"])
            self.assertIn("materialDisposalDigest", observation.public())
            self.assertEqual(
                observation.cleanup_receipt_digest,
                contracts.digest(observation.cleanup_observation.sanitation.public()),
            )
            with self.assertRaises(Exception):
                execution.require_observation(replace(observation))

        self.assertTrue(self.device.requires_reconciliation)
        self.assertGreater(
            self.fixture.runs.status(self.fixture.context.operation_id)["reservedBytes"],
            0,
        )

    def test_configuration_or_foreign_owner_cannot_enter_recovery_execution(self):
        with self._recovery() as recovery:
            changed = replace(self.fixture.config, runtime_policy_digest="0" * 64)
            with self.assertRaises(Exception):
                IOSRecoveryExecution(recovery, changed).__enter__()

            execution = IOSRecoveryExecution(recovery, self.fixture.config)
            with execution as owner:
                foreign = object()
                with self.assertRaises(Exception):
                    execution.require_owner(foreign)
                self.assertIs(execution.require_owner(owner), owner)

    def test_three_attempts_reuse_material_budget_and_a_fourth_is_refused(self):
        for expected in range(1, 4):
            with self._recovery() as recovery:
                execution = IOSRecoveryExecution(recovery, self.fixture.config)
                with execution:
                    attempts = execution.inspect_attempts()
                    self.assertEqual(len(attempts), expected)
                    self.assertEqual(attempts[-1]["materialState"], "ready")
                self.assertEqual(
                    execution.inspect_attempts()[-1]["materialState"], "retired"
                )
        with self._recovery() as recovery:
            with self.assertRaises(Exception):
                IOSRecoveryExecution(recovery, self.fixture.config).__enter__()

        recovery_root = (
            self.operations._operation_root(self.fixture.context.operation_id)
            / "native-recovery"
        )
        for number in range(1, 4):
            attempt = recovery_root / f"attempt-{number:03d}"
            self.assertFalse(any((attempt / role).exists() for role in (
                "original", "helper-host", "helper-runner"
            )))


if __name__ == "__main__":
    unittest.main()
