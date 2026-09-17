"""A native consumer must receive the exact selected app in a new directory."""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from reproloop.core import ContractError
from reproloop.ios_artifact_transfer import parse_ios_artifact
from tests import test_ios_artifact_transfer as support


class IOSArtifactStagingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.IOSArtifactTransferTests(methodName='runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.app = self.fixture.make_app()
        self.selected = parse_ios_artifact(self.app)

    def stage(self, selected=None, output=None):
        from reproloop.ios_artifact_staging import stage_ios_artifact
        return stage_ios_artifact(self.selected if selected is None else selected,
                                  self.root / 'Staged.app' if output is None else output)

    def test_app_bytes_profiles_modes_and_framework_links_are_preserved(self):
        staged = self.stage()
        self.assertEqual(staged.app_digest, self.selected.app_digest)
        self.assertEqual(staged.format, 'app')
        self.assertEqual(staged._source, self.root / 'Staged.app')
        copied = self.root / 'Staged.app'
        self.assertEqual((copied / 'embedded.mobileprovision').read_bytes(),
                         (self.app / 'embedded.mobileprovision').read_bytes())
        self.assertTrue((copied / 'Frameworks/Foo.framework/Foo').is_symlink())
        self.assertEqual((copied / 'Inventory').stat().st_mode & 0o777, 0o700)
        self.assertEqual((copied / 'Info.plist').stat().st_mode & 0o777, 0o600)
        self.assertEqual(parse_ios_artifact(self.app).app_digest, self.selected.app_digest)

    def test_ipa_stages_the_same_app_after_container_validation(self):
        # Produce a flat, valid framework for a symlink-free IPA.
        framework = self.app / 'Frameworks/Foo.framework'
        (framework / 'Foo').unlink()
        (framework / 'Versions/Current').unlink()
        (framework / 'Versions/A/Foo').rename(framework / 'Foo')
        (framework / 'Versions/A').rmdir(); (framework / 'Versions').rmdir()
        selected_app = parse_ios_artifact(self.app)
        archive = self.root / 'owned.ipa'
        with zipfile.ZipFile(archive, 'w') as output:
            for item in sorted(self.app.rglob('*')):
                name = 'Payload/Inventory.app/' + item.relative_to(self.app).as_posix()
                if item.is_dir():
                    support._zip_directory(output, name)
                else:
                    support._zip_file(output, name, item.read_bytes(), executable=bool(item.stat().st_mode & 0o111))
        selected = parse_ios_artifact(archive)
        staged = self.stage(selected)
        self.assertEqual(staged.app_digest, selected.app_digest)
        self.assertEqual(staged.app_digest, selected_app.app_digest)
        self.assertNotEqual(staged.container_digest, selected.container_digest)
        self.assertEqual(parse_ios_artifact(archive).container_digest, selected.container_digest)

    def test_changed_source_or_copied_capability_cannot_create_output(self):
        with self.assertRaises(ContractError):
            self.stage(replace(self.selected))
        self.assertFalse((self.root / 'Staged.app').exists())
        (self.app / 'Info.plist').write_bytes(b'changed')
        with self.assertRaises(ContractError):
            self.stage()
        self.assertFalse((self.root / 'Staged.app').exists())

    def test_existing_output_and_parent_alias_are_rejected_before_writes(self):
        existing = self.root / 'Staged.app'; existing.mkdir()
        marker = existing / 'marker'; marker.write_bytes(b'preserve')
        with self.assertRaises(ContractError):
            self.stage()
        self.assertEqual(marker.read_bytes(), b'preserve')
        actual = self.root / 'actual'; actual.mkdir()
        alias = self.root / 'alias'; alias.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(ContractError):
            self.stage(output=alias / 'Other.app')
        self.assertEqual(list(actual.iterdir()), [])

    def test_source_mutation_during_copy_never_publishes_a_valid_capability(self):
        from reproloop import ios_artifact_staging as module
        actual = module._copy_file
        def changed(tree, entry, *args, **kwargs):
            if entry.path == 'Inventory':
                (self.app / entry.path).write_bytes(b'changed during copy')
            return actual(tree, entry, *args, **kwargs)
        with patch.object(module, '_copy_file', side_effect=changed):
            with self.assertRaises(ContractError):
                self.stage()
        # A partial directory is retained as failure evidence; it is not
        # mistaken for a valid app or a native cleanup receipt.
        self.assertTrue((self.root / 'Staged.app').is_dir())


if __name__ == '__main__':
    unittest.main()
