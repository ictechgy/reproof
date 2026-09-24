"""Persistent Android instrumentation over an authenticated ADB-forwarded port."""
from __future__ import annotations
import base64
from collections import OrderedDict
import http.client
import json
from pathlib import Path
import secrets
import shlex
import signal
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from .model import LiveError,check
from ..device import AdbDevice,DeviceError
from ..storage import sha_file
from ..core import compile_capture,ContractError
from ..android_profile import (AndroidAppProfile, AndroidRuntimeProfile,
                               sample_app_profile, validate_app_profile,
                               validate_android_runtime_profile)
from .authority import HELPER_VERSION,NATIVE_PROTOCOL_VERSION

HELPER='io.reproof.live'
SAMPLE='io.reproof.sample'
REJECTED_CODES={'editable_required','stale_geometry','invalid_bounds','invalid_pointer_id','pointer_missing','pointer_exists','raw_pointer_active','invalid_duration','secure_input','text_too_long','invalid_text','unsupported_action','target_unavailable','authority_expired'}
ACTIONS=['tap','long_press','swipe','text','home','reset','pointer']


def _general_android_observation(apk, profile):
    from ..android_artifact import verify_apk
    artifact = profile.data['artifact']
    apk = Path(apk)
    try:
        verify_apk(apk,expected_digest=artifact['sha256'],expected_bytes=artifact['bytes'])
    except (ContractError,OSError):
        raise LiveError('app_changed','Android artifact differs from the selected runtime profile',400) from None
    if profile.data['capabilities']['logAdapter'] is None:
        return None
    from ..android_observation import profile_from_apk
    try:
        automatic = profile_from_apk(apk)
        accepted = (automatic is not None and automatic.data['package'] == profile.package
                    and automatic.component_name == profile.component_name)
    except (ContractError, OSError, ValueError):
        accepted = False
    check(accepted, 'unsupported_operation',
          'The selected Android app must embed its configured Views observation adapter', 400)
    return automatic


def _exact_flat_mapping(actual,expected):
    return (isinstance(actual,dict) and set(actual)==set(expected)
            and all(type(actual[key]) is type(value) and actual[key]==value
                    for key,value in expected.items()))


class UsbBridgeClient:
    def __init__(self,port,token,*,_device=None):
        if _device is not None:
            from ..repair_android import PinnedAdbDevice
            check(type(_device) is PinnedAdbDevice and _device.scoped_endpoint_enabled,
                  'native_bridge_error','Scoped Android bridge is unavailable')
        self.port=port;self.token=token;self._device=_device
    def call(self,path,body=None,timeout=5,binary=False):
        check(path in {'/status','/frame','/command','/stop','/inspect'}
              or re.fullmatch(r'/ack/(?:[a-f0-9]{32}|[a-z][a-z0-9_-]{0,63})',path)
              or re.fullmatch(r'/frames/after/[0-9]+',path),
              'invalid_path','Invalid native operation',400)
        if self._device is not None:
            return self._device.call_live_helper(path,body,token=self.token,timeout=timeout,binary=binary)
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=timeout)
        try:
            data=None if body is None else json.dumps(body,allow_nan=False).encode()
            headers={'Authorization':'Bearer '+self.token,'Connection':'close'}
            if data is not None:headers['Content-Type']='application/json'
            connection.request('GET' if data is None else 'POST',path,data,headers)
            response=connection.getresponse();raw=response.read(4*1024*1024+1)
            check(response.status in {200,202} and len(raw)<=4*1024*1024,'native_bridge_error','Android helper rejected the request')
            if binary:return raw
            result=json.loads(raw);check(isinstance(result,dict),'native_bridge_error','Invalid helper response');return result
        finally:connection.close()


def decode_native_frame(data):
    check(isinstance(data,bytes) and len(data)>=8,'invalid_frame','Incomplete native frame')
    header_size,image_size=struct.unpack('>II',data[:8])
    check(0<header_size<=16*1024 and 0<image_size<=3*1024*1024 and len(data)==8+header_size+image_size,
          'invalid_frame','Native frame exceeds bounds')
    metadata=json.loads(data[8:8+header_size]);image=data[8+header_size:]
    check(isinstance(metadata,dict) and metadata.get('type')=='frame' and type(metadata.get('id')) is int and metadata['id']>0
          and type(metadata.get('geometryVersion')) is int and metadata['geometryVersion']>0 and metadata.get('mime')=='image/jpeg',
          'invalid_frame','Native frame metadata is invalid')
    return metadata,image


