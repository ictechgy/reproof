"""Synthetic workspace closure and command-selection checks; no Xcode execution."""
import copy
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof.ios_instrumentation import prepare_ios_instrumentation, validate_ios_auto_profile
from reproof.ios_observation import build_observation_app
from tests.test_ios_observation import ordinary_project


def workspace_project(root):
    document = ordinary_project(root)
    document['workspace'] = 'Inventory.xcworkspace'
    document['sourceInputs'].remove('Inventory.xcodeproj/xcshareddata/xcschemes/Inventory.xcscheme')
    framework = {'objects': {'LIB': {'isa': 'PBXNativeTarget', 'name': 'Support',
                    'productType': 'com.apple.product-type.framework'}}}
    files = {
        'Support/Support.xcodeproj/project.pbxproj': plistlib.dumps(framework),
        'Inventory.xcworkspace/contents.xcworkspacedata': b'''<Workspace version="1.0">
<FileRef location="group:Inventory.xcodeproj"/>
<Group location="group:Support" name="Support"><FileRef location="group:Support.xcodeproj"/></Group>
</Workspace>''',
        'Inventory.xcworkspace/xcshareddata/xcschemes/Inventory.xcscheme': b'''<Scheme version="1.7">
<BuildAction><BuildActionEntries><BuildActionEntry buildForRunning="YES">
<BuildableReference BlueprintIdentifier="APP" BlueprintName="Inventory" ReferencedContainer="container:Inventory.xcodeproj"/>
</BuildActionEntry><BuildActionEntry buildForRunning="YES">
<BuildableReference BlueprintIdentifier="LIB" BlueprintName="Support" ReferencedContainer="container:Support/Support.xcodeproj"/>
</BuildActionEntry></BuildActionEntries></BuildAction>
<LaunchAction><BuildableProductRunnable><BuildableReference BlueprintIdentifier="APP" BlueprintName="Inventory" ReferencedContainer="container:Inventory.xcodeproj"/></BuildableProductRunnable></LaunchAction>
</Scheme>''',
    }
    for name, raw in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    document['sourceInputs'] += list(files)
    return document


class WorkspaceObservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.source = self.root / 'source'
        self.document = workspace_project(self.source)

    def prepare(self, document=None, name='prepared'):
        return prepare_ios_instrumentation(self.source, self.root / name,
                                           profile=document or self.document)

    def reject(self, document=None):
        with self.assertRaises(ContractError): self.prepare(document)
        self.assertFalse((self.root / 'prepared').exists())

    def test_runtime_profile_parser_accepts_only_explicit_workspace_inputs(self):
        # Static source wiring only; not a Swift runtime assertion.
        from reproof.resources import read_resource
        source = read_resource('reproof/ios_instrumentation_templates/RLAutomaticRecorder.swift').decode()
        self.assertIn('requiredKeys.isSubset(of: Set(document.keys))', source)
        self.assertIn('requiredKeys.union(["workspace", "inputExtensions"])', source)
        self.assertIn('if let rawWorkspace = document["workspace"]', source)
        self.assertIn('workspace.hasSuffix(".xcworkspace"), inputPath(workspace)', source)
        self.assertIn('inputs.contains(workspace + "/contents.xcworkspacedata")', source)

    def test_workspace_prepares_only_selected_project_and_preserves_original(self):
        before = {name: (self.source / name).read_bytes() for name in self.document['sourceInputs']}
        result = self.prepare(); output = Path(result['source'])
        for name, raw in before.items():
            self.assertEqual((self.source / name).read_bytes(), raw)
            if name != 'Inventory.xcodeproj/project.pbxproj':
                self.assertEqual((output / name).read_bytes(), raw)
        self.assertFalse(result['behaviorVerified'])

    def test_build_uses_workspace_and_explicit_scheme(self):
        output = self.root / 'built'; commands = []
        def command(argv, cwd, **kwargs):
            commands.append(argv)
            if '-version' in argv: return 'Fake Xcode (not executed)'
            app = output / 'DerivedData/Build/Products/Release-iphonesimulator/Inventory.app'
            app.mkdir(parents=True)
            (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.example.inventory',
                'CFBundleShortVersionString': '1.0', 'CFBundleVersion': '1'}))
            (app / 'Inventory').write_bytes(b'SYNTHETIC')
            return 'fake build'
        with patch('reproof.ios_observation.run_command', side_effect=command), \
             patch('reproof.ios_build.xcode_environment', return_value={}):
            result = build_observation_app(self.source, output, profile=self.document, configuration='Release')
        argv = commands[0]
        self.assertEqual(argv[argv.index('-workspace') + 1], str(output / 'source/Inventory.xcworkspace'))
        self.assertNotIn('-project', argv)
        self.assertEqual(argv[argv.index('-scheme') + 1], 'Inventory')
        self.assertIn('-disableAutomaticPackageResolution', argv)
        self.assertFalse(result['receipt']['behaviorVerified'])

    def test_workspace_and_shared_scheme_must_be_declared(self):
        for suffix in ('contents.xcworkspacedata', 'Inventory.xcscheme'):
            value = copy.deepcopy(self.document)
            value['sourceInputs'] = [p for p in value['sourceInputs']
                if not (p.startswith('Inventory.xcworkspace/') and p.endswith(suffix))]
            with self.subTest(suffix=suffix), self.assertRaises(ContractError): validate_ios_auto_profile(value)

    def test_rejects_absolute_escaping_and_external_workspace_references(self):
        path = self.source / 'Inventory.xcworkspace/contents.xcworkspacedata'; original = path.read_bytes()
        for value in ('absolute:/tmp/Other.xcodeproj', 'group:../Other.xcodeproj',
                      'group:/Other.xcodeproj', 'group:Other.xcodeproj', 'self:Inventory.xcodeproj'):
            path.write_bytes(original.replace(b'group:Inventory.xcodeproj', value.encode()))
            with self.subTest(value=value): self.reject()

    def test_rejects_duplicate_or_undeclared_projects(self):
        path = self.source / 'Inventory.xcworkspace/contents.xcworkspacedata'
        path.write_bytes(path.read_bytes().replace(b'</Workspace>', b'<FileRef location="container:Inventory.xcodeproj"/></Workspace>'))
        self.reject()
        path.write_bytes(b'<Workspace><FileRef location="group:Inventory.xcodeproj"/></Workspace>')
        self.reject()

    def test_rejects_xml_entities_malformed_and_structural_limits(self):
        path = self.source / 'Inventory.xcworkspace/contents.xcworkspacedata'
        for raw in (b'<!DOCTYPE Workspace [<!ENTITY x "boom">]><Workspace/>',
                    '<!DOCTYPE Workspace><Workspace/>'.encode('utf-16'), b'<Workspace>',
                    b'<Workspace>' + b'<Group location="group:">' * 34 + b'</Group>' * 34 + b'</Workspace>',
                    b'<Workspace>' + b'<FileRef location="group:Inventory.xcodeproj"/>' * 2050 + b'</Workspace>'):
            path.write_bytes(raw)
            with self.subTest(raw=raw[:50]): self.reject()

    def test_scheme_must_bind_selected_project_target_and_build(self):
        path = self.source / 'Inventory.xcworkspace/xcshareddata/xcschemes/Inventory.xcscheme'
        original = path.read_bytes()
        for old, new in ((b'BlueprintIdentifier="APP"', b'BlueprintIdentifier="WRONG"'),
                         (b'BlueprintName="Inventory"', b'BlueprintName="Other"'),
                         (b'container:Inventory.xcodeproj', b'container:../Inventory.xcodeproj'),
                         (b'buildForRunning="YES"', b'buildForRunning="NO"'),
                         (b'</Scheme>', b'<PreActions/></Scheme>')):
            path.write_bytes(original.replace(old, new))
            with self.subTest(new=new): self.reject()

    def test_unknown_scheme_paths_and_custom_working_directories_rejected(self):
        path = self.source / 'Inventory.xcworkspace/xcshareddata/xcschemes/Inventory.xcscheme'
        original = path.read_bytes()
        for member in (b'<PathRunnable FilePath="/tmp/private"/>',
                       b'<TestPlanReference reference="container:../private.xctestplan"/>',
                       b'<LaunchAction customWorkingDirectory="/tmp/private"/>',
                       b'<LaunchAction useCustomWorkingDirectory="YES"/>',
                       b'<EnvironmentVariables/>'):
            path.write_bytes(original.replace(b'</Scheme>', member + b'</Scheme>'))
            with self.subTest(member=member): self.reject()

    def test_other_actions_cannot_reference_external_projects(self):
        path = self.source / 'Inventory.xcworkspace/xcshareddata/xcschemes/Inventory.xcscheme'
        path.write_bytes(path.read_bytes().replace(b'</Scheme>', b'<TestAction><Testables><BuildableReference BlueprintIdentifier="APP" BlueprintName="Inventory" ReferencedContainer="container:Other.xcodeproj"/></Testables></TestAction></Scheme>'))
        self.reject()

    def test_package_and_implicit_project_closure_rejected(self):
        path = self.source / 'Support/Support.xcodeproj/project.pbxproj'; value = plistlib.loads(path.read_bytes())
        for extra in ({'isa': 'XCRemoteSwiftPackageReference'}, {'isa': 'XCLocalSwiftPackageReference'},
                      {'isa': 'PBXProject', 'projectReferences': [{'ProjectRef': 'OUTSIDE'}]}):
            changed = copy.deepcopy(value); changed['objects']['EXTRA'] = extra
            path.write_bytes(plistlib.dumps(changed))
            with self.subTest(extra=extra): self.reject()

    def test_duplicate_shared_scheme_rejected(self):
        self.document['sourceInputs'].append('Inventory.xcodeproj/xcshareddata/xcschemes/Inventory.xcscheme')
        self.reject()

    def test_duplicate_selected_target_rejected(self):
        path = self.source / 'Inventory.xcodeproj/project.pbxproj'; value = plistlib.loads(path.read_bytes())
        value['objects']['OTHER_APP'] = copy.deepcopy(value['objects']['APP'])
        path.write_bytes(plistlib.dumps(value)); self.reject()

    def test_workspace_tampering_invalidates_preparation_before_build(self):
        prepared = self.prepare()
        path = Path(prepared['source']) / 'Inventory.xcworkspace/contents.xcworkspacedata'
        path.write_bytes(path.read_bytes() + b' ')
        with patch('reproof.ios_observation.run_command') as command, \
             patch('reproof.ios_build.xcode_environment') as environment:
            with self.assertRaises(ContractError):
                build_observation_app(prepared['source'], self.root / 'built')
        command.assert_not_called(); environment.assert_not_called()
        self.assertFalse((self.root / 'built').exists())

    def test_original_workspace_changed_during_preparation_is_not_published(self):
        from reproof.ios_instrumentation import _configure_project
        path = self.source / 'Inventory.xcworkspace/contents.xcworkspacedata'
        def configure(*args):
            result = _configure_project(*args)
            path.write_bytes(path.read_bytes() + b' ')
            return result
        with patch('reproof.ios_instrumentation._configure_project', side_effect=configure):
            self.reject()

    def test_symlink_workspace_input_rejected(self):
        path = self.source / 'Inventory.xcworkspace/contents.xcworkspacedata'
        outside = self.root / 'outside'; outside.write_bytes(path.read_bytes())
        path.unlink(); path.symlink_to(outside); self.reject()

    def test_invalid_workspace_rejects_before_environment_or_build(self):
        path = self.source / 'Inventory.xcworkspace/contents.xcworkspacedata'; path.write_bytes(b'<Workspace/>')
        with patch('reproof.ios_observation.run_command') as command, \
             patch('reproof.ios_build.xcode_environment') as environment:
            with self.assertRaises(ContractError):
                build_observation_app(self.source, self.root / 'built', profile=self.document)
        command.assert_not_called(); environment.assert_not_called()
        self.assertFalse((self.root / 'built').exists())
