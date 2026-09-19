"""Physical iPhone discovery and a bridge bound to the paired USB tunnel.

No personal device name, UDID, tunnel address or signing credential is exposed
in public registry records. Signing is an explicit preparation step.
"""
from __future__ import annotations
import base64
from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import plistlib
import re
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from .model import LiveError,check
from .authority import HELPER_VERSION,NATIVE_PROTOCOL_VERSION
from .providers import (CAPABILITIES, IosProvider, require_ios_helper_v2,
                        _exact_flat_mapping, _require_ios_runtime_capabilities,
                        require_runtime_frame)
from ..core import ContractError, digest
from ..ios_cases import case_spec
from ..ios_instrumentation import profile_from_app
from ..ios_runner import (_require_profile_fixture,
                          prepare_xctestrun, _targets, validate_ios_auto_diagnostics,
                          validate_ios_auto_marker)
from ..ios_storage import app_info, tree_manifest
from ..ios_profile import IosAppProfile, validate_ios_profile
from ..storage import Lease,read_json


@dataclass(frozen=True)
class Iphone:
    identifier:str
    udid:str
    public_id:str
    model:str
    os_version:str
    paired:bool
    developer_mode:bool
    ddi_ready:bool
    wired:bool
    connected:bool
    tunnel_address:str|None


def validate_tunnel_address(address):
    try:value=ipaddress.ip_address(address)
    except (ValueError,TypeError):raise LiveError('invalid_tunnel','A private IPv6 USB tunnel address is required',400) from None
    check(value.version==6 and value.is_private and not value.is_loopback and not value.is_unspecified and not value.is_multicast,
          'invalid_tunnel','A private IPv6 USB tunnel address is required',400)
    return str(value)


def device_from_details(value):
    check(isinstance(value,dict),'invalid_device','Invalid device details',400)
    h=value.get('hardwareProperties',{});p=value.get('deviceProperties',{});c=value.get('connectionProperties',{})
    check(h.get('deviceType')=='iPhone' and h.get('platform')=='iOS','unsupported_device','Select a physical iPhone',400)
    identifier=value.get('identifier');udid=h.get('udid')
    check(isinstance(identifier,str) and isinstance(udid,str) and bool(udid),'invalid_device','Missing device identity',400)
    address=c.get('tunnelIPAddress')
    try:address=validate_tunnel_address(address)
    except LiveError:address=None
    return Iphone(identifier,udid,'iphone-'+hashlib.sha256(udid.encode()).hexdigest()[:16],
        h.get('marketingName') or 'iPhone',p.get('osVersionNumber') or 'unknown',c.get('pairingState')=='paired',
        p.get('developerModeStatus')=='enabled',p.get('ddiServicesAvailable') is True,
        c.get('transportType')=='wired',c.get('tunnelState')=='connected',address)


def public_device_status(device):
    ready=device.paired and device.developer_mode and device.ddi_ready and device.wired and device.connected and device.tunnel_address is not None
    return {'id':device.public_id,'platform':'ios-physical','model':device.model,'osVersion':device.os_version,'ready':ready,
        'paired':device.paired,'developerMode':device.developer_mode,'developerServices':device.ddi_ready,
        'wired':device.wired,'tunnelConnected':device.connected,'requiresSignedTestProducts':True}


def _devicectl(*arguments,timeout=30):
    with tempfile.TemporaryDirectory(prefix='repro-device-probe-') as directory:
        output=Path(directory)/'result.json'
        process=subprocess.run(['/usr/bin/xcrun','devicectl',*map(str,arguments),'--json-output',str(output)],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=timeout)
        check(process.returncode==0 and output.is_file(),'device_unavailable','Device command failed; unlock and reconnect the iPhone')
        value=read_json(output)
        check(value.get('info',{}).get('outcome')=='success','device_unavailable','Device did not confirm the operation')
        return value.get('result',{})


def discover_iphones(refresh=True):
    listing=_devicectl('list','devices');result=[]
    for item in listing.get('devices',[]):
        if item.get('hardwareProperties',{}).get('deviceType')!='iPhone' or item.get('hardwareProperties',{}).get('reality')=='simulated':continue
        if refresh and item.get('connectionProperties',{}).get('transportType')=='wired':
            try:
                details=_devicectl('device','info','details','--device',item['identifier'])
                # Details does not always repeat the CoreDevice identifier.
                item=dict(details,identifier=item['identifier'])
            except LiveError:pass
        result.append(device_from_details(item))
    return result


