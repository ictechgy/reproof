from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof.android_build import create_protected_build, validate_protected_build
from reproof.android_profile import validate_app_profile
from reproof.core import ContractError, digest
from reproof.repair import snapshot_source
from reproof.storage import sha_file, read_json
from tests.test_android_profile import profile_document


class ProfileBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'app'
        self.source.mkdir()
        (self.source / 'build.gradle.kts').write_text('// public app build input')
        self.profile = validate_app_profile(profile_document())
        product = self.source / self.profile.data['edit']['path']
        product.parent.mkdir(parents=True)
        product.write_text('fun unitsPerItem() = 2')
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def build(self, source, **kwargs):
        source = Path(source)
        self.calls.append(source)
        self.assertNotEqual(source.resolve(), self.source.resolve())
        apk = source / kwargs['apk_relative']
        apk.parent.mkdir(parents=True, exist_ok=True)
        apk.write_bytes(kwargs['task'].encode())
        profile = kwargs.get('app_profile')
        files = snapshot_source(source, source_inputs=profile.data.get('sourceInputs') if profile else None)
        return apk, {'sourceFiles': files, 'sourceDigest': digest(files), 'buildCompleted': True,
                     'buildTask': kwargs['task'], 'apkSha256': sha_file(apk)}

    def create(self):
        with patch('reproof.android_build.build_android', self.build):
            return create_protected_build(self.source, self.root / 'built',
                gradle='fake', java_home='fake', sdk_home='fake', app_profile=self.profile)

    def test_original_and_platform_driver_build_from_frozen_inputs(self):
        receipt = self.create()
        self.assertEqual(receipt['appProfileDigest'], self.profile.digest)
        self.assertEqual(receipt['buildTask'], ':app:assembleDebug')
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(receipt['sourceDigest'], receipt['driverProof']['sourceDigest'])
        self.assertEqual(read_json(self.root / 'built/app-profile.json'), self.profile.data)
        self.assertEqual(validate_protected_build(self.root / 'built', source=self.source,
            app_profile=self.profile)['apkSha256'], receipt['apkSha256'])

    def test_generic_build_cannot_be_opened_without_matching_profile(self):
        self.create()
        with self.assertRaises(ContractError):
            validate_protected_build(self.root / 'built', source=self.source)
        document = profile_document()
        document['oracle']['expectedCondition']['text'] = '3'
        with self.assertRaises(ContractError):
            validate_protected_build(self.root / 'built', source=self.source,
                app_profile=validate_app_profile(document))

    def test_mutated_frozen_build_inputs_are_rejected(self):
        self.create()
        (self.root / 'built/app-source' / self.profile.data['edit']['path']).write_text('fun unitsPerItem() = 99')
        with self.assertRaises(ContractError):
            validate_protected_build(self.root / 'built', source=self.source, app_profile=self.profile)

    def test_explicit_binary_inputs_remain_bound_through_protected_build_validation(self):
        (self.source / 'settings.gradle.kts').write_text('include(":app")\n')
        (self.source / 'app/build.gradle.kts').write_text('// owned app build\n')
        binary = 'app/src/main/assets/logo.png'
        (self.source / binary).parent.mkdir(parents=True)
        (self.source / binary).write_bytes(b'\x89PNG\r\n\x1a\n\xff')
        document = self.profile.data
        document['sourceInputs'] = sorted([*snapshot_source(self.source), binary])
        self.profile = validate_app_profile(document)
        receipt = self.create()
        self.assertEqual(receipt['buildInputPolicy'], 'explicit-public-android-inputs-v2')
        self.assertIn(binary, receipt['sourceFiles'])
        validate_protected_build(self.root / 'built', source=self.source, app_profile=self.profile)
        (self.root / 'built/app-source' / binary).write_bytes(b'changed image')
        with self.assertRaises(ContractError):
            validate_protected_build(self.root / 'built', app_profile=self.profile)


if __name__ == '__main__':
    unittest.main()
