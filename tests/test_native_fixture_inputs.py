"""Portable controls for explicit native-fixture selection, not native evidence."""
import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.native_fixture_inputs import native_fixture


class NativeFixtureInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_missing_selection_is_precise_unqualified_skip(self):
        for name in ('REPROOF_TEST_ANDROID_APK', 'REPROOF_TEST_ANDROID_UNSIGNED_APK',
                     'REPROOF_TEST_IOS_APP', 'REPROOF_TEST_IOS_DEVICE_RUNNER',
                     'REPROOF_TEST_IOS_DEVICE_RUNNER_SECOND'):
            with self.subTest(name=name), self.assertRaises(unittest.SkipTest) as caught:
                native_fixture(name, environment={})
            self.assertIn(name, str(caught.exception))
            self.assertIn('qualification not run', str(caught.exception))

    def test_explicit_regular_apk_selection_does_not_inspect_contents(self):
        apk = self.root / 'Synthetic.apk'
        apk.write_bytes(b'synthetic selector test; not a real APK')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('selector read contents')):
            self.assertEqual(native_fixture('REPROOF_TEST_ANDROID_APK',
                environment={'REPROOF_TEST_ANDROID_APK': str(apk)}), apk)
        self.assertEqual(apk.read_bytes(), b'synthetic selector test; not a real APK')

    def test_explicit_app_directory_selection_is_not_signature_evidence(self):
        app = self.root / 'Synthetic.app'; app.mkdir()
        self.assertEqual(native_fixture('REPROOF_TEST_IOS_APP',
            environment={'REPROOF_TEST_IOS_APP': str(app)}), app)
        self.assertEqual(list(app.iterdir()), [])

    def test_invalid_selected_paths_fail_instead_of_skip_and_redact(self):
        wrong = self.root / 'private-canary.txt'; wrong.write_text('private')
        folder = self.root / 'Folder.apk'; folder.mkdir()
        empty = self.root / 'Empty.apk'; empty.touch()
        for value in ('', 'relative.apk', str(self.root / 'Missing.apk'), str(wrong),
                      str(folder), str(empty), str(self.root / '..' / 'escape.apk'),
                      str(self.root / 'line\nbreak.apk'), None, 123):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                native_fixture('REPROOF_TEST_ANDROID_APK',
                    environment={'REPROOF_TEST_ANDROID_APK': value})
            self.assertNotIn(str(self.root), str(caught.exception))
            self.assertNotIn('private-canary', str(caught.exception))

    def test_selected_symlink_and_symlink_ancestor_fail(self):
        actual = self.root / 'actual'; actual.mkdir()
        apk = actual / 'Synthetic.apk'; apk.write_bytes(b'test')
        linked = self.root / 'Linked.apk'; linked.symlink_to(apk)
        directory = self.root / 'link'; directory.symlink_to(actual, target_is_directory=True)
        for path in (linked, directory / apk.name):
            with self.subTest(path=path), self.assertRaises(ValueError):
                native_fixture('REPROOF_TEST_ANDROID_APK',
                    environment={'REPROOF_TEST_ANDROID_APK': str(path)})

    def test_oversized_fixture_is_rejected_without_read(self):
        apk = self.root / 'Oversized.apk'
        with apk.open('wb') as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('read oversized')):
            with self.assertRaises(ValueError):
                native_fixture('REPROOF_TEST_ANDROID_APK',
                    environment={'REPROOF_TEST_ANDROID_APK': str(apk)})

    def test_unknown_selector_is_not_arbitrary_environment_access(self):
        with self.assertRaises(ValueError):
            native_fixture('HOME', environment={'HOME': str(self.root)})

    def test_native_consumers_no_longer_depend_on_ignored_input_paths(self):
        root = Path(__file__).parent
        modules = ('test_android_native_inspector', 'test_android_signing_owner',
                   'test_repair_android_signing', 'test_ios_code_signature',
                   'test_ios_native_signing_owner', 'test_ios_native_pipeline',
                   'test_ios_xctest_dsym')
        for module in modules:
            source = (root / (module + '.py')).read_text()
            with self.subTest(module=module):
                ast.parse(source)
                self.assertNotIn('artifacts/product-delivery', source)
                self.assertIn('native_fixture(', source)

    def test_native_release_gate_still_rejects_missing_evidence(self):
        # Existing executable release-harness tests prove skip -> failed gate;
        # this wiring check prevents opting these native suites out of gates.
        import json
        inventory = json.loads((Path(__file__).parent / 'release_inventory.json').read_text())
        raw = json.dumps(inventory)
        self.assertIn('tests.test_android_native_inspector', raw)
        self.assertIn('tests.test_ios_native_pipeline', raw)
        self.assertIn('tests.test_repair_android_signing', raw)


if __name__ == '__main__':
    unittest.main()
