"""Inert registered iOS mobile inputs; no tool, device or extraction starts here."""
from dataclasses import dataclass, field
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import plistlib
import stat
import zipfile

from . import contracts
from .contracts.versions import exact
from .execution.artifacts import BlobSet, open_regular
from .execution.wire import MAX_TRANSFER_BYTES
from .ios_artifact_transfer import (MAX_APP_ENTRIES, MAX_EXPANDED_APP_BYTES, MAX_PLIST_BYTES,
    _check_component, _file_stat_signature, _validated_ipa_members, _zip_preflight)
from .ios_device_tools import IOSDeviceQueryDefinition, IOSDeviceTools
from .ios_mobile_operation import IOSMobileDefinition, _bundle
from .ios_profile import IosAppProfile, validate_ios_profile
from .live.issue_sessions import IssueSessionService
from .live.model import Lab
from .live.recording_session import TrustedProjectRegistration
from .protected_mobile_inputs import ProtectedMobileInputsError, _json, _preparations, _require
from .repair_android_operation import _fixture_digest
from .repair_signing_configuration import _path


@dataclass(frozen=True, slots=True)
class IOSBaselineReference:
    role: str
    bundle_id: str
    path: Path = field(repr=False)
    sha256: str
    bytes: int

    def read(self):
        """Bound metadata before ZIP allocation and verify one immutable memory snapshot."""
        try:
            path=_path(str(self.path))
            descriptor=open_regular(path.parent,path.name)
            with os.fdopen(descriptor,'rb') as stream:
                before=os.fstat(stream.fileno())
                _require(stat.S_ISREG(before.st_mode) and before.st_uid in {0,os.getuid()}
                    and before.st_nlink==1 and not before.st_mode & 0o022 and before.st_size==self.bytes)
                _zip_preflight(path,self.bytes,max_entries=MAX_APP_ENTRIES,
                    expected_signature=_file_stat_signature(before),_descriptor=stream.fileno())
                body=stream.read(MAX_TRANSFER_BYTES+1);after=os.fstat(stream.fileno())
                _require(len(body)==self.bytes and hashlib.sha256(body).hexdigest()==self.sha256
                    and _file_stat_signature(before)==_file_stat_signature(after))
            hashes={};total=0;info=None
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                entries,app=_validated_ipa_members(archive.infolist(),max_bytes=MAX_EXPANDED_APP_BYTES,
                                                  max_entries=MAX_APP_ENTRIES)
                prefix='Payload/'+app+'/'
                for entry,name,directory in entries:
                    if directory:continue
                    relative=name[len(prefix):];checksum=hashlib.sha256();size=0;parts=[]
                    with archive.open(entry) as source:
                        while chunk:=source.read(1024*1024):
                            size+=len(chunk);_require(size<=entry.file_size)
                            checksum.update(chunk)
                            if relative=='Info.plist':
                                _require(size<=MAX_PLIST_BYTES);parts.append(chunk)
                    _require(size==entry.file_size)
                    if relative=='Info.plist':info=plistlib.loads(b''.join(parts))
                    # Match the existing tree_manifest identity; the complete
                    # compressed container remains independently SHA-256 bound.
                    if PurePosixPath(relative).name!='.DS_Store':
                        hashes[relative]=checksum.hexdigest();total+=size
            _require(type(info) is dict and info.get('CFBundleIdentifier')==self.bundle_id
                and info.get('CFBundlePackageType')=='APPL')
            executable=info.get('CFBundleExecutable');_check_component(executable)
            _require(executable in hashes and all(type(info.get(name)) is str and info[name]
                for name in ('CFBundleShortVersionString','CFBundleVersion')))
            return body,{'treeDigest':contracts.digest(hashes),'bytes':total,'bundleId':self.bundle_id,
                'bundleVersion':info['CFBundleShortVersionString'],'bundleBuild':info['CFBundleVersion'],
                'members':hashes}
        except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError,
                zipfile.BadZipFile,plistlib.InvalidFileException):
            raise ProtectedMobileInputsError() from None


def local_ios_device(row, lab):
    device=lab.devices[row['deviceId']]
    _require(device.get('_remoteAuthority',False) is False and device.get('kind')=='ios-physical'
        and device.get('_authority',{}).get('deviceKind')=='ios-physical')
    return device


