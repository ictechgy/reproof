"""Proposal-only source identities and finite edits; no compiler or device use."""
import copy
from dataclasses import replace, FrozenInstanceError
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from reproof import source_snapshot as snapshots
from reproof.core import digest
from reproof.execution.artifacts import BlobSet
from reproof.project_repair import RepairError
from reproof.snapshot_repair_source import SnapshotRepairSource,legacy_source_digest,freeze_edits
from tests.test_project_repair import source_fixture,PRODUCT,BEFORE,EDIT


class SnapshotRepairSourceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.project,self.blobs,self.artifacts=source_fixture()
        self.project['applications'][0]['platform']='android'
        self.blobs.write_new(self.root/'source')
        self.apk=self.root/'original.apk';self.apk.write_bytes(self.artifacts.entries[0][1])
        self.reference=self.capture(self.root/'snapshot')

    def capture(self,output,*,names=None,budget=None):
        result=snapshots.capture(self.root/'source',names or [name for name,_ in self.blobs.entries],output,budgets=budget)
        return {'schemaVersion':1,'kind':'reproof-local-source-snapshot',
                **{key:result[key] for key in ('digest','files','bytes')}}

    def selected(self,*,project=None,reference=None,root=None,protected=None,budget=None):
        return SnapshotRepairSource(project or self.project,root or self.root/'snapshot',
            snapshot_reference=reference or self.reference,protected_paths=['tests/CheckoutTests.swift'] if protected is None else protected,
            original_artifact=self.apk,budgets=budget)

    def test_metadata_digest_matches_blobset_and_is_distinct_from_snapshot(self):
        manifest=snapshots.inspect_snapshot(self.root/'snapshot')
        self.assertEqual(legacy_source_digest(manifest),self.blobs.digest)
        self.assertNotEqual(self.reference['digest'],self.blobs.digest)
        selected=self.selected();frozen=selected.freeze('original')
        self.assertEqual(frozen.digest,self.blobs.digest);self.assertEqual(frozen.reference,self.reference)
        self.assertEqual(frozen.artifact_digest,hashlib.sha256(self.apk.read_bytes()).hexdigest())
        self.assertEqual(selected.snapshot_reservation_bytes,selected.budgets.max_bytes+selected.budgets.max_manifest_bytes)
        self.assertFalse(hasattr(frozen,'entries'))

    def test_multiple_builds_share_one_metadata_tuple(self):
        project=copy.deepcopy(self.project)
        second=copy.deepcopy(project['builds'][0]);second['id']='second';project['builds'].append(second)
        selected=self.selected(project=project)
        original=selected.freeze('original');other=selected.freeze('second')
        self.assertIs(original._files,other._files)
        self.assertIsNot(original,other)
        self.assertIs(selected.require_original(other,'second'),other)

    def test_constructor_reads_neither_snapshot_nor_apk_and_copies_policy(self):
        project=copy.deepcopy(self.project);reference=copy.deepcopy(self.reference)
        with patch('reproof.snapshot_repair_source.open_regular',side_effect=AssertionError('No read')), \
             patch('reproof.snapshot_repair_source.snapshots.inspect_snapshot',side_effect=AssertionError('No read')):
            selected=self.selected(project=project,reference=reference)
        project['builds'][0]['sourceDigest']='f'*64;reference['digest']='f'*64
        self.assertEqual(selected.freeze('original').digest,self.blobs.digest)
        modified=selected.snapshot_reference;modified['digest']='a'*64
        self.assertEqual(selected.snapshot_reference,self.reference)
        with self.assertRaises(AttributeError):selected.original_artifact=Path('/elsewhere.apk')

    def test_single_apk_bytes_have_no_format_or_execution_claim(self):
        selected=self.selected();frozen=selected.freeze('original')
        self.assertEqual(frozen.artifact_bytes,len(b'original build artifact'))
        self.assertFalse(hasattr(frozen,'verified'));self.assertFalse(hasattr(frozen,'buildCompleted'))

    def test_android_platform_build_and_provenance_required(self):
        project=copy.deepcopy(self.project);project['applications'][0]['platform']='ios'
        with self.assertRaises(RepairError):self.selected(project=project).freeze('original')
        project=copy.deepcopy(self.project);project['builds'][0]['sourceDigest']='f'*64
        with self.assertRaises(RepairError):self.selected(project=project).freeze('original')
        with self.assertRaises(RepairError):self.selected().freeze('missing')

    def test_process_owned_frozen_and_candidate_objects_cannot_be_copied_or_transferred(self):
        selected=self.selected();frozen=selected.freeze('original')
        with self.assertRaises(RepairError):selected.require_original(replace(frozen),'original')
        with self.assertRaises(RepairError):self.selected().require_original(frozen,'original')
        candidate=selected.apply(frozen,[EDIT],self.root/'candidate')
        with self.assertRaises(RepairError):selected.require_candidate(replace(candidate))
        with self.assertRaises(RepairError):self.selected().require_candidate(candidate)
        with self.assertRaises(FrozenInstanceError):candidate.digest='f'*64
        self.assertIs(selected.require_candidate(candidate),candidate)

    def test_only_editable_bytes_are_exposed_and_every_other_input_preserved(self):
        selected=self.selected();frozen=selected.freeze('original')
        self.assertEqual(selected.editable_entries(frozen),((PRODUCT,BEFORE),))
        candidate=selected.apply(frozen,[EDIT],self.root/'candidate')
        self.assertEqual(candidate.changed_paths,(PRODUCT,))
        self.assertNotEqual(candidate.digest,frozen.digest)
        expected=self.blobs.entries
        for name,raw in expected:
            actual=(candidate.path/'data'/name).read_bytes()
            self.assertEqual(actual,raw.replace(b'if !ready',b'if ready') if name==PRODUCT else raw)
        self.assertEqual((self.root/'snapshot/data'/PRODUCT).read_bytes(),BEFORE)
        self.assertEqual(candidate.digest,BlobSet(tuple((name,(candidate.path/'data'/name).read_bytes()) for name,_ in expected)).digest)

    def test_pure_edit_copy_has_no_shared_dictionary_and_static_unicode_errors(self):
        original=[dict(EDIT)];fixed=freeze_edits(original)
        original[0]['new']='changed';original.append(dict(EDIT))
        self.assertEqual(fixed,[EDIT]);self.assertIsNot(fixed[0],original[0])
        with self.assertRaises(RepairError):freeze_edits([dict(EDIT,path='bad\ud800')])
        with self.assertRaises(RepairError):freeze_edits([dict(EDIT,new='bad\ud800')])

    def test_edits_are_frozen_before_source_read_callbacks(self):
        selected=self.selected();frozen=selected.freeze('original');edits=[dict(EDIT)]
        original=SnapshotRepairSource.editable_entries
        def mutate(source,*args,**kwargs):
            edits[0]['new']='unvalidated replacement';edits.append(dict(EDIT,path='undeclared.kt'))
            return original(source,*args,**kwargs)
        with patch.object(SnapshotRepairSource,'editable_entries',mutate):
            candidate=selected.apply(frozen,edits,self.root/'candidate')
        self.assertEqual((candidate.path/'data'/PRODUCT).read_bytes(),BEFORE.replace(EDIT['old'].encode(),EDIT['new'].encode()))

    def test_patch_invalid_anchor_overlap_noop_and_protected_paths_rejected(self):
        selected=self.selected();frozen=selected.freeze('original')
        changes=[[],[dict(EDIT,new=EDIT['old'])],[dict(EDIT,old='return')],
                 [EDIT,dict(EDIT,old='if !ready {',new='if ready {')],
                 [dict(EDIT,path='tests/CheckoutTests.swift',old='protected',new='changed')],
                 [dict(EDIT,path='../escape.swift')],[dict(EDIT,new='\0')]]
        for index,edits in enumerate(changes):
            with self.subTest(edits=edits),self.assertRaises(RepairError):selected.apply(frozen,edits,self.root/f'invalid{index}')
            self.assertFalse((self.root/f'invalid{index}').exists())

    def test_recipe_membership_and_explicit_protection_are_required(self):
        project=copy.deepcopy(self.project);project['editablePaths'].append('tests/CheckoutTests.swift')
        with self.assertRaises(RepairError):self.selected(project=project)
        with self.assertRaises(RepairError):self.selected(protected=['missing.json']).freeze('original')
        names=[name for name,_ in self.blobs.entries if name!='checks/ui.json']
        reference=self.capture(self.root/'missing-recipe',names=names)
        with self.assertRaises(RepairError):self.selected(root=self.root/'missing-recipe',reference=reference).freeze('original')

    def test_source_and_original_artifact_changes_abort_before_derived_output(self):
        selected=self.selected();frozen=selected.freeze('original')
        self.apk.write_bytes(b'changed original')
        with self.assertRaises(RepairError):selected.apply(frozen,[EDIT],self.root/'artifact-changed')
        self.assertFalse((self.root/'artifact-changed').exists())
        self.apk.write_bytes(self.artifacts.entries[0][1])
        (self.root/'snapshot/data/tests/CheckoutTests.swift').write_bytes(b'changed protected')
        with self.assertRaises(snapshots.SnapshotError):selected.apply(frozen,[EDIT],self.root/'source-changed')
        self.assertFalse((self.root/'source-changed').exists())

    def test_candidate_tampering_is_detected_without_build(self):
        selected=self.selected();frozen=selected.freeze('original');candidate=selected.apply(frozen,[EDIT],self.root/'candidate')
        (candidate.path/'data'/PRODUCT).write_bytes(b'changed')
        with self.assertRaises(snapshots.SnapshotError):selected.require_candidate(candidate)

    def test_apk_symlink_hardlink_and_oversize_are_rejected(self):
        self.apk.unlink();self.apk.symlink_to(self.root/'source'/PRODUCT)
        with self.assertRaises(RepairError):self.selected().freeze('original')
        self.apk.unlink();self.apk.write_bytes(self.artifacts.entries[0][1]);os.link(self.apk,self.root/'alias.apk')
        with self.assertRaises(RepairError):self.selected().freeze('original')
        (self.root/'alias.apk').unlink()
        with self.apk.open('wb') as stream:stream.truncate(150*1024*1024+1)
        with self.assertRaises(RepairError):self.selected().freeze('original')

    def test_editable_byte_budget_is_checked_from_metadata_before_text_read(self):
        (self.root/'source'/PRODUCT).write_bytes(b'x'*(1024*1024+1))
        reference=self.capture(self.root/'large-editable')
        with patch('reproof.snapshot_repair_source.snapshots.read_snapshot_files',side_effect=AssertionError('No text read')):
            with self.assertRaises(RepairError):self.selected(root=self.root/'large-editable',reference=reference).freeze('original')

    def test_editable_count_and_aggregate_bytes_are_bounded(self):
        project=copy.deepcopy(self.project)
        project['editablePaths']=[f'app/Source{index}.kt' for index in range(129)]
        with self.assertRaises(RepairError):self.selected(project=project)
        names=[f'app/Source{index}.kt' for index in range(9)]
        for name in names:
            path=self.root/'source'/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b'x'*(1024*1024))
        project['editablePaths']=names
        reference=self.capture(self.root/'aggregate',names=[name for name,_ in self.blobs.entries]+names)
        with patch('reproof.snapshot_repair_source.snapshots.read_snapshot_files',side_effect=AssertionError('No text read')):
            with self.assertRaises(RepairError) as error:
                self.selected(project=project,root=self.root/'aggregate',reference=reference).freeze('original')
        self.assertEqual(error.exception.code,'patch_limit')

    def test_changed_path_count_rejected_before_editable_read(self):
        names=[f'app/Source{index}.kt' for index in range(17)]
        for name in names:
            path=self.root/'source'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'before')
        project=copy.deepcopy(self.project);project['editablePaths']=names
        reference=self.capture(self.root/'many-editable',names=[name for name,_ in self.blobs.entries]+names)
        manifest=snapshots.inspect_snapshot(self.root/'many-editable')
        project['builds'][0]['sourceDigest']=legacy_source_digest(manifest)
        selected=self.selected(project=project,root=self.root/'many-editable',reference=reference)
        frozen=selected.freeze('original')
        with patch.object(SnapshotRepairSource,'editable_entries',side_effect=AssertionError('No source read')):
            with self.assertRaises(RepairError) as error:
                selected.apply(frozen,[{'path':name,'old':'before','new':'after'} for name in names],self.root/'too-many')
        self.assertEqual(error.exception.code,'patch_limit');self.assertFalse((self.root/'too-many').exists())

    def test_parent_fence_prevents_writing_replaced_journal_directory(self):
        selected=self.selected();frozen=selected.freeze('original');parent=self.root/'journal';parent.mkdir()
        fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
        try:
            info=os.fstat(fd);parent.rename(self.root/'owned-journal');parent.mkdir();(parent/'foreign').write_bytes(b'preserve')
            with self.assertRaises(snapshots.SnapshotError):
                selected.apply(frozen,[EDIT],parent/'candidate',output_parent_identity=(info.st_dev,info.st_ino))
            self.assertEqual([item.name for item in parent.iterdir()],['foreign'])
        finally:os.close(fd)

    def test_oversized_patch_rejected_before_editable_read(self):
        selected=self.selected();frozen=selected.freeze('original')
        edits=[dict(EDIT,old='x'*10000,new='y'*10000) for _ in range(20)]
        with patch.object(SnapshotRepairSource,'editable_entries',side_effect=AssertionError('No source read')):
            with self.assertRaises(RepairError) as error:selected.apply(frozen,edits,self.root/'oversized-patch')
        self.assertEqual(error.exception.code,'patch_limit');self.assertFalse((self.root/'oversized-patch').exists())

    def test_cancel_before_apply_creates_no_output(self):
        selected=self.selected();frozen=selected.freeze('original');cancel=threading.Event();cancel.set()
        with self.assertRaises(snapshots.SnapshotError):selected.apply(frozen,[EDIT],self.root/'cancelled',cancel=cancel)
        self.assertFalse((self.root/'cancelled').exists())

    def test_late_original_change_retains_derived_output_as_incomplete(self):
        selected=self.selected();frozen=selected.freeze('original');actual=snapshots.derive_snapshot
        def derive(*args,**kwargs):
            result=actual(*args,**kwargs);self.apk.write_bytes(b'changed after derivation');return result
        with patch('reproof.snapshot_repair_source.snapshots.derive_snapshot',side_effect=derive),self.assertRaises(snapshots.SnapshotError) as error:
            selected.apply(frozen,[EDIT],self.root/'candidate')
        self.assertTrue(error.exception.cleanup_incomplete);self.assertTrue((self.root/'candidate').is_dir())


if __name__=='__main__':unittest.main()
