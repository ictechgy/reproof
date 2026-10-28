"""Dedicated slow synthetic large-source consumer proof; no native build runs."""
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from reproof import source_snapshot as snapshots
from reproof.ios_snapshot_observation import prepare_snapshot_observation, build_snapshot_observation_app
from tests.test_ios_snapshot_observation import fixture, compiler


class SnapshotObservationScaleTests(unittest.TestCase):
    def test_22k_inputs_over_64mib_prepare_and_unsigned_build(self):
        budget = snapshots.SnapshotBudget(max_files=32768, max_bytes=256*1024*1024)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source, profile = fixture(root, extra_files=22000, asset_size=65*1024*1024, budgets=budget)
            self.assertGreater(profile.data['sourceSnapshot']['files'], 22000)
            self.assertGreater(profile.data['sourceSnapshot']['bytes'], 64*1024*1024)
            tracemalloc.start()
            try:
                prepared = prepare_snapshot_observation(source, root/'prepared', profile, budgets=budget)
                output = root/'built'
                with patch('reproof.ios_snapshot_observation.run_command', side_effect=compiler(output,profile)), \
                        patch('reproof.ios_build.xcode_environment', return_value={}):
                    built = build_snapshot_observation_app(prepared['source'], output, profile=profile, budgets=budget)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            original = snapshots.inspect_snapshot(source, budgets=budget)
            prepared_manifest = snapshots.inspect_snapshot(prepared['source'], budgets=budget)
            after = {item['path']:item for item in prepared_manifest['files']}
            for item in original['files']:
                if item['path'] != profile.data['project']+'/project.pbxproj':
                    self.assertEqual(item, after[item['path']])
            proof = snapshots.verify(output/'post-build-snapshot', budgets=budget)
            self.assertEqual(proof['digest'], prepared_manifest['digest'])
            self.assertTrue(built['receipt']['automaticObservations'])
            self.assertFalse(built['receipt']['signed']); self.assertFalse(built['receipt']['behaviorVerified'])
            # Python allocations only, not process RSS or native compiler memory.
            self.assertLess(peak, 128*1024*1024)
            print(json.dumps({'syntheticFiles': profile.data['sourceSnapshot']['files'],
                              'syntheticBytes': profile.data['sourceSnapshot']['bytes'],
                              'pythonTracemallocPeakBytes': peak,
                              'compiler': 'mocked', 'protectedInputDigestsEqual': True}))


if __name__ == '__main__': unittest.main()
