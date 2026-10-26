"""Cross-feature preparation with synthetic inputs; no compiler or device."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from reproof.core import ContractError
from reproof.ios_instrumentation import prepare_ios_instrumentation, validate_ios_auto_profile
from reproof.android_observation import validate_observation_profile
from reproof.build_instrumentation import prepare_build_instrumentation
from reproof.instrumentation import validate_instrumented_source
from reproof.storage import read_json
from tests.test_ios_workspace import workspace_project
from tests import test_groovy_observation as groovy_support


class EnterpriseBuildInputTests(unittest.TestCase):
    def test_workspace_and_unicode_extensions_prepare_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root / 'original'
            document = workspace_project(source)
            name = 'App/효과.metal'
            (source / name).write_bytes(b'inert shader asset')
            document['sourceInputs'].append(name)
            document['inputExtensions'] = ['.metal']
            profile = validate_ios_auto_profile(document)
            result = prepare_ios_instrumentation(source, root / 'prepared', profile=profile)
            prepared = Path(result['source'])
            self.assertEqual((prepared / name).read_bytes(), (source / name).read_bytes())
            self.assertEqual((prepared / 'Inventory.xcworkspace/contents.xcworkspacedata').read_bytes(),
                             (source / 'Inventory.xcworkspace/contents.xcworkspacedata').read_bytes())
            self.assertFalse(result['behaviorVerified'])
            del document['inputExtensions']
            with self.assertRaises(ContractError): validate_ios_auto_profile(document)

    def test_groovy_unicode_source_and_extensions_keep_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root / 'original'
            document = groovy_support.GroovyObservationTests().fixture(source)
            old = 'app/src/main/java/example/MainActivity.kt'
            new = 'app/src/main/java/example/화면.kt'
            (source / old).rename(source / new)
            document['sourceInputs'].remove(old); document['sourceInputs'].append(new)
            asset = 'app/src/main/assets/목록.js'
            (source / asset).write_bytes(b'inert web asset\x00')
            document['sourceInputs'].append(asset); document['inputExtensions'] = ['.js']
            site = {'id': 's123abc', 'path': new, 'line': 12, 'target': 'save', 'kind': 'tap'}
            analyzer = Mock(return_value={'schemaVersion': 1, 'activityPath': new, 'sites': [site]})
            before = {name: (source / name).read_bytes() for name in document['sourceInputs']}
            with patch('reproof.kotlin_instrumenter.configure_gradle', side_effect=AssertionError('wrong DSL')):
                result = prepare_build_instrumentation(source, validate_observation_profile(document),
                                                       root / 'prepared', analyzer=analyzer)
            profile = validate_observation_profile(read_json(result['appProfile']))
            receipt = validate_instrumented_source(result['source'], profile)
            self.assertEqual(profile.data['build']['dsl'], 'groovy')
            self.assertEqual(profile.data['inputExtensions'], ['.js'])
            self.assertEqual(receipt['buildIntegration'], 'groovy-insertion-v1')
            self.assertFalse(receipt['behaviorVerified'])
            for name, raw in before.items(): self.assertEqual((source / name).read_bytes(), raw)
            self.assertEqual((result['source'] / asset).read_bytes(), before[asset])
            (result['source'] / asset).write_bytes(b'tampered')
            with self.assertRaises(ContractError): validate_instrumented_source(result['source'], profile)
            document['sourceInputs'].append('settings.gradle.kts')
            with self.assertRaises(ContractError): validate_observation_profile(document)

    def test_swift_optional_fields_are_combined_not_replaced(self):
        # Source wiring only; native parser execution remains separately qualified.
        source = (Path(__file__).resolve().parents[1] /
                  'reproof/ios_instrumentation_templates/RLAutomaticRecorder.swift').read_text()
        self.assertIn('requiredKeys.union(["workspace", "inputExtensions"])', source)
        self.assertIn('value.utf8.elementsEqual(value.precomposedStringWithCanonicalMapping.utf8)', source)
        self.assertIn('inputs.contains(workspace + "/contents.xcworkspacedata")', source)