def select_iphone(public_id=None, *, query_client=None, cancellation=None, deadline_monotonic=None):
    if query_client is None:
        check(cancellation is None and deadline_monotonic is None,
              'device_selection','Bounded selection requires an explicit device query client',400)
        candidates=discover_iphones()
    else:
        from ..ios_device_tools import IOSDeviceToolError, PinnedDeviceCtlClient
        check(type(query_client) is PinnedDeviceCtlClient,
              'device_selection','Invalid selected device query client',400)
        cancel=threading.Event() if cancellation is None else cancellation
        deadline=time.monotonic()+30 if deadline_monotonic is None else deadline_monotonic
        try:
            observed=query_client.query('details',cancellation=cancel,deadline_monotonic=deadline)
            candidates=[device_from_details(observed.data)]
        except IOSDeviceToolError:
            raise LiveError('device_unavailable','Selected device query could not be confirmed',409) from None
    if public_id:candidates=[device for device in candidates if device.public_id==public_id]
    else:candidates=[device for device in candidates if public_device_status(device)['ready']]
    check(len(candidates)==1,'device_selection','Select exactly one ready iPhone using live-device-doctor',400)
    device=candidates[0];check(public_device_status(device)['ready'],'device_unavailable','Unlock, trust, and connect the iPhone with developer mode enabled')
    return device


def _tree_bytes(root):
    manifest=tree_manifest(root)
    return manifest,sum((Path(root)/name).stat().st_size for name in manifest)


def validate_signed_products(products,app,profile=None,ipa=None):
    products=Path(products).resolve();app=Path(app).resolve()
    if profile is not None and not isinstance(profile,IosAppProfile):
        profile=validate_ios_profile(profile)
    runners=list(products.glob('Debug-iphoneos/*Tests-Runner.app'));hosts=list(products.glob('Debug-iphoneos/ReproLiveHost.app'))
    check(len(runners)==1 and len(hosts)==1 and app.is_dir(),'missing_device_build','Build the iPhone app and test products first',400)
    for target in [*runners,*hosts,app]:
        check((target/'embedded.mobileprovision').is_file(),'signing_required','Physical iPhone requires signed app and test products',400)
        verified=subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(target)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        check(verified.returncode==0,'signing_required','iPhone product signature is invalid',400)
    with (app/'Info.plist').open('rb') as stream:info=plistlib.load(stream)
    if profile is None:
        check(info.get('CFBundleIdentifier')=='io.reproloop.sample.ios','unsupported_app','The physical fixture currently supports the Repro Loop sample only',400)
        return {'bundle':info['CFBundleIdentifier'],'artifactDigest':digest(tree_manifest(app))}
    manifest,size=_tree_bytes(app);data=profile.data;artifact=data['artifact']
    check(info.get('CFBundleIdentifier')==data['bundle']
          and info.get('CFBundleShortVersionString')==artifact['bundleVersion']
          and info.get('CFBundleVersion')==artifact['bundleBuild'],
          'app_changed','iOS bundle identity differs from the selected profile',400)
    actual=digest(manifest)
    if artifact['kind']=='ios-ipa':
        # ios-ipa는 살균된 승인 증거(IPA 컨테이너 해시)를 선언한다. 설치 아티팩트는
        # 완전 서명된 .app이므로 선언 페이로드가 설치 트리에 파일 단위로 포함돼야
        # 한다. 초과 파일은 앱 자체 코드 서명이 함께 봉인한다.
        from ..ios_mobile_inputs import IOSBaselineReference,ProtectedMobileInputsError
        check(ipa is not None,'missing_artifact','ios-ipa profiles require the declared IPA artifact for staging validation',400)
        try:
            _body,identity=IOSBaselineReference('original',data['bundle'],Path(ipa).resolve(),artifact['sha256'],artifact['bytes']).read()
        except ProtectedMobileInputsError:
            check(False,'app_changed','Declared iOS IPA artifact is unreadable or changed',400)
        check(bool(identity['members'])
              and all(manifest.get(relative)==checksum
                      for relative,checksum in identity['members'].items()),
              'app_changed','Selected iOS application does not contain the declared IPA payload',400)
    else:
        check(actual==artifact['sha256'] and size==artifact['bytes'],
              'app_changed','iOS application artifact differs from the selected profile',400)
    return dict(profile.application_identity,signatureVerified=True)


class TunnelClient:
    def __init__(self,address,port,token):
        self.address=validate_tunnel_address(address);self.port=port;self.token=token
        check(type(port) is int and 1024<=port<=65535,'invalid_port','Invalid device bridge port',400)
    def call(self,path,body=None,timeout=5,binary=False):
        check(path in {'/status','/frame','/command','/stop','/activate','/retire'}
              or re.fullmatch(r'/ack/(?:[a-f0-9]{32}|[a-z][a-z0-9_-]{0,63})',path)
              or re.fullmatch(r'/frames/after/[0-9]+',path),
              'invalid_path','Invalid device operation',400)
        connection=http.client.HTTPConnection(self.address,self.port,timeout=timeout)
        try:
            data=None if body is None else json.dumps(body,allow_nan=False).encode()
            headers={'Authorization':'Bearer '+self.token,'Connection':'close'}
            if data is not None:headers['Content-Type']='application/json'
            connection.request('GET' if data is None else 'POST',path,body=data,headers=headers)
            response=connection.getresponse();raw=response.read(5*1024*1024+1)
            check(len(raw)<=5*1024*1024,'device_bridge_error','Invalid device bridge response')
            if response.status not in {200,202}:
                try:value=json.loads(raw)
                except (UnicodeDecodeError,json.JSONDecodeError):value={}
                code=value.get('error')
                if response.status==409 and code=='authority_rejected':
                    raise LiveError('authority_rejected','iPhone helper rejected expired authority',409)
                if response.status==404 and code=='frame_unavailable':
                    raise LiveError('frame_unavailable','Device bridge has no captured frame yet',404)
                raise LiveError('device_bridge_error','Device bridge rejected the operation',409)
            if binary:return raw
            value=json.loads(raw);check(isinstance(value,dict),'device_bridge_error','Invalid device bridge response')
            return value
        finally:connection.close()


