"""Inert public references for an existing iOS preparation journal.

The reference contains enough immutable metadata to reopen the original owner
with ``create=False``.  It does not contain baseline IPA contents, query tools,
signing inputs, or device/service configuration.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from . import contracts
from .contracts.versions import exact, require
from .execution.artifacts import open_regular
from .execution.journal import RunStore
from .execution.wire import decode_json, safe_transfer_path


def _path(value):
    require(type(value) is str and value.startswith("/"),
            "Absolute iOS mobile configuration path required")
    safe_transfer_path(value[1:])
    selected = Path(value)
    require(str(selected) == value, "iOS mobile configuration path rejected")
    return selected


def _read_configuration(path):
    selected = _path(str(path))
    require(selected.suffix == ".json", "Public JSON configuration required")
    descriptor = open_regular(selected.parent, selected.name)
    with os.fdopen(descriptor, "rb") as incoming:
        before = os.fstat(incoming.fileno())
        require(before.st_uid == os.getuid() and before.st_nlink == 1
                and not (before.st_mode & (stat.S_IWGRP | stat.S_IWOTH |
                                           stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX))
                and 0 < before.st_size <= 64 * 1024,
                "iOS mobile configuration file rejected")
        raw = incoming.read(64 * 1024 + 1)
        after = os.fstat(incoming.fileno())
        require(len(raw) == before.st_size
                and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                "iOS mobile configuration changed while reading")
    return decode_json(raw)


def _definition_from_document(value):
    exact(value, ('project_digest', 'application_id', 'runtime_policy_digest', 'device_id',
                   'bundle_id', 'query_definition_digest', 'original_profile_digest',
                   'baseline_digest', 'helper_bundles', 'scopeDigest',
                   'executionAuthority'), ('xctest_definition_digest','sanitation_policy_digest',
                   'egress_policy_digest'))
    require(value['executionAuthority'] == 'none', 'Execution authority is not recoverable')
    helpers = value['helper_bundles']
    require(type(helpers) is list, 'Invalid helper bundle reference')
    converted = []
    for item in helpers:
        require(type(item) is list and len(item) == 2, 'Invalid helper bundle reference')
        converted.append((item[0], item[1]))
    return {
        'project_digest': value['project_digest'],
        'application_id': value['application_id'],
        'runtime_policy_digest': value['runtime_policy_digest'],
        'device_id': value['device_id'],
        'bundle_id': value['bundle_id'],
        'query_definition_digest': value['query_definition_digest'],
        'original_profile_digest': value['original_profile_digest'],
        'baseline_digest': value['baseline_digest'],
        'helper_bundles': tuple(converted),
        **({'xctest_definition_digest': value['xctest_definition_digest']}
           if 'xctest_definition_digest' in value else {}),
        **({'sanitation_policy_digest': value['sanitation_policy_digest']}
           if 'sanitation_policy_digest' in value else {}),
        **({'egress_policy_digest': value['egress_policy_digest']}
           if 'egress_policy_digest' in value else {}),
    }


class IOSMobileRecoveryConfiguration:
    """Validated, inert reference for one original preparation owner.

    ``document`` is retained for opening the owner.  ``public`` and ``repr``
    intentionally omit the private UDID reference.
    """
    __slots__ = ('_document', '_definition_public', '_udid')

    def __init__(self, value):
        exact(value, ('schemaVersion', 'kind', 'runStorePath', 'ownerRoot',
                      'environmentDigest', 'diskBudgetBytes', 'ownerConfigurationDigest',
                      'ownerDefinitionDigest', 'scopeDigest', 'definition', 'private'))
        require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1
                and value['kind'] == 'ios-mobile-preparation-recovery-v1',
                'Invalid iOS mobile recovery configuration version')
        run_path = _path(value['runStorePath']); owner_root = _path(value['ownerRoot'])
        require(run_path != owner_root and run_path not in owner_root.parents
                and owner_root not in run_path.parents, 'Overlapping iOS mobile roots')
        contracts.validate_digest(value['environmentDigest'])
        contracts.bounded_int(value['diskBudgetBytes'], 'mobile disk budget', 1, 512 * 1024 ** 3)
        contracts.validate_digest(value['ownerConfigurationDigest'])
        contracts.validate_digest(value['ownerDefinitionDigest'])
        contracts.validate_digest(value['scopeDigest'])
        definition = value['definition']
        private = value['private']
        exact(private, ('udid',))
        require(type(private['udid']) is str and private['udid'], 'Private device reference required')
        parsed = _definition_from_document(definition)
        # Reconstructing the definition is the only use of this private value.
        from .ios_mobile_operation import IOSMobileDefinition
        definition_args = [parsed['project_digest'], parsed['application_id'],
            parsed['runtime_policy_digest'], parsed['device_id'], private['udid'], parsed['bundle_id'],
            parsed['query_definition_digest'], parsed['original_profile_digest'],
            parsed['baseline_digest'], parsed['helper_bundles']]
        if 'xctest_definition_digest' in parsed:
            definition_args.append(parsed['xctest_definition_digest'])
        if 'sanitation_policy_digest' in parsed:
            require('xctest_definition_digest' in parsed, 'Sanitation requires fixed XCTest inputs')
            definition_args.append(parsed['sanitation_policy_digest'])
        if 'egress_policy_digest' in parsed:
            require('xctest_definition_digest' in parsed, 'Egress policy requires fixed XCTest inputs')
            definition_args.append(parsed['egress_policy_digest'])
        selected = IOSMobileDefinition(*definition_args)
        canonical_definition = selected.public()
        canonical_definition['helper_bundles'] = [list(item) for item in selected.helper_bundles]
        require(selected.scope_digest == value['scopeDigest']
                and value['definition'] == canonical_definition
                and contracts.digest(selected.public()) == value['ownerDefinitionDigest'],
                'iOS mobile definition binding changed')
        self._document = json.dumps({
            'schemaVersion': 1,
            'kind': value['kind'],
            'runStorePath': str(run_path),
            'ownerRoot': str(owner_root),
            'environmentDigest': value['environmentDigest'],
            'diskBudgetBytes': value['diskBudgetBytes'],
            'ownerConfigurationDigest': value['ownerConfigurationDigest'],
            'ownerDefinitionDigest': value['ownerDefinitionDigest'],
            'scopeDigest': value['scopeDigest'],
            'definition': dict(value['definition']),
            'private': {'udid': private['udid']},
        }, sort_keys=True, separators=(',', ':'))
        self._definition_public = json.loads(json.dumps(value['definition']))
        self._udid = private['udid']

    @property
    def document(self):
        return json.loads(self._document)

    def public(self):
        value = self.document
        value.pop('private', None)
        return value

    def __repr__(self):
        return '<IOSMobileRecoveryConfiguration>'

    def open_existing(self):
        value = self.document
        from .ios_mobile_operation import IOSMobileDefinition, IOSMobileOperationStore
        definition_values = value['definition']
        definition_args = [definition_values['project_digest'],
            definition_values['application_id'], definition_values['runtime_policy_digest'],
            definition_values['device_id'], value['private']['udid'],
            definition_values['bundle_id'], definition_values['query_definition_digest'],
            definition_values['original_profile_digest'], definition_values['baseline_digest'],
            tuple(tuple(item) for item in definition_values['helper_bundles'])]
        if 'xctest_definition_digest' in definition_values:
            definition_args.append(definition_values['xctest_definition_digest'])
        if 'sanitation_policy_digest' in definition_values:
            definition_args.append(definition_values['sanitation_policy_digest'])
        if 'egress_policy_digest' in definition_values:
            definition_args.append(definition_values['egress_policy_digest'])
        definition = IOSMobileDefinition(*definition_args)
        operations = None
        try:
            store = RunStore(value['runStorePath'], environment_digest=value['environmentDigest'],
                             disk_limit=value['diskBudgetBytes'], create=False)
            scope = store._load().get('scope')
            require(scope == {'kind': 'mobile-device', 'scopeDigest': value['scopeDigest']},
                    'Existing iOS mobile journal scope required')
            operations = IOSMobileOperationStore(store, definition, value['ownerRoot'], create=False)
            require(str(operations.root) == value['ownerRoot']
                and str(store.root) == value['runStorePath']
                and operations.configuration_digest == value['ownerConfigurationDigest']
                and operations.definition.scope_digest == value['scopeDigest'],
                'iOS mobile owner binding changed')
            return operations
        except Exception:
            try:
                if operations is not None:
                    operations.close()
            except (AttributeError, OSError, RuntimeError):
                pass
            raise


def export_ios_mobile_configuration(owner):
    """Export a strict reference from an existing ``IOSMobileOperationStore``."""
    from .ios_mobile_operation import IOSMobileDefinition, IOSMobileOperationStore
    require(type(owner) is IOSMobileOperationStore
            and type(owner.definition) is IOSMobileDefinition
            and type(owner.run_store) is RunStore,
            'Existing iOS mobile owner required')
    definition = owner.definition.public()
    definition['helper_bundles'] = [list(item) for item in owner.definition.helper_bundles]
    return {
        'schemaVersion': 1,
        'kind': 'ios-mobile-preparation-recovery-v1',
        'runStorePath': str(owner.run_store.root),
        'ownerRoot': str(owner.root),
        'environmentDigest': owner.run_store.environment_digest,
        'diskBudgetBytes': owner.run_store.disk_limit,
        'ownerConfigurationDigest': owner.configuration_digest,
        'ownerDefinitionDigest': contracts.digest(owner.definition.public()),
        'scopeDigest': owner.definition.scope_digest,
        'definition': definition,
        'private': {'udid': owner.definition.udid},
    }


def load_ios_mobile_configuration(path):
    return IOSMobileRecoveryConfiguration(_read_configuration(path))


__all__ = ['IOSMobileRecoveryConfiguration', 'export_ios_mobile_configuration',
           'load_ios_mobile_configuration']
