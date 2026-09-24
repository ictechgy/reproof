import unittest

from reproof.core import ContractError


class GradleIntegrationTests(unittest.TestCase):
    def test_existing_plugin_management_and_plugins_preserve_other_bytes(self):
        from reproof.kotlin_instrumenter import configure_gradle
        settings = ('import java.io.File\n// plugins { is a comment }\n'
                    'pluginManagement { includeBuild("conventions") /* } */ }\n'
                    'rootProject.name = "inventory"\ninclude(":app")\n')
        module = ('// plugins { in a comment }\nplugins { id("com.android.application") }\n'
                  'val example = "plugins { fake }"\n')
        result = configure_gradle(settings, module)
        self.assertEqual(result['settings.gradle.kts'].replace(
            '\n    includeBuild("reproof-build-logic")\n', ''), settings)
        self.assertEqual(result['module.gradle.kts'].replace(
            '\n    id("io.reproof.instrumentation")\n', ''), module)

    def test_absent_blocks_are_inserted_after_imports_before_statements(self):
        from reproof.kotlin_instrumenter import configure_gradle
        result = configure_gradle('import java.io.File\nrootProject.name = "inventory"\n',
                                  'import java.io.File\nval original = 1\n')
        self.assertLess(result['settings.gradle.kts'].index('import '),
                        result['settings.gradle.kts'].index('pluginManagement {'))
        self.assertLess(result['settings.gradle.kts'].index('pluginManagement {'),
                        result['settings.gradle.kts'].index('rootProject.name'))
        self.assertLess(result['module.gradle.kts'].index('plugins {'),
                        result['module.gradle.kts'].index('val original'))

    def test_malformed_duplicate_or_reserved_integration_fails_closed(self):
        from reproof.kotlin_instrumenter import configure_gradle
        for settings, module in [
            ('pluginManagement {', ''),
            ('pluginManagement {}\npluginManagement {}', ''),
            ('', 'plugins {}\nplugins {}'),
            ('', 'plugins(something) {}'),
            ('includeBuild("reproof-build-logic")', ''),
            ('', 'plugins { id("io.reproof.instrumentation") }'),
        ]:
            with self.subTest(settings=settings, module=module), self.assertRaises(ContractError):
                configure_gradle(settings, module)


if __name__ == '__main__':
    unittest.main()
