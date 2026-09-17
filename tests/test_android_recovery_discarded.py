"""An interrupted normal cleanup can recover from currently installed APKs."""
import json
import threading
import time
import unittest
from unittest.mock import patch

from reproloop.repair_android_operation import AndroidOperationError
from tests import test_android_recovery as device_support
from tests import test_android_recovery_finalization as support


class AndroidDiscardedStageRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AndroidRecoveryFinalizationTests.setUpClass()
        cls.addClassCleanup(support.AndroidRecoveryFinalizationTests.doClassCleanups)

    def fixture(self, mode="complete"):
        fixture = support.AndroidRecoveryFinalizationTests(methodName="runTest")
        self.addCleanup(fixture.doCleanups)
        with patch.object(device_support.AndroidDeviceRecoveryTests, "discard_before_recovery", mode, create=True):
            fixture.setUp()
        return fixture

    def recover(self, fixture, *, expected_failure=False):
        from reproloop import android_recovery, android_recovery_helper
        calls=[]
        original=android_recovery.run_native_adb
        def observed(*args,**kwargs):
            started=time.monotonic()
            try:
                result=original(*args,**kwargs)
            except Exception as error:
                calls.append({'seconds':round(time.monotonic()-started,3),'errorType':type(error).__name__})
                raise
            calls.append({'seconds':round(time.monotonic()-started,3),'returncode':result.returncode,
                'interrupted':result.interrupted,'bounded':result.bounded,'terminated':result.terminated,
                'stdoutBytes':len(result.stdout),'stderrBytes':len(result.stderr)})
            return result
        with patch.object(android_recovery,'run_native_adb',side_effect=observed), \
                patch.object(android_recovery_helper,'run_native_adb',side_effect=observed):
            try:
                return fixture.operations.finalize_recovery(
                    fixture.f.operation.operation_id, fixture.f.operation.request_digest,
                    device=fixture.device, parent_grant=fixture.grant(), cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic()+15,
                )
            except AndroidOperationError:
                if not expected_failure:
                    path=fixture.directory/'recovery.json'
                    self.fail({'calls':calls,'record':json.loads(path.read_bytes()) if path.exists() else None})
                raise

    def respond(self, fixture, command, output):
        original = fixture.helper.server.shell_session
        def changed(connection, current):
            observed = original(connection, current)  # Consume the owned SDK stdin close first.
            return output if current == command else observed
        fixture.helper.server.shell_session = changed

    def test_already_discarded_staging_uses_fresh_installed_hashes_and_releases(self):
        f = self.fixture()
        self.assertEqual(list(f.f.operation.staging_root.iterdir()), [])
        self.assertFalse((f.directory/"finalization.json").exists())
        result = self.recover(f)
        self.assertTrue(result.reservation_released and result.ownership_released)
        self.assertEqual(f.runs.status(f.f.operation.operation_id)["reservedBytes"], 0)
        self.assertIsNone(f.helper.server.installed_bytes)
        record = json.loads((f.directory/"recovery.json").read_bytes())
        self.assertEqual(record["recoveryMode"], "installed-original")
        self.assertEqual(set(record["steps"]), {"stop-helper", "stop-target", "confirm-stopped",
            "installed-path", "installed-hash", "clear-original", "confirm-cleared"})
        self.assertTrue(f.helper.started.is_set() and f.helper.stopped.is_set())
        self.assertFalse(any(request.startswith(b"exec:cmd package ") for request in f.helper.server.requests))

    def test_partial_normal_discard_is_finished_only_after_fresh_recovery(self):
        f = self.fixture("partial")
        self.assertEqual(len(list(f.f.operation.staging_root.iterdir())), 2)
        self.assertTrue(self.recover(f).ownership_released)
        self.assertEqual(list(f.f.operation.staging_root.iterdir()), [])
        self.assertIsNone(f.helper.server.installed_bytes)

    def test_different_installed_original_does_not_clear_or_release(self):
        f = self.fixture()
        self.respond(f, b"sha256sum /data/app/owned/base.apk", b"0"*64+b"  /data/app/owned/base.apk\n")
        with self.assertRaises(AndroidOperationError):
            self.recover(f,expected_failure=True)
        self.assertFalse(any(command.startswith("pm clear ") for command in f.f.commands))
        self.assertIsNone(f.helper.configured)
        self.assertTrue(f.device.requires_reconciliation)
        self.assertGreater(f.runs.status(f.f.operation.operation_id)["reservedBytes"], 0)

    def test_different_installed_helper_is_not_started_or_released(self):
        f = self.fixture()
        self.respond(f, b"sha256sum /data/app/helper/base.apk", b"0"*64+b"  /data/app/helper/base.apk\n")
        with self.assertRaises(AndroidOperationError):
            self.recover(f,expected_failure=True)
        self.assertFalse(f.helper.started.is_set())
        self.assertIsNone(f.helper.configured)
        self.assertTrue(f.device.requires_reconciliation)
        self.assertGreater(f.runs.status(f.f.operation.operation_id)["reservedBytes"], 0)

    def test_missing_installed_apk_does_not_start_helper_or_release(self):
        from reproloop.live.android_live import HELPER
        for target in ('original','helper'):
            with self.subTest(target=target):
                f=self.fixture()
                package=f.f.config.package if target=='original' else HELPER
                self.respond(f,('pm path '+package).encode(),b'')
                with self.assertRaises(AndroidOperationError):
                    self.recover(f,expected_failure=True)
                self.assertFalse(f.helper.started.is_set())
                self.assertIsNone(f.helper.configured)
                self.assertTrue(f.helper.server.installed_digests)
                self.assertTrue(set(f.helper.server.installed_digests) <= {
                    f.f.config.original_profile.data['artifact']['sha256'],f.f.config.helper_digest})
                self.assertTrue(f.device.requires_reconciliation)
                self.assertGreater(f.runs.status(f.f.operation.operation_id)['reservedBytes'],0)

    def test_missing_files_without_discard_intent_are_not_treated_as_cleaned(self):
        f = self.fixture()
        (f.directory/"discard.json").unlink()
        with self.assertRaises(AndroidOperationError):
            self.recover(f,expected_failure=True)
        self.assertEqual(f.helper.server.requests, [])
        self.assertGreater(f.runs.status(f.f.operation.operation_id)["reservedBytes"], 0)

    def test_incomplete_installed_recovery_record_does_not_claim_restoration(self):
        f = self.fixture()
        self.assertTrue(self.recover(f).ownership_released)
        path = f.directory/"recovery.json"
        record = json.loads(path.read_bytes())
        record["steps"].pop("installed-hash")
        path.write_text(json.dumps(record))
        self.assertEqual(f.operations.status(f.f.operation.operation_id)["state"], "record-invalid")
