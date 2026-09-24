"""Durable IPA preparation with owned inert app fixtures, not device execution."""
from dataclasses import replace
import hashlib
import io
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import secrets
import stat
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

from reproof import contracts
from reproof.execution.artifacts import BlobSet
from reproof.execution.journal import RunDenied, RunStore
from reproof.execution.wire import canonical
from reproof.repair_mobile import MobileContext
from tests import test_ios_artifact_transfer as fixtures


def definition(udid, baselines):
    from reproof.ios_mobile_operation import IOSMobileDefinition
    return IOSMobileDefinition('a'*64,'ios_app','b'*64,'selected-phone',udid,
        'com.example.flat','c'*64,'d'*64,baselines.digest)


def context(selected, body, identifier='mobile-one'):
    return MobileContext(identifier,contracts.digest(identifier),'e'*64,selected.project_digest,
        selected.application_id,'f'*64,hashlib.sha256(body).hexdigest(),selected.scope_digest,
        selected.runtime_policy_digest,'owned-context-nonce')


def crash_during_prepare(root, udid):
    from reproof import ios_mobile_operation as mobile
    root=Path(root);body=(root/'input.ipa').read_bytes()
    baselines=BlobSet((('original.ipa',body),));selected=definition(udid,baselines)
    store=RunStore(root/'runs',environment_digest='9'*64,disk_limit=4*1024**3)
    operations=mobile.IOSMobileOperationStore(store,selected,root/'operations')
    with operations.admit(context(selected,body),BlobSet((('candidate.ipa',body),)),baselines) as operation:
        with patch.object(mobile,'_move_new_app',side_effect=lambda *_:os._exit(73)):
            operations.prepare(operation,'candidate',cancellation=threading.Event(),
                               deadline_monotonic=time.monotonic()+10)
    os._exit(74)


class IOSMobileOperationTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.IOSArtifactTransferTests(methodName='runTest')
        self.addCleanup(fixture.doCleanups);fixture.setUp()
        self.root=fixture.root
        self.body=fixture.make_ipa(fixture.make_flat_app(),self.root/'input.ipa').read_bytes()
        self.baselines=BlobSet((('original.ipa',self.body),))
        self.selected=definition('owned-'+secrets.token_hex(16),self.baselines)
        self.context=context(self.selected,self.body)
        self.artifacts=BlobSet((('candidate.ipa',self.body),))
        self.runs=RunStore(self.root/'runs',environment_digest='9'*64,disk_limit=4*1024**3)
        from reproof.ios_mobile_operation import IOSMobileOperationStore
        self.operations=IOSMobileOperationStore(self.runs,self.selected,self.root/'operations')
        self.addCleanup(self.operations.close)

    def prepare(self,operation,role='candidate',**changes):
        return self.operations.prepare(operation,role,
            cancellation=changes.get('cancellation',threading.Event()),
            deadline_monotonic=changes.get('deadline',time.monotonic()+10))

    def test_budgeted_archives_and_app_are_bound_to_original_context(self):
        with self.operations.admit(self.context,self.artifacts,self.baselines) as operation:
            prepared=self.prepare(operation)
            self.assertEqual(prepared.manifest['applicationId'],'com.example.flat')
            self.assertTrue(prepared._source.is_relative_to(self.operations.root))
            self.assertGreater(self.runs.status(self.context.operation_id)['reservedBytes'],len(self.body)*2)
            status=self.operations.status(self.context.operation_id)
            self.assertEqual(status['roles']['candidate']['state'],'prepared')
            self.assertEqual(status['roles']['original']['state'],'received')
            self.assertFalse(status['deviceCleanupConfirmed'])
            self.assertEqual(status['executionAuthority'],'none')
            self.assertNotIn(self.selected.udid,json.dumps(status))
        self.assertEqual(self.runs.status(self.context.operation_id)['state'],'quarantined')
        self.assertTrue(prepared._source.is_dir())

    def test_wrong_context_or_original_never_stages_payload(self):
        from reproof.ios_mobile_operation import IOSMobileOperationError
        for changed in (replace(self.context,artifact_digest='0'*64),
                        replace(self.context,scope_digest='0'*64),
                        replace(self.context,project_digest='0'*64)):
            with self.subTest(changed=changed),self.assertRaises(IOSMobileOperationError):
                with self.operations.admit(changed,self.artifacts,self.baselines):pass
        bad=BlobSet((('original.ipa',b'foreign'),))
        with self.assertRaises(IOSMobileOperationError):
            with self.operations.admit(self.context,self.artifacts,bad):pass
        self.assertEqual(list((self.operations.root/'operations').iterdir()),[])

    def test_disk_rejection_precedes_archive_writes(self):
        self.runs.disk_limit=1
        with self.assertRaises(RunDenied):
            with self.operations.admit(self.context,self.artifacts,self.baselines):pass
        self.assertFalse(any(self.operations.root.rglob('input.ipa')))

    def test_baseline_set_cannot_shadow_the_candidate_role(self):
        from reproof.ios_mobile_operation import IOSMobileOperationError,IOSMobileOperationStore
        baselines=BlobSet((('candidate.ipa',self.body),('original.ipa',self.body)))
        selected=replace(self.selected,baseline_digest=baselines.digest)
        other=IOSMobileOperationStore(self.runs,selected,self.root/'overlapping-owner')
        self.addCleanup(other.close)
        with self.assertRaises(IOSMobileOperationError):
            with other.admit(self.context,self.artifacts,baselines):pass
        self.assertFalse(any(other.root.rglob('input.ipa')))

    def test_changed_archive_or_foreign_bundle_does_not_publish_preparation(self):
        from reproof.ios_mobile_operation import IOSMobileOperationError
        with self.operations.admit(self.context,self.artifacts,self.baselines) as operation:
            operation.archive_path('candidate').write_bytes(b'changed')
            with self.assertRaises(IOSMobileOperationError):self.prepare(operation)
            self.assertNotEqual(self.operations.status(self.context.operation_id)['roles']['candidate']['state'],'prepared')

    def test_cancelled_or_closed_operation_cannot_prepare(self):
        from reproof.ios_mobile_operation import IOSMobileOperationError
        cancel=threading.Event();cancel.set()
        with self.operations.admit(self.context,self.artifacts,self.baselines) as operation:
            with self.assertRaises(IOSMobileOperationError):self.prepare(operation,cancellation=cancel)
        with self.assertRaises(IOSMobileOperationError):self.prepare(operation)

    def test_a_new_journal_or_work_root_cannot_bypass_uncertain_original_work(self):
        from reproof.ios_mobile_operation import IOSMobileOperationStore
        with self.operations.admit(self.context,self.artifacts,self.baselines):pass
        for runs in (self.runs,RunStore(self.root/'other-runs',environment_digest='9'*64,disk_limit=4*1024**3)):
            other=IOSMobileOperationStore(runs,self.selected,self.root/('other-'+secrets.token_hex(4)))
            self.addCleanup(other.close)
            with self.assertRaises(RunDenied):
                with other.admit(context(self.selected,self.body,'mobile-two'),self.artifacts,self.baselines):pass

    def test_arbitrary_run_finish_cannot_release_still_staged_files(self):
        with self.operations.admit(self.context,self.artifacts,self.baselines) as operation:
            operation.run.finish('failed',stopped=True)
        status=self.runs.status(self.context.operation_id)
        self.assertEqual(status['state'],'quarantined')
        self.assertGreater(status['reservedBytes'],0)

    def test_rewritten_archive_and_intent_cannot_replace_the_live_operation_input(self):
        from reproof.ios_mobile_operation import IOSMobileOperationError
        with self.operations.admit(self.context,self.artifacts,self.baselines) as operation:
            root=operation.archive_path('candidate').parent.parent
            intent=json.loads((root/'intent.json').read_bytes())
            # Even valid IPA bytes cannot be substituted by rewriting local metadata.
            output=io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(self.body)) as original, zipfile.ZipFile(output,'w') as archive:
                for entry in original.infolist():archive.writestr(entry,original.read(entry))
                app='/'.join(original.namelist()[0].split('/')[:2])
                added=zipfile.ZipInfo(app+'/added.txt');added.create_system=3
                added.external_attr=(stat.S_IFREG|0o600)<<16
                archive.writestr(added,b'changed owned app resource')
            changed=output.getvalue()
            operation.archive_path('candidate').write_bytes(changed)
            intent['roles']['candidate']['sha256']=hashlib.sha256(changed).hexdigest()
            intent['roles']['candidate']['bytes']=len(changed)
            intent['reservedBytes']+=len(changed)-len(self.body)
            (root/'intent.json').write_bytes(canonical(intent))
            with self.assertRaises(IOSMobileOperationError):self.prepare(operation)

    def test_forged_status_payload_is_rejected_instead_of_being_published(self):
        from reproof.ios_mobile_operation import IOSMobileOperationError
        with self.operations.admit(self.context,self.artifacts,self.baselines) as operation:
            path=operation.archive_path('candidate').parent.parent/'state.json'
            state=json.loads(path.read_bytes());state['roles']['candidate']['private']='owned-private-value'
            path.write_bytes(canonical(state))
            with self.assertRaises(IOSMobileOperationError):self.operations.status(self.context.operation_id)

    def test_wrong_bundle_cannot_publish_a_prepared_app(self):
        from reproof.ios_mobile_operation import IOSMobileOperationError,IOSMobileOperationStore
        selected=replace(self.selected,bundle_id='com.example.foreign')
        other=IOSMobileOperationStore(self.runs,selected,self.root/'foreign-owner')
        self.addCleanup(other.close)
        with other.admit(self.context,self.artifacts,self.baselines) as operation:
            with self.assertRaises(IOSMobileOperationError):
                other.prepare(operation,'candidate',cancellation=threading.Event(),deadline_monotonic=time.monotonic()+5)
            self.assertEqual(other.status(self.context.operation_id)['roles']['candidate']['state'],'preparing')

    def test_close_keeps_running_preparation_and_budget_until_callback_returns(self):
        from reproof import ios_mobile_operation as mobile
        entered=threading.Event();release=threading.Event();errors=[]
        original=mobile._move_new_app
        def paused(*args):
            entered.set()
            if not release.wait(3):raise RuntimeError('Owned preparation pause expired')
            return original(*args)
        with self.operations.admit(self.context,self.artifacts,self.baselines) as operation:
            def prepare():
                try:self.prepare(operation)
                except mobile.IOSMobileOperationError:errors.append('cancelled')
            with patch.object(mobile,'_move_new_app',side_effect=paused):
                worker=threading.Thread(target=prepare);worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(self.operations.close(deadline_monotonic=time.monotonic()+.02))
                    self.assertGreater(self.runs.status(self.context.operation_id)['reservedBytes'],0)
                finally:
                    release.set();worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors,['cancelled'])
        self.assertTrue(self.operations.close())
        self.assertEqual(self.operations.status(self.context.operation_id)['roles']['candidate']['state'],'preparing')

    def test_invalid_status_identifier_is_rejected_before_directory_access(self):
        from reproof import ios_mobile_operation as mobile
        with patch.object(mobile,'_walk_directory',side_effect=AssertionError('Invalid identifier reached storage')):
            with self.assertRaises(mobile.IOSMobileOperationError):self.operations.status('../outside')

    def test_callback_keeps_producer_lock_after_admission_context_exits(self):
        from reproof import ios_mobile_operation as mobile
        entered=threading.Event();release=threading.Event();errors=[]
        original=mobile._move_new_app
        def paused(*args):
            entered.set()
            if not release.wait(3):raise RuntimeError('Owned callback pause expired')
            return original(*args)
        manager=self.operations.admit(self.context,self.artifacts,self.baselines)
        operation=manager.__enter__();exited=False;worker=None
        root=operation.archive_path('candidate').parent.parent
        def prepare():
            try:self.prepare(operation)
            except mobile.IOSMobileOperationError:errors.append('cancelled')
        try:
            with patch.object(mobile,'_move_new_app',side_effect=paused):
                worker=threading.Thread(target=prepare);worker.start()
                self.assertTrue(entered.wait(2))
                manager.__exit__(None,None,None);exited=True
                fd=os.open(root/'producer.lock',os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                finally:os.close(fd)
                release.set();worker.join(5)
                self.assertFalse(worker.is_alive());self.assertEqual(errors,['cancelled'])
            fd=os.open(root/'producer.lock',os.O_RDWR)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(fd)
        finally:
            release.set()
            if worker is not None:worker.join(5)
            if not exited:manager.__exit__(None,None,None)

    def test_actual_child_loss_retains_transfer_and_budget_for_fresh_inspection(self):
        process=multiprocessing.get_context('spawn').Process(target=crash_during_prepare,
            args=(str(self.root),self.selected.udid))
        process.start();process.join(15)
        if process.is_alive():
            process.terminate();process.join(5)
            self.fail('Owned preparation child did not stop')
        self.assertEqual(process.exitcode,73);process.close()
        from reproof.ios_mobile_operation import IOSMobileOperationStore
        fresh=IOSMobileOperationStore(self.runs,self.selected,self.operations.root,create=False)
        self.addCleanup(fresh.close)
        status=fresh.status(self.context.operation_id)
        self.assertEqual(status['roles']['candidate']['state'],'preparing')
        self.assertGreater(status['reservedBytes'],0)
        self.assertFalse(status['deviceCleanupConfirmed'])
        self.assertTrue(any(self.operations.root.rglob('transfer/app/Info.plist')))

    def test_store_reopen_normalizes_mount_bound_device_field(self):
        path=self.operations.root/'intent.json'
        stored=json.loads(path.read_bytes())
        self.assertNotIn('device',stored['operationsIdentity'])
        stored['operationsIdentity']['device']=16777234
        path.write_bytes(canonical(stored))
        from reproof.ios_mobile_operation import IOSMobileOperationStore
        fresh=IOSMobileOperationStore(self.runs,self.selected,self.operations.root)
        self.addCleanup(fresh.close)
        normalized=json.loads(path.read_bytes())
        self.assertNotIn('device',normalized['operationsIdentity'])
        self.assertEqual(contracts.digest(normalized),self.operations.configuration_digest)
        with self.operations.admit(self.context,self.artifacts,self.baselines) as operation:
            self.prepare(operation)
            self.assertEqual(self.operations.status(self.context.operation_id)['roles']['candidate']['state'],'prepared')

    def test_durable_identity_tolerates_only_the_device_field(self):
        path=self.operations.root/'intent.json'
        stored=json.loads(path.read_bytes())
        stored['operationsIdentity']['device']=16777234
        stored['operationsIdentity']['inode']=stored['operationsIdentity']['inode']+1
        path.write_bytes(canonical(stored))
        from reproof.ios_mobile_operation import IOSMobileOperationError,IOSMobileOperationStore
        with self.assertRaises(IOSMobileOperationError):
            IOSMobileOperationStore(self.runs,self.selected,self.operations.root,create=False)

    def test_tampered_configuration_beside_device_field_is_rejected(self):
        path=self.operations.root/'intent.json'
        stored=json.loads(path.read_bytes())
        stored['operationsIdentity']['device']=16777234
        stored['environmentDigest']='0'*64
        path.write_bytes(canonical(stored))
        from reproof.ios_mobile_operation import IOSMobileOperationError,IOSMobileOperationStore
        with self.assertRaises(IOSMobileOperationError):
            IOSMobileOperationStore(self.runs,self.selected,self.operations.root,create=False)

    def test_replaced_operations_directory_is_rejected(self):
        operations=self.operations.root/'operations'
        moved=self.operations.root/'operations-moved'
        os.rename(operations,moved);os.mkdir(operations,0o700)
        from reproof.ios_mobile_operation import IOSMobileOperationError,IOSMobileOperationStore
        try:
            with self.assertRaises(IOSMobileOperationError):
                IOSMobileOperationStore(self.runs,self.selected,self.operations.root,create=False)
        finally:
            os.rmdir(operations);os.rename(moved,operations)
