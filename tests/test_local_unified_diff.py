"""Local unified patches are exact bound proposals, never code execution."""
import copy
import difflib
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from reproof.agents import AgentUnavailable, ProjectUnifiedDiffAgent
from reproof.unified_diff import exact_edits
from reproof.project_repair import proposal_packet
from tests.test_project_repair import BEFORE, PRODUCT, source_fixture
from tests import test_repair_configuration as configuration_fixture


def patch(path, before, after, context=3):
    return ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                      'a/' + path, 'b/' + path, n=context))


class UnifiedDiffTests(unittest.TestCase):
    def test_existing_file_multiple_exact_hunks_and_git_headers(self):
        before = ''.join(f'line {i}\n' for i in range(20))
        after = before.replace('line 1\n', 'one\n').replace('line 18\n', 'eighteen\n')
        diff = 'diff --git a/src/file.txt b/src/file.txt\nindex abcdef0..1234567 100644\n' + patch('src/file.txt', before, after, 1)
        self.assertEqual(exact_edits(diff, {'src/file.txt': before}),
                         [{'path': 'src/file.txt', 'old': before, 'new': after}])

    def test_exact_insert_and_delete_hunks_at_boundaries(self):
        before = 'one\ntwo\nthree\n'
        for after in ('zero\n' + before, before + 'four\n', 'one\nthree\n', 'two\nthree\n'):
            with self.subTest(after=after):
                self.assertEqual(exact_edits(patch('src/x', before, after, 0), {'src/x': before})[0]['new'], after)

    def test_no_side_effects_or_partial_result_on_second_file_failure(self):
        source = {'src/one': 'old\n', 'src/two': 'original\n'}
        diff = patch('src/one', 'old\n', 'new\n') + patch('src/two', 'wrong\n', 'changed\n')
        original = dict(source)
        with self.assertRaises(Exception): exact_edits(diff, source)
        self.assertEqual(source, original)

    def test_rejects_paths_metadata_binary_and_newline_anomalies(self):
        valid = patch('src/x', 'old\n', 'new\n')
        variants = [valid.replace('src/x', path) for path in
                    ('../x', '/x', '.env', 'src/../x', 'src\\x', 'src/x\tdate')]
        variants += [valid.replace('+++ b/src/x', '+++ b/src/y'),
                     valid.replace('--- a/src/x', '--- /dev/null'),
                     valid.replace('+++ b/src/x', '+++ /dev/null'),
                     'GIT binary patch\n', 'old mode 100644\nnew mode 100755\n' + valid,
                     'diff --git a/src/x b/src/x\nindex abcdef0..1234567 100755\n' + valid,
                     valid + '\\ No newline at end of file\n', valid.replace('\n', '\r\n'),
                     valid.replace('new', 'n\x00ew'), valid[:-1], valid + valid,
                     valid.replace('new', 'n\u2028ew')]
        for diff in variants:
            with self.subTest(diff=diff), self.assertRaises(Exception): exact_edits(diff, {'src/x': 'old\n'})

    def test_rejects_wrong_positions_context_counts_and_noop(self):
        valid = patch('src/x', 'old\n', 'new\n')
        for diff in (valid.replace('@@ -1 +1 @@', '@@ -2 +1 @@'),
                     valid.replace('@@ -1 +1 @@', '@@ -1 +2 @@'),
                     valid.replace('@@ -1 +1 @@', '@@ -1,2 +1 @@'),
                     valid.replace('-old', '-wrong'),
                     '--- a/src/x\n+++ b/src/x\n@@ -1 +1 @@\n old\n',
                     valid + '@@ -1 +2 @@\n-old\n+other\n'):
            with self.subTest(diff=diff), self.assertRaises(Exception): exact_edits(diff, {'src/x': 'old\n'})

    def test_rejects_empty_non_lf_source_and_oversized_input(self):
        diff = patch('src/x', 'old\n', 'new\n')
        for source in ('', 'old', 'old\r\n', 'old\0\n', 'x' * (128 * 1024) + '\n'):
            with self.subTest(source=source[:10]), self.assertRaises(Exception): exact_edits(diff, {'src/x': source})
        with self.assertRaises(Exception): exact_edits('+' * (256 * 1024) + '\n', {'src/x': 'old\n'})
        with self.assertRaises(Exception): exact_edits(patch('src/x', 'old\n', ''), {'src/x': 'old\n'})

    def test_packet_binds_full_snapshot_and_adapter_emits_only_proposal_receipt(self):
        project, frozen, _ = source_fixture()
        packet = proposal_packet(project, frozen, {'actions': [], 'assertions': [], 'waits': []},
                                 provider_id='local-unified-diff', external=False)
        self.assertEqual(packet['sourceDigest'], frozen.digest)
        self.assertEqual(set(packet['sourceFiles']), {PRODUCT})
        before = BEFORE.decode()
        after = before.replace('return 1', 'return 2') if 'return 1' in before else before + '// proposal\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'proposal.diff'; path.write_text(patch(PRODUCT, before, after))
            agent = ProjectUnifiedDiffAgent(path, base_source_digest=frozen.digest)
            edits = agent.propose_project(packet, cancellation=threading.Event())
            self.assertEqual(edits, [{'path': PRODUCT, 'old': before, 'new': after}])
            self.assertFalse(agent.external)
            self.assertEqual(agent.last_receipt['kind'], 'local-proposal')
            self.assertEqual(agent.last_receipt['sourceDigest'], frozen.digest)
            self.assertNotIn(before, json.dumps(agent.last_receipt))
            self.assertNotIn('verified', json.dumps(agent.last_receipt))
            with mock.patch('reproof.execution.artifacts.read_regular', side_effect=AssertionError('must not read')):
                with self.assertRaises(AgentUnavailable):
                    agent.propose_project(dict(packet, sourceDigest='0' * 64), cancellation=threading.Event())
            self.assertIsNone(agent.last_receipt)

    def test_proposal_applies_only_to_frozen_candidate_and_protected_bytes_stay_identical(self):
        from reproof.project_repair import RepairSource
        project, frozen, artifacts = source_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen.write_new(root / 'source'); artifacts.write_new(root / 'artifacts')
            source = RepairSource(project, root / 'source', source_paths=[p for p, _ in frozen.entries],
                protected_paths=['tests/CheckoutTests.swift'], original_artifact_root=root / 'artifacts',
                original_artifact_paths=['original.bin'])
            original = source.freeze('original')
            packet = proposal_packet(project, original, {'actions': [], 'assertions': [], 'waits': []},
                provider_id='local-unified-diff', external=False)
            path = root / 'proposal.diff'
            path.write_text(patch(PRODUCT, BEFORE.decode(), BEFORE.decode().replace('if !ready', 'if ready')))
            agent = ProjectUnifiedDiffAgent(path, base_source_digest=original.digest)
            candidate = source.apply(original, agent.propose_project(packet, cancellation=threading.Event()))
            self.assertEqual((root / 'source' / PRODUCT).read_bytes(), BEFORE)
            for name, data in original.entries:
                if name != PRODUCT: self.assertEqual(dict(candidate.entries)[name], data)
            self.assertNotEqual(candidate.digest, original.digest)

    def test_cancellation_during_read_leaves_no_completed_receipt(self):
        cancel = threading.Event()
        agent = ProjectUnifiedDiffAgent('/not-read.diff', base_source_digest='a' * 64)
        def read(*args, **kwargs):
            cancel.set()
            return patch('src/x', 'old\n', 'new\n').encode()
        with mock.patch('reproof.execution.artifacts.read_regular', side_effect=read):
            with self.assertRaises(AgentUnavailable):
                agent.propose_project({'sourceDigest': 'a' * 64, 'sourceFiles': {'src/x': 'old\n'}}, cancellation=cancel)
        self.assertIsNone(agent.last_receipt)

    def test_protected_file_symlink_cancel_and_invalid_utf8_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'proposal.diff'
            packet = {'sourceDigest': 'a' * 64, 'sourceFiles': {'src/x': 'old\n'}}
            agent = ProjectUnifiedDiffAgent(path, base_source_digest='a' * 64)
            path.write_text(patch('tests/protected', 'old\n', 'new\n'))
            with self.assertRaises(AgentUnavailable): agent.propose_project(packet, cancellation=threading.Event())
            path.write_bytes(b'\xff')
            with self.assertRaises(AgentUnavailable): agent.propose_project(packet, cancellation=threading.Event())
            path.unlink(); path.symlink_to('missing')
            with self.assertRaises(AgentUnavailable): agent.propose_project(packet, cancellation=threading.Event())
            cancel = threading.Event(); cancel.set()
            with self.assertRaises(AgentUnavailable): agent.propose_project(packet, cancellation=cancel)


