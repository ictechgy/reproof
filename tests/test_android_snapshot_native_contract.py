"""Synthetic fixture and CI wiring checks, not Android runtime qualification."""
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from reproof.groovy_integration import configure_groovy
from reproof.source_snapshot import capture
from reproof.android_snapshot_observation import validate_snapshot_observation_profile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/check-android-snapshot-build.py'


class AndroidSnapshotNativeContractTests(unittest.TestCase):
    def test_both_tiny_fixtures_capture_and_validate_without_native_execution(self):
        harness = runpy.run_path(str(SCRIPT))
        for dsl in ('kotlin', 'groovy'):
            with self.subTest(dsl=dsl), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                names = harness['fixture'](root / 'source', dsl)
                self.assertEqual(len(names), 5)
                summary = capture(root / 'source', names, root / 'snapshot')
                doc = {'schemaVersion': 3, 'kind': 'views-observation-v3',
                    'package': harness['PACKAGE'], 'activity': '.MainActivity',
                    'activitySource': harness['ACTIVITY'],
                    'build': dict(zip(('task', 'apk'), harness['build_selection'](dsl)), dsl=dsl),
                    'tapTargets': ['save'], 'screenTargets': {'inventory_root': 'inventory'},
                    'sourceSnapshot': {'schemaVersion': 1, 'kind': 'reproof-local-source-snapshot',
                        **{key: summary[key] for key in ('digest', 'files', 'bytes')}}}
                selected = validate_snapshot_observation_profile(doc)
                self.assertEqual(selected.data, doc)
                suffix = '.gradle.kts' if dsl == 'kotlin' else '.gradle'
                settings = (root / 'source' / ('settings' + suffix)).read_text()
                module = (root / 'source/app' / ('build' + suffix)).read_text()
                self.assertIn('signingConfig = null', module)
                self.assertNotIn('storePassword', module)
                self.assertNotIn('keyAlias', module)
                if dsl == 'groovy':
                    self.assertEqual(harness['build_selection'](dsl), (
                        ':app:assembleDemoDebug', 'app/build/outputs/apk/demo/debug/app-demo-debug-unsigned.apk'))
                    self.assertIn("flavorDimensions 'qa'", module)
                    self.assertEqual(module.count('signingConfig = signingConfigs.debug'), 2)
                    self.assertIn("id 'io.reproof.instrumentation'", configure_groovy(settings, module)['module.gradle'])
                    from reproof.android_snapshot_observation import prepare_snapshot_observation
                    def analyzer(files, activity, taps):
                        self.assertEqual(set(files), {harness['ACTIVITY']})
                        return {'schemaVersion': 1, 'activityPath': harness['ACTIVITY'],
                            'sites': [{'id': 's123abc', 'path': harness['ACTIVITY'],
                                       'line': 12, 'target': 'save', 'kind': 'tap'}]}
                    with patch('subprocess.Popen', side_effect=AssertionError('Native execution forbidden')):
                        prepared = prepare_snapshot_observation(root / 'snapshot', selected,
                                                                root / 'prepared', analyzer=analyzer)
                    derived = (prepared['source'] / 'data/app/build.gradle').read_text()
                    self.assertEqual(derived.count('__reproofClearUnsignedSigning('), 3)
                    self.assertIn('setter.invoke(dimension, new Object[1])', derived)
                    self.assertIn('getter.invoke(dimension, new Object[0]) != null', derived)
                    self.assertIn('assert android.defaultConfig.getSigningConfig() != null', module)
                    self.assertIn("assert android.productFlavors.getByName('demo').getSigningConfig() != null", module)
                    self.assertIn("dimension.getClass().getName().contains('AgpDecorated')", module)
                    self.assertIn('assert dimension.getSigningConfig() == null', module)
                    self.assertEqual((root / 'source/app/build.gradle').read_text(), module)
                self.assertEqual((root / 'source' / harness['ACTIVITY']).read_text().count('setOnClickListener'), 1)

    def test_synthetic_workspace_rejects_any_created_key_store(self):
        harness = runpy.run_path(str(SCRIPT))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            harness['no_keys'](root)
            for filename in ('debug.keystore', 'unexpected.jks'):
                path = root / filename
                path.write_bytes(b'public synthetic marker, not a key')
                with self.assertRaises(RuntimeError): harness['no_keys'](root)
                path.unlink()

    def test_online_warm_and_offline_consumer_evidence_remain_distinct(self):
        source = SCRIPT.read_text()
        self.assertIn("'onlineWarmBuild': True, 'offlineConsumerBuild': True", source)
        self.assertIn("task, '--dry-run'", source)
        self.assertIn("require_unsigned_task_plan(planned, dsl)", source)
        self.assertIn('_unsigned_apk(warm / apk_relative)', source)
        self.assertIn("receipt['signed'] is False", source)
        self.assertIn("receipt['bytecodeInstrumentation']", source)
        self.assertIn('embedded.digest == prepared_profile.digest', source)
        self.assertIn('build_snapshot_observation_app(snapshot', source)
        self.assertIn("':app:verifyReproofUnsignedConfiguration'", source)
        self.assertIn("verified.count('REPROOF_GROOVY_UNSIGNED_DIMENSIONS 3') == 1", source)
        self.assertLess(source.index("':app:verifyReproofUnsignedConfiguration'"), source.index("task, '--dry-run'"))
        self.assertIn("'groovyDispatchReceivers': decorated_receivers", source)
        self.assertNotIn('build_android(', source)
        self.assertNotIn('adb ', source)

    def test_task_plan_allows_only_exact_selected_variant_metadata(self):
        harness = runpy.run_path(str(SCRIPT))
        check = harness['require_unsigned_task_plan']
        for dsl, variant in (('kotlin', 'Debug'), ('groovy', 'DemoDebug')):
            metadata = ':app:write' + variant + 'SigningConfigVersions'
            selected, _ = harness['build_selection'](dsl)
            valid = [':app:preBuild', metadata, ':app:package' + variant, selected]
            check(valid, dsl)
            for forbidden in (
                ':app:validateSigning' + variant, ':app:sign' + variant,
                ':app:signingConfigWriter' + variant, ':app:createKeystore',
                ':app:generateKey', ':app:keyGeneration', ':other:write' + variant + 'SigningConfigVersions',
                metadata + 'Extra', metadata.lower(),
                ':app:writeReleaseSigningConfigVersions',
                ':app:write' + ('Debug' if variant == 'DemoDebug' else 'DemoDebug') + 'SigningConfigVersions',
            ):
                with self.subTest(dsl=dsl, forbidden=forbidden), self.assertRaises(RuntimeError):
                    check(valid + [forbidden], dsl)
            for incomplete in ([], [selected], [metadata], [':app:preBuild']):
                with self.subTest(dsl=dsl, incomplete=incomplete), self.assertRaises(RuntimeError):
                    check(incomplete, dsl)
        with self.assertRaises(RuntimeError):
            check([':app:writeDebugSigningConfigVersions', ':app:assembleDebug'], 'unknown')

    def test_ci_preserves_existing_jvm_gates_and_validates_both_typed_reports(self):
        workflow = (ROOT / '.github/workflows/android-compile.yml').read_text()
        self.assertIn(':sdk:testDebugUnitTest', workflow)
        self.assertIn('scripts/check-android-fixture-junit.py android/sdk/build/test-results/testDebugUnitTest', workflow)
        self.assertIn('scripts/check-android-snapshot-build.py', workflow)
        self.assertIn("for dsl in ('kotlin', 'groovy')", workflow)
        self.assertIn('assert type(actual) is type(wanted)', workflow)
        self.assertIn('assert len(reports) == 1', workflow)
        self.assertIn('exact(proof, expected)', workflow)
        self.assertIn('exact(reports[0], expected)', workflow)
        self.assertIn("'deviceExecution': False, 'behaviorVerified': False", workflow)
        self.assertIn("'groovyDispatchReceivers': 3 if dsl == 'groovy' else 0", workflow)
        self.assertNotIn('connectedAndroidTest', workflow)
        self.assertNotIn('emulator -', workflow)

    def test_psi_bootstrap_is_pinned_and_uses_existing_builder(self):
        harness = runpy.run_path(str(SCRIPT))
        self.assertEqual(len(harness['PSI_DEPENDENCIES']), 7)
        self.assertTrue(all(item.count(':') == 2 and '+' not in item for item in harness['PSI_DEPENDENCIES']))
        source = SCRIPT.read_text()
        self.assertIn("tools/kotlin-instrumenter/build.sh", source)
        self.assertIn('repositories { mavenCentral() }', source)
        self.assertNotIn('environment = dict(os.environ)', source)
        self.assertIn("GRADLE_USER_HOME=str(home / '.gradle')", source)


if __name__ == '__main__':
    unittest.main()
