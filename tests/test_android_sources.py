import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproloop.android_profile import validate_app_profile
from reproloop.core import ContractError
from reproloop.repair import build_android, copy_source, snapshot_source
from tests.test_android_profile import profile_document


class AndroidPublicInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'original'
        self.entries = {
            'settings.gradle.kts': b'rootProject.name = "inventory"\ninclude(":app")\n',
            'app/build.gradle.kts': b'plugins { id("com.android.application") }\n',
            'app/src/main/java/example/Stock.kt': b'fun unitsPerItem() = 2\n',
            'buildSrc/build.gradle.kts': b'plugins { `java-library` }\n',
            'buildSrc/src/main/java/Convention.java': b'public class Convention {}\n',
            'gradle/libs.versions.toml': b'[versions]\nexample = "1"\n',
            'public-build.properties': b'feature.inventory=true\n',
            'app/src/main/assets/catalog.json': b'{"sku":"OWNED-TEST"}\n',
            'app/src/main/res/drawable/logo.png': b'\x89PNG\r\n\x1a\n\xff',
        }
        for name, raw in self.entries.items():
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        self.inputs = sorted(self.entries)

    def tearDown(self):
        self.temp.cleanup()

    def profile(self):
        document = profile_document()
        document['sourceInputs'] = self.inputs
        return validate_app_profile(document)

    def test_profile_binds_selection_without_changing_native_runtime_contract(self):
        profile = self.profile()
        legacy = validate_app_profile(profile_document())
        self.assertEqual(profile.native(), legacy.native())
        self.assertNotEqual(profile.digest, legacy.digest)
        self.assertEqual(profile.data['sourceInputs'], self.inputs)

    def test_freezes_only_selected_public_inputs_including_binary_and_buildsrc(self):
        # Owned canary files must remain unopened and absent from the copy.
        (self.source / '.env').write_text('OWNED_UNSELECTED_CANARY\n')
        (self.source / 'unselected.kt').write_text('unselected source\n')
        before = snapshot_source(self.source, source_inputs=self.inputs)
        result = copy_source(self.source, self.root / 'copy', source_inputs=self.inputs)
        self.assertEqual(result, before)
        for name, raw in self.entries.items():
            self.assertEqual((self.root / 'copy' / name).read_bytes(), raw)
        self.assertFalse((self.root / 'copy/.env').exists())
        self.assertFalse((self.root / 'copy/unselected.kt').exists())
        (self.root / 'copy/app/src/main/assets/catalog.json').write_bytes(b'changed')
        self.assertNotEqual(snapshot_source(self.root / 'copy', source_inputs=self.inputs), before)

    def test_rejects_ambiguous_private_generated_and_missing_required_selection(self):
        for extra in ['.env', 'auth.json', 'local.properties', 'app/keys.json',
                      'app/build/output.kt', '../escape.kt', 'app/private.pem',
                      'gradle/libs.versions.toml', 'APP/src/main/assets/catalog.json']:
            with self.subTest(extra=extra):
                document = profile_document()
                document['sourceInputs'] = self.inputs + [extra]
                with self.assertRaises(ContractError):
                    validate_app_profile(document)
        document = profile_document()
        document['sourceInputs'] = ['settings.gradle.kts']
        with self.assertRaises(ContractError):
            validate_app_profile(document)

    def test_links_and_nonregular_inputs_fail_before_publication(self):
        original = self.source / self.inputs[0]
        for kind in ('symlink', 'hardlink', 'fifo', 'parent-link'):
            with self.subTest(kind=kind):
                path = self.source / ('linked-' + kind + '.kt')
                if kind == 'symlink':
                    path.symlink_to(original)
                elif kind == 'hardlink':
                    os.link(original, path)
                elif kind == 'fifo':
                    os.mkfifo(path)
                else:
                    path = self.source / 'alias'
                    path.symlink_to(self.source / 'app', target_is_directory=True)
                    path = path / 'build.gradle.kts'
                with self.assertRaises(ContractError):
                    copy_source(self.source, self.root / kind,
                                source_inputs=[path.relative_to(self.source).as_posix()])
                self.assertFalse((self.root / kind).exists())
                if kind == 'hardlink':
                    path.unlink()

    def test_build_rejects_changed_binary_and_unlisted_new_build_input(self):
        profile = self.profile()
        for changed in ('app/src/main/assets/catalog.json', 'app/src/main/extra.bin'):
            source = self.root / ('build-' + str(len(changed)))
            copy_source(self.source, source, source_inputs=self.inputs)
            def command(*args, **kwargs):
                (source / changed).write_bytes(b'changed during build')
                apk = source / profile.data['build']['apk']
                apk.parent.mkdir(parents=True)
                apk.write_bytes(b'owned-apk')
                return ''
            with self.subTest(changed=changed), patch('reproloop.repair.run_command', command):
                with self.assertRaises(ContractError):
                    build_android(source, 'gradle', '/java', '/sdk', ':app:assembleDebug',
                                  profile.data['build']['apk'], app_profile=profile)

    def test_gradle_and_kotlin_generated_caches_are_never_copied_as_inputs(self):
        for name in ('.kotlin/sessions', '.gradle/cache', 'app/build/generated'):
            directory = self.source / name
            directory.mkdir(parents=True)
            (directory / 'owned-output.kt').write_text('generated cache\n')
        before = snapshot_source(self.source, source_inputs=self.inputs, isolated=True)
        result = copy_source(self.source, self.root / 'copy', source_inputs=self.inputs)
        self.assertEqual(before, result)
        self.assertFalse((self.root / 'copy/.kotlin').exists())
        self.assertFalse((self.root / 'copy/.gradle').exists())
        self.assertFalse((self.root / 'copy/app/build').exists())


if __name__ == '__main__':
    unittest.main()