@dataclass(frozen=True, slots=True)
class IOSMobileInputsConfig:
    lab: Lab = field(repr=False)
    service: IssueSessionService = field(repr=False)
    registration: TrustedProjectRegistration = field(repr=False)
    device_id: str
    owner: str
    original_profile: IosAppProfile = field(repr=False)
    query: IOSDeviceQueryDefinition = field(repr=False)
    baselines: tuple[IOSBaselineReference,...] = field(repr=False)
    preparations: tuple = field(repr=False)
    runtime_policy_digest: str
    xctest: object = field(default=None,repr=False)
    sanitation: object = field(default=None,repr=False)
    egress: object = field(default=None,repr=False)

    @property
    def application_id(self):return self.original_profile.data['applicationId']

    @property
    def original_build_id(self):return self.original_profile.data['buildId']

    @property
    def definition(self):
        manifest=[{'path':item.role+'.ipa','digest':item.sha256,'size':item.bytes}
                  for item in sorted(self.baselines,key=lambda item:item.role)]
        return IOSMobileDefinition(self.registration.project_digest,self.application_id,self.runtime_policy_digest,
            self.device_id,self.query.udid,self.original_profile.bundle,self.query.definition_digest,
            self.original_profile.digest,contracts.digest(manifest),
            tuple((item.role,item.bundle_id) for item in self.baselines if item.role!='original'),
            self.xctest.definition_digest if self.xctest is not None else None,
            self.sanitation.digest if self.sanitation is not None else None,
            self.egress.digest if self.egress is not None else None)

    @property
    def scope_digest(self):return self.definition.scope_digest

    def snapshot(self, source_digest):
        return contracts.digest({'sourceDigest':source_digest,'definition':self.definition.public(),'owner':self.owner,
            'references':[{'role':item.role,'path':str(item.path),'sha256':item.sha256,'bytes':item.bytes}
                          for item in self.baselines],'fixturePlansDigest':_fixture_digest(self.preparations)})

    def validate(self):
        _require(type(self.lab) is Lab and type(self.service) is IssueSessionService
            and type(self.registration) is TrustedProjectRegistration and type(self.original_profile) is IosAppProfile
            and type(self.query) is IOSDeviceQueryDefinition and self.service.lab is self.lab
            and self.service.runner is not None and self.service.runner.lab is self.lab
            and self.service.registry is self.service.runner.registry)
        contracts.validate_id(self.owner);contracts.validate_digest(self.runtime_policy_digest)
        data=self.original_profile.data
        _require(data['projectDigest']==self.registration.project_digest
            and data['projectId']==self.registration.project['id'] and data['bundle']==self.query.bundle)
        device=local_ios_device({'deviceId':self.device_id},self.lab)
        capabilities=device.get('capabilities',{})
        _require(device['_authority'].get('physicalId')==self.query.udid
            and capabilities.get('applicationProfile')==data
            and capabilities.get('applicationProfileDigest')==self.original_profile.digest
            and capabilities.get('applicationIdentity')==self.original_profile.application_identity)
        self.lab._validate_release_selection(self.device_id,self.registration,self.application_id,self.original_build_id)
        self.service._checked_preparations(self.preparations,self.registration,self.application_id)
        self.query.verify()
        if self.xctest is not None:
            from .ios_mobile_xctest import IOSXCTestTools
            _require(type(self.xctest) is IOSXCTestTools and self.query.native_guardian is not None
                and self.xctest.guardian.definition_digest == self.query.native_guardian.definition_digest
                and len(self.baselines) == 3)
            self.xctest.verify()
        if self.sanitation is not None:
            from .ios_sanitation import IOSSanitationPolicy, validate_ios_sanitation_policy
            _require(type(self.sanitation) is IOSSanitationPolicy and self.xctest is not None
                and validate_ios_sanitation_policy(self.sanitation.data).digest == self.sanitation.digest)
        if self.egress is not None:
            from .ios_egress import IOSEgressPolicy, egress_policy
            _require(type(self.egress) is IOSEgressPolicy and self.xctest is not None
                and egress_policy(self.egress.data).digest == self.egress.digest)
        _require(type(self.baselines) is tuple and len(self.baselines) in (1,3)
            and all(type(item) is IOSBaselineReference for item in self.baselines)
            and sum(item.bytes for item in self.baselines)<=MAX_TRANSFER_BYTES)
        roles=[item.role for item in self.baselines]
        _require(len(set(roles))==len(roles) and set(roles) in ({'original'},{'original','helper-host','helper-runner'}))
        _require(len({item.bundle_id for item in self.baselines})==len(self.baselines))
        if self.xctest is not None:
            _require(next(item.bundle_id for item in self.baselines if item.role == 'helper-runner')
                == self.xctest.template.runner_bundle_identifier)
        for item in self.baselines:
            body,identity=item.read()
            if item.role=='original':
                expected=data['artifact']
                expected_digest=item.sha256 if expected['kind']=='ios-ipa' else identity['treeDigest']
                expected_bytes=item.bytes if expected['kind']=='ios-ipa' else identity['bytes']
                _require(expected_digest==expected['sha256'] and expected_bytes==expected['bytes']
                    and identity['bundleId']==data['bundle'] and identity['bundleVersion']==expected['bundleVersion']
                    and identity['bundleBuild']==expected['bundleBuild'])
                if self.sanitation is not None:
                    from .ios_sanitation import validate_ios_sanitation_policy
                    with zipfile.ZipFile(io.BytesIO(body)) as archive:
                        info_name=next(name for name in archive.namelist()
                            if name.count('/')==2 and name.endswith('/Info.plist'))
                        info=plistlib.loads(archive.read(info_name))
                    _require(validate_ios_sanitation_policy(info.get('ReproSanitationPolicy')).digest
                        == info.get('ReproSanitationPolicyDigest') == self.sanitation.digest)
            del body

    def read_baselines(self):
        result=BlobSet(tuple((item.role+'.ipa',item.read()[0]) for item in self.baselines))
        _require(result.digest==self.definition.baseline_digest)
        return result


