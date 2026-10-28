"""Private UNQUALIFIED recovery transport. No normal Live recovery integration.

The deliberately narrow constructor/start copy avoids changing ordinary provider
hooks, discovery, installation, cleanup, and credential semantics.
"""
from __future__ import annotations
import base64
from collections import OrderedDict
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import threading
import time
from . import android_live as live
from ..core import ContractError
from ..device import AdbDevice, find_adb
from ..execution.artifacts import open_regular
from ..repair_android_signing import _ProcessOwner
from ..android_profile import AndroidRuntimeProfile


def _require(value):
    if not value:raise ContractError('Private Android recovery transport unavailable')


def _ascii(raw):
    try:return raw.decode('ascii')
    except UnicodeError:raise ContractError('Invalid bounded Android response') from None


def _digest_file(path,limit):
    path=Path(path).absolute();sha=hashlib.sha256()
    with os.fdopen(open_regular(path.parent,path.name),'rb') as stream:
        before=os.fstat(stream.fileno());_require(0<before.st_size<=limit and before.st_nlink==1)
        total=0
        while raw:=stream.read(1024*1024):
            total+=len(raw);_require(total<=before.st_size);sha.update(raw)
        after=os.fstat(stream.fileno());_require(total==before.st_size and
            (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_nlink)==
            (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_nlink))
    return sha.hexdigest()


class _QualificationAdbDevice(AdbDevice):
    """No device enumeration; every future command has finite output ownership."""
    def __init__(self,serial,work):
        _require(type(serial) is str and re.fullmatch(r'emulator-[0-9]{4,5}',serial)
            and 1024<=int(serial[9:])<=65535)
        self.serial=serial;self.adb=find_adb();self.work=Path(work)
        self.identity=hashlib.sha256(serial.encode()).hexdigest()[:16]
        self.authority_lease=None;self._lease_local=threading.local();self.app_profile=None
        self.owner=_ProcessOwner();self.guard=None;self.cancel=threading.Event()

    def _run(self,args,timeout=5,input_bytes=b'',allow_absent=False):
        _require(callable(self.guard));self.guard()
        result=self.owner.run(tuple(map(str,args)),work=self.work,input_bytes=input_bytes,pass_fds=(),
            cancellation=self.cancel,deadline_monotonic=time.monotonic()+min(timeout or 5,10),max_output_bytes=65536)
        self.guard();_require(result.terminated and result.bounded and not result.interrupted)
        _require(result.returncode==0 or (allow_absent and result.returncode==1 and result.stdout==b''))
        return result.stdout

    def _command(self,args,timeout=None):return self._run(args,timeout or 5)

    def _fixed(self,*args,allow_absent=False):
        return self._run((self.adb,'-s',self.serial,'shell',shlex.join(args)),allow_absent=allow_absent)

    def verify_emulator(self):_require(self._fixed('getprop','ro.kernel.qemu').strip()==b'1')

    def helper_process(self,expected=None,absent=False,allow_missing=False):
        if expected is not None:
            _require(type(expected) is dict and set(expected)=={'pid','startTicks'}
                and type(expected['pid']) is int and 1<=expected['pid']<=2147483647
                and type(expected['startTicks']) is str and re.fullmatch('[1-9][0-9]{0,19}',expected['startTicks']))
        raw=self._fixed('pidof',live.HELPER,allow_absent=absent or allow_missing).strip()
        if allow_missing and not raw:return None
        if absent:
            _require(raw==b'');return None
        _require(re.fullmatch(rb'[1-9][0-9]{0,9}',raw) is not None)
        pid=int(raw);_require(pid<=2147483647)
        command=self._fixed('cat',f'/proc/{pid}/cmdline')
        _require(command.rstrip(b'\0')==live.HELPER.encode())
        stat=_ascii(self._fixed('cat',f'/proc/{pid}/stat'))
        end=stat.rfind(')');_require(end>0 and stat[:stat.find(' ')].isdigit() and int(stat[:stat.find(' ')])==pid)
        fields=stat[end+2:].split();_require(len(fields)>=20 and re.fullmatch('[1-9][0-9]{0,19}',fields[19]))
        result={'pid':pid,'startTicks':fields[19]}
        _require(expected is None or result==expected)
        return result


