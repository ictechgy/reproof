"""Public fixture artifact policy tests; no Gradle, APK execution or signing."""
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/build-android-recovery-fixtures.py'


class RecoveryFixtureArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = runpy.run_path(str(SCRIPT))

    def test_gradle_jvm_home_is_the_fresh_private_home(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / 'private-home'; home.mkdir()
            command = self.script['gradle_command'](root / 'gradle', home)
            self.assertEqual(command, [str(root / 'gradle'), '-Duser.home=' + str(home),
                '--no-daemon', '--no-build-cache', '--console=plain', '--offline',
                ':sample:assembleBuggyDebug', ':live:assembleDebug'])
            self.assertIn('command = gradle_command(args.gradle, home)', SCRIPT.read_text())

    def test_exact_unsigned_task_plan(self):
        valid = '\n'.join([entry[1] + ' SKIPPED' for entry in self.script['SELECTIONS']]
                          + [task + ' SKIPPED' for task in self.script['METADATA_TASKS']])
        self.script['require_plan'](valid)
        for extra in (':sample:validateSigningBuggyDebug', ':live:signDebug',
                      ':live:createKey', ':foreign:writeDebugSigningConfigVersions'):
            with self.subTest(extra=extra), self.assertRaises(RuntimeError):
                self.script['require_plan'](valid + '\n' + extra)
        with self.assertRaises(RuntimeError): self.script['require_plan']('')

    def test_copy_is_public_tracked_and_unsigned_without_original_edit(self):
        before = {name: (ROOT / 'android' / name / 'build.gradle.kts').read_bytes()
                  for name in ('sample', 'live')}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.script['copy_source'](root)
            for module in before:
                copied = (root / module / 'build.gradle.kts').read_text()
                self.assertEqual(copied.count('signingConfig = null'), 3)
                self.assertEqual((ROOT / 'android' / module / 'build.gradle.kts').read_bytes(), before[module])
            self.assertFalse((root / 'local.properties').exists())
            self.assertFalse((root / '.gradle').exists())

    def test_key_audit_rejects_created_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.script['no_keys'](root)
            (root / 'debug.keystore').write_text('synthetic, not a key')
            with self.assertRaises(RuntimeError): self.script['no_keys'](root)

    def test_exact_two_unsigned_artifacts_and_commit_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / 'source'; output = root / 'artifacts'; output.mkdir()
            for _, _, relative, _ in self.script['SELECTIONS']:
                path = source / relative; path.parent.mkdir(parents=True)
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr('AndroidManifest.xml', b'synthetic fixture')
            replies = [subprocess.CompletedProcess([], 0, ("package: name='%s'\n" % entry[3]).encode())
                       for entry in self.script['SELECTIONS']]
            with patch('subprocess.run', side_effect=replies) as run:
                manifest = self.script['publish_apks'](source, output, root / 'sdk', {}, 'a' * 40)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(manifest['sourceCommit'], 'a' * 40)
            self.assertFalse(manifest['deviceExecution']); self.assertFalse(manifest['runtimeQualified'])
            self.assertEqual({entry['name'] for entry in manifest['artifacts']},
                             {'sample-unsigned.apk', 'helper-unsigned.apk'})
            for entry in manifest['artifacts']:
                self.assertFalse(entry['signed']); self.assertEqual(len(entry['sha256']), 64)
                self.assertEqual(entry['bytes'], (output / entry['name']).stat().st_size)

    def test_signed_apk_rejected_before_package_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / 'source'; output = root / 'artifacts'; output.mkdir()
            path = source / self.script['SELECTIONS'][0][2]; path.parent.mkdir(parents=True)
            with zipfile.ZipFile(path, 'w') as archive: archive.writestr('META-INF/CERT.RSA', b'synthetic')
            with patch('subprocess.run', side_effect=AssertionError('Must reject before native metadata probe')):
                with self.assertRaises(ValueError):
                    self.script['publish_apks'](source, output, root / 'sdk', {}, 'a' * 40)

    def test_ci_uploads_only_unsigned_fixture_set_without_device_execution(self):
        workflow = (ROOT / '.github/workflows/android-compile.yml').read_text()
        self.assertIn('python3 scripts/build-android-recovery-fixtures.py', workflow)
        for name in ('sample-unsigned.apk', 'helper-unsigned.apk', 'manifest.json'):
            self.assertIn('/reproof-recovery-fixtures/artifacts/' + name, workflow)
        script = SCRIPT.read_text()
        self.assertIn("'--offline'", script)
        self.assertIn('ANDROID_USER_HOME=str(android_home)', script)
        self.assertNotIn('adb ', script)
        self.assertNotIn('keytool', script)
        self.assertNotIn('rmtree', script)


if __name__ == '__main__':
    unittest.main()
