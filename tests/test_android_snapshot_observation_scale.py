"""Dedicated large Android consumer proof; PSI and Gradle are explicit doubles."""
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch
from reproof import source_snapshot as snapshots
from reproof.android_snapshot_observation import prepare_snapshot_observation,build_snapshot_observation_app,validate_snapshot_observation_profile
from tests.test_android_snapshot_observation import fixture,analyzer,integration,compiler,ACTIVITY


class AndroidSnapshotScaleTests(unittest.TestCase):
    def test_enterprise_sources_keep_psi_to_one_activity(self):
        budgets=snapshots.SnapshotBudget(max_files=32768,max_bytes=256*1024*1024)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()
            source,profile=fixture(root,extra_files=7100,kotlin_files=2900,asset_bytes=65*1024*1024,budgets=budgets)
            self.assertGreater(profile.data['sourceSnapshot']['files'],9400)
            self.assertGreater(profile.data['sourceSnapshot']['bytes'],64*1024*1024)
            selected=analyzer();tracemalloc.start()
            try:
                with patch('reproof.kotlin_instrumenter.configure_gradle',side_effect=integration):
                    prepared=prepare_snapshot_observation(source,profile,root/'prepared',analyzer=selected,budgets=budgets)
                selected_profile=validate_snapshot_observation_profile(json.loads(prepared['appProfile'].read_bytes()))
                with patch('reproof.android_snapshot_observation.run_command',side_effect=compiler(selected_profile)):
                    built=build_snapshot_observation_app(prepared['source'],root/'built',profile=selected_profile,
                        gradle='synthetic-gradle',java_home='synthetic-java',sdk_home='synthetic-sdk',budgets=budgets)
                _,peak=tracemalloc.get_traced_memory()
            finally:tracemalloc.stop()
            self.assertEqual(set(selected.call_args.args[0]),{ACTIVITY})
            original=snapshots.inspect_snapshot(source,budgets=budgets)
            prepared_manifest=snapshots.inspect_snapshot(prepared['source'],budgets=budgets)
            after={item['path']:item for item in prepared_manifest['files']}
            for item in original['files']:
                if item['path'] not in {'settings.gradle.kts','app/build.gradle.kts'}:
                    self.assertEqual(item,after[item['path']])
            proof=snapshots.verify(root/'built/post-build-snapshot',budgets=budgets)
            self.assertEqual(proof['digest'],prepared_manifest['digest'])
            self.assertTrue(built['receipt']['automaticObservations']);self.assertFalse(built['receipt']['signed'])
            self.assertFalse(built['receipt']['behaviorVerified']);self.assertLess(peak,128*1024*1024)
            print(json.dumps({'syntheticFiles':profile.data['sourceSnapshot']['files'],
                'syntheticBytes':profile.data['sourceSnapshot']['bytes'],'unrelatedKotlinFiles':2900,
                'analyzerInputFiles':len(selected.call_args.args[0]),'pythonTracemallocPeakBytes':peak,
                'compiler':'mocked','psi':'mocked','protectedInputDigestsEqual':True}))


if __name__=='__main__':unittest.main()