class _QualificationBridge(live.UsbBridgeClient):
    def call(self,path,body=None,timeout=5,binary=False):
        if not path.startswith('/qualification-recovery'):return super().call(path,body,timeout,binary)
        _require(path in {'/qualification-recovery','/qualification-recovery/status'} or
            re.fullmatch(r'/qualification-recovery/recovery_[a-f0-9]{32}(?:/frame)?',path))
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=min(timeout,5))
        try:
            data=None if body is None else json.dumps(body,allow_nan=False,separators=(',',':')).encode()
            _require(data is None or len(data)<=8192)
            headers={'Authorization':'Bearer '+self.token,'Connection':'close'}
            if data is not None:headers['Content-Type']='application/json'
            connection.request('GET' if data is None else 'POST',path,data,headers)
            response=connection.getresponse();limit=4*1024*1024 if binary else 65536
            raw=response.read(limit+1);_require(response.status in {200,202} and len(raw)<=limit)
            if binary:return raw
            value=json.loads(raw);_require(type(value) is dict);return value
        except (ValueError,UnicodeError,RecursionError):raise ContractError('Invalid recovery response') from None
        finally:connection.close()


class _Producer:
    def _configure(self,configuration):
        from .android_recovery_authority import _digest, _binding
        _require(type(configuration) is dict and configuration.get('phase') in {'session','probe'})
        bound=dict(configuration);bound.pop('phase')
        bound.update(hostIncarnation='validation',helperIncarnation='validation',providerIncarnation='validation')
        _binding(bound)
        raw=json.dumps(configuration,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        _require(len(raw)<=8192)
        self._configuration=raw;self.configuration_digest=_digest(configuration)
        self._retirement=None;self._exit_receipt=None;self._requests={};self._preexit_process=None

    @property
    def qualification_configuration(self):return json.loads(self._configuration)

    @property
    def startup_status(self):return None if self._startup_status is None else json.loads(self._startup_status)

    def read_status(self):return self.transport.call('/qualification-recovery/status')

    def recovery_call(self,grant):
        wire=grant.wire();grant._issuer.check()
        _require(wire['configurationDigest']==self.configuration_digest)
        _require(wire['operationId'] not in self._requests and len(self._requests)<3)
        if wire['purpose'] in {'old-exit','probe-exit'}:
            _require(self._retirement is not None)
            self.device.guard=grant._issuer.check
            self._preexit_process=self.device.helper_process(self._retirement['nativeProcessIdentity'])
            grant._issuer.check()
        self._requests[wire['operationId']]=wire
        value=self.transport.call('/qualification-recovery',{'grant':wire})
        grant._issuer.check();return value

    def recovery_result(self,grant):
        wire=grant.wire();_require(self._requests.get(wire['operationId'])==wire)
        value=self.transport.call('/qualification-recovery/'+wire['operationId']);grant._issuer.check()
        if value.get('pending') is False and type(value.get('receipt')) is dict:
            receipt=value['receipt']
            _require(receipt.get('grant')==wire and receipt.get('status')=='complete')
            if receipt.get('kind')=='retirement':self._retirement=json.loads(json.dumps(receipt))
            elif receipt.get('kind')=='exit-accepted':self._exit_receipt=json.loads(json.dumps(receipt))
        return value

    def recovery_frame(self,grant):
        wire=grant.wire();_require(wire['purpose']=='fresh-probe' and self._requests.get(wire['operationId'])==wire)
        value=self.transport.call('/qualification-recovery/'+wire['operationId']+'/frame',binary=True)
        grant._issuer.check();return value

    def collect_exit(self,issuer,grant):
        wire=grant.wire();issuer.check();_require(grant._issuer is issuer)
        _require(wire['purpose'] in {'old-exit','probe-exit'} and self._retirement is not None
            and self._exit_receipt is not None and self._exit_receipt['grant']==wire)
        evidence=self._retirement.get('evidence',{})
        _require(all(evidence.get(key) is True for key in ('normalAdmissionClosed','commandQueueDrained',
            'inflightCompleted','pointersCancelled','captureWorkersStopped','inspectionMediaDrained','publicationClosed')))
        expected=self._retirement['nativeProcessIdentity']
        _require(expected==self._exit_receipt['nativeProcessIdentity']==self._preexit_process)
        self.device.guard=issuer.check
        present=self.device.helper_process(expected,allow_missing=True)
        if present is not None:
            issuer.check();self.device._fixed('am','force-stop',live.HELPER);issuer.check()
        deadline=time.monotonic()+5
        while True:
            issuer.check()
            try:self.device.helper_process(absent=True);break
            except ContractError:
                _require(time.monotonic()<deadline);time.sleep(.05)
        issuer.check();self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5);issuer.check();_require(not self.thread.is_alive())
        _require(self.process is not None);issuer.check()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            issuer.check();_require(_ProcessOwner._terminate(self.process))
        issuer.check();_require(self.process.poll() is not None and _ProcessOwner._group_empty(self.process.pid))
        self.device.helper_process(absent=True)
        self.device._fixed('test','!','-e',f"/proc/{expected['pid']}");issuer.check()
        if self.port is not None:self.device.adb_call('forward','--remove','tcp:'+str(self.port),timeout=5)
        issuer.check();self.token='';self.transport.token=''
        return {'remoteHelperAbsent':True,'clientCollected':True,'pollerJoined':True,
            'nativeProcessIdentity':expected}

    def _verify_installed(self,package,path,expected_digest,expected_version):
        _require(package in {live.HELPER,self.target_package})
        _require(_digest_file(path,150*1024*1024)==expected_digest)
        identity=self.device.apk_identity(path)
        _require(identity=={'package':package,'versionCode':expected_version})
        raw=_ascii(self.device._fixed('pm','path',package)).splitlines()
        _require(len(raw)==1 and re.fullmatch(r'package:/data/app/[A-Za-z0-9_./+=~-]+\.apk',raw[0]))
        installed=raw[0][8:];_require('..' not in installed.split('/') and len(installed)<=1024)
        observed=_ascii(self.device._fixed('sha256sum',installed)).split()
        _require(len(observed)==2 and observed==[expected_digest,installed])
        _require(_digest_file(path,150*1024*1024)==expected_digest)

    def _spawn(self,config):
        # Never overwrite/start against an unrelated existing helper owner.
        self.device.helper_process(absent=True)
        raw=json.dumps(config,separators=(',',':')).encode();_require(len(raw)<=16384)
        command=shlex.join(['run-as',live.HELPER,'sh','-c','umask 077 && mkdir -p files && cat > files/live-config.json'])
        self.device._run((self.device.adb,'-s',self.device.serial,'shell','-T',command),input_bytes=raw)
        port=self.device.adb_call('forward','tcp:0','tcp:8766').strip()
        _require(port.isdigit() and 1024<=int(port)<=65535);self.port=int(port)
        self.transport=_QualificationBridge(self.port,self.token)
        self.device.guard()
        self.process=subprocess.Popen([self.device.adb,'-s',self.device.serial,'shell',
            'am instrument -w -r '+live.HELPER+'/.LiveInstrumentation'],stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,start_new_session=True)
        self.device.guard()

    def _config(self,helper,host,provider):
        raw=self._configuration
        return {'token':self.token,'port':8766,'targetPackage':self.target_package,'maxFps':15,'maxWidth':960,
            'recordSdk':False,'protocolVersion':live.NATIVE_PROTOCOL_VERSION,'helperVersion':live.HELPER_VERSION,
            'helperIncarnation':helper,'hostIncarnation':host,'providerIncarnation':provider,
            'appProfile':self.app_profile.native(),'profileDigest':self.app_profile.digest,
            'qualificationRecovery':{'configurationBase64':base64.b64encode(raw).decode(),
                'configurationDigest':self.configuration_digest}}