@dataclass(frozen=True, slots=True)
class LoadedIOSMobileInputs:
    profile_id: str
    config: IOSMobileInputsConfig = field(repr=False)
    source_digest: str
    definition_digest: str
    input_paths: tuple = field(repr=False)

    def public(self):
        return {'profileId':self.profile_id,'definitionDigest':self.definition_digest,
            'scopeDigest':self.config.scope_digest,'applicationId':self.config.application_id,
            'originalBuildId':self.config.original_build_id}


def ios_recovery_device_descriptor(row):
    """Validate public recovery metadata without launching a tool or device."""
    from .protected_mobile_inputs import _recovery_only_provider
    value = _json(row['mobile']['definition'])
    exact(value, ('schemaVersion', 'kind', 'owner', 'udid', 'coreDeviceIdentifier',
        'runtimeProfile', 'query', 'baselines', 'preparations'), ('xctest', 'sanitation', 'egress'))
    _require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1
        and value['kind'] == 'ios-mobile-definition-v1')
    profile = validate_ios_profile(_json(value['runtimeProfile']))
    data = profile.data
    _require((data['projectId'], data['projectDigest'], data['applicationId'], data['buildId'])
        == (row['projectId'], row['projectDigest'], row['applicationId'], row['originalBuildId']))
    from .live.authority import canonical_device_fingerprint
    canonical_device_fingerprint('ios-physical', value['udid'])
    _require(type(value['baselines']) is list and len(value['baselines']) in (1, 3))
    seen = set()
    for item in value['baselines']:
        exact(item, ('role', 'bundleId', 'archive'))
        archive = item['archive']; exact(archive, ('path', 'sha256', 'bytes'))
        _require(item['role'] in {'original', 'helper-host', 'helper-runner'}
            and item['role'] not in seen and _bundle(item['bundleId']))
        seen.add(item['role'])
        contracts.validate_digest(archive['sha256'])
        contracts.bounded_int(archive['bytes'], 'IPA bytes', 1, MAX_TRANSFER_BYTES)
        path = _path(archive['path']); _require(path.suffix == '.ipa')
        body, identity = IOSBaselineReference(item['role'], item['bundleId'], path,
            archive['sha256'], archive['bytes']).read()
        if item['role'] == 'original':
            expected = data['artifact']
            _require(item['bundleId'] == data['bundle']
                and identity['bundleVersion'] == expected['bundleVersion']
                and identity['bundleBuild'] == expected['bundleBuild']
                and (archive['sha256'] if expected['kind'] == 'ios-ipa' else identity['treeDigest']) == expected['sha256']
                and (len(body) if expected['kind'] == 'ios-ipa' else identity['bytes']) == expected['bytes'])
    _require('original' in seen)
    return {'id': row['deviceId'], 'name': 'iOS recovery', 'platform': 'ios', 'kind': 'ios-physical',
        '_authority': {'deviceKind': 'ios-physical', 'physicalId': value['udid']}, '_recoveryOnly': True,
        'capabilities': {'authorityMode': 'shared-v2', 'actions': [], 'recovery': False,
            'recoveryOnly': True, 'applicationIdentity': profile.application_identity,
            'applicationProfile': data, 'applicationProfileDigest': profile.digest},
        'factory': _recovery_only_provider}


