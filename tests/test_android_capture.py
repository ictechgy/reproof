"""Finite capture selection and pre-target handshake; no native/device execution."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock,patch
from reproof.android_capture import CAPTURE_WIDTHS,capture_policy,require_capture_receipt
from reproof.core import ContractError
from reproof.live import android_live as android
from reproof.live.model import LiveError
from tests import test_android_launch as launch_tests
from tests import test_android_startup_wait as startup_tests


class AndroidCaptureTests(unittest.TestCase):
    def test_finite_exact_integer_policy(self):
        for width in CAPTURE_WIDTHS:
            policy=capture_policy(width)
            self.assertEqual(require_capture_receipt(policy,width),policy)
            self.assertEqual(policy['pixelLimit'],8294400);self.assertEqual(policy['frameBytes'],3145728)
        for invalid in (True,False,960.0,'960',0,-1,959,961,4096,None):
            with self.subTest(invalid=invalid),self.assertRaises(ContractError):capture_policy(invalid)

    def test_exact_receipt_rejects_types_missing_extra_and_wrong_policy(self):
        expected=capture_policy(1280)
        variants=[None,{},dict(expected,extra=1),capture_policy(960)]
        for key in expected:
            for value in (True,float(expected[key]),str(expected[key]),None):
                changed=dict(expected);changed[key]=value;variants.append(changed)
        for value in variants:
            with self.subTest(value=value),self.assertRaises(ContractError):require_capture_receipt(value,1280)

    def test_unsupported_routes_reject_before_device_construction(self):
        profile,_=launch_tests.fixture()
        for kwargs in ({},{'authority_mode':'legacy-offline-v1','runtime_profile':profile}):
            with patch.object(android,'AdbDevice') as adb,self.assertRaises(LiveError):
                android.android_live_device('fake','missing-helper','missing-app',capture_max_width=1280,**kwargs)
            adb.assert_not_called()
        with patch.object(android,'AdbDevice') as adb,self.assertRaises(LiveError):
            android.AndroidLiveProvider('fake','missing-helper','missing-app',capture_max_width=1280)
        adb.assert_not_called()

    def provider(self):
        provider,status=launch_tests.AndroidLaunchTests().provider()
        provider.capture_max_width=1280;provider.capture_policy=capture_policy(1280)
        status['capturePolicy']=capture_policy(1280);status['capabilities']['capturePolicyVersion']=1
        return provider,status

    def test_metadata_capability_and_policy_are_both_required(self):
        for kind in ('missing','wrong','boolean-version','boolean-schema'):
            provider,status=self.provider()
            if kind=='missing':del status['capturePolicy']
            elif kind=='wrong':status['capturePolicy']=capture_policy(1920)
            elif kind=='boolean-version':status['capabilities']['capturePolicyVersion']=True
            else:status['capturePolicy']['schemaVersion']=True
            with self.subTest(kind=kind),self.assertRaises(LiveError):provider._validate_native_metadata(status)
        provider,status=self.provider();provider._validate_native_metadata(status)

    def test_policy_drift_after_accepted_handshake_is_rejected(self):
        provider,status=self.provider();provider._validate_native_metadata(status)
        status['capturePolicy']['maxWidth']=1920
        with self.assertRaises(LiveError):provider._validate_native_metadata(status)
        provider,status=self.provider();provider._validate_native_metadata(status)
        del status['capabilities']['capturePolicyVersion']
        with self.assertRaises(LiveError):provider._validate_native_metadata(status)

    def test_incoming_frames_cannot_exceed_policy_or_use_boolean_dimensions(self):
        provider,_=self.provider();provider.transport=Mock()
        for width,height in ((1281,720),(1280,10000),(True,720),(1280,False),(0,720),(1280,-1),(1280.0,720)):
            with self.subTest(width=width,height=height), \
                 patch.object(android,'decode_native_frame',return_value=({'width':width,'height':height},b'inert')), \
                 self.assertRaises(LiveError):provider._receive_frame()

    def test_default_accepts_legacy_metadata_without_new_capability(self):
        provider,status=launch_tests.AndroidLaunchTests().provider()
        provider._validate_native_metadata(status)
        self.assertEqual(provider.capture_max_width,960)

    def test_missing_policy_startup_never_installs_or_launches_target(self):
        harness=startup_tests.AndroidStartupWaitTests();harness.setUp();self.addCleanup(harness.doCleanups)
        harness.provider.capture_max_width=1280;harness.provider.capture_policy=capture_policy(1280)
        harness.assert_failure('android_startup_metadata_rejected')

    def test_general_start_serializes_policy_and_installs_only_after_handshake(self):
        harness=startup_tests.AndroidStartupWaitTests();harness.setUp();self.addCleanup(harness.doCleanups)
        provider=harness.provider;profile,_=launch_tests.fixture()
        provider.general_profile=True;provider.explicit_profile=True;provider.app_profile=profile
        provider.target_package=profile.data['package'];provider.capture_max_width=1280
        provider.capture_policy=capture_policy(1280);provider.capture_policy['maxWidth']=1920
        provider.installed_identity_evidence={}
        status={'profileDigest':profile.digest,'nativeDigest':profile.native_digest,'ready':True,
            'targetPackage':provider.target_package,'generalProfile':True,'capturePolicy':capture_policy(1280),
            'capabilities':{'actions':profile.data['capabilities']['actions'],'capturePolicyVersion':1}}
        harness.transport.call.side_effect=lambda *args,**kwargs:status
        configured=[]
        def write(*args,**kwargs):
            configured.append(json.loads(kwargs['input']))
            return type('Result',(),{'returncode':0})()
        with patch('reproof.android_artifact.stage_apk',return_value=Path('/synthetic.apk')), \
             patch.object(android,'_general_android_observation',return_value=None), \
             patch.object(android.subprocess,'run',side_effect=write), \
             patch.object(provider,'_execute',return_value={'ok':True}) as execute:
            result=harness.start()
        if provider.artifact_temp is not None:provider.artifact_temp.cleanup()
        self.assertTrue(result['ok']);self.assertTrue(provider.target_launched)
        self.assertEqual(harness.installs,[android.HELPER,provider.target_package])
        self.assertEqual(configured[0]['maxWidth'],1280)
        self.assertEqual(configured[0]['capturePolicy'],capture_policy(1280))
        self.assertEqual(execute.call_args.args[0],'launch')

    def test_unbound_nondefault_provider_rejects_before_startup_effects(self):
        provider,_=self.provider()
        with patch.object(provider,'_install') as install,self.assertRaises(LiveError):
            provider.start({'id':'synthetic'},None)
        install.assert_not_called()

    def test_descriptor_preserves_reset_truth_and_factory_width(self):
        profile,_=launch_tests.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            apk=Path(temporary)/'app.apk';apk.write_bytes(b'inert')
            with patch.object(android,'AdbDevice'),patch.object(android,'_general_android_observation'):
                descriptor=android.android_live_device('fake',apk,apk,runtime_profile=profile,capture_max_width=1440)
                provider=descriptor['factory']()
            self.assertEqual(provider.capture_max_width,1440)
            self.assertEqual(descriptor['capabilities']['capturePolicy'],capture_policy(1440))
            self.assertIsNone(descriptor['capabilities']['resetContract'])
            self.assertNotIn('reset',descriptor['capabilities']['actions'])

    def test_cli_and_worker_reject_wrong_routes_before_device_access(self):
        from reproof.live.cli import main as live
        from reproof.live.worker_cli import main as worker
        for main,args in ((live,['live-serve']),(worker,[])):
            for extra in ([],['--android','fake'],['--authority-mode','legacy-offline-v1']):
                with redirect_stderr(io.StringIO()),patch.object(android,'AdbDevice') as adb,self.assertRaises(SystemExit):
                    main(args+extra+['--android-capture-max-width','1280'])
                adb.assert_not_called()

    def test_live_and_worker_cli_forward_valid_width(self):
        from reproof.live.cli import main as live
        from reproof.live.worker_cli import main as worker
        class Configured(Exception):pass
        profile,_=launch_tests.fixture()
        for main,prefix in ((live,['live-serve']),(worker,['--output','/tmp/not-written','--token-stdin'])):
            with patch('reproof.android_profile.load_android_runtime_profile',return_value=profile), \
                 patch('reproof.device.AdbDevice'), \
                 patch('reproof.live.worker_cli.stdin_object',return_value={'token':'synthetic'}), \
                 patch.object(android,'android_live_device',side_effect=Configured) as factory:
                with self.assertRaises(Configured):
                    main(prefix+['--android','fake','--android-helper','/tmp/helper.apk','--android-app','/tmp/app.apk',
                         '--android-profile','/tmp/profile.json','--android-capture-max-width','1440'])
            self.assertEqual(factory.call_args.kwargs['capture_max_width'],1440)
            self.assertEqual(factory.call_args.kwargs['authority_mode'],'shared-v2')
            self.assertIs(factory.call_args.kwargs['runtime_profile'],profile)

    def test_worker_configuration_forwards_only_finite_operator_width(self):
        from reproof.live.worker_cli import configured_local_devices
        profile,_=launch_tests.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'profile.json').write_text(json.dumps(profile.data))
            item={'platform':'android','deviceId':'fake','profile':'profile.json','application':'app.apk','helper':'helper.apk','captureMaxWidth':1920}
            path=root/'worker.json';path.write_text(json.dumps({'schemaVersion':1,'devices':[item]}))
            with patch.object(android,'android_live_device',return_value={'id':'fake'}) as factory:
                configured_local_devices(path)
            self.assertEqual(factory.call_args.kwargs['capture_max_width'],1920)
            item['captureMaxWidth']=True;path.write_text(json.dumps({'schemaVersion':1,'devices':[item]}))
            with patch.object(android,'android_live_device') as factory,self.assertRaises(ContractError):configured_local_devices(path)
            factory.assert_not_called()
