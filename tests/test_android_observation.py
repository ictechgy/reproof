import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from reproloop.core import ContractError
from reproloop.storage import read_json, sha_file


def ordinary_views_project(root):
    files = {
        'settings.gradle.kts': 'pluginManagement {}\ninclude(":app")\n',
        'app/build.gradle.kts': 'plugins { id("com.android.application") }\n',
        'app/src/main/AndroidManifest.xml': '<manifest/>\n',
        'app/src/main/java/example/MainActivity.kt': 'package com.example.inventory\nclass MainActivity\n',
        'buildSrc/build.gradle.kts': 'plugins { `java-library` }\n',
        'buildSrc/src/main/java/PublicBuild.java': 'public class PublicBuild {}\n',
        'app/src/main/assets/catalog.json': '{"items":["public-demo"]}\n',
    }
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    return {
        'schemaVersion': 2, 'kind': 'views-observation-v2',
        'package': 'com.example.inventory', 'activity': '.MainActivity',
        'build': {'task': ':app:assembleDebug', 'apk': 'app/build/outputs/apk/debug/app-debug.apk'},
        'sourceInputs': sorted(files), 'tapTargets': ['save'],
        'screenTargets': {'inventory_root': 'inventory'},
    }


class AndroidObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'original'
        self.document = ordinary_views_project(self.source)
        self.site = {'id': 's123abc', 'path': 'app/src/main/java/example/MainActivity.kt',
                     'line': 12, 'target': 'save', 'kind': 'tap'}
        self.analyzer = Mock(return_value={'schemaVersion': 1, 'activityPath': self.site['path'],
            'files': {self.site['path']: 'must not be used'}, 'sites': [self.site]})

    def prepare(self):
        from reproloop.android_observation import validate_observation_profile
        from reproloop.build_instrumentation import prepare_build_instrumentation
        profile = validate_observation_profile(self.document)
        result = prepare_build_instrumentation(self.source, profile, self.root / 'prepared', analyzer=self.analyzer)
        return result, validate_observation_profile(read_json(result['appProfile']))

    def test_observation_contract_has_no_fixture_or_repair_policy(self):
        from reproloop.android_observation import validate_observation_profile
        from reproloop.android_profile import validate_app_profile
        profile = validate_observation_profile(self.document)
        self.assertEqual(profile.component_name, 'com.example.inventory/.MainActivity')
        with self.assertRaises(ContractError):
            validate_app_profile(self.document)
        for field in ('fixture', 'oracle', 'edit', 'captureMode'):
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_observation_profile(dict(self.document, **{field: {}}))
        changed = copy.deepcopy(self.document)
        changed['screenTargets']['inventory_root'] = 'other'
        self.assertNotEqual(profile.digest, validate_observation_profile(changed).digest)

    def test_invalid_inputs_and_unbound_sites_fail_closed(self):
        from reproloop.android_observation import validate_observation_profile
        for change in (
            {'build': {'task': ':app:assembleRelease', 'apk': self.document['build']['apk']}},
            {'sourceInputs': self.document['sourceInputs'] + ['auth.json']},
            {'sourceInputs': self.document['sourceInputs'][1:]},
            {'tapTargets': ['save', 'save']},
            {'screenTargets': {'inventory_root': 'inventory', 'other': 'inventory'}},
            {'instrumentation': None},
            {'instrumentation': {'kind': 'android_asm_v1', 'sites': [dict(self.site, target='other')]}},
        ):
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_observation_profile(dict(self.document, **change))

    def test_preparation_preserves_product_inputs_and_embeds_only_observations(self):
        from reproloop.instrumentation import validate_instrumented_source
        original = {name: sha_file(self.source / name) for name in self.document['sourceInputs']}
        result, profile = self.prepare()
        prepared = result['source']
        receipt = validate_instrumented_source(prepared, profile)
        self.assertEqual(receipt['originalFiles'], original)
        for name, checksum in original.items():
            self.assertEqual(sha_file(self.source / name), checksum)
            if name not in {'settings.gradle.kts', 'app/build.gradle.kts'}:
                self.assertEqual(sha_file(prepared / name), checksum)
        support = prepared / 'app/reproloop-instrumentation'
        embedded = read_json(support / 'assets/reproloop-observation.json')
        self.assertEqual(embedded, profile.data)
        runtime = support / 'runtime/io/reproloop/autotrace'
        self.assertFalse((runtime / 'AutoExportReceiver.kt').exists())
        self.assertFalse((support / 'runtime/io/reproloop/sdk').exists())
        self.assertNotIn('fixture', (runtime / 'ReproAuto.kt').read_text())
        self.assertNotIn('receiver', (support / 'AndroidManifest.xml').read_text())
        self.assertIn('RECORD_MODE = "observe"', (runtime / 'ReproConfig.kt').read_text())
        (prepared / 'app/src/main/assets/catalog.json').write_text('{}')
        with self.assertRaises(ContractError):
            validate_instrumented_source(prepared, profile)

    def test_apk_metadata_requires_a_single_bounded_prepared_profile(self):
        from reproloop.android_observation import profile_from_apk
        _, profile = self.prepare()
        apk = self.root / 'app.apk'
        with zipfile.ZipFile(apk, 'w') as archive:
            archive.writestr('assets/reproloop-observation.json', json.dumps(profile.data))
        self.assertEqual(profile_from_apk(apk).data, profile.data)
        for raw in (json.dumps(self.document), '{"kind":1,"kind":2}', 'x' * (256 * 1024 + 1)):
            with zipfile.ZipFile(apk, 'w') as archive:
                archive.writestr('assets/reproloop-observation.json', raw)
            with self.assertRaises(ContractError):
                profile_from_apk(apk)
        with zipfile.ZipFile(apk, 'w') as archive:
            archive.writestr('classes.dex', b'no adapter')
        self.assertIsNone(profile_from_apk(apk))

    def test_build_uses_frozen_inputs_and_rejects_missing_embedded_adapter(self):
        from reproloop.android_observation import build_observation_app
        from reproloop.core import digest
        from reproloop.repair import snapshot_source
        result, profile = self.prepare()

        def builder(source, **kwargs):
            self.assertNotEqual(Path(source), result['source'])
            current = snapshot_source(source, source_inputs=profile.data['sourceInputs'], isolated=True)
            apk = Path(source) / profile.data['build']['apk']
            apk.parent.mkdir(parents=True)
            with zipfile.ZipFile(apk, 'w') as archive:
                archive.writestr('assets/reproloop-observation.json', json.dumps(profile.data))
            return apk, {'sourceFiles': current, 'sourceDigest': digest(current), 'buildCompleted': True,
                         'apkSha256': sha_file(apk), 'bytecodeInstrumentation': {'synthetic': True}}

        with patch('reproloop.repair.build_android', side_effect=builder):
            built = build_observation_app(result['source'], self.root / 'built', profile=profile,
                gradle='unused', java_home='unused', sdk_home='unused')
        self.assertTrue(built['receipt']['automaticObservations'])
        self.assertTrue(built['app'].is_file())
        self.assertNotIn('fixture', built['receipt'])
        self.assertFalse((self.root / 'built/driver.apk').exists())

        def invalid_builder(source, **kwargs):
            apk, proof = builder(source, **kwargs)
            with zipfile.ZipFile(apk, 'w') as archive:
                archive.writestr('classes.dex', b'missing observation adapter')
            proof['apkSha256'] = sha_file(apk)
            return apk, proof

        with patch('reproloop.repair.build_android', side_effect=invalid_builder), self.assertRaises(ContractError):
            build_observation_app(result['source'], self.root / 'invalid-build', profile=profile,
                gradle='unused', java_home='unused', sdk_home='unused')
        self.assertFalse((self.root / 'invalid-build').exists())


if __name__ == '__main__':
    unittest.main()
