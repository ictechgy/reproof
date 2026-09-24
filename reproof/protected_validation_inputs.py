"""Fixed external-observer definitions and explicit scoped credential binding."""
from __future__ import annotations

from dataclasses import dataclass,field
import json
import os

from . import contracts
from .contracts.versions import exact
from .execution.protocol import validate_external_validation_plan
from .execution.wire import canonical,decode_json
from .protected_mobile_inputs import LoadedAndroidMobileInputs,_snapshot
from .ios_mobile_inputs import LoadedIOSMobileInputs
from .protected_signing_inputs import _read_bound
from .protected_validation import (ValidationSecretRegistry,UnixAndroidValidationObserver,
                                   UnixIOSValidationObserver)
from .repair_android import AndroidTrustedMobileAdapter
from .repair_ios import IOSTrustedMobileAdapter
from .repair_signing_configuration import _path
from .validation import TrustedValidationAuthority,ValidationError


def _require(value):
    if not value:raise ValidationError('validation_definition_invalid')


@dataclass(frozen=True,slots=True)
class AndroidValidationInputs:
    mobile_inputs: LoadedAndroidMobileInputs = field(repr=False)
    _plan: str = field(repr=False)
    _observers: str = field(repr=False)
    source_digest: str

    @property
    def plan(self):return json.loads(self._plan)

    @property
    def definition_digest(self):
        return contracts.digest({'sourceDigest':self.source_digest,'planDigest':contracts.digest(self.plan),
            'mobileDefinitionDigest':self.mobile_inputs.definition_digest})

    def validate_binding(self,config):
        _require(config is self.mobile_inputs.config)
        config.validate()
        _require(_snapshot(config,self.mobile_inputs.source_digest)==self.mobile_inputs.definition_digest)

    def bind(self,adapter,secret_registry):
        _require(type(adapter) is AndroidTrustedMobileAdapter and type(secret_registry) is ValidationSecretRegistry)
        self.validate_binding(adapter.config)
        result=TrustedValidationAuthority(self.plan)
        for row in json.loads(self._observers):
            observer=UnixAndroidValidationObserver(adapter,self.plan,source_id=row['sourceId'],provider_id=row['providerId'],
                socket_path=row['socketPath'],authentication_reference_id=row['authenticationReferenceId'],
                secret_registry=secret_registry)
            result.register(row['sourceId'],observer,kind='external-observation')
        result.ready()
        return result

    def require_secrets(self, secret_registry, owner):
        _require(type(secret_registry) is ValidationSecretRegistry)
        secret_registry.require_owner(owner)
        for row in json.loads(self._observers):
            secret_registry.require_reference(row['authenticationReferenceId'],
                self.mobile_inputs.config.registration.project_digest,row['providerId'])

    def authentication_references(self):
        return tuple(sorted({(row['authenticationReferenceId'],row['providerId'])
                             for row in json.loads(self._observers)}))


@dataclass(frozen=True,slots=True)
class IOSValidationInputs:
    mobile_inputs: LoadedIOSMobileInputs = field(repr=False)
    _plan: str = field(repr=False)
    _observers: str = field(repr=False)
    source_digest: str

    @property
    def plan(self):return json.loads(self._plan)

    @property
    def definition_digest(self):
        return contracts.digest({'sourceDigest':self.source_digest,'planDigest':contracts.digest(self.plan),
            'mobileDefinitionDigest':self.mobile_inputs.definition_digest})

    def validate_binding(self,config):
        _require(config is self.mobile_inputs.config)
        config.validate()
        _require(config.snapshot(self.mobile_inputs.source_digest)==self.mobile_inputs.definition_digest)

    def bind(self,adapter,secret_registry):
        _require(type(adapter) is IOSTrustedMobileAdapter and type(secret_registry) is ValidationSecretRegistry)
        self.validate_binding(adapter.config)
        result=TrustedValidationAuthority(self.plan)
        for row in json.loads(self._observers):
            observer=UnixIOSValidationObserver(adapter,self.plan,source_id=row['sourceId'],provider_id=row['providerId'],
                socket_path=row['socketPath'],authentication_reference_id=row['authenticationReferenceId'],
                secret_registry=secret_registry)
            result.register(row['sourceId'],observer,kind='external-observation')
        result.ready()
        return result

    def require_secrets(self,secret_registry,owner):
        _require(type(secret_registry) is ValidationSecretRegistry)
        secret_registry.require_owner(owner)
        for row in json.loads(self._observers):
            secret_registry.require_reference(row['authenticationReferenceId'],
                self.mobile_inputs.config.registration.project_digest,row['providerId'])

    def authentication_references(self):
        return tuple(sorted({(row['authenticationReferenceId'],row['providerId'])
                             for row in json.loads(self._observers)}))