class QualificationAndroidProvider(_Producer,live.AndroidLiveProvider):
    """General shared session with preinstalled artifacts; never ordinary cleanup."""
    def __init__(self,owner,serial,helper_apk,app_apk,*,runtime_profile,helper_digest,work):
        _require(type(runtime_profile) is AndroidRuntimeProfile)
        data=runtime_profile.data
        _require(data['package']!=live.HELPER and data['capabilities']['logAdapter'] is None
            and data['capabilities']['captureAdapter']=={'id':'native-frame','version':1}
            and 'pixels' in data['capabilities']['observations'])
        _require(type(helper_digest) is str and re.fullmatch('[0-9a-f]{64}',helper_digest))
        self.owner=owner;self.app_profile=runtime_profile;self.general_profile=True;self.explicit_profile=True
        self.target_package=data['package'];self.target_activity=data['launchTarget']['value']
        self.device=_QualificationAdbDevice(serial,work);self.helper_apk=Path(helper_apk).absolute()
        self.sample_apk=Path(app_apk).absolute();self.helper_digest=helper_digest;self.identity=runtime_profile.application_identity
        self.launch_config=None;self.launch_configuration_verified=False;self.fixture=None;self.fixture_spec=None
        self.fixture_config=None;self._fixture_context=None;self._fixture_session=None;self._fixture_committed=False
        self._fixture_challenge=None;self.record_sdk=False;self.automatic_app_logs=False;self.observation_profile=None
        self._managed_device=False;self.artifact_temp=None;self.capture_max_width=960
        self.stop=threading.Event();self.frame_mutex=threading.RLock();self.lease=None;self.lease_held=False
        self.process=None;self.port=None;self.transport=None;self.thread=None;self.token=secrets.token_urlsafe(32)
        self.last_native_frame=0;self.frame_map=OrderedDict();self.capture_started_at=None;self.capture_session_id=None
        self.app_log_run_id=None;self.app_log_marker=None;self.device_authority=None;self.provider_incarnation=None
        self.native_handshake=None;self.handshake_ready=None;self._startup_permit=None;self._startup_lock=threading.RLock()
        self._startup_deadline=None;self._startup_result=None;self._startup_transport_failed=False
        self.native_frame_buffer_bound=False;self.native_frame_buffered=False;self.native_frame_buffer_version=None
        self.native_frame_buffer_capacity=16;self.native_frame_timing_declared=False;self.installed_identity_evidence=None
        self.helper_ready=threading.Event();self.target_launched=False;self._startup_status=None;self.frame_clock=None

    def _validate_native_metadata(self,value):
        result=super()._validate_native_metadata(value)
        _require(type(value.get('recoveryQualificationVersion')) is int and value['recoveryQualificationVersion']==1
            and value.get('recoveryQualificationConfigurationDigest')==self.configuration_digest)
        if self.startup_status is None:self._startup_status=json.dumps(value)
        return result

    def start_authorized(self,session,lab,permit):
        self._startup_permit=permit;self.handshake_ready=threading.Event()
        try:
            self.start(session,lab,permit)
            return {'ok':True,'identityEvidence':dict(self.installed_identity_evidence,
                launchedPackage=self.target_package,profileDigest=self.app_profile.digest,
                helperProtocolVersion=live.NATIVE_PROTOCOL_VERSION,helperVersion=live.HELPER_VERSION)}
        except BaseException:
            self.stop.set();self.owner.failure(self);raise

    def start(self,session,lab,permit=None):
        self._check_permit(permit);_require(self.process is None and self.startup_status is None)
        self.sid=session['id'];self.lab=lab;self._fixture_session=session
        configuration=self.owner.bind_initial(self,session,permit);self._configure(configuration)
        _require(configuration['phase']=='session' and configuration['applicationId']==self.app_profile.data['applicationId']
            and configuration['profileDigest']==self.app_profile.digest
            and configuration['artifactDigest']==self.app_profile.data['artifact']['sha256']
            and configuration['helperArtifactDigest']==self.helper_digest)
        self.owner.require_initial(self);self.owner.initial_dispatch(self)
        self.device.guard=lambda:self._check_permit(permit)
        self.lease.__enter__();self.lease_held=True
        self.device.verify_emulator()
        artifact=self.app_profile.data['artifact']
        self._verify_installed(live.HELPER,self.helper_apk,self.helper_digest,live.HELPER_VERSION)
        self._verify_installed(self.target_package,self.sample_apk,artifact['sha256'],artifact['versionCode'])
        self.installed_identity_evidence={'package':self.target_package,'versionCode':artifact['versionCode'],
            'selectedArtifactDigest':artifact['sha256'],'installedArtifactDigest':artifact['sha256'],
            'installedDigestProof':'sha256sum-single-apk','splitApkSupport':'unsupported'}
        self.helper_incarnation=permit.helper_incarnation;self.host_incarnation=permit.host_incarnation
        self.provider_incarnation=permit.provider_incarnation
        from .native_frame_clock import NativeFrameClock
        self.frame_clock=NativeFrameClock.for_session(lab,self.sid,self.device_authority)
        self.capture_started_at=int(time.time()*1000)
        with self._startup_lock:self._startup_deadline=time.monotonic()+live.ANDROID_STARTUP_TIMEOUT_SECONDS
        self._spawn(self._config(self.helper_incarnation,self.host_incarnation,self.provider_incarnation))
        self.thread=threading.Thread(target=self._poll,daemon=True);self.thread.start();self._wait_startup()
        self._check_target_startup(permit)
        self.device.helper_process(self.startup_status['nativeProcessIdentity'])
        _require(self.helper_ready.wait(5))
        result=self._execute('launch',{'applicationId':self.app_profile.data['applicationId']},permit=permit)
        _require(result.get('ok') is True);self.target_launched=True
        self.owner.initial_started(self)

    def close(self):raise ContractError('Private qualification owner must retain and collect producer')
    def close_authorized(self,permit):return self.close()


