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
from tests import test_live_authority as authority_tests


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
    result.process.pid=12345;result.token='ephemeral';result._startup_status=None
    return result


def grant(purpose='old-retire'):
    issuer=Mock();wire={'purpose':purpose,'operationId':'recovery_'+'c'*32,
        'configurationDigest':'unused'}
    result=SimpleNamespace(_issuer=issuer,wire=lambda:dict(wire))
    return result,wire


class ContextDouble:
    """Only a test substitute; production requires the exact isolated context."""
    def __init__(self):
        self.require_bound = Mock()
        self.run_shell = Mock(return_value=b'1\n')
        self.apk_identity = Mock()
        self.call_helper = Mock(return_value={})
        self.write_helper_configuration = Mock()
        self.start_instrumentation = Mock(return_value=Mock())
        self.collect_instrumentation = Mock(return_value=True)


class AndroidRecoveryTransportTests(unittest.TestCase):
    def setUp(self):
        from reproof.live import android_recovery_context
        selected = patch.object(android_recovery_context, 'IsolatedAdbContext', ContextDouble)
        selected.start()
        self.addCleanup(selected.stop)
        self.context = ContextDouble()

    def device(self, serial='emulator-5554', context=None):
        return transport._QualificationAdbDevice(serial, self.context if context is None else context,
            target_package='example.app', helper_digest='b'*64, app_digest='a'*64)

    def _startup(self,*,missing=False,late_failure=False):
        harness=startup_tests.AndroidStartupWaitTests();harness.setUp();self.addCleanup(harness.doCleanups)
        profile,_=fixture();owner=Mock();owner.bind_initial.return_value=configuration()
        provider=transport.QualificationAndroidProvider(owner,'emulator-5554','helper.apk','app.apk',
            runtime_profile=profile,helper_digest='b'*64,work='/tmp',context=self.context)
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
        profile, _ = fixture()
        provider = transport.QualificationAndroidProvider(Mock(), 'emulator-5554', 'helper.apk', 'app.apk',
            runtime_profile=profile, helper_digest='b'*64, work='/tmp', context=self.context)
        self.context.require_bound.assert_called_once()
        self.context.run_shell.assert_not_called()
        self.context.start_instrumentation.assert_not_called()
        self.assertIsNone(provider.process)
        self.assertIsNone(provider.fixture_config)
        self.assertFalse(provider.record_sdk)
        self.assertFalse(provider.automatic_app_logs)
        with self.assertRaises(ContractError):
            provider.close()

    def test_only_explicit_emulator_serial_and_exact_context_accepted(self):
        for serial in ('device123', 'auto', 'emulator-1', 'emulator-99999', 'emulator-5554;bad', None):
            with self.subTest(serial=serial), self.assertRaises(ContractError):
                self.device(serial)
        self.context.require_bound.assert_not_called()
        with self.assertRaises(ContractError):
            self.device(context=Mock())

    def test_fixed_shell_requires_guard_and_never_uses_direct_command(self):
        device = self.device()
        with self.assertRaises(ContractError):
            device.verify_emulator()
        device.guard = Mock()
        device.verify_emulator()
        self.context.run_shell.assert_called_once_with(('getprop', 'ro.kernel.qemu'),
            guard=device.guard, allow_absent=False)
        self.assertFalse(hasattr(device, '_command'))
        self.assertFalse(hasattr(device, 'adb_call'))

    def test_emulator_property_must_be_measured_one(self):
        device = self.device()
        for value in (b'0\n',b'',b'yes'):
            with patch.object(device,'_fixed',return_value=value),self.assertRaises(ContractError):device.verify_emulator()

    def test_exact_helper_process_identity_and_safe_fixed_paths(self):
        device = self.device()
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
        selected.device.context.collect_instrumentation.return_value = True
        result=selected.collect_exit(issued._issuer,issued)
        selected.device.context.collect_instrumentation.assert_called_once_with(selected.process, guard=issued._issuer.check)
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
        old=transport.QualificationAndroidProvider(Mock(),'emulator-5554','helper.apk','app.apk',
            runtime_profile=profile,helper_digest='b'*64,work='/tmp',context=self.context)
        old._configure(configuration());old._exit_receipt={'nativeProcessIdentity':{'pid':123,'startTicks':'789'}}
        old.token='';old._client_collected=True;old.process=Mock();old.process.poll.side_effect=AssertionError('Retired handle');old.device=Mock()
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
        selected.transport.call.assert_called_once_with('/qualification-recovery/status', guard=issuer.check)
        selected.device.helper_process.assert_called_once_with(status['nativeProcessIdentity'])
        copy_status=selected.startup_status;copy_status['hostIncarnation']='changed'
        self.assertEqual(selected.startup_status['hostIncarnation'],'host')

    def test_spawn_uses_only_context_configuration_and_owned_instrumentation(self):
        selected = producer()
        selected.app_profile = fixture()[0]
        selected.target_package = selected.app_profile.data['package']
        selected.device.context = self.context
        selected._ordinary_transport_guard = Mock()
        config = selected._config('helper', 'host', 'provider')
        selected._spawn(config)
        self.context.write_helper_configuration.assert_called_once()
        self.assertEqual(self.context.write_helper_configuration.call_args.kwargs['guard'], selected.device.guard)
        self.context.start_instrumentation.assert_called_once_with(guard=selected.device.guard)
        self.assertIs(selected.process, self.context.start_instrumentation.return_value)
        self.assertIs(selected.transport.context, self.context)
        self.context.run_shell.assert_not_called()
        self.context.start_instrumentation.reset_mock()
        self.context.write_helper_configuration.side_effect = ContractError('revoked')
        with self.assertRaises(ContractError):
            selected._spawn(config)
        self.context.start_instrumentation.assert_not_called()

    def test_gateway_preserves_ordinary_guard_and_never_lends_recovery_authority(self):
        ordinary, recovery = Mock(), Mock()
        bridge = transport._QualificationBridge(self.context, 'private-token', ordinary)
        bridge.call('/frame')
        self.assertIs(self.context.call_helper.call_args.kwargs['guard'], ordinary)
        bridge.call('/qualification-recovery/status', guard=recovery)
        self.assertIs(self.context.call_helper.call_args.kwargs['guard'], recovery)
        bridge.call('/frame')
        self.assertIs(self.context.call_helper.call_args.kwargs['guard'], ordinary)
        with self.assertRaises(ContractError):
            bridge.call('/command', {}, guard=recovery)
        probe = transport._QualificationBridge(self.context, 'new-token', recovery, probe=True)
        with self.assertRaises(ContractError):
            probe.call('/frame')
        probe.call('/qualification-recovery/status')
        self.assertIs(self.context.call_helper.call_args.kwargs['guard'], recovery)

    def test_real_authority_observation_survives_startup_confirmation_not_revocation(self):
        harness = authority_tests.AuthorityTestCase()
        harness.setUp()
        self.addCleanup(harness.tearDown)
        authority = harness.claim()
        self.addCleanup(authority.close)
        admission = authority.admit_operation(operation_id='startup', payload_digest='a'*64,
            session_id='session', sequence=1)
        permit = authority.prepare_dispatch(admission, provider_incarnation='provider')
        provider = transport.QualificationAndroidProvider.__new__(transport.QualificationAndroidProvider)
        provider.device_authority = authority
        provider.provider_incarnation = 'provider'
        provider.sid = 'session'
        provider.stop = threading.Event()
        session = {'provider': provider, 'deviceAuthority': authority, 'providerIncarnation': 'provider'}
        provider._fixture_session = session
        provider.lab = Mock()
        provider.lab._session.return_value = session
        provider._ordinary_binding = (authority, 'provider', session)
        self.context.call_helper.side_effect = lambda *args, **kwargs: (kwargs['guard']() or {})
        bridge = transport._QualificationBridge(self.context, 'private-token', provider._ordinary_transport_guard)
        bridge.call('/status')
        authority.confirm_operation(permit, authority_tests.ProviderResult('startup-receipt', 'succeeded', 'b'*64))
        with self.assertRaises(ContractError):
            authority.check_dispatch_permit(permit)
        bridge.call('/frame')
        session['providerIncarnation'] = 'replacement'
        with self.assertRaises(ContractError):
            bridge.call('/frame')
        session['providerIncarnation'] = 'provider'
        authority.revoke_dispatches()
        with self.assertRaises(ContractError):
            bridge.call('/frame')

    def test_context_failure_never_falls_back_to_direct_transport(self):
        bridge = transport._QualificationBridge(self.context, 'private-token', Mock())
        self.context.call_helper.side_effect = ContractError('unqualified')
        with self.assertRaises(ContractError):
            bridge.call('/status')
        source = Path(transport.__file__).read_text()
        for forbidden in ('find_adb(', 'subprocess.', 'HTTPConnection(', 'adb_call(', '_ProcessOwner', 'build-tools'):
            self.assertNotIn(forbidden, source)

    def test_uncollected_context_process_retains_ephemeral_token(self):
        selected = producer()
        identity = self._terminal(selected)
        issued, wire = grant('old-exit')
        wire['configurationDigest'] = selected.configuration_digest
        selected._preexit_process = identity
        selected._exit_receipt = {'grant': wire, 'nativeProcessIdentity': identity}
        selected.device.helper_process.return_value = None
        selected.device.context.collect_instrumentation.return_value = False
        with self.assertRaises(ContractError):
            selected.collect_exit(issued._issuer, issued)
        self.assertEqual(selected.token, 'ephemeral')

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
