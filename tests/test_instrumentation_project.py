from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

from reproloop.android_profile import validate_app_profile
from reproloop.core import ContractError
from reproloop.storage import read_json
from tests.test_android_profile import profile_document


class InstrumentationProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.activity_path = 'app/src/main/java/example/MainActivity.kt'
        self.activity = self.source / self.activity_path
        self.activity.parent.mkdir(parents=True)
        self.activity.write_text('package io.reproloop.inventory\nclass MainActivity\n')
        self.product = self.source / 'app/src/main/java/example/Stock.kt'
        self.product.write_text('fun unitsPerItem() = 2\n')
        (self.source / 'app/build.gradle.kts').write_text('// public build input\n')
        self.profile = validate_app_profile(profile_document())
        self.original = self.activity.read_text()
        self.analyzer = Mock(return_value={'schemaVersion': 1, 'activityPath': self.activity_path,
            'files': {self.activity_path: self.original + '// injected fixture hook\n'},
            'sites': [{'id': 's123abc', 'path': self.activity_path, 'line': 2, 'target': 'commit', 'kind': 'tap'}]})

    def tearDown(self):
        self.temp.cleanup()

    def test_stages_debug_runtime_release_stub_profile_and_reviewable_patch(self):
        from reproloop.instrumentation import instrument_project, validate_instrumented_source
        output = self.root / 'instrumented'
        result = instrument_project(self.source, self.profile, output, analyzer=self.analyzer)
        self.assertEqual(result['status'], 'instrumented')
        self.assertEqual(self.activity.read_text(), self.original)
        staged = output / 'source'
        self.assertEqual((staged / 'app/src/main/java/example/Stock.kt').read_text(), self.product.read_text())
        self.assertTrue((staged / 'app/src/debug/java/io/reproloop/sdk/ReproRecorder.kt').is_file())
        self.assertFalse((staged / 'app/src/release/java/io/reproloop/sdk/ReproRecorder.kt').exists())
        self.assertTrue((staged / 'app/src/release/java/io/reproloop/autotrace/ReproAuto.kt').is_file())
        self.assertIn('android.permission.DUMP', (staged / 'app/src/debug/AndroidManifest.xml').read_text())
        profile = validate_app_profile(read_json(output / 'app-profile.json'))
        self.assertEqual(profile.data['captureMode'], 'debug_receiver')
        self.assertIsNone(profile.data['targets']['report'])
        self.assertEqual(validate_instrumented_source(staged, profile)['appProfileDigest'], profile.digest)
        self.assertIn('injected fixture hook', (output / 'patch.diff').read_text())

    def test_refuses_source_nested_output_and_repeat_instrumentation(self):
        from reproloop.instrumentation import instrument_project
        with self.assertRaises(ContractError):
            instrument_project(self.source, self.profile, self.source / 'out', analyzer=self.analyzer)
        self.analyzer.assert_not_called()
        output = self.root / 'instrumented'
        instrument_project(self.source, self.profile, output, analyzer=self.analyzer)
        with self.assertRaises(ContractError):
            instrument_project(output / 'source', self.profile, self.root / 'twice', analyzer=self.analyzer)
        self.assertFalse((self.root / 'twice').exists())

    def test_rejects_mutated_source_after_instrumentation(self):
        from reproloop.instrumentation import instrument_project, validate_instrumented_source
        output = self.root / 'instrumented'
        instrument_project(self.source, self.profile, output, analyzer=self.analyzer)
        profile = validate_app_profile(read_json(output / 'app-profile.json'))
        (output / 'source' / self.activity_path).write_text('changed')
        with self.assertRaises(ContractError):
            validate_instrumented_source(output / 'source', profile)

    def test_refuses_analyzer_changes_outside_activity(self):
        from reproloop.instrumentation import instrument_project
        self.analyzer.return_value['files'] = {'app/build.gradle.kts': 'changed build code'}
        with self.assertRaises(ContractError):
            instrument_project(self.source, self.profile, self.root / 'invalid', analyzer=self.analyzer)
        self.assertFalse((self.root / 'invalid').exists())


class ManifestInstrumentationTests(unittest.TestCase):
    def test_self_closing_application_and_namespaces_remain_valid(self):
        from reproloop.instrumentation import merge_debug_manifest
        for source in ['<manifest><application/></manifest>',
            '<manifest xmlns:a="http://schemas.android.com/apk/res/android"><application a:label="Demo" /></manifest>',
            '<manifest><application><!-- keep --> <activity name="Original" /></application></manifest>']:
            result = merge_debug_manifest(source)
            root = ET.fromstring(result)
            self.assertEqual(len(root.findall('application/receiver')), 1)
            self.assertEqual(len(root.findall('receiver')), 0)
            if '<!-- keep -->' in source:self.assertIn('<!-- keep -->', result)


if __name__ == '__main__':
    unittest.main()