def load_ios_mobile_profile(row, runtime, lab):
    device=local_ios_device(row,lab)
    source=row['mobile']['definition'];value=_json(source)
    exact(value,('schemaVersion','kind','owner','udid','coreDeviceIdentifier','runtimeProfile','query','baselines','preparations'),('xctest','sanitation','egress'))
    _require(type(value['schemaVersion']) is int and value['schemaVersion']==1 and value['kind']=='ios-mobile-definition-v1')
    _require(device['_authority'].get('physicalId')==value['udid'])
    profile=validate_ios_profile(_json(value['runtimeProfile']));data=profile.data
    _require((data['projectId'],data['projectDigest'],data['applicationId'],data['buildId'])
        == (row['projectId'],row['projectDigest'],row['applicationId'],row['originalBuildId']))
    query=value['query'];exact(query,('devicectlPath','devicectlSha256','workRoot'),('nativeGuardian',))
    tools=IOSDeviceTools(_path(query['devicectlPath']),query['devicectlSha256'])
    guardian=None
    if 'nativeGuardian' in query:
        from .ios_device_guardian import IOSDeviceGuardianTools
        selected=query['nativeGuardian'];exact(selected,('path','sha256'))
        guardian=IOSDeviceGuardianTools(_path(selected['path']),selected['sha256'])
    declared=IOSDeviceQueryDefinition(tools,value['coreDeviceIdentifier'],value['udid'],data['bundle'],
        _path(query['workRoot']),guardian)
    xctest=None
    if 'xctest' in value:
        from .ios_mobile_xctest import IOSXCTestTools
        from .ios_xctest_template import IOSXCTestTemplate
        selected=value['xctest'];exact(selected,('xcodebuildPath','xcodebuildSha256','developerRoot','template'),('port',))
        exact(selected['template'],('path','sha256'))
        _require(guardian is not None)
        xctest=IOSXCTestTools(_path(selected['xcodebuildPath']),selected['xcodebuildSha256'],
            _path(selected['developerRoot']),guardian,
            IOSXCTestTemplate(_path(selected['template']['path']),selected['template']['sha256']),selected.get('port',8765))
    sanitation=None
    if 'sanitation' in value:
        from .ios_sanitation import validate_ios_sanitation_policy
        sanitation=validate_ios_sanitation_policy(_json(value['sanitation']))
        _require(xctest is not None)
    egress=None
    if 'egress' in value:
        from .ios_egress import egress_policy
        egress=egress_policy(_json(value['egress']))
        _require(xctest is not None)
    _require(type(value['baselines']) is list and len(value['baselines']) in (1,3))
    baselines=[]
    for item in value['baselines']:
        exact(item,('role','bundleId','archive'));exact(item['archive'],('path','sha256','bytes'))
        _require(item['role'] in {'original','helper-host','helper-runner'} and _bundle(item['bundleId']))
        archive=item['archive'];path=_path(archive['path']);_require(path.suffix=='.ipa')
        contracts.validate_digest(archive['sha256']);contracts.bounded_int(archive['bytes'],'IPA bytes',1,MAX_TRANSFER_BYTES)
        baselines.append(IOSBaselineReference(item['role'],item['bundleId'],path,archive['sha256'],archive['bytes']))
    protected=[_path(row[phase]['journal']['root']) for phase in ('build','signing','mobile')]
    protected += [_path(row[phase]['ownerRoot']) for phase in ('signing','mobile')]
    build_key='hostPath' if row['build']['route']['executionClass']=='host-build' else 'bundlePath'
    protected += [_path(row['build'][build_key]),_path(row['signing']['toolsPath']),
        declared.tools.devicectl,_path(value['runtimeProfile']['path']),_path(source['path']),*(item.path for item in baselines)]
    if guardian is not None:protected.append(guardian.path)
    if xctest is not None:protected.extend((xctest.xcodebuild,xctest.developer_root,xctest.template.path))
    if sanitation is not None:protected.append(_path(value['sanitation']['path']))
    if egress is not None:protected.append(_path(value['egress']['path']))
    _require(all(declared.work_root!=path and declared.work_root not in path.parents
                 and path not in declared.work_root.parents for path in protected))
    config=IOSMobileInputsConfig(lab,runtime.service,runtime.registration,row['deviceId'],value['owner'],profile,
        declared,tuple(sorted(baselines,key=lambda item:item.role)),
        _preparations(value['preparations'],runtime,row['applicationId']),row['runtimePolicyDigest'],xctest,sanitation,egress)
    config.validate()
    return LoadedIOSMobileInputs(row['id'],config,source['sha256'],config.snapshot(source['sha256']),
        tuple(protected)+(_path(row['signing']['definition']['path']),_path(row['validation']['observers']['path'])))
