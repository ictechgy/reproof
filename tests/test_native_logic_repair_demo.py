"""Portable guardrail tests only. Fabricated parser fixtures are not native proof."""
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from reproof import native_logic_repair_demo as demo
from reproof.execution.artifacts import ArtifactError, BlobSet
from reproof.project_repair import RepairError

ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = Path('/owned/package/Tests/LogicTests.swift')


def evidence(mode='fixed'):
    fail = int(mode != 'fixed')
    verdict = 'failed' if fail else 'passed'
    failure = '<failure message="failed"></failure>' if fail else ''
    xml = (f'<testsuites><testsuite name="TestResults" errors="0" tests="1" failures="{fail}" time="0.01">'
           '<testcase classname="ReproLogicTests.SubmissionLogicTests" name="testDuplicateIsRejected" time="0.01">'
           f'{failure}</testcase></testsuite></testsuites>').encode()
    assertion = ''
    if fail:
        oracle = demo.RECIPE_VALUE['failureAssertions'][mode]
        assertion = f"{ORACLE_PATH}:{oracle['line']}: error: {demo.OBJC_CASE} : {oracle['assertion']} failed -\n"
    summary = f'\t Executed 1 test, with {fail} failures ({0} unexpected) in 0.01 (0.02) seconds\n'
    log = ("Test Suite 'Selected tests' started at 2026-10-25 10:00:00.\n"
           "Test Suite 'SubmissionLogicTests' started at 2026-10-25 10:00:00.\n"
           f"Test Case '{demo.OBJC_CASE}' started.\n" + assertion +
           f"Test Case '{demo.OBJC_CASE}' {verdict} (0.01 seconds).\n"
           f"Test Suite 'SubmissionLogicTests' {verdict} at 2026-10-25 10:00:00.\n" + summary +
           f"Test Suite 'Selected tests' {verdict} at 2026-10-25 10:00:00.\n" + summary).encode()
    return xml, log, fail


