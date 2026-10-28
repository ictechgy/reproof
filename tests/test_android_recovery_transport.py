"""Private qualification transport doubles; never invokes adb or native code."""
import copy
import hashlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from reproof.core import ContractError
from reproof.live import android_recovery_transport as transport
from tests.test_android_launch import fixture
from tests import test_android_startup_wait as startup_tests


def configuration():
    profile,_=fixture()
    return {'schemaVersion':1,'kind':'reproof-android-recovery-qualification','phase':'session',
        'attemptId':'attempt','sessionId':'session','deviceScopeDigest':'a'*64,
        'applicationId':profile.data['applicationId'],'artifactDigest':profile.data['artifact']['sha256'],
        'profileDigest':profile.digest,'helperArtifactDigest':'b'*64,'priorOwnershipGeneration':1}


def producer():
    result=transport._Producer();result._configure(configuration())
    result.device=Mock();result.transport=Mock();result.stop=threading.Event();result.thread=Mock()
    result.thread.is_alive.return_value=False;result.process=Mock();result.process.poll.return_value=0
    result.process.pid=12345;result.port=None;result.token='ephemeral';result._startup_status=None
    return result


def grant(purpose='old-retire'):
    issuer=Mock();wire={'purpose':purpose,'operationId':'recovery_'+'c'*32,
        'configurationDigest':'unused'}
    result=SimpleNamespace(_issuer=issuer,wire=lambda:dict(wire))
    return result,wire


