"""Persistent iOS signing staging and lock-proven cleanup authority.

This journal owns the IPA snapshot and app namespace. Staging does not issue
a signature, provisioning approval or mobile qualification. Native dispatch
is added only through the fixed owner, after these ownership checks.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import ctypes
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
import time

from . import contracts
from .execution.artifacts import BlobSet, read_regular
from .execution.journal import MAX_RUNS, RunStore, TERMINAL
from .execution.wire import MAX_TRANSFER_BYTES, decode_json
from .ios_artifact_transfer import (_app_capability, _opened_ipa_contents, MAX_APP_ENTRIES,
    MAX_CODE_OBJECTS, MAX_EXPANDED_APP_BYTES, _file_stat_signature, parse_ios_artifact)
from .ios_code_signature import _remove_owned_contents
from .ios_signing_inputs import IOSSigningDefinition, IOSSigningOwnerTools
from .repair_signing_recovery import (SigningCleanupCapability, SigningOperationStore,
    SigningRecoveryError, _context_common, _open_child_directory, _open_owned_regular,
    _private_directory, _read_at, _replace_at, _stat_identity, _valid_identity, _validate_context,
    _walk_directory)


_FILES = ('producer.lock', 'owner.lock', 'start.json', 'termination.json',
          'request.plist', 'incoming.ipa', 'signed.ipa')
_VERIFICATION_FILES = ('arguments.plist', 'native-result.plist')
_TRANSIENT = re.compile(r'\.state-[0-9a-f]{32}\Z')
_PHASES = {'intent', 'receiving', 'extracting', 'moving', 'staged', 'cleaning', 'cleaned'}


def _require(value, code='ios_signing_journal'):
    if not value:
        raise SigningRecoveryError(code)


def _same_directory(actual, expected):
    return (_valid_identity(expected, directory=True) and
            {k: v for k, v in _stat_identity(actual).items() if k != 'links'} ==
            {k: v for k, v in expected.items() if k != 'links'})


def _write_bytes(descriptor, body):
    os.ftruncate(descriptor, 0)
    offset = 0
    while offset < len(body):
        count = os.pwrite(descriptor, body[offset:offset+1024*1024], offset)
        _require(count > 0)
        offset += count
    os.fsync(descriptor)


def _move_new_app(source_directory, destination_directory):
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renameatx_np
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    _require(rename(source_directory, b'app', destination_directory, b'App.app', 4) == 0,
             'ios_signing_staging_collision')


@dataclass(slots=True)
class IOSSigningOperation:
    store: object = field(repr=False)
    run: object = field(repr=False)
    context: object = field(repr=False)
    request_digest: str
    _issuer: object = field(repr=False)
    _pid: int = field(repr=False)
    _active: bool = field(default=True, repr=False)

    @property
    def operation_id(self):
        return self.context.operation_id


class IOSSigningOperationStore:
    def __init__(self, run_store, tools, definition, work_root, *, create=True):
        _require(type(run_store) is RunStore and type(tools) is IOSSigningOwnerTools
            and type(definition) is IOSSigningDefinition and type(create) is bool)
        self.run_store, self.tools, self.definition = run_store, tools, definition
        self._read_only = False
        self._configuration_sha256 = None
        self._application_id = definition.identity.application_id
        self._native_enabled = tools.guardian is not None
        self._files = _FILES + (_VERIFICATION_FILES if self._native_enabled else ())
        self.scope_digest = definition.identity.scope_digest
        self.minimum_operation_bytes = (2*MAX_EXPANDED_APP_BYTES + 2*MAX_TRANSFER_BYTES
            + definition.profile_bytes_total + 4*1024*1024)
        _require(run_store.disk_limit >= self.minimum_operation_bytes)
        self.root = Path(work_root)
        _require(self.root.is_absolute() and '..' not in self.root.parts)
        self._configuration = {'schemaVersion': 1, 'kind': 'ios-signing-operations',
            'scopeDigest': self.scope_digest, 'toolsDigest': tools.definition_digest,
            'definitionDigest': definition.definition_digest,
            'runRootDigest': contracts.digest(str(run_store.root)),
            'environmentDigest': run_store.environment_digest, 'diskBytes': self.minimum_operation_bytes}
        if self._native_enabled: self._configuration['nativeLayout'] = 1
        self.definition_digest = contracts.digest(self._configuration)
        self._initialize_memory()
        _private_directory(self.root, create=create)
        root_fd = _walk_directory(self.root)
        try:
            names = set(os.listdir(root_fd))
            if create and not names:
                os.mkdir('operations', mode=0o700, dir_fd=root_fd)
                fd = os.open('control.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
                os.close(fd)
                _replace_at(root_fd, 'configuration.json', self._configuration)
            else:
                _require(names == {'operations', 'control.lock', 'configuration.json'})
                _require(_read_at(root_fd, 'configuration.json') == self._configuration)
            fd = _open_owned_regular(root_fd, 'control.lock', writable=True, zero=True); os.close(fd)
            self._root_identity = _stat_identity(os.fstat(root_fd))
            operations = _open_child_directory(root_fd, 'operations')
            self._operations_identity = _stat_identity(os.fstat(operations)); os.close(operations)
        finally:
            os.close(root_fd)

    def _initialize_memory(self):
        self._mutex = threading.RLock()
        self._changed = threading.Condition(self._mutex)
        self._issuer = object()
        self._active = {}
        self._callbacks = set()
        self._native_controls = set()
        self._recoveries = {}
        self._closed = False

    @property
    def recovery_only(self):
        return self._read_only

    @staticmethod
    def _configuration_bytes(root):
        descriptor = _open_owned_regular(root, 'configuration.json')
        try:
            before = os.fstat(descriptor)
            _require(0 < before.st_size <= 64*1024)
            data = bytearray()
            while len(data) <= 64*1024:
                block = os.read(descriptor, min(65536, 64*1024+1-len(data)))
                if not block: break
                data.extend(block)
            after = os.fstat(descriptor)
            _require(len(data) == before.st_size and (before.st_size,before.st_mtime_ns,before.st_ctime_ns)
                == (after.st_size,after.st_mtime_ns,after.st_ctime_ns))
            return bytes(data)
        finally: os.close(descriptor)

    def recovery_configuration(self):
        with self._root() as root:
            raw = self._configuration_bytes(root)
            _require(decode_json(raw) == self._configuration)
        return {'schemaVersion':1,'kind':'ios-signing-recovery-v1','applicationId':self._application_id,
            'runStorePath':str(self.run_store.root),'ownerRoot':str(self.root),
            'environmentDigest':self.run_store.environment_digest,'diskBudgetBytes':self.run_store.disk_limit,
            'operationDiskBytes':self.minimum_operation_bytes,'ownerConfigurationSha256':hashlib.sha256(raw).hexdigest(),
            'ownerDefinitionDigest':self.definition_digest,'scopeDigest':self.scope_digest,
            'inputsDefinitionDigest':self._configuration['definitionDigest'],'toolsDigest':self._configuration['toolsDigest']}

    @classmethod
    def open_for_recovery(cls, configuration):
        from .ios_signing_configuration import IOSSigningRecoveryConfiguration
        _require(cls is IOSSigningOperationStore and type(configuration) is IOSSigningRecoveryConfiguration)
        value = configuration.document
        store = RunStore(value['runStorePath'], environment_digest=value['environmentDigest'],
                         disk_limit=value['diskBudgetBytes'], create=False)
        _require(store._load().get('scope') == {'kind':'signing','scopeDigest':value['scopeDigest']})
        root_path = Path(value['ownerRoot'])
        root = _walk_directory(root_path)
        try:
            _require(set(os.listdir(root)) == {'operations','control.lock','configuration.json'})
            raw = cls._configuration_bytes(root)
            _require(hashlib.sha256(raw).hexdigest() == value['ownerConfigurationSha256'])
            stored = decode_json(raw)
            _require(type(stored) is dict and type(stored.get('schemaVersion')) is int
                     and type(stored.get('diskBytes')) is int)
            expected = {'schemaVersion':1,'kind':'ios-signing-operations',
                'scopeDigest':value['scopeDigest'],'toolsDigest':value['toolsDigest'],
                'definitionDigest':value['inputsDefinitionDigest'],'runRootDigest':contracts.digest(str(store.root)),
                'environmentDigest':value['environmentDigest'],'diskBytes':value['operationDiskBytes']}
            if 'nativeLayout' in stored:
                _require(type(stored['nativeLayout']) is int and stored['nativeLayout'] == 1)
                expected['nativeLayout'] = 1
            _require(stored == expected and contracts.digest(stored) == value['ownerDefinitionDigest'])
            control = _open_owned_regular(root,'control.lock',writable=False,zero=True); os.close(control)
            operations = _open_child_directory(root,'operations')
            try: operations_identity = _stat_identity(os.fstat(operations))
            finally: os.close(operations)
            result = cls.__new__(cls)
            result.run_store, result.tools, result.definition = store, None, None
            result._read_only = True
            result._configuration_sha256 = value['ownerConfigurationSha256']
            result._application_id = value['applicationId']
            result._configuration = stored
            result._native_enabled = 'nativeLayout' in stored
            result._files = _FILES + (_VERIFICATION_FILES if result._native_enabled else ())
            result.minimum_operation_bytes = value['operationDiskBytes']
            result.scope_digest = value['scopeDigest']; result.definition_digest = value['ownerDefinitionDigest']
            result.root = root_path
            result._root_identity = _stat_identity(os.fstat(root))
            result._operations_identity = operations_identity
            result._initialize_memory()
            return result
        finally: os.close(root)

    @contextmanager
    def _root(self):
        descriptor = _walk_directory(self.root, expected=self._root_identity)
        try:
            raw = self._configuration_bytes(descriptor)
            _require(decode_json(raw) == self._configuration)
            if self._read_only:
                _require(hashlib.sha256(raw).hexdigest() == self._configuration_sha256)
            else:
                _require(self.definition.definition_digest == self._configuration['definitionDigest']
                    and self.tools.definition_digest == self._configuration['toolsDigest'])
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _control(self):
        with self._mutex, self._root() as root:
            descriptor = _open_owned_regular(root, 'control.lock', writable=True, zero=True)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)

    def operation_root(self, operation_id):
        contracts.validate_id(operation_id)
        return self.root/'operations'/operation_id

    @contextmanager
    def _operation_directory(self, operation_id):
        contracts.validate_id(operation_id)
        with self._root() as root:
            parent = _open_child_directory(root, 'operations', expected=self._operations_identity)
            descriptor = None
            try:
                descriptor = _open_child_directory(parent, operation_id)
                yield descriptor
            finally:
                if descriptor is not None: os.close(descriptor)
                os.close(parent)

    def _records(self, descriptor, operation_id, request_digest=None):
        intent = _read_at(descriptor, 'intent.json')
        state = _read_at(descriptor, 'state.json')
        intent_keys = {'schemaVersion', 'operationId', 'requestDigest',
            'contextDigest', 'context', 'definitionDigest', 'scopeDigest', 'diskBytes', 'rootIdentity',
            'transferIdentity', 'fileIdentities'}
        state_keys = {'schemaVersion', 'operationId', 'requestDigest',
            'phase', 'inputDigest', 'inputBytes', 'appIdentity', 'appDigest', 'recovery'}
        if self._native_enabled:
            intent_keys.add('checksIdentity'); state_keys.add('execution')
        _require(type(intent) is dict and set(intent) == intent_keys)
        _require(type(state) is dict and set(state) == state_keys)
        _require(type(intent['schemaVersion']) is int and intent['schemaVersion'] == 1
            and type(state['schemaVersion']) is int and state['schemaVersion'] == 1
            and intent['operationId'] == state['operationId'] == operation_id
            and intent['requestDigest'] == state['requestDigest']
            and (request_digest is None or intent['requestDigest'] == request_digest)
            and intent['definitionDigest'] == self.definition_digest and intent['scopeDigest'] == self.scope_digest
            and type(intent['diskBytes']) is int and intent['diskBytes'] == self.minimum_operation_bytes
            and _same_directory(os.fstat(descriptor), intent['rootIdentity'])
            and _valid_identity(intent['transferIdentity'], directory=True)
            and type(intent['fileIdentities']) is dict and set(intent['fileIdentities']) == set(self._files)
            and all(_valid_identity(value) for value in intent['fileIdentities'].values())
            and type(state['phase']) is str and state['phase'] in _PHASES
            and type(state['inputBytes']) is int and 0 <= state['inputBytes'] <= MAX_TRANSFER_BYTES
            and (state['appIdentity'] is None or _valid_identity(state['appIdentity'], directory=True))
            and state['recovery'] in (None, 'sanitized'))
        for value in (intent['requestDigest'], intent['contextDigest']): contracts.validate_digest(value)
        execution = state.get('execution')
        if self._native_enabled:
            _require(_valid_identity(intent['checksIdentity'], directory=True))
            if execution is not None:
                _require(type(execution) is dict and set(execution) == {'role', 'contextDigest', 'policyDigest',
                    'provisioningDigest', 'sequence', 'historyDigest', 'active', 'signedDigest', 'signedBytes', 'complete'}
                    and execution['role'] in {'sign', 'inspect'}
                    and type(execution['sequence']) is int and 0 <= execution['sequence'] <= 40000
                    and type(execution['complete']) is bool
                    and type(execution['signedBytes']) is int and 0 <= execution['signedBytes'] <= MAX_TRANSFER_BYTES)
                for key in ('contextDigest', 'policyDigest', 'provisioningDigest', 'historyDigest'):
                    contracts.validate_digest(execution[key])
                if execution['signedDigest'] is not None: contracts.validate_digest(execution['signedDigest'])
                if execution['active'] is not None:
                    _require(type(execution['active']) is dict and set(execution['active']) == {'kind', 'contextDigest', 'commandDigest'}
                        and execution['active']['kind'] in {'sign', 'verify'})
                    contracts.validate_digest(execution['active']['contextDigest'])
                    contracts.validate_digest(execution['active']['commandDigest'])
        for value in (state['inputDigest'], state['appDigest']):
            if value is not None: contracts.validate_digest(value)
        context = intent['context']
        _require(type(context) is dict and set(context) == {'operationId', 'repairPlanDigest', 'projectDigest',
            'applicationId', 'sourceDigest', 'unsignedArtifactDigest', 'signingPolicyDigest'}
            and context['operationId'] == operation_id and context['applicationId'] == self._application_id)
        for name in ('repairPlanDigest', 'projectDigest', 'sourceDigest', 'unsignedArtifactDigest', 'signingPolicyDigest'):
            contracts.validate_digest(context[name])
        _require((state['appIdentity'] is None) == (state['appDigest'] is None))
        if state['inputDigest'] is None:
            _require(state['inputBytes'] == 0 and state['appIdentity'] is None)
        else:
            expected_input = (execution['signedDigest'] if execution is not None and execution['role'] == 'inspect'
                              else context['unsignedArtifactDigest'])
            _require(state['inputDigest'] == expected_input and state['inputBytes'] > 0)
        if state['phase'] == 'intent':
            _require(state['inputDigest'] is None and state['appIdentity'] is None)
        if state['phase'] in {'receiving', 'extracting'}:
            _require(state['inputDigest'] is not None and state['appIdentity'] is None)
        if state['phase'] in {'moving', 'staged'}:
            _require(state['inputDigest'] is not None and state['appIdentity'] is not None)
        _require(state['phase'] in {'cleaning', 'cleaned'} or state['recovery'] is None)
        if state['phase'] == 'cleaned': _require(state['recovery'] == 'sanitized')
        return intent, state

    @contextmanager
    def admit(self, context, request_digest):
        _require(not self._read_only, 'ios_signing_recovery_only')
        _validate_context(context); contracts.validate_digest(request_digest)
        _require(context.signed_artifact_digest is None and context.application_id == self.definition.identity.application_id)
        with self.run_store.repair_scope_lease('signing', self.scope_digest):
            self.run_store.require_available()
            with self._control(), self._root() as root:
                _require(not self._closed and not self._active)
                parent = _open_child_directory(root, 'operations', expected=self._operations_identity)
                descriptor = None
                try:
                    _require(len(os.listdir(parent)) < MAX_RUNS)
                    os.mkdir(context.operation_id, mode=0o700, dir_fd=parent)
                    descriptor = _open_child_directory(parent, context.operation_id)
                    files = {}
                    for name in self._files:
                        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
                        files[name] = _stat_identity(os.fstat(fd)); os.close(fd)
                    os.mkdir('transfer', mode=0o700, dir_fd=descriptor)
                    transfer = _open_child_directory(descriptor, 'transfer')
                    transfer_identity = _stat_identity(os.fstat(transfer)); os.close(transfer)
                    intent = {'schemaVersion': 1, 'operationId': context.operation_id, 'requestDigest': request_digest,
                        'contextDigest': context.digest, 'context': _context_common(context), 'definitionDigest': self.definition_digest,
                        'scopeDigest': self.scope_digest, 'diskBytes': self.minimum_operation_bytes,
                        'rootIdentity': _stat_identity(os.fstat(descriptor)), 'transferIdentity': transfer_identity,
                        'fileIdentities': files}
                    state = {'schemaVersion': 1, 'operationId': context.operation_id,
                        'requestDigest': request_digest, 'phase': 'intent', 'inputDigest': None, 'inputBytes': 0,
                        'appIdentity': None, 'appDigest': None, 'recovery': None}
                    if self._native_enabled:
                        os.mkdir('checks', mode=0o700, dir_fd=descriptor)
                        checks = _open_child_directory(descriptor, 'checks')
                        intent['checksIdentity'] = _stat_identity(os.fstat(checks)); os.close(checks)
                        state['execution'] = None
                    _replace_at(descriptor, 'intent.json', intent)
                    _replace_at(descriptor, 'state.json', state)
                    os.fsync(descriptor); os.fsync(parent)
                finally:
                    if descriptor is not None: os.close(descriptor)
                    os.close(parent)
            with self.run_store.admit(context.operation_id, request_digest, disk_bytes=self.minimum_operation_bytes) as run:
                operation = IOSSigningOperation(self, run, context, request_digest, self._issuer, os.getpid())
                with self._mutex: self._active[context.operation_id] = operation
                try:
                    yield operation
                finally:
                    operation._active = False
                    with self._changed:
                        self._active.pop(context.operation_id, None); self._changed.notify_all()

    def _require_operation(self, operation):
        _require(not self._read_only, 'ios_signing_recovery_only')
        with self._mutex:
            _require(type(operation) is IOSSigningOperation and operation.store is self
                and operation._issuer is self._issuer and operation._pid == os.getpid()
                and operation._active and self._active.get(operation.operation_id) is operation and not self._closed,
                'ios_signing_operation_unavailable')
        row = self.run_store.status(operation.operation_id)
        _require(row['requestDigest'] == operation.request_digest and row['state'] == 'admitted'
            and not operation.run.cancelled(), 'ios_signing_operation_unavailable')

    @contextmanager
    def _producer(self, descriptor, intent):
        handles = []
        try:
            for name in ('producer.lock', 'owner.lock'):
                fd = _open_owned_regular(descriptor, name, expected=intent['fileIdentities'][name], writable=True, zero=True)
                handles.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield tuple(handles)
        finally:
            # Closing a parent copy must not unlock a descriptor inherited by a native owner.
            for fd in reversed(handles): os.close(fd)

    def stage(self, operation, artifacts):
        self._require_operation(operation)
        callback = object()
        with self._mutex:
            _require(not self._closed, 'ios_signing_operation_unavailable')
            self._callbacks.add(callback)
        try:
            return self._stage(operation, artifacts)
        finally:
            with self._changed:
                self._callbacks.discard(callback); self._changed.notify_all()

    def _stage(self, operation, artifacts, *, _expected_digest=None, _already_locked=False):
        _require(type(artifacts) is BlobSet and len(artifacts.entries) == 1 and artifacts.entries[0][0] == 'candidate.ipa')
        body = artifacts.entries[0][1]; measured = hashlib.sha256(body).hexdigest()
        expected = operation.context.unsigned_artifact_digest if _expected_digest is None else _expected_digest
        _require(measured == expected and body, 'ios_signing_artifact')
        with self._operation_directory(operation.operation_id) as descriptor:
            intent, state = self._records(descriptor, operation.operation_id, operation.request_digest)
            from contextlib import nullcontext
            with (nullcontext() if _already_locked else self._producer(descriptor, intent)):
                self._require_operation(operation)
                intent, state = self._records(descriptor, operation.operation_id, operation.request_digest)
                _require(state['phase'] == 'intent' and intent['contextDigest'] == operation.context.digest
                    and intent['context'] == _context_common(operation.context))
                state.update(phase='receiving', inputDigest=measured, inputBytes=len(body))
                _replace_at(descriptor, 'state.json', state)
                incoming = _open_owned_regular(descriptor, 'incoming.ipa',
                    expected=intent['fileIdentities']['incoming.ipa'], writable=True, zero=True)
                try: _write_bytes(incoming, body)
                finally: os.close(incoming)
                state['phase'] = 'extracting'; _replace_at(descriptor, 'state.json', state)
                transfer_fd = _open_child_directory(descriptor, 'transfer', expected=intent['transferIdentity'])
                source_fd = None
                try:
                    _require(not os.listdir(transfer_fd))
                    root = self.operation_root(operation.operation_id)
                    source_fd = _open_owned_regular(descriptor, 'incoming.ipa',
                        expected=intent['fileIdentities']['incoming.ipa'])
                    with _opened_ipa_contents(root/'incoming.ipa', max_bytes=MAX_EXPANDED_APP_BYTES,
                            max_entries=MAX_APP_ENTRIES, _workspace=root/'transfer',
                            _workspace_fd=transfer_fd, _source_fd=source_fd) as (app, size, digest):
                        _require(size == len(body) and digest == measured)
                        app_fd = _open_child_directory(transfer_fd, 'app')
                        try:
                            app_stat = os.fstat(app_fd)
                            app_identity = _stat_identity(app_stat)
                        finally: os.close(app_fd)
                        parsed = _app_capability(app, format_name='ipa', container_digest=digest, container_bytes=size,
                            max_bytes=MAX_EXPANDED_APP_BYTES, max_entries=MAX_APP_ENTRIES,
                            max_code_objects=MAX_CODE_OBJECTS, capability_source=root/'incoming.ipa',
                            _root_signature=_file_stat_signature(app_stat))
                        policy = self.definition.bundle_policies
                        _require({row['bundlePath'] for row in parsed.manifest['codeObjects']} == set(policy), 'ios_signing_artifact')
                        import plistlib
                        for path, row in policy.items():
                            info = 'Info.plist' if path == '.' else path+'/Info.plist'
                            value = plistlib.loads(read_regular(app, info, maximum=2*1024*1024))
                            _require(value['CFBundleIdentifier'] == row['bundleId'], 'ios_signing_artifact')
                        self._require_operation(operation)
                        state.update(phase='moving', appIdentity=app_identity, appDigest=parsed.app_digest)
                        _replace_at(descriptor, 'state.json', state)
                        _require('App.app' not in os.listdir(descriptor))
                        _move_new_app(transfer_fd, descriptor)
                        moved = _open_child_directory(descriptor, 'App.app', expected=app_identity); os.close(moved)
                        os.fsync(transfer_fd); os.fsync(descriptor)
                    staged = parse_ios_artifact(root/'App.app')
                    _require(staged.app_digest == state['appDigest'])
                    state['phase'] = 'staged'; _replace_at(descriptor, 'state.json', state)
                    return staged
                finally:
                    if source_fd is not None: os.close(source_fd)
                    os.close(transfer_fd)

    def status(self, operation_id):
        with self._operation_directory(operation_id) as descriptor:
            intent, state = self._records(descriptor, operation_id)
            row = self.run_store.status(operation_id)
            _require(row['requestDigest'] == intent['requestDigest'])
            return {'operationId': operation_id, 'requestDigest': intent['requestDigest'],
                'phase': state['phase'], 'runState': row['state'], 'reservedBytes': row['reservedBytes'],
                'scopeDigest': self.scope_digest, 'signatureVerified': False}

    def sign(self, operation, artifacts, *, material_resolver, provisioning, policy_document,
             cancellation, deadline_monotonic):
        _require(not self._read_only, 'ios_signing_recovery_only')
        from .ios_signing_execution import execute_sign
        return execute_sign(self, operation, artifacts, material_resolver=material_resolver,
            provisioning=provisioning, policy_document=policy_document,
            cancellation=cancellation, deadline_monotonic=deadline_monotonic)

    def inspect(self, operation, context, artifacts, *, provisioning, policy_document,
                cancellation, deadline_monotonic):
        _require(not self._read_only, 'ios_signing_recovery_only')
        from .ios_signing_execution import execute_inspection
        return execute_inspection(self, operation, context, artifacts, provisioning=provisioning,
            policy_document=policy_document, cancellation=cancellation, deadline_monotonic=deadline_monotonic)

    def _cleanup(self, descriptor, intent, state):
        names = set(os.listdir(descriptor))
        fixed = {'intent.json', 'state.json', 'transfer', *self._files}
        if self._native_enabled: fixed.add('checks')
        transient = names - fixed - {'App.app'}
        _require(len(transient) <= 64 and all(_TRANSIENT.fullmatch(name) for name in transient))
        for name in self._files:
            fd = _open_owned_regular(descriptor, name, expected=intent['fileIdentities'][name], writable=True)
            try:
                maximum = (MAX_TRANSFER_BYTES if name.endswith('.ipa') else 2*1024*1024+4096
                    if name in {'request.plist', 'native-result.plist'} else 128*1024 if name == 'arguments.plist' else 4096)
                _require(os.fstat(fd).st_size <= maximum)
                if name in ('producer.lock', 'owner.lock'): _require(os.fstat(fd).st_size == 0)
            finally: os.close(fd)
        transfer = _open_child_directory(descriptor, 'transfer', expected=intent['transferIdentity'])
        app = None
        checks = None
        try:
            if self._native_enabled:
                checks = _open_child_directory(descriptor, 'checks', expected=intent['checksIdentity'])
            transfer_names = set(os.listdir(transfer))
            if state['phase'] == 'staged':
                _require('App.app' in names and 'app' not in transfer_names, 'ios_signing_staged_app_missing')
            elif state['phase'] == 'moving':
                _require(('App.app' in names) != ('app' in transfer_names), 'ios_signing_staged_app_missing')
                if 'app' in transfer_names:
                    pending_app = _open_child_directory(transfer, 'app', expected=state['appIdentity'])
                    os.close(pending_app)
            elif state['phase'] in {'intent', 'receiving', 'extracting'}:
                _require('App.app' not in names)
                if state['phase'] in {'intent', 'receiving'}: _require(not transfer_names)
            if 'App.app' in names:
                _require(state['appIdentity'] is not None)
                app = _open_child_directory(descriptor, 'App.app', expected=state['appIdentity'])
            if state['phase'] in {'extracting', 'moving', 'staged'}:
                original = read_regular(self.operation_root(intent['operationId']), 'incoming.ipa', maximum=MAX_TRANSFER_BYTES)
                _require(len(original) == state['inputBytes'] and hashlib.sha256(original).hexdigest() == state['inputDigest'])
            state['phase'] = 'cleaning'; _replace_at(descriptor, 'state.json', state)
            if app is not None:
                _remove_owned_contents(app, [MAX_APP_ENTRIES+1024])
                current = os.stat('App.app', dir_fd=descriptor, follow_symlinks=False)
                _require(_same_directory(current, state['appIdentity']))
                os.rmdir('App.app', dir_fd=descriptor)
            _remove_owned_contents(transfer, [MAX_APP_ENTRIES+1024])
            if checks is not None: _remove_owned_contents(checks, [MAX_APP_ENTRIES+4096])
            for name in self._files:
                if name in ('producer.lock', 'owner.lock'): continue
                fd = _open_owned_regular(descriptor, name, expected=intent['fileIdentities'][name], writable=True)
                try: _write_bytes(fd, b'')
                finally: os.close(fd)
            for name in transient:
                fd = _open_owned_regular(descriptor, name)
                try: _require(os.fstat(fd).st_size <= 64*1024)
                finally: os.close(fd)
                os.unlink(name, dir_fd=descriptor)
            os.fsync(transfer); os.fsync(descriptor)
            state['phase'] = 'cleaned'; state['recovery'] = 'sanitized'
            _replace_at(descriptor, 'state.json', state)
        finally:
            if app is not None: os.close(app)
            if checks is not None: os.close(checks)
            os.close(transfer)

    @contextmanager
    def recovery(self, operation_id, request_digest):
        contracts.validate_id(operation_id); contracts.validate_digest(request_digest)
        capability = None
        with self.run_store.repair_scope_lease('signing', self.scope_digest):
            run_root = _walk_directory(self.run_store.root)
            vm_fd = None
            try:
                vm_fd = _open_owned_regular(run_root, '.vm-lock', writable=True, zero=True)
                fcntl.flock(vm_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self._operation_directory(operation_id) as descriptor:
                    intent, state = self._records(descriptor, operation_id, request_digest)
                    row = self.run_store.status(operation_id)
                    _require(row['state'] not in TERMINAL and row['requestDigest'] == request_digest
                        and row['reservedBytes'] == intent['diskBytes'])
                    with self._producer(descriptor, intent) as handles:
                        self._cleanup(descriptor, intent, state)
                        evidence = contracts.digest({'operationId': operation_id, 'requestDigest': request_digest,
                            'definitionDigest': self.definition_digest, 'state': 'sanitized-under-native-locks'})
                        checks = ((run_root, '.vm-lock', _stat_identity(os.fstat(vm_fd))),
                            (descriptor, 'producer.lock', intent['fileIdentities']['producer.lock']),
                            (descriptor, 'owner.lock', intent['fileIdentities']['owner.lock']))
                        capability = SigningCleanupCapability(operation_id, request_digest, intent['contextDigest'],
                            self.scope_digest, evidence, self._issuer, os.getpid(), threading.get_ident(), (vm_fd, *handles), checks)
                        with self._mutex:
                            _require(operation_id not in self._recoveries)
                            self._recoveries[operation_id] = capability
                        try: yield capability
                        finally:
                            with self._mutex: self._recoveries.pop(operation_id, None)
            except (OSError, RuntimeError) as error:
                if type(error) is SigningRecoveryError: raise
                raise SigningRecoveryError('ios_signing_recovery_unavailable') from None
            finally:
                if capability is not None: capability._active = False
                if vm_fd is not None: os.close(vm_fd)
                os.close(run_root)

    def require_cleanup(self, capability, run_store):
        _require(type(capability) is SigningCleanupCapability)
        with self._mutex:
            _require(self._recoveries.get(capability.operation_id) is capability and capability._issuer is self._issuer
                and capability._active and not capability._consumed and capability._pid == os.getpid()
                and capability._thread == threading.get_ident() and run_store is self.run_store
                and capability.scope_digest == self.scope_digest
                and SigningOperationStore._locks_still_held(capability))
            parent = capability._lock_checks[1][0]
            intent, state = self._records(parent, capability.operation_id, capability.request_digest)
            _require(intent['contextDigest'] == capability.context_digest and state['phase'] == 'cleaned'
                and state['recovery'] == 'sanitized')
            names = set(os.listdir(parent))
            _require(names == {'intent.json', 'state.json', 'transfer', *self._files,
                               *(('checks',) if self._native_enabled else ())})
            transfer = _open_child_directory(parent, 'transfer', expected=intent['transferIdentity'])
            try: _require(not os.listdir(transfer))
            finally: os.close(transfer)
            if self._native_enabled:
                checks = _open_child_directory(parent, 'checks', expected=intent['checksIdentity'])
                try: _require(not os.listdir(checks))
                finally: os.close(checks)
            for name in self._files:
                fd = _open_owned_regular(parent, name, expected=intent['fileIdentities'][name], zero=True)
                os.close(fd)
            capability._consumed = True
            return capability

    def close(self, *, deadline_monotonic=None):
        deadline = time.monotonic()+5 if deadline_monotonic is None else deadline_monotonic
        _require(type(deadline) in (int, float) and 0 < deadline < float('inf'))
        with self._changed:
            self._closed = True
            for control in self._native_controls: control.close_liveness()
            while (self._active or self._callbacks or self._native_controls) and time.monotonic() < deadline:
                for control in tuple(self._native_controls):
                    process = control.process
                    if process is not None and process.returncode is not None:
                        try: os.killpg(process.pid, 0)
                        except ProcessLookupError: self._native_controls.discard(control)
                        except OSError: pass
                self._changed.wait(min(.05, max(0, deadline-time.monotonic())))
            return not self._active and not self._callbacks and not self._native_controls


__all__ = ['IOSSigningOperationStore', 'IOSSigningOperation']
