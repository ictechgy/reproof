"""Bounded, journal-bound storage for the fixed Android process guardian.

Files describe attempted host work. They never authorize device cleanup or
RunStore release. An unresolved slot survives caller and process failure.
"""
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import plistlib

from . import contracts
from .repair_android_operation import (
    AndroidOperationError, _require, _identity_info, _valid_identity,
    _open_child_directory, _open_regular_at, _read_fd, _read_json_at,
    _replace_at, _write_new_at,
)


MAX_OUTPUT_BYTES = 4 * 1024**2
MAX_INPUT_BYTES = 64 * 1024
MAX_COMMAND_BYTES = 128 * 1024
MAX_REQUEST_BYTES = 64 * 1024
_STATE_BYTES = 64 * 1024
_SLOTS = ('command', 'instrumentation')
_LIMITS = {
    'request.plist': MAX_REQUEST_BYTES, 'command.plist': MAX_COMMAND_BYTES,
    'stdin.bin': MAX_INPUT_BYTES, 'start.json': 4096, 'termination.json': 4096,
    'native-result.plist': 2 * MAX_OUTPUT_BYTES + 4096,
}
# Both slots may be live at once. Include an atomic state replacement and
# filesystem bookkeeping headroom before admission; payload is never unreserved.
RESERVED_BYTES = len(_SLOTS) * sum(_LIMITS.values()) + 2 * _STATE_BYTES + 64 * 1024


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def reservation(intent):
    value = intent.get('nativeCalls')
    if value is None:
        return 0
    _require(type(value) is dict and set(value) == {'schemaVersion', 'reservedBytes', 'identity', 'slots'}
        and type(value['schemaVersion']) is int and value['schemaVersion'] == 1
        and type(value['reservedBytes']) is int and value['reservedBytes'] == RESERVED_BYTES
        and _valid_identity(value['identity'], directory=True)
        and type(value['slots']) is dict and set(value['slots']) == set(_SLOTS)
        and all(_valid_identity(item, directory=True) for item in value['slots'].values())
        and 'adbEndpointDigest' in intent['configuration'], 'android_native_workspace')
    return RESERVED_BYTES


def initialize(operation_fd, context):
    os.mkdir('native-calls', mode=0o700, dir_fd=operation_fd)
    directory = _open_child_directory(operation_fd, 'native-calls')
    try:
        slots = {}
        for name in _SLOTS:
            os.mkdir(name, mode=0o700, dir_fd=directory)
            slot = _open_child_directory(directory, name)
            try:
                slots[name] = _identity_info(os.fstat(slot))
                os.fsync(slot)
            finally:
                os.close(slot)
        _write_new_at(directory, 'state.json', {
            'schemaVersion': 1, 'operationId': context.operation_id,
            'contextDigest': context.digest, 'nextSequence': 1,
            'historyDigest': '0' * 64, 'slots': {name: None for name in _SLOTS},
        })
        os.fsync(directory)
        result = {'schemaVersion': 1, 'reservedBytes': RESERVED_BYTES,
                  'identity': _identity_info(os.fstat(directory)), 'slots': slots}
    finally:
        os.close(directory)
    os.fsync(operation_fd)
    return result


def _state(directory, intent):
    state = _read_json_at(directory, 'state.json', _STATE_BYTES)
    _require(type(state) is dict and set(state) == {
        'schemaVersion', 'operationId', 'contextDigest', 'nextSequence', 'historyDigest', 'slots'}
        and type(state['schemaVersion']) is int and state['schemaVersion'] == 1
        and state['operationId'] == intent['operationId']
        and state['contextDigest'] == intent['contextDigest']
        and type(state['nextSequence']) is int and 1 <= state['nextSequence'] < 2**63
        and type(state['slots']) is dict and set(state['slots']) == set(_SLOTS),
        'android_native_workspace')
    try:
        contracts.validate_digest(state['historyDigest'])
    except (TypeError, ValueError, contracts.ContractError):
        raise AndroidOperationError('android_native_workspace') from None
    return state


