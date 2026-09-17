"""Freshly locked disposal of preparation-only IPA payloads and their reservation."""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import stat
import threading
import time
import zipfile

from . import contracts
from .execution.wire import MAX_TRANSFER_BYTES
from .ios_artifact_transfer import (MAX_APP_ENTRIES, MAX_EXPANDED_APP_BYTES,
    _file_stat_signature, _validated_ipa_members, _zip_preflight)
from .ios_mobile_operation import IOSMobileOperationError, IOSMobileOperationStore, _require
from .repair_android_operation import (_identity_info, _open_child_directory, _open_regular_at,
    _read_fd, _read_json_at, _replace_at, _retire_record_temps, _same_identity,
    _valid_identity, _walk_directory, _write_new_at)


@dataclass(slots=True)
class IOSPreparationCleanupCapability:
    operation_id: str
    request_digest: str
    scope_digest: str
    context_digest: str
    expected_reserved_bytes: int
    _session: object = field(repr=False)
    _consumed: bool = field(default=False, repr=False)


def _node(parent, name, *, directory):
    try: info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError: return None
    _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
        and (directory or info.st_nlink == 1))
    return _identity_info(info)


def _require_node(parent, name, expected, *, directory):
    current = _node(parent, name, directory=directory)
    if current is not None:
        _require(expected is not None and _same_identity(
            os.stat(name, dir_fd=parent, follow_symlinks=False), expected, directory=directory))
    return current


def _open_app_directory(parent, name, expected):
    _require(name in ('App.app','app'))
    descriptor = os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    try:
        _require(_same_identity(os.fstat(descriptor),expected,directory=True))
        return descriptor
    except BaseException:
        os.close(descriptor);raise


def _unlink_file(parent, name, expected, session):
    session.bounds()
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _require((info.st_dev,info.st_ino) == (expected.st_dev,expected.st_ino)
        and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1)
    os.unlink(name,dir_fd=parent);os.fsync(parent)


def _tree(directory, files, directories, session, *, remove=False, prefix='', depth=0):
    _require(depth <= 512)
    for name in os.listdir(directory):
        session.bounds()
        relative = name if not prefix else prefix+'/'+name
        info = os.stat(name,dir_fd=directory,follow_symlinks=False)
        _require(info.st_uid == os.getuid())
        if stat.S_ISDIR(info.st_mode):
            _require(relative in directories and stat.S_IMODE(info.st_mode) == 0o700)
            child = os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            try:
                actual = os.fstat(child);_require((actual.st_dev,actual.st_ino) == (info.st_dev,info.st_ino))
                _tree(child,files,directories,session,remove=remove,prefix=relative,depth=depth+1)
                if remove:
                    session.bounds()
                    current = os.stat(name,dir_fd=directory,follow_symlinks=False)
                    _require((current.st_dev,current.st_ino) == (info.st_dev,info.st_ino))
                    os.rmdir(name,dir_fd=directory);os.fsync(directory)
            finally: os.close(child)
        else:
            _require(relative in files and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) in (0o600,0o700) and info.st_size <= files[relative])
            if remove:_unlink_file(directory,name,info,session)


def _expected_tree(archive_fd, path):
    info = os.fstat(archive_fd)
    _zip_preflight(path,info.st_size,max_entries=MAX_APP_ENTRIES,
        expected_signature=_file_stat_signature(info),_descriptor=archive_fd)
    os.lseek(archive_fd,0,os.SEEK_SET)
    with os.fdopen(os.dup(archive_fd),'rb') as stream, zipfile.ZipFile(stream) as archive:
        entries, app = _validated_ipa_members(archive.infolist(),max_bytes=MAX_EXPANDED_APP_BYTES,
                                              max_entries=MAX_APP_ENTRIES)
    prefix = 'Payload/'+app+'/'
    files = {};directories = set()
    for entry,name,is_directory in entries:
        if not name.startswith(prefix):continue
        relative = name[len(prefix):]
        if is_directory:directories.add(relative)
        else:files[relative] = entry.file_size
        directories.update(str(parent) for parent in PurePosixPath(relative).parents if str(parent) != '.')
        _require(len(files)+len(directories) <= MAX_APP_ENTRIES)
    return files,directories


