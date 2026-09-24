"""Retire a prior fixed XCTest controller under a fresh native recovery lease.

Saved launch records select credentials, never authority. The current pinned
device query supplies the tunnel and a complete process observation. Failure
to parse or observe absence keeps the original operation quarantined.
"""
from dataclasses import dataclass, field
import hashlib
import io
import ipaddress
import os
from pathlib import PurePosixPath
import plistlib
import re
import time
from urllib.parse import unquote, urlsplit
import uuid
import zipfile

from . import contracts
from .ios_mobile_inputs import IOSMobileInputsConfig
from .ios_mobile_native import IOSMobileNativeOwner
from .ios_native_recovery import require_native_recovery, IOSNativeRecoveryError
from .ios_recovery_execution import IOSRecoveryExecution
from .ios_xctest_template import _target_location
from .live.iphone import TunnelClient
from .repair_android_operation import _open_child_directory, _open_regular_at, _read_fd, _read_json_at

_COMMAND = re.compile(r'command-xctest-(?:candidate|original)-00[1-3]-work\Z')
_ID = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
_NAME = re.compile(r'[A-Za-z0-9_. -]{1,128}\Z')
_RETIRE = {'action': 'authority_retire', 'payload': {}}


def _require(value):
    if not value:
        raise IOSNativeRecoveryError('ios_recovery_helper_unconfirmed')


@dataclass(frozen=True, slots=True, repr=False)
class IOSRecoveryHelperRetirement:
    context_digest: str
    evidence_digest: str
    _execution: object = field(repr=False, compare=False)

    def public(self):
        return {'kind': 'ios-prior-helper-retirement', 'contextDigest': self.context_digest,
            'evidenceDigest': self.evidence_digest, 'priorHelperAbsent': True,
            'deviceCleanupConfirmed': False, 'executionAuthority': 'none'}


def require_ios_recovery_helper_retirement(proof, context):
    require_native_recovery(context)
    _require(type(proof) is IOSRecoveryHelperRetirement
        and type(proof._execution) is IOSRecoveryExecution
        and proof._execution._context is context
        and getattr(proof._execution, '_retirement_observation', None) is proof
        and proof.context_digest == context.context_digest)
    return proof


def _helper_executables(config):
    names = set()
    for reference in config.baselines:
        if reference.role not in ('helper-host', 'helper-runner'):
            continue
        body, _ = reference.read()
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            selected = [entry for entry in archive.namelist()
                if entry.startswith('Payload/') and len(entry.split('/')) == 3
                and entry.endswith('/Info.plist')]
            _require(len(selected) == 1)
            info = plistlib.loads(archive.read(selected[0]))
        name = info.get('CFBundleExecutable')
        _require(type(name) is str and _NAME.fullmatch(name) and name not in ('.', '..'))
        names.add(name)
    _require(names)
    return names


def _running_helpers(data, names):
    rows = data.get('runningProcesses')
    _require(type(rows) is list and len(rows) <= 65536)
    selected = []
    for row in rows:
        _require(type(row) is dict and type(row.get('processIdentifier')) is int
            and 0 < row['processIdentifier'] < 2**31
            and type(row.get('executable')) is str)
        raw = row['executable']
        if raw.startswith('file:'):
            url = urlsplit(raw)
            _require(url.scheme == 'file' and url.netloc in ('', 'localhost')
                and not url.query and not url.fragment)
            raw = unquote(url.path, errors='strict')
        path = PurePosixPath(raw)
        _require(raw.startswith('/') and str(path) == raw and '..' not in path.parts
            and '\x00' not in raw and len(raw) <= 4096)
        if path.name in names:
            selected.append(row)
    return selected


