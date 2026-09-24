"""IPA extraction can remain inside a persistent operation's owned namespace."""
import hashlib
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof.ios_artifact_transfer import _opened_ipa_contents, MAX_APP_ENTRIES, MAX_EXPANDED_APP_BYTES
from tests import test_ios_artifact_transfer as fixtures


class IOSOwnedExtractionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.IOSArtifactTransferTests(methodName='runTest'); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.app = self.fixture.make_flat_app()
        self.archive = self.fixture.make_ipa(self.app, self.root/'input.ipa')
        self.workspace = self.root/'transfer'; self.workspace.mkdir(mode=0o700)

    def extract(self):
        return _opened_ipa_contents(self.archive, max_bytes=MAX_EXPANDED_APP_BYTES,
            max_entries=MAX_APP_ENTRIES, _workspace=self.workspace)

    def test_owned_snapshot_and_extraction_remain_for_the_journal_to_clean(self):
        with self.extract() as (app, size, digest):
            self.assertEqual(app, self.workspace/'app')
            self.assertEqual(size, self.archive.stat().st_size)
            self.assertEqual(digest, hashlib.sha256(self.archive.read_bytes()).hexdigest())
            self.assertTrue((app/'Info.plist').is_file())
        self.assertEqual((self.workspace/'source.ipa').read_bytes(), self.archive.read_bytes())
        self.assertEqual({path.name for path in self.workspace.iterdir()}, {'source.ipa', 'app'})

    def test_existing_contents_and_workspace_alias_fail_without_overwrite(self):
        marker = self.workspace/'marker'; marker.write_bytes(b'preserve')
        with self.assertRaises(ContractError):
            with self.extract(): pass
        self.assertEqual(list(self.workspace.iterdir()), [marker])
        marker.unlink()
        alias = self.root/'alias'; alias.symlink_to(self.workspace, target_is_directory=True)
        self.workspace = alias
        with self.assertRaises(ContractError):
            with self.extract(): pass
        self.assertEqual(list(alias.iterdir()), [])

    def test_actual_parent_exit_leaves_all_temporary_data_under_the_owned_workspace(self):
        script = '''
import os,sys
from pathlib import Path
from reproof.ios_artifact_transfer import _opened_ipa_contents,MAX_APP_ENTRIES,MAX_EXPANDED_APP_BYTES
with _opened_ipa_contents(Path(sys.argv[1]),max_bytes=MAX_EXPANDED_APP_BYTES,
                         max_entries=MAX_APP_ENTRIES,_workspace=Path(sys.argv[2])):
    os._exit(73)
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.archive), str(self.workspace)],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 73)
        self.assertTrue((self.workspace/'source.ipa').is_file())
        self.assertTrue((self.workspace/'app/Info.plist').is_file())

    def test_workspace_replacement_does_not_receive_snapshot_or_app_writes(self):
        from reproof import ios_artifact_transfer as module
        original = module._snapshot_ipa
        moved = self.root/'original-transfer'
        replacement = self.root/'replacement'; replacement.mkdir(mode=0o700)
        marker = replacement/'preserve'; marker.write_bytes(b'unrelated owned content')
        def swapped(*args, **kwargs):
            self.workspace.rename(moved)
            replacement.rename(self.workspace)
            return original(*args, **kwargs)
        with patch.object(module, '_snapshot_ipa', side_effect=swapped), self.assertRaises(ContractError):
            with self.extract(): pass
        self.assertEqual({path.name for path in self.workspace.iterdir()}, {'preserve'})
        self.assertEqual((self.workspace/'preserve').read_bytes(), b'unrelated owned content')
