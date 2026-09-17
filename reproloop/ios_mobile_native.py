"""Original iOS device/producer lock ownership, independent of device effects.

Binding permanently leaves preparation-only recovery. Exported descriptors
retain existing open file descriptions; they do not issue a dispatch permit,
prove CoreDevice daemon termination, or authorize device sanitation.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
import threading
import time

from . import contracts
from .execution.wire import MAX_TRANSFER_BYTES
from .ios_artifact_transfer import parse_ios_artifact
from .ios_mobile_operation import IOSMobileOperationError, IOSMobileOperationStore, _require
from .live.authority import DeviceAuthority, DispatchPermit, HostAuthority
from .repair_android_operation import (_identity_info, _open_child_directory, _open_regular_at, _read_fd, _read_json_at,
    _replace_at, _same_identity, _valid_identity, _write_new_at)


def require_native_dispatch(owner, permit, payload_digest):
    owner._check()
    if owner._recovery_context is not None:
        return owner._recovery_context.require_dispatch(owner,permit,payload_digest)
    _require(type(permit) is DispatchPermit)
    now = owner.device.check_dispatch_permit(permit)
    row = owner.device._authority.store.operation(permit.operation_id)
    fields = {'operation_fingerprint':'operation_fingerprint','payload_digest':'payload_digest',
        'project_id':'project_id','session_id':'session_id','controller_id':'controller_id',
        'sequence':'sequence','protocol_version':'protocol_version','generation':'ownership_generation',
        'host_incarnation':'host_incarnation','helper_incarnation':'helper_incarnation',
        'provider_incarnation':'provider_incarnation'}
    _require(all(type(getattr(permit, attr)) is type(row[key]) and getattr(permit, attr) == row[key]
        for key,attr in fields.items()) and type(permit.deadline_ns) is int
        and permit.deadline_ns <= row['deadline_ns'] and permit.payload_digest == payload_digest)
    return now


def prepared_app(owner, role):
    owner._check()
    if owner._recovery_context is not None:
        return owner._recovery_context.prepared_app(owner,role)
    intent, _ = owner.operations._records(owner.operation.context.operation_id,owner._directory)
    _require(type(role) is str and role in owner.operations._roles)
    record = intent['roles'][role]
    directory = _open_child_directory(owner._directory,role,expected=record['directoryIdentity'])
    archive = None
    try:
        archive = _open_regular_at(directory,'input.ipa',expected=record['archiveIdentity'])
        raw = _read_fd(archive,MAX_TRANSFER_BYTES)
        _require(len(raw) == record['bytes'] and hashlib.sha256(raw).hexdigest() == record['sha256'])
        del raw
        app = parse_ios_artifact(owner.operations._operation_root(owner.operation.context.operation_id)/role/'App.app')
        _require(app.app_digest == json.loads(owner._record)['preparedApps'][role]
            and app.manifest['applicationId'] == owner.operations._roles[role])
        return app
    finally:
        if archive is not None:os.close(archive)
        os.close(directory)


def _device(owner, device):
    _require(type(device) is DeviceAuthority and type(device._authority) is HostAuthority
        and device in device._authority._handles and device.device_kind == 'ios-physical'
        and device._device_fingerprint == owner.definition.scope_digest)
    # Also refuses revoked dispatches and expired grants while permitting a
    # receipt-pending operation to retain its original native ownership.
    device.check_observation_authority()


def _binding_record(directory, intent, state):
    names = set(os.listdir(directory))
    if 'native.json' not in names:
        _require('nativeBindingDigest' not in state)
        return None
    record = _read_json_at(directory, 'native.json')
    _require(type(record) is dict and set(record) == {
        'schemaVersion','operationId','requestDigest','contextDigest','configurationDigest',
        'scopeDigest','ownershipGeneration','hostIncarnation','helperIncarnation',
        'authorityRootDigest','deviceLeaseIdentity','deviceDirectoryIdentity','bindingDigest'} |
        ({'preparedApps'} if record.get('schemaVersion') == 2 else set())
        and type(record['schemaVersion']) is int and record['schemaVersion'] in (1,2)
        and type(record['ownershipGeneration']) is int and 0 < record['ownershipGeneration'] < 2**53
        and all(record[key] == intent[key] for key in
                ('operationId','requestDigest','contextDigest','configurationDigest'))
        and record['scopeDigest'] == intent['context']['scope_digest']
        and _valid_identity(record['deviceLeaseIdentity'])
        and _valid_identity(record['deviceDirectoryIdentity'], directory=True))
    for key in ('hostIncarnation','helperIncarnation'): contracts.validate_id(record[key])
    for key in ('scopeDigest','authorityRootDigest','bindingDigest'): contracts.validate_digest(record[key])
    if record['schemaVersion'] == 2:
        _require(type(record['preparedApps']) is dict and set(record['preparedApps']) == set(intent['roles']))
        for role, digest in record['preparedApps'].items():
            contracts.validate_digest(digest)
            _require(state['roles'][role] == {'state':'prepared','appDigest':digest})
    _require(record['bindingDigest'] == contracts.digest({k:v for k,v in record.items() if k != 'bindingDigest'})
        and state.get('nativeBindingDigest',record['bindingDigest']) == record['bindingDigest'])
    return record


def ownership_status(directory, intent, state):
    record = _binding_record(directory,intent,state)
    return None if record is None else {'state':'bound','bindingDigest':record['bindingDigest'],
        'ownershipGeneration':record['ownershipGeneration'], 'deviceCleanupConfirmed':False}


@dataclass(slots=True, repr=False)
class IOSNativeDescriptors:
    producer_fd: int
    operation_directory_fd: int
    device_fd: int
    device_directory_fd: int
    device_lock_name: str
    _owner: object = field(repr=False)
    _pid: int = field(repr=False)
    _thread: int = field(repr=False)
    _active: bool = field(default=True, repr=False)

    def __repr__(self):
        return '<IOSNativeDescriptors>'


class IOSMobileNativeOwner:
    def __init__(self, operations, operation, device, directory, producer, device_fds, record):
        self.operations, self.operation, self.device = operations, operation, device
        self._directory, self._producer = directory, producer
        self._device_fd, self._device_directory, self._device_name = device_fds
        self._record = json.dumps(record,sort_keys=True,separators=(',',':'))
        self._pid, self._thread = os.getpid(), threading.get_ident()
        # Keep the Thread object as well as its numeric ident.  Idents may be
        # reused after a worker exits; an owner must never become usable by a
        # later, unrelated thread that happens to receive the same ident.
        self._thread_object = threading.current_thread()
        self._active = True
        self._command_lock = threading.Lock()
        self._command_attempts = set()
        self._install_results = {}
        self._install_intent_digests = {}
        self._identity_results = {}
        self._runtime_results = {}
        self._sanitation_results = {}
        self._disposal_digest = None
        self._recovery_context = None

    def __repr__(self):
        return '<IOSMobileNativeOwner>'

    @property
    def binding_digest(self):
        return json.loads(self._record)['bindingDigest']

    @property
    def command_directory(self):
        return self._directory if self._recovery_context is None else self._recovery_context.command_directory

    @property
    def command_root(self):
        return (self.operations._operation_root(self.operation.context.operation_id)
                if self._recovery_context is None else self._recovery_context.command_root)

    @property
    def helper_incarnation(self):
        return (self.device.helper_incarnation if self._recovery_context is None
                else self._recovery_context.helper_incarnation)

    @property
    def native_deadline_ns(self):
        return (self.device._parent_grant.local_deadline_ns if self._recovery_context is None
                else self._recovery_context.native_deadline_ns)

    def bind_native_handshake(self, permit, **fields):
        self._check()
        if self._recovery_context is not None:
            return self._recovery_context.bind_native_handshake(self,permit,**fields)
        return self.device.bind_native_handshake(permit,**fields)

    def native_grant(self, permit, handshake):
        self._check()
        if self._recovery_context is not None:
            return self._recovery_context.native_grant(self,permit,handshake)
        return self.device.native_grant(permit,handshake)

    def _check(self):
        if self._recovery_context is not None:
            from .ios_recovery_execution import IOSRecoveryExecution
            _require(type(self._recovery_context) is IOSRecoveryExecution)
            self._recovery_context.require_owner(self)
            return
        with self.operations._changed:
            _require(self._active and self._pid == os.getpid() and self._thread == threading.get_ident()
                and self._thread_object is threading.current_thread()
                and self.operations._native_owners.get(id(self)) is self)
            self.operations._require_operation(self.operation)
        _device(self.operations,self.device)
        intent,state = self.operations._records(self.operation.context.operation_id,self._directory)
        _require(contracts.digest(intent) == self.operation._intent_digest)
        record = _binding_record(self._directory,intent,state)
        _require(record is not None and json.dumps(record,sort_keys=True,separators=(',',':')) == self._record
            and record['ownershipGeneration'] == self.device.generation
            and record['hostIncarnation'] == self.device._authority.host_incarnation
            and record['helperIncarnation'] == self.device.helper_incarnation
            and record['authorityRootDigest'] == self.device._authority.authority_root_digest
            and _same_identity(os.fstat(self._producer),intent['producerIdentity'])
            and _same_identity(os.fstat(self._device_fd),record['deviceLeaseIdentity'])
            and _same_identity(os.fstat(self._device_directory),record['deviceDirectoryIdentity'],directory=True))
        with self.operations._directory(self.operation.context.operation_id) as directory:
            _require(_same_identity(os.fstat(directory),intent['directoryIdentity'],directory=True))

    @contextmanager
    def borrow_descriptors(self):
        borrowed = None
        try:
            with ExitStack() as stack:
                with self.operations._changed:
                    self._check()
                    descriptors = []
                    for original in (self._producer,self._directory,self._device_fd,self._device_directory):
                        descriptor = os.dup(original); stack.callback(os.close,descriptor)
                        descriptors.append(descriptor)
                    borrowed = IOSNativeDescriptors(*descriptors,self._device_name,self,os.getpid(),threading.get_ident())
                    self.operations._native_exports[id(borrowed)] = borrowed
                self.require_descriptors(borrowed)
                yield borrowed
        except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError):
            raise IOSMobileOperationError() from None
        finally:
            if borrowed is not None:
                with self.operations._changed:
                    borrowed._active = False
                    self.operations._native_exports.pop(id(borrowed),None)
                    self.operations._changed.notify_all()

    def require_descriptors(self, borrowed):
        try:
            with self.operations._changed:
                _require(type(borrowed) is IOSNativeDescriptors and borrowed._owner is self
                    and self.operations._native_exports.get(id(borrowed)) is borrowed and borrowed._active
                    and borrowed._pid == os.getpid() and borrowed._thread == threading.get_ident())
                self._check()
                pairs = ((borrowed.producer_fd,self._producer,self._directory,'producer.lock'),
                         (borrowed.device_fd,self._device_fd,self._device_directory,self._device_name))
                for descriptor,original,directory,name in pairs:
                    expected = os.fstat(original)
                    _require(_same_identity(os.fstat(descriptor),_identity_info(expected)))
                    probe = _open_regular_at(directory,name,expected=_identity_info(expected),writable=True)
                    try:
                        try: fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        except BlockingIOError: pass
                        else: raise IOSMobileOperationError()
                        # A matching inode opened independently is insufficient.
                        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    finally: os.close(probe)
                _require(borrowed.device_lock_name == self._device_name)
                for descriptor,original in ((borrowed.operation_directory_fd,self._directory),
                                            (borrowed.device_directory_fd,self._device_directory)):
                    _require(_same_identity(os.fstat(descriptor),_identity_info(os.fstat(original)),directory=True))
                return borrowed
        except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError):
            raise IOSMobileOperationError() from None


def _close_native_clients(operations, owner):
    with operations._changed:
        owner._active = False
        clients = tuple(client for client in operations._native_clients if client.native_owner is owner)
    deadline = time.monotonic()+3
    for client in clients:client.close(deadline_monotonic=deadline)


@contextmanager
def native_owner(operations, operation, device):
    owner = None
    try:
        _require(type(operations) is IOSMobileOperationStore)
        with ExitStack() as stack:
            with operations._changed:
                operations._require_operation(operation); _device(operations,device)
                _require(operation._prepare_lock.acquire(blocking=False))
                stack.callback(operation._prepare_lock.release)
                device_fds = stack.enter_context(device.borrow_native_lease())
                _require(device_fds[2] == operations.definition.scope_digest+'.lock')
                producer = os.dup(operation._producer_fd); stack.callback(os.close,producer)
                directory = stack.enter_context(operations._directory(operation.context.operation_id))
                intent,state = operations._records(operation.context.operation_id,directory)
                _require(contracts.digest(intent) == operation._intent_digest
                    and 'nativeBindingDigest' not in state and 'native.json' not in os.listdir(directory)
                    and all(item['state'] == 'prepared' for item in state['roles'].values())
                    and _same_identity(os.fstat(producer),intent['producerIdentity']))
                record = {'schemaVersion':2, 'operationId':operation.context.operation_id,
                    'requestDigest':operation.context.request_digest,'contextDigest':operation.context.digest,
                    'configurationDigest':operations.configuration_digest,'scopeDigest':operations.definition.scope_digest,
                    'ownershipGeneration':device.generation,'hostIncarnation':device._authority.host_incarnation,
                    'helperIncarnation':device.helper_incarnation,
                    'authorityRootDigest':device._authority.authority_root_digest,
                    'deviceLeaseIdentity':_identity_info(os.fstat(device_fds[0])),
                    'deviceDirectoryIdentity':_identity_info(os.fstat(device_fds[1])),
                    'preparedApps':{role:item['appDigest'] for role,item in state['roles'].items()}}
                record['bindingDigest'] = contracts.digest(record)
                # Publish before exporting a single descriptor. Interrupted
                # binding retains its immutable record and all reserved files.
                _write_new_at(directory,'native.json',record)
                state['nativeBindingDigest'] = record['bindingDigest']
                _replace_at(directory,'state.json',state)
                owner = IOSMobileNativeOwner(operations,operation,device,directory,producer,device_fds,record)
                operations._native_owners[id(owner)] = owner
            # Client shutdown must run before this stack closes the original
            # directory/producer descriptors it borrows during cleanup.
            stack.callback(_close_native_clients,operations,owner)
            owner._check()
            yield owner
    except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError):
        raise IOSMobileOperationError() from None
    finally:
        if owner is not None:
            with operations._changed:
                owner._active = False
                operations._native_owners.pop(id(owner),None); operations._changed.notify_all()