class AndroidRecoveryTransportTests(unittest.TestCase):
    def _startup(self,*,missing=False,late_failure=False):
        harness=startup_tests.AndroidStartupWaitTests();harness.setUp();self.addCleanup(harness.doCleanups)
        profile,_=fixture();owner=Mock();owner.bind_initial.return_value=configuration()
        with patch.object(transport,'find_adb',return_value='/synthetic/adb'):
            provider=transport.QualificationAndroidProvider(owner,'emulator-5554','helper.apk','app.apk',
                runtime_profile=profile,helper_digest='b'*64,work='/tmp')
        provider.device_authority=Mock();provider.lease=Mock();provider.lease.__enter__=Mock();provider.lease.__exit__=Mock()
        provider.stop=startup_tests.Stop(harness.clock,provider)
        provider.device=Mock();harness.provider=provider
        identity={'pid':123,'startTicks':'789'}
        metadata={'profileDigest':profile.digest,'nativeDigest':profile.native_digest,'ready':True,
            'targetPackage':profile.data['package'],'generalProfile':True,'nativeProcessIdentity':identity,
            'capabilities':{'actions':profile.data['capabilities']['actions']},
            'recoveryQualificationVersion':1,
            'recoveryQualificationConfigurationDigest':transport.hashlib.sha256(
                transport.json.dumps(configuration(),sort_keys=True,separators=(',',':')).encode()).hexdigest()}
        if missing:metadata.pop('recoveryQualificationVersion')
        harness.transport.call.side_effect=lambda *args,**kwargs:metadata
        def spawn(config):
            self.assertEqual(owner.initial_dispatch.call_count,1)
            self.assertEqual(config['qualificationRecovery']['configurationDigest'],metadata['recoveryQualificationConfigurationDigest'])
            provider.transport=harness.transport;provider.process=harness.process
        with patch.object(provider,'_spawn',side_effect=spawn), \
             patch.object(provider,'_verify_installed') as installed, \
             patch.object(provider,'_execute',side_effect=ContractError('late') if late_failure else None,
                          return_value={'ok':True}) as execute:
            if missing or late_failure:
                with self.assertRaises(Exception) as error:provider.start_authorized({'id':'session'},harness.lab,harness.permit)
                if late_failure:self.assertEqual(str(error.exception),'late');execute.assert_called_once()
                else:self.assertEqual(error.exception.code,'android_startup_metadata_rejected')
                owner.failure.assert_called_once_with(provider);owner.initial_started.assert_not_called()
                provider.lease.__exit__.assert_not_called()
                if missing:execute.assert_not_called()
            else:
                result=provider.start_authorized({'id':'session'},harness.lab,harness.permit)
                self.assertTrue(result['ok']);owner.initial_started.assert_called_once_with(provider)
                self.assertEqual(execute.call_args.args[0],'launch')
                provider.device.helper_process.assert_called_once_with(identity)
                self.assertIsNotNone(provider.startup_status)
            self.assertEqual(installed.call_count,2)
        provider.device.install.assert_not_called()
        self.assertNotIn('/stop',[call.args[0] for call in harness.transport.call.call_args_list])

    def test_actual_private_startup_handshake_then_launch_without_install(self):self._startup()
    def test_actual_private_startup_rejects_missing_recovery_metadata_before_launch(self):self._startup(missing=True)
    def test_actual_private_startup_failure_never_runs_ordinary_cleanup(self):self._startup(late_failure=True)

    def test_no_discovery_or_device_commands_during_construction(self):
        profile,_=fixture()
        with patch.object(transport,'find_adb',return_value='/synthetic/adb'),patch.object(transport._ProcessOwner,'run') as run:
            provider=transport.QualificationAndroidProvider(Mock(),'emulator-5554','helper.apk','app.apk',
                runtime_profile=profile,helper_digest='b'*64,work='/tmp')
        run.assert_not_called();self.assertIsNone(provider.process);self.assertIsNone(provider.fixture_config)
        self.assertFalse(provider.record_sdk);self.assertFalse(provider.automatic_app_logs)
        with self.assertRaises(ContractError):provider.close()

    def test_only_explicit_emulator_serial_accepted(self):
        for serial in ('device123','auto','emulator-1','emulator-99999','emulator-5554;bad',None):
            with patch.object(transport,'find_adb') as find,self.assertRaises(ContractError):
                transport._QualificationAdbDevice(serial,'/tmp')
            find.assert_not_called()

    def test_bounded_command_requires_guard_and_collected_result(self):
        with patch.object(transport,'find_adb',return_value='/synthetic/adb'):
            device=transport._QualificationAdbDevice('emulator-5554','/tmp')
        with self.assertRaises(ContractError):device._command(('never-run',))
        device.guard=Mock();device.owner=Mock()
        device.owner.run.return_value=SimpleNamespace(terminated=True,bounded=True,interrupted=False,returncode=0,stdout=b'1\n')
        device.verify_emulator();self.assertEqual(device.owner.run.call_args.kwargs['max_output_bytes'],65536)
        self.assertEqual(device.guard.call_count,2)
        for field in ('terminated','bounded'):
            setattr(device.owner.run.return_value,field,False)
            with self.assertRaises(ContractError):device.verify_emulator()
            setattr(device.owner.run.return_value,field,True)

    def test_emulator_property_must_be_measured_one(self):
        with patch.object(transport,'find_adb',return_value='/synthetic/adb'):
            device=transport._QualificationAdbDevice('emulator-5554','/tmp')
        for value in (b'0\n',b'',b'yes'):
            with patch.object(device,'_fixed',return_value=value),self.assertRaises(ContractError):device.verify_emulator()

    def test_exact_helper_process_identity_and_safe_fixed_paths(self):
        with patch.object(transport,'find_adb',return_value='/synthetic/adb'):
            device=transport._QualificationAdbDevice('emulator-5554','/tmp')
        stat=b'123 (io.reproof.live) '+b' '.join([b'S']+[b'0']*18+[b'789']+[b'0']*5)
        with patch.object(device,'_fixed',side_effect=[b'123\n',b'io.reproof.live\0',stat]) as fixed:
            self.assertEqual(device.helper_process(),{'pid':123,'startTicks':'789'})
            self.assertEqual(fixed.call_args.args,('cat','/proc/123/stat'))
        with patch.object(device,'_fixed',return_value=b'123 124'),self.assertRaises(ContractError):device.helper_process()
        with patch.object(device,'_fixed',side_effect=[b'123',b'other.app\0']),self.assertRaises(ContractError):device.helper_process()

    def test_configuration_is_defensive_copy_and_cannot_add_effects(self):
        value=configuration();selected=producer();selected._configure(value);value['phase']='probe'
        self.assertEqual(selected.qualification_configuration['phase'],'session')
        modified=configuration();modified['command']='arbitrary'
        with self.assertRaises(ContractError):selected._configure(modified)

    def test_installed_identity_never_installs(self):
        selected=producer();selected.target_package='example.app'
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'app.apk';path.write_bytes(b'inert');sha=hashlib.sha256(b'inert').hexdigest()
            selected.device.apk_identity.return_value={'package':'example.app','versionCode':1}
            selected.device._fixed.side_effect=[b'package:/data/app/exact/base.apk\n',f'{sha}  /data/app/exact/base.apk\n'.encode()]
            selected._verify_installed('example.app',path,sha,1)
        self.assertEqual([call.args[0] for call in selected.device._fixed.call_args_list],['pm','sha256sum'])
        selected.device.install.assert_not_called()

    def test_split_or_changed_installed_apk_rejected(self):
        selected=producer();selected.target_package='example.app';selected.device.apk_identity.return_value={'package':'example.app','versionCode':1}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'app.apk';path.write_bytes(b'inert');sha=hashlib.sha256(b'inert').hexdigest()
            for raw in (b'package:/data/app/a/base.apk\npackage:/data/app/a/split.apk',b'package:/data/app/../bad.apk'):
                selected.device._fixed.return_value=raw
                with self.assertRaises(ContractError):selected._verify_installed('example.app',path,sha,1)

    def _terminal(self,selected):
        identity={'pid':123,'startTicks':'789'}
        selected._retirement={'nativeProcessIdentity':identity,'evidence':{key:True for key in (
            'normalAdmissionClosed','commandQueueDrained','inflightCompleted','pointersCancelled',
            'captureWorkersStopped','inspectionMediaDrained','publicationClosed')}}
        return identity

    def test_exit_dispatch_measures_exact_process_before_request(self):
        selected=producer();identity=self._terminal(selected);issued,wire=grant('old-exit')
        wire['configurationDigest']=selected.configuration_digest;selected.device.helper_process.return_value=identity
        selected.recovery_call(issued)
        selected.device.helper_process.assert_called_once_with(identity)
        self.assertEqual(selected._preexit_process,identity)
        self.assertEqual(selected.transport.call.call_args.args[0],'/qualification-recovery')

    def test_bare_absence_or_missing_terminal_cannot_authorize_exit(self):
        selected=producer();issued,wire=grant('old-exit');wire['configurationDigest']=selected.configuration_digest
        with self.assertRaises(ContractError):selected.recovery_call(issued)
        with self.assertRaises(ContractError):selected.collect_exit(issued._issuer,issued)
        selected.device._fixed.assert_not_called()

    def test_authenticated_terminal_plus_preexit_identity_and_collected_group_required(self):
        selected=producer();identity=self._terminal(selected);issued,wire=grant('old-exit')
        wire['configurationDigest']=selected.configuration_digest
        selected._preexit_process=identity;selected._exit_receipt={'grant':wire,'nativeProcessIdentity':identity}
        selected.device.helper_process.return_value=None
        with patch.object(transport._ProcessOwner,'_group_empty',return_value=True):
            result=selected.collect_exit(issued._issuer,issued)
        self.assertTrue(result['remoteHelperAbsent']);self.assertTrue(result['clientCollected'])
        self.assertEqual(selected.token,'');selected.device._fixed.assert_called_once_with('test','!','-e','/proc/123')

    def test_changed_remote_process_or_uncollected_host_keeps_token(self):
        selected=producer();identity=self._terminal(selected);issued,wire=grant('old-exit')
        wire['configurationDigest']=selected.configuration_digest
        selected._preexit_process=identity;selected._exit_receipt={'grant':wire,'nativeProcessIdentity':identity}
        selected.device.helper_process.side_effect=ContractError('changed')
        with self.assertRaises(ContractError):selected.collect_exit(issued._issuer,issued)
        self.assertEqual(selected.token,'ephemeral');selected.device._fixed.assert_not_called()

    def test_probe_start_is_fresh_and_has_no_normal_poller_or_launch(self):
        profile,_=fixture()
        with patch.object(transport,'find_adb',return_value='/synthetic/adb'):
            old=transport.QualificationAndroidProvider(Mock(),'emulator-5554','helper.apk','app.apk',
                runtime_profile=profile,helper_digest='b'*64,work='/tmp')
        old._configure(configuration());old._exit_receipt={'nativeProcessIdentity':{'pid':123,'startTicks':'789'}}
        old.token='';old.process=Mock();old.process.poll.return_value=0;old.device=Mock()
        old.helper_incarnation='oldhelper';old.provider_incarnation='oldprovider';old.host_incarnation='host'
        issuer=Mock();selected=transport._ProbeProducer(old,dict(configuration(),phase='probe'),'freshhelper','freshprovider',issuer)
        self.assertTrue(selected.token);self.assertIsNone(selected.thread)
        status={'recoveryQualificationVersion':1,'recoveryQualificationConfigurationDigest':selected.configuration_digest,
            'helperIncarnation':'freshhelper','providerIncarnation':'freshprovider','hostIncarnation':'host',
            'nativeProcessIdentity':{'pid':124,'startTicks':'790'}}
        def spawn(config):
            self.assertEqual(config['helperIncarnation'],'freshhelper')
            selected.process=Mock();selected.process.poll.return_value=None
            selected.transport=Mock();selected.transport.call.return_value=status
        with patch.object(selected,'_spawn',side_effect=spawn),patch.object(selected,'_verify_installed') as installed:
            self.assertIs(selected.start(),selected)
        self.assertEqual(installed.call_count,2);self.assertIsNone(selected.thread)
        selected.transport.call.assert_called_once_with('/qualification-recovery/status')
        selected.device.helper_process.assert_called_once_with(status['nativeProcessIdentity'])
        copy_status=selected.startup_status;copy_status['hostIncarnation']='changed'
        self.assertEqual(selected.startup_status['hostIncarnation'],'host')

    def test_spawn_writes_only_fixed_helper_configuration_and_has_guard_before_process(self):
        selected=producer();selected.app_profile=fixture()[0];selected.target_package=selected.app_profile.data['package']
        selected.device.adb='/synthetic/adb';selected.device.serial='emulator-5554'
        selected.device.adb_call.return_value=b'4567'
        selected.configuration_digest=selected.configuration_digest
        config=selected._config('helper','host','provider')
        with patch.object(transport.subprocess,'Popen',return_value=Mock()) as spawn:
            selected._spawn(config)
        command=selected.device._run.call_args.args[0]
        self.assertEqual(command[:5],('/synthetic/adb','-s','emulator-5554','shell','-T'))
        self.assertNotIn('install',command);self.assertNotIn('pm clear',str(command))
        self.assertEqual(spawn.call_args.kwargs['stdout'],transport.subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs['stderr'],transport.subprocess.DEVNULL)
        selected.device.guard.side_effect=ContractError('revoked')
        with patch.object(transport.subprocess,'Popen') as spawn,self.assertRaises(ContractError):selected._spawn(config)
        spawn.assert_not_called()

    def test_local_artifact_links_and_size_bounds_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'app.apk';path.write_bytes(b'inert');link=Path(temp)/'link.apk';link.symlink_to(path)
            self.assertEqual(transport._digest_file(path,5),hashlib.sha256(b'inert').hexdigest())
            with self.assertRaises(Exception):transport._digest_file(link,5)
            with self.assertRaises(ContractError):transport._digest_file(path,4)

    def test_retained_authenticated_receipt_is_not_mutable_via_returned_json(self):
        selected=producer();issued,wire=grant();wire['configurationDigest']=selected.configuration_digest
        selected.recovery_call(issued)
        receipt={'grant':wire,'status':'complete','kind':'retirement','evidence':{'normalAdmissionClosed':True}}
        selected.transport.call.return_value={'pending':False,'receipt':receipt}
        observed=selected.recovery_result(issued);observed['receipt']['evidence']['normalAdmissionClosed']=False
        self.assertIs(selected._retirement['evidence']['normalAdmissionClosed'],True)

    def test_receipt_cannot_be_imported_or_bound_to_other_grant(self):
        selected=producer();issued,wire=grant();wire['configurationDigest']=selected.configuration_digest
        with self.assertRaises(ContractError):selected.recovery_result(issued)
        selected.recovery_call(issued)
        selected.transport.call.return_value={'pending':False,'receipt':{'grant':{},'status':'complete','kind':'retirement'}}
        with self.assertRaises(ContractError):selected.recovery_result(issued)
        self.assertIsNone(selected._retirement)
