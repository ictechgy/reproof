"""Load fixed mobile inputs from an existing service registration.

No device process, fixture operation, device reservation or qualification starts
here. Fixture payloads come from the registered runtime, never this document.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass,field
from pathlib import Path

from . import contracts
from .android_artifact import verify_apk
from .android_profile import validate_android_runtime_profile
from .contracts.versions import exact
from .execution.wire import decode_json
from .live.issue_sessions import FixturePreparation
from .protected_signing_inputs import _read_bound
from .repair_android import AndroidMobileAdapterConfig,AndroidMobileTools
from .repair_android_operation import _fixture_digest
from .repair_configuration import ProtectedServiceConfiguration
from .repair_signing_configuration import _path
from .storage import MAX_APK


class ProtectedMobileInputsError(RuntimeError):
    def __init__(self):
        self.code='protected_mobile_inputs'
        super().__init__('Selected mobile inputs or service registration are invalid or changed')


def _require(value):
    if not value:raise ProtectedMobileInputsError()


def _json(reference):
    exact(reference,('path','sha256'))
    _require(_path(reference['path']).suffix=='.json')
    return decode_json(_read_bound(reference['path'],reference['sha256'],maximum=1024*1024))


def _apk(reference):
    exact(reference,('path','sha256','bytes'))
    path=_path(reference['path']);_require(path.suffix=='.apk')
    contracts.validate_digest(reference['sha256'])
    contracts.bounded_int(reference['bytes'],'APK bytes',1,MAX_APK)
    return path


def _snapshot(config, source_digest):
    value={'sourceDigest':source_digest,'scopeDigest':config.scope_digest,
        'projectDigest':config.registration.project_digest,'profileDigest':config.original_profile.digest,
        'runtimePolicyDigest':config.runtime_policy_digest,'fixturePlansDigest':_fixture_digest(config.preparations),
        'helperDigest':config.helper_digest,'owner':config.owner,'deviceId':config.device_id,
        'adbDigest':config.tools.adb_digest,'packageInspectorDigest':config.tools.package_inspector_digest}
    if config.adb_endpoint is not None:value['adbEndpointDigest']=config.adb_endpoint.definition_digest
    if config.native_guardian is not None:
        from .repair_android import NATIVE_TOOL_OWNERSHIP_VERSION
        from .live.issue_sessions import FIXTURE_RESERVATION_VERSION
        value['nativeGuardianDigest']=config.native_guardian.definition_digest
        value['nativeToolOwnershipVersion']=NATIVE_TOOL_OWNERSHIP_VERSION
        value['fixtureReservationVersion']=FIXTURE_RESERVATION_VERSION
    if config.tools.inspector_support:value['packageInspectorSupportDigest']=config.tools.inspector_support_digest
    return contracts.digest(value)


@dataclass(frozen=True,slots=True)
class LoadedAndroidMobileInputs:
    profile_id: str
    config: AndroidMobileAdapterConfig = field(repr=False)
    source_digest: str
    definition_digest: str

    def public(self):
        return {'profileId':self.profile_id,'definitionDigest':self.definition_digest,
            'scopeDigest':self.config.scope_digest,'applicationId':self.config.application_id,
            'originalBuildId':self.config.original_build_id}


@dataclass(frozen=True,slots=True)
class PreparedMobileInputs:
    configuration_digest: str
    _profiles: tuple = field(repr=False)

    def profile(self, identifier):
        for row in self._profiles:
            if row.profile_id==identifier:return row
        raise ProtectedMobileInputsError()

    def public(self):
        return {'schemaVersion':1,'kind':'protected-mobile-inputs','executionAuthority':'none',
            'configurationDigest':self.configuration_digest,'profiles':[row.public() for row in self._profiles]}

    def verify(self, configuration, issue_configuration, runtime_bundle):
        try:
            from .ios_mobile_inputs import LoadedIOSMobileInputs
            for row in self._profiles:
                row.config.validate()
                if type(row) is LoadedAndroidMobileInputs:
                    _require(_snapshot(row.config,row.source_digest)==row.definition_digest)
                else:
                    _require(type(row) is LoadedIOSMobileInputs
                        and row.config.snapshot(row.source_digest)==row.definition_digest)
            current=load_protected_mobile_inputs(configuration,issue_configuration,runtime_bundle)
            _require(current.public()==self.public())
            for row in self._profiles:
                config=current.profile(row.profile_id).config
                _require(config.lab is row.config.lab and config.service is row.config.service
                    and config.registration is row.config.registration)
        except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError,AttributeError):
            raise ProtectedMobileInputsError() from None

    def open_ios_preparation(self, identifier, configuration, issue_configuration, runtime_bundle, *, create=True):
        from .execution.journal import RunStore
        from .ios_mobile_inputs import LoadedIOSMobileInputs
        from .ios_mobile_operation import IOSMobileOperationStore
        self.verify(configuration,issue_configuration,runtime_bundle)
        selected=self.profile(identifier)
        _require(type(selected) is LoadedIOSMobileInputs and type(create) is bool)
        row=next(item for item in configuration.document['profiles'] if item['id']==identifier)
        journal=row['mobile']['journal']
        store=RunStore(journal['root'],environment_digest=journal['environmentDigest'],
                       disk_limit=journal['diskBudgetBytes'],create=create)
        return IOSMobileOperationStore(store,selected.config.definition,row['mobile']['ownerRoot'],create=create)


def _preparations(value, runtime, application_id):
    _require(type(value) is list and len(value)<=128)
    available={item.plan.fixture_id:item for item in runtime.preparations if item.plan.application_id==application_id}
    selected=[];seen=set()
    for row in value:
        exact(row,('fixtureId','payloadDigest'))
        contracts.validate_id(row['fixtureId']);contracts.validate_digest(row['payloadDigest'])
        _require(row['fixtureId'] not in seen and row['fixtureId'] in available)
        seen.add(row['fixtureId']);original=available[row['fixtureId']]
        payload=copy.deepcopy(original.payload)
        _require(contracts.digest(payload)==row['payloadDigest'])
        selected.append(FixturePreparation(original.plan,payload))
    result=tuple(selected)
    runtime.service._checked_preparations(result,runtime.registration,application_id)
    return result


def _local_device(row,lab):
    device=lab.devices[row['deviceId']]
    _require(device.get('_remoteAuthority',False) is False and device.get('kind')=='android-live'
             and device.get('_authority',{}).get('deviceKind')=='android')
    return device


def _recovery_only_provider():
    from .live.model import LiveError
    raise LiveError('recovery_only', 'This device is registered for protected recovery', 409)


def recovery_device_descriptors(configuration):
    """Bootstrap only configured metadata; never probe a default ADB transport."""
    try:
        _require(type(configuration) is ProtectedServiceConfiguration)
        devices=[]
        for row in configuration.document['profiles']:
            if row['platform'] == 'ios':
                from .ios_mobile_inputs import ios_recovery_device_descriptor
                devices.append(ios_recovery_device_descriptor(row))
                continue
            _require(row['platform']=='android')
            value=_json(row['mobile']['definition'])
            exact(value,('schemaVersion','kind','owner','serial','runtimeProfile','originalApk','helperApk',
                         'tools','preparations','adbEndpoint','nativeGuardian'))
            _require(type(value['schemaVersion']) is int and value['schemaVersion']==1
                and value['kind']=='android-mobile-definition-v1'
                and type(value['serial']) is str and 0<len(value['serial'])<=256
                and not any(character.isspace() or ord(character)<32 for character in value['serial']))
            profile=validate_android_runtime_profile(_json(value['runtimeProfile']))
            _apk(value['originalApk']);_apk(value['helperApk'])
            data=profile.data
            _require(data['projectId']==row['projectId'] and data['projectDigest']==row['projectDigest']
                and data['applicationId']==row['applicationId'] and data['buildId']==row['originalBuildId']
                and data['artifact']['sha256']==value['originalApk']['sha256']
                and data['artifact']['bytes']==value['originalApk']['bytes'])
            devices.append({'id':row['deviceId'],'name':'Android recovery','platform':'android','kind':'android-live',
                '_authority':{'deviceKind':'android','physicalId':value['serial']},'_recoveryOnly':True,
                'capabilities':{'authorityMode':'shared-v2','actions':[],'recovery':False,'recoveryOnly':True,
                    'applicationIdentity':profile.application_identity,'applicationProfile':data,
                    'applicationProfileDigest':profile.digest},'factory':_recovery_only_provider})
        return devices
    except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError):
        raise ProtectedMobileInputsError() from None


def _load_profile(row, runtime, lab):
    device=_local_device(row,lab)
    source=row['mobile']['definition'];value=_json(source)
    exact(value,('schemaVersion','kind','owner','serial','runtimeProfile','originalApk','helperApk','tools','preparations'),('adbEndpoint','nativeGuardian'))
    _require(type(value['schemaVersion']) is int and value['schemaVersion']==1
             and value['kind']=='android-mobile-definition-v1')
    contracts.validate_id(value['owner'])
    _require(device['_authority'].get('physicalId')==value['serial'])
    original=_apk(value['originalApk']);helper=_apk(value['helperApk'])
    tools=value['tools'];exact(tools,('adbPath','adbSha256','packageInspectorPath','packageInspectorSha256'))
    for name in ('adbPath','packageInspectorPath'):_path(tools[name])
    for name in ('adbSha256','packageInspectorSha256'):contracts.validate_digest(tools[name])
    profile=validate_android_runtime_profile(_json(value['runtimeProfile']))
    data=profile.data
    _require(data['projectId']==row['projectId'] and data['projectDigest']==row['projectDigest']
        and data['applicationId']==row['applicationId'] and data['buildId']==row['originalBuildId']
        and data['artifact']['sha256']==value['originalApk']['sha256']
        and data['artifact']['bytes']==value['originalApk']['bytes'])
    preparations=_preparations(value['preparations'],runtime,row['applicationId'])
    verify_apk(helper,expected_digest=value['helperApk']['sha256'],expected_bytes=value['helperApk']['bytes'])
    pinned=AndroidMobileTools(Path(tools['adbPath']),tools['adbSha256'],
        Path(tools['packageInspectorPath']),tools['packageInspectorSha256'])
    endpoint=None
    if 'adbEndpoint' in value:
        from .adb_endpoint import AdbEndpoint
        selected=value['adbEndpoint'];exact(selected,('socketPath','serverVersion','sandboxSha256'))
        endpoint=AdbEndpoint(_path(selected['socketPath']),server_version=selected['serverVersion'],
            sandbox_sha256=selected['sandboxSha256'])
    guardian=None
    if 'nativeGuardian' in value:
        from .android_native_process import AndroidGuardianTools
        selected=value['nativeGuardian'];exact(selected,('path','sha256'))
        _require(endpoint is not None)
        guardian=AndroidGuardianTools(_path(selected['path']),selected['sha256'])
    config=AndroidMobileAdapterConfig(lab=lab,service=runtime.service,registration=runtime.registration,
        device_id=row['deviceId'],owner=value['owner'],original_profile=profile,original_apk=original,
        helper_apk=helper,helper_digest=value['helperApk']['sha256'],preparations=preparations,
        serial=value['serial'],tools=pinned,runtime_policy_digest=row['runtimePolicyDigest'],
        adb_endpoint=endpoint,native_guardian=guardian)
    # Re-evaluate the live payload after file reads, not only the copied value.
    _require(_fixture_digest(_preparations(value['preparations'],runtime,row['applicationId']))
             ==_fixture_digest(config.preparations))
    return LoadedAndroidMobileInputs(row['id'],config,source['sha256'],_snapshot(config,source['sha256']))


def load_protected_mobile_inputs(configuration, issue_configuration, runtime_bundle):
    """Prepare local mobile definitions after exact metadata and assignment checks."""
    try:
        from .ios_mobile_inputs import LoadedIOSMobileInputs, load_ios_mobile_profile, local_ios_device
        _require(type(configuration) is ProtectedServiceConfiguration)
        document=configuration.document;digest=contracts.digest(document)
        _require(configuration.definition_digest==digest)
        configuration.validate_issue_configuration(issue_configuration)
        configuration.validate_runtime(runtime_bundle)
        def local(row):
            return (_local_device if row['platform']=='android' else local_ios_device)(row,runtime_bundle.workflow.lab)
        for row in document['profiles']:local(row)
        rows=tuple((_load_profile if row['platform']=='android' else load_ios_mobile_profile)(
                       row,runtime_bundle.workflow.runtimes[row['projectId']],runtime_bundle.workflow.lab)
                   for row in document['profiles'])
        queries=[item.config.query.work_root for item in rows if type(item) is LoadedIOSMobileInputs]
        protected=[]
        for row in document['profiles']:
            protected.extend(_path(row[phase]['journal']['root']) for phase in ('build','signing','mobile'))
            protected.extend(_path(row[phase]['ownerRoot']) for phase in ('signing','mobile'))
            build_key='hostPath' if row['build']['route']['executionClass']=='host-build' else 'bundlePath'
            protected.extend((_path(row['build'][build_key]),_path(row['signing']['toolsPath'])))
            protected.extend(_path(row[phase]['definition']['path']) for phase in ('signing','mobile'))
            protected.append(_path(row['validation']['observers']['path']))
        for row in rows:
            if type(row) is LoadedIOSMobileInputs:protected.extend(row.input_paths)
        _require(all(left!=right and left not in right.parents and right not in left.parents
            for index,left in enumerate(queries) for right in (*queries[index+1:],*protected)))
        configuration.validate_issue_configuration(issue_configuration)
        configuration.validate_runtime(runtime_bundle)
        for row in document['profiles']:local(row)
        _require(configuration.document==document and configuration.definition_digest==digest)
        return PreparedMobileInputs(digest,rows)
    except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError,AttributeError):
        raise ProtectedMobileInputsError() from None


__all__=['ProtectedMobileInputsError','LoadedAndroidMobileInputs','PreparedMobileInputs',
         'load_protected_mobile_inputs','recovery_device_descriptors']
