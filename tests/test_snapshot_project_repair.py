"""Real G9 local proposals from synthetic Android snapshots, without native code."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from reproof import contracts
from reproof.agents import ProjectUnifiedDiffAgent
from reproof.live.model import Lab
from reproof.project_repair import ProjectRepair, RepairError
from reproof.qualification import QualificationEngine
from reproof.repair_journal import RepairJournal
from reproof.source_snapshot import SnapshotBudget, capture, inspect_snapshot
from tests.g4_support import G4Environment
from tests.test_local_unified_diff import patch
from tests.test_project_repair import source_fixture

PRODUCT = 'src/Checkout.kt'
PROTECTED = 'tests/CheckoutTests.kt'
BEFORE = 'fun submit(ready: Boolean): String {\n    if (!ready) return "success"\n    return "error"\n}\n'
AFTER = BEFORE.replace('if (!ready)', 'if (ready)')


def stream_file(path, size, byte=b'x'):
    path.parent.mkdir(parents=True, exist_ok=True)
    checksum = hashlib.sha256()
    chunk = byte * (64 * 1024)
    with path.open('wb') as output:
        remaining = size
        while remaining:
            part = chunk[:min(remaining, len(chunk))]
            output.write(part); checksum.update(part); remaining -= len(part)
    return checksum.hexdigest()


class SnapshotRepairEnvironment(G4Environment):
    def __init__(self, *, scale=False):
        self.data_temp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.data_temp.name).resolve()
        original = self.data_root / 'original'
        project, sources, _ = source_fixture()
        project['applications'][0]['platform'] = 'android'
        project['editablePaths'] = [PRODUCT]
        for name, raw in sources.entries:
            if name == 'src/Checkout.swift': name, raw = PRODUCT, BEFORE.encode()
            elif name == 'tests/CheckoutTests.swift': name = PROTECTED
            path = original / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        if scale:
            for index in range(9500):
                suffix = '.kt' if index < 2900 else '.dat'
                stream_file(original / f'public_inputs/File{index:05d}{suffix}', 8192)
            stream_file(original / 'assets/large.dat', 8 * 1024 * 1024)
        self.paths = sorted(str(path.relative_to(original)) for path in original.rglob('*') if path.is_file())
        self.budgets = SnapshotBudget(max_files=16384, max_bytes=128 * 1024 * 1024) if scale else SnapshotBudget()
        self.snapshot = self.data_root / 'snapshot'
        summary = capture(original, self.paths, self.snapshot, budgets=self.budgets)
        self.reference = {'schemaVersion': 1, 'kind': 'reproof-local-source-snapshot',
            **{key: summary[key] for key in ('digest', 'files', 'bytes')}}
        manifest = inspect_snapshot(self.snapshot, budgets=self.budgets)
        self.legacy_digest = contracts.digest([{'path': item['path'], 'digest': item['sha256'], 'size': item['size']}
                                              for item in manifest['files']])
        self.artifact = self.data_root / 'original.apk'
        artifact_hash = stream_file(self.artifact, 120 * 1024 * 1024 if scale else 128)
        project['builds'][0].update(sourceDigest=self.legacy_digest, artifactDigest=artifact_hash)
        def android_lab(devices, *args, **kwargs):
            # This is the existing synthetic provider, not a native Android target.
            for device in devices: device['platform'] = 'android'
            return Lab(devices, *args, **kwargs)
        with mock.patch('tests.g4_support.project_document', return_value=project), \
             mock.patch('tests.g4_support.Lab', side_effect=android_lab):
            super().__init__(recording_capacity_bytes=512 * 1024 * 1024)
        self.root = self.root.resolve()
        self.lab.devices['device']['capabilities']['applicationIdentity']['artifactDigest'] = artifact_hash
        from reproof.snapshot_repair_source import SnapshotRepairSource
        self.source = SnapshotRepairSource(project, self.snapshot, snapshot_reference=self.reference,
            protected_paths=(PROTECTED,), original_artifact=self.artifact, budgets=self.budgets)
        self.original = self.record_original()
        self.approved = self.approve(self.original)
        self.engine = QualificationEngine(self.root / 'baseline', self.registry)

    def baseline(self):
        return self.engine.run_original(self.approved, lambda _number: self.service.replay(
            self.registry.original_execution(self.approved), registration=self.registration,
            device_id='device', owner='owner', controller_id='baseline', preparations=self.preparations()),
            campaign_id='original_baseline')

    def close(self):
        self.engine.close()
        super().close()
        self.data_temp.cleanup()


class SnapshotProjectRepairTests(unittest.TestCase):
    def setUp(self):
        self.env = SnapshotRepairEnvironment(); self.addCleanup(self.env.close)
        self.baseline = self.env.baseline()
        self.journal = RepairJournal(self.env.root / 'repairs', disk_limit=512 * 1024 * 1024)
        self.addCleanup(self.journal.close)

    def repair(self, *, diff=None, **kwargs):
        path = self.env.root / 'proposal.diff'
        path.write_text(diff if diff is not None else patch(PRODUCT, BEFORE, AFTER))
        self.agent = ProjectUnifiedDiffAgent(path, base_source_digest=self.env.legacy_digest)
        return ProjectRepair(self.env.source, self.env.registry, self.env.engine, self.journal,
            self.agent, build_recipe_id='build_app', validation_recipe_ids=('regression_ui',), **kwargs)

    def create(self, repair, **kwargs):
        return repair.create(self.env.approved, campaign_id=kwargs.pop('campaign_id', self.baseline['campaignId']),
            issue_id='issue', owner_id='owner', request_id=kwargs.pop('request_id', 'snapshot'), **kwargs)

    def execute(self, repair, **kwargs):
        job = self.create(repair, **kwargs)
        return repair.execute(job['id'], self.env.approved, authorize=lambda: True)

    def test_real_pipeline_publishes_compact_unverified_proposal_and_preserves_original(self):
        repair = self.repair(); result = self.execute(repair)
        self.assertEqual(result['status'], 'proposal-ready', result)
        self.assertFalse(result['result']['verified'])
        self.assertIsNone(result['result']['afterEvidence'])
        self.assertEqual(result['result']['changedPaths'], [PRODUCT])
        proposal = repair.proposal(result['id'])
        self.assertEqual(proposal['kind'], 'local-snapshot-proposal')
        self.assertEqual(proposal['schemaVersion'], 2)
        self.assertFalse(proposal['verified'])
        self.assertEqual(proposal['edits'][0]['new'], AFTER)
        self.assertNotEqual(proposal['candidateSnapshot']['digest'], self.env.reference['digest'])
        self.assertEqual(result['outputs']['candidate']['kind'], 'source-snapshot-v1')
        self.assertLess(len(json.dumps(result)), 96 * 1024)
        self.assertEqual((self.env.snapshot / 'data' / PRODUCT).read_text(), BEFORE)
        frozen = self.env.source.freeze('original')
        self.assertEqual(frozen.digest, self.env.legacy_digest)
        self.assertNotEqual(frozen.digest, self.env.reference['digest'])
        self.assertNotIn('protected independent harness', json.dumps(proposal))

    def test_missing_baseline_never_reads_source_or_patch(self):
        repair = self.repair()
        with mock.patch.object(type(self.env.source), 'freeze', side_effect=AssertionError('source effect')), \
             mock.patch.object(self.agent, 'propose_project', side_effect=AssertionError('adapter effect')):
            result = self.execute(repair, campaign_id='missing')
        self.assertEqual(result['reason'], 'baseline_unqualified')
        self.assertEqual(result['outputs'], {})

    def test_verify_and_external_policies_reject_before_source_or_adapter_effects(self):
        repair = self.repair()
        with mock.patch.object(type(self.env.source), 'freeze', side_effect=AssertionError('source effect')), \
             mock.patch.object(self.agent, 'propose_project', side_effect=AssertionError('adapter effect')):
            with self.assertRaises(RepairError): self.create(repair, mode='verify')
            for options in ({'transfer_policy': {}}, {'executor': object()}, {'diagnostic_policy': {}}):
                with self.subTest(options=options), self.assertRaises(RepairError): self.repair(**options)
            external = mock.Mock(provider_id='claude', external=True)
            with self.assertRaises(RepairError):
                ProjectRepair(self.env.source, self.env.registry, self.env.engine, self.journal,
                    external, build_recipe_id='build_app', validation_recipe_ids=('regression_ui',))
            external.propose_project.assert_not_called()
            job = self.create(repair, request_id='diagnostics')
            with self.assertRaises(RepairError):
                repair.execute(job['id'], self.env.approved, authorize=lambda: True, diagnostic_reader=mock.Mock())

    def test_tampered_original_apk_or_snapshot_never_publishes_candidate(self):
        for index, path in enumerate((self.env.artifact, self.env.snapshot / 'data' / PRODUCT)):
            original = path.read_bytes(); path.write_bytes(b'changed synthetic original')
            try:
                result = self.execute(self.repair(), request_id=f'tamper{index}')
                self.assertIn(result['status'], ('blocked', 'failed'))
                self.assertEqual(result['outputs'], {})
            finally: path.write_bytes(original)

    def test_protected_diff_is_rejected_by_real_local_adapter(self):
        repair = self.repair(diff=patch(PROTECTED, 'protected independent harness', 'silenced harness'))
        result = self.execute(repair)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['outputs'], {})

    def test_same_adapter_type_cannot_opt_into_external_provider_before_construction(self):
        self.repair()
        for field, value in (('external', True), ('provider_id', 'claude')):
            with self.subTest(field=field):
                agent = ProjectUnifiedDiffAgent(self.env.root / 'proposal.diff',
                                               base_source_digest=self.env.legacy_digest)
                setattr(agent, field, value)
                with mock.patch.object(type(self.env.source), 'freeze', side_effect=AssertionError('source effect')), \
                     self.assertRaises(RepairError):
                    ProjectRepair(self.env.source, self.env.registry, self.env.engine, self.journal,
                        agent, build_recipe_id='build_app', validation_recipe_ids=('regression_ui',))

    def test_mutated_adapter_or_injected_executor_never_dispatches(self):
        for field, value in (('external', True), ('provider_id', 'claude'), ('executor', None)):
            with self.subTest(field=field):
                repair = self.repair(); job = self.create(repair, request_id='mutated_' + field)
                executor = mock.Mock(definition_digest='a' * 64)
                if field == 'executor':
                    repair.executor = executor
                    self.assertFalse(repair.availability()['verificationAvailable'])
                else:
                    setattr(self.agent, field, value)
                with mock.patch.object(type(self.env.source), 'freeze', side_effect=AssertionError('source effect')), \
                     mock.patch.object(self.agent, 'propose_project', side_effect=AssertionError('adapter effect')):
                    result = repair.execute(job['id'], self.env.approved, authorize=lambda: True)
                self.assertIn(result['status'], ('failed', 'blocked'))
                self.assertEqual(result['outputs'], {})
                executor.ready.assert_not_called()
                executor.execute.assert_not_called()
                executor.discard.assert_not_called()

    def test_candidate_mutation_during_last_original_check_blocks_publication(self):
        repair = self.repair(); job = self.create(repair, request_id='late_mutation')
        original = type(self.env.source).require_original
        candidate = self.journal.root / job['id'] / 'candidate' / 'data' / PRODUCT
        mutated = []
        def mutate_after_check(source, frozen, build_id, **kwargs):
            value = original(source, frozen, build_id, **kwargs)
            if candidate.exists() and not mutated:
                candidate.write_text('concurrent synthetic candidate mutation')
                mutated.append(True)
            return value
        with mock.patch.object(type(self.env.source), 'require_original', autospec=True,
                               side_effect=mutate_after_check):
            result = repair.execute(job['id'], self.env.approved, authorize=lambda: True)
        self.assertEqual(mutated, [True])
        self.assertIn(result['status'], ('failed', 'quarantined'))
        self.assertFalse(result['result']['verified'])
        self.assertEqual((self.env.snapshot / 'data' / PRODUCT).read_text(), BEFORE)

    def test_cancel_or_revoke_after_real_adapter_prevents_publication(self):
        for index, revoke in enumerate((False, True)):
            allowed = [True]
            repair = self.repair(); job = self.create(repair, request_id=f'cancel{index}')
            actual = self.agent.propose_project
            def then_cancel(packet, *, cancellation):
                edits = actual(packet, cancellation=cancellation)
                if revoke: allowed[0] = False
                else: self.journal.cancel(job['id'])
                return edits
            with mock.patch.object(self.agent, 'propose_project', side_effect=then_cancel):
                result = repair.execute(job['id'], self.env.approved, authorize=lambda: allowed[0])
            self.assertEqual(result['status'], 'cancelled')
            self.assertEqual(result['outputs'], {})


class SnapshotLiveProjectRepairTests(unittest.TestCase):
    def setUp(self):
        from tests import test_live_project_repair as live_support
        with mock.patch.object(live_support, 'RepairEnvironment', SnapshotRepairEnvironment):
            live_support.LiveProjectRepairTests.setUp(self)

    def test_live_configuration_preserves_roles_and_returns_local_unverified_proposal(self):
        from tests.test_live_project_repair import LiveProjectRepairTests
        from reproof.live.access import AccessError
        path = self.env.root / 'live-proposal.diff'
        path.write_text(patch(PRODUCT, BEFORE, AFTER))
        agent = ProjectUnifiedDiffAgent(path, base_source_digest=self.env.legacy_digest)
        LiveProjectRepairTests.service(self, agent=agent)
        viewer = self.fixture.principals['viewer']
        with self.assertRaises(AccessError):
            LiveProjectRepairTests.start(self, owner=viewer)
        job = LiveProjectRepairTests.start(self)
        result = LiveProjectRepairTests.wait(self, job['id'])
        self.assertEqual(result['status'], 'proposal-ready', result)
        self.assertFalse(result['result']['verified'])
        with self.assertRaises(AccessError): self.repairs.proposal(viewer, job['id'])
        proposal = self.repairs.proposal(self.fixture.owner, job['id'])
        self.assertEqual(proposal['kind'], 'local-snapshot-proposal')
        self.assertEqual(proposal['edits'][0]['new'], AFTER)
        self.assertNotIn(str(self.env.data_root), json.dumps(self.repairs.get(viewer, job['id'])))
        self.assertFalse(self.workflow.projects(viewer)['projects'][0]['repair']['verificationAvailable'])


if __name__ == '__main__': unittest.main()
