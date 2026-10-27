"""Portable wiring/closure checks; actual Swift vectors run in macOS CI."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class IosLaunchContractTests(unittest.TestCase):
    def test_compiled_helper_contains_fenced_immutable_selection(self):
        source = (ROOT/'live-ios/Tests/LiveControlTests.swift').read_text()
        control = source[source.index('    func testControlSession() {'):source.index('    private func launchTarget(')]
        self.assertLess(control.index('approvedLaunch == nil || authority.enabled'), control.index('try bridge.ready()'))
        self.assertLess(control.index('try bridge.ready()'), control.index('launchTarget()'))
        self.assertIn('static let result: Result<ApprovedLaunchConfiguration?, Error>', source)
        self.assertIn('approvedLaunchConfiguration(ProcessInfo.processInfo.environment, simulator: false)', source)
        self.assertIn('launchEnvironment.merge(approvedLaunch.environment)', source)
        self.assertIn('approvedLaunch: try ApprovedLaunchSelection.result.get()?.handshake', source)

    def test_native_source_is_in_distribution_and_build_project(self):
        resources = json.loads((ROOT/'distribution-resources.json').read_text())['files']
        self.assertIn('live-ios/Tests/LiveControlTests.swift', resources)
        project = (ROOT/'live-ios/ReproLive.xcodeproj/project.pbxproj').read_text()
        self.assertIn('LiveControlTests.swift', project)
        # No out-of-manifest Swift source is introduced by this feature.
        swift = {str(path.relative_to(ROOT)) for path in (ROOT/'live-ios').rglob('*.swift')}
        self.assertLessEqual(swift, set(resources))

    def test_native_harness_extracts_actual_source_and_has_nonzero_cases(self):
        script = ROOT/'scripts/check-ios-launch-contract.py'
        spec = importlib.util.spec_from_file_location('ios_launch_vectors', script)
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        cases = module.vectors()
        self.assertEqual(len(cases), 56)
        self.assertGreaterEqual(sum(case['accepted'] for case in cases), 7)
        source = module.swift_source()
        self.assertIn('func approvedLaunchConfiguration(', source)
        self.assertIn('actual?.environment == expected', source)
        self.assertIn('accepted == expectedAccepted', source)
        self.assertIn('repeated_launch_environment', source)
        self.assertIn('iOS approved launch application: 4 synthetic launches passed', source)
        self.assertNotIn('import XCTest', source)
        self.assertNotIn('XCUIApplication', source)
        self.assertIn('check=True', script.read_text())

    def test_ci_runs_parser_vectors_and_compile_without_device_tasks(self):
        workflow = (ROOT/'.github/workflows/ios-sdk-compile.yml').read_text()
        self.assertIn('"$REPRO_PYTHON" scripts/check-ios-launch-contract.py', workflow)
        self.assertIn('ios-launch-contract.log', workflow)
        self.assertIn('xcodebuild build-for-testing', workflow)
        self.assertNotIn('simctl boot', workflow)
        self.assertNotIn('test-without-building', workflow)
        self.assertIn('tests.test_ios_launch_contract', (ROOT/'tests/release_inventory.json').read_text())


if __name__ == '__main__':
    unittest.main()
