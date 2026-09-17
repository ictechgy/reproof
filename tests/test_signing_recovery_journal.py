"""Only a live signing recovery capability can release signing reservations."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from reproloop import contracts
from reproloop.execution.journal import RunDenied, RunStore
from reproloop.repair_signing_recovery import MIN_OPERATION_BYTES, SigningOperationStore
from tests import test_repair_signing_recovery as support


class SigningRecoveryJournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.SigningRecoveryTests.setUpClass()
        cls.tools = support.SigningRecoveryTests.tools
        cls.identity = support.SigningRecoveryTests.identity

    @classmethod
    def tearDownClass(cls): support.SigningRecoveryTests.tearDownClass()

    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.store=RunStore(self.root/'state',environment_digest='e'*64,disk_limit=MIN_OPERATION_BYTES)
        self.scope=contracts.digest(str(self.root))
        self.operations=SigningOperationStore(self.store,self.scope,self.tools,self.identity,self.root/'operations')
        self.addCleanup(self.operations.close)
        self.context=support._context(); self.request='f'*64
        with self.operations.admit(self.context,self.request,MIN_OPERATION_BYTES): pass
        self.assertEqual(self.store.status(self.context.operation_id)['reservedBytes'],MIN_OPERATION_BYTES)

    def recover(self):
        return self.operations.recovery(self.context.operation_id,self.request)

    def test_live_cleanup_releases_only_as_failed_and_is_one_use(self):
        with self.recover() as capability:
            result=self.store.finish_signing_recovery(capability,authority=self.operations)
            self.assertEqual(result['state'],'failed')
            self.assertEqual(result['reservedBytes'],0)
            with self.assertRaises(RunDenied):
                self.store.finish_signing_recovery(capability,authority=self.operations)

    def test_cancellation_remains_cancelled_after_recovery(self):
        self.store.cancel(self.context.operation_id,self.request)
        with self.recover() as capability:
            result=self.store.finish_signing_recovery(capability,authority=self.operations)
        self.assertEqual(result['state'],'cancelled')
        self.assertEqual(result['reservedBytes'],0)

    def test_copied_stale_and_foreign_capabilities_keep_the_reservation(self):
        with self.recover() as capability:
            copied=replace(capability)
            with self.assertRaises(RunDenied):
                self.store.finish_signing_recovery(copied,authority=self.operations)
            with self.assertRaises(RunDenied):
                self.store.finish_signing_recovery(capability,authority=object())
        with self.assertRaises(RunDenied):
            self.store.finish_signing_recovery(capability,authority=self.operations)
        self.assertEqual(self.store.status(self.context.operation_id)['reservedBytes'],MIN_OPERATION_BYTES)

    def test_vm_shaped_or_unexpected_run_files_are_not_signing_cleanup(self):
        path=self.store.root/'runs'/self.context.operation_id/'termination.json'
        path.write_bytes(b'{}')
        with self.recover() as capability, self.assertRaises(RunDenied):
            self.store.finish_signing_recovery(capability,authority=self.operations)
        self.assertTrue(path.is_file())
        self.assertEqual(self.store.status(self.context.operation_id)['reservedBytes'],MIN_OPERATION_BYTES)

    def test_retry_after_empty_run_directory_removal_does_not_lose_cleanup_proof(self):
        with self.recover() as capability:
            with mock.patch.object(self.store,'_write',side_effect=RunDenied('injected journal write interruption')):
                with self.assertRaises(RunDenied):
                    self.store.finish_signing_recovery(capability,authority=self.operations)
        self.assertFalse((self.store.root/'runs'/self.context.operation_id).exists())
        self.assertEqual(self.store.status(self.context.operation_id)['reservedBytes'],MIN_OPERATION_BYTES)
        with self.recover() as capability:
            result=self.store.finish_signing_recovery(capability,authority=self.operations)
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['reservedBytes'],0)


if __name__=='__main__':unittest.main()