def _launches(context):
    """Select bounded previously dispatched commands; never follow saved paths."""
    roots = [os.dup(context.directory)]
    recovery = None
    try:
        if 'native-recovery' in os.listdir(context.directory):
            recovery = _open_child_directory(context.directory, 'native-recovery')
            for name in sorted(os.listdir(recovery)):
                if re.fullmatch(r'attempt-00[1-3]', name):
                    roots.append(_open_child_directory(recovery, name))
        records = []
        for root in roots:
            for name in sorted(os.listdir(root)):
                if not _COMMAND.fullmatch(name):
                    continue
                directory = _open_child_directory(root, name)
                try:
                    if 'native.json' not in os.listdir(directory):
                        continue
                    intent = _read_json_at(directory, 'intent.json')
                    native = _read_json_at(directory, 'native.json')
                    payload = intent.get('payload')
                    _require(type(payload) is dict
                        and payload.get('kind') == 'ios-fixed-xctest-launch-v1'
                        and payload.get('contextDigest') == context.context_digest
                        and payload.get('nativeBindingDigest') == context.binding_digest
                        and payload.get('scopeDigest') == context.scope_digest
                        and native.get('nativeBindingDigest') == context.binding_digest)
                    descriptor = _open_regular_at(directory, 'session.xctestrun')
                    try:
                        body = _read_fd(descriptor, 128*1024)
                    finally:
                        os.close(descriptor)
                    _require(hashlib.sha256(body).hexdigest() == payload['configurationDigest'])
                    _, target = _target_location(plistlib.loads(body))
                    environment = target['EnvironmentVariables']
                    _require(environment['REPRO_TARGET_BUNDLE'] == context._operations.definition.bundle_id
                        and environment['REPRO_LIVE_PROVIDER_INCARNATION'] == payload['providerIncarnation']
                        and environment['REPRO_LIVE_PROTOCOL_VERSION'] == '2'
                        and environment['REPRO_LIVE_HELPER_VERSION'] == '2'
                        and contracts.digest({'address': environment['REPRO_LIVE_LISTEN_HOST'],
                            'port': int(environment['REPRO_LIVE_LISTEN_PORT']),
                            'token': environment['REPRO_LIVE_TOKEN']}) == payload['endpointDigest'])
                    for key in ('HELPER', 'HOST', 'PROVIDER'):
                        _require(_ID.fullmatch(environment['REPRO_LIVE_'+key+'_INCARNATION']))
                    _require(type(environment['REPRO_LIVE_TOKEN']) is str
                        and re.fullmatch(r'[A-Za-z0-9_-]{43}', environment['REPRO_LIVE_TOKEN']))
                    records.append((payload, environment))
                finally:
                    os.close(directory)
        _require(len(records) <= 9)
        return records
    finally:
        for root in roots:
            os.close(root)
        if recovery is not None:
            os.close(recovery)