def _record_binding(intent):
    return {name:intent[name] for name in ('operationId','requestDigest','contextDigest','configurationDigest')} | {
        'intentDigest':contracts.digest(intent)}


def _read_record(directory, intent, roles):
    if 'recovery.json' not in os.listdir(directory):return None
    record = _read_json_at(directory,'recovery.json')
    binding = _record_binding(intent)
    _require(type(record) is dict and set(record) == {'schemaVersion','kind',*binding,'state','roots'}
        and type(record['schemaVersion']) is int and record['schemaVersion'] == 1
        and record['kind'] == 'ios-preparation-disposal-v1'
        and all(record[name] == value for name,value in binding.items())
        and record['state'] in {'discarding','discarded','completed'}
        and type(record['roots']) is dict and set(record['roots']) == set(roles))
    for row in record['roots'].values():
        _require(type(row) is dict and set(row) == {'app','transferApp','snapshot'})
        for name,identity in row.items():
            _require(identity is None or _valid_identity(identity,directory=name != 'snapshot'))
    return record


class _Recovery:
    def __init__(self, owner, intent, directory, producer, vm_lock, runs, cancellation, deadline):
        self.owner,self.intent,self.directory,self.producer = owner,intent,directory,producer
        self.vm_lock,self.runs,self.cancellation,self.deadline = vm_lock,runs,cancellation,deadline
        self.binding = _record_binding(intent)
        self.pid,self.thread = os.getpid(),threading.get_ident()
        self.active = True;self.capability = None;self.record = None
        self.vm_identity = _identity_info(os.fstat(vm_lock))
        self.run_directory_identity = None

    def bounds(self):
        _require(self.active and self.pid == os.getpid() and self.thread == threading.get_ident()
            and not self.owner._closed and time.monotonic() < self.deadline and not self.cancellation.is_set())
        _require(_same_identity(os.fstat(self.producer),self.intent['producerIdentity'])
            and _same_identity(os.fstat(self.vm_lock),self.vm_identity))
        with self.owner._directory(self.intent['operationId']) as directory:
            _require(_same_identity(os.fstat(directory),self.intent['directoryIdentity'],directory=True))

    @contextmanager
    def role(self, role):
        record = self.intent['roles'][role]
        directory = _open_child_directory(self.directory,role,expected=record['directoryIdentity'])
        transfer = None
        try:
            transfer = _open_child_directory(directory,'transfer',expected=record['transferIdentity'])
            _require(set(os.listdir(directory)) <= {'input.ipa','transfer','App.app'}
                and set(os.listdir(transfer)) <= {'source.ipa','app'})
            yield directory,transfer
        finally:
            if transfer is not None:os.close(transfer)
            os.close(directory)

    def empty(self):
        self.bounds()
        for role in self.owner._roles:
            with self.role(role) as (directory,transfer):
                _require(set(os.listdir(directory)) == {'transfer'} and not os.listdir(transfer))

    def check_run(self):
        self.bounds()
        name = self.intent['operationId']
        if _node(self.runs,name,directory=True) is None:
            _require(self.record is not None and self.record['state'] in {'discarded','completed'})
            return
        directory = _open_child_directory(self.runs,name)
        try:
            self.run_directory_identity = _identity_info(os.fstat(directory))
            if 'intent.json' in os.listdir(directory):
                _require(_read_json_at(directory,'intent.json') == {
                    'kind':'ios-mobile-preparation-hold','contextDigest':self.intent['contextDigest']})
                _retire_record_temps(directory)
                _require(set(os.listdir(directory)) == {'intent.json'})
            else:
                _require(not os.listdir(directory) and self.record is not None
                    and self.record['state'] in {'discarded','completed'})
        finally:os.close(directory)

    def remove_run_hold(self):
        self.bounds();self.empty()
        name = self.intent['operationId']
        if _node(self.runs,name,directory=True) is None:
            _require(self.run_directory_identity is None);return
        directory = _open_child_directory(self.runs,name,expected=self.run_directory_identity)
        try:
            _require(self.run_directory_identity is not None)
            names = set(os.listdir(directory));_require(names <= {'intent.json'})
            if names:
                _require(_read_json_at(directory,'intent.json') == {
                    'kind':'ios-mobile-preparation-hold','contextDigest':self.intent['contextDigest']})
                _unlink_file(directory,'intent.json',os.stat('intent.json',dir_fd=directory,follow_symlinks=False),self)
            self.bounds()
            _require(_same_identity(os.stat(name,dir_fd=self.runs,follow_symlinks=False),
                                   self.run_directory_identity,directory=True))
            os.rmdir(name,dir_fd=self.runs);os.fsync(self.runs)
        finally:os.close(directory)


