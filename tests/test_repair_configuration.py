import copy
import json
import unittest
from unittest import mock

from reproof import contracts
from reproof.live.access import AccessController, AccessStore
from reproof.live.issue_configuration import compose_issue_workflow, load_issue_configuration
from tests.g9_support import RepairEnvironment
from tests.test_issue_configuration import configuration
from tests.test_project_repair import EDIT
from tests.test_repair_diagnostics import diagnostic_policy


class RepairConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.env = RepairEnvironment(); self.addCleanup(self.env.close)
        self.patch = self.env.root / 'proposal.json'; self.patch.write_text(json.dumps({'edits': [EDIT]}))
        self.document = configuration(self.env)
        self.document['repairStorageBytes'] = 128 * 1024 * 1024
        self.document['projects'][0]['repair'] = {
            'sourceRoot': str(self.env.root / 'source'),
            'sourcePaths': [path for path, _ in self.env.source_blobs.entries],
            'protectedPaths': ['tests/CheckoutTests.swift'],
            'originalArtifactRoot': str(self.env.root / 'original-build'),
            'originalArtifactPaths': ['original.bin'], 'artifactIdentity': 'file-sha256',
            'buildRecipeId': 'build_app', 'agent': {'kind': 'local-patch', 'patchFile': str(self.patch)}}
        self.path = self.env.root / 'public-runtime.json'

    def load(self, document):
        self.path.write_text(json.dumps(document)); return load_issue_configuration(self.path)

    def test_explicit_local_source_configuration_composes_without_reading_source_or_invoking_ai(self):
        store = AccessStore(self.env.root / 'access'); self.addCleanup(store.close)
        store.bootstrap_administrator('admin'); store.register_project('admin', self.env.project)
        access = AccessController(store); access.bind_project(self.env.registration)
        with mock.patch('reproof.project_repair.RepairSource.freeze', side_effect=AssertionError('source read at startup')):
            bundle = compose_issue_workflow(self.env.lab, access, self.load(self.document), root=self.env.root / 'composed')
        self.addCleanup(bundle.close)
        availability = bundle.workflow.repairs.availability('checkout')
        self.assertEqual(availability['providerKind'], 'local-test-adapter')
        self.assertTrue(availability['proposalAvailable'])
        self.assertFalse(availability['verificationAvailable'])

    def test_importable_commands_secret_paths_and_incomplete_external_policy_are_rejected(self):
        mutations = (
            lambda r: r.update(command='candidate-controlled command'),
            lambda r: r['sourcePaths'].append('.env'),
            lambda r: r.update(sourceRoot='relative/source'),
            lambda r: r['agent'].update(kind='import-python', module='untrusted'),
            lambda r: r.update(agent={'kind': 'claude', 'model': 'operator-selected-model'}),
        )
        for mutation in mutations:
            document = copy.deepcopy(self.document); mutation(document['projects'][0]['repair'])
            with self.subTest(mutation=mutation), self.assertRaises(contracts.ContractError): self.load(document)

    def test_diagnostic_configuration_requires_separate_exact_external_field_approval(self):
        document = copy.deepcopy(self.document)
        repair = document['projects'][0]['repair']
        repair['diagnosticPolicy'] = diagnostic_policy(self.env.project)
        repair['agent'] = {'kind': 'claude', 'model': 'operator-selected-model'}
        repair['transferPolicy'] = {'schemaVersion': 2, 'projectDigest': contracts.digest(self.env.project),
            'providerId': 'claude', 'approvedTransfer': True, 'sourcePaths': self.env.project['editablePaths'],
            'specificationFields': ['actions'], 'diagnostics': {
                'policyDigest': contracts.digest(repair['diagnosticPolicy']), 'eventFields': ['seq', 'type', 'name']}}
        self.assertEqual(self.load(document), document)
        for mutation in (
            lambda r: r['diagnosticPolicy'].update(projectDigest='f' * 64),
            lambda r: r['diagnosticPolicy']['eventFields'].append('message'),
            lambda r: r['transferPolicy']['diagnostics'].update(policyDigest='b' * 64),
            lambda r: r['transferPolicy']['diagnostics']['eventFields'].append('component'),
            lambda r: r.pop('diagnosticPolicy'),
        ):
            changed = copy.deepcopy(document); mutation(changed['projects'][0]['repair'])
            with self.subTest(mutation=mutation), self.assertRaises(contracts.ContractError): self.load(changed)

    def test_external_diagnostic_configuration_composes_without_source_reads_or_provider_calls(self):
        from tests.test_project_repair import source_fixture
        project, sources, artifacts = source_fixture()
        project['evidencePolicy'].update(logs=True, aiEligible=True)
        with mock.patch('tests.g9_support.source_fixture', return_value=(project, sources, artifacts)):
            env = RepairEnvironment()
        self.addCleanup(env.close)
        store = AccessStore(env.root / 'access'); self.addCleanup(store.close)
        store.bootstrap_administrator('admin'); store.register_project('admin', env.project)
        access = AccessController(store); access.bind_project(env.registration)
        document = configuration(env)
        repair = copy.deepcopy(self.document['projects'][0]['repair'])
        repair.update(sourceRoot=str(env.root / 'source'), originalArtifactRoot=str(env.root / 'original-build'),
                      diagnosticPolicy=diagnostic_policy(env.project), agent={'kind': 'claude', 'model': 'selected-model'})
        repair['transferPolicy'] = {'schemaVersion': 2, 'projectDigest': contracts.digest(env.project),
            'providerId': 'claude', 'approvedTransfer': True, 'sourcePaths': env.project['editablePaths'],
            'specificationFields': [], 'diagnostics': {'policyDigest': contracts.digest(repair['diagnosticPolicy']),
                                                     'eventFields': ['seq', 'type', 'name']}}
        document['projects'][0]['repair'] = repair
        with mock.patch('reproof.project_repair.RepairSource.freeze', side_effect=AssertionError('source read')), \
             mock.patch('reproof.agents.ClaudeProjectAgent.propose_project', side_effect=AssertionError('AI invoked')):
            bundle = compose_issue_workflow(env.lab, access, self.load(document), root=env.root / 'composed')
        self.addCleanup(bundle.close)
        self.assertEqual(bundle.workflow.repairs.runtimes['checkout'].diagnostic_policy, repair['diagnosticPolicy'])


if __name__ == '__main__': unittest.main()
