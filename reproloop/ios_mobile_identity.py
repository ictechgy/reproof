"""Read-only identity confirmation for an issued iOS install observation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import time

from . import contracts
from .execution.wire import decode_json
from .ios_device_tools import IOSDeviceToolError, _require
from .ios_mobile_install import IOSInstallObservation
from .ios_mobile_native import prepared_app
from .repair_android_operation import (_open_child_directory, _open_regular_at, _read_fd,
    _read_json_at)


_ROLES = {'install-candidate': 'candidate', 'restore-original': 'original'}


@dataclass(frozen=True, slots=True)
class IOSInstalledIdentityObservation:
    command: str
    native_binding_digest: str
    context_digest: str
    source_role: str
    source_app_digest: str
    bundle_id: str
    bundle_version: str
    bundle_build: str
    evidence_digest: str

    def public(self):
        return {'kind': 'ios-installed-identity', 'command': self.command,
            'nativeBindingDigest': self.native_binding_digest,
            'contextDigest': self.context_digest, 'sourceRole': self.source_role,
            'sourceAppDigest': self.source_app_digest, 'bundleId': self.bundle_id,
            'bundleVersion': self.bundle_version, 'bundleBuild': self.bundle_build,
            'evidenceDigest': self.evidence_digest, 'identityConfirmed': True,
            'installedArtifactVerified': False, 'deviceCleanupConfirmed': False,
            'executionAuthority': 'none'}


def _bounds(installer, cancellation, deadline):
    _require(type(deadline) in (int, float) and math.isfinite(deadline)
             and time.monotonic() < deadline and not cancellation.is_set()
             and not installer._closed)
    installer.native_owner._check()


def _saved_command(installer, observation):
    owner = installer.native_owner
    kind = observation.command
    role = _ROLES.get(kind)
    _require(role is not None and type(observation) is IOSInstallObservation
             and owner._install_results.get(kind) is observation
             and observation.native_binding_digest == owner.binding_digest)
    operation_id = owner.operation.context.operation_id
    intent, state = owner.operations._records(operation_id, owner._directory)
    payload = installer.payload(kind)
    _require(intent['operationId'] == operation_id
             and observation.payload_digest == contracts.digest(payload)
             and type(state) is dict)
    name = 'command-' + kind + '-work'
    directory = _open_child_directory(owner.command_directory, name)
    try:
        saved_intent = _read_json_at(directory, 'intent.json')
        saved_state = _read_json_at(directory, 'state.json')
        _require(type(saved_intent) is dict and set(saved_intent) == {
            'schemaVersion', 'operationId', 'nativeBindingDigest', 'payload',
            'dispatchOperationId', 'permitFingerprint', 'state'}
            and contracts.digest(saved_intent) == owner._install_intent_digests.get(kind)
            and saved_intent['state'] == 'attempted'
            and saved_intent['operationId'] == operation_id
            and saved_intent['nativeBindingDigest'] == owner.binding_digest
            and saved_intent['payload'] == payload
            and type(saved_state) is dict
            and saved_state == dict(saved_intent, state='tool-succeeded'))
        result_fd = _open_regular_at(directory, 'result.json')
        try:
            body = _read_fd(result_fd, 256 * 1024)
            evidence = decode_json(body)
        finally:
            os.close(result_fd)
    finally:
        os.close(directory)
    _require(type(evidence) is dict and type(evidence.get('info')) is dict
             and evidence['info'].get('outcome') == 'success'
             and type(evidence.get('result')) is dict
             and contracts.digest(evidence['result']) == observation.evidence_digest)
    installed = evidence['result'].get('installedApplications')
    _require(type(installed) is list and len(installed) == 1
             and type(installed[0]) is dict
             and installed[0].get('bundleID') == installer.definition.bundle)
    return role, payload


def observe_installed(installer, observation, *, cancellation, deadline_monotonic):
    """Confirm the selected installed app identity under the original owner."""
    try:
        _require(callable(getattr(cancellation, 'is_set', None)))
        _bounds(installer, cancellation, deadline_monotonic)
        role, payload = _saved_command(installer, observation)
        _bounds(installer, cancellation, deadline_monotonic)
        source = prepared_app(installer.native_owner, role)
        manifest = source.manifest
        _require(payload['appDigest'] == source.app_digest
                 and payload['bundleId'] == manifest['applicationId'])
        _bounds(installer, cancellation, deadline_monotonic)
        query = installer._queries.query('apps', cancellation=cancellation,
            deadline_monotonic=deadline_monotonic)
        _bounds(installer, cancellation, deadline_monotonic)
        _require(query.definition_digest == installer.definition.definition_digest
                 and query._native_binding_digest == installer.native_owner.binding_digest)
        data = query.data
        apps = data.get('apps') if type(data) is dict else None
        _require(type(apps) is list and len(apps) == 1 and type(apps[0]) is dict)
        row = apps[0]
        expected = {'bundleIdentifier': manifest['applicationId'],
                    'version': manifest['bundleVersion'],
                    'bundleVersion': manifest['bundleBuild']}
        _require(all(type(row.get(key)) is str and row.get(key) == value
                     for key, value in expected.items()))
        result = IOSInstalledIdentityObservation(observation.command,
            observation.native_binding_digest, installer.native_owner.operation.context.digest,
            role, source.app_digest, manifest['applicationId'], manifest['bundleVersion'],
            manifest['bundleBuild'], query.public()['evidenceDigest'])
        installer.native_owner._identity_results[observation.command] = result
        return result
    except BaseException as error:
        if isinstance(error, IOSDeviceToolError):
            raise
        if not isinstance(error, Exception):
            raise
        raise IOSDeviceToolError() from None


__all__ = ['IOSInstalledIdentityObservation',
           'observe_installed']