class NativeLogicResultTests(unittest.TestCase):
    def parse(self, mode='fixed', xml=None, log=None, code=None):
        original_xml, original_log, original_code = evidence(mode)
        return demo._validate_result(original_xml if xml is None else xml,
            original_log if log is None else log, original_code if code is None else code,
            mode, ORACLE_PATH)

    def test_exact_one_native_test_with_two_assertions(self):
        for mode in ('baseline', 'wrong', 'fixed'):
            result = self.parse(mode)
            self.assertEqual(result['testCase'], demo.CASE)
            self.assertEqual(result['tests'], 1)
            self.assertEqual(result['assertionsInFrozenTest'], 2)
            self.assertEqual(result['failures'], int(mode != 'fixed'))

    def test_filter_matches_only_exact_case(self):
        self.assertIsNotNone(re.fullmatch(demo.FILTER, demo.CASE))
        for value in (demo.CASE + 'Extra', 'Other.' + demo.CASE, demo.CASE.replace('.', 'X')):
            self.assertIsNone(re.search(demo.FILTER, value))

    def test_expected_failure_not_generic_nonzero_or_another_assertion(self):
        _, baseline, _ = evidence('baseline')
        _, wrong, _ = evidence('wrong')
        for log in (b'compile failed', b'error: missing Xcode', baseline.replace(b'XCTAssertFalse', b'XCTAssertTrue'),
                    baseline.replace(b':13:', b':12:'), baseline.replace(b'/owned/', b'/other/'),
                    baseline + b'error: unexpected setup failure\n', wrong):
            with self.subTest(log=log), self.assertRaises(demo.NativeLogicError):
                self.parse('baseline', log=log)
        for code in (0, -9, 2, True):
            with self.subTest(code=code), self.assertRaises(demo.NativeLogicError):
                self.parse('baseline', code=code)

    def test_fail_closed_xml_and_test_identity(self):
        xml, _, _ = evidence()
        bad = [b'', b'<testsuites>', xml + xml, b'<!DOCTYPE x>' + xml,
               xml.replace(b'tests="1"', b'tests="0"'), xml.replace(b'errors="0"', b'errors="1"'),
               xml.replace(b'failures="0"', b'failures="1"'), xml.replace(b'time="0.01"', b'time="nan"'),
               xml.replace(b'time="0.01"', b'time="-1"'), xml.replace(b'SubmissionLogicTests', b'OtherTests'),
               xml.replace(b'testDuplicateIsRejected', b'testSkipped'),
               xml.replace(b'</testcase>', b'<skipped/></testcase>'),
               xml.replace(b'</testcase>', b'<error/></testcase>'),
               xml.replace(b'</testsuites>', b'<testsuite/></testsuites>'),
               xml.replace(b'</testsuite>', b'<testcase/></testsuite>'),
               xml.replace(b'tests="1"', b'tests="1" skipped="1"'),
               xml.replace(b'<testcase', b'TRUNCATED<testcase')]
        for value in bad:
            with self.subTest(xml=value), self.assertRaises(demo.NativeLogicError):
                self.parse(xml=value)
        baseline, _, _ = evidence('baseline')
        for value in (baseline.replace(b'<failure message="failed"></failure>', b''),
                      baseline.replace(b'</testcase>', b'<failure message="failed"/></testcase>'),
                      baseline.replace(b'message="failed"', b'message="setup failed"')):
            with self.subTest(xml=value), self.assertRaises(demo.NativeLogicError):
                self.parse('baseline', xml=value)

    def test_zero_tests_skipped_duplicate_truncated_reordered_logs_reject(self):
        _, log, _ = evidence()
        summary = b'\t Executed 1 test, with 0 failures (0 unexpected) in 0.01 (0.02) seconds\n'
        rows = log.splitlines(keepends=True)
        reordered = b''.join(rows[:2] + rows[4:] + rows[2:4])
        bad = [b'', log[:100], log + log, log.replace(b'Executed 1 test', b'Executed 0 tests'),
               log.replace(b'passed (', b'skipped ('), log + b'XCTSkip called\n',
               log + b'Test skipped\n', log + b'Executed 0 tests, with 0 failures\n',
               log.replace(b'SubmissionLogicTests', b'OtherTests'), log[:-len(summary)],
               log.replace(b'Test Suite \'Selected tests\' passed', b'Test Suite \'Other\' passed'),
               reordered, log + b'Test Case incomplete\n', log + b'error: process crashed\n',
               log.replace(b'with 0 failures', b'with 1 failure'), log + b'\xff',
               log.replace(b'(0.01 seconds)', b'(... seconds)'),
               log.replace(b'in 0.01 (0.02) seconds', b'in garbage'),
               log + b'*** Signal 11: Backtracing from 0x100... done ***\n']
        for value in bad:
            with self.subTest(log=value), self.assertRaises(demo.NativeLogicError):
                self.parse(log=value)

    def test_swift_testing_zero_footer_does_not_supply_missing_xctest(self):
        footer = b'\nTest run with 0 tests passed after 0.001 seconds.\n'
        _, log, _ = evidence()
        with self.assertRaises(demo.NativeLogicError):
            self.parse(log=log + footer)
        with self.assertRaises(demo.NativeLogicError):
            self.parse(log=footer)


class NativeLogicBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'source'
        paths = (*demo.SOURCE_PATHS, demo.PROJECT, 'reproof/native_logic_repair_demo.py',
                 'reproof/project_repair.py', 'reproof/ios_core.py', 'reproof/ios_cases.py', 'reproof/ticket_intake.py')
        BlobSet.from_directory(ROOT, paths).write_new(self.root)
        self.output = Path(self.temporary.name).resolve() / 'output'
        self.output.mkdir()

    def test_exact_checked_in_sources_and_oracle_preserved(self):
        source, frozen = demo._load_source(self.root)
        entries = dict(frozen.entries)
        self.assertEqual(entries[demo.ORACLE], (ROOT / demo.ORACLE).read_bytes())
        self.assertEqual(entries[demo.PRODUCT], (ROOT / demo.PRODUCT).read_bytes())
        self.assertEqual(source.editable_paths, (demo.PRODUCT,))
        self.assertEqual(demo._sha(entries[demo.ORACLE]), demo.PINS[demo.ORACLE])
        self.assertIn(b'SubmissionLogic.shouldAccept(submitted: submitted)', entries[demo.CALLSITE])

    def test_synthetic_duplicate_ticket_binds_native_oracle_not_shipping(self):
        source, frozen = demo._load_source(self.root)
        ticket, entry, specification = demo._ticket_material(source.project, demo.PINS[demo.ORACLE])
        draft = demo._draft(source, frozen)
        self.assertEqual(draft['references']['sourceDigest'], frozen.digest)
        self.assertEqual(draft['references']['specificationDigest'], demo.contracts.digest(specification))
        self.assertEqual(specification['provenance']['digest'], demo.PINS[demo.ORACLE])
        self.assertEqual(specification['assertions'][1]['predicate']['children'], [
            {'kind': 'property', 'observationId': 'first_submission', 'property': 'accepted', 'operator': 'equals', 'value': True},
            {'kind': 'property', 'observationId': 'duplicate_submission', 'property': 'accepted', 'operator': 'equals', 'value': False}])
        self.assertNotIn('shipping', json.dumps(ticket))
        self.assertIs(draft['approved'], False)
        self.assertEqual(draft['executionAuthority'], 'none')
        self.assertIs(draft['aiTransfer'], False)

    def test_tampering_pinned_input_or_project_rejects_before_toolchain(self):
        for path in (*demo.SOURCE_PATHS, demo.PROJECT):
            original = (self.root / path).read_bytes()
            try:
                (self.root / path).write_bytes(original.replace(b'editablePaths', b'otherPaths') if path == demo.PROJECT else original + b' ')
                with self.subTest(path=path), patch.object(demo, '_toolchain') as toolchain:
                    with self.assertRaises((demo.NativeLogicError, demo.contracts.ContractError, RepairError)):
                        demo._run_demo(self.root, self.output, 'fixed')
                    toolchain.assert_not_called()
            finally:
                (self.root / path).write_bytes(original)

    def test_symlink_input_is_rejected(self):
        for name in (*demo.SOURCE_PATHS, demo.PROJECT):
            path = self.root / name
            original = path.read_bytes()
            other = self.root.parent / 'linked'
            other.write_bytes(original)
            path.unlink(); path.symlink_to(other)
            try:
                with self.subTest(path=name), self.assertRaises((ArtifactError, RepairError)):
                    demo._load_source(self.root)
            finally:
                path.unlink(); path.write_bytes(original)

    def test_protected_paths_and_noop_rejected(self):
        source, frozen = demo._load_source(self.root)
        for name in (*source.protected_paths, demo.PROJECT, '../SubmissionLogic.swift', '/tmp/SubmissionLogic.swift',
                     'reproof/native_logic_repair_demo.py', 'tests/test_native_logic_repair_demo.py'):
            with self.subTest(path=name), self.assertRaises(RepairError) as caught:
                source.apply(frozen, [{'path': name, 'old': 'true', 'new': 'false'}])
            self.assertEqual(caught.exception.code, 'protected_path')
        with self.assertRaises(RepairError):
            source.apply(frozen, [{'path': demo.PRODUCT, 'old': 'true', 'new': 'true'}])

    def test_only_finite_boolean_change_is_accepted(self):
        source, frozen = demo._load_source(self.root)
        before = dict(frozen.entries)[demo.PRODUCT].decode()
        for expression in ('false', '!submitted'):
            demo.validate_swift_expression(before, before.replace('return true', 'return ' + expression), 'duplicate-submit')
        for expression in ('submitted', 'exit(0)', 'true; fatalError()', '[]', 'true\n print("forged")'):
            with self.subTest(expression=expression), self.assertRaises(demo.contracts.ContractError):
                demo.validate_swift_expression(before, before.replace('return true', 'return ' + expression), 'duplicate-submit')

    def test_bad_draft_never_invokes_toolchain(self):
        source, frozen = demo._load_source(self.root)
        draft = demo._draft(source, frozen)
        variants = []
        for key, value in (('approved', True), ('executionAuthority', 'protected'), ('status', 'abstained')):
            changed = copy.deepcopy(draft); changed[key] = value; variants.append(changed)
        for key in draft['references']:
            changed = copy.deepcopy(draft); changed['references'][key] = 'f' * 64; variants.append(changed)
        for changed in variants:
            with self.subTest(draft=changed), patch.object(demo, 'intake_ticket', return_value=changed), patch.object(demo, '_toolchain') as toolchain:
                with self.assertRaises(demo.NativeLogicError):
                    demo._run_demo(self.root, self.output, 'fixed')
                toolchain.assert_not_called()

    def test_control_flow_mock_is_not_native_execution_evidence(self):
        before = BlobSet.from_directory(self.root, demo.SOURCE_PATHS).digest
        for candidate in ('fixed', 'wrong', 'noop'):
            observations = []
            def fake_observe(blobs, directory, mode):
                observations.append((blobs, mode))
                return {'parserUnitTestStub': True}
            with patch.object(demo, '_toolchain', return_value={'unitTestStub': True}), patch.object(demo, '_observe', side_effect=fake_observe):
                report = demo._run_demo(self.root, self.output, candidate)
            self.assertEqual(len(observations), 1 if candidate == 'noop' else 2)
            self.assertEqual(observations[0][1], 'baseline')
            if candidate != 'noop':
                self.assertEqual(report['candidateOracleDigest'], report['oracleDigest'])
                self.assertNotEqual(report['candidateDigest'], report['sourceDigest'])
            self.assertEqual(BlobSet.from_directory(self.root, demo.SOURCE_PATHS).digest, before)
            for key in ('sdkCapture', 'scenarioReplay', 'mobileUI', 'deviceExecution', 'actualAI',
                        'externalAI', 'companyAcceptance', 'protectedVerification', 'sandbox'):
                self.assertIs(report[key], False)

    def test_failed_build_timeout_or_result_never_returns_success(self):
        for code in ('native_build_failed', 'command_timeout', 'expected_assertion_missing', 'test_count_mismatch'):
            with self.subTest(code=code), patch.object(demo, '_toolchain', return_value={}), patch.object(demo, '_observe', side_effect=demo.NativeLogicError(code)):
                with self.assertRaises(demo.NativeLogicError) as caught:
                    demo._run_demo(self.root, self.output, 'fixed')
                self.assertEqual(caught.exception.code, code)

    def test_runtime_and_portable_goals_are_separate_and_invoked(self):
        inventory = json.loads((ROOT / 'tests/release_inventory.json').read_text())['goals']
        portable = inventory['NATIVE_LOGIC_CONTRACTS']['checks'][0]
        native = inventory['NATIVE_LOGIC_RUNTIME']['checks'][0]
        self.assertEqual(portable['command'][-1], 'tests.test_native_logic_repair_demo')
        self.assertEqual(native['command'][-1], 'tests.test_native_logic_repair_runtime.NativeLogicRuntimeTests.test_fixed_candidate')
        self.assertEqual(len(inventory['NATIVE_LOGIC_RUNTIME']['checks']), 3)
        for check in inventory['NATIVE_LOGIC_RUNTIME']['checks']:
            self.assertEqual(check['timeoutSeconds'], 360)
        self.assertNotIn('native-compile', portable['effects'])
        self.assertIn('native-compile', native['effects'])
        self.assertIn('--goal NATIVE_LOGIC_CONTRACTS', (ROOT / '.github/workflows/portable-tests.yml').read_text())
        workflow = (ROOT / '.github/workflows/ios-sdk-compile.yml').read_text()
        self.assertIn('--goal NATIVE_LOGIC_RUNTIME', workflow)
        self.assertIn('REPRO_NATIVE_LOGIC_OUTPUT', workflow)
        self.assertIn('examples/native-logic-repair/**', workflow)
        runtime = (ROOT / 'tests/test_native_logic_repair_runtime.py').read_text()
        self.assertNotIn('unittest.mock', runtime)
        self.assertNotIn('@unittest.skip', runtime)

    def test_original_and_evaluator_mutation_rejects_result(self):
        for target in (demo.ORACLE, 'reproof/native_logic_repair_demo.py'):
            path = self.root / target
            original = path.read_bytes()
            def mutate(blobs, directory, mode):
                if mode == 'fixed':
                    path.write_bytes(original + b'\n')
                return {'unitTestStub': True}
            try:
                with patch.object(demo, '_toolchain', return_value={}), patch.object(demo, '_observe', side_effect=mutate):
                    with self.assertRaises((RepairError, demo.NativeLogicError)):
                        demo._run_demo(self.root, self.output, 'fixed')
            finally:
                path.write_bytes(original)

    def test_unavailable_swift_reports_rejected_not_skipped_or_passed(self):
        output = io.StringIO()
        with patch.object(demo.sys, 'platform', 'linux'), redirect_stdout(output):
            code = demo.main([])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(report['error']['code'], 'native_toolchain_unavailable')
        self.assertIs(report['baselineDefectReproduced'], False)


