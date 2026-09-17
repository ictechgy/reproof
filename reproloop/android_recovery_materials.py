"""Bound APK copies for recovery after original staging was discarded."""
from contextlib import ExitStack
import hashlib
import os

from . import contracts
from .android_native_process import _bounds
from .repair_android_operation import (
    AndroidOperationError, _file_digest, _identity_info, _open_child_directory,
    _open_regular_at, _read_json_at, _replace_at, _require, _valid_identity, _write_new_at,
)
from .storage import MAX_APK


NAMES = ('original.apk', 'helper.apk')
RECORD = 'recovery-materials.json'


def _prepared_digest(record):
    return contracts.digest({key: value for key, value in record.items()
                             if key not in {'state', 'preparedDigest'}})


def validate_materials(directory, intent, state, *, full=True):
    if RECORD not in os.listdir(directory):
        return None
    record = _read_json_at(directory, RECORD, 16*1024)
    _require(type(record) is dict and set(record) == {
        'schemaVersion', 'operationId', 'requestDigest', 'contextDigest', 'configurationDigest',
        'bindingDigest', 'sourceFilesDigest', 'stagingIdentity', 'state', 'files', 'preparedDigest'}
        and type(record['schemaVersion']) is int and record['schemaVersion'] == 1
        and all(record[key] == intent[key] for key in
                ('operationId', 'requestDigest', 'contextDigest', 'configurationDigest', 'stagingIdentity'))
        and record['bindingDigest'] is not None and record['bindingDigest'] == state['nativeBindingDigest']
        and record['sourceFilesDigest'] == contracts.digest(intent['files'])
        and record['state'] in ('preparing', 'prepared', 'discarding', 'discarded')
        and type(record['files']) is dict and set(record['files']) <= set(NAMES)
        and 'discard.json' in os.listdir(directory), 'android_recovery_material_record')
    for identity in record['files'].values():
        _require(identity is None or _valid_identity(identity), 'android_recovery_material_record')
    if record['state'] == 'preparing':
        _require(record['preparedDigest'] is None, 'android_recovery_material_record')
    else:
        _require(all(record['files'].values()) and record['preparedDigest'] == _prepared_digest(record),
                 'android_recovery_material_record')
    staging = _open_child_directory(directory, 'staging', expected=intent['stagingIdentity'])
    try:
        names = set(os.listdir(staging))
        _require(names <= set(intent['files']), 'android_recovery_material_record')
        if record['state'] == 'prepared':
            _require(set(NAMES) <= names, 'android_recovery_material_record')
        if record['state'] == 'discarded':
            _require(not set(record['files']) & names, 'android_recovery_material_record')
        for name, identity in record['files'].items():
            if name not in names:
                _require(record['state'] in ('discarding', 'discarded')
                    or record['state'] == 'preparing' and identity is None,
                    'android_recovery_material_record')
                continue
            descriptor = _open_regular_at(staging, name, expected=identity)
            try:
                size = os.fstat(descriptor).st_size
                expected = intent['files'][name]
                if identity is None:
                    # Creation precedes its identity commit; no data is written
                    # before that commit. An unregistered nonempty file is refused.
                    _require(record['state'] == 'preparing' and size == 0,
                             'android_recovery_material_record')
                elif record['state'] == 'preparing':
                    _require(0 <= size <= expected['bytes'], 'android_recovery_material_record')
                else:
                    _require(size == expected['bytes'], 'android_recovery_material_record')
                    if full:
                        _require(_file_digest(descriptor, MAX_APK) == (expected['digest'], expected['bytes']),
                                 'android_recovery_material_changed')
            finally:
                os.close(descriptor)
        # Recreated payloads replace discarded bytes; they never add a second
        # APK slot or enlarge the original operation's reservation.
        total = sum(intent['files'][name]['bytes'] for name in names | set(record['files']))
        _require(total <= sum(item['bytes'] for item in intent['files'].values()),
                 'android_recovery_material_budget')
    finally:
        os.close(staging)
    return record


def expected_apk(directory, intent, state, name):
    _require(name in intent['files'], 'android_recovery_material_record')
    record = validate_materials(directory, intent, state, full=False)
    expected = dict(intent['files'][name])
    if record is not None and name in record['files']:
        _require(record['state'] == 'prepared', 'android_recovery_material_unprepared')
        expected['identity'] = record['files'][name]
    return expected


