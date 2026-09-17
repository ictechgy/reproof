"""Configured UIKit preparation preserves real public build inputs."""
import copy
from contextlib import nullcontext
import json
import os
from pathlib import Path
import plistlib
import shutil
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

from reproloop.core import ContractError
from reproloop.ios_instrumentation import (
    prepare_ios_instrumentation, profile_from_source, sample_ios_auto_profile,
    validate_ios_auto_profile, validate_ios_preparation,
)


def ordinary_project(root):
    files = {
        'App/InventoryView.swift': b'import UIKit\nfinal class InventoryView: UIViewController {}\n',
        'App/Info.plist': plistlib.dumps({'CFBundleIdentifier': '$(PRODUCT_BUNDLE_IDENTIFIER)',
            'CFBundleExecutable': '$(EXECUTABLE_NAME)', 'CFBundleShortVersionString': '1.0',
            'CFBundleVersion': '1'}),
        'App/Assets.xcassets/Contents.json': b'{"info":{"version":1,"author":"xcode"}}\n',
        'App/Assets.xcassets/Logo.imageset/Contents.json': b'{"images":[],"info":{"version":1,"author":"xcode"}}\n',
        'App/Assets.xcassets/Logo.imageset/logo.png': b'OWNED_BINARY_ASSET\x00\xff',
        'App/en.lproj/Localizable.strings': b'"save" = "Save";\n',
        'Config/App.xcconfig': b'SWIFT_VERSION = 5.0\n',
        'Inventory.xcodeproj/xcshareddata/xcschemes/Inventory.xcscheme': b'<Scheme version="1.7"/>\n',
    }
    objects = {
        'PROJECT': {'isa': 'PBXProject', 'mainGroup': 'GROUP', 'targets': ['APP']},
        'GROUP': {'isa': 'PBXGroup', 'children': [], 'sourceTree': '<group>'},
        'APP': {'isa': 'PBXNativeTarget', 'name': 'Inventory',
            'productType': 'com.apple.product-type.application',
            'buildConfigurationList': 'CONFIGS', 'buildPhases': ['SOURCES']},
        'SOURCES': {'isa': 'PBXSourcesBuildPhase', 'files': []},
        'CONFIGS': {'isa': 'XCConfigurationList', 'buildConfigurations': ['DEBUG', 'RELEASE', 'STORE']},
    }
    for key, name in [('DEBUG', 'QA Debug'), ('RELEASE', 'Release'), ('STORE', 'App Store')]:
        objects[key] = {'isa': 'XCBuildConfiguration', 'name': name,
            'buildSettings': {'PRODUCT_BUNDLE_IDENTIFIER': 'com.example.inventory',
                'INFOPLIST_FILE': 'App/Info.plist', 'OTHER_SWIFT_FLAGS': ['$(inherited)', '-DPRODUCT_FLAG']}}
    files['Inventory.xcodeproj/project.pbxproj'] = plistlib.dumps({
        'archiveVersion': '1', 'objectVersion': '56', 'rootObject': 'PROJECT', 'objects': objects})
    for name, raw in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    return {'schemaVersion': 2, 'kind': 'uikit-observation-v2',
        'applicationId': 'com.example.inventory', 'project': 'Inventory.xcodeproj', 'target': 'Inventory',
        'build': {'scheme': 'Inventory', 'product': 'Inventory', 'infoPlist': 'App/Info.plist',
                  'debugConfiguration': 'QA Debug'},
        'sourceInputs': sorted(files), 'tapTargets': ['inventory.save', 'inventory.close'],
        'screenTargets': {'inventory.editor': 'editor', 'inventory.saved': 'saved'}}


class ConfiguredIosObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='repro-ios-observation-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        self.document = ordinary_project(self.source)

    def test_configured_profile_accepts_ordinary_ids_without_widening_legacy(self):
        selected = validate_ios_auto_profile(self.document)
        self.assertEqual(selected.data['applicationId'], 'com.example.inventory')
        legacy = sample_ios_auto_profile().data
        legacy['applicationId'] = self.document['applicationId']
        with self.assertRaises(ContractError): validate_ios_auto_profile(legacy)
        self.assertNotIn('cases', selected.data)
        self.assertNotIn('startState', selected.data)

    def test_preparation_copies_selected_binary_and_localized_inputs_byte_for_byte(self):
        original = {name: (self.source / name).read_bytes() for name in self.document['sourceInputs']}
        (self.source / '.env').write_text('OWNED_EXCLUDED_MARKER')
        (self.source / 'private.json').write_text('OWNED_EXCLUDED_MARKER')
        result = prepare_ios_instrumentation(self.source, self.root / 'prepared', profile=self.document)
        prepared = Path(result['source'])
        for name, raw in original.items():
            self.assertEqual((self.source / name).read_bytes(), raw)
            if not name.endswith('project.pbxproj'):
                self.assertEqual((prepared / name).read_bytes(), raw)
        self.assertFalse((prepared / '.env').exists()); self.assertFalse((prepared / 'private.json').exists())
        self.assertEqual(profile_from_source(prepared).data, self.document)
        receipt = validate_ios_preparation(prepared)
        self.assertTrue(receipt['productSourcesUnchanged'])
        self.assertFalse(result['behaviorVerified'])

    def test_runtime_is_excluded_from_every_non_selected_configuration(self):
        result = prepare_ios_instrumentation(self.source, self.root / 'prepared', profile=self.document)
        root = Path(result['source'])
        project = plistlib.loads((root / 'Inventory.xcodeproj/project.pbxproj').read_bytes())
        for key in ('RELEASE', 'STORE'):
            settings = project['objects'][key]['buildSettings']
            self.assertEqual(settings['INFOPLIST_FILE'], 'App/Info.plist')
            self.assertIn('RLAutomaticRecorder.swift', settings['EXCLUDED_SOURCE_FILE_NAMES'])
            self.assertEqual(settings['OTHER_SWIFT_FLAGS'], ['$(inherited)', '-DPRODUCT_FLAG'])
        debug = project['objects']['DEBUG']['buildSettings']
        self.assertEqual(debug['INFOPLIST_FILE'], 'ReproLoopInstrumentation/Info.plist')
        self.assertNotIn('DEBUG', debug['SWIFT_ACTIVE_COMPILATION_CONDITIONS'])
        self.assertIn('REPRO_OBSERVATIONS', debug['SWIFT_ACTIVE_COMPILATION_CONDITIONS'])
        info = plistlib.loads((root / debug['INFOPLIST_FILE']).read_bytes())
        self.assertEqual(info['ReproBuildID'], '$(REPRO_BUILD_ID)')
        self.assertEqual(info['ReproAutoProfile'], self.document)

    def test_binary_asset_tampering_invalidates_preparation(self):
        result = prepare_ios_instrumentation(self.source, self.root / 'prepared', profile=self.document)
        (Path(result['source']) / 'App/Assets.xcassets/Logo.imageset/logo.png').write_bytes(b'CHANGED')
        with self.assertRaises(ContractError): validate_ios_preparation(result['source'])

    def test_unsafe_or_secret_input_names_are_rejected_before_reading(self):
        for name in ('.env', 'Config/auth.json', 'Config/credentials.json', 'Config/key.p12',
                     '../outside.swift', '/outside.swift', 'App/../outside.swift',
                     'App/build/private.json', 'Config/local.properties'):
            value = copy.deepcopy(self.document); value['sourceInputs'].append(name)
            with self.subTest(name=name), self.assertRaises(ContractError):
                validate_ios_auto_profile(value)
        for field, value in [('tapTargets', ['bad target']), ('tapTargets', ['one', 'one']),
                             ('screenTargets', {'one': 'same', 'two': 'same'}),
                             ('sourceInputs', self.document['sourceInputs'] + ['App/INFO.plist'])]:
            invalid = copy.deepcopy(self.document); invalid[field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_ios_auto_profile(invalid)

    def test_symlink_and_hardlink_inputs_are_rejected_without_publishing(self):
        file = self.source / 'App/InventoryView.swift'; raw = file.read_bytes()
        other = self.root / 'outside.swift'; other.write_bytes(raw)
        for link in ('symlink', 'hardlink'):
            file.unlink()
            if link == 'symlink': file.symlink_to(other)
            else: os.link(other, file)
            output = self.root / link
            with self.subTest(link=link), self.assertRaises(ContractError):
                prepare_ios_instrumentation(self.source, output, profile=self.document)
            self.assertFalse(output.exists())

    def test_conflicting_output_and_missing_declared_input_preserve_original(self):
        with self.assertRaises(ContractError):
            prepare_ios_instrumentation(self.source, self.source / 'nested', profile=self.document)
        (self.source / 'App/Info.plist').unlink()
        with self.assertRaises(ContractError):
            prepare_ios_instrumentation(self.source, self.root / 'missing', profile=self.document)
        self.assertFalse((self.root / 'missing').exists())

    def test_build_uses_selected_scheme_and_frozen_public_inputs(self):
        from reproloop.ios_observation import build_observation_app
        prepared = prepare_ios_instrumentation(self.source, self.root / 'prepared', profile=self.document)
        commands = []
        output = self.root / 'built'
        def command(argv, cwd, **options):
            commands.append((argv, Path(cwd)))
            if '-version' in argv: return 'Owned Xcode test double'
            app = output / 'DerivedData/Build/Products/QA Debug-iphonesimulator/Inventory.app'
            app.mkdir(parents=True)
            profile = profile_from_source(prepared['source'])
            identity = {'CFBundleIdentifier': profile.data['applicationId'],
                'CFBundleShortVersionString': '1.0', 'CFBundleVersion': '1',
                'ReproAutoProfile': profile.data, 'ReproAutoProfileDigest': profile.digest,
                'ReproBuildID': next(item.split('=', 1)[1] for item in argv if item.startswith('REPRO_BUILD_ID='))}
            (app / 'Info.plist').write_bytes(plistlib.dumps(identity))
            (app / 'Inventory').write_bytes(b'OWNED_PRODUCT')
            return 'Built'
        with patch('reproloop.ios_observation.run_command', side_effect=command), \
             patch('reproloop.ios_build.xcode_environment', return_value={}):
            result = build_observation_app(prepared['source'], output)
        build = commands[0][0]
        self.assertEqual(build[build.index('-scheme') + 1], 'Inventory')
        self.assertEqual(build[build.index('-configuration') + 1], 'QA Debug')
        self.assertEqual(commands[0][1], output / 'source')
        self.assertIn('-disableAutomaticPackageResolution', build)
        self.assertIn('-skipPackageUpdates', build)
        self.assertIn('CODE_SIGNING_ALLOWED=NO', build)
        self.assertEqual(result['receipt']['applicationId'], 'com.example.inventory')
        self.assertFalse(result['receipt']['signed'])
        self.assertTrue((output / 'source/App/Assets.xcassets/Logo.imageset/logo.png').is_file())

    def test_build_rejects_wrong_product_identity(self):
        from reproloop.ios_observation import build_observation_app
        output = self.root / 'wrong-app'
        def command(argv, cwd, **options):
            app = output / 'DerivedData/Build/Products/Release-iphonesimulator/Inventory.app'
            app.mkdir(parents=True)
            (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.example.other'}))
            return 'Built'
        with patch('reproloop.ios_observation.run_command', side_effect=command), \
             patch('reproloop.ios_build.xcode_environment', return_value={}):
            with self.assertRaises(ContractError):
                build_observation_app(self.source, output, profile=self.document, configuration='Release')
        self.assertFalse((output / 'receipt.json').exists())

    def test_general_provider_accepts_only_an_embedded_configured_log_adapter(self):
        from reproloop.core import digest
        from reproloop.ios_profile import validate_ios_profile
        from reproloop.ios_storage import tree_manifest
        from reproloop.live.providers import IosProvider
        from tests.test_worker_profiles import physical_ios_document
        automatic = validate_ios_auto_profile(self.document)
        app = self.root / 'Inventory.app'; app.mkdir()
        info = {'CFBundleIdentifier': self.document['applicationId'], 'CFBundleShortVersionString': '1.4',
                'CFBundleVersion': '27', 'ReproAutoProfile': automatic.data,
                'ReproAutoProfileDigest': automatic.digest, 'ReproBuildID': 'a' * 32,
                'ReproAppLogSchemaVersion': 1}
        (app / 'Info.plist').write_bytes(plistlib.dumps(info))
        value = physical_ios_document(digest(tree_manifest(app)))
        value.update(bundle=self.document['applicationId'])
        value['launchTarget']['value'] = value['bundle']
        value['capabilities']['observations'].append('logs')
        value['capabilities']['logAdapter'] = {'id': 'repro-app-log', 'version': 1}
        value['artifact']['bytes'] = (app / 'Info.plist').stat().st_size
        runtime = validate_ios_profile(value)
        provider = IosProvider('owned-synthetic', self.root / 'helper-products', runtime.bundle,
                               runtime.application_identity, app=app, profile=runtime)
        self.assertTrue(provider.app_logs_only)
        self.assertFalse(provider.record_sdk)
        self.assertIsNone(provider.fixture)
        provider.auto_profile = automatic; provider.auto_run_id = '11111111-1111-4111-8111-111111111111'
        provider.automatic_app_logs = True
        marker = {'schemaVersion': 1, 'platform': 'ios', 'applicationId': runtime.bundle,
            'runId': provider.auto_run_id, 'sessionId': '22222222-2222-4222-8222-222222222222',
            'profileDigest': automatic.digest, 'startedAtMs': 1000}
        snapshot = dict(marker, endSequence=1, truncated=False, lostEvents=False, events=[{
            'seq': 1, 'elapsedMs': 0, 'type': 'click', 'name': 'returned', 'component': 'view',
            'componentId': 'c1111111111111111', 'target': 'inventory.save'}])
        provider._read_app_log_json = lambda path: copy.deepcopy(marker if path == 'app-log-session.json' else snapshot)
        self.assertEqual(provider.collect_app_logs(), snapshot)
        changed = dict(info, ReproAutoProfileDigest='0' * 64)
        (app / 'Info.plist').write_bytes(plistlib.dumps(changed))
        with self.assertRaises(ContractError):
            IosProvider('owned-synthetic', self.root / 'helper-products', runtime.bundle,
                        runtime.application_identity, app=app, profile=runtime)

    def test_general_provider_keeps_authorized_cleanup_available(self):
        from reproloop.ios_profile import validate_ios_profile
        from reproloop.live.providers import IosProvider
        from tests.test_worker_profiles import physical_ios_document
        profile = validate_ios_profile(physical_ios_document())
        provider = IosProvider('owned-synthetic', self.root, profile.bundle,
            profile.application_identity, app=self.root / 'app', profile=profile)
        provider.device_authority = Mock()
        provider.device_authority.native_grant.return_value.wire.return_value = {'owned': True}
        provider.native_handshake = object()
        permit = SimpleNamespace(operation_id='owned-cleanup')
        seen = []
        def dispatch(command):
            seen.append(command)
            pending = provider.pending[command['id']]
            pending['result'] = {'ok': True}
            pending['event'].set()
        provider.commands.put_nowait = dispatch
        self.assertEqual(provider._execute('authority_cleanup', {}, permit), {'ok': True})
        self.assertEqual(seen[0]['action'], 'authority_cleanup')
        self.assertEqual(seen[0]['authority'], {'owned': True})
        with self.assertRaises(ContractError): provider._execute('authority_cleanup', {}, None)
        with self.assertRaises(ContractError): provider._execute('reset', {}, permit)

    def test_observation_profile_cannot_adopt_a_legacy_fixture_capture(self):
        from reproloop.ios_cases import case_spec
        from reproloop.ios_instrumentation import validate_ios_auto_marker
        profile = validate_ios_auto_profile(self.document)
        run = '11111111-1111-4111-8111-111111111111'
        fixture = case_spec('counter').fixture
        marker = {'schemaVersion': 1, 'runId': run, 'sessionId': '22222222-2222-4222-8222-222222222222',
            'profileDigest': profile.digest, 'buildId': 'a' * 32, 'fixture': fixture,
            'startedAtMs': 1000, 'finalized': False}
        with self.assertRaises(ContractError):
            validate_ios_auto_marker(marker, profile, run_id=run, build_id='a' * 32, fixture=fixture)

    def test_profile_file_rejects_duplicate_json_fields(self):
        from reproloop.ios_observation import load_observation_profile
        text = json.dumps(self.document)
        path = self.root / 'selected-profile.json'
        path.write_text('{"applicationId":"com.example.different",' + text[1:])
        with self.assertRaises(ContractError): load_observation_profile(path)

    def test_physical_log_reader_uses_the_selected_application_container(self):
        from reproloop.ios_device import IosPhysicalDevice
        reader = object.__new__(IosPhysicalDevice)
        reader.device = SimpleNamespace(identifier='owned-device-reference')
        reader._mutation_lease = lambda: nullcontext()
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            destination = Path(args[args.index('--destination') + 1])
            destination.write_text('{"owned": true}')
            return {}
        with patch('reproloop.ios_device._devicectl', side_effect=command):
            self.assertEqual(reader.read_app_json('app-log-session.json',
                application_id='com.example.inventory-app'), {'owned': True})
            self.assertEqual(calls[0][calls[0].index('--domain-identifier') + 1], 'com.example.inventory-app')
            with self.assertRaises(ContractError): reader.read_app_json('app-log-session.json', application_id='../other')
        self.assertEqual(len(calls), 1)

    def test_general_launch_rotates_observation_run_before_native_dispatch(self):
        from reproloop.ios_profile import validate_ios_profile
        from reproloop.live.providers import IosProvider
        from tests.test_worker_profiles import physical_ios_document
        runtime = validate_ios_profile(physical_ios_document())
        provider = IosProvider('owned-synthetic', self.root, runtime.bundle,
            runtime.application_identity, app=self.root / 'app', profile=runtime)
        provider.app_logs_only = True
        provider.auto_profile = validate_ios_auto_profile(self.document)
        provider.auto_run_id = '11111111-1111-4111-8111-111111111111'
        provider.app_log_marker = {'owned': True}
        seen = []
        def dispatch(command):
            seen.append(command)
            self.assertNotEqual(command['payload']['autoRunId'], '11111111-1111-4111-8111-111111111111')
            self.assertEqual(command['payload']['autoRunId'], provider.auto_run_id)
            self.assertIsNone(provider.app_log_marker)
            pending = provider.pending[command['id']]
            pending['result'] = {'ok': True}; pending['event'].set()
        provider.commands.put_nowait = dispatch
        provider._wait_for_app_log_marker = Mock()
        self.assertEqual(provider._execute('launch', {'applicationId': 'ios_app'}, None), {'ok': True})
        provider._wait_for_app_log_marker.assert_called_once()

    def test_selected_simulator_artifact_cannot_change_between_registration_and_start(self):
        from reproloop.core import digest
        from reproloop.ios_profile import validate_ios_profile
        from reproloop.ios_storage import tree_manifest
        from reproloop.live.providers import IosProvider
        from tests.test_worker_profiles import physical_ios_document
        app = self.root / 'Selected.app'; app.mkdir()
        (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.example.checkout',
            'CFBundleShortVersionString': '1.4', 'CFBundleVersion': '27'}))
        (app / 'Selected').write_bytes(b'original')
        value = physical_ios_document(digest(tree_manifest(app)))
        value['artifact']['bytes'] = sum(p.stat().st_size for p in app.iterdir())
        profile = validate_ios_profile(value)
        provider = IosProvider('owned-synthetic', self.root, profile.bundle,
            profile.application_identity, app=app, profile=profile)
        (app / 'Selected').write_bytes(b'modified')
        with patch('reproloop.live.providers.Lease', return_value=nullcontext()), \
             patch('reproloop.live.providers.subprocess.run', side_effect=AssertionError('Installation must not run')) as command:
            with self.assertRaises(ContractError): provider.start({'id': 'owned-session'}, Mock())
        command.assert_not_called()

    def test_nested_xcode_project_keeps_source_root_relative_build_settings(self):
        nested = self.root / 'nested-source'; nested.mkdir()
        shutil.copytree(self.source, nested / 'Client')
        document = copy.deepcopy(self.document)
        document['project'] = 'Client/' + document['project']
        document['build']['infoPlist'] = 'Client/' + document['build']['infoPlist']
        document['sourceInputs'] = ['Client/' + name for name in document['sourceInputs']]
        prepared = prepare_ios_instrumentation(nested, self.root / 'nested-prepared', profile=document)
        workspace = Path(prepared['source'])
        project = plistlib.loads((workspace / document['project'] / 'project.pbxproj').read_bytes())
        debug = project['objects']['DEBUG']['buildSettings']
        self.assertEqual(debug['INFOPLIST_FILE'], '../ReproLoopInstrumentation/Info.plist')
        for obj in project['objects'].values():
            if obj.get('isa') == 'PBXFileReference':
                self.assertTrue((workspace / 'Client' / obj['path']).is_file())
        validate_ios_preparation(workspace)


if __name__ == '__main__': unittest.main()
