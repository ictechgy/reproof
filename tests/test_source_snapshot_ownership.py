"""Deterministic ownership regressions, independent of filesystem inode reuse."""
import os
import json
import subprocess
import sys
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import reproof.source_snapshot as implementation
from reproof.source_snapshot import SnapshotBudget,SnapshotError,capture


class SourceSnapshotOwnershipTests(unittest.TestCase):
    def test_recycled_file_identity_never_authorizes_foreign_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'source';source.mkdir()
            (source/'one.txt').write_bytes(b'original synthetic bytes')
            destination=root/'snapshot';changed=[]
            original_identity=implementation._identity
            original_manifest=implementation._manifest
            def recycled_identity(info):
                # Model the exact old-ledger hazard on every filesystem. The
                # real retained inode's link count still becomes zero on unlink.
                if stat.S_ISREG(info.st_mode):return info.st_dev,777
                return original_identity(info)
            def replace(*args,**kwargs):
                if not changed:
                    changed.append(True)
                    path=destination/'data/one.txt'
                    path.unlink();path.write_bytes(b'foreign replacement must survive')
                return original_manifest(*args,**kwargs)
            with patch.object(implementation,'_identity',side_effect=recycled_identity), \
                    patch.object(implementation,'_manifest',side_effect=replace), \
                    self.assertRaises(SnapshotError) as error:
                capture(source,['one.txt'],destination)
            self.assertEqual(error.exception.code,'snapshot_cleanup_incomplete')
            self.assertTrue(error.exception.cleanup_incomplete)
            self.assertEqual((destination/'data/one.txt').read_bytes(),b'foreign replacement must survive')

    def test_same_inode_changed_file_is_preserved_conservatively(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'source';source.mkdir()
            (source/'one.txt').write_bytes(b'original')
            destination=root/'snapshot';changed=[]
            original=implementation._manifest
            def modify(*args,**kwargs):
                if not changed:
                    changed.append(True)
                    path=destination/'data/one.txt';before=path.stat().st_ino
                    path.write_bytes(b'changed in place')
                    self.assertEqual(path.stat().st_ino,before)
                return original(*args,**kwargs)
            with patch.object(implementation,'_manifest',side_effect=modify), \
                    self.assertRaises(SnapshotError) as error:
                capture(source,['one.txt'],destination)
            self.assertEqual(error.exception.code,'snapshot_cleanup_incomplete')
            self.assertEqual((destination/'data/one.txt').read_bytes(),b'changed in place')

    def test_evicted_file_is_not_reacquired_as_cleanup_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'source';source.mkdir()
            paths=[f'f{index:03d}.txt' for index in range(80)]
            for name in paths:(source/name).write_bytes(b'original')
            destination=root/'snapshot';changed=[];original=implementation._manifest
            def replace(*args,**kwargs):
                if not changed:
                    changed.append(True)
                    path=destination/'data'/paths[0]
                    path.unlink();path.write_bytes(b'foreign old unpinned entry')
                return original(*args,**kwargs)
            original_identity=implementation._identity
            def recycled_identity(info):
                return (info.st_dev,777) if stat.S_ISREG(info.st_mode) else original_identity(info)
            with patch.object(implementation,'_manifest',side_effect=replace), \
                    patch.object(implementation,'_identity',side_effect=recycled_identity), \
                    self.assertRaises(SnapshotError) as error:
                capture(source,paths,destination,budgets=SnapshotBudget(max_files=80))
            self.assertEqual(error.exception.code,'snapshot_cleanup_incomplete')
            self.assertEqual((destination/'data'/paths[0]).read_bytes(),b'foreign old unpinned entry')

    def test_completed_directory_prefix_is_never_reopened_for_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();destination=root/'output'
            output=implementation._Output(destination)
            try:
                with output.create('a/one.txt') as stream:stream.write(b'one')
                with output.create('b/two.txt') as stream:stream.write(b'two')
                original_identity=implementation._identity
                old_identity=original_identity((destination/'a').stat())
                (destination/'a').rename(root/'moved')
                (destination/'a').mkdir();(destination/'a/foreign.txt').write_bytes(b'foreign')
                replacement_identity=original_identity((destination/'a').stat())
                def recycled_directory(info):
                    value=original_identity(info)
                    return old_identity if value==replacement_identity else value
                with patch.object(implementation,'_identity',side_effect=recycled_directory):
                    with self.assertRaises(SnapshotError):
                        with output.create('a/three.txt') as stream:stream.write(b'must not appear')
                    self.assertFalse((destination/'a/three.txt').exists())
                    self.assertFalse(output.rollback())
                self.assertEqual((destination/'a/foreign.txt').read_bytes(),b'foreign')
                self.assertEqual((root/'moved/one.txt').read_bytes(),b'one')
            finally:output.close()


    def test_isolated_descriptor_budget_success_and_safe_failure(self):
        # Only each newly spawned synthetic child lowers its own soft limit.
        # The parent and operating-system configuration are never changed.
        program = r'''import json,os,resource,sys,tempfile
from pathlib import Path
from reproof.source_snapshot import capture,verify,materialize,SnapshotError
limit=int(sys.argv[1])
soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
if soft != resource.RLIM_INFINITY and soft < limit:
    print(json.dumps({'unsupported':'preexisting descriptor limit below test budget'}));sys.exit(77)
fdroot=Path('/proc/self/fd') if Path('/proc/self/fd').is_dir() else Path('/dev/fd')
if not fdroot.is_dir():
    print(json.dumps({'unsupported':'descriptor enumeration unavailable'}));sys.exit(77)
resource.setrlimit(resource.RLIMIT_NOFILE,(limit,hard))
before=len(os.listdir(fdroot))
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory).resolve();source=root/'source';source.mkdir()
    prefix='/'.join('d'+str(index) for index in range(31))
    names=[prefix+'/f'+str(index)+'.txt' for index in range(80)]
    for name in names:
        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic')
    snapshot=root/'snapshot';output=root/'output'
    first=capture(source,names,snapshot);verify(snapshot)
    try:
        last=materialize(snapshot,output)
        assert first['digest']==last['digest']
        outcome='complete';failure=None
    except SnapshotError as error:
        outcome='rejected';failure=error.code
    after=len(os.listdir(fdroot))
    assert before==after
    print(json.dumps({'limit':limit,'fdBefore':before,'fdAfter':after,'outcome':outcome,'failure':failure}))
'''
        try:import resource
        except ImportError:self.skipTest('POSIX descriptor limits unavailable')
        root=Path(__file__).resolve().parents[1]
        for limit in (256,96):
            with self.subTest(limit=limit):
                result=subprocess.run([sys.executable,'-c',program,str(limit)],cwd=root,
                                      capture_output=True,text=True,timeout=60)
                if result.returncode==77:self.skipTest(json.loads(result.stdout)['unsupported'])
                self.assertEqual(result.returncode,0,result.stderr[-2000:])
                report=json.loads(result.stdout)
                self.assertEqual(report['fdBefore'],report['fdAfter'])
                self.assertEqual(report['outcome'],'complete' if limit==256 else 'rejected')
                if limit==96:self.assertEqual(report['failure'],'snapshot_cleanup_incomplete')
                print('SOURCE_SNAPSHOT_FD_BUDGET '+json.dumps(report,sort_keys=True),flush=True)


if __name__=='__main__':unittest.main()
