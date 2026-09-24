import copy
from pathlib import Path
import plistlib
import tempfile
import unittest

from reproof.core import ContractError
from reproof.ios_storage import tree_manifest
from reproof.storage import read_json, sha_file


def minimal_project(root):
    (root / 'Sample').mkdir(parents=True)
    (root / 'Sample/App.swift').write_text('import UIKit\nfinal class Example: UIViewController {}\n')
    with (root / 'Sample/Info.plist').open('wb') as handle:
        plistlib.dump({'CFBundleIdentifier': '$(PRODUCT_BUNDLE_IDENTIFIER)',
                      'CFBundleExecutable': '$(EXECUTABLE_NAME)', 'ReproBuildID': '$(REPRO_BUILD_ID)'}, handle)
    project = root / 'Reproof.xcodeproj'
    project.mkdir()
    document = {'archiveVersion': '1', 'objectVersion': '56', 'rootObject': 'PROJECT', 'objects': {
        'PROJECT': {'isa': 'PBXProject', 'mainGroup': 'GROUP', 'targets': ['APP']},
        'GROUP': {'isa': 'PBXGroup', 'children': [], 'sourceTree': '<group>'},
        'APP': {'isa': 'PBXNativeTarget', 'name': 'ReproSample', 'productType': 'com.apple.product-type.application',
                'buildConfigurationList': 'CONFIGS', 'buildPhases': ['SOURCES']},
        'SOURCES': {'isa': 'PBXSourcesBuildPhase', 'files': []},
        'CONFIGS': {'isa': 'XCConfigurationList', 'buildConfigurations': ['DEBUG', 'RELEASE']},
        'DEBUG': {'isa': 'XCBuildConfiguration', 'name': 'Debug', 'buildSettings': {
            'PRODUCT_BUNDLE_IDENTIFIER': 'io.reproof.sample.ios', 'INFOPLIST_FILE': 'Sample/Info.plist'}},
        'RELEASE': {'isa': 'XCBuildConfiguration', 'name': 'Release', 'buildSettings': {
            'PRODUCT_BUNDLE_IDENTIFIER': 'io.reproof.sample.ios', 'INFOPLIST_FILE': 'Sample/Info.plist'}},
    }}
    with (project / 'project.pbxproj').open('wb') as handle:plistlib.dump(document, handle)


class IosInstrumentationPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'plain'
        minimal_project(self.source)
        self.before = tree_manifest(self.source, True)

    def tearDown(self):self.temp.cleanup()

    def test_preparation_keeps_original_product_code_and_info_plist_unchanged(self):
        from reproof.ios_instrumentation import prepare_ios_instrumentation, profile_from_source, validate_ios_preparation
        out = self.root / 'prepared'
        result = prepare_ios_instrumentation(self.source, out)
        self.assertEqual(tree_manifest(self.source, True), self.before)
        for name, checksum in self.before.items():
            if name.startswith('Sample/'):
                self.assertEqual(sha_file(out / 'source' / name), checksum)
        profile = profile_from_source(out / 'source')
        receipt = validate_ios_preparation(out / 'source')
        self.assertEqual(receipt['profileDigest'], profile.digest)
        self.assertTrue(receipt['productSourcesUnchanged'])
        self.assertFalse(result['behaviorVerified'])
        project = plistlib.loads((out / 'source/Reproof.xcodeproj/project.pbxproj').read_bytes())
        debug = project['objects']['DEBUG']['buildSettings']
        release = project['objects']['RELEASE']['buildSettings']
        self.assertEqual(debug['INFOPLIST_FILE'], 'ReproofInstrumentation/Info.plist')
        self.assertEqual(release['INFOPLIST_FILE'], 'Sample/Info.plist')
        self.assertIn('RLAutoConfig.swift', release['EXCLUDED_SOURCE_FILE_NAMES'])
        self.assertIn('RLAutoBootstrap.m', release['EXCLUDED_SOURCE_FILE_NAMES'])
        self.assertIn('RLAutomaticRecorder.swift', release['EXCLUDED_SOURCE_FILE_NAMES'])
        names = tree_manifest(out / 'source', True)
        self.assertIn('ReproofInstrumentation/Runtime/RLAutoBootstrap.m', names)

    def test_refuses_nested_output_repeat_and_existing_manual_recorder_calls(self):
        from reproof.ios_instrumentation import prepare_ios_instrumentation
        with self.assertRaises(ContractError):prepare_ios_instrumentation(self.source, self.source / 'out')
        (self.source / 'Sample/App.swift').write_text('Recorder.shared.recordTap(target: "counter.add")\n')
        with self.assertRaises(ContractError):prepare_ios_instrumentation(self.source, self.root / 'manual')
        self.assertFalse((self.root / 'manual').exists())
        (self.source / 'Sample/App.swift').write_text('import UIKit\n')
        out = self.root / 'prepared'
        prepare_ios_instrumentation(self.source, out)
        with self.assertRaises(ContractError):prepare_ios_instrumentation(out / 'source', self.root / 'twice')

    def test_mutating_generated_runtime_or_original_code_breaks_preparation_receipt(self):
        from reproof.ios_instrumentation import prepare_ios_instrumentation, validate_ios_preparation
        out = self.root / 'prepared'
        prepare_ios_instrumentation(self.source, out)
        (out / 'source/Sample/App.swift').write_text('changed\n')
        with self.assertRaises(ContractError):validate_ios_preparation(out / 'source')


