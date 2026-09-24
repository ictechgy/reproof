"""Bounded iOS native staged-file disposal over owned local fixtures."""
from contextlib import contextmanager
import json
import os
import threading
import time
import unittest

from reproof.ios_mobile_callbacks import IOSNativeCallbackCoordinator
from reproof.ios_mobile_finalization import (
    FINALIZATION_DIRECTORY,
    IOSNativeFinalizationError,
    discard_native_staged,
)
from reproof.live.authority import HostAuthority, issue_local_parent_grant
from tests import test_ios_mobile_operation as preparation


class IOSMobileFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.case = preparation.IOSMobileOperationTests(methodName="runTest")
        self.case.setUp()
        self.authority = HostAuthority(
            self.case.root / "authority/state.sqlite3",
            lease_directory=self.case.root / "device-leases",
        )
        self.grant = issue_local_parent_grant(self.authority, lifetime_ns=600_000_000_000)
        self.device = self.authority.claim_device(
            device_kind="ios-physical",
            physical_id=self.case.selected.udid,
            helper_incarnation="owned-ios-finalization-helper",
            parent_grant=self.grant,
        )
        self.addCleanup(self.case.doCleanups)
        self.addCleanup(self.authority.close)
        self.addCleanup(self.device.close)

    @contextmanager
    def owned(self):
        with self.case.operations.admit(
            self.case.context, self.case.artifacts, self.case.baselines
        ) as operation:
            for role in self.case.operations._roles:
                self.case.operations.prepare(
                    operation,
                    role,
                    cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 10,
                )
            with self.case.operations.native_owner(operation, self.device) as owner:
                yield operation, owner

    @staticmethod
    def _command_work(root):
        work = root / "command-install-candidate-work"
        work.mkdir(mode=0o700)
        for name in ("intent.json", "state.json", "native.json", "session.xctestrun"):
            path = work / name
            path.write_bytes(b"{}")
            path.chmod(0o600)
        result = work / "result.json"
        result.write_bytes(b"{}")
        result.chmod(0o600)
        return work

    @staticmethod
    def _run_install_failure(coordinator, operation):
        with coordinator.callback(operation, "install") as token:
            token.fail()

    def _cleanup_once(self, coordinator, operation, owner, *, cancellation=None, deadline=None,
                      install_failure=False):
        result = []
        cancellation = cancellation or threading.Event()
        deadline = time.monotonic() + 20 if deadline is None else deadline

        def worker():
            try:
                if install_failure:
                    self._run_install_failure(coordinator, operation)
                with coordinator.callback(operation, "cleanup") as token:
                    result.append(
                        (
                            "ok",
                            discard_native_staged(
                                owner,
                                token,
                                cancellation=cancellation,
                                deadline_monotonic=deadline,
                            ),
                        )
                    )
            except BaseException as error:  # retain the callback's exact failure for assertions
                result.append(("error", error))

        thread = threading.Thread(target=worker, name="ios-native-finalization-test")
        thread.start()
        thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        return result[0]

    def test_disposes_owned_payload_and_outputs_preserving_native_journals(self):
        with self.owned() as (operation, owner):
            root = self.case.operations.operations / operation.context.operation_id
            work = self._command_work(root)
            coordinator = IOSNativeCallbackCoordinator(owner)
            status, evidence = self._cleanup_once(
                coordinator, operation, owner, install_failure=True
            )
            self.assertEqual(status, "ok")
            self.assertRegex(evidence, r"^[0-9a-f]{64}$")
            for role in self.case.operations._roles:
                role_root = root / role
                self.assertFalse((role_root / "input.ipa").exists())
                self.assertFalse((role_root / "App.app").exists())
                self.assertEqual(list((role_root / "transfer").iterdir()), [])
            self.assertTrue((root / "native.json").exists())
            self.assertTrue((root / "phases" / "install" / "state.json").exists())
            self.assertTrue((root / "phases" / "cleanup" / "state.json").exists())
            self.assertEqual(
                {path.name for path in work.iterdir()},
                {"intent.json", "state.json", "native.json", "session.xctestrun"},
            )
            state = json.loads((root / FINALIZATION_DIRECTORY / "state.json").read_bytes())
            self.assertEqual(state["state"], "discarded")
            self.assertEqual(state["evidenceDigest"], evidence)

    def test_known_xctest_results_are_removed_but_launch_journals_remain(self):
        with self.owned() as (operation, owner):
            root = self.case.operations.operations / operation.context.operation_id
            work = root / "command-xctest-candidate-001-work"
            work.mkdir(mode=0o700)
            for name in ("intent.json", "state.json", "native.json", "session.xctestrun"):
                path = work / name
                path.write_bytes(b"{}")
                path.chmod(0o600)
            snapshot = work / ".native-session.xctestrun"
            snapshot.write_bytes(b"snapshot")
            snapshot.chmod(0o400)
            home = work / "home"
            home.mkdir(mode=0o700)
            (home / "xcode.log").write_bytes(b"owned log")
            (home / "xcode.log").chmod(0o600)
            result = work / "result.xcresult"
            result.mkdir(mode=0o755)
            (result / "Data").write_bytes(b"owned result")
            (result / "Data").chmod(0o600)
            coordinator = IOSNativeCallbackCoordinator(owner)
            status, evidence = self._cleanup_once(
                coordinator, operation, owner, install_failure=True
            )
            self.assertEqual(status, "ok")
            self.assertRegex(evidence, r"^[0-9a-f]{64}$")
            self.assertFalse(home.exists())
            self.assertFalse(result.exists())
            self.assertFalse(snapshot.exists())
            self.assertEqual(
                {path.name for path in work.iterdir()},
                {"intent.json", "state.json", "native.json", "session.xctestrun"},
            )

    def test_unknown_file_is_preserved_and_retry_uses_same_owner(self):
        with self.owned() as (operation, owner):
            root = self.case.operations.operations / operation.context.operation_id
            unknown = root / "candidate" / "transfer" / "unexpected.bin"
            unknown.write_bytes(b"keep")
            unknown.chmod(0o600)
            coordinator = IOSNativeCallbackCoordinator(owner)
            status, failure = self._cleanup_once(
                coordinator, operation, owner, install_failure=True
            )
            self.assertEqual(status, "error")
            self.assertIsInstance(failure, IOSNativeFinalizationError)
            self.assertTrue(unknown.exists())
            self.assertTrue((root / "candidate" / "input.ipa").exists())
            unknown.unlink()
            status, evidence = self._cleanup_once(coordinator, operation, owner)
            self.assertEqual(status, "ok")
            self.assertRegex(evidence, r"^[0-9a-f]{64}$")
            self.assertFalse((root / "candidate" / "input.ipa").exists())

    def test_symlink_output_is_rejected_without_deletion(self):
        with self.owned() as (operation, owner):
            root = self.case.operations.operations / operation.context.operation_id
            foreign = self.case.root / "foreign-output"
            foreign.write_bytes(b"outside")
            link = root / "candidate" / "transfer" / "linked"
            link.symlink_to(foreign)
            coordinator = IOSNativeCallbackCoordinator(owner)
            status, failure = self._cleanup_once(
                coordinator, operation, owner, install_failure=True
            )
            self.assertEqual(status, "error")
            self.assertIsInstance(failure, IOSNativeFinalizationError)
            self.assertTrue(link.is_symlink())
            self.assertTrue((root / "original" / "input.ipa").exists())

    def test_hardlink_output_is_rejected_without_deletion(self):
        with self.owned() as (operation, owner):
            root = self.case.operations.operations / operation.context.operation_id
            extra = root / "candidate" / "App.app" / "known-extra"
            extra.write_bytes(b"unknown")
            extra.chmod(0o600)
            hardlink = root / "candidate" / "transfer" / "hardlink"
            os.link(extra, hardlink)
            coordinator = IOSNativeCallbackCoordinator(owner)
            status, failure = self._cleanup_once(
                coordinator, operation, owner, install_failure=True
            )
            self.assertEqual(status, "error")
            self.assertIsInstance(failure, IOSNativeFinalizationError)
            self.assertTrue(extra.exists())
            self.assertTrue(hardlink.exists())

    def test_expired_attempt_keeps_files_and_retry_completes(self):
        with self.owned() as (operation, owner):
            root = self.case.operations.operations / operation.context.operation_id
            coordinator = IOSNativeCallbackCoordinator(owner)
            status, failure = self._cleanup_once(
                coordinator,
                operation,
                owner,
                install_failure=True,
                deadline=time.monotonic() - 1,
            )
            self.assertEqual(status, "error")
            self.assertIsInstance(failure, IOSNativeFinalizationError)
            self.assertTrue((root / "original" / "input.ipa").exists())
            status, evidence = self._cleanup_once(coordinator, operation, owner)
            self.assertEqual(status, "ok")
            self.assertRegex(evidence, r"^[0-9a-f]{64}$")
            self.assertFalse((root / "original" / "input.ipa").exists())

    def test_preparation_recovery_capability_and_wrong_phase_token_cannot_dispose(self):
        with self.owned() as (operation, owner):
            with self.assertRaises(IOSNativeFinalizationError):
                discard_native_staged(
                    owner,
                    object(),
                    cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 5,
                )
            coordinator = IOSNativeCallbackCoordinator(owner)
            result = []

            def wrong_phase():
                try:
                    with coordinator.callback(operation, "install") as token:
                        discard_native_staged(
                            owner,
                            token,
                            cancellation=threading.Event(),
                            deadline_monotonic=time.monotonic() + 5,
                        )
                except BaseException as error:
                    result.append(error)

            thread = threading.Thread(target=wrong_phase)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], IOSNativeFinalizationError)
            self.assertTrue((self.case.operations.operations / operation.context.operation_id
                             / "original" / "input.ipa").exists())

    def test_rewritten_finalization_record_cannot_become_authority(self):
        with self.owned() as (operation, owner):
            root = self.case.operations.operations / operation.context.operation_id
            coordinator = IOSNativeCallbackCoordinator(owner)
            result = []

            def worker():
                try:
                    self._run_install_failure(coordinator, operation)
                    with coordinator.callback(operation, "cleanup") as token:
                        result.append(discard_native_staged(
                            owner, token, cancellation=threading.Event(),
                            deadline_monotonic=time.monotonic() + 20,
                        ))
                        intent = json.loads((root / FINALIZATION_DIRECTORY / "intent.json").read_bytes())
                        intent["nativeBindingDigest"] = "0" * 64
                        (root / FINALIZATION_DIRECTORY / "intent.json").write_text(json.dumps(intent))
                        with self.assertRaises(IOSNativeFinalizationError):
                            discard_native_staged(
                                owner, token, cancellation=threading.Event(),
                                deadline_monotonic=time.monotonic() + 20,
                            )
                except BaseException as error:
                    result.append(error)

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(20)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(result), 1)
            self.assertRegex(result[0], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