class AndroidLiveProvider:
    def __init__(self,serial,helper_apk,sample_apk,*,fixture='counter',record_sdk=False,app_profile=None,
                 runtime_profile=None,_device=None):
        check(not (app_profile is not None and runtime_profile is not None),
              'invalid_argument','Select one Android profile schema',400)
        self.general_profile=runtime_profile is not None
        if self.general_profile:
            self.app_profile=(runtime_profile if isinstance(runtime_profile,AndroidRuntimeProfile)
                              else validate_android_runtime_profile(runtime_profile))
            capabilities = self.app_profile.data['capabilities']
            check('pixels' in capabilities['observations']
                  and capabilities['captureAdapter'] == {
                      'id': 'native-frame', 'version': 1},
                  'unsupported_operation',
                  'Android Live requires its declared native frame adapter', 400)
            self.explicit_profile=True
            fixture=None;record_sdk=False
        elif app_profile is None:
            check(fixture=='counter','invalid_fixture','Unsupported Android sample fixture',400)
            self.app_profile=sample_app_profile()
            self.explicit_profile=False
        else:
            self.app_profile = app_profile if isinstance(app_profile,AndroidAppProfile) else validate_app_profile(app_profile)
            self.explicit_profile=True
        profile_data=self.app_profile.data
        self.target_package=profile_data['package']
        self.target_activity=(profile_data['launchTarget']['value'] if self.general_profile
                              else profile_data['activity'])
        self.fixture_spec=None if self.general_profile else profile_data['fixture']
        self._managed_device=_device is not None
        if self._managed_device:
            from ..repair_android import PinnedAdbDevice
            check(self.general_profile and type(_device) is PinnedAdbDevice
                  and _device.serial==serial and _device.package==self.target_package,
                  'invalid_argument','Pinned device differs from the selected Android app',400)
            self.device=_device
        else:
            self.device=(AdbDevice(serial) if self.general_profile else
                         AdbDevice(serial,app_profile=self.app_profile) if self.explicit_profile
                         else AdbDevice(serial))
        self.helper_apk=Path(helper_apk).resolve()
        self.sample_apk=Path(sample_apk).absolute() if self.general_profile else Path(sample_apk).resolve()
        self.observation_profile=None;self.artifact_temp=None
        if self.general_profile:
            self.observation_profile=_general_android_observation(self.sample_apk,self.app_profile)
            self.identity=self.app_profile.application_identity
        else:
            self.identity={'bundle':self.target_package,'artifactDigest':sha_file(self.sample_apk)}
            if self.explicit_profile:self.identity['appProfileDigest']=self.app_profile.digest
        self.stop=threading.Event();self.frame_mutex=threading.RLock();self.lease=None;self.lease_held=False;self.process=None;self.port=None;self.transport=None
        self.last_native_frame=0;self.frame_map=OrderedDict();self.thread=None;self.token=secrets.token_urlsafe(32)
        self.fixture=(None if self.general_profile else
                      fixture if not self.explicit_profile else self.app_profile.data['id']);self.record_sdk=record_sdk
        # Host wall-clock boundary for Live recording freshness checks. Android
        # capture timestamps use a separate device clock and are never compared
        # with this value; the SDK UUID pointer provides provenance.
        self.capture_started_at=None;self.capture_session_id=None
        self.automatic_app_logs=(profile_data['capabilities']['logAdapter'] is not None
                                 if self.general_profile else
                                 record_sdk and profile_data.get('appLogs')==1)
        self.app_log_run_id=None;self.app_log_marker=None
        self.device_authority=None;self.provider_incarnation=None
        self.native_handshake=None;self.handshake_ready=None;self._startup_permit=None
        self.native_frame_buffer_bound=False;self.native_frame_buffered=False
        self.native_frame_buffer_version=None;self.native_frame_buffer_capacity=16
        self.native_frame_timing_declared=False
        self.installed_identity_evidence=None
        self.helper_ready=threading.Event();self.target_launched=False
    def bind_authority(self,device_authority,provider_incarnation):
        self.device_authority=device_authority;self.provider_incarnation=provider_incarnation
        self.lease=device_authority.borrowed_lease()
        self.device.authority_lease=self.lease
    def _check_permit(self,permit):
        check(self.device_authority is not None and permit is not None,
              'authority_required','A current device authority permit is required')
        self.device_authority.check_dispatch_permit(permit)
    def _install(self,apk,package,permit=None):
        if self.device_authority is not None:self._check_permit(permit)
        if self.general_profile and package==self.target_package:
            check(sha_file(apk)==self.app_profile.data['artifact']['sha256'],
                  'app_changed','Selected Android artifact changed before installation',400)
        identity=self.device.apk_identity(apk)
        check(identity['package']==package,'wrong_app','APK identity differs from the expected application',400)
        if self.general_profile and package==self.target_package:
            check(identity.get('versionCode')==self.app_profile.data['artifact']['versionCode'],
                  'app_changed','Android APK version differs from the selected profile',400)
        if self.device_authority is not None and package==HELPER:
            check(identity['versionCode']==HELPER_VERSION,'native_protocol_mismatch',
                  'Android helper artifact has an incompatible version',400)
        if self.device_authority is not None:self._check_permit(permit)
        output=self.device.adb_call('install','-r','-t',str(apk),timeout=90).decode('utf-8','replace')
        check('Success' in output,'install_failed','Android installation failed; check USB installation permission')
        entries=self.device.shell('pm','path',package).splitlines()
        paths=[line[8:].strip() for line in entries if line.startswith('package:')]
        check(len(paths)==1 and paths[0].startswith('/data/app/') and paths[0].endswith('.apk'),'app_identity','Expected a single installed APK')
        actual=self.device.shell('sha256sum',paths[0]).split()[0]
        expected=(self.app_profile.data['artifact']['sha256'] if self.general_profile and package==self.target_package
                  else sha_file(apk))
        check(actual==expected and sha_file(apk)==expected,'app_changed','Installed Android artifact digest differs')
        if self.general_profile and package==self.target_package:
            self.installed_identity_evidence={
                'package':package,'versionCode':identity['versionCode'],
                'selectedArtifactDigest':self.app_profile.data['artifact']['sha256'],
                'installedArtifactDigest':actual,'installedDigestProof':'sha256sum-single-apk',
                'splitApkSupport':'unsupported'}
    def start_authorized(self,session,lab,permit):
        self._check_permit(permit);self._startup_permit=permit
        identity=self.device.apk_identity(self.helper_apk)
        check(identity=={'package':HELPER,'versionCode':HELPER_VERSION},'startup_rejected',
              'Android helper artifact has an incompatible version',400)
        self.handshake_ready=threading.Event()
        self.start(session,lab,permit=permit)
        check(self.handshake_ready.wait(15) and self.native_handshake is not None,
              'native_protocol_mismatch','Android helper handshake did not complete')
        result={'ok':True}
        if self.general_profile:
            check(self.installed_identity_evidence is not None,'app_changed',
                  'Android installed identity evidence is unavailable')
            result['identityEvidence']=dict(self.installed_identity_evidence,
                launchedPackage=self.target_package,profileDigest=self.app_profile.digest,
                helperProtocolVersion=NATIVE_PROTOCOL_VERSION,helperVersion=HELPER_VERSION)
        return result
    def start(self,session,lab,permit=None):
        self.sid=session['id'];self.lab=lab
        from .native_frame_clock import NativeFrameClock
        self.frame_clock=NativeFrameClock.for_session(lab,self.sid,self.device_authority)
        install_apk=self.sample_apk
        if self.general_profile:
            from ..android_artifact import stage_apk
            try:
                self.artifact_temp=tempfile.TemporaryDirectory(prefix='repro-android-app-')
                artifact=self.app_profile.data['artifact']
                install_apk=stage_apk(self.sample_apk,Path(self.artifact_temp.name).resolve()/'selected.apk',
                    expected_digest=artifact['sha256'],expected_bytes=artifact['bytes'])
                self.observation_profile=_general_android_observation(install_apk,self.app_profile)
            except (ContractError,OSError,LiveError):
                if self.artifact_temp is not None:self.artifact_temp.cleanup();self.artifact_temp=None
                raise LiveError('app_changed','Selected Android artifact changed before startup',400) from None
        self.capture_started_at=int(time.time()*1000)
        if self.device_authority is None:self.lease=self.device.lease()
        self.lease.__enter__();self.lease_held=True
        self._install(self.helper_apk,HELPER,permit)
        config={'token':self.token,'port':8766,'targetPackage':self.target_package,'maxFps':15,'maxWidth':960,
            'recordSdk':self.record_sdk}
        if self.device_authority is not None:
            config.update(protocolVersion=NATIVE_PROTOCOL_VERSION,helperVersion=HELPER_VERSION,
                          helperIncarnation=permit.helper_incarnation,
                          hostIncarnation=permit.host_incarnation,
                          providerIncarnation=permit.provider_incarnation)
        if self.explicit_profile:
            config.update(appProfile=self.app_profile.native(),profileDigest=self.app_profile.digest)
        command=shlex.join(['run-as',HELPER,'sh','-c','umask 077 && mkdir -p files && cat > files/live-config.json'])
        if self.device_authority is not None:self._check_permit(permit)
        if self._managed_device:
            self.device.write_live_configuration(command,json.dumps(config).encode())
        else:
            written=subprocess.run([self.device.adb,'-s',self.device.serial,'shell','-T',command],input=json.dumps(config).encode(),
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
            check(written.returncode==0,'helper_config_failed','Could not configure the debug Android helper')
        if self.device_authority is not None:self._check_permit(permit)
        if getattr(self,'_managed_device',False) and getattr(self.device,'scoped_endpoint_enabled',False) is True:
            self.transport=UsbBridgeClient(None,self.token,_device=self.device)
        else:
            raw=self.device.adb_call('forward','tcp:0','tcp:8766').decode().strip()
            check(raw.isdigit() and 1024<=int(raw)<=65535,'forward_failed','ADB did not allocate a local port')
            self.port=int(raw);self.transport=UsbBridgeClient(self.port,self.token)
        if self.device_authority is not None:self._check_permit(permit)
        if self._managed_device:
            self.process=self.device.start_live_instrumentation(HELPER)
        else:
            self.process=subprocess.Popen([self.device.adb,'-s',self.device.serial,'shell','am instrument -w -r '+HELPER+'/.LiveInstrumentation'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        self.thread=threading.Thread(target=self._poll,daemon=True);self.thread.start()
        if self.device_authority is not None:
            check(self.handshake_ready.wait(15) and self.native_handshake is not None,
                  'native_protocol_mismatch','Android helper handshake did not complete')
        self._install(install_apk,self.target_package,permit)
        # These are sample-only fixture operations; no user app data is cleared.
        if self.device_authority is not None:self._check_permit(permit)
        self.device.shell('am','force-stop',self.target_package)
        if self.record_sdk or self.automatic_app_logs:
            # Published captures survive a force-stop. Remove only that latest
            # pointer and active-session identity so an old report cannot be
            # mistaken for this run; session directories remain available for
            # crash recovery.
            if self.device_authority is not None:self._check_permit(permit)
            cleanup=('files/repro/app-log-session.json' if self.general_profile else
                     'files/repro/capture.json files/repro/current-session')
            self.device.shell('run-as',self.target_package,'sh','-c','rm -f '+cleanup,timeout=10)
        mode='record' if self.record_sdk else 'replay'
        self.app_log_run_id=str(uuid.uuid4()) if self.automatic_app_logs else None
        log_args=['--es','repro_log_run_id',self.app_log_run_id] if self.app_log_run_id else []
        if self.device_authority is not None:self._check_permit(permit)
        if self.general_profile:
            check(self.helper_ready.wait(15),'native_protocol_mismatch',
                  'Android helper did not become ready before target launch')
            launch_payload={'applicationId':self.app_profile.data['applicationId']}
            launched=self._execute('launch',launch_payload,permit=permit)
            check(launched.get('ok') is True,'startup_unknown',
                  'Android target launch was not acknowledged')
            self.target_launched=True
        else:
            launch=['am','start','-W','-n',self._component_name()]
            launch.extend(['--es','repro_mode',mode,'--es','fixture_id',self.fixture_spec['id'],
                           '--ei','fixture_version',str(self.fixture_spec['version'])])
            launch.extend(log_args)
            self.device.shell(*launch)
            self.target_launched=True
        if self.record_sdk:self.capture_session_id=self._wait_for_sdk_session()
    def _component_name(self):
        return self.app_profile.component_name
    def _receive_frame(self):
        buffered=getattr(self,'native_frame_buffered',False)
        frame_path=f'/frames/after/{self.last_native_frame}' if buffered else '/frame'
        metadata,data=decode_native_frame(self.transport.call(frame_path,binary=True))
        if getattr(self,'native_frame_timing_declared',False):
            check(metadata.get('nativeFrameId')==metadata['id'],
                  'invalid_frame','Android native frame identity differs')
        if self.general_profile:
            from .providers import require_runtime_frame
            require_runtime_frame(self.app_profile,metadata['width'],metadata['height'],metadata['orientation'])
        with self.frame_mutex:
            if metadata['id']<=self.last_native_frame:return
            session=self.lab._session(self.sid)
            gap=(self.last_native_frame + 1, metadata['id'] - 1) if (self.last_native_frame or buffered) and metadata['id'] > self.last_native_frame + 1 else None
            clock=getattr(self,'frame_clock',None)
            timing=(clock.frame_arguments(metadata) if clock is not None else
                    {'timing_source':'native-unmapped'})
            kwargs={'acquisition_sequence':metadata['id'],**timing}
            if gap is not None:kwargs['native_sequence_gap']=gap
            published=self.lab.publish_frame(self.sid,data,metadata['mime'],metadata['width'],metadata['height'],metadata['orientation'],metadata['capturedAt'],
                                             **kwargs)
            self.last_native_frame=metadata['id']
            if not published:return
            with session['frameLock']:
                self.frame_map[session['frameSequence']]=metadata['geometryVersion']
                while len(self.frame_map)>60:self.frame_map.popitem(last=False)
    def _poll(self):
        deadline=time.monotonic()+30;ready=False
        while not self.stop.wait(.025):
            if self.process.poll() is not None:
                self.lab.fail(self.sid,'Android instrumentation stopped; close the session');return
            try:
                clock=getattr(self,'frame_clock',None)
                if not ready or (clock is not None and clock.refresh_due()):
                    sent=clock.synchronizer.sample() if clock is not None else None
                    status=self.transport.call('/status',timeout=2)
                    received=clock.synchronizer.sample() if clock is not None else None
                    self._validate_native_metadata(status)
                    if clock is not None:
                        clock.accept_status(self.native_handshake,status,sent,received)
                    ready=status.get('ready') is True
                    if ready:self.helper_ready.set()
                    if not ready:
                        check(time.monotonic()<deadline,'startup_timeout','Android helper did not start')
                        continue
                if self.general_profile and not self.target_launched:
                    continue
                self._receive_frame()
            except LiveError as exc:
                self.lab.fail(self.sid, str(exc))
                return
            except Exception:
                if ready or time.monotonic()>=deadline:
                    self.lab.fail(self.sid,'Android stream disconnected; close and reconnect the device');return
    def _validate_native_metadata(self, value):
        profile=getattr(self,'app_profile',sample_app_profile())
        explicit=getattr(self,'explicit_profile',True)
        check(isinstance(value,dict),'native_profile_mismatch','Android helper returned invalid profile metadata',400)
        expected_profile=profile.digest if explicit else None
        check(value.get('profileDigest')==expected_profile,'native_profile_mismatch',
              'Android helper profile digest differs from the requested profile',400)
        check(value.get('nativeDigest')==profile.native_digest,'native_profile_mismatch',
              'Android helper native profile digest differs from the requested profile',400)
        capabilities=value.get('capabilities',{})
        check(isinstance(capabilities,dict),'native_protocol_mismatch',
              'Android helper capabilities are unavailable',400)
        version=capabilities.get('nativeFrameBufferVersion')
        check(version is None or (type(version) is int and version==1),
              'native_protocol_mismatch','Android helper frame buffer version is unsupported',400)
        timing_version=capabilities.get('nativeFrameTimingVersion')
        check(timing_version is None or (type(timing_version) is int and timing_version==1),
              'native_protocol_mismatch','Android helper frame timing version is unsupported',400)
        capacity=capabilities.get('nativeFrameBufferCapacity',16)
        check(type(capacity) is int and 1<=capacity<=4096,
              'native_protocol_mismatch','Android helper frame buffer capacity is invalid',400)
        if getattr(self,'native_frame_buffer_bound',False):
            check(version==getattr(self,'native_frame_buffer_version',None)
                  and capacity==getattr(self,'native_frame_buffer_capacity',16),
                  'native_protocol_mismatch','Android helper frame buffer changed',409)
            check((timing_version==1)==getattr(self,'native_frame_timing_declared',False),
                  'native_protocol_mismatch','Android helper frame timing changed',409)
        else:
            self.native_frame_buffer_version=version
            self.native_frame_buffered=version==1
            self.native_frame_buffer_capacity=capacity
            self.native_frame_timing_declared=timing_version==1
            self.native_frame_buffer_bound=True
        if self.general_profile:
            check(value.get('targetPackage')==self.target_package
                  and value.get('generalProfile') is True
                  and isinstance(value.get('capabilities'),dict)
                  and type(value['capabilities'].get('actions')) is list
                  and set(value['capabilities'].get('actions',[]))
                      == set(self.app_profile.data['capabilities']['actions']),
                  'native_profile_mismatch',
                  'Android helper target capabilities differ from the profile',400)
            if self.automatic_app_logs:
                check(type(value['capabilities'].get('viewsObservationLaunchVersion')) is int
                      and value['capabilities']['viewsObservationLaunchVersion']==2,
                      'native_protocol_mismatch','Android helper does not support configured Views observations',400)
        if self.device_authority is not None and self.native_handshake is None:
            permit=self._startup_permit
            try:
                self.native_handshake=self.device_authority.bind_native_handshake(
                    permit,protocol_version=value.get('protocolVersion'),
                    helper_version=value.get('helperVersion'),
                    helper_incarnation=value.get('helperIncarnation'),
                    provider_incarnation=value.get('providerIncarnation'),
                    native_incarnation=value.get('nativeIncarnation'),
                    native_clock_id=value.get('nativeClockId'),
                    native_time_ms=value.get('nativeTimeMs'),
                )
            except ContractError:
                raise LiveError('native_protocol_mismatch','Android helper protocol is incompatible',400) from None
            if self.handshake_ready is not None:self.handshake_ready.set()
        return value
    def observe(self):
        return self._validate_native_metadata(self.transport.call('/inspect'))
    def resolve_locator(self, target):
        """Resolve one registered resource ID against a fresh native snapshot."""
        check(self.general_profile and isinstance(target, dict)
              and set(target) == {'kind', 'value'}
              and target.get('kind') == 'resource-id',
              'unsupported_operation',
              'Android locator kind is not supported', 400)
        locator = self.app_profile.data['capabilities']['locator']
        check(locator is not None and target.get('value') in locator['targets'],
              'unsupported_operation',
              'Android locator is not registered', 400)
        if self.device_authority is not None:
            try:
                self.device_authority.check_ownership()
            except Exception:
                raise LiveError('authority_unavailable',
                                'Android locator authority is unavailable') from None
        observed = self._validate_native_metadata(self.transport.call('/inspect'))
        nodes = observed.get('nodes')
        screen = observed.get('screenBounds')
        matches = [node for node in nodes if isinstance(node, dict)
                   and node.get('id') == target['value']] if isinstance(nodes, list) else []
        check(len(matches) == 1 and isinstance(screen, dict)
              and set(screen) == {'left', 'top', 'right', 'bottom'},
              'observation_failed', 'Android locator is not uniquely visible', 409)
        node = matches[0]
        bounds = node.get('bounds')
        numbers = [screen.get(key) for key in ('left', 'top', 'right', 'bottom')]
        values = ([bounds.get(key) for key in ('left', 'top', 'right', 'bottom')]
                  if isinstance(bounds, dict) else [])
        check(set(bounds or {}) == {'left', 'top', 'right', 'bottom'}
              and all(type(value) is int for value in numbers + values)
              and screen['right'] > screen['left']
              and screen['bottom'] > screen['top']
              and bounds['right'] > bounds['left']
              and bounds['bottom'] > bounds['top']
              and node.get('visible') is True and node.get('enabled') is True,
              'observation_failed', 'Android locator bounds are invalid', 409)
        frame = self.lab.frame(self.sid)
        x = ((bounds['left'] + bounds['right']) / 2 - screen['left']) \
            / (screen['right'] - screen['left'])
        y = ((bounds['top'] + bounds['bottom']) / 2 - screen['top']) \
            / (screen['bottom'] - screen['top'])
        check(0 <= x <= 1 and 0 <= y <= 1,
              'observation_failed', 'Android locator is outside the current frame', 409)
        return {'target': dict(target), 'x': x, 'y': y,
                'frameId': frame['id'],
                'geometryVersion': frame['geometryVersion'],
                'providerIncarnation': self.provider_incarnation,
                'observedAtMs': int(time.time() * 1000)}
    def execute(self,action,payload):return self._execute(action,payload)
    def execute_authorized(self,action,payload,permit,frame=None):
        self._check_permit(permit)
        if frame is not None:return self.execute_with_frame(action,payload,frame,permit=permit)
        return self._execute(action,payload,permit=permit)
    def execute_with_frame(self,action,payload,frame,permit=None):
        if action=='pointer' and payload.get('phase')=='cancel':return self._execute(action,payload,permit=permit)
        with self.frame_mutex:geometry=self.frame_map.get(frame['frameId'])
        check(geometry is not None,'stale_frame','The displayed Android frame is no longer available')
        return self._execute(action,payload,geometry,permit=permit)
    def _execute(self,action,payload,geometry=None,permit=None):
        if self.general_profile:
            check(action in self.app_profile.data['capabilities']['actions'],
                  'unsupported_operation','General Android profile does not allow this action',400)
        if action=='reset' and self.record_sdk:
            self.capture_started_at=int(time.time()*1000)
        if action=='reset' and self.automatic_app_logs:
            self.app_log_run_id=str(uuid.uuid4());self.app_log_marker=None
            payload=dict(payload,appLogRunId=self.app_log_run_id)
        if action=='launch' and self.general_profile and self.automatic_app_logs:
            check(self.observation_profile is not None,'unsupported_operation','Missing Views observation adapter',400)
            check(type(payload) is dict and set(payload)=={'applicationId'}
                  and payload['applicationId']==self.app_profile.data['applicationId'],
                  'invalid_argument','Launch requires the selected application identity',400)
            self.app_log_run_id=str(uuid.uuid4());self.app_log_marker=None
            payload=dict(payload,appLogRunId=self.app_log_run_id,
                         appLogProfileDigest=self.observation_profile.digest)
        command_id=permit.operation_id if permit is not None else uuid.uuid4().hex
        command={'id':command_id,'action':action,'payload':payload}
        if geometry is not None:command['geometryVersion']=geometry
        if permit is not None:
            self._check_permit(permit)
            check(self.native_handshake is not None,'native_protocol_mismatch',
                  'Android helper handshake is unavailable')
            command['authority']=self.device_authority.native_grant(
                permit,self.native_handshake
            ).wire()
        accepted=self.transport.call('/command',command)
        check(accepted.get('accepted') is True,'injection_unknown','Android helper did not accept the input')
        deadline=time.monotonic()+20
        while not self.stop.wait(.01):
            result=self.transport.call('/ack/'+command_id)
            if result.get('pending') is True:
                check(set(result)=={'pending'},'injection_unknown','Android pending acknowledgement is invalid')
                check(time.monotonic()<deadline,'native_timeout','Android input acknowledgement timed out');continue
            keys=set(result)
            legacy_valid=(permit is None and {'id','ok'}<=keys
                          and keys<={'pending','id','ok','timing','error'}
                          and ('pending' not in result or result['pending'] is False)
                          and ('timing' not in result or result['timing']=='best-effort'))
            expected={'pending','id','ok','timing','authority'}|({'error'} if 'error' in result else set())
            shared_valid=(permit is not None and keys==expected and result.get('pending') is False
                          and result.get('timing')=='best-effort'
                          and _exact_flat_mapping(result.get('authority'),command['authority']))
            check((legacy_valid or shared_valid) and result.get('id')==command_id
                  and type(result.get('ok')) is bool,
                  'injection_unknown','Android acknowledgement identity differs')
            self.last_ack={key:result.get(key) for key in ('id','ok','error','timing')}
            if action=='reset' and result.get('ok') is True:
                self._receive_frame()
                if self.record_sdk:self.capture_session_id=self._wait_for_sdk_session()
            if action=='launch' and self.general_profile and self.automatic_app_logs and result.get('ok') is True:
                self._wait_for_app_log_marker(permit=permit)
            if result.get('ok') is False and result.get('error') in REJECTED_CODES:
                return {'ok':False,'outcome':'rejected','code':result['error']}
            return {'ok':result.get('ok') is True,'timing':'best-effort'}
        raise LiveError('session_inactive','Android session is closing')
    def _read_sdk_json(self,path,*,maximum=20*1024*1024):
        raw=self.device.adb_call('exec-out','run-as',self.target_package,'cat',path,timeout=8)
        check(len(raw)<=maximum,'capture_invalid','Android capture exceeds the size limit')
        try:value=json.loads(raw)
        except (TypeError,UnicodeDecodeError,json.JSONDecodeError) as exc:
            raise LiveError('capture_invalid','Android SDK capture is not valid JSON',400) from exc
        check(isinstance(value,dict),'capture_invalid','Android SDK capture must be an object',400)
        return value
    def _read_sdk_text(self,path):
        raw=self.device.adb_call('exec-out','run-as',self.target_package,'cat',path,timeout=8)
        check(len(raw)<=512,'capture_invalid','Android SDK session marker exceeds the size limit')
        try:return raw.decode('ascii')
        except UnicodeDecodeError as exc:raise LiveError('capture_invalid','Android SDK session marker is invalid',400) from exc
    def _wait_for_sdk_session(self):
        deadline=time.monotonic()+8
        last_error=None
        while time.monotonic()<deadline:
            try:
                value=self._read_sdk_text('files/repro/current-session').strip()
                check(re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value) is not None,
                      'capture_invalid','Android SDK session identity is invalid',400)
                return value
            except LiveError:
                raise
            except Exception as exc:
                last_error=exc;time.sleep(.2)
        raise LiveError('capture_invalid','Android SDK recorder session did not become available',400) from last_error
    def _validate_sdk_capture(self,capture,metadata):
        profile=getattr(self,'app_profile',sample_app_profile())
        # compile_capture applies the shared event, fixture and profile policy.
        oracle=profile.oracle()
        try:compile_capture(capture,oracle,app_profile=profile)
        except TypeError as exc:
            # Keep this adapter usable while older host-only tests import the
            # provider before the profile-aware core contract is installed.
            if 'app_profile' not in str(exc):
                raise LiveError('capture_invalid','Android SDK capture failed the recording contract',400) from exc
            try:compile_capture(capture,oracle)
            except Exception as nested:raise LiveError('capture_invalid','Android SDK capture failed the recording contract',400) from nested
        except Exception as exc:raise LiveError('capture_invalid','Android SDK capture failed the recording contract',400) from exc
        session_id=capture.get('sessionId')
        check(re.fullmatch(r'[A-Za-z0-9_-]{1,128}',session_id or '') is not None,
              'capture_invalid','Android SDK capture session identity is invalid',400)
        check(metadata.get('schemaVersion')==1 and metadata.get('sessionId')==session_id,
              'capture_invalid','Android capture session metadata does not match the capture',400)
        check(set(metadata)=={'schemaVersion','sessionId','fixture','startState','finalized','incomplete','lostEvents','unsupported'},
              'capture_invalid','Android capture metadata contains unsupported fields',400)
        fixture=profile.data['fixture']
        check(metadata.get('fixture')=={'id':fixture['id'],'version':fixture['version']},
              'capture_invalid','Android capture fixture metadata is unsupported',400)
        check(metadata.get('startState')==profile.data['startState'] and capture.get('startState')==profile.data['startState'],
              'capture_invalid','Android capture starting state differs from session metadata',400)
        check(metadata.get('finalized') is True and metadata.get('incomplete') is False and
              metadata.get('lostEvents') is False and metadata.get('unsupported') is False,
              'capture_invalid','Android SDK session was not finalized cleanly',400)
        check(type(capture.get('startedAtMs')) is int and capture['startedAtMs']>=0,
              'capture_invalid','Android SDK capture start timestamp is invalid',400)
        return capture
    def collect_sdk_capture_authorized(self,permit):
        self._check_permit(permit)
        return self.collect_sdk_capture(permit=permit)
    def collect_sdk_capture(self,permit=None):
        check(not self.general_profile,'unsupported_operation',
              'General Android profiles do not imply the legacy SDK capture contract',400)
        check(self.record_sdk,'unsupported_operation','SDK capture is not configured',400)
        check(self.capture_started_at is not None,'session_inactive','Android session has not started')
        check(self.capture_session_id is not None,'session_inactive','Android capture session is not pinned')
        if getattr(self, 'transport', None) is not None:
            self._validate_native_metadata(self.transport.call('/inspect'))
        if getattr(self, 'explicit_profile', False) and self.app_profile.data.get('captureMode') == 'debug_receiver':
            check(self.observe().get('ready') is True, 'capture_invalid', 'Return to the configured app before exporting')
            try:
                if permit is not None:self._check_permit(permit)
                self.device.request_sdk_export()
            except Exception:raise LiveError('capture_invalid', 'Debug SDK export was not accepted', 400) from None
            result = {'ok': True}
        else:
            result=self._execute('report_capture',{},permit=permit)
        check(result.get('ok') is True,'capture_invalid','Return to the sample counter screen and finish a valid QA/Test recording',400)
        deadline=time.monotonic()+8
        last_error=None
        while time.monotonic()<deadline:
            try:
                if permit is not None:self._check_permit(permit)
                current_session=self._read_sdk_text('files/repro/current-session').strip()
                check(current_session==self.capture_session_id,'capture_invalid',
                      'Android SDK recorder session changed during capture',400)
                capture=self._read_sdk_json('files/repro/capture.json')
                session_id=capture.get('sessionId')
                if not isinstance(session_id,str) or re.fullmatch(r'[A-Za-z0-9_-]{1,128}',session_id) is None:
                    raise LiveError('capture_invalid','Android SDK capture session identity is invalid',400)
                check(session_id==self.capture_session_id,'capture_invalid',
                      'Android SDK capture belongs to a different recorder session',400)
                metadata=self._read_sdk_json('files/repro/'+session_id+'/session.json')
                if metadata.get('finalized') is not True:
                    # The SDK publishes capture.json before finalized metadata;
                    # wait for that bounded publication race to settle.
                    last_error=LiveError('capture_pending','Android SDK capture is still being finalized',409)
                    time.sleep(.2);continue
                return self._validate_sdk_capture(capture,metadata)
            except LiveError:
                raise
            except Exception as exc:
                # Report publication is asynchronous and both files are written
                # atomically. A missing file is expected briefly after the click.
                last_error=exc;time.sleep(.2)
        raise LiveError('capture_invalid','Android SDK capture did not become available',400) from last_error
    def collect_sdk_diagnostics_authorized(self,capture,permit):
        self._check_permit(permit)
        return self.collect_sdk_diagnostics(capture)
    def collect_sdk_diagnostics(self, capture):
        try:
            check(capture['sessionId'] == self.capture_session_id, 'capture_invalid', 'Capture session changed')
            return self.device.collect_instrumentation_diagnostics(capture)
        except Exception:
            raise LiveError('capture_invalid', 'SDK diagnostics do not match the captured session', 400) from None
    def collect_app_logs_authorized(self,permit):
        self._check_permit(permit)
        result=self.collect_app_logs(permit=permit)
        self._check_permit(permit)
        return result
    def collect_app_logs(self,*,permit=None):
        from ..app_logs import IDENTITY_KEYS
        check(self.automatic_app_logs and self.app_log_run_id is not None,
              'unsupported_operation','Automatic app logs are not configured',400)
        try:
            if self.general_profile:
                from ..app_logs import collect_app_logs
                from ..app_logs import MAX_APP_LOG_BYTES
                check(self.observation_profile is not None,'unsupported_operation','Missing Views observation adapter',400)
                profile=self.observation_profile.data
                def read(relative):
                    if permit is not None:self._check_permit(permit)
                    value=self._read_sdk_json('files/repro/'+relative,maximum=MAX_APP_LOG_BYTES)
                    if permit is not None:self._check_permit(permit)
                    return value
                value=collect_app_logs(
                    read,
                    platform='android',application_id=self.target_package,
                    profile_digest=self.observation_profile.digest,run_id=self.app_log_run_id,
                    click_targets=set(profile['tapTargets']),screen_targets=set(profile['screenTargets'].values()),
                    expected_marker=self.app_log_marker)
            else:
                value=self.device.collect_app_logs(self.app_log_run_id,expected_marker=self.app_log_marker)
        except (ContractError,DeviceError,OSError,ValueError):
            raise LiveError('app_log_unavailable','A matching app log snapshot is not available yet',409) from None
        self.app_log_marker={key:value[key] for key in IDENTITY_KEYS}
        return value
    def _wait_for_app_log_marker(self,timeout=10,*,permit=None):
        deadline=time.monotonic()+min(timeout,10)
        while time.monotonic()<deadline and not self.stop.is_set():
            if permit is not None:self._check_permit(permit)
            try:return self.collect_app_logs(permit=permit)
            except LiveError as error:
                if error.code not in {'app_log_unavailable','capture_invalid'}:raise
                self.stop.wait(.1)
        raise LiveError('app_log_unavailable','Automatic Android app logging did not become ready',409)
    def close_authorized(self,permit):
        self._check_permit(permit)
        return self._close(permit)
    def close(self):return self._close(None)
    def _close(self,permit):
        if getattr(self,'_managed_device',False):
            with self.device.cleanup_bounds():
                return self._close_native(permit)
        return self._close_native(permit)
    def _close_native(self,permit):
        self.stop.set();confirmed=self.process is None;ports=[]
        if self.port is not None:ports.append(self.port)
        if self.transport is not None and self.process is not None and self.process.poll() is None:
            try:
                body={}
                if permit is not None:
                    self._check_permit(permit)
                    body['authority']=self.device_authority.native_grant(
                        permit,self.native_handshake
                    ).wire()
                stopped=self.transport.call('/stop',body)
                confirmed=(set(stopped)==({'stopped','authority'} if permit is not None else {'stopped'})
                           and stopped.get('stopped') is True
                           and (permit is None or _exact_flat_mapping(stopped.get('authority'),body['authority'])))
            except Exception:
                confirmed=False
                # A lost forwarding socket must not prevent cleanup when USB is
                # still connected. Reconnect only to this authenticated helper.
                recovery=None
                try:
                    if getattr(self,'_managed_device',False) and getattr(self.device,'scoped_endpoint_enabled',False) is True:
                        recovery=UsbBridgeClient(None,self.token,_device=self.device)
                    else:
                        value=self.device.adb_call('forward','tcp:0','tcp:8766',timeout=5).decode().strip()
                        check(value.isdigit(),'forward_failed','Could not reconnect cleanup transport')
                        ports.append(int(value));recovery=UsbBridgeClient(int(value),self.token)
                    if permit is not None:self._check_permit(permit)
                    stopped=recovery.call('/stop',body,timeout=3)
                    confirmed=(set(stopped)==({'stopped','authority'} if permit is not None else {'stopped'})
                               and stopped.get('stopped') is True
                               and (permit is None or _exact_flat_mapping(stopped.get('authority'),body['authority'])))
                    self.cleanup_reconnected=confirmed
                except Exception:confirmed=False
                finally:
                    if recovery is not None:recovery.token=''
        if self.process is not None:
            if getattr(self,'_managed_device',False):
                process_clean=self.device.collect_live_instrumentation(self.process)
                confirmed=confirmed and process_clean
            else:
                from ..repair_android_signing import _ProcessOwner
                try:self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    _ProcessOwner._terminate(self.process)
                    confirmed=False
                confirmed=(confirmed and self.process.returncode==0
                           and _ProcessOwner._group_empty(self.process.pid))
        if self.thread is not None:
            self.thread.join(timeout=6)
            confirmed=confirmed and not self.thread.is_alive()
        if getattr(self,'frame_clock',None) is not None:self.frame_clock.close()
        for port in set(ports):
            try:self.device.adb_call('forward','--remove','tcp:'+str(port),timeout=5)
            except Exception:
                try:
                    mappings=self.device.adb_call('forward','--list',timeout=5).decode().splitlines()
                    if any(line.split()[:2]==[self.device.serial,'tcp:'+str(port)] for line in mappings):confirmed=False
                except Exception:confirmed=False
        self.token=''
        if self.transport:self.transport.token=''
        if getattr(self,'automatic_app_logs',False) and getattr(self,'lease_held',False):
            try:
                # Keep the target inside the existing device lease until its
                # automatic observation process has been stopped.  A shell
                # exception is cleanup uncertainty and deliberately retains
                # the lease for quarantine handling.
                if permit is not None:self._check_permit(permit)
                self.device.shell('am','force-stop',self.target_package,timeout=10)
            except Exception:
                confirmed=False
        check(confirmed,'cleanup_uncertain','Android helper did not confirm cleanup; device remains quarantined')
        if getattr(self,'lease_held',False) and self.lease:
            self.lease.__exit__(None,None,None);self.lease=None;self.lease_held=False
        if getattr(self,'artifact_temp',None) is not None:
            self.artifact_temp.cleanup();self.artifact_temp=None
        return {'ok':True}


def android_live_device(serial,helper_apk,sample_apk,*,fixture='counter',record_sdk=False,app_profile=None,
                        runtime_profile=None,
                        authority_mode='shared-v2'):
    check(authority_mode in {'shared-v2','legacy-offline-v1'},'invalid_argument','Invalid authority compatibility mode',400)
    check(not (app_profile is not None and runtime_profile is not None),
          'invalid_argument','Select one Android profile schema',400)
    if app_profile is None and runtime_profile is None:
        check(fixture=='counter','invalid_fixture','Unsupported Android sample fixture',400)
    device=AdbDevice(serial)
    check(Path(helper_apk).is_file() and Path(sample_apk).is_file(),'missing_build','Build the Android Live helper and sample first',400)
    general=runtime_profile is not None
    profile=(runtime_profile if isinstance(runtime_profile,AndroidRuntimeProfile)
             else validate_android_runtime_profile(runtime_profile)) if general else (
             sample_app_profile() if app_profile is None else
             app_profile if isinstance(app_profile,AndroidAppProfile) else validate_app_profile(app_profile))
    explicit = app_profile is not None or general
    package=profile.data['package']
    if general:_general_android_observation(sample_apk,profile)
    identity=(profile.application_identity if general else
              {'bundle':package,'artifactDigest':sha_file(sample_apk)})
    if explicit and not general:identity['appProfileDigest']=profile.digest
    reset_contract=(None if general else 'sample-counter-fixture-v1' if not explicit
                    else f'app-profile-{profile.digest}-fixture-{profile.data["id"]}-v1')
    actions=list(profile.data['capabilities']['actions']) if general else ACTIONS
    capabilities={'actions':actions,'inputMode':'continuous-pointer','maxPointers':5,'multitouch':True,'media':'jpeg-stream',
            'resetContract':reset_contract,'timing':'best-effort','applicationIdentity':identity,
            'pointerTimeoutSeconds':10,'verification':'device-validation-pending','fixture':fixture,'sdkCapture':record_sdk}
    capabilities['automaticAppLogs']=(profile.data['capabilities']['logAdapter'] is not None
                                      if general else record_sdk and profile.data.get('appLogs')==1)
    if explicit:capabilities['fixture']=None if general else profile.data['id']
    if general:
        locator=profile.data['capabilities']['locator']
        capabilities.update(applicationProfile=profile.data,
                            applicationProfileDigest=profile.digest,
                            sdkCapture=False,
                            identityEvidence='selected-and-installed-single-apk-sha256',
                            locatorKinds=[] if locator is None else ['resource-id'])
    descriptor={'id':'android-'+device.identity,'name':'Android · Live','platform':'android','kind':'android-live',
        'capabilities':capabilities,
        'factory':lambda:AndroidLiveProvider(serial,helper_apk,sample_apk,fixture=fixture,record_sdk=record_sdk,
            app_profile=profile if explicit and not general else None,
            runtime_profile=profile if general else None)}
    descriptor['capabilities']['authorityMode']=authority_mode
    if authority_mode=='shared-v2':descriptor['_authority']={'deviceKind':'android','physicalId':serial}
    return descriptor