class IosAutoCaptureContractTests(unittest.TestCase):
    def setUp(self):
        from reproof.ios_instrumentation import sample_ios_auto_profile
        from reproof.ios_cases import case_spec
        self.profile = sample_ios_auto_profile()
        self.capture = case_spec('counter').capture()
        self.capture.update(sessionId='22222222-2222-4222-8222-222222222222', startedAtMs=1234)
        self.run = '11111111-1111-4111-8111-111111111111'
        self.build = 'a' * 32
        self.marker = {'schemaVersion': 1, 'runId': self.run, 'sessionId': self.capture['sessionId'],
            'profileDigest': self.profile.digest, 'buildId': self.build, 'fixture': self.capture['fixture'],
            'startedAtMs': 1234, 'finalized': True, 'endSequence': 2}
        self.diagnostics = {'schemaVersion': 1, 'platform': 'ios', 'runId': self.run,
            'sessionId': self.capture['sessionId'], 'profileDigest': self.profile.digest, 'buildId': self.build,
            'endSequence': 2, 'actions': [{'eventId': 'e2', 'target': 'counter.add',
                'before': {'counter.count': '0'}, 'after': {'counter.count': '2'},
                'beforeScreen': 'main', 'afterScreen': 'main', 'outcome': 'returned'}]}

    def test_marker_requires_selected_run_profile_build_and_fresh_fixture(self):
        from reproof.ios_instrumentation import validate_ios_auto_marker
        validate_ios_auto_marker(self.marker, self.profile, run_id=self.run, build_id=self.build,
                                 fixture=self.capture['fixture'], min_started_at=1234)
        for values in [{'runId': '33333333-3333-4333-8333-333333333333'}, {'profileDigest': '0' * 64},
                       {'buildId': 'b' * 32}, {'startedAtMs': 1000}]:
            value = dict(self.marker, **values)
            with self.assertRaises(ContractError):
                validate_ios_auto_marker(value, self.profile, run_id=self.run, build_id=self.build,
                                         fixture=self.capture['fixture'], min_started_at=1234)

    def test_diagnostics_match_every_tap_and_reject_extra_private_channels(self):
        from reproof.ios_instrumentation import validate_ios_auto_diagnostics
        validate_ios_auto_diagnostics(self.diagnostics, self.capture, self.profile,
                                      run_id=self.run, build_id=self.build)
        for mutate in [lambda d: d.update(sessionId='stale'),
                       lambda d: d.update(profileDigest='0' * 64),
                       lambda d: d.update(actions=[]),
                       lambda d: d['actions'][0].update(eventId='e1'),
                       lambda d: d['actions'][0].update(message='private exception'),
                       lambda d: d['actions'][0]['after'].update(**{'counter.count': 'secret'}),
                       lambda d: d['actions'][0].update(afterScreen='private_screen')]:
            document = copy.deepcopy(self.diagnostics)
            mutate(document)
            with self.assertRaises(ContractError):
                validate_ios_auto_diagnostics(document, self.capture, self.profile,
                                              run_id=self.run, build_id=self.build)


if __name__ == '__main__':unittest.main()
