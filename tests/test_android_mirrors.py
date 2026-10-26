import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof.android_mirrors import MirrorError, PINS, main, parse_config, prepare, render_init

ROOT = Path(__file__).resolve().parents[1] / 'android'


def config():
    return {'schemaVersion': 1,
            'pluginRepositories': ['https://mirror.example.test/plugins/'],
            'dependencyRepositories': ['https://mirror.example.test/android/', 'https://mirror.example.test/central/'],
            'distributionUrl': 'https://mirror.example.test/gradle/gradle-8.14.5-bin.zip'}


class AndroidMirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / 'config.json'
        self.config.write_text(json.dumps(config()))
        self.output = self.home / 'bundle'

    def test_bundle_preserves_inputs_and_exact_wrapper_checksum(self):
        before = {p: (ROOT / p).read_bytes() for p in PINS}
        manifest = prepare(ROOT, self.config, self.output)
        self.assertEqual(before, {p: (ROOT / p).read_bytes() for p in PINS})
        props = (self.output / 'gradle/wrapper/gradle-wrapper.properties').read_text()
        self.assertIn('distributionUrl=https\\://mirror.example.test/gradle/gradle-8.14.5-bin.zip', props)
        self.assertIn('distributionSha256Sum=6f74b601422d6d6fc4e1f9a1ab6522f642c2fdcbc15ae33ebd30ba3d7198e854', props)
        for name, meta in manifest['files'].items():
            data = (self.output / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), meta['sha256'])
            self.assertEqual((self.output / name).stat().st_mode & 0o777, int(meta['mode'], 8))
        self.assertFalse(manifest['executed'])
        self.assertTrue((self.output / 'READY').exists())
        self.assertNotIn('https:', (self.output / 'manifest.json').read_text())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)

    def test_settings_level_replacement_preserves_strict_mode(self):
        text = render_init(parse_config(self.config.read_bytes())).decode()
        self.assertIn('gradle.settingsEvaluated', text)
        self.assertIn('settings.pluginManagement.repositories.clear()', text)
        self.assertIn('settings.dependencyResolutionManagement.repositories.clear()', text)
        self.assertIn('FAIL_ON_PROJECT_REPOS', text)
        self.assertIn(PINS['settings.gradle.kts'], text)
        self.assertIn('project.buildscript.repositories', text)
        self.assertIn('repositories.whenObjectAdded', text)
        self.assertNotIn('project.repositories', text)
        self.assertNotIn('allprojects', text)
        self.assertNotIn('google()', text)
        self.assertNotIn('gradlePluginPortal()', text)
        self.assertLess(text.index('/android/'), text.index('/central/'))

    def test_urls_reject_credentials_escaping_queries_and_injection(self):
        for url in ['http://mirror.example.test/', 'https://user:pass@mirror.example.test/',
                    'https://mirror.example.test/a?token=x', 'https://mirror.example.test/#x',
                    'https://mirror.example.test/%40', 'https://mirror.example.test/a\nxxx',
                    "https://mirror.example.test/a');exec('x", 'https://mirror.example.test/$x',
                    'https://mirror.example.test/../a', 'https://mirror.example.test:99999/',
                    'https://mirror.example.test:0/', 'https://-bad.example.test/',
                    'https://mirror.example.test/\\a', 'file:///tmp/a']:
            value = config()
            value['pluginRepositories'] = [url]
            with self.subTest(url=url), self.assertRaises(MirrorError):
                parse_config(json.dumps(value))

    def test_schema_duplicate_missing_unknown_limits(self):
        for change in [{'schemaVersion': True}, {'schemaVersion': 2}, {'extra': 'x'},
                       {'pluginRepositories': []}, {'dependencyRepositories': ['https://a.test/'] * 9},
                       {'pluginRepositories': ['https://a.test/', 'https://a.test/']},
                       {'dependencyRepositories': 'https://a.test/'}]:
            value = config() | change
            with self.subTest(change=change), self.assertRaises(MirrorError):
                parse_config(json.dumps(value))
        with self.assertRaises(MirrorError):
            parse_config('{"schemaVersion":1,"schemaVersion":1}')
        with self.assertRaises(MirrorError):
            parse_config(b'\xff')

    def test_distribution_version_cannot_be_overridden(self):
        for url in ['https://a.test/gradle-9.0-bin.zip', 'https://a.test/gradle-8.14.5-all.zip']:
            with self.assertRaisesRegex(MirrorError, '^mirror_distribution_invalid$'):
                parse_config(json.dumps(config() | {'distributionUrl': url}))

    def test_bad_configuration_does_not_create_output_and_never_logs_it(self):
        self.config.write_text(json.dumps(config() | {'distributionUrl': 'https://secret:password@a.test/x'}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = main(['--android-root', str(ROOT), '--config', str(self.config), '--output', str(self.output)])
        self.assertEqual(result, 2)
        self.assertEqual(err.getvalue(), 'mirror_url_invalid\n')
        self.assertEqual(out.getvalue(), '')
        self.assertFalse(self.output.exists())

    def test_no_overwrite_or_source_tree_output(self):
        self.output.mkdir()
        marker = self.output / 'original'
        marker.write_text('untouched')
        with self.assertRaises(MirrorError):
            prepare(ROOT, self.config, self.output)
        self.assertEqual(marker.read_text(), 'untouched')
        with self.assertRaises(MirrorError):
            prepare(ROOT, self.config, ROOT / 'forbidden-generated')
        self.assertFalse((ROOT / 'forbidden-generated').exists())

    def test_config_symlink_hardlink_or_parent_symlink_rejected(self):
        link = self.home / 'linked'
        link.symlink_to(self.config)
        with self.assertRaises(MirrorError):
            prepare(ROOT, link, self.output)
        link.unlink()
        link.hardlink_to(self.config)
        with self.assertRaises(MirrorError):
            prepare(ROOT, link, self.output)
        link.unlink()
        parent = self.home / 'parent'
        parent.symlink_to(ROOT)
        with self.assertRaises(MirrorError):
            prepare(parent, self.config, self.output)

    def test_output_parent_must_be_private_and_symlink_free(self):
        parent = self.home / 'parent'
        parent.mkdir()
        parent.chmod(0o777)
        with self.assertRaisesRegex(MirrorError, '^mirror_output_parent_unsafe$'):
            prepare(ROOT, self.config, parent / 'bundle')
        parent.chmod(0o700)
        alias = self.home / 'alias'
        alias.symlink_to(parent)
        with self.assertRaisesRegex(MirrorError, '^mirror_path_invalid$'):
            prepare(ROOT, self.config, alias / 'bundle')
        self.assertFalse((parent / 'bundle').exists())

    def test_partial_write_has_no_ready_and_does_not_touch_existing_files(self):
        import os
        original = os.open
        def failing_open(path, flags, *args, **kwargs):
            if str(path).endswith('mirrors.init.gradle'):
                raise OSError('simulated storage failure')
            return original(path, flags, *args, **kwargs)
        with patch('reproof.android_mirrors.os.open', side_effect=failing_open):
            with self.assertRaisesRegex(MirrorError, '^mirror_output_incomplete$'):
                prepare(ROOT, self.config, self.output)
        self.assertTrue(self.output.exists())
        self.assertFalse((self.output / 'READY').exists())
        existing = (self.output / 'gradlew').read_bytes()
        with self.assertRaisesRegex(MirrorError, '^mirror_output_invalid$'):
            prepare(ROOT, self.config, self.output)
        self.assertEqual((self.output / 'gradlew').read_bytes(), existing)

    def test_pinned_source_mismatch_fails_without_output(self):
        with patch.dict(PINS, {'settings.gradle.kts': '0' * 64}):
            with self.assertRaisesRegex(MirrorError, '^mirror_source_changed$'):
                prepare(ROOT, self.config, self.output)
        self.assertFalse(self.output.exists())

    def test_oversized_config_rejected(self):
        self.config.write_bytes(b' ' * 32769)
        with self.assertRaises(MirrorError):
            prepare(ROOT, self.config, self.output)

    def test_unexpected_filesystem_failure_is_redacted(self):
        out, err = io.StringIO(), io.StringIO()
        with patch('reproof.android_mirrors.prepare', side_effect=OSError('private/path/host')), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = main(['--android-root', str(ROOT), '--config', str(self.config), '--output', str(self.output)])
        self.assertEqual(result, 2)
        self.assertEqual(err.getvalue(), 'mirror_preparation_failed\n')
        self.assertEqual(out.getvalue(), '')

    def test_prepare_is_offline_and_stdout_has_fixed_status(self):
        out = io.StringIO()
        with patch('subprocess.run', side_effect=AssertionError('no process')), \
             patch('socket.socket', side_effect=AssertionError('no network')), \
             contextlib.redirect_stdout(out):
            result = main(['--android-root', str(ROOT), '--config', str(self.config), '--output', str(self.output)])
        self.assertEqual(result, 0)
        self.assertEqual(out.getvalue(), 'mirror_bundle_prepared_not_executed\n')


if __name__ == '__main__':
    unittest.main()