class UnifiedDiffRepairJobTests(unittest.TestCase):
    def test_real_repair_pipeline_proposes_without_verification_or_mutating_original(self):
        from reproof.project_repair import ProjectRepair
        from reproof.repair_journal import RepairJournal
        from tests.g9_support import RepairEnvironment
        env = RepairEnvironment(); self.addCleanup(env.close)
        baseline = env.baseline()
        journal = RepairJournal(env.root / 'repairs', disk_limit=256 * 1024 * 1024)
        self.addCleanup(journal.close)
        path = env.root / 'proposal.diff'
        path.write_text(patch(PRODUCT, BEFORE.decode(), BEFORE.decode().replace('if !ready', 'if ready')))
        agent = ProjectUnifiedDiffAgent(path, base_source_digest=env.source_blobs.digest)
        repair = ProjectRepair(env.source, env.registry, env.engine, journal, agent,
            build_recipe_id='build_app', validation_recipe_ids=('regression_ui',))
        job = repair.create(env.approved, campaign_id=baseline['campaignId'], issue_id='issue',
            owner_id='owner', request_id='unified', mode='propose')
        result = repair.execute(job['id'], env.approved, authorize=lambda: True)
        self.assertEqual(result['status'], 'proposal-ready')
        self.assertEqual(result['provider']['kind'], 'local-proposal')
        self.assertFalse(result['result']['verified'])
        self.assertIsNone(result['result']['afterEvidence'])
        self.assertEqual(result['result']['changedPaths'], [PRODUCT])
        self.assertEqual((env.root / 'source' / PRODUCT).read_bytes(), BEFORE)
        self.assertIn('+    if ready', repair.proposal(result['id'])['patch'])
        # A requested verify still requires a qualified executor before this adapter is read.
        with mock.patch.object(agent, 'propose_project', side_effect=AssertionError('unqualified verify')):
            job = repair.create(env.approved, campaign_id=baseline['campaignId'], issue_id='issue',
                owner_id='owner', request_id='unified_verify', mode='verify')
            result = repair.execute(job['id'], env.approved, authorize=lambda: True)
        self.assertEqual((result['status'], result['reason']), ('blocked', 'protected_verification_unavailable'))
        self.assertEqual(result['outputs'], {})


