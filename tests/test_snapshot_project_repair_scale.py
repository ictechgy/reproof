"""Enterprise-sized inert inputs through the real local G9 proposal path."""
import json
from pathlib import Path
import tracemalloc
import unittest
from unittest import mock

from reproof.agents import ProjectUnifiedDiffAgent
from reproof.project_repair import ProjectRepair
from reproof.repair_journal import RepairJournal
from reproof.source_snapshot import inspect_snapshot
from tests.test_local_unified_diff import patch
from tests.test_snapshot_project_repair import SnapshotRepairEnvironment, PRODUCT, BEFORE, AFTER


class SnapshotProjectRepairScaleTests(unittest.TestCase):
    def test_over_9400_files_64mib_and_120mib_apk_use_bounded_local_proposal(self):
        env = SnapshotRepairEnvironment(scale=True); self.addCleanup(env.close)
        self.assertGreater(env.reference['files'], 9400)
        self.assertGreater(env.reference['bytes'], 64 * 1024 * 1024)
        self.assertGreaterEqual(sum(path.endswith('.kt') for path in env.paths), 2900)
        self.assertEqual(env.artifact.stat().st_size, 120 * 1024 * 1024)
        baseline = env.baseline()
        journal = RepairJournal(env.root / 'repairs', disk_limit=256 * 1024 * 1024)
        self.addCleanup(journal.close)
        path = env.root / 'proposal.diff'; path.write_text(patch(PRODUCT, BEFORE, AFTER))
        agent = ProjectUnifiedDiffAgent(path, base_source_digest=env.legacy_digest)
        repair = ProjectRepair(env.source, env.registry, env.engine, journal, agent,
            build_recipe_id='build_app', validation_recipe_ids=('regression_ui',))
        job = repair.create(env.approved, campaign_id=baseline['campaignId'], issue_id='issue',
            owner_id='owner', request_id='scale', mode='propose')
        real_read_bytes = Path.read_bytes
        def bounded_read(path):
            self.assertLess(path.stat().st_size, 4 * 1024 * 1024, 'large inert inputs must stream')
            return real_read_bytes(path)
        tracemalloc.start()
        try:
            with mock.patch.object(Path, 'read_bytes', bounded_read), \
                 mock.patch('reproof.execution.artifacts.BlobSet.from_directory',
                            side_effect=AssertionError('legacy whole-tree transfer is unavailable')):
                result = repair.execute(job['id'], env.approved, authorize=lambda: True)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result['status'], 'proposal-ready', result)
        self.assertFalse(result['result']['verified'])
        self.assertEqual(result['result']['changedPaths'], [PRODUCT])
        self.assertEqual(result['outputs']['candidate']['kind'], 'source-snapshot-v1')
        self.assertLess(peak, 64 * 1024 * 1024, f'proposal peak {peak} bytes')
        self.assertLess(len(json.dumps(result)), 96 * 1024)
        proposal = repair.proposal(job['id'])
        self.assertEqual(proposal['kind'], 'local-snapshot-proposal')
        self.assertEqual(proposal['candidateSnapshot']['files'], env.reference['files'])
        original_manifest = inspect_snapshot(env.snapshot, budgets=env.budgets)
        candidate_manifest = inspect_snapshot(journal.root / job['id'] / 'candidate',
            expected={key: proposal['candidateSnapshot'][key] for key in ('digest', 'files', 'bytes')},
            budgets=env.budgets)
        original_files = {entry['path']: (entry['sha256'], entry['size']) for entry in original_manifest['files']}
        candidate_files = {entry['path']: (entry['sha256'], entry['size']) for entry in candidate_manifest['files']}
        self.assertEqual(set(original_files), set(candidate_files))
        self.assertNotEqual(original_files[PRODUCT], candidate_files[PRODUCT])
        self.assertEqual({name: value for name, value in original_files.items() if name != PRODUCT},
                         {name: value for name, value in candidate_files.items() if name != PRODUCT})
        self.assertEqual((env.snapshot / 'data' / PRODUCT).read_text(), BEFORE)
        print('SNAPSHOT_G9_SCALE ' + json.dumps({'files': env.reference['files'],
            'sourceBytes': env.reference['bytes'], 'artifactBytes': env.artifact.stat().st_size,
            'proposalPeakBytes': peak, 'protectedInputDigestsEqual': True,
            'status': result['status'], 'verified': False}, sort_keys=True))


if __name__ == '__main__': unittest.main()
