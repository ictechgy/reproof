"""Journal-bound IPA preparation and original native ownership; no cleanup authority."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time

from . import contracts
from .execution.artifacts import BlobSet
from .execution.journal import RunStore
from .execution.wire import MAX_TRANSFER_BYTES
from .ios_artifact_transfer import (MAX_APP_ENTRIES, MAX_CODE_OBJECTS, MAX_EXPANDED_APP_BYTES,
    _app_capability, _opened_ipa_contents, parse_ios_artifact)
from .ios_signing_operation import _move_new_app, _write_bytes
from .live.authority import canonical_device_fingerprint
from .repair_android_operation import (_context_record, _identity_info, _open_child_directory,
    _open_regular_at, _private_directory, _read_fd, _read_json_at, _replace_at, _retire_record_temps,
    _same_identity, _valid_identity, _validate_context, _walk_directory, _write_new_at)

MAX_XCTEST_ITERATIONS = 3
XCTEST_WORK_BYTES = 64 * 1024 * 1024


class IOSMobileOperationError(RuntimeError):
    def __init__(self, code='ios_mobile_operation_unavailable'):
        self.code = code
        super().__init__(code)


def _require(value):
    if not value:
        raise IOSMobileOperationError()


def _bundle(value):
    return type(value) is str and len(value) <= 180 and re.fullmatch(
        r'[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+', value)


@dataclass(frozen=True, slots=True)
class IOSMobileDefinition:
    project_digest: str
    application_id: str
    runtime_policy_digest: str
    device_id: str
    udid: str = field(repr=False)
    bundle_id: str
    query_definition_digest: str
    original_profile_digest: str
    baseline_digest: str
    helper_bundles: tuple = ()
    xctest_definition_digest: str | None = None
    sanitation_policy_digest: str | None = None

    def __post_init__(self):
        try:
            for name in ('project_digest','runtime_policy_digest','query_definition_digest',
                         'original_profile_digest','baseline_digest'):
                contracts.validate_digest(getattr(self, name))
            contracts.validate_id(self.application_id); contracts.validate_id(self.device_id)
            _require(type(self.udid) is str and re.fullmatch(r'[A-Za-z0-9-]{1,128}', self.udid)
                and _bundle(self.bundle_id) and type(self.helper_bundles) is tuple)
            _require(all(type(row) is tuple and len(row) == 2 and _bundle(row[1]) for row in self.helper_bundles))
            roles = [row[0] for row in self.helper_bundles]
            _require(not roles or len(roles) == 2 and set(roles) == {'helper-host','helper-runner'})
            if self.xctest_definition_digest is not None:
                contracts.validate_digest(self.xctest_definition_digest)
                _require(len(roles) == 2)
            if self.sanitation_policy_digest is not None:
                contracts.validate_digest(self.sanitation_policy_digest)
                _require(self.xctest_definition_digest is not None)
        except (contracts.ContractError, ValueError, TypeError):
            raise IOSMobileOperationError() from None

    @property
    def scope_digest(self):
        return canonical_device_fingerprint('ios-physical', self.udid)

    def public(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__
            if name != 'udid' and (name not in {'xctest_definition_digest','sanitation_policy_digest'}
                or getattr(self,name) is not None)} | {
            'scopeDigest': self.scope_digest, 'executionAuthority': 'none'}

    @property
    def xctest_reserved_bytes(self):
        return 2 * MAX_XCTEST_ITERATIONS * XCTEST_WORK_BYTES if self.xctest_definition_digest is not None else 0

    @property
    def recovery_reserved_bytes(self):
        # Fresh original/helper copies plus three bounded recovery attempts.
        return (3*MAX_EXPANDED_APP_BYTES+MAX_TRANSFER_BYTES+3*XCTEST_WORK_BYTES+2*1024*1024
                if self.sanitation_policy_digest is not None else 0)


@dataclass(slots=True)
class IOSMobileOperation:
    store: object = field(repr=False)
    run: object = field(repr=False)
    context: object = field(repr=False)
    _issuer: object = field(repr=False)
    _pid: int = field(repr=False)
    _intent_digest: str = field(repr=False)
    _producer_fd: int = field(repr=False)
    _active: bool = field(default=True, repr=False)
    _prepare_lock: object = field(default_factory=threading.Lock, repr=False)

    def archive_path(self, role):
        self.store._require_operation(self)
        _require(role in self.store._roles)
        return self.store._operation_root(self.context.operation_id)/role/'input.ipa'


class IOSMobileOperationStore:
    def __init__(self, run_store, definition, private_root, *, create=True):
        _require(type(run_store) is RunStore and type(definition) is IOSMobileDefinition and type(create) is bool)
        self.run_store, self.definition = run_store, definition
        self.root = _private_directory(Path(private_root), create=create)
        self.operations = _private_directory(self.root/'operations', create=create)
        self._operations_identity = _identity_info(self.operations.stat())
        self._roles = {'candidate': definition.bundle_id, 'original': definition.bundle_id,
                       **dict(definition.helper_bundles)}
        self._issuer = object(); self._closed = False; self._active = {}; self._callbacks = set()
        self._changed = threading.Condition(threading.RLock())
        self._recoveries = {}
        self._native_owners = {}; self._native_exports = {}
        self._native_clients = set()
        self._native_recovery_finalization_exports = {}
        self._ios_recovery_cleanup_exports = {}
        self._configuration = {'schemaVersion': 1, 'kind': 'ios-mobile-preparation-v1', 'producerOwnershipVersion': 1,
            'definition': definition.public(), 'ownerRootDigest': contracts.digest(str(self.root)),
            'runStoreRootDigest': contracts.digest(str(run_store.root)),
            'operationsIdentity': {key:value for key,value in self._operations_identity.items() if key not in ('links','device')},
            'environmentDigest': run_store.environment_digest}
        self._configuration = json.loads(json.dumps(self._configuration))
        self.configuration_digest = contracts.digest(self._configuration)
        root_fd = _walk_directory(self.root)
        try:
            self._root_identity = _identity_info(os.fstat(root_fd))
            if create:
                try:
                    lock = os.open('control.lock', os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
                except FileExistsError:
                    lock = _open_regular_at(root_fd, 'control.lock', writable=True)
            else:
                lock = _open_regular_at(root_fd, 'control.lock', writable=True)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
                if create:
                    try: _write_new_at(root_fd, 'intent.json', self._configuration)
                    except FileExistsError: pass
                stored = _read_json_at(root_fd, 'intent.json')
                if contracts.digest(stored) != self.configuration_digest:
                    _require(self._compatible_record(stored))
                    _replace_at(root_fd, 'intent.json', self._configuration)
                if create: _retire_record_temps(root_fd)
            finally:
                os.close(lock)
        finally:
            os.close(root_fd)

    def _compatible_record(self, stored):
        # Records written while st_dev was part of the durable identity remain
        # valid once that mount-bound field is stripped.
        if type(stored) is not dict or type(stored.get('operationsIdentity')) is not dict:
            return False
        candidate = dict(stored)
        candidate['operationsIdentity'] = {
            key:value for key,value in stored['operationsIdentity'].items() if key != 'device'}
        return contracts.digest(candidate) == self.configuration_digest

    def _operation_root(self, identifier):
        contracts.validate_id(identifier)
        return self.operations/identifier

    @contextmanager
    def _directory(self, identifier):
        contracts.validate_id(identifier)
        root = _walk_directory(self.root)
        operations = selected = None
        try:
            _require(_same_identity(os.fstat(root), self._root_identity, directory=True))
            operations = _open_child_directory(root, 'operations',expected=self._operations_identity)
            selected = _open_child_directory(operations, identifier)
            yield selected
        finally:
            for fd in (selected, operations, root):
                if fd is not None: os.close(fd)

    def _records(self, identifier, directory):
        intent = _read_json_at(directory, 'intent.json')
        state = _read_json_at(directory, 'state.json')
        _require(type(intent) is dict and set(intent) == {'schemaVersion','operationId','requestDigest',
            'contextDigest','context','configurationDigest','reservedBytes','directoryIdentity','producerIdentity','roles'}
            and type(state) is dict and set(state) in ({'schemaVersion','operationId','roles'},
                {'schemaVersion','operationId','roles','nativeBindingDigest'})
            and type(intent['schemaVersion']) is type(state['schemaVersion']) is int
            and intent['schemaVersion'] == state['schemaVersion'] == 1
            and intent.get('operationId') == state.get('operationId') == identifier
            and intent.get('configurationDigest') == self.configuration_digest
            and _same_identity(os.fstat(directory), intent['directoryIdentity'], directory=True)
            and _valid_identity(intent['producerIdentity'])
            and set(intent['roles']) == set(state['roles']) == set(self._roles))
        for name in ('requestDigest','contextDigest','configurationDigest'):
            contracts.validate_digest(intent[name])
        _require(type(intent['reservedBytes']) is int and intent['reservedBytes'] > 0)
        if 'nativeBindingDigest' in state: contracts.validate_digest(state['nativeBindingDigest'])
        expected_context = {'operation_id','request_digest','repair_plan_digest','project_digest',
            'application_id','source_digest','artifact_digest','scope_digest','runtime_policy_digest'}
        values = intent['context']
        _require(type(values) is dict and set(values) == expected_context
            and values['operation_id'] == identifier and values['request_digest'] == intent['requestDigest']
            and (values['project_digest'],values['application_id'],values['scope_digest'],values['runtime_policy_digest'])
            == (self.definition.project_digest,self.definition.application_id,self.definition.scope_digest,
                self.definition.runtime_policy_digest))
        for name in expected_context - {'operation_id','application_id'}:
            contracts.validate_digest(values[name])
        total = 0
        for role in self._roles:
            record, observed = intent['roles'][role], state['roles'][role]
            _require(type(record) is dict and set(record) == {'directoryIdentity','archiveIdentity',
                'transferIdentity','sha256','bytes'} and type(record['bytes']) is int
                and 0 < record['bytes'] <= MAX_TRANSFER_BYTES
                and _valid_identity(record['directoryIdentity'],directory=True)
                and _valid_identity(record['transferIdentity'],directory=True)
                and _valid_identity(record['archiveIdentity']))
            contracts.validate_digest(record['sha256']); total += record['bytes']
            _require(type(observed) is dict and observed.get('state') in {'empty','received','preparing','prepared'})
            if observed['state'] == 'prepared':
                _require(set(observed) == {'state','appDigest'});contracts.validate_digest(observed['appDigest'])
            else:
                _require(set(observed) == {'state'})
        _require(intent['reservedBytes'] == total+(len(self._roles)+1)*MAX_EXPANDED_APP_BYTES+MAX_TRANSFER_BYTES+2*1024*1024
            +self.definition.xctest_reserved_bytes+self.definition.recovery_reserved_bytes)
        _require(intent['roles']['candidate']['sha256'] == values['artifact_digest'])
        baselines = [{'path':role+'.ipa','digest':intent['roles'][role]['sha256'],
                      'size':intent['roles'][role]['bytes']} for role in sorted(self._roles) if role != 'candidate']
        _require(contracts.digest(baselines) == self.definition.baseline_digest)
        return intent, state

    def _require_operation(self, operation):
        with self._changed:
            _require(type(operation) is IOSMobileOperation and operation.store is self
                and operation._issuer is self._issuer and operation._pid == os.getpid()
                and operation._active and not self._closed and not operation.run.finished
                and self._active.get(operation.context.operation_id) is operation)

    def require_operation(self, operation, context=None):
        self._require_operation(operation)
        if context is not None:
            _validate_context(context)
            _require(context.digest == operation.context.digest)
        return operation

    @contextmanager
    def admit(self, context, artifacts, baselines):
        handles = []
        try:
            with self._admit(context,artifacts,baselines,handles) as operation:
                yield operation
        finally:
            with self._changed:
                for descriptor in handles:os.close(descriptor)

    @contextmanager
    def _admit(self, context, artifacts, baselines, handles):
        try:
            _validate_context(context)
            _require(type(artifacts) is BlobSet and type(baselines) is BlobSet
                and len(artifacts.entries) == 1 and artifacts.entries[0][0] == 'candidate.ipa'
                and baselines.digest == self.definition.baseline_digest
                and {name for name,_ in baselines.entries} == {role+'.ipa' for role in self._roles if role != 'candidate'})
            contents = dict(artifacts.entries+baselines.entries)
            _require(set(contents) == {role+'.ipa' for role in self._roles}
                and all(0 < len(body) <= MAX_TRANSFER_BYTES for body in contents.values())
                and hashlib.sha256(contents['candidate.ipa']).hexdigest() == context.artifact_digest)
            _require((context.project_digest,context.application_id,context.runtime_policy_digest,context.scope_digest)
                == (self.definition.project_digest,self.definition.application_id,
                    self.definition.runtime_policy_digest,self.definition.scope_digest))
        except (contracts.ContractError, RuntimeError, TypeError, ValueError):
            raise IOSMobileOperationError() from None
        # Archives, all retained app trees, one extraction tree/snapshot and metadata.
        reserved = sum(map(len, contents.values())) + (len(contents)+1)*MAX_EXPANDED_APP_BYTES + MAX_TRANSFER_BYTES + 2*1024*1024
        reserved += self.definition.xctest_reserved_bytes
        reserved += self.definition.recovery_reserved_bytes
        operation = None
        with self.run_store.repair_scope_lease('mobile-device',self.definition.scope_digest):
            self.run_store.require_available()
            with self.run_store.admit(context.operation_id,context.request_digest,disk_bytes=reserved) as run:
                with self._changed: _require(not self._closed)
                # Generic run.finish(stopped=True) must not release these files.
                guard = _walk_directory(run.directory)
                try: _write_new_at(guard,'intent.json',{'kind':'ios-mobile-preparation-hold','contextDigest':context.digest})
                finally: os.close(guard)
                parent = _walk_directory(self.operations)
                try:
                    _require(_same_identity(os.fstat(parent),self._operations_identity,directory=True))
                    os.mkdir(context.operation_id,mode=0o700,dir_fd=parent);os.fsync(parent)
                finally:
                    os.close(parent)
                with self._directory(context.operation_id) as directory:
                    producer = os.open('producer.lock',os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=directory)
                    handles.append(producer)
                    fcntl.flock(producer,fcntl.LOCK_EX|fcntl.LOCK_NB);os.fsync(producer)
                    role_records = {}
                    for role in self._roles:
                        os.mkdir(role,mode=0o700,dir_fd=directory)
                        role_fd = _open_child_directory(directory,role)
                        archive = transfer = None
                        try:
                            archive = os.open('input.ipa',os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=role_fd)
                            os.mkdir('transfer',mode=0o700,dir_fd=role_fd)
                            transfer = _open_child_directory(role_fd,'transfer')
                            body = contents[role+'.ipa']
                            role_records[role] = {'directoryIdentity':_identity_info(os.fstat(role_fd)),
                                'archiveIdentity':_identity_info(os.fstat(archive)),
                                'transferIdentity':_identity_info(os.fstat(transfer)),
                                'sha256':hashlib.sha256(body).hexdigest(),'bytes':len(body)}
                            os.fsync(archive);os.fsync(transfer);os.fsync(role_fd)
                        finally:
                            for fd in (archive,transfer,role_fd):
                                if fd is not None:os.close(fd)
                    intent = {'schemaVersion':1,'operationId':context.operation_id,'requestDigest':context.request_digest,
                        'contextDigest':context.digest,'context':_context_record(context),
                        'configurationDigest':self.configuration_digest,'reservedBytes':reserved,
                        'directoryIdentity':_identity_info(os.fstat(directory)),
                        'producerIdentity':_identity_info(os.fstat(producer)),'roles':role_records}
                    state = {'schemaVersion':1,'operationId':context.operation_id,
                             'roles':{role:{'state':'empty'} for role in self._roles}}
                    _write_new_at(directory,'intent.json',intent);_write_new_at(directory,'state.json',state)
                    os.fsync(directory)
                    for role, record in role_records.items():
                        role_fd = _open_child_directory(directory,role,expected=record['directoryIdentity'])
                        archive = None
                        try:
                            archive = _open_regular_at(role_fd,'input.ipa',expected=record['archiveIdentity'],writable=True)
                            _write_bytes(archive,contents[role+'.ipa'])
                        finally:
                            if archive is not None:os.close(archive)
                            os.close(role_fd)
                        state['roles'][role] = {'state':'received'};_replace_at(directory,'state.json',state)
                operation = IOSMobileOperation(self,run,context,self._issuer,os.getpid(),contracts.digest(intent),producer)
                with self._changed:
                    _require(not self._closed);self._active[context.operation_id] = operation
                try:
                    yield operation
                finally:
                    with self._changed:
                        operation._active = False
                        self._active.pop(context.operation_id,None);self._changed.notify_all()

    def prepare(self, operation, role, *, cancellation, deadline_monotonic):
        acquired = False; callback = object(); producer = None
        try:
            self._require_operation(operation)
            _require(type(role) is str and role in self._roles)
            def bounds():
                self._require_operation(operation)
                _require(callable(getattr(cancellation,'is_set',None))
                    and type(deadline_monotonic) in (int,float) and math.isfinite(deadline_monotonic)
                    and time.monotonic() < deadline_monotonic and not cancellation.is_set()
                    and not operation.run.cancelled())
            bounds()
            acquired = operation._prepare_lock.acquire(blocking=False);_require(acquired)
            with self._changed:
                self._require_operation(operation)
                producer = os.dup(operation._producer_fd)
                self._callbacks.add(callback)
            with self._directory(operation.context.operation_id) as directory:
                intent,state = self._records(operation.context.operation_id,directory)
                _require(contracts.digest(intent) == operation._intent_digest
                    and intent['contextDigest'] == operation.context.digest
                    and _same_identity(os.fstat(producer),intent['producerIdentity'])
                    and 'nativeBindingDigest' not in state and 'native.json' not in os.listdir(directory)
                    and state['roles'][role]['state'] == 'received')
                record = intent['roles'][role]
                role_fd = _open_child_directory(directory,role,expected=record['directoryIdentity'])
                transfer = archive = None
                try:
                    archive = _open_regular_at(role_fd,'input.ipa',expected=record['archiveIdentity'])
                    raw = _read_fd(archive,MAX_TRANSFER_BYTES)
                    _require(len(raw) == record['bytes'] and hashlib.sha256(raw).hexdigest() == record['sha256'])
                    del raw
                    state['roles'][role] = {'state':'preparing'};_replace_at(directory,'state.json',state)
                    transfer = _open_child_directory(role_fd,'transfer',expected=record['transferIdentity'])
                    role_root = self._operation_root(operation.context.operation_id)/role
                    with _opened_ipa_contents(role_root/'input.ipa',max_bytes=MAX_EXPANDED_APP_BYTES,
                            max_entries=MAX_APP_ENTRIES,_workspace=role_root/'transfer',
                            _workspace_fd=transfer,_source_fd=archive) as (app,size,checksum):
                        _require(size == record['bytes'] and checksum == record['sha256'])
                        parsed = _app_capability(app,format_name='ipa',container_digest=checksum,
                            container_bytes=size,max_bytes=MAX_EXPANDED_APP_BYTES,
                            max_entries=MAX_APP_ENTRIES,max_code_objects=MAX_CODE_OBJECTS,
                            capability_source=role_root/'input.ipa')
                        _require(parsed.manifest['applicationId'] == self._roles[role]);bounds()
                        _move_new_app(transfer,role_fd)
                        prepared = parse_ios_artifact(role_root/'App.app')
                        _require(prepared.app_digest == parsed.app_digest);bounds()
                        state['roles'][role] = {'state':'prepared','appDigest':prepared.app_digest}
                        _replace_at(directory,'state.json',state)
                    return prepared
                finally:
                    for fd in (archive,transfer,role_fd):
                        if fd is not None:os.close(fd)
        except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError):
            raise IOSMobileOperationError() from None
        finally:
            with self._changed:
                if producer is not None:os.close(producer)
                self._callbacks.discard(callback);self._changed.notify_all()
            if acquired:operation._prepare_lock.release()

    def status(self, identifier):
        try:
            with self._directory(identifier) as directory:
                intent,state = self._records(identifier,directory)
                from .ios_mobile_native import ownership_status
                native = ownership_status(directory,intent,state)
            row = self.run_store.status(identifier)
            _require(row['requestDigest'] == intent['requestDigest'])
            return {'schemaVersion':1,'operationId':identifier,'requestDigest':intent['requestDigest'],
                'configurationDigest':self.configuration_digest,'scopeDigest':self.definition.scope_digest,
                'roles':state['roles'],'runState':row['state'],'reservedBytes':row['reservedBytes'],
                'executionAuthority':'none','deviceCleanupConfirmed':False,'nativeOwnership':native}
        except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError):
            raise IOSMobileOperationError() from None

    def preparation_recovery(self, operation_id, request_digest, *, cancellation, deadline_monotonic):
        from .ios_mobile_recovery import preparation_recovery
        return preparation_recovery(self,operation_id,request_digest,cancellation=cancellation,
                                    deadline_monotonic=deadline_monotonic)

    def native_recovery(self, operation_id, request_digest, *, device, snapshot,
                        parent_grant, cancellation, deadline_monotonic):
        from .ios_native_recovery import native_recovery
        return native_recovery(self, operation_id, request_digest, device=device,
            snapshot=snapshot, parent_grant=parent_grant, cancellation=cancellation,
            deadline_monotonic=deadline_monotonic)

    def require_native_recovery(self, context):
        from .ios_native_recovery import require_native_recovery
        result = require_native_recovery(context)
        _require(result._operations is self)
        return result

    def finalize_recovery(self, operation_id, request_digest, *, config, device,
                          parent_grant, cancellation, deadline_monotonic):
        from .ios_recovery_finalization import finalize_recovery
        return finalize_recovery(self, operation_id, request_digest, config=config,
            device=device, parent_grant=parent_grant, cancellation=cancellation,
            deadline_monotonic=deadline_monotonic)

    def native_owner(self, operation, device):
        from .ios_mobile_native import native_owner
        return native_owner(self,operation,device)

    def require_preparation_cleanup(self, capability, run_store):
        from .ios_mobile_recovery import require_cleanup
        return require_cleanup(capability,self,run_store)

    def close(self, *, deadline_monotonic=None):
        deadline = time.monotonic()+3 if deadline_monotonic is None else deadline_monotonic
        _require(type(deadline) in (int,float) and math.isfinite(deadline))
        with self._changed:
            self._closed = True
            clients = tuple(self._native_clients)
        for client in clients:client.close(deadline_monotonic=deadline)
        with self._changed:
            while (self._active or self._callbacks or self._recoveries or self._native_owners
                   or self._native_exports or self._native_clients
                   or self._native_recovery_finalization_exports
                   or self._ios_recovery_cleanup_exports) and time.monotonic() < deadline:
                self._changed.wait(max(0,deadline-time.monotonic()))
            return not (self._active or self._callbacks or self._recoveries or self._native_owners
                        or self._native_exports or self._native_clients
                        or self._native_recovery_finalization_exports
                        or self._ios_recovery_cleanup_exports)
