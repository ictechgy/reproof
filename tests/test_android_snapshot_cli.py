"""Version routing and budget contracts; no Gradle, signing or device execution."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reproof.android_observation import cli_main, build_observation_app
from reproof.build_instrumentation import prepare_build_instrumentation
from reproof.core import ContractError
from reproof.source_snapshot import SnapshotError


class AndroidSnapshotCliTests(unittest.TestCase):
    def invoke(self, command='android-instrument', *extra):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = cli_main([command, '--source', 'snapshot', '--output', 'output',
                             '--observation-profile', 'profile.json', *extra])
        return code, json.loads(stream.getvalue())

    def test_instrument_budget_forwarding(self):
        profile = SimpleNamespace(data={'kind': 'views-observation-v3'})
        with patch('reproof.android_observation.load_observation_profile', return_value=profile), \
                patch('reproof.build_instrumentation.prepare_build_instrumentation', return_value={'status': 'prepared'}) as call:
            code, result = self.invoke('android-instrument', '--snapshot-max-files', '9500',
                                       '--snapshot-max-bytes', '90000000')
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(call.call_args.kwargs['snapshot_budgets'].max_files, 9500)
        self.assertEqual(call.call_args.kwargs['snapshot_budgets'].max_bytes, 90000000)
        self.assertEqual(call.call_args.args, (Path('snapshot'), profile, Path('output')))

    def test_build_budget_forwarding(self):
        profile = SimpleNamespace(data={'kind': 'views-observation-v3'})
        with patch('reproof.android_observation.load_observation_profile', return_value=profile), \
                patch('reproof.cli.toolchain', return_value={}), \
                patch('reproof.android_observation.build_observation_app', return_value={
                    'app': Path('output/app.apk'), 'receipt': {'automaticObservations': True}}) as call:
            code, result = self.invoke('android-app-build', '--snapshot-max-files', '9500')
        self.assertEqual(code, 0)
        self.assertTrue(result['automaticObservations'])
        self.assertEqual(call.call_args.kwargs['snapshot_budgets'].max_files, 9500)
        self.assertEqual(call.call_args.kwargs['snapshot_budgets'].max_bytes, 60 * 1024 * 1024)

    def test_v2_budget_is_rejected_before_preparation(self):
        profile = SimpleNamespace(data={'kind': 'views-observation-v2'})
        with patch('reproof.android_observation.load_observation_profile', return_value=profile), \
                patch('reproof.build_instrumentation.prepare_build_instrumentation') as call:
            code, _ = self.invoke('android-instrument', '--snapshot-max-files', '9500')
        self.assertEqual(code, 1)
        call.assert_not_called()

    def test_invalid_budget_fails_before_profile_read(self):
        with patch('reproof.android_observation.load_observation_profile') as call:
            code, result = self.invoke('android-instrument', '--snapshot-max-files', '32769')
        self.assertEqual(code, 1)
        self.assertEqual(result['error'], 'snapshot_budget_invalid')
        call.assert_not_called()

    def test_cleanup_incomplete_static_diagnostic(self):
        with patch('reproof.android_observation.load_observation_profile',
                   side_effect=SnapshotError('snapshot_cleanup_incomplete')):
            code, result = self.invoke()
        self.assertEqual(code, 1)
        self.assertTrue(result['cleanupIncomplete'])
        self.assertEqual(result['error'], 'snapshot_cleanup_incomplete')

    def test_direct_legacy_build_and_validation_reject_v3(self):
        from reproof.instrumentation import validate_instrumented_source
        from reproof.repair import build_android
        profile = SimpleNamespace(data={'kind': 'views-observation-v3'})
        with self.assertRaisesRegex(ContractError, 'snapshot route'):
            validate_instrumented_source('missing', profile)
        with self.assertRaisesRegex(ContractError, 'snapshot route'):
            build_android('missing', 'gradle', 'java', 'sdk', ':app:assembleDebug',
                          'app.apk', app_profile=profile)

    def test_legacy_preparation_rejects_snapshot_budget(self):
        from reproof.source_snapshot import SnapshotBudget
        profile = SimpleNamespace(data={'kind': 'views-observation-v2'})
        with self.assertRaisesRegex(ContractError, 'Snapshot budgets'):
            prepare_build_instrumentation('missing', profile, 'output', snapshot_budgets=SnapshotBudget())

    def test_legacy_build_rejects_snapshot_options(self):
        from reproof.source_snapshot import SnapshotBudget
        with self.assertRaisesRegex(ContractError, 'Snapshot options'):
            build_observation_app('missing', 'output', profile={'kind': 'views-observation-v2'},
                gradle='gradle', java_home='java', sdk_home='sdk', snapshot_budgets=SnapshotBudget())

    def test_source_rewrite_route_rejects_v3_before_reading(self):
        from reproof.instrumentation import prepare_instrumentation
        profile = SimpleNamespace(data={'kind': 'views-observation-v3'})
        with patch('reproof.instrumentation.snapshot_source', side_effect=AssertionError('Legacy read forbidden')):
            with self.assertRaisesRegex(ContractError, 'snapshot route'):
                prepare_instrumentation('missing', profile, 'output', mode='source')
