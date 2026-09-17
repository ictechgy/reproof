"""Non-secret references for observing and recovering an existing iOS journal."""
from __future__ import annotations

import json

from . import contracts
from .contracts.versions import exact, require
from .repair_signing_configuration import _path, _read_configuration


class IOSSigningRecoveryConfiguration:
    __slots__ = ('_document',)

    def __init__(self, value):
        exact(value, ('schemaVersion','kind','applicationId','runStorePath','ownerRoot','environmentDigest',
            'diskBudgetBytes','operationDiskBytes','ownerConfigurationSha256','ownerDefinitionDigest',
            'scopeDigest','inputsDefinitionDigest','toolsDigest'))
        require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1
            and value['kind'] == 'ios-signing-recovery-v1', 'Invalid iOS recovery configuration version')
        contracts.validate_id(value['applicationId'])
        for name in ('runStorePath','ownerRoot'): _path(value[name])
        for name in ('environmentDigest','ownerConfigurationSha256','ownerDefinitionDigest',
                     'scopeDigest','inputsDefinitionDigest','toolsDigest'):
            contracts.validate_digest(value[name])
        contracts.bounded_int(value['diskBudgetBytes'],'signing disk budget',1,512*1024**3)
        contracts.bounded_int(value['operationDiskBytes'],'signing operation budget',1,value['diskBudgetBytes'])
        self._document = json.dumps(value,sort_keys=True,separators=(',',':'))

    @property
    def document(self):
        return json.loads(self._document)

    def open_existing(self):
        from .ios_signing_operation import IOSSigningOperationStore
        return IOSSigningOperationStore.open_for_recovery(self)


def load_ios_signing_configuration(path):
    return IOSSigningRecoveryConfiguration(_read_configuration(path))


__all__ = ['IOSSigningRecoveryConfiguration','load_ios_signing_configuration']
