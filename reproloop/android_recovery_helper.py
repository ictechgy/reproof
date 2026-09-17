"""Fresh authenticated helper observation under retained recovery ownership."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
import secrets
import shlex
import time
import uuid

from . import contracts
from .adb_endpoint import ScopedAdbClient,AdbEndpointError
from .android_native_process import _bounds,run_native_adb
from .android_recovery import AndroidRecoveryDispatch,_GrantCancellation,recover_android_resources,validate_record
from .live.android_live import HELPER
from .live.authority import NATIVE_PROTOCOL_VERSION,HELPER_VERSION,QUALIFIED_NATIVE_CLOCKS
from .repair_android_operation import _require,_replace_at


@dataclass(frozen=True,slots=True)
class AndroidHelperRecoveryObservation:
    context_digest: str
    evidence_digest: str
    helper_incarnation: str
    fresh_helper_verified: bool
    helper_collected: bool
    ownership_released: bool = False
    needs_recovery_apks: bool = False


class _RecoveryHelperProbe:
    def __init__(self,session,token):
        self.session=session;self.token=token;self.verified=False

    def require_binding(self,operations,token,client):
        _require(self.session.operations is operations and token is self.token and client is self.session.client
            and token.command==('shell','am instrument -w -r '+HELPER+'/.LiveInstrumentation')
            and token.slot=='instrumentation','android_recovery_helper_probe')
        operations.require_native_descriptors(token)

    def run(self,process):
        session=self.session
        try:
            while True:
                session.check()
                _require(process.poll() is None,'android_recovery_helper_exited')
                session.step('status-helper')
                try:
                    status=session.client.call_helper('/status',None,token=session.config['token'],
                        timeout=min(1,max(.001,session.deadline-time.monotonic())),
                        cancellation=session.cancellation,deadline_monotonic=session.deadline)
                except AdbEndpointError:
                    session.check();time.sleep(.02);continue
                expected={'protocolVersion':NATIVE_PROTOCOL_VERSION,'helperVersion':HELPER_VERSION,
                    'helperIncarnation':session.config['helperIncarnation'],'hostIncarnation':session.config['hostIncarnation'],
                    'providerIncarnation':session.config['providerIncarnation'],
                    'profileDigest':session.owner.config.original_profile.digest,
                    'nativeDigest':session.owner.config.original_profile.native_digest,
                    'targetPackage':session.owner.config.package,'generalProfile':True}
                _require(type(status) is dict and status.get('ready') is True and status.get('stopped') is False
                    and all(type(status.get(key)) is type(value) and status[key]==value for key,value in expected.items())
                    and status.get('activePointerIds')==[]
                    and type(status.get('nativeTimeMs')) is int and 0<=status['nativeTimeMs']<2**53
                    and ('android',status.get('nativeClockId')) in QUALIFIED_NATIVE_CLOCKS,
                    'android_recovery_helper_identity')
                contracts.validate_id(status['nativeIncarnation'])
                session.helper['statusDigest']=contracts.digest(status);session.save()
                self.verified=True;break
        except (OSError,RuntimeError,ValueError,TypeError,contracts.ContractError):
            self.verified=False
        finally:
            stopped=session.sdk('stop-helper',('shell','am force-stop '+HELPER))
            session.helper['stopDigest']=hashlib.sha256(stopped).hexdigest();session.save()


class _HelperSession:
    def __init__(self,operations,recovery,record,cancellation,deadline):
        self.operations=operations;self.owner=operations;self.recovery=recovery;self.record=record
        self.cancellation=_GrantCancellation(cancellation,recovery);self.deadline=deadline
        self.helper=record['helperRecovery'];self.config={};self.client=None

    def check(self):
        self.operations.require_recovery_descriptors(self.recovery);_bounds(self.cancellation,self.deadline)

    def save(self):
        _replace_at(self.recovery.operation_directory_fd,'recovery.json',self.record)

    def step(self,name):
        self.check();self.helper['state']='running';self.helper['activeStep']=name;self.save()

    def sdk(self,name,command,*,input_bytes=b'',probe=False):
        self.step(name)
        token=AndroidRecoveryDispatch(self.recovery,command,None,hashlib.sha256(input_bytes).hexdigest(),
                                     'instrumentation' if probe else 'command')
        with self.operations._mutex:self.operations._recovery_dispatches[id(token)]=token
        selected=_RecoveryHelperProbe(self,token) if probe else None
        try:
            result=run_native_adb(self.operations,token,self.operations.config.native_guardian,self.client,command,
                cancellation=self.cancellation,deadline_monotonic=self.deadline,input_bytes=input_bytes,
                slot=token.slot,_recovery_probe=selected)
            empty_missing=(name=='helper-path' and result.returncode==1
                           and not result.stdout.strip() and not result.stderr.strip())
            _require(result.terminated and result.bounded and not result.interrupted
                and (result.returncode==0 or empty_missing),
                     'android_recovery_helper_tool')
            if selected is not None:return selected.verified
            return result.stdout
        finally:
            with self.operations._mutex:self.operations._recovery_dispatches.pop(id(token),None)


def recover_android_helper(operations,recovery,*,cancellation,deadline_monotonic):
    from .android_recovery_materials import prepare_recovery_materials
    operations.require_recovery_descriptors(recovery)
    state=operations._state(recovery.operation_id,recovery.operation_directory_fd)
    if operations._stage_status(recovery.operation_directory_fd,recovery._files.intent,state,full=True)=='recovery-copy-incomplete':
        prepare_recovery_materials(operations,recovery,cancellation=cancellation,deadline_monotonic=deadline_monotonic)
    observation=_recover_helper_once(operations,recovery,cancellation=cancellation,deadline_monotonic=deadline_monotonic)
    if observation.needs_recovery_apks:
        prepare_recovery_materials(operations,recovery,cancellation=cancellation,deadline_monotonic=deadline_monotonic)
        observation=_recover_helper_once(operations,recovery,cancellation=cancellation,deadline_monotonic=deadline_monotonic)
    return observation


def _recover_helper_once(operations,recovery,*,cancellation,deadline_monotonic):
    resources=recover_android_resources(operations,recovery,cancellation=cancellation,deadline_monotonic=deadline_monotonic)
    needs_apks=resources.needs_original_apk
    session=None;verified=collected=False;helper_id='';evidence={'resources':resources.evidence_digest,'helper':'unconfirmed'}
    try:
        _require(resources.device_recovered and resources.fixtures_clean,'android_recovery_resources_unconfirmed')
        operations.require_recovery_descriptors(recovery);_bounds(cancellation,deadline_monotonic)
        config=operations.config
        record=validate_record(recovery.operation_directory_fd,recovery._files.intent,recovery._files.native)
        helper_id='helper_recovery_'+uuid.uuid4().hex
        _require(helper_id!=recovery.prior_helper_incarnation,'android_recovery_helper_identity')
        record['helperRecovery']={'state':'prepared','helperIncarnation':helper_id,
            'hostIncarnation':recovery._device._authority.host_incarnation,
            'providerIncarnation':'provider_recovery_'+uuid.uuid4().hex,'activeStep':None,
            'statusDigest':None,'stopDigest':None,'helperCollected':False}
        session=_HelperSession(operations,recovery,record,cancellation,deadline_monotonic);session.save()
        work=operations._root(recovery.operation_id)/'staging'
        session.client=ScopedAdbClient(config.tools.adb,config.tools.adb_digest,config.adb_endpoint,
            serial=config.serial,work_root=work,sandbox_sha256=config.adb_endpoint.sandbox_sha256)
        use_copies=record.get('recoveryMode','staged-apks') in ('staged-apks','recovery-apks')
        if use_copies:
            installed=session.sdk('install-helper',('install','-r','-t',str(work/'helper.apk')))
            _require(installed.strip().splitlines() in ([b'Success'],[b'Performing Streamed Install',b'Success']),
                     'android_recovery_helper_install')
        paths=session.sdk('helper-path',('shell','pm path '+HELPER)).decode().splitlines()
        needs_apks=not use_copies and not paths
        _require(len(paths)==1 and re.fullmatch(r'package:/data/app/[A-Za-z0-9_./=+~-]+/base[.]apk',paths[0]),
                 'android_recovery_helper_path')
        path=paths[0][8:]
        _require(str(PurePosixPath(path))==path and '..' not in PurePosixPath(path).parts,'android_recovery_helper_path')
        digest=session.sdk('helper-hash',('shell','sha256sum '+path)).decode('ascii').split()
        _require(len(digest)==2 and re.fullmatch(r'[0-9a-f]{64}',digest[0]) and digest[1]==path,
                 'android_recovery_helper_hash')
        needs_apks=not use_copies and digest[0]!=config.helper_digest
        _require(digest[0]==config.helper_digest,'android_recovery_helper_hash')
        session.config={'token':secrets.token_urlsafe(32),'port':8766,'targetPackage':config.package,
            'maxFps':1,'maxWidth':32,'recordSdk':False,'protocolVersion':NATIVE_PROTOCOL_VERSION,
            'helperVersion':HELPER_VERSION,'helperIncarnation':helper_id,
            'hostIncarnation':session.helper['hostIncarnation'],'providerIncarnation':session.helper['providerIncarnation'],
            'appProfile':config.original_profile.native(),'profileDigest':config.original_profile.digest}
        body=json.dumps(session.config,allow_nan=False).encode()
        _require(len(body)<=16*1024,'android_recovery_helper_configuration')
        command=shlex.join(['run-as',HELPER,'sh','-c','umask 077 && mkdir -p files && cat > files/live-config.json'])
        session.sdk('write-config',('shell','-T',command),input_bytes=body)
        verified=session.sdk('start-helper',('shell','am instrument -w -r '+HELPER+'/.LiveInstrumentation'),probe=True)
        cleared=session.sdk('clear-helper',('shell','pm clear '+HELPER))
        _require(cleared.strip()==b'Success','android_recovery_helper_clear')
        lines=session.sdk('confirm-helper-exit',('shell','ps -A -o NAME')).decode().splitlines()
        _require(lines and lines[0].strip()=='NAME' and not any(name.strip()==HELPER or name.strip().startswith(HELPER+':')
            for name in lines[1:]),'android_recovery_helper_live')
        collected=True;session.check()
        session.helper.update(state='verified' if verified else 'failed',activeStep=None,helperCollected=True)
        session.save();evidence={'recordDigest':contracts.digest(record),'ownershipReleased':False}
    except (OSError,RuntimeError,ValueError,TypeError,KeyError,contracts.ContractError):
        verified=False
        if session is not None:
            session.helper.update(state='failed',activeStep=None,helperCollected=collected);session.save()
            evidence={'recordDigest':contracts.digest(session.record),'ownershipReleased':False}
    finally:
        if session is not None:
            session.config.clear()
            if session.client is not None:session.client.close(deadline_monotonic=deadline_monotonic)
    return AndroidHelperRecoveryObservation(getattr(recovery,'context_digest',''),contracts.digest(evidence),
                                            helper_id,verified,collected,needs_recovery_apks=needs_apks)
