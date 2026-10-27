import importlib.util
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fixture_junit_check', ROOT/'scripts/check-android-fixture-junit.py')
validator = importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)


class FixtureJvmEvidenceTests(unittest.TestCase):
    def reports(self, root):
        paths=[]
        for name, count in validator.SUITES.items():
            suite=ET.Element('testsuite', name=name, tests=str(count), failures='0', errors='0', skipped='0')
            for n in range(count):ET.SubElement(suite, 'testcase', name=f'case_{n}', classname=name)
            path=root/f'TEST-{name}.xml';ET.ElementTree(suite).write(path);paths.append(path)
        return paths

    def test_actual_non_skipped_case_counts_required(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths=self.reports(root)
            self.assertEqual(validator.verify(root),10)
            for tag in ('failure','error','skipped'):
                paths=self.reports(root);tree=ET.parse(paths[0]);ET.SubElement(tree.getroot().find('testcase'),tag);tree.write(paths[0])
                with self.subTest(tag=tag), self.assertRaises(ValueError):validator.verify(root)
            paths=self.reports(root);tree=ET.parse(paths[0]);tree.getroot().remove(tree.getroot().find('testcase'));tree.write(paths[0])
            with self.assertRaises(ValueError):validator.verify(root)

    def test_missing_duplicate_wrong_class_or_nonzero_summary_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths=self.reports(root);paths[0].unlink()
            with self.assertRaises(ValueError):validator.verify(root)
            for mutate in (lambda s:s.set('skipped','1'), lambda s:s.set('failures','1'),
                           lambda s:s.set('errors','1'), lambda s:s.find('testcase').set('classname','wrong'),
                           lambda s:s.findall('testcase')[1].set('name','case_0')):
                paths=self.reports(root);tree=ET.parse(paths[0]);mutate(tree.getroot());tree.write(paths[0])
                with self.assertRaises(ValueError):validator.verify(root)

    def test_workflow_executes_sdk_jvm_tests_and_checks_exact_reports_without_device_tasks(self):
        workflow=(ROOT/'.github/workflows/android-compile.yml').read_text()
        self.assertIn(':sdk:testDebugUnitTest',workflow)
        self.assertIn('python3 scripts/check-android-fixture-junit.py android/sdk/build/test-results/testDebugUnitTest',workflow)
        self.assertIn('actions/upload-artifact@v4',workflow)
        for forbidden in ('connectedAndroidTest','connectedDebugAndroidTest','managedDevice','adb ','emulator ','signingConfig'):
            self.assertNotIn(forbidden,workflow)

    def test_native_authority_generation_is_widened_not_truncated(self):
        # Source wiring regression, not a substitute for helper compilation.
        helper=(ROOT/'android/live/src/main/java/io/reproof/live/LiveInstrumentation.kt').read_text()
        wire=(ROOT/'android/native-common/src/main/java/io/reproof/fixture/FixtureWire.kt').read_text()
        self.assertIn('val ownershipGeneration: Long', wire)
        self.assertIn('val ownershipGeneration: Int', helper)
        self.assertIn('challenge.ownershipGeneration != grant.ownershipGeneration.toLong()', helper)
        self.assertNotIn('challenge.ownershipGeneration.toInt()', helper)
        self.assertIn('exactInt(value, "ownershipGeneration") ?: return null', helper)