def _copy_apk(source, destination, expected, check):
    os.lseek(source, 0, os.SEEK_SET)
    os.ftruncate(destination, 0)
    os.lseek(destination, 0, os.SEEK_SET)
    total = 0
    digest = hashlib.sha256()
    while True:
        check()
        chunk = os.read(source, 1024*1024)
        if not chunk:
            break
        _require(total + len(chunk) <= expected['bytes'], 'android_recovery_material_source')
        offset = 0
        while offset < len(chunk):
            check()
            written = os.write(destination, chunk[offset:])
            _require(written > 0, 'android_recovery_material_source')
            offset += written
        total += len(chunk)
        digest.update(chunk)
    os.fsync(destination)
    _require((digest.hexdigest(), total) == (expected['digest'], expected['bytes'])
        and _file_digest(destination, MAX_APK) == (expected['digest'], expected['bytes']),
        'android_recovery_material_source')


def prepare_recovery_materials(operations, recovery, *, cancellation, deadline_monotonic):
    def check():
        operations.require_recovery_descriptors(recovery)
        _bounds(cancellation, deadline_monotonic)
        recovery._device._authority._require_parent_grant(recovery._lease._grant)
    staging = None
    try:
        check()
        directory, intent = recovery.operation_directory_fd, recovery._files.intent
        state = operations._state(recovery.operation_id, directory)
        status = operations._stage_status(directory, intent, state, full=True)
        _require(status in ('discard-intent-uncommitted', 'staged-discard-incomplete',
            'discard-state-uncommitted', 'staged-discarded', 'recovery-copy-incomplete',
            'recovery-apks-prepared'), 'android_recovery_material_stage')
        record = validate_materials(directory, intent, state)
        if record is not None and record['state'] == 'prepared':
            return record
        _require(record is None or record['state'] == 'preparing', 'android_recovery_material_stage')
        staging = _open_child_directory(directory, 'staging', expected=intent['stagingIdentity'])
        planned = (tuple(name for name in NAMES if name not in os.listdir(staging))
                   if record is None else tuple(record['files']))
        with ExitStack() as stack:
            sources = {}
            for name in planned:
                source = operations.config.original_apk if name == 'original.apk' else operations.config.helper_apk
                descriptor, digest, size = operations._source(source, MAX_APK)
                stack.callback(os.close, descriptor)
                _require((digest, size) == (intent['files'][name]['digest'], intent['files'][name]['bytes']),
                         'android_recovery_material_source')
                sources[name] = descriptor
            if record is None:
                record = {'schemaVersion': 1, 'operationId': recovery.operation_id,
                    'requestDigest': recovery.request_digest, 'contextDigest': recovery.context_digest,
                    'configurationDigest': recovery.configuration_digest, 'bindingDigest': recovery.binding_digest,
                    'sourceFilesDigest': contracts.digest(intent['files']), 'stagingIdentity': intent['stagingIdentity'],
                    'state': 'preparing', 'files': {name: None for name in planned}, 'preparedDigest': None}
                _write_new_at(directory, RECORD, record)
            for name in planned:
                check()
                identity = record['files'][name]
                if name not in os.listdir(staging):
                    _require(identity is None, 'android_recovery_material_record')
                    destination = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                          0o600, dir_fd=staging)
                    os.fsync(staging)
                else:
                    destination = _open_regular_at(staging, name, expected=identity, writable=True)
                try:
                    if identity is None:
                        _require(os.fstat(destination).st_size == 0, 'android_recovery_material_record')
                        record['files'][name] = _identity_info(os.fstat(destination))
                        _replace_at(directory, RECORD, record)
                    _copy_apk(sources[name], destination, intent['files'][name], check)
                    os.fsync(staging)
                finally:
                    os.close(destination)
        check()
        record['state'] = 'prepared'
        record['preparedDigest'] = _prepared_digest(record)
        _replace_at(directory, RECORD, record)
        validate_materials(directory, intent, state)
        return record
    except OSError:
        raise AndroidOperationError('android_recovery_material_unavailable') from None
    finally:
        if staging is not None:
            os.close(staging)
