from pathlib import Path
import plistlib
import tempfile
import unittest

from reproof.core import ContractError, digest
from reproof.ios_cases import case_spec
from reproof.ios_instrumentation import sample_ios_auto_profile
from reproof.ios_storage import create_ios_bundle, load_ios_bundle, tree_manifest


class IosAutoStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.products = self.root / 'products'
        self.app = self.products / 'Debug-iphonesimulator/ReproSample.app'
        self.app.mkdir(parents=True)
        self.profile = sample_ios_auto_profile()
        self.build_id = 'b' * 32
        info = {'CFBundleIdentifier': 'io.reproof.sample.ios', 'ReproBuildID': self.build_id,
                'ReproAutoProfile': self.profile.data, 'ReproAutoProfileDigest': self.profile.digest}
        with (self.app / 'Info.plist').open('wb') as handle:plistlib.dump(info, handle)
        (self.app / 'ReproSample').write_bytes(b'synthetic app')
        sources = {'ReproofInstrumentation/Runtime/RLAutomaticRecorder.swift': 'a' * 64}
        self.receipt = {'productsDigest': digest(tree_manifest(self.products)), 'buildCompleted': True,
            'buildId': self.build_id, 'appRelative': 'Debug-iphonesimulator/ReproSample.app',
            'sourceFiles': sources, 'sourceDigest': digest(sources),
            'automaticInstrumentation': {'profile': self.profile.data, 'profileDigest': self.profile.digest,
                                          'sourceFiles': sources}}
        self.capture = case_spec('counter').capture()
        self.capture.update(sessionId='22222222-2222-4222-8222-222222222222', startedAtMs=1234)
        self.diagnostics = {'schemaVersion': 1, 'platform': 'ios', 'runId': '11111111-1111-4111-8111-111111111111',
            'sessionId': self.capture['sessionId'], 'profileDigest': self.profile.digest,
            'buildId': self.build_id, 'endSequence': 2, 'actions': [{'eventId': 'e2', 'target': 'counter.add',
                'before': {'counter.count': '0'}, 'after': {'counter.count': '2'},
                'beforeScreen': 'main', 'afterScreen': 'main', 'outcome': 'returned'}]}

    def tearDown(self):self.temp.cleanup()

    def test_auto_app_requires_diagnostics_and_binds_them_into_scenario(self):
        with self.assertRaises(ContractError):
            create_ios_bundle(self.capture, case_spec().oracle(), self.products, self.receipt, self.root / 'missing')
        self.assertFalse((self.root / 'missing').exists())
        bundle = create_ios_bundle(self.capture, case_spec().oracle(), self.products, self.receipt,
                                   self.root / 'good', diagnostics=self.diagnostics)
        self.assertEqual(bundle['scenario']['diagnostics'], self.diagnostics)
        self.assertEqual(bundle['scenario']['autoProfileDigest'], self.profile.digest)
        self.assertIn('diagnostics.json', bundle['manifest']['files'])
        (bundle['path'] / 'diagnostics.json').write_text('{}')
        with self.assertRaises(ContractError):load_ios_bundle(bundle['path'])

    def test_auto_app_cannot_be_downgraded_to_an_uninstrumented_receipt(self):
        self.receipt.pop('automaticInstrumentation')
        with self.assertRaises(ContractError):
            create_ios_bundle(self.capture, case_spec().oracle(), self.products, self.receipt, self.root / 'downgrade')


if __name__ == '__main__':unittest.main()
