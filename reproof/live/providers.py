"""Local providers. Native commands are serialized and acknowledged, never retried."""
from __future__ import annotations
import base64
import json
import hashlib
import os
from pathlib import Path
import plistlib
import queue
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from .model import LiveError,check
from .authority import HELPER_VERSION,NATIVE_PROTOCOL_VERSION
from ..storage import Lease
from ..core import ContractError,digest
from ..ios_storage import app_info, tree_manifest
from ..ios_cases import case_spec
from ..ios_runner import (_require_profile_fixture,
                          prepare_xctestrun, _targets)
from ..ios_profile import IosAppProfile, validate_ios_profile

ACTIONS=['tap','long_press','swipe','text','home','reset']
CAPABILITIES={'actions':ACTIONS,'inputMode':'gesture-batch','media':'sampled-jpeg','multitouch':False,'timing':'best-effort','resetContract':'sample-counter-fixture-v1'}

# XCTest startup on a cold Simulator can exceed the old 15-second handshake
# wait.  This remains one bounded window shared by the authenticated waiter and
# its monitor; it never renews the original dispatch permit.
IOS_STARTUP_TIMEOUT_SECONDS = 90.0
IOS_STARTUP_POLL_SECONDS = 0.25


def require_runtime_frame(profile,width,height,orientation):
    if profile is None:return
    geometry=profile.data['capabilities']['geometry']
    check(type(width) is int and type(height) is int
          and 0<width<=geometry['maxWidth'] and 0<height<=geometry['maxHeight']
          and type(orientation) is str and orientation in geometry['orientations'],
          'native_profile_mismatch','Native frame exceeds the declared runtime geometry',409)


def _require_ios_runtime_capabilities(profile, app=None):
    capabilities = profile.data['capabilities']
    observations = set(capabilities['observations'])
    check('pixels' in observations
          and capabilities['captureAdapter'] == {
              'id': 'native-frame', 'version': 1},
          'unsupported_operation',
          'iOS Live requires its declared native frame adapter', 400)
    check(capabilities['locator'] is None
          and 'accessibility' not in observations,
          'unsupported_operation',
          'This iOS helper does not provide general accessibility locators', 400)
    if capabilities['logAdapter'] is not None:
        from ..ios_instrumentation import profile_from_app, app_logs_enabled
        from ..ios_observation import is_observation_profile
        try:
            automatic = profile_from_app(app) if app is not None else None
            accepted = (is_observation_profile(automatic) and automatic.data['applicationId'] == profile.bundle
                        and app_logs_enabled(app))
        except (ContractError, OSError, ValueError):
            accepted = False
        check(accepted, 'unsupported_operation',
              'The selected iOS app must embed its configured observation adapter', 400)


def _exact_flat_mapping(actual,expected):
    return (isinstance(actual,dict) and set(actual)==set(expected)
            and all(type(actual[key]) is type(value) and actual[key]==value
                    for key,value in expected.items()))


def require_ios_helper_v2(document):
    targets=[target for _,target in _targets(document) if target.get('BlueprintName')=='ReproLiveTests']
    check(len(targets)==1,'startup_rejected','iOS helper artifact is ambiguous',400)
    bundle=Path(targets[0].get('TestBundlePath','')).resolve()
    info=bundle/'Info.plist'
    check(info.is_file() and not info.is_symlink(),'startup_rejected','iOS helper metadata is missing',400)
    try:
        with info.open('rb') as stream:value=plistlib.load(stream)
    except (OSError,ValueError,plistlib.InvalidFileException):
        raise LiveError('startup_rejected','iOS helper metadata is invalid',400) from None
    check(type(value.get('ReproLiveProtocolVersion')) is int
          and type(value.get('ReproLiveHelperVersion')) is int
          and value.get('ReproLiveProtocolVersion')==NATIVE_PROTOCOL_VERSION
          and value.get('ReproLiveHelperVersion')==HELPER_VERSION,
          'startup_rejected','iOS helper artifact has an incompatible version',400)