class NativeLogicProcessTests(unittest.TestCase):
    def test_command_timeout_stops_owned_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(demo.NativeLogicError) as caught:
                demo._command([sys.executable, '-c', 'import time; time.sleep(10)'], root, root / 'command.log', root, .1)
            self.assertEqual(caught.exception.code, 'command_timeout')

    def test_command_environment_excludes_inherited_secrets(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict('os.environ', {'REPRO_SECRET_TEST': 'not-for-child'}):
            root = Path(temporary).resolve()
            code, raw = demo._command([sys.executable, '-c', 'import os; print("REPRO_SECRET_TEST" in os.environ)'], root, root / 'command.log', root, 5)
            self.assertEqual(code, 0)
            self.assertEqual(raw, b'False\n')

    def test_termination_and_interrupt_signals_cleanup_and_restore_handlers(self):
        for signum in (demo.signal.SIGTERM, demo.signal.SIGINT):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                previous = {sig: demo.signal.getsignal(sig) for sig in (demo.signal.SIGTERM, demo.signal.SIGINT)}
                with self.assertRaises(demo.NativeLogicError) as caught:
                    demo._command([sys.executable, '-c',
                        f'import os,time; os.kill(os.getppid(), {int(signum)}); time.sleep(10)'],
                        root, root / 'command.log', root, 5)
                self.assertEqual(caught.exception.code, 'command_cancelled')
                for sig, handler in previous.items():
                    self.assertIs(demo.signal.getsignal(sig), handler)

    def test_signal_during_final_log_read_cannot_return_success(self):
        real_read = demo.read_regular
        def interrupted_read(*args, **kwargs):
            raw = real_read(*args, **kwargs)
            demo.signal.raise_signal(demo.signal.SIGTERM)
            return raw
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(demo, 'read_regular', side_effect=interrupted_read), self.assertRaises(demo.NativeLogicError) as caught:
                demo._command([sys.executable, '-c', 'print("done")'], root, root / 'command.log', root, 5)
            self.assertEqual(caught.exception.code, 'command_cancelled')

    def test_expired_deadline_rejects_even_already_completed_child(self):
        class Completed:
            returncode = 0
            def poll(self):
                return 0
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(demo.subprocess, 'Popen', return_value=Completed()), \
                    patch.object(demo.time, 'monotonic', side_effect=[0, 1]), \
                    patch.object(demo, '_stop'), self.assertRaises(demo.NativeLogicError) as caught:
                demo._command(['not-executed'], root, root / 'command.log', root, .5)
            self.assertEqual(caught.exception.code, 'command_timeout')

    def test_reaped_group_gets_no_signals(self):
        class Reaped:
            pid = 123456789
            returncode = 0
        with patch.object(demo.os, 'killpg', side_effect=ProcessLookupError) as signal_group:
            demo._stop(Reaped())
        signal_group.assert_called_once_with(Reaped.pid, 0)

    def test_unreaped_group_is_signaled_before_reap_and_not_after(self):
        events = []
        class Owned:
            pid = 123456789
            returncode = None
            def wait(self, timeout):
                events.append('reap')
                self.returncode = -9
        def signal_group(pid, sig):
            events.append(sig)
            if sig == 0:
                raise ProcessLookupError
        with patch.object(demo.os, 'killpg', side_effect=signal_group), patch.object(demo.time, 'sleep'):
            demo._stop(Owned())
        self.assertEqual(events, [demo.signal.SIGTERM, demo.signal.SIGKILL, 'reap', 0])

    def test_version_parser_accepts_observed_driver_prefix_not_arbitrary_text(self):
        version = 'Apple Swift version 6.1.2 (swiftlang-6.1.2.1.2 clang-1700.0.13.5)\nTarget: arm64-apple-macosx15.0'
        for prefix in ('', 'swift-driver version: 1.120.5 ', 'swift-driver version: 1.120.5\n'):
            demo._validate_swift_version(prefix + version)
        for value in ('Swift unavailable', 'error: using fallback\n' + version, version + '\nunknown output'):
            with self.assertRaises(demo.NativeLogicError):
                demo._validate_swift_version(value)

    def test_cleanup_does_not_use_unbounded_wait(self):
        class Unreapable:
            pid = 123456789
            returncode = None
            def wait(self, timeout):
                raise subprocess.TimeoutExpired('owned child', timeout)
        with patch.object(demo.os, 'killpg'), self.assertRaises(demo.NativeLogicError) as caught:
            demo._stop(Unreapable())
        self.assertEqual(caught.exception.code, 'cleanup_unconfirmed')


if __name__ == '__main__':
    unittest.main()
