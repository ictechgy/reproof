"""Fresh producer ownership is required before preparation disposal and budget release."""
from dataclasses import replace
import json
import hashlib
import multiprocessing
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from reproloop.execution.artifacts import BlobSet
from reproloop.execution.journal import RunDenied, RunStore
from reproloop.execution.wire import canonical
from reproloop.ios_mobile_operation import IOSMobileOperationError, IOSMobileOperationStore
from tests import test_ios_mobile_operation as preparation


def crash_recovery(root, udid, mode):
    from reproloop import ios_mobile_recovery as recovery
    root=Path(root);body=(root/'input.ipa').read_bytes()
    selected=preparation.definition(udid,BlobSet((('original.ipa',body),)))
    runs=RunStore(root/'runs',environment_digest='9'*64,disk_limit=4*1024**3)
    owner=IOSMobileOperationStore(runs,selected,root/'operations',create=False)
    context=preparation.context(selected,body)
    if mode=='unlink':
        original=recovery._unlink_file;count=0
        def changed(*args,**kwargs):
            nonlocal count
            result=original(*args,**kwargs);count+=1
            if count==3:os._exit(71)
            return result
        target=patch.object(recovery,'_unlink_file',side_effect=changed)
    else:
        original=runs._write
        def changed(value):
            releasing=value['runs'][context.operation_id]['reservedBytes']==0
            if releasing and mode=='before-commit':os._exit(72)
            result=original(value)
            if releasing and mode=='after-commit':os._exit(73)
            return result
        target=patch.object(runs,'_write',side_effect=changed)
    with target, owner.preparation_recovery(context.operation_id,context.request_digest,
            cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10) as capability:
        runs.finish_ios_preparation_recovery(capability,authority=owner)
    os._exit(74)


class IOSMobileRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.c=preparation.IOSMobileOperationTests(methodName='runTest')
        self.addCleanup(self.c.doCleanups);self.c.setUp()
        self.root=self.c.operations.root/'operations'/self.c.context.operation_id

    def staged(self):
        with self.c.operations.admit(self.c.context,self.c.artifacts,self.c.baselines) as operation:
            self.c.prepare(operation)

    def fresh(self):
        owner=IOSMobileOperationStore(self.c.runs,self.c.selected,self.c.operations.root,create=False)
        self.addCleanup(owner.close)
        return owner

    def recovery(self, owner=None, **changes):
        return (owner or self.fresh()).preparation_recovery(self.c.context.operation_id,
            changes.get('request_digest',self.c.context.request_digest),
            cancellation=changes.get('cancellation',threading.Event()),
            deadline_monotonic=changes.get('deadline',time.monotonic()+10))

    def finish(self, owner=None):
        owner=owner or self.fresh()
        with self.recovery(owner) as capability:
            return self.c.runs.finish_ios_preparation_recovery(capability,authority=owner)

    def test_fresh_owner_discards_only_preparation_payload_and_returns_original_budget(self):
        self.staged();(self.c.root/'input.ipa').unlink()
        result=self.finish()
        self.assertEqual(result['state'],'failed');self.assertEqual(result['reservedBytes'],0)
        self.assertFalse(any(self.root.rglob('input.ipa')))
        self.assertFalse(any(self.root.rglob('App.app')))
        self.assertTrue((self.root/'producer.lock').is_file())
        record=json.loads((self.root/'recovery.json').read_bytes())
        self.assertEqual(record['state'],'completed')

    def test_live_admission_and_wrong_request_never_start_disposal(self):
        with self.c.operations.admit(self.c.context,self.c.artifacts,self.c.baselines):
            with self.assertRaises((IOSMobileOperationError,RunDenied)):
                with self.recovery():pass
        with self.assertRaises((IOSMobileOperationError,RunDenied)):
            with self.recovery(request_digest='0'*64):pass
        self.assertFalse((self.root/'recovery.json').exists())
        self.assertTrue((self.root/'candidate/input.ipa').exists())

    def test_capability_copies_reuse_wrong_store_and_late_use_are_rejected(self):
        self.staged();owner=self.fresh()
        with self.recovery(owner) as capability:
            with self.assertRaises(RunDenied):
                self.c.runs.finish_ios_preparation_recovery(replace(capability),authority=owner)
            with self.assertRaises(RunDenied):
                self.c.runs.finish_ios_preparation_recovery(replace(capability,operation_id=[]),authority=owner)
            other=RunStore(self.c.root/'unrelated-runs',environment_digest='9'*64,disk_limit=4*1024**3)
            with self.assertRaises(RunDenied):other.finish_ios_preparation_recovery(capability,authority=owner)
            result=self.c.runs.finish_ios_preparation_recovery(capability,authority=owner)
            self.assertEqual(result['reservedBytes'],0)
            with self.assertRaises(RunDenied):self.c.runs.finish_ios_preparation_recovery(capability,authority=owner)
        with self.assertRaises(RunDenied):self.c.runs.finish_ios_preparation_recovery(capability,authority=owner)

    def test_disposal_without_capability_consumption_keeps_reservation(self):
        self.staged()
        with self.recovery() as capability:
            self.assertFalse(any(self.root.rglob('input.ipa')))
        self.assertGreater(self.c.runs.status(self.c.context.operation_id)['reservedBytes'],0)
        self.assertEqual(self.finish()['reservedBytes'],0)

    def test_unexpected_files_and_links_are_preserved(self):
        self.staged();outside=self.c.root/'outside.txt';outside.write_text('owned outside sentinel')
        unexpected=self.root/'candidate/App.app/unexpected.txt';unexpected.write_text('owned unregistered data')
        with self.assertRaises(IOSMobileOperationError):self.finish()
        self.assertTrue(unexpected.exists());self.assertTrue((self.root/'candidate/input.ipa').exists())
        unexpected.unlink();unexpected.symlink_to(outside)
        with self.assertRaises(IOSMobileOperationError):self.finish()
        self.assertEqual(outside.read_text(),'owned outside sentinel')
        self.assertGreater(self.c.runs.status(self.c.context.operation_id)['reservedBytes'],0)

    def test_discarded_json_flag_cannot_replace_actual_payload_disposal(self):
        from reproloop import ios_mobile_recovery as recovery
        self.staged()
        with patch.object(recovery,'_dispose_role',side_effect=RuntimeError('owned pause')):
            with self.assertRaises(IOSMobileOperationError):self.finish()
        record=json.loads((self.root/'recovery.json').read_bytes());record['state']='discarded'
        (self.root/'recovery.json').write_bytes(canonical(record))
        with self.assertRaises(IOSMobileOperationError):self.finish()
        self.assertTrue((self.root/'candidate/input.ipa').exists())
        self.assertGreater(self.c.runs.status(self.c.context.operation_id)['reservedBytes'],0)

    def test_rewritten_baseline_and_journal_cannot_change_the_original_binding(self):
        self.staged()
        archive=self.root/'original/input.ipa';body=archive.read_bytes()
        changed=body[:-1]+bytes([body[-1]^1]);archive.write_bytes(changed)
        intent=json.loads((self.root/'intent.json').read_bytes())
        intent['roles']['original']['sha256']=hashlib.sha256(changed).hexdigest()
        (self.root/'intent.json').write_bytes(canonical(intent))
        with self.assertRaises(IOSMobileOperationError):self.finish()
        self.assertTrue(archive.exists());self.assertTrue((self.root/'candidate/input.ipa').exists())

    def test_cancelled_or_expired_recovery_does_not_delete_files(self):
        self.staged();cancel=threading.Event();cancel.set()
        for changes in ({'cancellation':cancel},{'deadline':time.monotonic()-1}):
            with self.subTest(changes=changes),self.assertRaises(IOSMobileOperationError):
                with self.recovery(**changes):pass
        self.assertTrue((self.root/'candidate/input.ipa').exists())

    def test_cancelled_original_remains_cancelled_after_storage_cleanup(self):
        self.staged()
        self.c.runs.cancel(self.c.context.operation_id,self.c.context.request_digest)
        result=self.finish()
        self.assertEqual(result['state'],'cancelled');self.assertEqual(result['reservedBytes'],0)

    def test_capability_cannot_be_consumed_from_another_thread(self):
        self.staged();owner=self.fresh();errors=[]
        with self.recovery(owner) as capability:
            def consume():
                try:self.c.runs.finish_ios_preparation_recovery(capability,authority=owner)
                except RunDenied:errors.append('denied')
            worker=threading.Thread(target=consume);worker.start();worker.join(3)
            self.assertFalse(worker.is_alive());self.assertEqual(errors,['denied'])
            self.assertGreater(self.c.runs.status(self.c.context.operation_id)['reservedBytes'],0)
            self.assertEqual(self.c.runs.finish_ios_preparation_recovery(capability,authority=owner)['reservedBytes'],0)

    def test_producer_lock_blocks_recovery_after_admission_exits_but_callback_is_live(self):
        from reproloop import ios_mobile_operation as mobile
        entered=threading.Event();release=threading.Event();errors=[]
        original=mobile._move_new_app
        def paused(*args):
            entered.set()
            if not release.wait(3):raise RuntimeError('Owned callback pause expired')
            return original(*args)
        manager=self.c.operations.admit(self.c.context,self.c.artifacts,self.c.baselines)
        operation=manager.__enter__();exited=False;worker=None
        def prepare():
            try:self.c.prepare(operation)
            except IOSMobileOperationError:errors.append('cancelled')
        try:
            with patch.object(mobile,'_move_new_app',side_effect=paused):
                worker=threading.Thread(target=prepare);worker.start();self.assertTrue(entered.wait(2))
                manager.__exit__(None,None,None);exited=True
                with self.assertRaises(IOSMobileOperationError):self.finish()
                self.assertFalse((self.root/'recovery.json').exists())
                self.assertTrue((self.root/'candidate/input.ipa').exists())
                release.set();worker.join(5)
                self.assertFalse(worker.is_alive());self.assertEqual(errors,['cancelled'])
            self.assertEqual(self.finish()['reservedBytes'],0)
        finally:
            release.set()
            if worker is not None:worker.join(5)
            if not exited:manager.__exit__(None,None,None)

    def test_abrupt_preparation_loss_recovers_partial_transfer(self):
        process=multiprocessing.get_context('spawn').Process(target=preparation.crash_during_prepare,
            args=(str(self.c.root),self.c.selected.udid))
        process.start();process.join(15)
        if process.is_alive():
            process.terminate();process.join(5);self.fail('Owned preparation child did not stop')
        self.assertEqual(process.exitcode,73);process.close()
        self.assertTrue((self.root/'candidate/transfer/app').exists())
        self.assertEqual(self.finish()['reservedBytes'],0)
        self.assertFalse((self.root/'candidate/transfer/app').exists())

    def test_actual_process_loss_during_disposal_and_budget_commit_resumes(self):
        for mode,code in (('unlink',71),('before-commit',72),('after-commit',73)):
            with self.subTest(mode=mode):
                # Each case owns a distinct canonical device and journal.
                case=IOSMobileRecoveryTests(methodName='runTest')
                self.addCleanup(case.doCleanups);case.setUp();case.staged()
                process=multiprocessing.get_context('spawn').Process(target=crash_recovery,
                    args=(str(case.c.root),case.c.selected.udid,mode))
                process.start();process.join(15)
                if process.is_alive():
                    process.terminate();process.join(5)
                    self.fail('Owned recovery child did not stop')
                self.assertEqual(process.exitcode,code);process.close()
                self.assertEqual(case.finish()['reservedBytes'],0)
                self.assertFalse(any(case.root.rglob('input.ipa')))
                self.assertEqual(case.c.runs.status(case.c.context.operation_id)['state'],'failed')
