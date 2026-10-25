"""Portable adversarial contracts only. Synthetic XML is NOT native proof."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof import android_logic_repair_demo as demo
from reproof.execution.artifacts import BlobSet
from reproof.project_repair import RepairError

ROOT = Path(__file__).resolve().parents[1]


def result(mode='fixed'):
    failing = mode != 'fixed'
    actual = 2 if mode == 'baseline' else 3
    failure = '' if not failing else f'''<failure message="java.lang.AssertionError: expected:&lt;1&gt; but was:&lt;{actual}&gt;" type="java.lang.AssertionError">java.lang.AssertionError: expected:&lt;1&gt; but was:&lt;{actual}&gt;
    at org.junit.Assert.fail(Assert.java:89)
    at io.reproof.sample.CounterLogicTest.addingOneItemIncrementsCountByOne(CounterLogicTest.kt:9)
</failure>'''
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="io.reproof.sample.CounterLogicTest" tests="1" skipped="0" failures="{int(failing)}" errors="0" timestamp="2026-01-01T00:00:00" hostname="host" time="0.01">
<properties/>
<testcase name="addingOneItemIncrementsCountByOne" classname="io.reproof.sample.CounterLogicTest" time="0.01">{failure}</testcase>
<system-out><![CDATA[]]></system-out><system-err><![CDATA[]]></system-err>
</testsuite>'''.encode()
    log = '''> Task :sample:compileBuggyDebugKotlin
> Task :sample:compileBuggyDebugUnitTestKotlin
> Task :sample:testBuggyDebugUnitTest'''
    if failing:
        log += " FAILED\n1 test completed, 1 failed\nExecution failed for task ':sample:testBuggyDebugUnitTest'.\n> There were failing tests.\nBUILD FAILED in 2s\n"
    else:
        log += '\nBUILD SUCCESSFUL in 2s\n'
    return xml, log.encode(), int(failing)


class AndroidLogicContractTests(unittest.TestCase):
    def validate(self, mode='fixed', transform=lambda value: value):
        return demo._validate_result(*transform(result(mode)), mode)

    def test_exact_three_stage_observations(self):
        for mode, actual in (('baseline', 2), ('wrong', 3), ('fixed', None)):
            with self.subTest(mode=mode):
                report = self.validate(mode)
                self.assertEqual((report['tests'], report['failures'], report['skipped']), (1, int(mode != 'fixed'), 0))
                self.assertEqual(report['expectedActual'], actual)

    def test_invalid_xml_and_entities_rejected(self):
        for raw in (b'', b'<testsuite>', b'<!DOCTYPE x><testsuite/>', b'<!ENTITY x "y"><testsuite/>', b'x' * 65537):
            with self.subTest(raw=raw[:30]), self.assertRaises(demo.NativeLogicError):
                demo._validate_result(raw, result()[1], 0, 'fixed')

    def test_zero_multiple_skipped_errors_wrong_identity(self):
        edits = [(b'tests="1"', b'tests="0"'), (b'tests="1"', b'tests="2"'),
                 (b'skipped="0"', b'skipped="1"'), (b'errors="0"', b'errors="1"'),
                 (b'failures="0"', b'failures="1"'),
                 (b'name="addingOneItemIncrementsCountByOne"', b'name="other"'),
                 (b'classname="io.reproof.sample.CounterLogicTest"', b'classname="other"'),
                 (b'</testcase>', b'<skipped/></testcase>'),
                 (b'</testcase>', b'</testcase><testcase/>'),
                 (b'time="0.01"', b'time="NaN"'),
                 (b'<properties/>', b'<properties><property/></properties>'),
                 (b'<![CDATA[]]>', b'<![CDATA[unexpected]]>')]
        for old, new in edits:
            with self.subTest(edit=new), self.assertRaises(demo.NativeLogicError):
                self.validate(transform=lambda r: (r[0].replace(old, new), r[1], r[2]))

    def test_unrelated_or_wrong_failure_rejected(self):
        for old, new in ((b'was:&lt;2&gt;', b'was:&lt;3&gt;'),
                         (b'java.lang.AssertionError', b'java.lang.RuntimeException'),
                         (b'CounterLogicTest.kt:9', b'CounterLogicTest.kt:10'),
                         (b'<failure ', b'<error '),
                         (b'</failure>', b'Caused by: java.lang.OutOfMemoryError\n</failure>'),
                         (b'</failure>', b'</failure><failure/>')):
            with self.subTest(edit=new), self.assertRaises(demo.NativeLogicError):
                self.validate('baseline', lambda r: (r[0].replace(old, new), r[1], r[2]))

    def test_exit_code_must_match(self):
        for mode in ('baseline', 'wrong', 'fixed'):
            for code in (-9, 2, True, 1 if mode == 'fixed' else 0):
                with self.subTest(mode=mode, code=code), self.assertRaises(demo.NativeLogicError):
                    self.validate(mode, lambda r: (r[0], r[1], code))

    def test_cached_skipped_missing_and_failed_compilation_rejected(self):
        for task in (b':sample:compileBuggyDebugKotlin', b':sample:compileBuggyDebugUnitTestKotlin', demo.TASK.encode()):
            for suffix in (b' UP-TO-DATE', b' FROM-CACHE', b' SKIPPED', b' NO-SOURCE', b' FAILED'):
                with self.subTest(task=task, suffix=suffix), self.assertRaises(demo.NativeLogicError):
                    self.validate(transform=lambda r: (r[0], r[1].replace(task+b'\n', task+suffix+b'\n'), r[2]))
            with self.assertRaises(demo.NativeLogicError):
                self.validate(transform=lambda r: (r[0], r[1].replace(b'> Task '+task+b'\n', b''), r[2]))

    def test_unrelated_task_errors_and_truncation_rejected(self):
        for extra in (b'> Task :sdk:compileDebugKotlin FAILED\n', b'e: syntax error\n',
                      b'Could not resolve dependencies\n', b'No tests found\n',
                      b'BUILD FAILED in 2s\n', b'> Task :sample:testBuggyDebugUnitTest\n'):
            with self.subTest(extra=extra), self.assertRaises(demo.NativeLogicError):
                self.validate(transform=lambda r: (r[0], r[1]+extra, r[2]))
        with self.assertRaises(demo.NativeLogicError):
            self.validate(transform=lambda r: (r[0], r[1].split(b'BUILD')[0], r[2]))

    def test_only_exact_configuration_diagnostic_skips_permitted(self):
        rows = b''.join(f'> Task :{module}:checkKotlinGradlePluginConfigurationErrors SKIPPED\n'.encode()
                        for module in ('sample', 'sdk'))
        for mode in ('baseline', 'wrong', 'fixed'):
            self.validate(mode, lambda r: (r[0], rows + r[1], r[2]))
        for invalid in (rows + rows, rows.replace(b':sdk:', b':other:'),
                        rows.replace(b'Errors SKIPPED', b'ErrorsExtra SKIPPED'),
                        b'> Task :sdk:compileDebugKotlin SKIPPED\n',
                        b'SKIPPED\n', b'prefix ' + rows):
            with self.subTest(invalid=invalid), self.assertRaises(demo.NativeLogicError):
                self.validate(transform=lambda r: (r[0], invalid + r[1], r[2]))

    def test_expected_failure_summary_required(self):
        for old, new in ((b'1 test completed, 1 failed', b'0 tests completed, 1 failed'),
                         (b'1 test completed, 1 failed', b'1 test completed, 1 failed, 1 skipped'),
                         (b'There were failing tests.', b'compile failed')):
            with self.subTest(edit=new), self.assertRaises(demo.NativeLogicError):
                self.validate('baseline', lambda r: (r[0], r[1].replace(old, new), r[2]))

    def test_expected_failure_allows_only_single_terminal_or_start_then_terminal(self):
        row = b'> Task ' + demo.TASK.encode()
        for mode in ('baseline', 'wrong'):
            self.validate(mode, lambda r: (r[0], r[1].replace(row + b' FAILED\n',
                row + b'\n' + row + b' FAILED\n'), r[2]))
            for suffixes in ([], [''], [' FAILED', ''], ['', ''],
                             [' FAILED', ' FAILED'], ['', '', ' FAILED'],
                             ['', ' FAILED', ' FAILED'], ['', ' FAILED', ''],
                             [' SKIPPED', ' FAILED'], [' FROM-CACHE', ' FAILED'],
                             [' UP-TO-DATE', ' FAILED'], [' NO-SOURCE', ' FAILED'],
                             ['', ' FAILED extra'], [' extra', ' FAILED']):
                replacement = b''.join(row + suffix.encode() + b'\n' for suffix in suffixes)
                with self.subTest(mode=mode, suffixes=suffixes), self.assertRaises(demo.NativeLogicError):
                    self.validate(mode, lambda r: (r[0], r[1].replace(row + b' FAILED\n',
                        replacement), r[2]))
            with self.assertRaises(demo.NativeLogicError):
                self.validate(mode, lambda r: (r[0], r[1].replace(row + b' FAILED\n',
                    row + b'\n' + row + b' FAILED\n') + b'> Task :sdk:compileDebugKotlin FAILED\n', r[2]))
        with self.assertRaises(demo.NativeLogicError):
            self.validate('fixed', lambda r: (r[0], r[1].replace(row + b'\n',
                row + b'\n' + row + b' FAILED\n'), r[2]))

    def test_pinned_source_and_real_callsite(self):
        source, frozen = demo._load_source(ROOT)
        entries = dict(frozen.entries)
        self.assertIn(b'count += CounterLogic.increment()', entries[demo.CALLSITE])
        self.assertIn(b'assertEquals(1, CounterLogic.increment())', entries[demo.ORACLE])
        self.assertEqual(source.project['editablePaths'], [demo.PRODUCT])
        self.assertIn(b'distributionSha256Sum=', entries['android/gradle/wrapper/gradle-wrapper.properties'])

    def test_all_build_and_oracle_inputs_protected(self):
        source, frozen = demo._load_source(ROOT)
        for path, _ in frozen.entries:
            if path != demo.PRODUCT:
                with self.subTest(path=path), self.assertRaises(RepairError) as raised:
                    source.apply(frozen, [{'path': path, 'old': 'x', 'new': 'y'}])
                self.assertEqual(raised.exception.code, 'protected_path')

    def test_only_finite_product_expression_changes(self):
        source, frozen = demo._load_source(ROOT)
        for value in ('1', '3'):
            changed = source.apply(frozen, [{'path': demo.PRODUCT, 'old': 'if (BuildConfig.BUGGY) 2 else 1',
                'new': f'if (BuildConfig.BUGGY) {value} else 1'}])
            self.assertNotEqual(frozen.digest, changed.digest)
            self.assertEqual([p for p, raw in changed.entries if dict(frozen.entries)[p] != raw], [demo.PRODUCT])

    def test_source_pin_mutation_rejected(self):
        _, frozen = demo._load_source(ROOT)
        for target in (demo.PRODUCT, demo.CALLSITE, demo.ORACLE, 'android/sample/build.gradle.kts', demo.RECIPE):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                entries = frozen.entries + ((demo.PIN_FILE, (ROOT/demo.PIN_FILE).read_bytes()),)
                BlobSet(entries).write_new(root/'input')
                path = root/'input'/target
                path.write_bytes(path.read_bytes()+b'\n')
                with self.assertRaises(demo.NativeLogicError):
                    demo._load_source(root/'input')

    def test_synthetic_ticket_binding_not_authority(self):
        source, frozen = demo._load_source(ROOT)
        draft = demo._draft(source, frozen)
        self.assertEqual(draft['status'], 'review-required')
        self.assertFalse(draft['approved'])
        self.assertFalse(draft['aiTransfer'])
        self.assertEqual(draft['executionAuthority'], 'none')
        self.assertEqual(draft['references']['sourceDigest'], frozen.digest)

    def test_noop_never_reaches_toolchain_or_compilation(self):
        with patch.object(demo, '_toolchain', side_effect=AssertionError('native prohibited')), \
             patch.object(demo, '_observe', side_effect=AssertionError('native prohibited')):
            report = demo.run_demo('noop')
        self.assertEqual(report['error']['code'], 'patch_invalid')
        self.assertFalse(report['baselineDefectReproduced'])
        self.assertIsNone(report['baseline'])
        self.assertIsNone(report['candidate'])

    def test_runtime_requires_retained_evidence_directory(self):
        for candidate in ('fixed', 'wrong'):
            with self.assertRaises(demo.NativeLogicError) as raised:
                demo.run_demo(candidate)
            self.assertEqual(raised.exception.code, 'evidence_output_required')

    def test_existing_output_and_invalid_selection_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(demo.NativeLogicError):
                demo.run_demo('fixed', output=temporary)
        with self.assertRaises(demo.NativeLogicError):
            demo.run_demo('arbitrary')

    def test_sanitized_environment(self):
        env = demo._environment('/jdk', '/sdk', Path('/home'))
        self.assertEqual(set(env), {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'JAVA_HOME',
                                   'ANDROID_HOME', 'ANDROID_USER_HOME', 'GRADLE_USER_HOME'})
        self.assertEqual(env['GRADLE_USER_HOME'], '/home/gradle')

    def test_claims_are_bounded(self):
        claims = demo._claims()
        for key in ('sdkCapture', 'scenarioReplay', 'mobileUI', 'deviceExecution', 'actualAI',
                    'externalAI', 'companyAcceptance', 'protectedVerification', 'sandbox', 'credentialsUsed'):
            self.assertIs(claims[key], False)


if __name__ == '__main__':
    unittest.main()