class DemoProvider:
    def recover(self):return {'ok':True}
    def start(self,session,lab):
        self.sid=session['id'];self.lab=lab;self.count=0;self.stop=threading.Event();self.render()
        self.thread=threading.Thread(target=self._frames,daemon=True);self.thread.start()
    def _frames(self):
        while not self.stop.wait(.5):self.render()
    def render(self):
        svg=f'''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="800"><rect width="400" height="800" fill="#101a2c"/><g fill="white" text-anchor="middle" font-family="sans-serif"><text x="200" y="110" font-size="22">Synthetic demo device</text><text x="200" y="280" font-size="72">{self.count}</text><rect x="70" y="370" width="260" height="80" rx="20" fill="#2563eb"/><text x="200" y="420" font-size="24">Tap to add</text><text x="200" y="700" font-size="16">Demo · no physical device</text></g></svg>'''
        self.lab.publish_frame(self.sid,svg.encode(),'image/svg+xml',400,800)
    def execute(self,action,payload):
        if action=='reset':self.count=0
        elif action in {'tap','long_press'} and .175<=payload['x']<=.825 and .4625<=payload['y']<=.5625:self.count+=1
        self.render();return {'ok':True,'timing':'best-effort'}
    def close(self):self.stop.set();self.thread.join(timeout=2)