class UnifiedDiffConfigurationTests(unittest.TestCase):
    setUp = configuration_fixture.RepairConfigurationTests.setUp
    load = configuration_fixture.RepairConfigurationTests.load
    def test_unified_adapter_config_composes_without_reading_diff(self):
        from reproof.live.issue_configuration import compose_issue_workflow
        from reproof.live.access import AccessStore, AccessController
        repair = self.document['projects'][0]['repair']
        repair['agent'] = {'kind': 'local-unified-diff', 'patchFile': str(self.patch),
                           'baseSourceDigest': self.env.source_blobs.digest}
        store = AccessStore(self.env.root / 'access'); self.addCleanup(store.close)
        store.bootstrap_administrator('admin'); store.register_project('admin', self.env.project)
        access = AccessController(store); access.bind_project(self.env.registration)
        with mock.patch('reproof.agents.ProjectUnifiedDiffAgent.propose_project', side_effect=AssertionError('proposal at startup')):
            bundle = compose_issue_workflow(self.env.lab, access, self.load(self.document), root=self.env.root / 'composed')
        self.addCleanup(bundle.close)
        available = bundle.workflow.repairs.availability('checkout')
        self.assertEqual(available['providerKind'], 'local-proposal')
        self.assertFalse(available['verificationAvailable'])
        for digest in ('bad', 'A' * 64, None):
            changed = copy.deepcopy(self.document)
            changed['projects'][0]['repair']['agent']['baseSourceDigest'] = digest
            with self.assertRaises(Exception): self.load(changed)
