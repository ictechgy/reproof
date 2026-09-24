"""Recovery APK copies retain original intent, storage bounds and native ownership."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from reproof.repair_android_operation import AndroidOperationError
from tests import test_android_recovery_discarded as support


class AndroidRecoveryMaterialsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AndroidDiscardedStageRecoveryTests.setUpClass()
        cls.addClassCleanup(support.AndroidDiscardedStageRecoveryTests.doClassCleanups)

    def setUp(self):
        self.owner=support.AndroidDiscardedStageRecoveryTests(methodName='runTest')
        self.addCleanup(self.owner.doCleanups)
        self.f=self.owner.fixture()
        self.intent_bytes=(self.f.directory/'intent.json').read_bytes()
        self.intent=json.loads(self.intent_bytes)

    @contextmanager
    def borrow(self):
        f=self.f
        with f.operations.native_recovery(f.f.operation.operation_id,f.f.operation.request_digest,
            device=f.device,snapshot=f.device.recovery_snapshot(),parent_grant=f.grant()) as recovery:
            yield recovery

    def prepare(self):
        from reproof.android_recovery_materials import prepare_recovery_materials
        with self.borrow() as recovery:
            return prepare_recovery_materials(self.f.operations,recovery,cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+10)

    def test_copies_use_new_identities_without_changing_original_intent_or_budget(self):
        record=self.prepare()
        self.assertEqual(record['state'],'prepared')
        self.assertEqual(set(record['files']),{'original.apk','helper.apk'})
        self.assertEqual((self.f.directory/'intent.json').read_bytes(),self.intent_bytes)
        for name in record['files']:
            path=self.f.f.operation.staging_root/name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),self.intent['files'][name]['digest'])
            self.assertEqual(record['files'][name]['inode'],path.stat().st_ino)
        self.assertLessEqual(sum(path.stat().st_size for path in self.f.f.operation.staging_root.iterdir()),
                            sum(row['bytes'] for row in self.intent['files'].values()))
        self.assertEqual(self.f.runs.status(self.f.f.operation.operation_id)['reservedBytes'],self.intent['reservedBytes'])
        self.assertTrue(self.f.device.requires_reconciliation)

    def test_partial_owned_copy_can_resume_with_the_same_new_inode(self):
        from reproof import android_recovery_materials
        def interrupted(source,destination,expected,check):
            os.write(destination,os.read(source,2));os.fsync(destination)
            raise OSError('owned copy interruption')
        with patch.object(android_recovery_materials,'_copy_apk',side_effect=interrupted):
            with self.assertRaises(AndroidOperationError):self.prepare()
        prior=json.loads((self.f.directory/'recovery-materials.json').read_bytes())
        identities={name:row for name,row in prior['files'].items() if row is not None}
        self.assertTrue(identities)
        current=self.prepare()
        for name,identity in identities.items():self.assertEqual(current['files'][name],identity)
        self.assertEqual(current['state'],'prepared')
        self.assertEqual((self.f.directory/'intent.json').read_bytes(),self.intent_bytes)

    def test_empty_file_before_identity_commit_is_explicitly_recovered(self):
        from reproof import android_recovery_materials
        write=android_recovery_materials._replace_at
        def interrupted(directory,name,value):
            if name=='recovery-materials.json' and value['state']=='preparing' and any(value['files'].values()):
                raise OSError('owned identity commit interruption')
            return write(directory,name,value)
        with patch.object(android_recovery_materials,'_replace_at',side_effect=interrupted):
            with self.assertRaises(AndroidOperationError):self.prepare()
        paths=list(self.f.f.operation.staging_root.iterdir())
        self.assertEqual(len(paths),1);self.assertEqual(paths[0].stat().st_size,0)
        self.assertEqual(self.prepare()['state'],'prepared')

    def test_unregistered_nonempty_file_is_not_adopted_as_a_partial_copy(self):
        from reproof import android_recovery_materials
        write=android_recovery_materials._replace_at
        def interrupted(directory,name,value):
            if name=='recovery-materials.json' and value['state']=='preparing' and any(value['files'].values()):
                raise OSError('owned identity commit interruption')
            return write(directory,name,value)
        with patch.object(android_recovery_materials,'_replace_at',side_effect=interrupted):
            with self.assertRaises(AndroidOperationError):self.prepare()
        path=next(self.f.f.operation.staging_root.iterdir())
        path.write_bytes(b'unregistered-data')
        with self.assertRaises(AndroidOperationError):self.prepare()
        self.assertEqual(path.read_bytes(),b'unregistered-data')

    def test_changed_immutable_input_is_rejected_before_new_copies(self):
        self.f.f.config.original_apk.write_bytes(b'changed-input')
        with self.assertRaises(AndroidOperationError):self.prepare()
        self.assertEqual(list(self.f.f.operation.staging_root.iterdir()),[])
        self.assertEqual((self.f.directory/'intent.json').read_bytes(),self.intent_bytes)

    def test_changed_prepared_copy_is_not_promoted_to_a_native_input(self):
        self.prepare()
        (self.f.f.operation.staging_root/'original.apk').write_bytes(b'changed-copy')
        with self.assertRaises(AndroidOperationError):self.prepare()
        self.assertTrue(self.f.device.requires_reconciliation)
        self.assertGreater(self.f.runs.status(self.f.f.operation.operation_id)['reservedBytes'],0)

    def test_recovery_descriptor_copy_cannot_prepare_materials(self):
        from reproof.android_recovery_materials import prepare_recovery_materials
        with self.borrow() as recovery:
            with self.assertRaises(AndroidOperationError):
                prepare_recovery_materials(self.f.operations,replace(recovery),cancellation=threading.Event(),
                                           deadline_monotonic=time.monotonic()+10)
        self.assertEqual(list(self.f.f.operation.staging_root.iterdir()),[])

    def test_helper_recovery_rejects_copied_descriptor_before_reading_state(self):
        from reproof.android_recovery_helper import recover_android_helper
        with self.borrow() as recovery:
            with patch.object(self.f.operations,'_state') as read_state:
                with self.assertRaises(AndroidOperationError):
                    recover_android_helper(self.f.operations,replace(recovery),cancellation=threading.Event(),
                                           deadline_monotonic=time.monotonic()+10)
                read_state.assert_not_called()

    def test_recovery_apk_changed_after_inspection_cannot_be_installed(self):
        from reproof import android_recovery
        self.prepare()
        original=android_recovery.run_native_adb
        changed=[]
        def tamper(*args,**kwargs):
            if args[4][0]=='install':
                (self.f.f.operation.staging_root/'original.apk').write_bytes(b'changed-after-inspection')
                changed.append(True)
            return original(*args,**kwargs)
        with patch.object(android_recovery,'run_native_adb',side_effect=tamper):
            with self.assertRaises(AndroidOperationError):self.owner.recover(self.f,expected_failure=True)
        self.assertTrue(changed)
        self.assertEqual(self.f.helper.server.installed_digests,[])
        self.assertGreater(self.f.runs.status(self.f.f.operation.operation_id)['reservedBytes'],0)

    def test_interrupted_recovery_copy_discard_resumes_without_more_device_effects(self):
        self.prepare()
        remove=self.f.operations._remove_staged
        def interrupted(directory,name):
            remove(directory,name);raise OSError('owned recovery-copy discard interruption')
        with patch.object(self.f.operations,'_remove_staged',side_effect=interrupted):
            with self.assertRaises(AndroidOperationError):self.owner.recover(self.f,expected_failure=True)
        self.assertEqual(self.f.device.generation,2)
        self.assertTrue(self.f.device.requires_reconciliation)
        prior=json.loads((self.f.directory/'recovery-materials.json').read_bytes())
        self.assertEqual(prior['state'],'discarding')
        requests=len(self.f.helper.server.requests)
        self.assertTrue(self.owner.recover(self.f).ownership_released)
        self.assertEqual(len(self.f.helper.server.requests),requests)
        self.assertEqual(list(self.f.f.operation.staging_root.iterdir()),[])

    def missing_until_installed(self,f,targets):
        from reproof.live.android_live import HELPER
        server=f.helper.server;original=server.shell_session
        selected={'original':(f.f.config.package,f.f.config.original_profile.data['artifact']['sha256']),
                  'helper':(HELPER,f.f.config.helper_digest)}
        def installed(connection,command):
            output=original(connection,command)
            for target in targets:
                package,digest=selected[target]
                if command==('pm path '+package).encode() and digest not in server.installed_digests:return b''
            return output
        server.shell_session=installed

    def test_missing_installed_packages_are_restored_from_bound_recovery_copies(self):
        f=self.f;server=f.helper.server
        self.missing_until_installed(f,('original','helper'))
        result=self.owner.recover(f)
        self.assertTrue(result.ownership_released and result.reservation_released)
        self.assertIn(self.intent['files']['original.apk']['digest'],server.installed_digests)
        self.assertIn(self.intent['files']['helper.apk']['digest'],server.installed_digests)
        self.assertEqual(list(f.f.operation.staging_root.iterdir()),[])
        self.assertEqual(json.loads((f.directory/'recovery-materials.json').read_bytes())['state'],'discarded')
        self.assertEqual((f.directory/'intent.json').read_bytes(),self.intent_bytes)

    def test_missing_helper_after_original_verification_uses_the_same_bound_copy_path(self):
        self.missing_until_installed(self.f,('helper',))
        self.assertTrue(self.owner.recover(self.f).ownership_released)
        self.assertIn(self.intent['files']['helper.apk']['digest'],self.f.helper.server.installed_digests)
        record=json.loads((self.f.directory/'recovery.json').read_bytes())
        self.assertEqual(record['recoveryMode'],'recovery-apks')
        self.assertEqual(record['attempt'],2)

    def test_remaining_original_files_are_reused_without_replacement(self):
        f=self.owner.fixture('partial')
        original=(f.directory/'intent.json').read_bytes()
        self.missing_until_installed(f,('original','helper'))
        self.assertTrue(self.owner.recover(f).ownership_released)
        materials=json.loads((f.directory/'recovery-materials.json').read_bytes())
        self.assertEqual(materials['files'],{})
        self.assertEqual(materials['state'],'discarded')
        self.assertEqual((f.directory/'intent.json').read_bytes(),original)
