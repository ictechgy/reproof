"""Synthetic snapshot instrumentation and mocked unsigned compiler contracts."""
import copy
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof import source_snapshot as snapshots
from reproof.ios_instrumentation import validate_ios_auto_profile
from reproof.ios_snapshot_observation import (
    prepare_snapshot_observation, validate_snapshot_preparation, build_snapshot_observation_app,
    validate_snapshot_observation_document, _project_metadata,
)
from tests.test_ios_observation import ordinary_project


def fixture(root, *, extra_files=0, asset_size=0, budgets=None):
    source = root/'original'; source.mkdir()
    document = ordinary_project(source)
    names = document.pop('sourceInputs')
    for index in range(extra_files):
        name = f'App/Files/item{index:05}.json'
        path = source/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'{}')
        names.append(name)
    if asset_size:
        for index in range((asset_size + 1024*1024-1)//(1024*1024)):
            name = f'App/Assets/blob{index:03}.png'
            path = source/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x'*min(1024*1024, asset_size-index*1024*1024)); names.append(name)
    captured = snapshots.capture(source, names, root/'original-snapshot', budgets=budgets)
    document.update(schemaVersion=3, kind='uikit-observation-v3', sourceSnapshot={
        'schemaVersion': 1, 'kind': 'reproof-local-source-snapshot',
        **{key: captured[key] for key in ('digest', 'files', 'bytes')}})
    return root/'original-snapshot', validate_ios_auto_profile(document)


def compiler(output, profile, *, mutate=None):
    def command(argv, cwd, **options):
        if '-version' in argv: return 'Synthetic compiler, not native qualification'
        if mutate: mutate(Path(cwd))
        configuration = argv[argv.index('-configuration')+1]
        app = output/f'DerivedData/Build/Products/{configuration}-iphonesimulator/Inventory.app'
        app.mkdir(parents=True)
        info = {'CFBundleIdentifier': profile.data['applicationId'],
                'CFBundleShortVersionString': '1.0', 'CFBundleVersion': '1'}
        if configuration == profile.data['build']['debugConfiguration']:
            info.update(ReproAutoProfile=profile.data, ReproAutoProfileDigest=profile.digest,
                        ReproBuildID=next(v.split('=',1)[1] for v in argv if v.startswith('REPRO_BUILD_ID=')))
        (app/'Info.plist').write_bytes(plistlib.dumps(info)); (app/'Inventory').write_bytes(b'SYNTHETIC')
        return 'Synthetic build'
    return command


class SnapshotObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source, self.profile = fixture(self.root)

    def prepare(self):
        return prepare_snapshot_observation(self.source, self.root/'prepared', self.profile)

    def test_prepare_uses_new_snapshot_original_reference_and_no_source_copy(self):
        result = self.prepare()
        self.assertFalse((self.root/'prepared/source').exists())
        receipt = validate_snapshot_preparation(result['source'], self.profile)
        self.assertEqual(receipt['originalSnapshot'], self.profile.data['sourceSnapshot'])
        self.assertNotEqual(receipt['preparedSnapshot']['digest'], receipt['originalSnapshot']['digest'])
        original = snapshots.inspect_snapshot(self.source)
        prepared = snapshots.inspect_snapshot(result['source'])
        before = {item['path']:item for item in original['files']}
        after = {item['path']:item for item in prepared['files']}
        for path, item in before.items():
            if not path.endswith('project.pbxproj'): self.assertEqual(item, after[path])
        self.assertFalse(receipt['behaviorVerified'])
        self.assertNotIn('patch', result)

    def test_declared_original_digest_and_counts_cannot_be_raised_or_changed(self):
        for field, value in [('digest', 'f'*64), ('files', 32769), ('bytes', 256*1024*1024+1)]:
            document = self.profile.data; document['sourceSnapshot'][field] = value
            with self.subTest(field=field), self.assertRaises((ContractError,snapshots.SnapshotError)):
                selected = validate_ios_auto_profile(document)
                prepare_snapshot_observation(self.source, self.root/'invalid', selected)
        self.assertFalse((self.root/'invalid').exists())

    def test_snapshot_contract_does_not_accept_v2_source_lists_or_extra_fields(self):
        for field in ('sourceInputs', 'commands', 'oracle'):
            document = self.profile.data; document[field] = []
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_snapshot_observation_document(document)

    def test_original_and_prepared_tampering_fails_without_build(self):
        result = self.prepare()
        path = result['source']/'data/App/InventoryView.swift'; path.write_bytes(b'changed')
        with self.assertRaises((ContractError,snapshots.SnapshotError)):
            validate_snapshot_preparation(result['source'], self.profile)
        with patch('reproof.ios_snapshot_observation.run_command') as run:
            with self.assertRaises((ContractError,snapshots.SnapshotError)):
                build_snapshot_observation_app(result['source'], self.root/'built', profile=self.profile)
        run.assert_not_called()

    def test_receipt_protected_original_manifest_cannot_be_rewritten(self):
        result = self.prepare(); path = result['source'].parent/'original-manifest.json'
        manifest = json.loads(path.read_bytes()); manifest['files'][0]['sha256'] = 'f'*64
        path.write_text(json.dumps(manifest))
        with self.assertRaises((ContractError,snapshots.SnapshotError)):
            validate_snapshot_preparation(result['source'], self.profile)

    def test_build_materializes_once_and_keeps_unsigned_and_unverified_truth(self):
        result = self.prepare(); output = self.root/'built'
        with patch('reproof.ios_snapshot_observation.run_command', side_effect=compiler(output,self.profile)) as run, \
                patch('reproof.ios_build.xcode_environment', return_value={}):
            built = build_snapshot_observation_app(result['source'], output, profile=self.profile)
        command = run.call_args_list[0].args[0]
        self.assertIn('CODE_SIGNING_ALLOWED=NO', command)
        self.assertIn('CODE_SIGNING_REQUIRED=NO', command)
        self.assertIn('-disableAutomaticPackageResolution', command)
        self.assertIn('iphonesimulator', command)
        receipt = built['receipt']
        self.assertTrue(receipt['buildCompleted']); self.assertTrue(receipt['automaticObservations'])
        self.assertFalse(receipt['signed']); self.assertFalse(receipt['behaviorVerified'])
        self.assertEqual(snapshots.verify(output/'post-build-snapshot')['digest'], receipt['sourceSnapshot']['digest'])

    def test_device_build_rejected_before_compiler_or_output(self):
        with patch('reproof.ios_snapshot_observation.run_command') as run, self.assertRaises(ContractError):
            build_snapshot_observation_app(self.source, self.root/'device', profile=self.profile, sdk='device')
        run.assert_not_called(); self.assertFalse((self.root/'device').exists())

    def test_compiler_mutation_or_failure_preserves_executed_tree(self):
        result = self.prepare(); output = self.root/'mutated'
        def mutate(path): (path/'App/InventoryView.swift').write_bytes(b'compiler mutated protected input')
        with patch('reproof.ios_snapshot_observation.run_command', side_effect=compiler(output,self.profile,mutate=mutate)), \
                patch('reproof.ios_build.xcode_environment', return_value={}):
            with self.assertRaises(snapshots.SnapshotError) as error:
                build_snapshot_observation_app(result['source'], output, profile=self.profile)
        self.assertTrue(error.exception.cleanup_incomplete)
        self.assertTrue((output/'source/App/InventoryView.swift').exists())
        self.assertFalse((output/'receipt.json').exists())
        output = self.root/'failed'
        with patch('reproof.ios_snapshot_observation.run_command', side_effect=ContractError('synthetic compiler error')), \
                patch('reproof.ios_build.xcode_environment', return_value={}):
            with self.assertRaises(snapshots.SnapshotError) as error:
                build_snapshot_observation_app(result['source'], output, profile=self.profile)
        self.assertTrue((output/'source').is_dir()); self.assertFalse((output/'receipt.json').exists())

    def test_metadata_preflight_rejects_depth_and_entity_expansion(self):
        for raw in (b'<?xml version="1.0"?><!DOCTYPE plist [<!ENTITY x "value">]><plist><dict/></plist>',
                    b'<?xml version="1.0"?><plist>'+b'<array>'*70+b'<string>x</string>'+b'</array>'*70+b'</plist>',
                    b'bplist00'+b'\0'*8+b'\0'*8+(250001).to_bytes(8,'big')+b'\0'*16):
            with self.subTest(raw=raw[:20]), self.assertRaises(ContractError): _project_metadata(raw)

    def test_decoded_alias_and_cycle_expansion_is_bounded_before_extend(self):
        from reproof.ios_snapshot_observation import _decoded
        cycle = []; cycle.extend([cycle]*100000)
        with self.assertRaises(ContractError): _decoded(cycle)
        shared = ['small']*100000
        with self.assertRaises(ContractError): _decoded([shared]*4)

    def test_workspace_closure_preserves_secondary_project_and_build_selection(self):
        from tests.test_ios_workspace import workspace_project
        raw = self.root/'workspace-source'
        document = workspace_project(raw)
        summary = snapshots.capture(raw, document.pop('sourceInputs'), self.root/'workspace-snapshot')
        document.update(schemaVersion=3, kind='uikit-observation-v3', sourceSnapshot={
            'schemaVersion': 1, 'kind': 'reproof-local-source-snapshot',
            **{key:summary[key] for key in ('digest','files','bytes')}})
        profile = validate_ios_auto_profile(document)
        prepared = prepare_snapshot_observation(self.root/'workspace-snapshot',self.root/'workspace-prepared',profile)
        output=self.root/'workspace-built'
        with patch('reproof.ios_snapshot_observation.run_command',side_effect=compiler(output,profile)) as run, \
                patch('reproof.ios_build.xcode_environment',return_value={}):
            build_snapshot_observation_app(prepared['source'],output,profile=profile)
        argv=run.call_args_list[0].args[0]
        self.assertIn('-workspace',argv); self.assertNotIn('-project',argv)
        self.assertEqual((output/'source/Support/Support.xcodeproj/project.pbxproj').read_bytes(),
                         (raw/'Support/Support.xcodeproj/project.pbxproj').read_bytes())

    def test_binary_metadata_keeps_bounded_valid_values_and_preflights_huge_lengths(self):
        from reproof import ios_snapshot_observation as module
        raw = plistlib.dumps({'items': ['one', 'two']}, fmt=plistlib.FMT_BINARY)
        self.assertEqual(module._bounded_plist(raw), {'items': ['one', 'two']})
        object_bytes = b'\xaf\x13' + (2**40).to_bytes(8, 'big')
        table = 8 + len(object_bytes)
        malicious = b'bplist00' + object_bytes + b'\x08' + b'\0'*6 + bytes([1,1]) + \
            (1).to_bytes(8,'big') + (0).to_bytes(8,'big') + table.to_bytes(8,'big')
        with patch.object(module, '_plist', side_effect=AssertionError('Unsafe parser must not run')) as parser:
            with self.assertRaises(ContractError): module._bounded_plist(malicious)
        parser.assert_not_called()

    def test_openstep_preflight_ignores_quoted_and_commented_punctuation(self):
        from reproof.ios_snapshot_observation import _preflight
        self.assertGreater(_preflight(b'{ /* ) } */ script = "echo ) {"; list = (one,two); // }\n }'),0)
        with self.assertRaises(ContractError): _preflight(b'{'*65+b'}'*65)

    def test_binary_duplicate_offsets_fail_before_parser(self):
        from reproof import ios_snapshot_observation as module
        # Two object IDs alias one serialized string span; malicious input is
        # passed only to preflight, never to the real plist parser.
        objects=b'\x53abc'; table=8+len(objects)
        malicious=b'bplist00'+objects+b'\x08\x08'+b'\0'*6+bytes([1,1]) + \
            (2).to_bytes(8,'big')+(0).to_bytes(8,'big')+table.to_bytes(8,'big')
        with patch.object(module,'_plist',side_effect=AssertionError('Unsafe parser must not run')) as parser:
            with self.assertRaises(ContractError): module._bounded_plist(malicious)
        parser.assert_not_called()

    def test_parent_inode_alias_is_rejected_before_output_creation(self):
        from reproof import ios_snapshot_observation as module
        actual = module.open_directory
        # Model case-insensitive ancestry: the spelling looks separate but its
        # parent resolves to the selected snapshot inode.
        def aliased(path):
            return actual(self.source if Path(path) == self.root/'alias' else path)
        before = snapshots.verify(self.source)
        with patch.object(module, 'open_directory', side_effect=aliased), self.assertRaises(ContractError):
            module._Publication(self.source, self.root/'alias'/'new-output')
        self.assertEqual(snapshots.verify(self.source), before)
        self.assertFalse((self.source/'new-output').exists())

    def test_swapped_outer_root_never_receives_sidecars(self):
        from reproof import ios_snapshot_observation as module
        derive = snapshots.derive_snapshot
        output = self.root/'prepared'
        def swapped(*args, **kwargs):
            result = derive(*args, **kwargs)
            output.rename(self.root/'owned-output')
            output.mkdir(); (output/'foreign.txt').write_text('must survive')
            return result
        with patch.object(module.snapshots, 'derive_snapshot', side_effect=swapped), self.assertRaises(snapshots.SnapshotError) as error:
            self.prepare()
        self.assertTrue(error.exception.cleanup_incomplete)
        self.assertEqual(sorted(path.name for path in output.iterdir()), ['foreign.txt'])
        self.assertTrue((self.root/'owned-output/snapshot').is_dir())

    def test_cancelled_preparation_never_publishes_output(self):
        import threading
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(snapshots.SnapshotError):
            prepare_snapshot_observation(self.source,self.root/'cancelled',self.profile,cancel=cancel)
        self.assertFalse((self.root/'cancelled').exists())

    def test_sidecar_links_are_rejected(self):
        result = self.prepare(); receipt = result['source'].parent/'instrumentation.json'
        other = self.root/'receipt'; receipt.rename(other); receipt.symlink_to(other)
        with self.assertRaises(ContractError): validate_snapshot_preparation(result['source'],self.profile)


if __name__ == '__main__': unittest.main()