def _slot_record(record, state):
    _require(type(record) is dict and set(record) == {
        'sequence', 'phase', 'replayNumber', 'bindingDigest', 'ownershipGeneration',
        'hostIncarnation', 'helperIncarnation', 'requestDigest', 'commandDigest',
        'stdinDigest', 'maximumOutputBytes', 'state', 'files'}
        and type(record['sequence']) is int and 1 <= record['sequence'] < state['nextSequence']
        and type(record['phase']) is str and record['phase'] in {'install', 'replay', 'cleanup'}
        and ((record['phase'] == 'replay' and type(record['replayNumber']) is int
              and 1 <= record['replayNumber'] <= 128)
             or (record['phase'] != 'replay' and record['replayNumber'] is None))
        and type(record['ownershipGeneration']) is int and 1 <= record['ownershipGeneration'] < 2**63
        and type(record['maximumOutputBytes']) is int
        and 1 <= record['maximumOutputBytes'] <= MAX_OUTPUT_BYTES
        and type(record['state']) is str and record['state'] in {'preparing', 'prepared', 'retiring'}
        and type(record['files']) is dict
        and (not record['files'] if record['state'] == 'preparing'
             else set(record['files']) == set(_LIMITS)), 'android_native_workspace')
    try:
        for name in ('bindingDigest', 'requestDigest', 'commandDigest', 'stdinDigest'):
            contracts.validate_digest(record[name])
        for name in ('hostIncarnation', 'helperIncarnation'):
            contracts.validate_id(record[name])
    except (TypeError, ValueError, contracts.ContractError):
        raise AndroidOperationError('android_native_workspace') from None
    _require(all(_valid_identity(item) for item in record['files'].values()), 'android_native_workspace')


def validate_workspace(operation_fd, intent):
    """Inspect bounded original files; a host intent is never cleanup evidence."""
    if not reservation(intent):
        _require('native-calls' not in os.listdir(operation_fd), 'android_native_workspace')
        return 'idle'
    descriptor = intent['nativeCalls']
    directory = _open_child_directory(operation_fd, 'native-calls', expected=descriptor['identity'])
    try:
        _require(set(os.listdir(directory)) == {'state.json', *_SLOTS}, 'android_native_workspace')
        state = _state(directory, intent)
        sequences = set()
        for name in _SLOTS:
            slot = _open_child_directory(directory, name, expected=descriptor['slots'][name])
            try:
                names = set(os.listdir(slot))
                record = state['slots'][name]
                if record is None:
                    _require(not names, 'android_native_workspace')
                    continue
                _slot_record(record, state)
                _require(record['sequence'] not in sequences, 'android_native_workspace')
                sequences.add(record['sequence'])
                _require(names <= set(_LIMITS) and
                    (record['state'] != 'prepared' or names == set(_LIMITS)), 'android_native_workspace')
                for filename in names:
                    opened = _open_regular_at(slot, filename, expected=record['files'].get(filename))
                    try:
                        _require(0 <= os.fstat(opened).st_size <= _LIMITS[filename], 'android_native_workspace')
                        if record['state'] == 'prepared' and filename in {'request.plist', 'command.plist', 'stdin.bin'}:
                            key = {'request.plist': 'requestDigest', 'command.plist': 'commandDigest', 'stdin.bin': 'stdinDigest'}[filename]
                            _require(_sha(_read_fd(opened, _LIMITS[filename], allow_empty=filename == 'stdin.bin'))
                                     == record[key], 'android_native_workspace')
                    finally:
                        os.close(opened)
            finally:
                os.close(slot)
        return 'native-call-unresolved' if sequences else 'idle'
    finally:
        os.close(directory)


@dataclass(slots=True)
class AndroidNativeCall:
    work_root: Path
    slot: str
    sequence: int
    request_digest: str
    _operations: object = field(repr=False)
    _descriptors: object = field(repr=False)


def _write_bytes(directory, name, body):
    _require(type(body) is bytes and len(body) <= _LIMITS[name], 'android_native_input')
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        offset = 0
        while offset < len(body):
            count = os.write(descriptor, body[offset:])
            _require(count > 0, 'android_native_storage')
            offset += count
        os.fsync(descriptor)
        return _identity_info(os.fstat(descriptor))
    finally:
        os.close(descriptor)


def prepare_call(operations, descriptors, command, *, input_bytes=b'', slot='command',
                 max_output_bytes=MAX_OUTPUT_BYTES):
    """Persist exact SDK intent before a guardian may inherit any descriptor."""
    from .adb_endpoint import adb_client_sandbox
    operations.require_native_descriptors(descriptors)
    config = operations.config
    work = operations._root(descriptors.operation_id) / 'staging'
    _require(slot in _SLOTS and type(command) is tuple and 9 <= len(command) <= 128
        and all(type(item) is str and '\0' not in item and len(item) <= 32768 for item in command)
        and command[:2] == ('/usr/bin/sandbox-exec', '-p')
        and command[3:5] == (str(config.tools.adb), '-L')
        and command[5].startswith('localfilesystem:/')
        and command[6:8] == ('-s', config.serial)
        and command[8] in {'devices', 'shell', 'exec-out', 'install'}
        and type(input_bytes) is bytes and len(input_bytes) <= MAX_INPUT_BYTES
        and type(max_output_bytes) is int and 1 <= max_output_bytes <= MAX_OUTPUT_BYTES,
        'android_native_input')
    gateway = Path(command[5][len('localfilesystem:'):])
    _require(gateway.is_absolute() and str(gateway.resolve(strict=True)) == str(gateway)
        and command[2] == adb_client_sandbox(config.tools.adb, work, gateway), 'android_native_input')
    from .android_recovery import require_command
    require_command(operations,descriptors,command=command[8:],input_bytes=input_bytes,slot=slot)
    return _prepare_request(operations,descriptors,command,input_bytes=input_bytes,
                            slot=slot,max_output_bytes=max_output_bytes)


