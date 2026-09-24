"""Bounded operator-selected signing definitions, without private key loading.

Certificate/profile bytes are captured against explicit hashes. Their actual
cryptography is checked by the fixed signer and inspector at execution time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat

from . import contracts
from .contracts.versions import exact
from .execution.artifacts import open_regular
from .execution.protocol import validate_signing_policy
from .execution.wire import decode_json
from .ios_provisioning_cms import MAX_CMS_BYTES,MAX_RETAINED_PROFILE_BYTES,IOSCmsTools,IOSCmsTrust
from .ios_signing_inputs import IOSSigningDefinition,IOSSigningIdentity,IOSSigningProvisioning
from .repair_android_signing import AndroidSigningIdentity,_validated_policy
from .repair_signing_configuration import _path


class ProtectedSigningInputsError(RuntimeError):
    def __init__(self):
        self.code='protected_signing_inputs'
        super().__init__('Selected signing definition or referenced input is invalid or changed')


def _require(value):
    if not value: raise ProtectedSigningInputsError()


def _read_bound(path, checksum, *, maximum, expected_bytes=None):
    selected=_path(path);contracts.validate_digest(checksum)
    descriptor=open_regular(selected.parent,selected.name)
    with os.fdopen(descriptor,'rb') as stream:
        before=os.fstat(stream.fileno())
        _require(before.st_uid in {0,os.getuid()} and before.st_nlink==1
            and not before.st_mode & (0o022|stat.S_ISUID|stat.S_ISGID|stat.S_ISVTX)
            and 0<before.st_size<=maximum and (expected_bytes is None or before.st_size==expected_bytes))
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
        _require(len(raw)==before.st_size and hashlib.sha256(raw).hexdigest()==checksum
            and (before.st_dev,before.st_ino,before.st_mode,before.st_uid,before.st_nlink,
                 before.st_size,before.st_mtime_ns,before.st_ctime_ns)
            == (after.st_dev,after.st_ino,after.st_mode,after.st_uid,after.st_nlink,
                after.st_size,after.st_mtime_ns,after.st_ctime_ns))
        return raw


def _blob_reference(value, *, certificate):
    exact(value,('path','sha256','bytes'))
    path=_path(value['path']);contracts.validate_digest(value['sha256'])
    allowed=('.der','.cer') if certificate else ('.cms','.mobileprovision','.provisionprofile')
    _require(path.suffix in allowed)
    maximum=128*1024 if certificate else MAX_CMS_BYTES
    contracts.bounded_int(value['bytes'],'signing input bytes',1,maximum)
    return value['bytes']


def _read_blob(value, *, certificate):
    _blob_reference(value,certificate=certificate)
    return _read_bound(value['path'],value['sha256'],expected_bytes=value['bytes'],
        maximum=128*1024 if certificate else MAX_CMS_BYTES)


@dataclass(frozen=True,slots=True)
class AndroidSigningDefinitionInputs:
    identity: AndroidSigningIdentity = field(repr=False)
    source_digest: str

    @property
    def definition_digest(self):
        return contracts.digest({'sourceDigest':self.source_digest,'referenceId':self.identity.reference_id,
            'applicationId':self.identity.application_id,'package':self.identity.package_name,
            'certificateSha256':self.identity.certificate_sha256,
            'signingConfigurationDigest':self.identity.signing_configuration_digest})

    def public(self):
        return {'platform':'android','sourceDigest':self.source_digest,
            'definitionDigest':self.definition_digest,'executionAuthority':'none'}


@dataclass(frozen=True,slots=True)
class IOSSigningDefinitionInputs:
    definition: IOSSigningDefinition = field(repr=False)
    provisioning: IOSSigningProvisioning = field(repr=False)
    source_digest: str

    @property
    def identity(self): return self.definition.identity

    @property
    def definition_digest(self):
        return contracts.digest({'sourceDigest':self.source_digest,
            'definitionDigest':self.definition.definition_digest,
            'provisioningDigest':self.provisioning.definition_digest})

    def public(self):
        return {'platform':'ios','sourceDigest':self.source_digest,
            'definitionDigest':self.definition_digest,'executionAuthority':'none'}


def _android(value, source_digest, policy, application):
    exact(value,('schemaVersion','kind','identity'))
    _require(value['kind']=='android-signing-definition-v1')
    identity=value['identity']
    exact(identity,('referenceId','applicationId','packageName','certificateSha256','signatureSchemes','permissions'))
    _require(type(identity['signatureSchemes']) is list and type(identity['permissions']) is list)
    selected=AndroidSigningIdentity(identity['referenceId'],identity['applicationId'],identity['packageName'],
        identity['certificateSha256'],tuple(identity['signatureSchemes']),tuple(identity['permissions']))
    _validated_policy(selected,policy)
    _require(selected.package_name==application['bundle'])
    return AndroidSigningDefinitionInputs(selected,source_digest)


def _ios(value, source_digest, policy, application, profile_bytes_limit):
    exact(value,('schemaVersion','kind','identity','provisioningReferenceId','bundlePolicies','profiles','provisioning'))
    _require(value['kind']=='ios-signing-definition-v1')
    identity=value['identity'];exact(identity,('referenceId','applicationId','teamId','certificateChain'))
    chain=identity['certificateChain']
    _require(type(chain) is list and 1<=len(chain)<=8)
    for item in chain: _blob_reference(item,certificate=True)
    profiles=value['profiles'];_require(type(profiles) is dict and 1<=len(profiles)<=512)
    total=0
    for row in profiles.values():
        exact(row,('cms','profileDigest'));contracts.validate_digest(row['profileDigest'])
        total+=_blob_reference(row['cms'],certificate=False)
    _require(total<=profile_bytes_limit)
    provisioning=value['provisioning']
    exact(provisioning,('tools','trust','applicationIdentifierPrefix','selectedDevice'))
    tools=provisioning['tools'];exact(tools,('opensslPath','opensslSha256','sandboxSha256'))
    _path(tools['opensslPath']);contracts.validate_digest(tools['opensslSha256']);contracts.validate_digest(tools['sandboxSha256'])
    trust=provisioning['trust'];exact(trust,('referenceId','signer','anchors'))
    _blob_reference(trust['signer'],certificate=True)
    _require(type(trust['anchors']) is list and 1<=len(trust['anchors'])<=8)
    for item in trust['anchors']: _blob_reference(item,certificate=True)
    selected=IOSSigningIdentity(identity['referenceId'],identity['applicationId'],identity['teamId'],
        tuple(_read_blob(item,certificate=True) for item in chain))
    captured={path:{'cms':_read_blob(row['cms'],certificate=False),'profileDigest':row['profileDigest']}
        for path,row in profiles.items()}
    definition=IOSSigningDefinition(selected,value['provisioningReferenceId'],value['bundlePolicies'],captured)
    definition.validate_policy(policy)
    _require(definition.bundle_policies['.']['bundleId']==application['bundle'])
    cms_tools=IOSCmsTools(Path(tools['opensslPath']),tools['opensslSha256'],tools['sandboxSha256'])
    cms_trust=IOSCmsTrust(trust['referenceId'],_read_blob(trust['signer'],certificate=True),
        tuple(_read_blob(item,certificate=True) for item in trust['anchors']))
    provision=IOSSigningProvisioning(cms_tools,cms_trust,
        provisioning['applicationIdentifierPrefix'],provisioning['selectedDevice'])
    return IOSSigningDefinitionInputs(definition,provision,source_digest)


def load_signing_definition(reference, *, policy_document, application,
                            profile_bytes_limit=MAX_RETAINED_PROFILE_BYTES):
    """Capture only the explicitly registered certificate/profile definition."""
    try:
        contracts.bounded_int(profile_bytes_limit,'retained profile budget',0,MAX_RETAINED_PROFILE_BYTES)
        exact(reference,('path','sha256'))
        _require(_path(reference['path']).suffix=='.json')
        policy=validate_signing_policy(policy_document)
        _require(type(application) is dict and application['id']==policy['applicationId']
            and application['platform']==policy['platform'] and type(application['bundle']) is str)
        raw=_read_bound(reference['path'],reference['sha256'],maximum=2*1024*1024)
        value=decode_json(raw)
        _require(type(value) is dict and type(value.get('schemaVersion')) is int and value['schemaVersion']==1)
        if policy['platform']=='android':
            return _android(value,reference['sha256'],policy,application)
        return _ios(value,reference['sha256'],policy,application,profile_bytes_limit)
    except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError,AttributeError):
        raise ProtectedSigningInputsError() from None


__all__=['ProtectedSigningInputsError','AndroidSigningDefinitionInputs','IOSSigningDefinitionInputs',
         'load_signing_definition']