class IosProvider:
    def __init__(self,udid,products,bundle,identity=None,*,app=None,fixture='counter',record_sdk=False,app_logs_only=False,
                 profile=None):
        if profile is not None and not isinstance(profile,IosAppProfile):profile=validate_ios_profile(profile)
        if profile is not None:
            _require_ios_runtime_capabilities(profile, app)
            check(app is not None and bundle==profile.bundle,'recording_identity',
                  'General iOS Simulator profile requires its selected app',400)
            fixture=None;record_sdk=False
            app_logs_only=profile.data['capabilities']['logAdapter'] is not None
        self.udid=udid;self.products=Path(products).resolve();self.bundle=bundle;self.identity=identity
        self.profile=profile
        self.app=Path(app).resolve() if app is not None else None
        self.fixture=fixture;self.record_sdk=record_sdk
        self.app_logs_only=app_logs_only
        self.auto_profile=None;self.auto_run_id=None;self.auto_marker=None;self.capture_started_at=None
        self.automatic_app_logs=False;self.app_log_marker=None
        self.token=secrets.token_urlsafe(32);self.commands=queue.Queue(maxsize=1)
        self.pending={};self.lock=threading.Lock();self.stop=threading.Event();self.process=None;self.temp=None;self.lease=None
        self.device_authority=None;self.provider_incarnation=None
        self.native_handshake=None;self.handshake_ready=None;self._startup_permit=None
        self._startup_lock=threading.RLock();self._startup_accepting=False;self._startup_deadline=0.0
        self.frame_clock=None;self.last_native_frame=0
    def bind_authority(self,device_authority,provider_incarnation):
        self.device_authority=device_authority;self.provider_incarnation=provider_incarnation
        self.lease=device_authority.borrowed_lease()
    def _check_permit(self,permit):
        check(self.device_authority is not None and permit is not None,
              'authority_required','A current device authority permit is required')
        self.device_authority.check_dispatch_permit(permit)

    def _startup_window_open_locked(self):
        check(not self.stop.is_set() and self._startup_accepting
              and time.monotonic() < self._startup_deadline,
              'startup_rejected','iOS helper startup window is closed',409)
        self._check_permit(self._startup_permit)

    def _require_startup_window(self):
        with self._startup_lock:
            self._startup_window_open_locked()

    def _abort_startup(self):
        with self._startup_lock:
            self._startup_accepting=False
        # Stop is a local cancellation fence.  Durable authority revocation
        # remains the Lab failure path, so this helper never creates or renews
        # authority while trying to clean up a late XCTest callback.
        self.stop.set()

    def start_authorized(self,session,lab,permit):
        self._check_permit(permit)
        self._startup_permit=permit;self.handshake_ready=threading.Event()
        with self._startup_lock:
            self._startup_accepting=True
            self._startup_deadline=time.monotonic()+IOS_STARTUP_TIMEOUT_SECONDS
        succeeded=False
        try:
            self.start(session,lab,permit=permit)
            while True:
                self._require_startup_window()
                process=self.process
                if process is not None and process.poll() is not None:
                    raise LiveError('startup_rejected',
                                    'Native driver stopped before startup',409)
                remaining=self._startup_deadline-time.monotonic()
                check(remaining>0,'native_protocol_mismatch',
                      'iOS helper handshake did not complete',409)
                if self.handshake_ready.wait(min(IOS_STARTUP_POLL_SECONDS,remaining)):
                    # The event alone is not authority.  A permit may have
                    # expired or been revoked while the native callback ran.
                    self._require_startup_window()
                    check(self.native_handshake is not None,
                          'native_protocol_mismatch','iOS helper handshake did not complete',409)
                    succeeded=True
                    result={'ok':True}
                    if self.profile is not None:
                        result['identityEvidence']={'bundle':self.bundle,
                            'selectedArtifactDigest':self.profile.data['artifact']['sha256'],
                            'installedArtifactDigest':self.identity['artifactDigest'],
                            'installedDigestProof':'readable-simulator-app-tree',
                            'profileDigest':self.profile.digest,
                            'helperProtocolVersion':NATIVE_PROTOCOL_VERSION,
                            'helperVersion':HELPER_VERSION}
                    return result
        finally:
            if not succeeded:self._abort_startup()
            else:
                with self._startup_lock:self._startup_accepting=False
    def start(self,session,lab,permit=None):
        self.sid=session['id'];self.lab=lab
        from .native_frame_clock import NativeFrameClock
        self.frame_clock=NativeFrameClock.for_session(lab,self.sid,self.device_authority)
        self.capture_started_at=int(time.time()*1000)
        install_app = self.app
        if self.profile is not None:
            from .ios_artifact import stage_simulator_app
            try:
                _general_ios_simulator_identity(self.app, self.profile)
                self.temp=tempfile.TemporaryDirectory(prefix='repro-live-')
                install_app=stage_simulator_app(self.app,Path(self.temp.name).resolve()/'Selected.app',
                    expected_digest=self.profile.data['artifact']['sha256'],
                    expected_bytes=self.profile.data['artifact']['bytes'])
            except (ContractError,OSError):
                if self.temp is not None:self.temp.cleanup();self.temp=None
                raise LiveError('startup_rejected','Selected Simulator app could not be frozen',409) from None
        self._install_app = install_app
        if self.device_authority is None:self.lease=Lease('ios-simulator:'+self.udid)
        self.lease.__enter__()
        if self.app is not None:
            if self.profile is not None:
                expected_identity={'bundle':self.bundle,'artifactDigest':self.profile.data['artifact']['sha256']}
            else:
                app_info(self.app)
                expected_identity={'bundle':self.bundle,'artifactDigest':digest(tree_manifest(self.app))}
            if self.device_authority is not None:
                # The v2 helper calls ready before launching the target.  Defer
                # installation to that authenticated callback so the native
                # version/clock/incarnation handshake precedes target mutation.
                self._pending_install_identity=expected_identity
            else:
                install=subprocess.run(['/usr/bin/xcrun','simctl','install',self.udid,str(install_app)],
                                       capture_output=True,timeout=90)
                check(install.returncode==0,'app_changed','Could not install the selected Simulator app')
                self.identity=installed_identity(self.udid,self.bundle)
                check(self.identity==expected_identity,'app_changed','Installed Simulator app differs from selected app')
        else:
            check(self.identity==installed_identity(self.udid,self.bundle),'app_changed','Installed app changed; restart the local server')
        if (self.record_sdk or self.app_logs_only) and self.app is not None:
            from ..ios_instrumentation import profile_from_app,app_logs_enabled
            self.auto_profile=profile_from_app(install_app)
            self.automatic_app_logs=app_logs_enabled(install_app)
            check(self.profile is None or self.automatic_app_logs,
                  'app_changed','The declared iOS log adapter is absent from the selected app',400)
        self.auto_run_id=str(uuid.uuid4()) if self.auto_profile is not None else None
        if self.temp is None:self.temp=tempfile.TemporaryDirectory(prefix='repro-live-')
        config=prepare_xctestrun(self.products,'ReproLiveTests',Path(self.temp.name)/'live.xctestrun')
        with config.open('rb') as stream:document=plistlib.load(stream)
        if self.device_authority is not None:require_ios_helper_v2(document)
        for _,target in _targets(document):
            target.setdefault('EnvironmentVariables',{}).update(REPRO_LIVE_URL=f'{lab.base_url}/bridge/{self.sid}',
                REPRO_LIVE_TOKEN=self.token,REPRO_TARGET_BUNDLE=self.bundle)
            if self.profile is None:
                target['EnvironmentVariables'].update(REPRO_LIVE_CASE=self.fixture,
                    REPRO_LIVE_RECORD_SDK='1' if self.record_sdk else '0')
            else:
                target['EnvironmentVariables'].update(
                    REPRO_LIVE_GENERAL_PROFILE_DIGEST=self.profile.digest,
                    REPRO_LIVE_APPLICATION_ID=self.profile.data['applicationId'],
                    REPRO_LIVE_GENERAL_ACTIONS=','.join(
                        self.profile.data['capabilities']['actions']))
            if self.device_authority is not None:
                target['EnvironmentVariables'].update(
                    REPRO_LIVE_PROTOCOL_VERSION=str(NATIVE_PROTOCOL_VERSION),
                    REPRO_LIVE_HELPER_VERSION=str(HELPER_VERSION),
                    REPRO_LIVE_HELPER_INCARNATION=permit.helper_incarnation,
                    REPRO_LIVE_HOST_INCARNATION=permit.host_incarnation,
                    REPRO_LIVE_PROVIDER_INCARNATION=permit.provider_incarnation)
            if self.auto_profile is not None:
                target['EnvironmentVariables'].update(REPRO_LIVE_AUTO_RUN_ID=self.auto_run_id,
                    REPRO_LIVE_AUTO_PROFILE_DIGEST=self.auto_profile.digest)
        with config.open('wb') as stream:plistlib.dump(document,stream)
        # XCTest logs may contain typed text and environment values: discard output and
        # keep all automatically produced results inside this private temporary directory.
        command=['/usr/bin/xcodebuild','test-without-building','-xctestrun',str(config),'-destination',f'id={self.udid}',
                 '-resultBundlePath',str(Path(self.temp.name)/'result.xcresult'),'-parallel-testing-enabled','NO',
                 '-only-testing:ReproLiveTests/LiveControlTests/testControlSession']
        if self.device_authority is not None:self._check_permit(permit)
        self.process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        self.monitor=threading.Thread(target=self._monitor,daemon=True);self.monitor.start()
    def _monitor(self):
        started=time.monotonic()
        with self._startup_lock:
            startup_deadline=self._startup_deadline if self._startup_deadline > 0 else None
        while not self.stop.wait(.5):
            if self.process.poll() is not None:
                self._abort_startup()
                self.lab.fail(self.sid,'Native driver stopped; close and reopen the session');return
            deadline=(startup_deadline if startup_deadline is not None
                      else started+IOS_STARTUP_TIMEOUT_SECONDS)
            if (time.monotonic()>=deadline
                    and self.lab._session(self.sid)['state']=='connecting'):
                self._abort_startup()
                self.lab.fail(self.sid,'Native driver startup timed out');return
    def execute(self,action,payload):return self._execute(action,payload,None)
    def execute_authorized(self,action,payload,permit,frame=None):
        self._check_permit(permit)
        return self._execute(action,payload,permit)
    def _execute(self,action,payload,permit):
        if action == 'authority_cleanup':
            check(permit is not None, 'authority_required', 'Native cleanup requires its current authority permit', 400)
        elif self.profile is not None:
            check(action in self.profile.data['capabilities']['actions'],
                  'unsupported_operation','General iOS profile does not allow this action',400)
        if action=='reset':check(self.identity==installed_identity(self.udid,self.bundle),'app_changed','Installed app changed; close the session')
        payload=dict(payload or {})
        restart_logs = action == 'launch' and self.profile is not None and self.app_logs_only
        if action=='reset' or restart_logs:
            self.capture_started_at=int(time.time()*1000)
            self.auto_marker=None
            self.app_log_marker=None
            if self.auto_profile is not None:
                self.auto_run_id=str(uuid.uuid4());payload['autoRunId']=self.auto_run_id
        command={'id':permit.operation_id if permit is not None else uuid.uuid4().hex,
                 'action':action,'payload':payload}
        if permit is not None:
            check(self.native_handshake is not None,'native_protocol_mismatch','iOS helper handshake is unavailable')
            self._check_permit(permit)
            command['authority']=self.device_authority.native_grant(permit,self.native_handshake).wire()
        event=threading.Event()
        pending={'event':event,'result':None,'authority':command.get('authority')}
        with self.lock:self.pending[command['id']]=pending
        try:
            self.commands.put_nowait(command)
            check(event.wait(25),'native_timeout','Native input acknowledgement timed out')
            result=pending['result']
            if (action=='reset' or restart_logs) and self.auto_profile is not None:
                if self.app_logs_only:self._wait_for_app_log_marker()
                else:self._wait_for_auto_marker()
            return result
        finally:
            with self.lock:self.pending.pop(command['id'],None)
    def _simulator_json(self,relative,max_bytes=1024*1024):
        result=subprocess.run(['/usr/bin/xcrun','simctl','get_app_container',self.udid,self.bundle,'data'],
                              capture_output=True,text=True,timeout=15)
        check(result.returncode==0,'app_unavailable','Installed Simulator app data is unavailable')
        root=Path(result.stdout.strip()).resolve();path=(root/relative).resolve()
        check(path.is_relative_to(root) and path.is_file() and not path.is_symlink()
              and path.stat().st_size<=max_bytes,'capture_invalid','Simulator app JSON is invalid')
        from ..storage import read_json
        return read_json(path)
    def _pin_auto_marker(self, expected_fixture=None):
        if self.auto_profile is None or self.auto_run_id is None:return None
        expected_fixture=expected_fixture or case_spec(self.fixture).fixture
        _require_profile_fixture(self.auto_profile, expected_fixture)
        try:marker=self._simulator_json('Library/Application Support/ReproLoop/auto-session.json')
        except (ContractError, LiveError, OSError, ValueError, json.JSONDecodeError):return None
        from ..ios_runner import validate_ios_auto_marker
        result=subprocess.run(['/usr/bin/xcrun','simctl','get_app_container',self.udid,self.bundle,'app'],
                              capture_output=True,text=True,timeout=15)
        check(result.returncode==0,'app_unavailable','Installed Simulator app identity is unavailable')
        build_id=app_info(Path(result.stdout.strip()))['buildId']
        validate_ios_auto_marker(marker,self.auto_profile,run_id=self.auto_run_id,
                                 build_id=build_id,fixture=expected_fixture,
                                 min_started_at=self.capture_started_at)
        self.auto_marker=marker;return marker
    def _wait_for_auto_marker(self, timeout=10):
        deadline=time.monotonic()+min(timeout,10)
        while time.monotonic()<deadline:
            marker=self._pin_auto_marker()
            if marker is not None:return marker
            time.sleep(.1)
        raise LiveError('capture_invalid','Automatic iOS session marker did not become ready',400)
    def collect_sdk_capture_authorized(self,permit):
        self._check_permit(permit)
        return self.collect_sdk_capture(permit=permit)
    def collect_sdk_capture(self,permit=None):
        check(self.record_sdk,'unsupported_operation','SDK capture is not configured',400)
        check(not self.app_logs_only,'unsupported_operation','This session records app observations only',400)
        if self.auto_profile is None:
            result=self._execute('report_capture',{},permit)
            check(result.get('ok') is True,'capture_invalid','iOS SDK capture was not finalized',400)
            from ..ios_runner import IosSimulator
            if permit is not None:self._check_permit(permit)
            return IosSimulator(self.udid).collect_capture(self.capture_started_at)
        check(self.auto_run_id is not None,
              'unsupported_operation','Automatic iOS capture profile is unavailable',400)
        result=self._execute('report_capture',{},permit)
        check(result.get('ok') is True,'capture_invalid','Automatic iOS capture was not finalized',400)
        from ..ios_runner import IosSimulator
        reader=IosSimulator(self.udid)
        if permit is not None:self._check_permit(permit)
        capture=reader.collect_capture(self.capture_started_at,expected_run_id=self.auto_run_id,
                                        auto_profile=self.auto_profile,
                                        expected_fixture=case_spec(self.fixture).fixture)
        self.auto_marker=reader.read_app_json('Library/Application Support/ReproLoop/auto-session.json')
        return capture
    def collect_sdk_diagnostics_authorized(self,capture,permit):
        self._check_permit(permit)
        return self.collect_sdk_diagnostics(capture)
    def collect_sdk_diagnostics(self,capture):
        check(self.auto_profile is not None and self.auto_run_id is not None,
              'unsupported_operation','Automatic iOS capture profile is unavailable',400)
        from ..ios_runner import IosSimulator
        return IosSimulator(self.udid).collect_auto_diagnostics(
            capture,self.auto_run_id,self.auto_profile,expected_fixture=case_spec(self.fixture).fixture)
    def _read_app_log_json(self,relative):
        from ..app_logs import MAX_APP_LOG_BYTES
        return self._simulator_json('Library/Application Support/ReproLoop/'+relative,MAX_APP_LOG_BYTES)
    def collect_app_logs_authorized(self,permit):
        self._check_permit(permit)
        result = self.collect_app_logs()
        self._check_permit(permit)
        return result
    def collect_app_logs(self):
        from ..app_logs import collect_app_logs,IDENTITY_KEYS
        check(self.automatic_app_logs and self.auto_profile is not None and self.auto_run_id is not None,
              'unsupported_operation','Automatic app logs are not configured',400)
        profile=self.auto_profile.data
        try:
            value=collect_app_logs(self._read_app_log_json,platform='ios',application_id=self.bundle,
                profile_digest=self.auto_profile.digest,run_id=self.auto_run_id,
                click_targets=set(profile['tapTargets'])|({profile['backTarget']} if profile.get('backTarget') else set()),
                screen_targets=set(profile['screenTargets'].values()),expected_marker=self.app_log_marker)
        except (ContractError,OSError,ValueError):
            raise LiveError('app_log_unavailable','A matching app log snapshot is not available yet',409) from None
        self.app_log_marker={key:value[key] for key in IDENTITY_KEYS}
        return value
    def _wait_for_app_log_marker(self,timeout=10):
        deadline=time.monotonic()+min(timeout,10)
        while time.monotonic()<deadline:
            try:return self.collect_app_logs()
            except LiveError:time.sleep(.1)
        raise LiveError('app_log_unavailable','Automatic app logging did not become ready',409)
    def bridge(self,operation,body):
        if operation=='next':
            if self.stop.is_set():return {'stop':True,'command':None}
            try:command=self.commands.get_nowait()
            except queue.Empty:command=None
            return {'stop':False,'command':command}
        if operation=='ready':
            if self.device_authority is not None:
                self._require_startup_window()
                expected={'capabilities','protocolVersion','helperVersion','helperIncarnation','hostIncarnation',
                          'providerIncarnation','nativeIncarnation','nativeClockId','nativeTimeMs'}
                check(isinstance(body,dict) and set(body)==expected,
                      'native_protocol_mismatch','iOS helper handshake is invalid',400)
                try:
                    with self._startup_lock:
                        self._startup_window_open_locked()
                        handshake=self.device_authority.bind_native_handshake(
                            self._startup_permit,protocol_version=body.get('protocolVersion'),
                            helper_version=body.get('helperVersion'),helper_incarnation=body.get('helperIncarnation'),
                            provider_incarnation=body.get('providerIncarnation'),native_incarnation=body.get('nativeIncarnation'),
                            native_clock_id=body.get('nativeClockId'),native_time_ms=body.get('nativeTimeMs'))
                        self._startup_window_open_locked()
                        self.native_handshake=handshake
                except ContractError:
                    raise LiveError('native_protocol_mismatch','iOS helper protocol is incompatible',400) from None
                if getattr(self,'frame_clock',None) is not None:
                    self.frame_clock.configure(self.native_handshake,body.get('capabilities'))
                expected_identity=getattr(self,'_pending_install_identity',None)
                if expected_identity is not None:
                    self._require_startup_window()
                    install=subprocess.run(['/usr/bin/xcrun','simctl','install',self.udid,str(self._install_app)],
                                           capture_output=True,timeout=90)
                    check(install.returncode==0,'app_changed','Could not install the selected Simulator app')
                    self._require_startup_window()
                    self.identity=installed_identity(self.udid,self.bundle)
                    check(self.identity==expected_identity,'app_changed','Installed Simulator app differs from selected app')
                    self._pending_install_identity=None
                self._require_startup_window()
                return {'ok':True,'authority':self.device_authority.native_grant(
                    self._startup_permit,self.native_handshake).wire()}
            else:
                if self.app_logs_only:self._wait_for_app_log_marker()
                elif self.auto_profile is not None:self._wait_for_auto_marker()
            return {'ok':True}
        if operation=='started':
            if self.device_authority is not None:self._require_startup_window()
            check(self.device_authority is not None and self.native_handshake is not None,
                  'native_protocol_mismatch','iOS startup authority is unavailable',400)
            expected=self.device_authority.native_grant(
                self._startup_permit,self.native_handshake).wire()
            check(isinstance(body,dict) and set(body)=={'authority'}
                  and _exact_flat_mapping(body.get('authority'),expected),
                  'authority_rejected','iOS startup acknowledgement is not bound',409)
            self._check_permit(self._startup_permit)
            if self.app_logs_only: self._wait_for_app_log_marker()
            if self.handshake_ready is not None:self.handshake_ready.set()
            return {'ok':True}
        if operation=='ack':
            with self.lock:
                pending=self.pending.get(body.get('id'))
                check(pending is not None,'unknown_command','Unknown native command',409)
                keys=set(body)
                legacy_valid=(self.device_authority is None and {'id','ok'}<=keys
                              and keys<={'id','ok','timing','error'}
                              and ('timing' not in body or body['timing']=='best-effort'))
                expected={'id','ok','timing','authority'}|({'error'} if 'error' in body else set())
                shared_valid=(self.device_authority is not None and keys==expected
                              and body.get('timing')=='best-effort'
                              and _exact_flat_mapping(body.get('authority'),pending['authority']))
                check((legacy_valid or shared_valid) and type(body.get('ok')) is bool,
                      'authority_rejected','Native acknowledgement is not bound',409)
                result={'ok':body.get('ok') is True,'timing':'best-effort'}
                error=body.get('error')
                if result['ok'] is False and isinstance(error,str) and re.fullmatch(r'[a-z_]{1,64}',error):
                    result.update(outcome='rejected',code=error)
                pending['result']=result;pending['event'].set()
            return {'ok':True}
        if operation in {'clock-start','clock-end'}:
            clock=getattr(self,'frame_clock',None)
            check(clock is not None and self.native_handshake is not None
                  and self.handshake_ready is not None and self.handshake_ready.is_set(),
                  'native_timing_invalid','Native clock startup is incomplete',409)
            return (clock.begin_exchange(body) if operation=='clock-start'
                    else clock.finish_exchange(body))
        if operation=='frame':
            require_runtime_frame(self.profile,body.get('width'),body.get('height'),body.get('orientation'))
            try:data=base64.b64decode(body['imageBase64'],validate=True)
            except (ValueError,KeyError,TypeError):raise LiveError('invalid_frame','Invalid native image',400)
            native_frame_id=body.get('nativeFrameId')
            if type(native_frame_id) is int and native_frame_id>0:
                if native_frame_id<=getattr(self,'last_native_frame',0):return {'ok':True}
            clock=getattr(self,'frame_clock',None)
            timing=(clock.frame_arguments(body) if clock is not None else
                    {'timing_source':'native-unmapped'})
            self.lab.publish_frame(self.sid,data,body.get('mime'),body.get('width'),body.get('height'),body.get('orientation'),body.get('capturedAt'),
                                   acquisition_sequence=(native_frame_id if type(native_frame_id) is int and native_frame_id>0 else None),
                                   **timing)
            if type(native_frame_id) is int and native_frame_id>0:self.last_native_frame=native_frame_id
            return {'ok':True}
        raise LiveError('not_found','Bridge operation not found',404)
    def close_authorized(self,permit):
        self._check_permit(permit)
        result=self.execute_authorized('authority_cleanup',{},permit)
        check(result.get('ok') is True,'cleanup_uncertain','iOS helper did not confirm target cleanup')
        return self._close(permit)
    def close(self):return self._close(None)
    def _close(self,permit):
        self.stop.set()
        if getattr(self,'frame_clock',None) is not None:self.frame_clock.close()
        if self.process is not None:
            try:self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:os.killpg(self.process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:os.killpg(self.process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    self.process.wait(timeout=5)
        if self.temp is not None:self.temp.cleanup();self.temp=None
        self.token=''
        if self.process is not None:
            check(self.process.returncode==0,'cleanup_uncertain','Native runner did not confirm a clean exit; device remains quarantined')
        if self.lease is not None:self.lease.__exit__(None,None,None);self.lease=None
        return {'ok':True}


def demo_device():
    return {'id':'demo','name':'Synthetic counter','platform':'demo','kind':'demo',
            'capabilities':dict(CAPABILITIES,media='demo-svg',recovery=True),'factory':DemoProvider}


def installed_identity(udid,bundle):
    result=subprocess.run(['/usr/bin/xcrun','simctl','get_app_container',udid,bundle,'app'],capture_output=True,timeout=15)
    check(result.returncode==0,'app_unavailable','Install the target app before starting Live')
    app=Path(result.stdout.decode().strip())
    return {'bundle':bundle,'artifactDigest':digest(tree_manifest(app))}


def _general_ios_simulator_identity(app,profile):
    app=Path(app).resolve();data=profile.data;artifact=data['artifact']
    check(app.is_dir() and not app.is_symlink(),'missing_build','Selected Simulator app is unavailable',400)
    with (app/'Info.plist').open('rb') as stream:info=plistlib.load(stream)
    manifest=tree_manifest(app);size=sum((app/name).stat().st_size for name in manifest)
    check(info.get('CFBundleIdentifier')==profile.bundle
          and info.get('CFBundleShortVersionString')==artifact['bundleVersion']
          and info.get('CFBundleVersion')==artifact['bundleBuild']
          and digest(manifest)==artifact['sha256'] and size==artifact['bytes'],
          'app_changed','iOS Simulator artifact differs from its runtime profile',400)
    return dict(profile.application_identity,installedDigestProof='pending-install')


def ios_device(udid,products,bundle,*,app=None,fixture='counter',record_sdk=False,
               authority_mode='shared-v2',profile=None):
    check(authority_mode in {'shared-v2','legacy-offline-v1'},'invalid_argument','Invalid authority compatibility mode',400)
    if profile is not None and not isinstance(profile,IosAppProfile):profile=validate_ios_profile(profile)
    if profile is not None:
        _require_ios_runtime_capabilities(profile, app)
        check(app is not None and bundle==profile.bundle,'recording_identity',
              'General iOS Simulator profile does not match its target',400)
    identity=(_general_ios_simulator_identity(app,profile) if profile is not None else
              {'bundle':bundle,'artifactDigest':digest(tree_manifest(Path(app).resolve()))}
               if app is not None else installed_identity(udid,bundle))
    public_id='ios-simulator-'+hashlib.sha256(udid.encode()).hexdigest()[:16]
    descriptor={'id':public_id,'name':'iOS Simulator','platform':'ios','kind':'ios-simulator',
            'capabilities':dict(CAPABILITIES,applicationIdentity=identity,fixture=fixture,sdkCapture=record_sdk,
                                resetContract='sample-'+fixture+'-fixture-v1' if bundle=='io.reproloop.sample.ios' else 'app-relaunch-only',
                                authorityMode=authority_mode),
            'factory':lambda:IosProvider(udid,products,bundle,identity,app=app,fixture=fixture,record_sdk=record_sdk,profile=profile)}
    if profile is not None:
        descriptor['capabilities'].update(
            actions=list(profile.data['capabilities']['actions']),fixture=None,
            sdkCapture=False,automaticAppLogs=profile.data['capabilities']['logAdapter'] is not None,
            resetContract=None,applicationProfile=profile.data,
            applicationProfileDigest=profile.digest,
            identityEvidence='selected-and-readable-simulator-app-tree',
            locatorKinds=[])
    if authority_mode=='shared-v2':descriptor['_authority']={'deviceKind':'ios-simulator','physicalId':udid}
    return descriptor
