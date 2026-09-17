"""Fixed native Android recovery; device observations do not release ownership."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import time

from . import contracts
from .adb_endpoint import ScopedAdbClient
from .android_native_calls import _LIMITS, _SLOTS, _state, validate_workspace
from .android_native_process import _bounds, run_native_adb, run_native_inspector
from .live.android_live import HELPER
from .repair_android import NATIVE_TOOL_OWNERSHIP_VERSION
from .repair_android_operation import (
    AndroidOperationError, _require, _open_child_directory, _open_regular_at,
    _read_fd, _read_json_at, _replace_at, _write_new_at,
)

STEPS=('stop-helper','stop-target','confirm-stopped','inspect-original','install-original',
       'installed-path','installed-hash','clear-original','confirm-cleared')
INSTALLED_STEPS=('stop-helper','stop-target','confirm-stopped','installed-path','installed-hash',
                 'clear-original','confirm-cleared')
DISCARD_STATES=('discard-intent-uncommitted','staged-discard-incomplete',
                'discard-state-uncommitted','staged-discarded')


@dataclass(frozen=True,slots=True)
class _RecoveryPhase:
    phase: str = 'cleanup'
    replay_number: object = None


@dataclass(frozen=True,slots=True)
class AndroidRecoveryDispatch:
    recovery: object
    command: tuple | None
    apk: Path | None
    input_digest: str = hashlib.sha256(b'').hexdigest()
    slot: str = 'command'
    _phase: _RecoveryPhase = _RecoveryPhase()

    def __getattr__(self,name):
        aliases={'ownership_generation':'prior_generation','host_incarnation':'prior_host_incarnation',
                 'helper_incarnation':'prior_helper_incarnation'}
        forwarded={'operation_id','request_digest','context_digest','scope_digest','configuration_digest',
            'binding_digest','producer_fd','operation_directory_fd','device_fd','device_directory_fd','device_lock_name'}
        if name in aliases:return getattr(self.recovery,aliases[name])
        if name in forwarded:return getattr(self.recovery,name)
        raise AttributeError(name)


@dataclass(frozen=True,slots=True)
class AndroidDeviceRecoveryObservation:
    context_digest: str
    evidence_digest: str
    processes_stopped: bool
    original_restored: bool
    data_cleared: bool
    ownership_released: bool = False
    needs_original_apk: bool = False


@dataclass(frozen=True,slots=True)
class AndroidResourceRecoveryObservation:
    context_digest: str
    evidence_digest: str
    device_recovered: bool
    fixtures_clean: bool
    ownership_released: bool = False
    needs_original_apk: bool = False


def require_dispatch(operations,token):
    with operations._mutex:
        _require(type(token) is AndroidRecoveryDispatch
            and operations._recovery_dispatches.get(id(token)) is token,'android_recovery_dispatch')
    recovery=operations.require_recovery_descriptors(token.recovery)
    _require(recovery._files.intent['configuration'].get('nativeToolOwnershipVersion')==NATIVE_TOOL_OWNERSHIP_VERSION
        and operations.config.native_guardian is not None,'android_recovery_contract')
    return token


def require_command(operations,token,*,command=None,apk=None,input_bytes=b'',slot='command'):
    if type(token) is not AndroidRecoveryDispatch:return
    require_dispatch(operations,token)
    _require(token.input_digest==hashlib.sha256(input_bytes).hexdigest() and token.slot==slot,
             'android_recovery_command')
    _require((command is not None and token.command==command and token.apk is None)
        or (apk is not None and token.apk==apk and token.command is None),'android_recovery_command')
    if command is not None and command[0]=='install':
        from .android_recovery_materials import expected_apk
        from .repair_android_operation import _file_digest
        path=Path(command[-1]);directory=token.operation_directory_fd
        _require(command[:3]==('install','-r','-t') and len(command)==4
            and path.parent==operations._root(token.operation_id)/'staging'
            and path.name in ('original.apk','helper.apk'),'android_recovery_command')
        intent=operations._intent(token.operation_id,directory)
        expected=expected_apk(directory,intent,operations._state(token.operation_id,directory),path.name)
        staging=_open_child_directory(directory,'staging',expected=intent['stagingIdentity'])
        opened=None
        try:
            opened=_open_regular_at(staging,path.name,expected=expected['identity'])
            _require(_file_digest(opened,expected['bytes'])==(expected['digest'],expected['bytes']),
                     'android_recovery_install_input')
        finally:
            if opened is not None:os.close(opened)
            os.close(staging)


def validate_record(directory,intent,native):
    if 'recovery.json' not in os.listdir(directory):return None
    value=_read_json_at(directory,'recovery.json',16*1024)
    expected={'schemaVersion','operationId','requestDigest',
        'contextDigest','configurationDigest','bindingDigest','attempt','state','activeStep','steps',
        'previousNativeDigest','historyDigest'}
    if type(value) is dict and 'fixtureRecovery' in value:expected.add('fixtureRecovery')
    if type(value) is dict and 'helperRecovery' in value:expected.add('helperRecovery')
    if type(value) is dict and value.get('schemaVersion') in (2,3):expected.add('recoveryMode')
    if type(value) is dict and value.get('schemaVersion')==3:expected.add('recoveryMaterialsDigest')
    mode=value.get('recoveryMode','staged-apks') if type(value) is dict else None
    steps=INSTALLED_STEPS if mode=='installed-original' else STEPS
    _require(type(value) is dict and set(value)==expected and type(value['schemaVersion']) is int
        and value['schemaVersion'] in (1,2,3) and mode in ('staged-apks','installed-original','recovery-apks')
        and (mode=='recovery-apks')==(value['schemaVersion']==3)
        and native is not None and value['bindingDigest']==native['bindingDigest']
        and all(value[key]==intent[key] for key in ('operationId','requestDigest','contextDigest','configurationDigest'))
        and type(value['attempt']) is int and 1<=value['attempt']<=32
        and value['state'] in ('prepared','running','device-restored','failed')
        and (value['activeStep'] is None or value['activeStep'] in steps)
        and type(value['steps']) is dict and set(value['steps'])<=set(steps),'android_recovery_record')
    try:
        if mode=='recovery-apks':
            from .android_recovery_materials import validate_materials
            material=validate_materials(directory,intent,{'nativeBindingDigest':native['bindingDigest']},full=False)
            _require(material is not None and material['preparedDigest'] is not None
                and value['recoveryMaterialsDigest']==material['preparedDigest'],'android_recovery_record')
        for key in ('previousNativeDigest','historyDigest'):contracts.validate_digest(value[key])
        for name,row in value['steps'].items():
            _require(type(row) is dict and set(row)=={'state','evidenceDigest'}
                and row['state'] in ('completed','failed'),'android_recovery_record')
            contracts.validate_digest(row['evidenceDigest'])
        if value['state']=='device-restored':
            _require(value['activeStep'] is None and set(value['steps'])==set(steps)
                and all(row['state']=='completed' for row in value['steps'].values()),'android_recovery_record')
        if 'fixtureRecovery' in value:
            fixture=value['fixtureRecovery']
            _require(type(fixture) is dict and set(fixture)=={'state','total','completed','selectionDigest','historyDigest','active'}
                and fixture['state'] in ('prepared','running','complete','failed')
                and type(fixture['total']) is int and 0<=fixture['total']<=16384
                and type(fixture['completed']) is int and 0<=fixture['completed']<=fixture['total'],
                'android_recovery_record')
            for key in ('selectionDigest','historyDigest'):contracts.validate_digest(fixture[key])
            active=fixture['active']
            _require(active is None or (type(active) is dict and set(active)=={'issueId','fixtureId','allocationId','generation'}
                and type(active['generation']) is int and active['generation']>0),'android_recovery_record')
            if active is not None:
                for key in ('issueId','fixtureId','allocationId'):contracts.validate_id(active[key])
            if fixture['state']=='complete':
                _require(active is None and fixture['completed']==fixture['total'],'android_recovery_record')
        if 'helperRecovery' in value:
            helper=value['helperRecovery']
            _require(type(helper) is dict and set(helper)=={'state','helperIncarnation','hostIncarnation','providerIncarnation',
                'activeStep','statusDigest','stopDigest','helperCollected'}
                and helper['state'] in ('prepared','running','verified','failed')
                and helper['activeStep'] in (None,'install-helper','helper-path','helper-hash','write-config',
                    'start-helper','status-helper','stop-helper','clear-helper','confirm-helper-exit')
                and type(helper['helperCollected']) is bool,'android_recovery_record')
            for name in ('helperIncarnation','hostIncarnation','providerIncarnation'):contracts.validate_id(helper[name])
            for name in ('statusDigest','stopDigest'):
                if helper[name] is not None:contracts.validate_digest(helper[name])
            if helper['state']=='verified':
                _require(helper['activeStep'] is None and helper['helperCollected']
                    and helper['statusDigest'] is not None and helper['stopDigest'] is not None,'android_recovery_record')
    except (TypeError,ValueError,contracts.ContractError):
        raise AndroidOperationError('android_recovery_record') from None
    return value


class _GrantCancellation:
    def __init__(self,parent,recovery):self.parent,self.recovery=parent,recovery
    def is_set(self):
        if self.parent.is_set():return True
        try:self.recovery._device._authority._require_parent_grant(self.recovery._lease._grant)
        except contracts.ContractError:return True
        return False


def _retire_host_slots(operations,recovery,directory,state):
    """Original locks and the current contract cover host tools, not device effects."""
    operations.require_recovery_descriptors(recovery)
    with operations._mutex:
        _require(not operations._native_controls and not operations._native_dispatch_threads
            and not operations._recovery_dispatches,'android_recovery_host_busy')
        intent=recovery._files.intent
        for name in _SLOTS:
            record=state['slots'][name]
            if record is None:continue
            if record['state']!='preparing':record['state']='retiring'
            _replace_at(directory,'state.json',state)
            slot=_open_child_directory(directory,name,expected=intent['nativeCalls']['slots'][name])
            try:
                digests={}
                for filename in sorted(os.listdir(slot)):
                    descriptor=_open_regular_at(slot,filename,expected=record['files'].get(filename))
                    try:digests[filename]=hashlib.sha256(_read_fd(descriptor,_LIMITS[filename],allow_empty=True)).hexdigest()
                    finally:os.close(descriptor)
                # The original command records remain represented in a bounded
                # history digest; no native exit JSON is promoted to cleanup.
                state['historyDigest']=contracts.digest({'previous':state['historyDigest'],
                    'retiredForRecovery':record,'files':digests,'deviceCleanupConfirmed':False})
                for filename in sorted(digests):os.unlink(filename,dir_fd=slot)
                os.fsync(slot)
            finally:os.close(slot)
            state['slots'][name]=None;_replace_at(directory,'state.json',state)


def _stopped(output,package):
    lines=output.decode('utf-8').splitlines()
    _require(lines and lines[0].strip()=='NAME','android_recovery_process_snapshot')
    _require(not any(name.strip()==target or name.strip().startswith(target+':')
        for name in lines[1:] for target in (package,HELPER)),'android_recovery_process_live')


def recover_android_device(operations,recovery,*,cancellation,deadline_monotonic):
    """Run the fixed stop/restore/sanitize sequence; fixtures and release remain separate."""
    processes=restored=cleared=needs_original=False
    record=None;client=None
    evidence={'contextDigest':getattr(recovery,'context_digest',''),'deviceRecovery':'unconfirmed'}
    try:
        operations.require_recovery_descriptors(recovery)
        cancellation=_GrantCancellation(cancellation,recovery)
        _bounds(cancellation,deadline_monotonic)
        config=operations.config;intent=recovery._files.intent
        _require(intent['configuration'].get('nativeToolOwnershipVersion')==NATIVE_TOOL_OWNERSHIP_VERSION
            and config.native_guardian is not None and config.adb_endpoint is not None,'android_recovery_contract')
        config.tools.verify();config.native_guardian.verify()
        stage=operations._state(recovery.operation_id,recovery.operation_directory_fd)
        stage_status=operations._stage_status(recovery.operation_directory_fd,intent,stage,full=True)
        _require(stage_status in ('prepared','recovery-apks-prepared') or stage_status in DISCARD_STATES,
                 'android_recovery_staging')
        mode=('staged-apks' if stage_status=='prepared' else
              'recovery-apks' if stage_status=='recovery-apks-prepared' else 'installed-original')
        validate_workspace(recovery.operation_directory_fd,intent)
        prior=validate_record(recovery.operation_directory_fd,intent,recovery._files.native)
        native=_open_child_directory(recovery.operation_directory_fd,'native-calls',expected=intent['nativeCalls']['identity'])
        try:
            state=_state(native,intent)
            attempt=1 if prior is None else prior['attempt']+1
            _require(attempt<=32,'android_recovery_attempt_limit')
            record={'schemaVersion':2,'recoveryMode':mode,'operationId':recovery.operation_id,'requestDigest':recovery.request_digest,
                'contextDigest':recovery.context_digest,'configurationDigest':recovery.configuration_digest,
                'bindingDigest':recovery.binding_digest,'attempt':attempt,
                'state':'prepared','activeStep':None,'steps':{},'previousNativeDigest':contracts.digest(state),
                'historyDigest':'0'*64 if prior is None else contracts.digest(prior)}
            if mode=='recovery-apks':
                from .android_recovery_materials import validate_materials
                material=validate_materials(recovery.operation_directory_fd,intent,stage,full=False)
                record.update(schemaVersion=3,recoveryMaterialsDigest=material['preparedDigest'])
            writer=_write_new_at if prior is None else _replace_at
            writer(recovery.operation_directory_fd,'recovery.json',record)
            _retire_host_slots(operations,recovery,native,state)
        finally:os.close(native)
        work=operations._root(recovery.operation_id)/'staging'
        client=ScopedAdbClient(config.tools.adb,config.tools.adb_digest,config.adb_endpoint,
            serial=config.serial,work_root=work,sandbox_sha256=config.adb_endpoint.sandbox_sha256)

        def execute(name,command=None,apk=None):
            operations.require_recovery_descriptors(recovery);_bounds(cancellation,deadline_monotonic)
            record['state']='running';record['activeStep']=name
            _replace_at(recovery.operation_directory_fd,'recovery.json',record)
            token=AndroidRecoveryDispatch(recovery,command,apk)
            with operations._mutex:operations._recovery_dispatches[id(token)]=token
            try:
                result=(run_native_inspector(operations,token,config.native_guardian,apk,
                            cancellation=cancellation,deadline_monotonic=deadline_monotonic)
                        if apk is not None else run_native_adb(operations,token,config.native_guardian,client,command,
                            cancellation=cancellation,deadline_monotonic=deadline_monotonic))
                empty_missing=(name=='installed-path' and result.returncode==1
                               and not result.stdout.strip() and not result.stderr.strip())
                _require(result.terminated and result.bounded and not result.interrupted
                    and (result.returncode==0 or empty_missing),
                         'android_recovery_tool_failed')
                return result.stdout
            finally:
                with operations._mutex:operations._recovery_dispatches.pop(id(token),None)

        def complete(name,output):
            record['steps'][name]={'state':'completed','evidenceDigest':hashlib.sha256(output).hexdigest()}
            record['activeStep']=None;_replace_at(recovery.operation_directory_fd,'recovery.json',record)

        for name,target in (('stop-helper',HELPER),('stop-target',config.package)):
            output=execute(name,('shell','am force-stop '+target));complete(name,output)
        output=execute('confirm-stopped',('shell','ps -A -o NAME'));_stopped(output,config.package)
        processes=True;complete('confirm-stopped',output)
        if mode in ('staged-apks','recovery-apks'):
            output=execute('inspect-original',apk=work/'original.apk')
            identity=re.findall(rb"^package: name='([^']+)' versionCode='([0-9]+)'[^\r\n]*$",output,re.M)
            _require(len(identity)==1 and identity[0][0].decode('ascii')==config.package
                and int(identity[0][1])==config.original_profile.data['artifact']['versionCode'],'android_recovery_apk_identity')
            complete('inspect-original',output)
            output=execute('install-original',('install','-r','-t',str(work/'original.apk')))
            _require(output.strip().splitlines() in ([b'Success'],[b'Performing Streamed Install',b'Success']),
                     'android_recovery_install');complete('install-original',output)
        processes=False
        output=execute('installed-path',('shell','pm path '+config.package))
        lines=output.decode('utf-8').splitlines()
        needs_original=not lines and mode=='installed-original'
        _require(len(lines)==1 and re.fullmatch(r'package:/data/app/[A-Za-z0-9_./=+~-]+/base[.]apk',lines[0]),
                 'android_recovery_installed_path')
        installed=lines[0][8:]
        _require(str(PurePosixPath(installed))==installed and '..' not in PurePosixPath(installed).parts,
                 'android_recovery_installed_path')
        complete('installed-path',output)
        output=execute('installed-hash',('shell','sha256sum '+installed))
        fields=output.decode('ascii').split()
        _require(len(fields)==2 and re.fullmatch(r'[0-9a-f]{64}',fields[0]) and fields[1]==installed,
                 'android_recovery_installed_hash')
        needs_original=mode=='installed-original' and fields[0]!=intent['files']['original.apk']['digest']
        _require(fields[0]==intent['files']['original.apk']['digest'],
                 'android_recovery_installed_hash')
        restored=True;complete('installed-hash',output)
        output=execute('clear-original',('shell','pm clear '+config.package))
        _require(output.strip()==b'Success','android_recovery_clear');cleared=True;complete('clear-original',output)
        output=execute('confirm-cleared',('shell','ps -A -o NAME'));_stopped(output,config.package)
        complete('confirm-cleared',output)
        operations.require_recovery_descriptors(recovery);_bounds(cancellation,deadline_monotonic)
        processes=True
        record['state']='device-restored';record['activeStep']=None
        _replace_at(recovery.operation_directory_fd,'recovery.json',record)
        evidence={'recordDigest':contracts.digest(record),'deviceCleanupConfirmed':False}
    except (OSError,RuntimeError,ValueError,TypeError,contracts.ContractError):
        processes=False
        if record is not None:
            record['state']='failed'
            if record['activeStep'] is not None:
                record['steps'][record['activeStep']]={'state':'failed','evidenceDigest':contracts.digest(evidence)}
            record['activeStep']=None
            _replace_at(recovery.operation_directory_fd,'recovery.json',record)
            evidence={'recordDigest':contracts.digest(record),'deviceCleanupConfirmed':False}
    finally:
        if client is not None:client.close(deadline_monotonic=deadline_monotonic)
    return AndroidDeviceRecoveryObservation(getattr(recovery,'context_digest',''),contracts.digest(evidence),
        processes,restored,cleared,needs_original_apk=needs_original)


def recover_android_resources(operations,recovery,*,cancellation,deadline_monotonic):
    """Recover the device, then the original fixtures; final ownership stays quarantined."""
    from .live.issue_sessions import _operation, fixture_reservation_id, FIXTURE_RESERVATION_VERSION
    device=recover_android_device(operations,recovery,cancellation=cancellation,deadline_monotonic=deadline_monotonic)
    ready=device.processes_stopped and device.original_restored and device.data_cleared
    clean=False;record=None;fixture=None
    evidence={'device':device.evidence_digest,'fixtures':'unconfirmed'}
    try:
        _require(ready,'android_recovery_device_unconfirmed')
        operations.require_recovery_descriptors(recovery)
        cancellation=_GrantCancellation(cancellation,recovery)
        _bounds(cancellation,deadline_monotonic)
        config=operations.config;service=config.service;coordinator=service.fixtures
        state=operations._state(recovery.operation_id,recovery.operation_directory_fd)
        selections=[];planned_allocations=set()
        plans={item.plan.fixture_id:item for item in config.preparations}
        build='candidate_'+contracts.digest({'operation':recovery.operation_id,'request':recovery.request_digest})[:32]
        for phase in sorted(state['phases']):
            if not phase.startswith('replay-'):continue
            number=int(phase.split('-')[1])
            issue_id='mobile_'+contracts.digest({'context':recovery.context_digest,'attempt':number})[:40]
            with service._lock:
                _require(issue_id not in service._active,'android_recovery_issue_active')
                issue=service.get(issue_id)
            _require(issue['issueId']==issue_id and issue['projectDigest']==config.registration.project_digest
                and issue['deviceId']==config.device_id and issue['applicationId']==config.application_id
                and issue['buildId']==build and issue['state'] in ('complete','failed','quarantined')
                and type(issue['fixtures']) is list and len(issue['fixtures'])<=len(plans),
                'android_recovery_fixture_binding')
            planned=issue.get('fixtureReservations')
            if planned is not None:
                _require(recovery._files.intent['configuration'].get('fixtureReservationVersion')==FIXTURE_RESERVATION_VERSION
                    and type(issue.get('fixtureReservationVersion')) is int
                    and issue['fixtureReservationVersion']==FIXTURE_RESERVATION_VERSION
                    and type(planned) is list and len(planned)==len(plans),'android_recovery_fixture_binding')
                expected=[{'fixtureId':name,'allocationId':fixture_reservation_id(issue_id,name)} for name in plans]
                _require(planned==expected,'android_recovery_fixture_binding')
                planned_allocations.update(entry['allocationId'] for entry in planned)
            seen=set()
            for entry in issue['fixtures']:
                _require(type(entry) is dict and entry.get('fixtureId') in plans
                    and entry['fixtureId'] not in seen and type(entry.get('generation')) is int
                    and entry['generation']>0,'android_recovery_fixture_binding')
                contracts.validate_id(entry['allocationId']);seen.add(entry['fixtureId'])
                if planned is not None:
                    _require(entry['allocationId']==fixture_reservation_id(issue_id,entry['fixtureId']),
                             'android_recovery_fixture_binding')
                selections.append({'issueId':issue_id,'fixtureId':entry['fixtureId'],
                    'allocationId':entry['allocationId'],'generation':entry['generation']})
            for name in plans:
                if name in seen:continue
                _require(planned is not None,'android_recovery_fixture_binding')
                allocation_id=fixture_reservation_id(issue_id,name)
                found=coordinator.lookup_recovery_allocation(plans[name].plan,allocation_id=allocation_id,
                    owner=config.owner,device_id=config.device_id)
                selections.append({'issueId':issue_id,'fixtureId':name,'allocationId':allocation_id,
                    'generation':0 if found is None else found['generation']})
        record=validate_record(recovery.operation_directory_fd,recovery._files.intent,recovery._files.native)
        _require(record is not None and record['state']=='device-restored','android_recovery_device_unconfirmed')
        fixture={'state':'prepared','total':len(selections),'completed':0,
            'selectionDigest':contracts.digest(selections),'historyDigest':'0'*64,'active':None}
        record['fixtureRecovery']=fixture;_replace_at(recovery.operation_directory_fd,'recovery.json',record)
        def authorize(_kind):
            operations.require_recovery_descriptors(recovery);_bounds(cancellation,deadline_monotonic)
            return True
        for selection in selections:
            authorize('fixture_recovery')
            fixture['state']='running';fixture['active']=selection if selection['generation'] else None
            _replace_at(recovery.operation_directory_fd,'recovery.json',record)
            preparation=plans[selection['fixtureId']]
            if selection['generation']==0:
                _require(selection['allocationId'] in planned_allocations,'android_recovery_fixture_binding')
                result=coordinator.seal_unstarted_reservation(preparation.plan,allocation_id=selection['allocationId'],
                    owner=config.owner,device_id=config.device_id)
            else:
                result=coordinator.recover_cleanup(preparation.plan,allocation_id=selection['allocationId'],
                    generation=selection['generation'],owner=config.owner,device_id=config.device_id,
                    prepare_operation_id=_operation(selection['issueId'],selection['fixtureId'],'prepare'),
                    payload_digest=coordinator.payload_digest(preparation.payload),
                    cleanup_operation_id=_operation(selection['issueId'],selection['fixtureId'],'native-recovery-cleanup',
                        str(selection['generation']),str(record['attempt'])),
                    timeout_seconds=min(60,max(.001,deadline_monotonic-time.monotonic())),effect_authorizer=authorize,
                    allow_unstarted=selection['allocationId'] in planned_allocations)
            _require(result['status']=='complete','android_recovery_fixture_unconfirmed')
            fixture['completed']+=1;fixture['active']=None
            fixture['historyDigest']=contracts.digest({'previous':fixture['historyDigest'],'result':result})
            _replace_at(recovery.operation_directory_fd,'recovery.json',record)
        authorize('fixture_recovery_complete')
        fixture['state']='complete';fixture['active']=None
        _replace_at(recovery.operation_directory_fd,'recovery.json',record)
        clean=True;evidence={'recordDigest':contracts.digest(record),'ownershipReleased':False}
    except (OSError,RuntimeError,ValueError,TypeError,KeyError,contracts.ContractError):
        if record is not None and fixture is not None:
            fixture['state']='failed';fixture['active']=None
            _replace_at(recovery.operation_directory_fd,'recovery.json',record)
            evidence={'recordDigest':contracts.digest(record),'ownershipReleased':False}
    return AndroidResourceRecoveryObservation(getattr(recovery,'context_digest',''),contracts.digest(evidence),ready,clean,
                                              needs_original_apk=device.needs_original_apk)