def load_android_validation_inputs(reference,*,plan,mobile_inputs):
    """Read metadata only; socket connections and credential reads occur during checks."""
    try:
        _require(type(mobile_inputs) is LoadedAndroidMobileInputs)
        mobile_inputs.config.validate()
        _require(_snapshot(mobile_inputs.config,mobile_inputs.source_digest)==mobile_inputs.definition_digest)
        checked=validate_external_validation_plan(plan)
        _require(checked['projectDigest']==mobile_inputs.config.registration.project_digest
            and all(row['kind']=='external-observation' for row in checked['checks']))
        exact(reference,('path','sha256'));_require(_path(reference['path']).suffix=='.json')
        value=decode_json(_read_bound(reference['path'],reference['sha256'],maximum=1024*1024))
        exact(value,('schemaVersion','kind','observers'))
        _require(type(value['schemaVersion']) is int and value['schemaVersion']==1
            and value['kind']=='unix-validation-observers-v1'
            and type(value['observers']) is list and 1<=len(value['observers'])<=128)
        sources=set()
        for row in value['observers']:
            exact(row,('sourceId','providerId','socketPath','authenticationReferenceId'))
            for name in ('sourceId','providerId','authenticationReferenceId'):contracts.validate_id(row[name])
            path=_path(row['socketPath']);_require(len(os.fsencode(path))<104)
            _require(row['sourceId'] not in sources);sources.add(row['sourceId'])
        _require(sources=={row['evidenceSourceId'] for row in checked['checks']})
        return AndroidValidationInputs(mobile_inputs,canonical(checked).decode(),canonical(value['observers']).decode(),reference['sha256'])
    except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError,AttributeError):
        raise ValidationError('validation_definition_invalid') from None


def load_ios_validation_inputs(reference,*,plan,mobile_inputs):
    """Read iOS observer metadata without connecting or reading credentials."""
    try:
        _require(type(mobile_inputs) is LoadedIOSMobileInputs)
        mobile_inputs.config.validate()
        _require(mobile_inputs.config.snapshot(mobile_inputs.source_digest)==mobile_inputs.definition_digest)
        checked=validate_external_validation_plan(plan)
        _require(checked['projectDigest']==mobile_inputs.config.registration.project_digest
            and all(row['kind']=='external-observation' for row in checked['checks']))
        exact(reference,('path','sha256'));_require(_path(reference['path']).suffix=='.json')
        value=decode_json(_read_bound(reference['path'],reference['sha256'],maximum=1024*1024))
        exact(value,('schemaVersion','kind','observers'))
        _require(type(value['schemaVersion']) is int and value['schemaVersion']==1
            and value['kind']=='unix-validation-observers-v1'
            and type(value['observers']) is list and 1<=len(value['observers'])<=128)
        sources=set()
        for row in value['observers']:
            exact(row,('sourceId','providerId','socketPath','authenticationReferenceId'))
            for name in ('sourceId','providerId','authenticationReferenceId'):contracts.validate_id(row[name])
            path=_path(row['socketPath']);_require(len(os.fsencode(path))<104)
            _require(row['sourceId'] not in sources);sources.add(row['sourceId'])
        _require(sources=={row['evidenceSourceId'] for row in checked['checks']})
        return IOSValidationInputs(mobile_inputs,canonical(checked).decode(),canonical(value['observers']).decode(),
                                   reference['sha256'])
    except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError,AttributeError):
        raise ValidationError('validation_definition_invalid') from None


__all__=['AndroidValidationInputs','IOSValidationInputs','load_android_validation_inputs',
         'load_ios_validation_inputs']
