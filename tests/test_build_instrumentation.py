import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from reproloop.android_profile import validate_app_profile
from reproloop.core import ContractError
from reproloop.repair import snapshot_source
from reproloop.storage import read_json, sha_file, write_json
from tests.test_android_profile import profile_document


class BuildInstrumentationPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'original'
        self.activity_path = 'app/src/main/java/example/MainActivity.kt'
        self.activity = self.source / self.activity_path
        self.activity.parent.mkdir(parents=True)
        self.activity.write_text('package io.reproloop.inventory\nclass MainActivity\n')
        self.product = self.source / 'app/src/main/java/example/Stock.kt'
        self.product.write_text('fun unitsPerItem() = 2\n')
        (self.source / 'app/build.gradle.kts').write_text('// existing public build input\n')
        manifest = self.source / 'app/src/debug/AndroidManifest.xml'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('<manifest xmlns:android="http://schemas.android.com/apk/res/android">'
                            '<application android:label="Existing"><provider android:name="ExistingProvider" '
                            'android:authorities="existing.authority" /></application></manifest>\n')
        self.before = snapshot_source(self.source)
        self.profile = validate_app_profile(profile_document())
        self.analyzer = Mock(return_value={'schemaVersion': 1, 'activityPath': self.activity_path,
            'files': {self.activity_path: '// source-insertion output must never be applied'},
            'sites': [{'id': 's123abc', 'path': self.activity_path, 'line': 12,
                       'target': 'commit', 'kind': 'tap'}]})

    def tearDown(self):
        self.temp.cleanup()

    def test_build_mode_preserves_every_original_product_source_and_manifest(self):
        from reproloop.build_instrumentation import prepare_build_instrumentation
        from reproloop.instrumentation import validate_instrumented_source
        output = self.root / 'prepared'
        result = prepare_build_instrumentation(self.source, self.profile, output, analyzer=self.analyzer)
        self.assertEqual(snapshot_source(self.source), self.before)
        for name, checksum in self.before.items():
            if '/src/' in name:
                self.assertEqual(sha_file(output / 'source' / name), checksum)
        staged = output / 'source'
        self.assertFalse((staged / 'app/src/release/java/io/reproloop/autotrace').exists())
        self.assertFalse((staged / 'app/src/debug/java/io/reproloop/autotrace').exists())
        self.assertTrue((staged / 'app/reproloop-instrumentation/runtime/io/reproloop/autotrace/ReproHooks.kt').is_file())
        import xml.etree.ElementTree as ET
        merged = ET.parse(staged / 'app/reproloop-instrumentation/AndroidManifest.xml').getroot()
        self.assertEqual(len(merged.findall('application/provider')), 1)
        self.assertEqual(len(merged.findall('application/receiver')), 1)
        profile = validate_app_profile(read_json(output / 'app-profile.json'))
        receipt = validate_instrumented_source(staged, profile)
        self.assertEqual(profile.data['instrumentation']['kind'], 'android_asm_v1')
        self.assertEqual(receipt['kind'], 'build-instrumentation')
        self.assertTrue(receipt['productSourcesUnchanged'])
        self.assertEqual(result['mode'], 'build')
        self.assertFalse(result['behaviorVerified'])
        self.assertNotIn('a/' + self.activity_path, (output / 'patch.diff').read_text())

    def test_existing_buildsrc_is_rejected_without_touching_source_or_output(self):
        from reproloop.build_instrumentation import prepare_build_instrumentation
        (self.source / 'buildSrc').mkdir()
        with self.assertRaises(ContractError):
            prepare_build_instrumentation(self.source, self.profile, self.root / 'out', analyzer=self.analyzer)
        self.assertEqual(snapshot_source(self.source), self.before)
        self.assertFalse((self.root / 'out').exists())

    def test_explicit_inputs_preserve_existing_buildsrc_and_binary_resources(self):
        from reproloop.build_instrumentation import prepare_build_instrumentation
        from reproloop.instrumentation import validate_instrumented_source
        additions = {
            'settings.gradle.kts': b'pluginManagement { includeBuild("conventions") }\ninclude(":app")\n',
            'buildSrc/build.gradle.kts': b'plugins { `java-library` }\n',
            'buildSrc/src/main/java/PublicConvention.java': b'public class PublicConvention {}\n',
            'gradle/libs.versions.toml': b'[versions]\nexample="1"\n',
            'app/src/main/res/drawable/logo.png': b'\x89PNG\r\n\x1a\n\xff',
        }
        for name, raw in additions.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        document = self.profile.data
        document['sourceInputs'] = sorted(set(self.before) | set(additions))
        profile = validate_app_profile(document)
        before = snapshot_source(self.source.resolve(), source_inputs=document['sourceInputs'])
        output = self.root / 'explicit'
        prepare_build_instrumentation(self.source, profile, output, analyzer=self.analyzer)
        prepared = validate_app_profile(read_json(output / 'app-profile.json'))
        receipt = validate_instrumented_source(output / 'source', prepared)
        self.assertEqual(receipt['originalFiles'], before)
        for name, checksum in before.items():
            if name not in {'settings.gradle.kts', 'app/build.gradle.kts'}:
                self.assertEqual(sha_file(output / 'source' / name), checksum)
        self.assertTrue((output / 'source/reproloop-build-logic/build.gradle.kts').is_file())
        self.assertIn('io.reproloop.instrumentation', (output / 'source/app/build.gradle.kts').read_text())
        self.assertEqual(snapshot_source(self.source.resolve(), source_inputs=document['sourceInputs']), before)
        (output / 'source/app/src/main/res/drawable/logo.png').write_bytes(b'changed')
        with self.assertRaises(ContractError):
            validate_instrumented_source(output / 'source', prepared)

    def test_ambiguous_bytecode_line_mapping_is_rejected_before_staging(self):
        from reproloop.build_instrumentation import prepare_build_instrumentation
        self.analyzer.return_value['sites'].append(dict(self.analyzer.return_value['sites'][0], id='s234abc'))
        with self.assertRaises(ContractError):
            prepare_build_instrumentation(self.source, self.profile, self.root / 'out', analyzer=self.analyzer)
        self.assertFalse((self.root / 'out').exists())


class BytecodeProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        document = profile_document()
        document['captureMode'] = 'debug_receiver'
        document['targets']['report'] = None
        document['instrumentation'] = {'kind': 'android_asm_v1', 'sites': [
            {'id': 's123abc', 'path': 'app/src/main/java/example/MainActivity.kt',
             'line': 12, 'target': 'commit', 'kind': 'tap'}]}
        self.profile = validate_app_profile(document)
        self.path = self.root / 'app/build/reproloop/debug'
        self.path.mkdir(parents=True)
        self.class_bytes = b'fixture transformed class'
        with zipfile.ZipFile(self.path / 'classes.jar', 'w') as jar:
            jar.writestr('io/reproloop/inventory/MainActivity.class', self.class_bytes)
        self.report = {'schemaVersion': 1, 'kind': 'android_asm_v1', 'appProfileDigest': self.profile.digest,
            'activityClass': 'io.reproloop.inventory.MainActivity', 'instrumentedClasses': 1,
            'sites': [{'id': 's123abc', 'line': 12}], 'lifecycle': {'onCreate': True, 'onDestroy': True},
            'inputClassSha256': 'a' * 64, 'outputClassSha256': hashlib.sha256(self.class_bytes).hexdigest(),
            'outputJarSha256': sha_file(self.path / 'classes.jar')}
        write_json(self.path / 'report.json', self.report)

    def tearDown(self):
        self.temp.cleanup()

    def test_proof_binds_profile_sites_and_actual_transformed_class(self):
        from reproloop.build_instrumentation import validate_bytecode_artifacts
        proof = validate_bytecode_artifacts(self.root, self.profile)
        self.assertEqual(proof['report'], self.report)
        self.assertEqual(proof['transformedJarSha256'], self.report['outputJarSha256'])

    def test_rejects_missing_coverage_wrong_profile_and_false_lifecycle(self):
        from reproloop.build_instrumentation import validate_bytecode_artifacts
        for change in [{'sites': []}, {'appProfileDigest': '0' * 64},
                       {'lifecycle': {'onCreate': True, 'onDestroy': False}},
                       {'outputClassSha256': '0' * 64}]:
            report = copy.deepcopy(self.report)
            report.update(change)
            write_json(self.path / 'report.json', report)
            with self.assertRaises(ContractError):
                validate_bytecode_artifacts(self.root, self.profile)

    def test_changed_jar_cannot_reuse_a_passing_report(self):
        from reproloop.build_instrumentation import validate_bytecode_artifacts
        with zipfile.ZipFile(self.path / 'classes.jar', 'a') as jar:
            jar.writestr('untracked.class', b'changed')
        with self.assertRaises(ContractError):
            validate_bytecode_artifacts(self.root, self.profile)

    def test_build_receipt_requires_bytecode_evidence_in_addition_to_an_apk(self):
        from reproloop.repair import build_android
        product = self.root / self.profile.data['edit']['path']
        product.parent.mkdir(parents=True)
        product.write_text('fun unitsPerItem() = 2\n')
        apk = self.root / self.profile.data['build']['apk']
        apk.parent.mkdir(parents=True, exist_ok=True)
        apk.write_bytes(b'fixture APK')
        kwargs = dict(gradle='unused', java_home='unused', sdk_home='unused',
            task=self.profile.data['build']['task'], apk_relative=self.profile.data['build']['apk'],
            app_profile=self.profile)
        with patch('reproloop.repair.run_command', return_value='BUILD SUCCESSFUL'):
            _, proof = build_android(self.root, **kwargs)
            self.assertEqual(proof['bytecodeInstrumentation']['report'], self.report)
            (self.path / 'report.json').unlink()
            with self.assertRaisesRegex(ContractError, 'bytecode instrumentation output'):
                build_android(self.root, **kwargs)


if __name__ == '__main__':
    unittest.main()