def recover_prior_helpers(context, config, *, cancellation, deadline_monotonic):
    query = transport = None
    try:
        require_native_recovery(context)
        _require(type(config) is IOSMobileInputsConfig
            and config.definition == context._operations.definition)
        owners = [owner for owner in context._operations._native_owners.values()
            if type(owner) is IOSMobileNativeOwner
            and type(owner._recovery_context) is IOSRecoveryExecution
            and owner._recovery_context._context is context]
        _require(len(owners) == 1)
        owner = owners[0]
        execution = owner._recovery_context
        # Any previously issued execution dispatch means retirement is too late.
        _require(not execution._issued)
        def bounds():
            require_native_recovery(context)
            owner._check()
            _require(not cancellation.is_set() and time.monotonic() < deadline_monotonic)
            return min(deadline_monotonic, context.deadline_monotonic)
        names = _helper_executables(config)
        launches = _launches(context)
        query = config.query.open_client(native_owner=owner)
        observations = []
        processes = query.query('processes', cancellation=cancellation, deadline_monotonic=bounds())
        observations.append(processes.public()['evidenceDigest'])
        present = _running_helpers(processes.data, names)
        if present:
            details = query.query('details', cancellation=cancellation, deadline_monotonic=bounds())
            connection = details.data.get('connectionProperties', {})
            address = connection.get('tunnelIPAddress')
            parsed = ipaddress.IPv6Address(address)
            _require(str(parsed) == address and parsed.is_private and not
                (parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast)
                and connection.get('pairingState') == 'paired'
                and connection.get('transportType') == 'wired'
                and connection.get('tunnelState') == 'connected')
            matched = False
            for payload, environment in reversed(launches):
                _require(int(environment['REPRO_LIVE_LISTEN_PORT']) == config.xctest.port)
                transport = TunnelClient(address, config.xctest.port, environment['REPRO_LIVE_TOKEN'])
                sent = context._device._authority.clock_sync.sample().nanoseconds
                try:
                    status = transport.call('/status', timeout=min(2, max(.001, bounds()-time.monotonic())))
                except Exception:
                    transport.token = ''
                    continue
                received = context._device._authority.clock_sync.sample().nanoseconds
                _require(type(status) is dict and type(status.get('retirementVersion')) is int
                    and status['retirementVersion'] == 1
                    and status.get('protocolVersion') == 2 and status.get('helperVersion') == 2
                    and status.get('targetBundle') == config.query.bundle
                    and status.get('applicationProfileDigest') == payload['profileDigest'])
                for key in ('helper', 'host', 'provider'):
                    _require(status.get(key+'Incarnation') == environment['REPRO_LIVE_'+key.upper()+'_INCARNATION'])
                _require(type(status.get('nativeTimeMs')) is int and status['nativeTimeMs'] >= 0
                    and type(status.get('authoritySequence')) is int
                    and 0 <= status['authoritySequence'] < 2**53-1
                    and type(status.get('nativeIncarnation')) is str
                    and _ID.fullmatch(status['nativeIncarnation'])
                    and status.get('nativeClockId') == 'ios-mach-continuous')
                remaining = min((context.grant_deadline_ns-received)//1_000_000,
                    int((bounds()-time.monotonic())*1000), 10000)
                uncertainty = max(2, (received-sent+999999)//1_000_000 + 2)
                duration = remaining*999000//1000000 - uncertainty
                _require(duration > 0)
                grant = {'protocolVersion': 2, 'operationId': 'ios_retire_'+uuid.uuid4().hex,
                    'operationFingerprint': contracts.digest({'context': context.context_digest,
                        'grant': context._parent_grant.grant_id, 'launch': contracts.digest(payload),
                        'nonce': uuid.uuid4().hex}),
                    'payloadDigest': contracts.digest(_RETIRE),
                    'projectId': context._parent_grant.project_id,
                    'sessionId': context.operation_id, 'controllerId': context._parent_grant.controller_id,
                    'sequence': status['authoritySequence']+1,
                    'ownershipGeneration': context.prior_generation,
                    'hostIncarnation': status['hostIncarnation'],
                    'helperIncarnation': status['helperIncarnation'],
                    'providerIncarnation': status['providerIncarnation'],
                    'nativeIncarnation': status['nativeIncarnation'],
                    'nativeClockId': 'ios-mach-continuous',
                    'nativeDeadlineMs': status['nativeTimeMs']+duration}
                bounds()
                response = transport.call('/retire', dict(_RETIRE, id=grant['operationId'], authority=grant),
                    timeout=min(2, max(.001, bounds()-time.monotonic())))
                _require(response == {'accepted': True})
                observations.append(contracts.digest({'status': status, 'grant': grant, 'response': response}))
                transport.token = ''
                matched = True
                break
            _require(matched)
            # A stop acknowledgement alone is insufficient: observe every
            # helper executable absent from a fresh complete device process list.
            for _ in range(128):
                processes = query.query('processes', cancellation=cancellation, deadline_monotonic=bounds())
                present = _running_helpers(processes.data, names)
                if not present:
                    observations.append(processes.public()['evidenceDigest'])
                    break
                time.sleep(min(.1, max(0, bounds()-time.monotonic())))
            _require(not present)
        bounds()
        _require(query.close(deadline_monotonic=bounds()))
        query = None
        proof = IOSRecoveryHelperRetirement(context.context_digest,
            contracts.digest({'context': context.context_digest, 'binding': context.binding_digest,
                'helperNamesDigest': contracts.digest(sorted(names)), 'observations': observations}), execution)
        execution._retirement_observation = proof
        return require_ios_recovery_helper_retirement(proof, context)
    except IOSNativeRecoveryError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        raise IOSNativeRecoveryError('ios_recovery_helper_unconfirmed') from None
    finally:
        if transport is not None:
            transport.token = ''
        if query is not None:
            query.close(deadline_monotonic=min(deadline_monotonic, time.monotonic()+3))