def _validate_role(session, role, state):
    record = session.intent['roles'][role]
    roots = session.record['roots'][role]
    with session.role(role) as (directory,transfer):
        app = _require_node(directory,'App.app',roots['app'],directory=True)
        transferred = _require_node(transfer,'app',roots['transferApp'],directory=True)
        snapshot = _require_node(transfer,'source.ipa',roots['snapshot'],directory=False)
        if _node(directory,'input.ipa',directory=False) is None:
            _require(app is None and transferred is None and snapshot is None);return None
        archive = _open_regular_at(directory,'input.ipa',expected=record['archiveIdentity'])
        try:
            body = _read_fd(archive,MAX_TRANSFER_BYTES,allow_empty=True)
            if state['state'] == 'empty':
                _require(len(body) <= record['bytes'] and app is None and transferred is None and snapshot is None)
            else:
                _require(len(body) == record['bytes'] and hashlib.sha256(body).hexdigest() == record['sha256'])
            if snapshot is not None:
                fd = _open_regular_at(transfer,'source.ipa',expected=roots['snapshot'])
                try:copied = _read_fd(fd,MAX_TRANSFER_BYTES,allow_empty=True)
                finally:os.close(fd)
                _require(len(copied) <= len(body) and copied == body[:len(copied)])
            if app is None and transferred is None:return ({},set())
            _require(state['state'] in {'preparing','prepared'})
            files,directories = _expected_tree(archive,session.owner._operation_root(session.intent['operationId'])/role/'input.ipa')
            for parent,name,identity in ((directory,'App.app',app),(transfer,'app',transferred)):
                if identity is not None:
                    fd = _open_app_directory(parent,name,identity)
                    try:_tree(fd,files,directories,session)
                    finally:os.close(fd)
            return files,directories
        finally:os.close(archive)


def _dispose_role(session, role, state):
    expected = _validate_role(session,role,state)
    roots = session.record['roots'][role]
    with session.role(role) as (directory,transfer):
        for parent,name,identity in ((directory,'App.app',roots['app']),(transfer,'app',roots['transferApp'])):
            if _require_node(parent,name,identity,directory=True) is not None:
                _require(expected is not None)
                fd = _open_app_directory(parent,name,identity)
                try:_tree(fd,*expected,session,remove=True)
                finally:os.close(fd)
                session.bounds();_require_node(parent,name,identity,directory=True)
                os.rmdir(name,dir_fd=parent);os.fsync(parent)
        if _require_node(transfer,'source.ipa',roots['snapshot'],directory=False) is not None:
            _unlink_file(transfer,'source.ipa',os.stat('source.ipa',dir_fd=transfer,follow_symlinks=False),session)
        _require(not os.listdir(transfer) and 'App.app' not in os.listdir(directory))
        if 'input.ipa' in os.listdir(directory):
            info = os.stat('input.ipa',dir_fd=directory,follow_symlinks=False)
            _require(_same_identity(info,session.intent['roles'][role]['archiveIdentity']))
            _unlink_file(directory,'input.ipa',info,session)


def require_cleanup(capability, owner, run_store):
    _require(type(capability) is IOSPreparationCleanupCapability and type(owner) is IOSMobileOperationStore
        and owner.run_store is run_store and not capability._consumed)
    session = capability._session
    _require(type(session) is _Recovery and session.owner is owner and session.capability is capability
        and owner._recoveries.get(session) is session
        and (capability.operation_id,capability.request_digest,capability.context_digest,capability.scope_digest,
             capability.expected_reserved_bytes) == (session.intent['operationId'],session.intent['requestDigest'],
             session.intent['contextDigest'],owner.definition.scope_digest,session.intent['reservedBytes']))
    session.bounds();session.empty()
    intent,_ = owner._records(capability.operation_id,session.directory)
    _require(contracts.digest(intent) == session.binding['intentDigest'])
    record = _read_record(session.directory,intent,owner._roles)
    _require(record is not None and record['state'] in {'discarded','completed'})
    capability._consumed = True
    return session