def prepare_inspector_call(operations,descriptors,apk,*,max_output_bytes=MAX_OUTPUT_BYTES):
    from .android_inspector import inspector_command
    command,fields=inspector_command(operations,descriptors,apk)
    return _prepare_request(operations,descriptors,command,input_bytes=b'',slot='command',
                            max_output_bytes=max_output_bytes,inspector_fields=fields)


def _prepare_request(operations,descriptors,command,*,input_bytes,slot,max_output_bytes,inspector_fields=None):
    config=operations.config
    work=operations._root(descriptors.operation_id)/'staging'
    _require(type(max_output_bytes) is int and 1<=max_output_bytes<=MAX_OUTPUT_BYTES,'android_native_input')
    encoded = plistlib.dumps(command, fmt=plistlib.FMT_BINARY)
    _require(len(encoded) <= MAX_COMMAND_BYTES, 'android_native_input')
    with operations._mutex:
        operations.require_native_descriptors(descriptors)
        intent = operations._intent(descriptors.operation_id, descriptors.operation_directory_fd)
        _require(reservation(intent) == RESERVED_BYTES, 'android_native_workspace')
        validate_workspace(descriptors.operation_directory_fd, intent)
        directory = _open_child_directory(descriptors.operation_directory_fd, 'native-calls',
                                         expected=intent['nativeCalls']['identity'])
        try:
            state = _state(directory, intent)
            _require(state['slots'][slot] is None and state['nextSequence'] < 2**63 - 1,
                     'android_native_slot_busy')
            root = work.parent / 'native-calls' / slot
            request = {
                'schemaVersion': '1', 'operationId': descriptors.operation_id,
                'requestDigest': descriptors.request_digest, 'contextDigest': descriptors.context_digest,
                'scopeDigest': descriptors.scope_digest, 'definitionDigest': descriptors.configuration_digest,
                'workPath': str(root), 'commandDigest': _sha(encoded), 'childWorkPath': str(work),
                'maxOutputBytes': str(max_output_bytes), 'nativeBindingDigest': descriptors.binding_digest,
                'ownershipGeneration': str(descriptors.ownership_generation),
                'hostIncarnation': descriptors.host_incarnation, 'helperIncarnation': descriptors.helper_incarnation,
                'adbPath': str(config.tools.adb), 'adbSha256': config.tools.adb_digest,
                'sandboxSha256': config.adb_endpoint.sandbox_sha256, 'stdinDigest': _sha(input_bytes),
            }
            if inspector_fields is not None:
                _require(set(inspector_fields)=={'toolKind','packageInspectorPath','packageInspectorSha256',
                    'packageInspectorSupport','apkPath','apkSha256','apkBytes'},'android_native_input')
                request.update(inspector_fields)
            request_bytes = plistlib.dumps(request, fmt=plistlib.FMT_BINARY)
            _require(len(request_bytes) <= MAX_REQUEST_BYTES, 'android_native_input')
            record = {
                'sequence': state['nextSequence'], 'phase': descriptors._phase.phase,
                'replayNumber': descriptors._phase.replay_number, 'bindingDigest': descriptors.binding_digest,
                'ownershipGeneration': descriptors.ownership_generation,
                'hostIncarnation': descriptors.host_incarnation, 'helperIncarnation': descriptors.helper_incarnation,
                'requestDigest': _sha(request_bytes), 'commandDigest': _sha(encoded), 'stdinDigest': _sha(input_bytes),
                'maximumOutputBytes': max_output_bytes, 'state': 'preparing', 'files': {},
            }
            state['nextSequence'] += 1
            state['slots'][slot] = record
            _replace_at(directory, 'state.json', state)
            slot_fd = _open_child_directory(directory, slot, expected=intent['nativeCalls']['slots'][slot])
            try:
                for name, body in (('request.plist', request_bytes), ('command.plist', encoded),
                                   ('stdin.bin', input_bytes), ('start.json', b''),
                                   ('termination.json', b''), ('native-result.plist', b'')):
                    record['files'][name] = _write_bytes(slot_fd, name, body)
                os.fsync(slot_fd)
            finally:
                os.close(slot_fd)
            record['state'] = 'prepared'
            operations.require_native_descriptors(descriptors)
            _replace_at(directory, 'state.json', state)
            return AndroidNativeCall(root, slot, record['sequence'], record['requestDigest'], operations, descriptors)
        finally:
            os.close(directory)