class _ProbeProducer(_Producer):
    """Recovery-only helper; never a Lab provider or ordinary dispatch permit."""
    def __init__(self,old,configuration,helper_incarnation,provider_incarnation,issuer):
        _require(type(old) is QualificationAndroidProvider and old._exit_receipt is not None
            and old.token=='' and old.process.poll() is not None)
        self.old=old;self.issuer=issuer;self.device=old.device;self.app_profile=old.app_profile
        self.target_package=old.target_package;self.helper_apk=old.helper_apk;self.sample_apk=old.sample_apk
        self.helper_digest=old.helper_digest;self.token=secrets.token_urlsafe(32);self.port=None
        self.process=None;self.transport=None;self.thread=None;self.stop=threading.Event();self._startup_status=None
        self.helper_incarnation=helper_incarnation;self.provider_incarnation=provider_incarnation
        self.host_incarnation=old.host_incarnation
        _require(helper_incarnation!=old.helper_incarnation and provider_incarnation!=old.provider_incarnation)
        _require(configuration==dict(old.qualification_configuration,phase='probe'))
        _require(all(type(value) is str and re.fullmatch('[a-z][a-z0-9_-]{0,63}',value)
            for value in (helper_incarnation,provider_incarnation)))
        self._configure(configuration);_require(configuration['phase']=='probe')

    def start(self):
        self.issuer.check();_require(self.process is None);self.device.guard=self.issuer.check
        self.device.verify_emulator();artifact=self.app_profile.data['artifact']
        self._verify_installed(live.HELPER,self.helper_apk,self.helper_digest,live.HELPER_VERSION)
        self._verify_installed(self.target_package,self.sample_apk,artifact['sha256'],artifact['versionCode'])
        self._spawn(self._config(self.helper_incarnation,self.host_incarnation,self.provider_incarnation))
        deadline=time.monotonic()+10
        while True:
            self.issuer.check();_require(self.process.poll() is None and time.monotonic()<deadline)
            try:status=self.read_status()
            except (OSError,http.client.HTTPException):time.sleep(.05);continue
            _require(status.get('recoveryQualificationVersion')==1 and type(status.get('recoveryQualificationVersion')) is int
                and status.get('recoveryQualificationConfigurationDigest')==self.configuration_digest
                and status.get('helperIncarnation')==self.helper_incarnation
                and status.get('providerIncarnation')==self.provider_incarnation
                and status.get('hostIncarnation')==self.host_incarnation)
            self.device.helper_process(status['nativeProcessIdentity'])
            self._startup_status=json.dumps(status);self.issuer.check();return self