class PhysicalIosProvider(IosProvider):
    ipa=None
    def __init__(self,device,products,app,identity,port=8766,*,fixture='counter',record_sdk=False,app_logs_only=False,
                 profile=None,ipa=None):
        if profile is not None and not isinstance(profile,IosAppProfile):profile=validate_ios_profile(profile)
        if profile is not None:
            _require_ios_runtime_capabilities(profile, app)
            check(identity.get('bundle')==profile.bundle
                  and identity.get('artifactDigest')==profile.data['artifact']['sha256']
                  and identity.get('applicationProfileDigest')==profile.digest,
                  'recording_identity','iOS profile and selected artifact identity differ',400)
            fixture=None;record_sdk=False
            app_logs_only=profile.data['capabilities']['logAdapter'] is not None
        super().__init__(device.udid,products,identity['bundle'],identity,app=app,fixture=fixture,record_sdk=record_sdk,app_logs_only=app_logs_only)
        self.profile=profile;self.ipa=ipa
        self.device=device;self.app=Path(app).resolve();self.port=port;self.last_native_frame=0
        self.transport=TunnelClient(device.tunnel_address,port,self.token);self.frame_mutex=threading.Lock()
        check(profile is not None or fixture in {'counter','duplicate-submit','reset'},'invalid_fixture','Unsupported sample fixture',400)
        self.fixture=fixture;self.record_sdk=record_sdk;self.capture_started_at=None
        self.installed_build_id=None;self.auto_profile=None;self.auto_run_id=None;self.auto_marker=None
        self.installed_identity_evidence=None
        self.launched_identity_evidence=None
        self.native_frame_buffer_bound=False;self.native_frame_buffered=False
        self.native_frame_buffer_version=None;self.native_frame_buffer_capacity=16
    def start_authorized(self,session,lab,permit):
        self._check_permit(permit);self._startup_permit=permit;self.handshake_ready=threading.Event()
        self.startup_complete=threading.Event()
        self.start(session,lab,permit=permit)
        check(self.handshake_ready.wait(20) and self.native_handshake is not None,
              'native_protocol_mismatch','iPhone helper handshake did not complete')
        self._check_permit(permit)
        if self.profile is not None:
            validate_signed_products(self.products,self.app,profile=self.profile,ipa=self.ipa)
            self._check_permit(permit)
        _devicectl('device','install','app','--device',self.device.identifier,str(self.app),timeout=90)
        if self.profile is None:
            try:self.installed_build_id=app_info(self.app)['buildId']
            except ContractError:self.installed_build_id=None
        else:
            self._check_permit(permit)
            queried=_devicectl('device','info','apps','--device',self.device.identifier,
                               '--bundle-id',self.profile.bundle,timeout=15)
            self._check_permit(permit)
            apps=queried.get('apps') if isinstance(queried,dict) else None
            check(type(apps) is list and len(apps)==1 and type(apps[0]) is dict,
                  'app_changed','Installed iOS application identity is unavailable',409)
            installed=apps[0]
            artifact=self.profile.data['artifact']
            check(installed.get('bundleIdentifier')==self.profile.bundle
                  and installed.get('version')==artifact['bundleVersion']
                  and installed.get('bundleVersion')==artifact['bundleBuild'],
                  'app_changed','Installed iOS application identity differs from the selected profile',409)
            self.installed_identity_evidence={
                'bundle':installed['bundleIdentifier'],'bundleVersion':installed['version'],
                'bundleBuild':installed['bundleVersion'],'installation':'devicectl-success',
                'installedIdentityProof':'devicectl-device-info-apps',
                'installedArtifactDigest':None,'installedDigestProof':'unavailable'}
        refreshed=select_iphone(self.device.public_id)
        check(refreshed.udid==self.udid and public_device_status(refreshed)['ready'],
              'device_unavailable','The selected iPhone connection is not ready after installation')
        self.device=refreshed;self.transport=TunnelClient(refreshed.tunnel_address,self.port,self.token)
        self._check_permit(permit)
        activated=self.transport.call('/activate',{'authority':self.device_authority.native_grant(
            permit,self.native_handshake).wire()},timeout=5)
        expected_authority=self.device_authority.native_grant(permit,self.native_handshake).wire()
        check(set(activated)=={'activated','authority'} and activated.get('activated') is True
              and _exact_flat_mapping(activated.get('authority'),expected_authority),
              'native_protocol_mismatch','iPhone helper activation failed')
        check(self.startup_complete.wait(20),'startup_unknown',
              'iPhone target launch was not acknowledged')
        self._check_permit(permit)
        result={'ok':True}
        if self.profile is not None:
            launched=self.launched_identity_evidence
            check(isinstance(launched,dict) and launched.get('launchedBundle')==self.profile.bundle
                  and launched.get('profileDigest')==self.profile.digest,
                  'native_profile_mismatch','iPhone target launch identity is unavailable',409)
            result['identityEvidence']=dict(self.installed_identity_evidence,**launched)
        return result
    def start(self,session,lab,permit=None):
        self.sid=session['id'];self.lab=lab
        from .native_frame_clock import NativeFrameClock
        self.frame_clock=NativeFrameClock.for_session(lab,self.sid,self.device_authority)
        self.capture_started_at=int(time.time()*1000)
        if self.device_authority is None:self.lease=Lease('ios-device:'+self.udid)
        self.lease.__enter__()
        validated=validate_signed_products(self.products,self.app,profile=self.profile,ipa=self.ipa)
        expected={key:self.identity.get(key) for key in ('bundle','artifactDigest')}
        check({key:validated.get(key) for key in expected}==expected,
              'app_changed','Prepared app artifact changed')
        if self.profile is None:
            try:self.installed_build_id=app_info(self.app)['buildId']
            except ContractError:self.installed_build_id=None
        if self.device_authority is None:
            _devicectl('device','install','app','--device',self.device.identifier,str(self.app),timeout=90)
            if self.profile is None:
                try:self.installed_build_id=app_info(self.app)['buildId']
                except ContractError:self.installed_build_id=None
        if self.record_sdk or (self.profile is not None and self.app_logs_only):
            from ..ios_instrumentation import app_logs_enabled
            self.auto_profile=profile_from_app(self.app)
            self.automatic_app_logs=app_logs_enabled(self.app)
            check(self.profile is None or self.automatic_app_logs,
                  'app_changed','The declared iOS log adapter is absent from the selected app',400)
            self.auto_run_id=str(uuid.uuid4()) if self.auto_profile is not None else None
        # Installing after an idle period can reopen CoreDevice with a new USB
        # tunnel address. Bind the helper to the post-install device endpoint.
        if self.device_authority is None:
            refreshed=select_iphone(self.device.public_id)
            check(refreshed.udid==self.udid and public_device_status(refreshed)['ready'],
                  'device_unavailable','The selected iPhone connection is not ready after installation')
            self.device=refreshed
            self.transport=TunnelClient(refreshed.tunnel_address,self.port,self.token)
        self.temp=tempfile.TemporaryDirectory(prefix='repro-iphone-live-')
        config=prepare_xctestrun(self.products,'ReproLiveTests',Path(self.temp.name)/'live.xctestrun')
        with config.open('rb') as stream:document=plistlib.load(stream)
        if self.device_authority is not None:require_ios_helper_v2(document)
        for _,target in _targets(document):
            target.setdefault('EnvironmentVariables',{}).update(REPRO_LIVE_LISTEN_HOST=self.device.tunnel_address,
                REPRO_LIVE_LISTEN_PORT=str(self.port),REPRO_LIVE_TOKEN=self.token,REPRO_TARGET_BUNDLE=self.bundle)
            if self.profile is None:
                target['EnvironmentVariables'].update(REPRO_LIVE_CASE=self.fixture,REPRO_LIVE_RECORD_SDK='1' if self.record_sdk else '0')
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
        if self.device_authority is not None:self._check_permit(permit)
        self.process=subprocess.Popen(['/usr/bin/xcodebuild','test-without-building','-xctestrun',str(config),
            '-destination',f'id={self.udid}','-resultBundlePath',str(Path(self.temp.name)/'result.xcresult'),
            '-parallel-testing-enabled','NO','-only-testing:ReproLiveTests/LiveControlTests/testControlSession'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        self.monitor=threading.Thread(target=self._poll,daemon=True);self.monitor.start()
    def _receive_frame(self):
        try:
            if getattr(self,'native_frame_buffered',False):
                raw=self.transport.call(f'/frames/after/{self.last_native_frame}',binary=True)
                try:frame=json.loads(raw)
                except (TypeError,UnicodeDecodeError,json.JSONDecodeError) as exc:
                    raise LiveError('invalid_frame','Native buffered frame is invalid') from exc
                check(isinstance(frame,dict),'invalid_frame','Native buffered frame is invalid',400)
            else:
                frame=self.transport.call('/frame')
        except LiveError as exc:
            # helper는 첫 프레임 버퍼링 전에 ready를 보고한다. 첫 프레임 대기 중의
            # 빈 버퍼 응답만 재시도하고 나머지 브리지 거절은 그대로 실패시킨다.
            # 첫 프레임이 계속 없으면 세션은 connecting에 머물러 발급 측 데드라인이 종료한다.
            if exc.code=='frame_unavailable' and self.last_native_frame==0:return
            raise
        require_runtime_frame(self.profile,frame.get('width'),frame.get('height'),frame.get('orientation'))
        frame_id=frame.get('nativeFrameId')
        check(type(frame_id) is int and frame_id>0,'invalid_frame','Device frame identity missing')
        with self.frame_mutex:
            if frame_id<=self.last_native_frame:return
            data=base64.b64decode(frame['imageBase64'],validate=True)
            clock=getattr(self,'frame_clock',None)
            timing=(clock.frame_arguments(frame) if clock is not None else
                    {'timing_source':'native-unmapped'})
            gap=(self.last_native_frame + 1, frame_id - 1) if (self.last_native_frame or getattr(self,'native_frame_buffered',False)) and frame_id > self.last_native_frame + 1 else None
            kwargs={'acquisition_sequence':frame_id,**timing}
            if gap is not None:kwargs['native_sequence_gap']=gap
            self.lab.publish_frame(self.sid,data,frame['mime'],frame['width'],frame['height'],frame['orientation'],frame['capturedAt'],
                                   **kwargs)
            self.last_native_frame=frame_id
    def _poll(self):
        deadline=time.monotonic()+95;ready=False
        while not self.stop.wait(.05):
            if self.process.poll() is not None:
                self.lab.fail(self.sid,'Physical iPhone driver stopped');return
            try:
                clock=getattr(self,'frame_clock',None)
                if not ready or (clock is not None and clock.refresh_due()):
                    sent=clock.synchronizer.sample() if clock is not None else None
                    status=self.transport.call('/status',timeout=2)
                    received=clock.synchronizer.sample() if clock is not None else None
                    if self.profile is not None:
                        check(status.get('targetBundle')==self.profile.bundle
                              and status.get('applicationProfileDigest')==self.profile.digest
                              and isinstance(status.get('capabilities'),dict)
                              and set(status['capabilities'].get('actions',[]))
                                  == set(self.profile.data['capabilities']['actions']),
                              'native_profile_mismatch','iPhone helper target identity differs',400)
                    self._validate_native_frame_buffer(status)
                    if self.device_authority is not None and self.native_handshake is None:
                        try:
                            self.native_handshake=self.device_authority.bind_native_handshake(
                                self._startup_permit,protocol_version=status.get('protocolVersion'),
                                helper_version=status.get('helperVersion'),helper_incarnation=status.get('helperIncarnation'),
                                provider_incarnation=status.get('providerIncarnation'),native_incarnation=status.get('nativeIncarnation'),
                                native_clock_id=status.get('nativeClockId'),native_time_ms=status.get('nativeTimeMs'))
                        except ContractError:
                            raise LiveError('native_protocol_mismatch','iPhone helper protocol is incompatible',400) from None
                        if self.handshake_ready is not None:self.handshake_ready.set()
                    if clock is not None:
                        clock.accept_status(self.native_handshake,status,sent,received)
                    native_ready=status.get('ready') is True and (self.device_authority is None or self.native_handshake is not None)
                    if native_ready and self.auto_profile is not None:
                        if self.app_logs_only:self.collect_app_logs();native_ready=self.app_log_marker is not None
                        else:self._pin_auto_marker();native_ready=self.auto_marker is not None
                    ready=native_ready
                    if ready and self.profile is not None:
                        self.launched_identity_evidence={
                            'launchedBundle':status['targetBundle'],
                            'profileDigest':status['applicationProfileDigest'],
                            'helperProtocolVersion':status['protocolVersion'],
                            'helperVersion':status['helperVersion']}
                    if ready and getattr(self,'startup_complete',None) is not None:
                        self.startup_complete.set()
                    if not ready:
                        check(time.monotonic()<deadline,'startup_timeout','Physical iPhone driver did not become ready')
                        continue
                self._receive_frame()
            except ContractError:
                if self.auto_profile is not None:
                    self.lab.fail(self.sid,'Automatic iOS session identity did not match the selected app');return
                if ready or time.monotonic()>=deadline:
                    self.lab.fail(self.sid,'iPhone bridge unavailable; reconnect and close the session');return
            except Exception:
                if ready or time.monotonic()>=deadline:
                    self.lab.fail(self.sid,'iPhone bridge unavailable; reconnect and close the session');return
    def execute(self,action,payload):return self._execute_physical(action,payload,None)
    def execute_authorized(self,action,payload,permit,frame=None):
        self._check_permit(permit)
        return self._execute_physical(action,payload,permit)
    def _execute_physical(self,action,payload,permit):
        if action == 'authority_cleanup':
            check(permit is not None, 'authority_required', 'Native cleanup requires its current authority permit', 400)
        elif self.profile is not None:
            check(action in self.profile.data['capabilities']['actions'],
                  'unsupported_operation','General iOS profile does not allow this action',400)
        payload=dict(payload or {})
        restart_logs = action == 'launch' and self.profile is not None and self.app_logs_only
        if action=='reset' or restart_logs:
            self.capture_started_at=int(time.time()*1000);self.auto_marker=None
            self.app_log_marker=None
            if self.auto_profile is not None:
                self.auto_run_id=str(uuid.uuid4());payload['autoRunId']=self.auto_run_id
        command_id=permit.operation_id if permit is not None else uuid.uuid4().hex
        command={'id':command_id,'action':action,'payload':payload}
        if permit is not None:
            self._check_permit(permit)
            command['authority']=self.device_authority.native_grant(permit,self.native_handshake).wire()
        try:accepted=self.transport.call('/command',command)
        except LiveError as exc:
            if exc.code=='authority_rejected':return {'ok':False,'outcome':'rejected','code':'authority_rejected'}
            raise
        check(accepted.get('accepted') is True,'injection_unknown','iPhone did not accept the command')
        deadline=time.monotonic()+25
        while not self.stop.wait(.05):
            result=self.transport.call('/ack/'+command_id)
            if result.get('pending') is True:
                check(set(result)=={'pending'},'injection_unknown','iPhone pending acknowledgement is invalid')
                check(time.monotonic()<deadline,'native_timeout','iPhone input acknowledgement timed out');continue
            expected={'pending','id','ok','timing'}|({'error'} if 'error' in result else set())
            if permit is not None:expected.add('authority')
            cleanup_evidence=action=='authority_cleanup' and result.get('ok') is True
            if cleanup_evidence:expected.add('cleanupEvidence')
            check(set(result)==expected and result.get('pending') is False
                  and result.get('id')==command_id and type(result.get('ok')) is bool
                  and result.get('timing')=='best-effort'
                  and (permit is None or _exact_flat_mapping(result.get('authority'),command['authority'])),
                  'injection_unknown','iPhone acknowledgement identity differs')
            if cleanup_evidence:
                evidence=result.get('cleanupEvidence')
                check(type(evidence) is dict and set(evidence)=={'bundleId','state','observer'}
                      and evidence.get('bundleId')==self.bundle
                      and evidence.get('state')=='not-running'
                      and evidence.get('observer')=='xctest-application-state',
                      'injection_unknown','iPhone cleanup evidence is invalid')
            if (action=='reset' or restart_logs) and self.auto_profile is not None:
                if self.app_logs_only:self._wait_for_app_log_marker()
                else:self._wait_for_auto_marker()
            self._receive_frame()
            response={'ok':result.get('ok') is True,'timing':'best-effort'}
            if response['ok'] is False and result.get('error') in {'authority_rejected','authority_expired'}:
                response.update(outcome='rejected',code=result['error'])
            return response
        raise LiveError('session_inactive','iPhone session is closing')

    def _validate_native_frame_buffer(self,status):
        capabilities=status.get('capabilities') if isinstance(status,dict) else None
        check(isinstance(capabilities,dict),'native_protocol_mismatch',
              'iPhone helper capabilities are unavailable',400)
        version=capabilities.get('nativeFrameBufferVersion')
        check(version is None or (type(version) is int and version==1),
              'native_protocol_mismatch','iPhone helper frame buffer version is unsupported',400)
        capacity=capabilities.get('nativeFrameBufferCapacity',16)
        check(type(capacity) is int and 1<=capacity<=4096,
              'native_protocol_mismatch','iPhone helper frame buffer capacity is invalid',400)
        if getattr(self,'native_frame_buffer_bound',False):
            check(version==getattr(self,'native_frame_buffer_version',None)
                  and capacity==getattr(self,'native_frame_buffer_capacity',16),
                  'native_protocol_mismatch','iPhone helper frame buffer changed',409)
        else:
            self.native_frame_buffer_version=version
            self.native_frame_buffered=version==1
            self.native_frame_buffer_capacity=capacity
            self.native_frame_buffer_bound=True

    def _read_auto_marker(self):
        with tempfile.TemporaryDirectory(prefix='repro-iphone-auto-marker-') as directory:
            destination=Path(directory)/'auto-session.json'
            _devicectl('device','copy','from','--device',self.device.identifier,
                '--domain-type','appDataContainer','--domain-identifier',self.bundle,
                '--source','Library/Application Support/ReproLoop/auto-session.json',
                '--destination',str(destination),timeout=30)
            check(destination.is_file() and destination.stat().st_size<=1024*1024,
                  'capture_invalid','Automatic iOS session marker is unavailable')
            return read_json(destination)
    def _pin_auto_marker(self):
        if self.auto_profile is None or self.auto_run_id is None:return None
        try:marker=self._read_auto_marker()
        except (LiveError,ContractError,OSError,ValueError,json.JSONDecodeError):return None
        expected_fixture=case_spec(self.fixture).fixture
        _require_profile_fixture(self.auto_profile,expected_fixture)
        validate_ios_auto_marker(marker,self.auto_profile,run_id=self.auto_run_id,
                                 build_id=self.installed_build_id,fixture=expected_fixture,
                                 min_started_at=self.capture_started_at)
        self.auto_marker=marker;return marker
    def _wait_for_auto_marker(self,timeout=10):
        deadline=time.monotonic()+min(timeout,10)
        while time.monotonic()<deadline:
            if self._pin_auto_marker() is not None:return self.auto_marker
            time.sleep(.1)
        raise LiveError('capture_invalid','Automatic iOS session marker did not become ready',400)
    def collect_sdk_capture_authorized(self,permit):
        self._check_permit(permit)
        return self.collect_sdk_capture(permit=permit)
    def collect_sdk_capture(self,permit=None):
        check(self.record_sdk,'unsupported_operation','SDK capture is not configured',400)
        check(not self.app_logs_only,'unsupported_operation','This session records app observations only',400)
        if self.auto_profile is not None:
            check(self._execute_physical('report_capture',{},permit).get('ok') is True,'capture_invalid','Automatic iOS capture was not finalized')
            from ..ios_device import IosPhysicalDevice
            reader=IosPhysicalDevice(self.device.public_id);reader.installed_build_id=self.installed_build_id
            if permit is not None:self._check_permit(permit)
            return reader.collect_capture(self.capture_started_at,expected_run_id=self.auto_run_id,
                                          auto_profile=self.auto_profile,expected_fixture=case_spec(self.fixture).fixture)
        check(self._execute_physical('report_capture',{},permit).get('ok') is True,'capture_invalid','Return to the sample counter screen and finish a valid QA/Test recording')
        from ..ios_device import IosPhysicalDevice
        if permit is not None:self._check_permit(permit)
        return IosPhysicalDevice(self.device.public_id).collect_capture(self.capture_started_at)
    def collect_sdk_diagnostics_authorized(self,capture,permit):
        self._check_permit(permit)
        return self.collect_sdk_diagnostics(capture)
    def collect_sdk_diagnostics(self,capture):
        check(self.auto_profile is not None and self.auto_run_id is not None,
              'unsupported_operation','Automatic iOS capture profile is unavailable',400)
        from ..ios_device import IosPhysicalDevice
        reader=IosPhysicalDevice(self.device.public_id);reader.installed_build_id=self.installed_build_id
        return reader.collect_auto_diagnostics(capture,self.auto_run_id,self.auto_profile,
                                               expected_fixture=case_spec(self.fixture).fixture)
    def _read_app_log_json(self,relative):
        from ..ios_device import IosPhysicalDevice
        from ..app_logs import MAX_APP_LOG_BYTES
        reader = IosPhysicalDevice(self.device.public_id)
        if self.device_authority is not None:
            reader.authority_lease = self.device_authority.borrowed_lease()
        if self.profile is not None:
            return reader.read_app_json(relative, max_bytes=MAX_APP_LOG_BYTES, application_id=self.bundle)
        return reader.read_app_json(relative,max_bytes=MAX_APP_LOG_BYTES)
    def close_authorized(self,permit):
        self._check_permit(permit)
        result=self.execute_authorized('authority_cleanup',{},permit)
        check(result.get('ok') is True,'cleanup_uncertain','iPhone helper did not confirm target cleanup')
        self.stop.set()
        try:
            stopped=self.transport.call('/stop',{'authority':self.device_authority.native_grant(
                permit,self.native_handshake).wire()},timeout=3)
            expected_authority=self.device_authority.native_grant(permit,self.native_handshake).wire()
            check(set(stopped)=={'stopped','authority'} and stopped.get('stopped') is True
                  and _exact_flat_mapping(stopped.get('authority'),expected_authority),
                  'cleanup_uncertain','iPhone helper stop was not confirmed')
            result=super()._close(permit)
            return result
        finally:self.transport.token=''
    def close(self):
        self.stop.set()
        if self.process is not None and self.process.poll() is None:
            try:self.transport.call('/stop',{},timeout=3)
            except Exception:pass
        try:super().close()
        finally:self.transport.token=''


def iphone_device(public_id,products,app,*,fixture='counter',record_sdk=False,app_logs_only=False,
                  authority_mode='shared-v2',profile=None):
    check(authority_mode in {'shared-v2','legacy-offline-v1'},'invalid_argument','Invalid authority compatibility mode',400)
    if profile is not None and not isinstance(profile,IosAppProfile):profile=validate_ios_profile(profile)
    if profile is not None:
        _require_ios_runtime_capabilities(profile, app)
    device=select_iphone(public_id);identity=validate_signed_products(products,app,profile=profile)
    from ..ios_instrumentation import app_logs_enabled
    general=profile is not None
    actions=list(profile.data['capabilities']['actions']) if general else list(CAPABILITIES['actions'])
    profile_logs=(general and profile.data['capabilities']['logAdapter'] is not None)
    descriptor={'id':device.public_id,'name':device.model,'platform':'ios','kind':'ios-physical' if general or fixture=='counter' else 'ios-physical-'+fixture,
        'capabilities':dict(CAPABILITIES,applicationIdentity=identity,transport='paired-usb-tunnel',verification='device-validation-pending',fixture=fixture,sdkCapture=record_sdk and not app_logs_only,automaticAppLogs=record_sdk and app_logs_enabled(app),resetContract='sample-'+fixture+'-fixture-v1',authorityMode=authority_mode),
        'factory':lambda:PhysicalIosProvider(device,products,app,identity,fixture=fixture,record_sdk=record_sdk,app_logs_only=app_logs_only,profile=profile)}
    if general:
        descriptor['capabilities'].update(actions=actions,fixture=None,sdkCapture=False,
            automaticAppLogs=profile_logs,resetContract=None,
            applicationProfile=profile.data,applicationProfileDigest=profile.digest,
            identityEvidence='selected-signed-artifact;installed-digest-unavailable',
            locatorKinds=[])
    if authority_mode=='shared-v2':descriptor['_authority']={'deviceKind':'ios-physical','physicalId':device.udid}
    return descriptor


def prepare_iphone_build(output,team=None,provision=False):
    """Build reviewable unsigned kits by default; signing/network are explicit flags."""
    from ..ios_build import xcode_environment
    from ..repair import run_command
    from ..storage import write_json
    from ..resources import resource_root
    root=resource_root();output=Path(output).resolve()
    check(not output.exists(),'output_exists','Use a new iPhone build output directory',400)
    check(team is None or re.fullmatch(r'[A-Z0-9]{10}',team),'invalid_team','Apple team ID must be 10 uppercase letters or digits',400)
    check(not provision or team is not None,'team_required','Provisioning requires an explicit Apple team ID',400)
    output.mkdir(parents=True,mode=0o700)
    sources={name:digest(tree_manifest(root/name,True)) for name in ('ios','live-ios')}
    build_id=sources['ios'][:32]
    signing=['CODE_SIGNING_ALLOWED=NO','CODE_SIGNING_REQUIRED=NO'] if team is None else ['CODE_SIGN_STYLE=Automatic','DEVELOPMENT_TEAM='+team]
    paths={}
    for name,project,scheme in [('runner','live-ios/ReproLive.xcodeproj','ReproLive'),('sample','ios/ReproLoop.xcodeproj','ReproReplay')]:
        derived=output/name
        command=['/usr/bin/xcodebuild','build-for-testing','-project',str(root/project),'-scheme',scheme,
            '-configuration','Debug','-sdk','iphoneos','-destination','generic/platform=iOS',
            '-derivedDataPath',str(derived),'-disableAutomaticPackageResolution','COMPILER_INDEX_STORE_ENABLE=NO',
            'REPRO_BUILD_ID='+build_id,*signing]
        if provision:command.append('-allowProvisioningUpdates')
        # Build output is intentionally not persisted: signing diagnostics may
        # contain personal account metadata. Errors returned by run_command are bounded.
        run_command(command,str(root),timeout=300,max_output=4*1024*1024,env_extra=xcode_environment())
        paths[name]=derived/'Build/Products'
    check(sources=={name:digest(tree_manifest(root/name,True)) for name in ('ios','live-ios')},'source_changed','Build modified protected source')
    app=paths['sample']/'Debug-iphoneos/ReproSample.app'
    check(app.is_dir(),'build_incomplete','iPhone sample app was not produced')
    receipt={'schemaVersion':1,'platform':'ios-physical','signed':team is not None,'provisioningRequested':provision,
        'sourceDigests':sources,'buildId':build_id,'runnerProducts':str(paths['runner']),'sampleApp':str(app)}
    if team is not None:receipt['applicationIdentity']=validate_signed_products(paths['runner'],app)
    write_json(output/'receipt.json',receipt);return receipt


def main(argv=None):
    import argparse
    from ..core import ContractError
    parser=argparse.ArgumentParser(prog='reproloop')
    subs=parser.add_subparsers(dest='command',required=True)
    subs.add_parser('live-device-doctor')
    build=subs.add_parser('live-iphone-build');build.add_argument('--output',type=Path,required=True)
    build.add_argument('--team');build.add_argument('--provision',action='store_true',help='Explicitly allow Xcode Apple portal provisioning')
    args=parser.parse_args(argv)
    try:
        if args.command=='live-device-doctor':
            result={'iphones':[public_device_status(d) for d in discover_iphones()]}
            from ..device import find_adb
            try:
                listing=subprocess.run([find_adb(),'devices'],capture_output=True,text=True,timeout=10)
                states=[line.split()[-1] for line in listing.stdout.splitlines()[1:] if line.strip()]
                result['android']={'ready':states.count('device'),'unauthorized':states.count('unauthorized'),'offline':states.count('offline')}
            except Exception:result['android']={'toolAvailable':False}
        else:result=prepare_iphone_build(args.output,args.team,args.provision)
        print(json.dumps(result));return 0
    except LiveError as error:
        print(json.dumps({'error':{'code':error.code,'message':str(error)}}));return 2
    except (ContractError,OSError,ValueError,subprocess.SubprocessError):
        print(json.dumps({'error':{'code':'device_preparation_failed','message':'Check device connection, Xcode configuration, and signing setup'}}));return 2
