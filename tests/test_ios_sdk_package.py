"""Portable structure/wiring checks only; these do not execute or validate Swift."""
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from reproof.ios_storage import copy_ios_source, tree_manifest
from reproof.sdk_review import ReviewInvalid, normalize_export
from reproof.sdk_review_demo import sample_export

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / 'ios/SDK'


class IOSSDKPackageTests(unittest.TestCase):
    def test_package_is_local_foundation_only_and_has_public_consumer(self):
        manifest = (SDK / 'Package.swift').read_text()
        self.assertIn('dependencies: []', manifest)
        self.assertIn('.library(name: "ReproofSDK"', manifest)
        self.assertIn('.testTarget(name: "ReproofSDKTests"', manifest)
        for source in (SDK / 'Sources').rglob('*.swift'):
            text = source.read_text()
            self.assertNotIn('import UIKit', text)
            self.assertNotIn('ReproCase', text)
            self.assertNotIn('URLSession', text)
            self.assertNotIn('print(', text)
        public_test = (SDK / 'Tests/ReproofSDKTests/PublicAPITests.swift').read_text()
        self.assertIn('\nimport ReproofSDK\n', public_test)
        self.assertNotIn('\n@testable', public_test)
        consumer = (SDK / 'Consumer/Package.swift').read_text()
        self.assertIn('path: ".."', consumer)
        self.assertNotIn('https://', consumer)

    def test_single_writer_with_thin_case_adapter_and_local_project_wiring(self):
        adapter = (ROOT / 'ios/Recorder/Recorder.swift').read_text()
        self.assertIn('import ReproofSDK', adapter)
        self.assertIn('let sampleCase = ReproCase.current', adapter)
        self.assertIn('if sampleCase == .reset', adapter)
        self.assertIn('fixture: FixtureMetadata(id: sampleCase.fixtureID, version: 1)', adapter)
        self.assertNotIn('JSONEncoder', adapter)
        self.assertNotIn('FileHandle', adapter)
        core = (SDK / 'Sources/ReproofSDK/ReproofRecorder.swift').read_text()
        self.assertIn('public final class ReproofRecorder', core)
        self.assertNotIn('[weak self]', core)
        spec = (ROOT / 'ios/project.yml').read_text()
        self.assertEqual(spec.count('packages:\n  ReproofSDK:\n    path: SDK\n'), 1)
        self.assertEqual(spec.count('      - package: ReproofSDK'), 1)
        project = (ROOT / 'ios/Reproof.xcodeproj/project.pbxproj').read_text()
        self.assertIn('isa = XCLocalSwiftPackageReference;', project)
        self.assertIn('relativePath = SDK;', project)
        self.assertIn('productName = ReproofSDK;', project)
        self.assertNotIn('XCRemoteSwiftPackageReference', project)

    def test_ios_only_staging_copies_complete_package_and_excludes_build_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'source'
            copied = copy_ios_source(ROOT / 'ios', destination)
            for name in ('SDK/Package.swift', 'SDK/Sources/ReproofSDK/Configuration.swift',
                         'SDK/Sources/ReproofSDK/ReproofRecorder.swift',
                         'SDK/Tests/ReproofSDKTests/RecorderTests.swift',
                         'SDK/Consumer/Package.swift', 'SDK/Consumer/Sources/SDKConsumer/main.swift'):
                self.assertIn(name, copied)
                self.assertEqual((destination / name).read_bytes(), (ROOT / 'ios' / name).read_bytes())
            for directory in ('.build', '.swiftpm'):
                state = destination / 'SDK' / directory
                state.mkdir(); (state / 'private.swift').write_text('EXCLUDED_SYNTHETIC_MARKER')
            self.assertFalse(any('/.build/' in name or '/.swiftpm/' in name for name in tree_manifest(destination, True)))

    def test_plain_fixture_removes_sdk_and_dependency_without_running_xcodegen(self):
        spec = importlib.util.spec_from_file_location('prepare_plain_ios', ROOT / 'scripts/prepare-ios-instrumentation-fixture.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary, patch.object(module, 'run_command') as native:
            result = module.prepare(Path(temporary) / 'plain')
            source = Path(result['source'])
            self.assertFalse((source / 'SDK').exists())
            self.assertFalse((source / 'Recorder').exists())
            self.assertNotIn('ReproofSDK', (source / 'project.yml').read_text())
            self.assertNotIn('Recorder', (source / 'Sample/CounterViewController.swift').read_text())
            self.assertNotIn('Recorder', (source / 'Sample/AppDelegate.swift').read_text())
            native.assert_called_once()
            self.assertEqual(native.call_args.args[0][:2], ['/opt/homebrew/bin/xcodegen', 'generate'])

    def test_distribution_lists_exact_package_resources(self):
        resources = json.loads((ROOT / 'distribution-resources.json').read_text())['files']
        declared = {name for name in resources if name.startswith('ios/SDK/')}
        actual = {path.relative_to(ROOT).as_posix() for path in SDK.rglob('*')
                  if path.is_file() and not any(part in {'.build', '.swiftpm'} for part in path.parts)}
        self.assertEqual(declared, actual)
        self.assertFalse(any('/.build/' in name or '/.swiftpm/' in name for name in resources))

    def test_host_stays_sample_only_and_rejects_custom_annotations(self):
        exported = sample_export('ios')
        capture = json.loads(exported['capture']); metadata = json.loads(exported['metadata'])
        capture['applicationId'] = metadata['applicationId'] = 'example.inventory'
        exported.update(capture=json.dumps(capture).encode(), metadata=json.dumps(metadata).encode())
        with self.assertRaisesRegex(ReviewInvalid, '^sdk_export_invalid$'):
            normalize_export(**exported)
        self.assertEqual(normalize_export(**sample_export('ios')).summary()['status'], 'review-only')

    def test_export_checker_is_bounded_and_logs_only_fixed_review_result(self):
        spec = importlib.util.spec_from_file_location('check_ios_export', ROOT / 'scripts/check-ios-sdk-export.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        exported = sample_export('ios')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for filename, field in [('capture.json', 'capture'), ('metadata.json', 'metadata'), ('finalized.json', 'marker')]:
                (directory / filename).write_bytes(exported[field])
            result = module.check(directory)
            self.assertEqual(result, {'status': 'synthetic-writer-export-compatible',
                'reviewStatus': 'review-only', 'repairReady': False, 'executionAuthority': 'none'})
            (directory / 'unexpected.txt').write_text('PRIVATE_CANARY')
            with self.assertRaisesRegex(ReviewInvalid, '^sdk_export_invalid$'):
                module.check(directory)
            (directory / 'unexpected.txt').unlink()
            (directory / 'capture.json').write_bytes(b'PRIVATE_CANARY' * 30000)
            with self.assertRaisesRegex(ReviewInvalid, '^sdk_export_invalid$'):
                module.check(directory)

    def test_export_checker_cli_failure_never_echoes_payload_or_paths(self):
        with tempfile.TemporaryDirectory(prefix='PRIVATE_CANARY-') as temporary:
            directory = Path(temporary)
            for name in ('capture.json', 'metadata.json', 'finalized.json'):
                (directory / name).write_text('PRIVATE_CANARY')
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/check-ios-sdk-export.py'),
                '--directory', str(directory)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout), {'status': 'sdk_export_invalid'})
            self.assertEqual(result.stderr, '')
            self.assertNotIn('PRIVATE_CANARY', result.stdout + result.stderr)

    def _assert_compile_only_workflow(self, workflow):
        for required in ('contents: read', 'persist-credentials: false', 'timeout-minutes: 20',
                         'Xcode_16.4.app', 'Build version 16F6', 'Python/3.12.', 'REPRO_PYTHON', 'swift test --package-path ios/SDK',
                         'Executed [1-9][0-9]* tests?, with 0 failures',
                         'swift build --package-path ios/SDK/Consumer',
                         'generic/platform=iOS Simulator', 'for configuration in Debug Release',
                         'CODE_SIGNING_ALLOWED=NO', '-disableAutomaticPackageResolution', '-skipPackageUpdates',
                         'scripts/check-ios-sdk-export.py', 'REPRO_SDK_TEST_EXPORT_DIRECTORY',
                         'actions/upload-artifact@v4'):
            self.assertIn(required, workflow)
        # Only this reviewed unsigned generic-destination command is allowed.
        compile_command = '          xcodebuild build-for-testing -project live-ios/ReproLive.xcodeproj -scheme ReproLive \\\n            -configuration Debug -destination \'generic/platform=iOS Simulator\' \\\n            -derivedDataPath "$RUNNER_TEMP/reproof-live-helper" \\\n            -disableAutomaticPackageResolution -skipPackageUpdates \\\n            CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY= \\\n            2>&1 | tee "$RUNNER_TEMP/reproof-sdk-logs/live-helper-compile.log"'
        self.assertEqual(workflow.count(compile_command), 1)
        marker = '      - name: Compile unsigned Live helper without booting or running a Simulator\n'
        self.assertEqual(workflow.count(marker), 1)
        helper_step = workflow.split(marker, 1)[1].split('      - name:', 1)[0]
        self.assertIn(compile_command, helper_step)
        remaining = workflow.replace(compile_command, '', 1)
        for required in ('timeout-minutes: 8', 'ReproLiveHost.app',
                         'ReproLiveTests-Runner.app/PlugIns/ReproLiveTests.xctest',
                         "assert info['CFBundleIdentifier'] == identifier",
                         "assert info['CFBundleExecutable'] == executable",
                         'binary.is_file() and binary.stat().st_size > 0',
                         "root.glob('ReproLive_iphonesimulator*.xctestrun')",
                         'assert len(configs) == 1 and configs[0].is_file()'):
            self.assertIn(required, workflow)
        self.assertEqual(workflow.count("      - 'live-ios/**'"), 2)
        for forbidden in ('simctl', 'devicectl', 'bootstatus', 'test-without-building',
                          'build-for-testing', 'xcode-select', 'security ', 'secrets.',
                          'pull_request_target', 'workflow_dispatch', 'brew install'):
            self.assertNotIn(forbidden, remaining)
        self.assertNotRegex(workflow, r'xcodebuild[^\n]*(?:\s+test(?:\s|$)|\s+install(?:\s|$))')

    def test_workflow_is_compile_only_with_nonzero_swift_test_evidence(self):
        self._assert_compile_only_workflow((ROOT / '.github/workflows/ios-sdk-compile.yml').read_text())

    def test_compile_only_contract_rejects_changed_actions_and_targets(self):
        workflow = (ROOT / '.github/workflows/ios-sdk-compile.yml').read_text()
        mutations = [
            workflow.replace('Compile unsigned Live helper without booting or running a Simulator', 'Unreviewed helper invocation'),
            workflow.replace('xcodebuild build-for-testing', 'xcodebuild test'),
            workflow.replace('xcodebuild build-for-testing', 'xcodebuild test-without-building'),
            workflow.replace('xcodebuild build-for-testing', 'xcodebuild install'),
            workflow.replace("-destination 'generic/platform=iOS Simulator'", "-destination 'platform=iOS Simulator,name=iPhone 16'"),
            workflow.replace('CODE_SIGNING_ALLOWED=NO', 'CODE_SIGNING_ALLOWED=YES'),
            workflow.replace('-disableAutomaticPackageResolution', ''),
            workflow.replace('binary.is_file() and binary.stat().st_size > 0', 'True'),
            workflow + '\n          xcodebuild build-for-testing -project other.xcodeproj\n',
            workflow + '\n          xcodebuild test -project other.xcodeproj\n',
        ]
        mutations.extend(workflow + '\n          xcrun simctl ' + action + ' target\n'
                         for action in ('boot', 'install', 'launch'))
        for index, changed in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(AssertionError):
                self._assert_compile_only_workflow(changed)