@contextmanager
def preparation_recovery(owner, operation_id, request_digest, *, cancellation, deadline_monotonic):
    session = None
    try:
        _require(type(owner) is IOSMobileOperationStore and callable(getattr(cancellation,'is_set',None))
            and type(deadline_monotonic) in (int,float) and math.isfinite(deadline_monotonic)
            and not owner._closed and not cancellation.is_set() and time.monotonic() < deadline_monotonic)
        contracts.validate_id(operation_id);contracts.validate_digest(request_digest)
        with ExitStack() as stack:
            stack.enter_context(owner.run_store.repair_scope_lease('mobile-device',owner.definition.scope_digest))
            run_root = _walk_directory(owner.run_store.root);stack.callback(os.close,run_root)
            vm = _open_regular_at(run_root,'.vm-lock',writable=True);stack.callback(os.close,vm)
            fcntl.flock(vm,fcntl.LOCK_EX|fcntl.LOCK_NB)
            directory = stack.enter_context(owner._directory(operation_id))
            intent,state = owner._records(operation_id,directory)
            _require(intent['requestDigest'] == request_digest and 'nativeBindingDigest' not in state
                and 'native.json' not in os.listdir(directory))
            producer = _open_regular_at(directory,'producer.lock',expected=intent['producerIdentity'],writable=True)
            stack.callback(os.close,producer);fcntl.flock(producer,fcntl.LOCK_EX|fcntl.LOCK_NB)
            runs = _open_child_directory(run_root,'runs');stack.callback(os.close,runs)
            session = _Recovery(owner,intent,directory,producer,vm,runs,cancellation,deadline_monotonic)
            with owner._changed:
                _require(not owner._closed and not owner._active and not owner._callbacks)
                owner._recoveries[session] = session
            session.bounds();_retire_record_temps(directory)
            _require(set(os.listdir(directory)) <= {'intent.json','state.json','producer.lock','recovery.json',*owner._roles})
            session.record = _read_record(directory,intent,owner._roles)
            row = owner.run_store.status(operation_id)
            _require(row['requestDigest'] == request_digest and row['state'] in {'admitted','quarantined','failed','cancelled'}
                and (row['reservedBytes'] == intent['reservedBytes'] if row['state'] in {'admitted','quarantined'}
                     else row['reservedBytes'] == 0))
            if session.record is None:
                _require(row['reservedBytes'] > 0)
                roots = {}
                for role in owner._roles:
                    with session.role(role) as (selected,transfer):
                        roots[role] = {'app':_node(selected,'App.app',directory=True),
                            'transferApp':_node(transfer,'app',directory=True),
                            'snapshot':_node(transfer,'source.ipa',directory=False)}
                session.record = {'schemaVersion':1,'kind':'ios-preparation-disposal-v1',
                    **session.binding,'state':'discarding','roots':roots}
                for role in owner._roles:_validate_role(session,role,state['roles'][role])
                _write_new_at(directory,'recovery.json',session.record)
            session.check_run()
            if session.record['state'] == 'discarding':
                _require(row['reservedBytes'] > 0)
                for role in owner._roles:_validate_role(session,role,state['roles'][role])
                for role in owner._roles:_dispose_role(session,role,state['roles'][role])
                session.empty();session.record['state'] = 'discarded'
                _replace_at(directory,'recovery.json',session.record)
            else:session.empty()
            session.bounds()
            capability = IOSPreparationCleanupCapability(operation_id,request_digest,owner.definition.scope_digest,
                intent['contextDigest'],intent['reservedBytes'],session)
            session.capability = capability
            try:
                yield capability
                row = owner.run_store.status(operation_id)
                if capability._consumed and row['requestDigest'] == request_digest and row['reservedBytes'] == 0:
                    session.record['state'] = 'completed';_replace_at(directory,'recovery.json',session.record)
            finally:session.active = False
    except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError):
        raise IOSMobileOperationError() from None
    finally:
        if session is not None:
            session.active = False
            with owner._changed:
                owner._recoveries.pop(session,None);owner._changed.notify_all()
