import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from reproof.core import ContractError
from reproof.groovy_integration import configure_groovy
from reproof.android_observation import validate_observation_profile
from reproof.android_sources import freeze_source
from reproof.build_instrumentation import prepare_build_instrumentation
from reproof.instrumentation import validate_instrumented_source
from reproof.storage import read_json
from tests.test_android_observation import ordinary_views_project


class GroovyObservationTests(unittest.TestCase):
    def test_insertions_preserve_all_original_bytes_and_repository_rules(self):
        settings = ("// pluginManagement { fake }\npluginManagement /* comment */ {\n"
                    " repositories { google(); mavenCentral() }\n}\n"
                    "dependencyResolutionManagement {\n repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)\n"
                    " repositories { google() }\n}\ninclude ':app'\n")
        module = ("/* plugins {fake} */\nplugins { id 'com.android.application' }\n"
                  "android { namespace 'com.example.inventory'; defaultConfig { minSdk 26 } }\n"
                  "// literal braces\nversion = 'plugins { \\\'quoted\\\' }'\n")
        result = configure_groovy(settings, module)
        self.assertEqual(result['settings.gradle'].replace("\n    includeBuild('reproof-build-logic')\n", ''), settings)
        self.assertEqual(result['module.gradle'].replace("\n    id 'io.reproof.instrumentation'\n", ''), module)

    def test_absent_settings_block_is_prepended_without_rewriting(self):
        settings = "// root config\nrootProject.name = 'example'\ninclude(':app')\n"
        result = configure_groovy(settings, "plugins { id('com.android.application') }\n")
        self.assertTrue(result['settings.gradle'].endswith(settings))
        self.assertTrue(result['settings.gradle'].startswith('pluginManagement {'))

    def test_unsupported_and_ambiguous_syntax_is_rejected(self):
        cases = ["apply plugin: 'com.android.application'", "plugins(x) {}", "x.plugins {}", "plugins {}\nplugins {}",
                 "; plugins {}", "@Annotation plugins {}", "plugins {}\napply from: 'other.gradle'",
                 'plugins {}\ntext = "${other}"', "plugins {}\ntext = /abc/", "plugins {}\ntext = $/abc/$",
                 "plugins {}\ntext = '''abc'''", "plugins {}\ntext = '\\u007b'", "plugins {}\ntext = '\\n'",
                 "plugins {}\ntext = 'unterminated", "plugins {", "plugins {[)}", "plugins {}\nimport x.y",
                 "plugins {}\n/* unclosed", "plugins {}\n/* outer /* inner */ */", "plugins {}\r\n",
                 "plugins {}\nincludeBuild('other')", "plugins {}\ntext = 'io.reproof.instrumentation'",
                 "plugins {}\nclass Thing {}", "plugins {}\nmetaClass.foo = 1", "plugins {}\nvalue = `abc`", "plugins {}\nthis.'apply'(from: 'other.gradle')",
                 "plugins {}\nthis.'includeBuild'('other')", "plugins {}\nname = 'metaClass'",
                 "plugins {}\nthis.'unknown'()"]
        for module in cases:
            with self.subTest(module=module), self.assertRaises(ContractError):
                configure_groovy("include ':app'", module)
        for settings in ["rootProject.name = 'x'\npluginManagement {}", "pluginManagement {}\npluginManagement {}",
                         "pluginManagement(x) {}", "pluginManagement { includeBuild('other') }", "includeBuild('other')"]:
            with self.subTest(settings=settings), self.assertRaises(ContractError):
                configure_groovy(settings, 'plugins {}')

    def test_bounds_and_wrong_types_reject(self):
        for settings in [None, b'abc', ' ' * (4 * 1024 * 1024 + 1), '{' * 129 + '}' * 129,
                         'x;' * 100001, '\x00', '\ud800']:
            with self.subTest(kind=type(settings)), self.assertRaises(ContractError):
                configure_groovy(settings, 'plugins {}')

    def fixture(self, root):
        doc = ordinary_views_project(root)
        for name in ['settings.gradle.kts', 'app/build.gradle.kts']:
            path = root / name
            path.rename(root / name[:-4])
            doc['sourceInputs'].remove(name)
            doc['sourceInputs'].append(name[:-4])
        (root / 'settings.gradle').write_text("pluginManagement { repositories { google() } }\ninclude ':app'\n")
        (root / 'app/build.gradle').write_text("plugins { id 'com.android.application' }\nandroid { namespace 'com.example.inventory' }\n")
        doc['sourceInputs'].sort()
        doc['build']['dsl'] = 'groovy'
        return doc

    def test_profile_requires_explicit_paired_nonambiguous_dsl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = self.fixture(root)
            self.assertEqual(validate_observation_profile(doc).data['build']['dsl'], 'groovy')
            invalid = []
            for dsl in [None, [], {}, 1, True, 'Groovy', 'auto', 'kotlin']:
                value = copy.deepcopy(doc); value['build']['dsl'] = dsl; invalid.append(value)
            value = copy.deepcopy(doc); del value['build']['dsl']; invalid.append(value)
            for extra in ['settings.gradle.kts', 'app/build.gradle.kts', '../settings.gradle']:
                value = copy.deepcopy(doc); value['sourceInputs'].append(extra); invalid.append(value)
            for value in invalid:
                with self.subTest(value=value['build']), self.assertRaises(ContractError):
                    validate_observation_profile(value)

    def test_preparation_changes_only_selected_build_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'original'; doc = self.fixture(source)
            before = freeze_source(source, doc['sourceInputs'])
            site = {'id': 's123abc', 'path': 'app/src/main/java/example/MainActivity.kt',
                    'line': 12, 'target': 'save', 'kind': 'tap'}
            analyzer = Mock(return_value={'schemaVersion': 1, 'activityPath': site['path'], 'sites': [site]})
            with patch('reproof.kotlin_instrumenter.configure_gradle', side_effect=AssertionError('Kotlin DSL path')):
                result = prepare_build_instrumentation(source, validate_observation_profile(doc), root / 'prepared', analyzer=analyzer)
            prepared = validate_observation_profile(read_json(result['appProfile']))
            receipt = validate_instrumented_source(result['source'], prepared)
            self.assertEqual(receipt['buildIntegration'], 'groovy-insertion-v1')
            self.assertFalse(receipt['behaviorVerified'])
            self.assertEqual(before, freeze_source(source, doc['sourceInputs']))
            for name, raw in before.entries:
                if name not in {'settings.gradle', 'app/build.gradle'}:
                    self.assertEqual((result['source'] / name).read_bytes(), raw)
            self.assertEqual(prepared.data['build']['dsl'], 'groovy')
            self.assertEqual(read_json(result['source'] / 'app/reproof-instrumentation/assets/reproof-observation.json'), prepared.data)
            (result['source'] / 'settings.gradle').write_text('changed')
            with self.assertRaises(ContractError):
                validate_instrumented_source(result['source'], prepared)

    def test_unsupported_build_rejects_before_any_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'original'; doc = self.fixture(source)
            (source / 'app/build.gradle').write_text("apply plugin: 'com.android.application'\n")
            before = freeze_source(source, doc['sourceInputs'])
            site = {'id': 's123abc', 'path': 'app/src/main/java/example/MainActivity.kt', 'line': 12, 'target': 'save', 'kind': 'tap'}
            analyzer = Mock(return_value={'schemaVersion': 1, 'activityPath': site['path'], 'sites': [site]})
            with self.assertRaises(ContractError):
                prepare_build_instrumentation(source, validate_observation_profile(doc), root / 'out', analyzer=analyzer)
            self.assertFalse((root / 'out').exists())
            self.assertEqual(before, freeze_source(source, doc['sourceInputs']))
