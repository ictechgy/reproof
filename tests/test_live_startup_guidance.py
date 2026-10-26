"""Synthetic-only actionable startup errors without weakening native admission."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
from reproof.ios_core import APPLICATION_ID
from reproof.ios_storage import app_info, WrongIOSApplication
from reproof.live.cli import main
from reproof.live.model import Lab, LiveError, STARTUP_DIAGNOSTICS
from reproof.live.providers import IosProvider, require_live_sample_identity

class StartupGuidanceTests(unittest.TestCase):
    def make_app(self, root, bundle=APPLICATION_ID, build='synthetic-build'):
        app=root/'Selected.app';app.mkdir()
        (app/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':bundle,'ReproBuildID':build}))
        return app

    def test_sample_identity_is_still_required_and_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.make_app(Path(directory))
            self.assertEqual(require_live_sample_identity(app),app_info(app))

    def test_general_app_gets_typed_guidance_not_identity_bypass(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.make_app(Path(directory),bundle='private-secret.bundle')
            with self.assertRaises(WrongIOSApplication):app_info(app)
            with self.assertRaises(LiveError) as raised:require_live_sample_identity(app)
            self.assertEqual(raised.exception.code,'ios_profile_required')
            self.assertEqual(str(raised.exception),STARTUP_DIAGNOSTICS['ios_profile_required'])
            self.assertNotIn('private-secret',str(raised.exception));self.assertNotIn(directory,str(raised.exception))

    def test_invalid_sample_is_not_misdiagnosed_as_missing_profile(self):
        for kind in ('missing','corrupt','non_object','missing_build','missing_bundle','invalid_bundle'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                app=self.make_app(Path(directory),build='')
                if kind=='missing':(app/'Info.plist').unlink()
                if kind=='corrupt':(app/'Info.plist').write_bytes(b'private-secret')
                if kind=='missing_bundle':(app/'Info.plist').write_bytes(plistlib.dumps({'ReproBuildID':'synthetic-build'}))
                if kind=='invalid_bundle':(app/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':42}))
                if kind=='non_object':(app/'Info.plist').write_bytes(plistlib.dumps([]))
                with self.assertRaises(LiveError) as raised:require_live_sample_identity(app)
                self.assertEqual(raised.exception.code,'ios_sample_identity_invalid')
                self.assertNotIn('private-secret',str(raised.exception))

    def test_unexpected_sample_checker_error_is_not_relabelled(self):
        with patch('reproof.live.providers.app_info',side_effect=RuntimeError('private-secret')):
            with self.assertRaises(RuntimeError):require_live_sample_identity(Path('unused'))

    def test_cli_general_artifact_rejected_before_native_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);app=self.make_app(root,bundle='private-secret.bundle');output=root/'output';errors=io.StringIO()
            with patch('reproof.ios_runner.IosSimulator') as native,patch('reproof.live.cli.ios_device') as device,patch('reproof.live.cli.Lab') as lab,redirect_stderr(errors),self.assertRaises(SystemExit) as raised:
                main(['live-serve','--simulator','fixture','--ios-app',str(app),'--output',str(output)])
            self.assertEqual(raised.exception.code,2);self.assertIn('--ios-profile',errors.getvalue())
            self.assertNotIn('private-secret',errors.getvalue())
            native.assert_not_called();device.assert_not_called();lab.assert_not_called();self.assertFalse(output.exists())

    def test_valid_sample_cli_reaches_existing_native_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.make_app(Path(directory))
            with patch('reproof.ios_runner.IosSimulator',side_effect=RuntimeError('native sentinel')) as native:
                with self.assertRaisesRegex(RuntimeError,'native sentinel'):
                    main(['live-serve','--simulator','fixture','--ios-app',str(app)])
            native.assert_called_once_with('fixture')

    def test_general_profile_uses_existing_profile_validation_not_sample_check(self):
        with patch('reproof.ios_profile.load_ios_profile',return_value=object()) as profile, \
             patch('reproof.live.cli.require_live_sample_identity') as sample, \
             patch('reproof.ios_runner.IosSimulator',side_effect=RuntimeError('native sentinel')):
            with self.assertRaisesRegex(RuntimeError,'native sentinel'):
                main(['live-serve','--simulator','fixture','--ios-app','general.app','--ios-profile','profile.json'])
        profile.assert_called_once_with(Path('profile.json'));sample.assert_not_called()

    def test_direct_provider_rejects_without_install_or_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.make_app(Path(directory),bundle='private-secret.bundle')
            provider=IosProvider('fake-device',Path(directory),'private-secret.bundle',app=app)
            with patch('reproof.live.native_frame_clock.NativeFrameClock.for_session'),patch('reproof.live.providers.Lease') as lease,patch('reproof.live.providers.subprocess.run') as run,self.assertRaises(LiveError) as raised:
                provider.start({'id':'fake-session'},object())
            self.assertEqual(raised.exception.code,'ios_profile_required');lease.assert_not_called();run.assert_not_called()

    def test_help_explains_profile_and_explicit_authority(self):
        output=io.StringIO()
        with redirect_stdout(output),self.assertRaises(SystemExit):main(['live-serve','--help'])
        text=' '.join(output.getvalue().split()).replace('--ios- profile','--ios-profile')
        self.assertIn('general apps require --ios-profile',text)
        self.assertIn('coordinator-issued parent grant',text);self.assertIn('never an automatic fallback',text)

    def test_missing_parent_grant_has_no_provider_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            device={'id':'synthetic-ios','name':'Synthetic','kind':'ios-simulator','platform':'ios','capabilities':{'actions':['tap']},'factory':lambda:calls.append('constructed'),'_authority':{'deviceKind':'ios-simulator','physicalId':'private-secret'}}
            lab=Lab([device],Path(directory)/'live')
            try:
                result=lab.create_session('synthetic-ios','owner','browser')
                self.assertEqual(result['state'],'failed');self.assertEqual(result['errorCode'],'authority_unavailable')
                self.assertEqual(result['error'],STARTUP_DIAGNOSTICS['authority_unavailable'])
                self.assertNotIn('private-secret',repr(result));self.assertEqual(calls,[])
                self.assertEqual(lab.list_devices()[0]['state'],'available')
            finally:lab.close_all()

    def test_safe_ios_code_retained_and_unknown_error_redacted(self):
        for code in ('ios_profile_required','ios_sample_identity_invalid','unknown-private-code'):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as directory:
                class Provider:
                    def start(self,session,lab):raise LiveError(code,'private-secret')
                    def close(self):pass
                descriptor={'id':'fake-ios','name':'Synthetic','kind':'ios-simulator','platform':'ios','capabilities':{'actions':['tap'],'authorityMode':'legacy-offline-v1'},'factory':Provider}
                lab=Lab([descriptor],Path(directory)/'live')
                try:
                    result=lab.create_session('fake-ios','owner','browser')
                    self.assertEqual(result['error'],STARTUP_DIAGNOSTICS.get(code,'Provider could not start'))
                    self.assertEqual(result.get('errorCode'),code if code in STARTUP_DIAGNOSTICS else None)
                    self.assertNotIn('private-secret',repr(result))
                finally:lab.close_all()

if __name__=='__main__':unittest.main()
