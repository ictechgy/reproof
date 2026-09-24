from pathlib import Path
import tempfile
import unittest

from reproof.core import ContractError


def profile_document():
    return {
        'schemaVersion': 1, 'id': 'inventory', 'package': 'io.reproof.inventory',
        'activity': '.MainActivity',
        'fixture': {'id': 'empty_inventory', 'version': 1, 'inputs': {}},
        'startState': {'screen': 'inventory', 'nodes': {'quantity': '0', 'label': ''}},
        'targets': {'tap': ['commit'], 'text': ['label'], 'numeric': ['quantity'],
                    'scroll': {}, 'back': 'go_back', 'report': 'export_capture'},
        'oracle': {'bugCondition': {'target': 'quantity', 'text': '2'},
                   'expectedCondition': {'target': 'quantity', 'text': '1'}},
        'build': {'task': ':app:assembleDebug', 'apk': 'app/build/outputs/apk/debug/app-debug.apk',
                  'regressionTask': ':app:testDebugUnitTest',
                  'regressionResults': 'app/build/test-results/testDebugUnitTest'},
        'edit': {'kind': 'kotlin_numeric_expression_v1', 'path': 'app/src/main/java/example/Stock.kt',
                 'function': 'unitsPerItem'},
    }


class AppProfileTests(unittest.TestCase):
    def test_explicit_profile_is_validated_and_immutable(self):
        from reproof.android_profile import validate_app_profile
        document = profile_document()
        profile = validate_app_profile(document)
        original_digest = profile.digest
        document['package'] = 'unexpected.package'
        view = profile.data
        view['targets']['tap'].append('unexpected')
        self.assertEqual(profile.data['package'], 'io.reproof.inventory')
        self.assertEqual(profile.data['targets']['tap'], ['commit'])
        self.assertEqual(profile.digest, original_digest)
        self.assertNotIn('build', profile.native())
        self.assertNotIn('edit', profile.native())

    def test_rejects_executable_paths_unknown_fields_and_private_targets(self):
        from reproof.android_profile import validate_app_profile
        mutations = [
            lambda d: d.update(command='arbitrary shell'),
            lambda d: d.update(package='other; command'),
            lambda d: d.update(activity='../OtherActivity'),
            lambda d: d['edit'].update(path='../product.kt'),
            lambda d: d['edit'].update(path='app/src/test/Test.kt'),
            lambda d: d['build'].update(task=':app:assembleDebug --init-script /tmp/inject'),
            lambda d: d['build'].update(regressionTask=':app:publish'),
            lambda d: d['build'].update(regressionResults='app/src/main/reports'),
            lambda d: d['build'].update(apk='/tmp/unbound.apk'),
            lambda d: d['edit'].update(path='build-logic/src/main/kotlin/Plugin.kt'),
            lambda d: d['edit'].update(path='other/src/main/java/Stock.kt'),
            lambda d: d['targets']['text'].append('password'),
            lambda d: d['targets']['numeric'].append('label'),
            lambda d: d['oracle']['bugCondition'].update(target='unconfigured'),
            lambda d: d['startState']['nodes'].update(label='not-approved-user-value'),
            lambda d: d['fixture'].update(inputs={'shell': 'untrusted'}),
        ]
        for mutate in mutations:
            document = profile_document()
            mutate(document)
            with self.subTest(document=document), self.assertRaises(ContractError):
                validate_app_profile(document)

    def test_profile_digest_changes_with_oracle_or_action_scope(self):
        from reproof.android_profile import validate_app_profile
        original = validate_app_profile(profile_document())
        document = profile_document()
        document['oracle']['expectedCondition']['text'] = '3'
        self.assertNotEqual(original.digest, validate_app_profile(document).digest)
        document = profile_document()
        document['targets']['tap'].append('cancel')
        self.assertNotEqual(original.digest, validate_app_profile(document).digest)

    def test_loader_rejects_linked_configuration(self):
        from reproof.android_profile import load_app_profile
        from reproof.storage import write_json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / 'profile.json', profile_document())
            (root / 'linked.json').symlink_to(root / 'profile.json')
            with self.assertRaises(ContractError):
                load_app_profile(root / 'linked.json')

    def test_activity_is_a_flattened_android_component(self):
        from reproof.android_profile import validate_app_profile
        for value, expected in [('.MainActivity', 'io.reproof.inventory/.MainActivity'),
                                ('MainActivity', 'io.reproof.inventory/.MainActivity'),
                                ('io.reproof.inventory.MainActivity', 'io.reproof.inventory/.MainActivity'),
                                ('shared.Entry', 'io.reproof.inventory/shared.Entry')]:
            document = profile_document(); document['activity'] = value
            self.assertEqual(validate_app_profile(document).component_name, expected)


if __name__ == '__main__':
    unittest.main()
