"""Synthetic inert input policy tests; no compiler, device, or network."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from reproof.core import ContractError
from reproof.source_input_policy import inert_input_path, input_paths, input_extensions
from reproof.android_sources import validate_source_inputs, freeze_source
from reproof.android_observation import validate_observation_profile
from reproof.ios_instrumentation import validate_ios_auto_profile, prepare_ios_instrumentation
from reproof.kotlin_instrumenter import _validate_inputs
from tests.test_android_observation import ordinary_views_project
from tests.test_ios_observation import ordinary_project


class SourceInputPolicyTests(unittest.TestCase):
    def test_unicode_paths_are_exact_and_nfc(self):
        for path in ('화면/설정.swift', '资源/图标.png', 'App/Écran.swift', 'App/日本 語.kt'):
            self.assertEqual(inert_input_path(path), path)
        for path in ('App/e\u0301.swift', 'App/\u1100\u1161.swift'):
            with self.assertRaises(ContractError): inert_input_path(path)

    def test_aliases_controls_credentials_and_syntax_rejected(self):
        for path in ('/a.kt', '../a.kt', 'a/../b.kt', 'a//b.kt', 'a\\b.kt', 'C:/a.kt',
                     'a/CON.kt', 'a/lPt9.txt', 'a/COM¹.txt', 'a/NUL/file.kt', 'a./b.kt',
                     ' a.kt', 'a /b.kt', 'a\u202e.kt', 'a\u200d.kt', 'a\x00.kt',
                     'a\n.kt', 'a/credentials.json', 'a/key.pem', '.env/a.kt',
                     'a/$name.kt', 'a/\uff0fb.kt', 'a/\ud800.kt'):
            with self.subTest(path=repr(path)), self.assertRaises(ContractError): inert_input_path(path)

    def test_collision_and_byte_budgets(self):
        for names in (['a.kt', 'A.kt'], ['ß.kt', 'ss.kt'], ['한글.kt', '한글.kt/x.kt'],
                      ['x.kt'] * 901, ['가' * 86 + '.kt'], ['a/' * 255 + '한.kt']):
            with self.assertRaises(ContractError): input_paths(names)
        self.assertEqual(input_paths(['가' * 80 + '.kt']), ['가' * 80 + '.kt'])

    def test_extension_selection_is_finite_exact_and_does_not_grant_paths(self):
        for value in ([], ['.exe'], ['.js', '.js'], ['.JS'], ['js'], [None], '*.js', True):
            with self.assertRaises(ContractError): input_extensions(value)
        with self.assertRaises(ContractError): validate_source_inputs(['assets/화면.js'])
        self.assertEqual(validate_source_inputs(['assets/화면.js'], extensions=['.js']), ['assets/화면.js'])
        for path in ('build/data.js', 'auth/data.js', 'private.key', 'CON.js'):
            with self.assertRaises(ContractError): validate_source_inputs([path], extensions=['.js'])

    def test_regular_freeze_preserves_binary_unicode_and_link_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); (root/'자산').mkdir()
            path = root/'자산'/'내장.jar'; path.write_bytes(b'opaque\x00\xff')
            frozen = freeze_source(root, ['자산/내장.jar'], extensions=['.jar'])
            self.assertEqual(frozen.entries, (('자산/내장.jar', b'opaque\x00\xff'),))
            os.link(path, root/'alias.jar')
            with self.assertRaises(ContractError): freeze_source(root, ['자산/내장.jar'], extensions=['.jar'])
            (root/'alias.jar').unlink(); (root/'link').symlink_to(root/'자산', target_is_directory=True)
            with self.assertRaises(ContractError): freeze_source(root, ['link/내장.jar'], extensions=['.jar'])
            with path.open('wb') as stream: stream.truncate(8 * 1024 * 1024 + 1)
            with self.assertRaises(ContractError): freeze_source(root, ['자산/내장.jar'], extensions=['.jar'])

    def test_kotlin_path_policy_accepts_names_not_identifiers(self):
        _validate_inputs({'app/화면.kt': 'class Main {}'}, 'example.Main', ['save'])
        with self.assertRaises(ContractError):
            _validate_inputs({'app/화면.kt': 'class Main {}'}, 'example.화면', ['save'])
        with self.assertRaises(ContractError):
            _validate_inputs({'a/ß.kt': 'x', 'a/ss.kt': 'x'}, 'example.Main', ['save'])

    def test_android_profile_and_prepared_source_extensions_propagate(self):
        from reproof.build_instrumentation import prepare_build_instrumentation
        from reproof.instrumentation import validate_instrumented_source
        from reproof.storage import read_json
        from reproof.repair import snapshot_source
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root/'original'
            doc = ordinary_views_project(source)
            old = 'app/src/main/java/example/MainActivity.kt'; new = 'app/src/main/java/example/화면.kt'
            (source/old).rename(source/new)
            doc['sourceInputs'].remove(old); doc['sourceInputs'].append(new)
            asset = 'app/src/main/assets/목록.js'; (source/asset).write_bytes(b'public asset\x00')
            doc['sourceInputs'].append(asset); doc['inputExtensions'] = ['.js']
            profile = validate_observation_profile(doc)
            site = {'id': 's123abc', 'path': new, 'line': 12, 'target': 'save', 'kind': 'tap'}
            analyzer = Mock(return_value={'schemaVersion': 1, 'activityPath': new, 'files': {}, 'sites': [site]})
            def configure(settings, module):
                return {'settings.gradle.kts': settings+'// synthetic\n', 'module.gradle.kts': module+'// synthetic\n'}
            with patch('reproof.kotlin_instrumenter.configure_gradle', side_effect=configure):
                result = prepare_build_instrumentation(source, profile, root/'prepared', analyzer=analyzer)
            prepared = validate_observation_profile(read_json(result['appProfile']))
            validate_instrumented_source(result['source'], prepared)
            self.assertEqual((result['source']/asset).read_bytes(), b'public asset\x00')
            snapshot_source(result['source'], source_inputs=prepared.data['sourceInputs'], source_extensions=['.js'], isolated=True)
            self.assertEqual(prepared.data['inputExtensions'], ['.js'])
            self.assertEqual((source/asset).read_bytes(), b'public asset\x00')

    def test_android_profile_invalid_extension_field(self):
        with tempfile.TemporaryDirectory() as directory:
            doc = ordinary_views_project(Path(directory).resolve()/'source')
            for value in (None, [], ['.exe'], ['.js', '.js']):
                with self.assertRaises(ContractError): validate_observation_profile(dict(doc, inputExtensions=value))

    def test_ios_preparation_copies_opted_in_unicode_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root/'original'
            doc = ordinary_project(source)
            for name in ('App/한국어.swift', 'App/효과.metal', 'App/사진.tiff'):
                (source/name).write_bytes(b'synthetic inert source\x00')
                doc['sourceInputs'].append(name)
            with self.assertRaises(ContractError): validate_ios_auto_profile(doc)
            doc['inputExtensions'] = ['.metal', '.tiff']
            profile = validate_ios_auto_profile(doc)
            result = prepare_ios_instrumentation(source, root/'prepared', profile=profile)
            for name in ('App/한국어.swift', 'App/효과.metal', 'App/사진.tiff'):
                self.assertEqual((Path(result['source'])/name).read_bytes(), (source/name).read_bytes())
            for invalid in (None, [], ['.pem']):
                with self.assertRaises(ContractError): validate_ios_auto_profile(dict(doc, inputExtensions=invalid))

    def test_runtime_template_accepts_exact_extension_field_and_unicode(self):
        from reproof.resources import read_resource
        source = read_resource('reproof/ios_instrumentation_templates/RLAutomaticRecorder.swift').decode()
        self.assertIn('value.utf8.elementsEqual(value.precomposedStringWithCanonicalMapping.utf8)', source)
        self.assertNotIn('value == value.precomposedStringWithCanonicalMapping', source)
        self.assertIn('value.utf8.count <= 512', source)
        self.assertIn('requiredKeys.union(["workspace", "inputExtensions"])', source)
        self.assertIn('Set(extensions).isSubset(of: allowedExtensions)', source)
        self.assertIn('Set(extensions).count == extensions.count', source)
        self.assertIn('CFGetTypeID(version) != CFBooleanGetTypeID()', source)
        for extension in input_extensions(['.metal', '.js', '.jar', '.so']):
            self.assertIn('"' + extension + '"', source)

    def test_android_build_snapshot_requires_same_explicit_extensions(self):
        from reproof.repair import build_android, snapshot_source
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root/'source'
            doc = ordinary_views_project(source)
            asset = 'app/src/main/assets/목록.css'
            (source/asset).write_bytes(b'public inert bytes')
            doc['sourceInputs'].append(asset); doc['inputExtensions'] = ['.css']
            profile = validate_observation_profile(doc)
            def command(*args, **kwargs):
                apk = source/doc['build']['apk']; apk.parent.mkdir(parents=True); apk.write_bytes(b'fakeapk')
                return ''
            with patch('reproof.repair.run_command', side_effect=command):
                _, receipt = build_android(source, '/fake/gradle', '/fake/java', '/fake/sdk',
                    doc['build']['task'], doc['build']['apk'], app_profile=profile)
            self.assertIn(asset, receipt['sourceFiles'])
            with self.assertRaises(ContractError): snapshot_source(source, source_inputs=doc['sourceInputs'])

    def test_current_caps_not_expanded(self):
        from reproof.execution.artifacts import BlobSet, ArtifactError
        from reproof.execution.wire import MAX_FILES, MAX_TRANSFER_BYTES
        self.assertEqual(MAX_FILES, 1024); self.assertEqual(MAX_TRANSFER_BYTES, 64 * 1024 * 1024)
        with self.assertRaises(ContractError): validate_source_inputs([f'a{i}.kt' for i in range(901)])
        with self.assertRaises(ArtifactError): BlobSet(tuple((f'a{i}.kt', b'') for i in range(1025)))
