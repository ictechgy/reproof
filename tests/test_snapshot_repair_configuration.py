"""Opt-in snapshot proposal declarations never authorize transfer or execution."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from reproof import contracts
from reproof.live.issue_configuration import _validate_repair_configuration


def snapshot_configuration(root):
    return {'schemaVersion': 2, 'kind': 'local-snapshot-proposal',
        'sourceRoot': str(root / 'snapshot'), 'sourceSnapshot': {
            'schemaVersion': 1, 'kind': 'reproof-local-source-snapshot',
            'digest': 'a' * 64, 'files': 10000, 'bytes': 70 * 1024 * 1024},
        'snapshotBudget': {'maxFiles': 12000, 'maxBytes': 80 * 1024 * 1024},
        'protectedPaths': ['checks/fixture.json'], 'originalArtifact': str(root / 'original.apk'),
        'buildRecipeId': 'build_app', 'agent': {'kind': 'local-unified-diff',
            'patchFile': str(root / 'candidate.diff'), 'baseSourceDigest': 'b' * 64}}


class SnapshotRepairConfigurationTests(unittest.TestCase):
    def test_declarations_require_no_source_reads_or_provider_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            value = snapshot_configuration(root)
            with mock.patch.object(Path, 'open', side_effect=AssertionError('unexpected read')):
                _validate_repair_configuration(value, 'c' * 64)
            self.assertEqual(list(root.iterdir()), [])

    def test_versioned_registration_composes_without_source_or_artifact_reads(self):
        from reproof.live.access import AccessController, AccessStore
        from reproof.live.issue_configuration import compose_issue_workflow, load_issue_configuration
        from reproof.snapshot_repair_source import SnapshotRepairSource
        from tests.test_issue_configuration import configuration
        from tests.test_snapshot_project_repair import SnapshotRepairEnvironment
        env = SnapshotRepairEnvironment(); self.addCleanup(env.close)
        store = AccessStore(env.root / 'access'); self.addCleanup(store.close)
        store.bootstrap_administrator('admin'); store.register_project('admin', env.project)
        access = AccessController(store); access.bind_project(env.registration)
        document = configuration(env)
        selected = snapshot_configuration(env.root)
        selected.update(sourceRoot=str(env.snapshot), sourceSnapshot=env.reference,
            snapshotBudget={'maxFiles': env.budgets.max_files, 'maxBytes': env.budgets.max_bytes},
            protectedPaths=list(env.source.protected_paths), originalArtifact=str(env.artifact))
        selected['agent']['baseSourceDigest'] = env.legacy_digest
        document['projects'][0]['repair'] = selected
        document['repairStorageBytes'] = 128 * 1024 * 1024
        path = env.root / 'runtime.json'; path.write_text(json.dumps(document))
        with mock.patch.object(SnapshotRepairSource, 'freeze', side_effect=AssertionError('startup read')), \
             mock.patch('reproof.agents.ProjectUnifiedDiffAgent.propose_project', side_effect=AssertionError('startup adapter')):
            bundle = compose_issue_workflow(env.lab, access, load_issue_configuration(path), root=env.root / 'composed')
        self.addCleanup(bundle.close)
        result = bundle.workflow.repairs.availability(env.project['id'])
        self.assertTrue(result['proposalAvailable'])
        self.assertFalse(result['verificationAvailable'])
        self.assertEqual(result['providerId'], 'local-unified-diff')

    def test_unknown_fields_transfer_execution_and_wrong_bounds_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            value = snapshot_configuration(Path(temporary).resolve())
            mutations = [lambda v: v.update(transferPolicy=None),
                lambda v: v.update(diagnosticPolicy=None), lambda v: v.update(protectedProfileId='profile'),
                lambda v: v.update(sourcePaths=['src/main.kt']),
                lambda v: v.update(schemaVersion=True), lambda v: v.update(kind='unknown'),
                lambda v: v['agent'].update(kind='claude'),
                lambda v: v['agent'].update(kind='local-patch'),
                lambda v: v['agent'].update(command='execute'),
                lambda v: v['snapshotBudget'].update(maxFiles=True),
                lambda v: v['snapshotBudget'].update(maxBytes=2**40),
                lambda v: v['sourceSnapshot'].update(files=12001),
                lambda v: v['sourceSnapshot'].update(bytes=True),
                lambda v: v['sourceSnapshot'].update(path='/secret'),
                lambda v: v.update(sourceRoot='relative'),
                lambda v: v['protectedPaths'].append('.env')]
            for mutation in mutations:
                changed = copy.deepcopy(value); mutation(changed)
                with self.subTest(mutation=mutation), self.assertRaises(contracts.ContractError):
                    _validate_repair_configuration(changed, 'c' * 64)


if __name__ == '__main__': unittest.main()
