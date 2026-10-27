"""CLI version routing only; no Xcode, signing, devices or private inputs."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from reproof.ios_cli import main
from reproof.source_snapshot import SnapshotError


class IosSnapshotCliTests(unittest.TestCase):
    def invoke(self, *args):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):code=main(list(args))
        return code,json.loads(stream.getvalue())

    def test_instrument_explicit_v3_budget_forwarded(self):
        profile=SimpleNamespace(data={'kind':'uikit-observation-v3'})
        with patch('reproof.ios_observation.load_observation_profile',return_value=profile), \
                patch('reproof.ios_instrumentation.prepare_ios_instrumentation',return_value={'status':'prepared'}) as call:
            code,report=self.invoke('ios-instrument','--source','snapshot','--output','prepared',
                '--profile','profile.json','--snapshot-max-files','22020','--snapshot-max-bytes','150000000')
        self.assertEqual(code,0);self.assertEqual(report['status'],'prepared')
        self.assertEqual(call.call_args.kwargs['snapshot_budgets'].max_files,22020)
        self.assertEqual(call.call_args.kwargs['snapshot_budgets'].max_bytes,150000000)
        self.assertEqual(call.call_args.args,(Path('snapshot'),Path('prepared')))

    def test_legacy_profile_cannot_receive_snapshot_budget(self):
        for profile in (None,SimpleNamespace(data={'kind':'uikit-observation-v2'})):
            with self.subTest(profile=profile), \
                    patch('reproof.ios_observation.load_observation_profile',return_value=profile), \
                    patch('reproof.ios_instrumentation.prepare_ios_instrumentation') as call:
                args=['ios-instrument','--source','source','--output','prepared','--snapshot-max-files','22020']
                if profile:args+=['--profile','profile.json']
                code,_=self.invoke(*args)
                self.assertEqual(code,2);call.assert_not_called()

    def test_build_forwards_budget_without_device_access(self):
        profile=SimpleNamespace(data={'kind':'uikit-observation-v3'})
        with patch('reproof.ios_observation.load_observation_profile',return_value=profile), \
                patch('reproof.ios_observation.build_observation_app',return_value={'app':Path('build/app')}) as call:
            code,report=self.invoke('ios-app-build','--source','prepared/snapshot','--output','build',
                '--profile','profile.json','--snapshot-max-files','22020')
        self.assertEqual(code,0);self.assertFalse(report['signed']);self.assertFalse(report['behaviorVerified'])
        self.assertEqual(call.call_args.kwargs['snapshot_budgets'].max_files,22020)
        self.assertEqual(call.call_args.kwargs['sdk'],'simulator')

    def test_snapshot_failure_reports_static_cleanup_incomplete(self):
        with patch('reproof.ios_instrumentation.prepare_ios_instrumentation',
                   side_effect=SnapshotError('snapshot_cleanup_incomplete')):
            code,report=self.invoke('ios-instrument','--source','source','--output','prepared')
        self.assertEqual(code,2);self.assertTrue(report['cleanupIncomplete'])
        self.assertEqual(report['message'],'snapshot_cleanup_incomplete')

    def test_legacy_source_helpers_reject_v3_instead_of_assuming_source_inputs(self):
        from reproof.core import ContractError
        from reproof.ios_observation import (source_snapshot, prepare_observation,
                                             validate_observation_preparation)
        profile=SimpleNamespace(data={'kind':'uikit-observation-v3'})
        for function,args in ((source_snapshot,('missing',profile)),
                              (prepare_observation,('missing','output',profile)),
                              (validate_observation_preparation,('missing',profile))):
            with self.subTest(function=function.__name__), self.assertRaisesRegex(ContractError,'snapshot route'):
                function(*args)

    def test_public_compile_fixture_reaches_actual_preparation_without_native_tools(self):
        import runpy
        import tempfile
        from reproof.source_snapshot import capture, verify
        from reproof.ios_instrumentation import prepare_ios_instrumentation
        harness=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/check-ios-snapshot-build.py'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            names=harness['fixture'](root/'source')
            captured=capture(root/'source',names,root/'snapshot')
            profile={'schemaVersion':3,'kind':'uikit-observation-v3',
                'applicationId':'com.example.snapshotsynthetic','project':'SnapshotSynthetic.xcodeproj',
                'target':'SnapshotSynthetic','build':{'scheme':'SnapshotSynthetic','product':'SnapshotSynthetic',
                    'infoPlist':'App/Info.plist','debugConfiguration':'Debug'},
                'tapTargets':['synthetic.save'],'screenTargets':{'synthetic.screen':'main'},
                'sourceSnapshot':{'schemaVersion':1,'kind':'reproof-local-source-snapshot',
                    **{key:captured[key] for key in ('digest','files','bytes')}}}
            with patch('subprocess.Popen',side_effect=AssertionError('Native execution forbidden')):
                result=prepare_ios_instrumentation(root/'snapshot',root/'prepared',profile=profile)
            checked=verify(result['source'])
            self.assertGreater(checked['files'],captured['files'])
            self.assertFalse(result['behaviorVerified'])

    def test_profile_loader_bounds_json_before_decode(self):
        from reproof.core import ContractError
        from reproof.execution.artifacts import BlobSet
        from reproof.ios_observation import load_observation_profile
        with patch('reproof.ios_observation._freeze',return_value=BlobSet((('profile.json',b' '*262145),))), \
                patch('reproof.ios_observation.json.loads',side_effect=AssertionError('Oversized JSON decoded')):
            with self.assertRaises(ContractError):
                load_observation_profile(Path('profile.json'))
        with patch('reproof.ios_observation._freeze',return_value=BlobSet((('profile.json',b'['*2000+b']'*2000),))):
            with self.assertRaises(ContractError):load_observation_profile(Path('profile.json'))

    def test_budget_ceiling_rejected_before_preparation(self):
        with patch('reproof.ios_instrumentation.prepare_ios_instrumentation') as call:
            code,report=self.invoke('ios-instrument','--source','source','--output','prepared',
                                   '--snapshot-max-files','32769')
        self.assertEqual(code,2);call.assert_not_called()
        self.assertEqual(report['message'],'snapshot_budget_invalid')
