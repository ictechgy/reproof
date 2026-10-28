"""Private, unqualified Android recovery authority and diagnostic journal.

Nothing in this module enables normal Lab recovery. Journal bytes are observations,
not provenance or resumable authority. Live capabilities are process/thread owned.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from reproof.core import ContractError, identifier
from .authority import (DeviceAuthority, NativeRecoveryLease, ParentGrant,
                        QUALIFIED_NATIVE_CLOCKS, MAX_SEQUENCE, MAX_NS)

_ID = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_KIND = 'reproof-android-recovery-qualification'
_BINDING_KEYS = frozenset(('schemaVersion', 'kind', 'attemptId', 'sessionId',
    'deviceScopeDigest', 'applicationId', 'artifactDigest', 'profileDigest',
    'helperArtifactDigest', 'priorOwnershipGeneration', 'hostIncarnation',
    'helperIncarnation', 'providerIncarnation'))
_PHASES = ('session-bound', 'producer-start-pending', 'producer-running', 'quarantined',
    'old-retire-pending', 'old-terminal', 'old-exit-pending', 'old-exited',
    'probe-start-pending', 'probe-running', 'fresh-probe-pending', 'probe-frame',
    'probe-retire-pending', 'probe-terminal', 'probe-exit-pending', 'probe-exited',
    'reconcile-pending', 'reconciled', 'release-pending', 'released')
_PENDING = {'old-retire':'old-retire-pending', 'old-exit':'old-exit-pending',
    'fresh-probe':'fresh-probe-pending', 'probe-retire':'probe-retire-pending',
    'probe-exit':'probe-exit-pending'}
MAX_JOURNAL_BYTES = 64 * 1024
MAX_RECORDS = 32
MAX_PHASE_NS = 30_000_000_000
MAX_ATTEMPT_NS = 180_000_000_000


def _require(value, message='Android recovery qualification unavailable'):
    if not value:
        raise ContractError(message)


def _encoded(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ContractError('Invalid Android recovery record') from None
    _require(len(raw) <= 8192, 'Android recovery record limit exceeded')
    return raw


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _valid_digest(value):
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _binding(value):
    _require(type(value) is dict and set(value) == _BINDING_KEYS, 'Invalid recovery binding')
    result = dict(value)
    _require(type(result['schemaVersion']) is int and result['schemaVersion'] == 1
        and result['kind'] == _KIND, 'Invalid recovery binding')
    for key in ('attemptId', 'sessionId', 'hostIncarnation', 'helperIncarnation', 'providerIncarnation'):
        _require(type(result[key]) is str and _ID.fullmatch(result[key]) is not None, 'Invalid recovery binding')
    for key in ('deviceScopeDigest', 'artifactDigest', 'profileDigest', 'helperArtifactDigest'):
        _require(_valid_digest(result[key]), 'Invalid recovery binding')
    _require(type(result['applicationId']) is str, 'Invalid recovery binding')
    identifier(result['applicationId'])
    generation = result['priorOwnershipGeneration']
    _require(type(generation) is int and 1 <= generation < MAX_SEQUENCE, 'Invalid recovery binding')
    return result


def _identity(info):
    return info.st_dev, info.st_ino


def _directory(path):
    """Open every component without following links; caller owns returned FD."""
    path = Path(path).absolute()
    _require(len(path.parts) <= 128 and '..' not in path.parts, 'Unsafe recovery journal path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.parts[1:]:
            new = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(fd):
    info = os.fstat(fd)
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        and info.st_uid == os.getuid() and info.st_size <= MAX_JOURNAL_BYTES,
        'Unsafe recovery journal record')
    raw = os.pread(fd, MAX_JOURNAL_BYTES + 1, 0)
    _require(len(raw) == info.st_size and len(raw) <= MAX_JOURNAL_BYTES,
        'Recovery journal changed')
    return raw


class QualificationJournal:
    """Fresh append-only diagnostic ledger; never a receipt authority."""
    @classmethod
    def create(cls, path, binding):
        value = _binding(binding)
        path = Path(path).absolute()
        parent = root = record = None
        try:
            parent = _directory(path.parent)
            os.mkdir(path.name, 0o700, dir_fd=parent)
            root = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            record = os.open('events.jsonl', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=root)
            obj = cls.__new__(cls)
            obj._path, obj._parent, obj._root, obj._record = path, parent, root, record
            obj._binding = value
            obj.binding_digest = _digest(value)
            obj._pid, obj._thread = os.getpid(), threading.get_ident()
            obj._closed = False
            obj._raw = b''
            obj._count = 0
            obj._state = None
            obj._append('session-bound', binding=value)
            os.fsync(root)
            os.fsync(parent)
            return obj
        except BaseException:
            for fd in (record, root, parent):
                if fd is not None:
                    os.close(fd)
            # Never delete a newly created or externally changed directory.
            raise

    @property
    def state(self):
        self.check()
        return self._state

    @property
    def binding(self):
        self.check()
        return dict(self._binding)

    def check(self):
        _require(not self._closed and self._pid == os.getpid()
            and self._thread == threading.get_ident(), 'Live recovery journal owner required')
        _require(_digest(self._binding) == self.binding_digest, 'Recovery binding changed')
        with_parent = _directory(self._path.parent)
        try:
            _require(_identity(os.fstat(with_parent)) == _identity(os.fstat(self._parent)),
                'Recovery journal parent changed')
        finally:
            os.close(with_parent)
        root = os.stat(self._path.name, dir_fd=self._parent, follow_symlinks=False)
        record = os.stat('events.jsonl', dir_fd=self._root, follow_symlinks=False)
        _require(stat.S_ISDIR(root.st_mode) and _identity(root) == _identity(os.fstat(self._root))
            and _identity(record) == _identity(os.fstat(self._record))
            and _read(self._record) == self._raw, 'Recovery journal changed')

    def _append(self, phase, **values):
        self.check()
        _require(self._count < MAX_RECORDS, 'Recovery journal record limit exceeded')
        record = dict(schemaVersion=1, index=self._count, phase=phase,
            bindingDigest=self.binding_digest, previousDigest=hashlib.sha256(self._raw).hexdigest(), **values)
        raw = _encoded(record) + b'\n'
        _require(len(self._raw) + len(raw) <= MAX_JOURNAL_BYTES, 'Recovery journal size limit exceeded')
        offset = len(self._raw)
        while raw:
            written = os.pwrite(self._record, raw, offset)
            _require(written > 0, 'Recovery journal write failed')
            offset += written
            raw = raw[written:]
        os.fsync(self._record)
        self._raw += _encoded(record) + b'\n'
        self._count += 1
        self._state = phase
        self.check()

    def advance(self, expected, phase, evidence_digest=None):
        self.check()
        _require(self._state == expected and expected in _PHASES[:-1]
            and _PHASES[_PHASES.index(expected) + 1] == phase, 'Recovery journal transition rejected')
        _require(evidence_digest is None or _valid_digest(evidence_digest), 'Invalid recovery evidence digest')
        self._append(phase, evidenceDigest=evidence_digest)

    def block(self, code='recovery-unconfirmed'):
        self.check()
        _require(type(code) is str and _ID.fullmatch(code) is not None, 'Invalid recovery failure code')
        if self._state == 'blocked':
            return
        _require(self._state != 'released', 'Completed recovery journal is immutable')
        self._append('blocked', code=code)

    def close(self):
        if self._closed:
            return
        self._closed = True
        for fd in (self._record, self._root, self._parent):
            os.close(fd)


def inspect_qualification_journal(path):
    """Bounded read-only diagnostics. No issuer, lease, token or resume is returned."""
    root = record = None
    try:
        root = _directory(path)
        record = os.open('events.jsonl', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
        raw = _read(record)
        lines = raw.splitlines(keepends=True)
        _require(1 <= len(lines) <= MAX_RECORDS, 'Invalid recovery journal')
        prior = b''
        binding_digest = None
        phase = None
        for index, line in enumerate(lines):
            _require(line.endswith(b'\n') and len(line) <= 8193, 'Incomplete recovery journal')
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                raise ContractError('Invalid recovery journal') from None
            _require(type(value) is dict and type(value.get('schemaVersion')) is int
                and value.get('schemaVersion') == 1 and _encoded(value) + b'\n' == line
                and type(value.get('index')) is int and value['index'] == index
                and type(value.get('phase')) is str
                and value.get('previousDigest') == hashlib.sha256(prior).hexdigest(), 'Invalid recovery journal')
            base_keys = {'schemaVersion', 'index', 'phase', 'bindingDigest', 'previousDigest'}
            extra_key = 'binding' if index == 0 else 'code' if value['phase'] == 'blocked' else 'evidenceDigest'
            _require(set(value) == base_keys | {extra_key}, 'Invalid recovery journal')
            if extra_key == 'evidenceDigest':
                _require(value[extra_key] is None or _valid_digest(value[extra_key]), 'Invalid recovery journal')
            if extra_key == 'code':
                _require(type(value[extra_key]) is str and _ID.fullmatch(value[extra_key]) is not None,
                    'Invalid recovery journal')
            if index == 0:
                binding_digest = _digest(_binding(value.get('binding')))
                _require(value.get('phase') == 'session-bound', 'Invalid recovery journal')
            else:
                next_phase = value.get('phase')
                _require(phase not in {'blocked', 'released'} and (next_phase == 'blocked' or
                    phase in _PHASES[:-1] and next_phase == _PHASES[_PHASES.index(phase) + 1]),
                    'Invalid recovery journal')
            _require(value.get('bindingDigest') == binding_digest, 'Invalid recovery journal')
            phase = value['phase']
            prior += line
        _require(_read(record) == raw, 'Recovery journal changed')
        current = _directory(path)
        try:
            _require(_identity(os.fstat(current)) == _identity(os.fstat(root)), 'Recovery journal changed')
        finally:
            os.close(current)
        return {'schemaVersion':1, 'qualification':'unqualified', 'phase':phase,
            'bindingDigest':binding_digest, 'journalDigest':hashlib.sha256(raw).hexdigest(),
            'resumable':False, 'normalRecoveryEnabled':False}
    finally:
        for fd in (record, root):
            if fd is not None:
                os.close(fd)


@dataclass(frozen=True, slots=True)
class RecoveryGrant:
    _wire_bytes: bytes
    _issuer: object
    _purpose: str
    _sequence: int

    def wire(self):
        self._issuer.require_grant(self)
        return json.loads(self._wire_bytes)


class RecoveryIssuer:
    """Distinct finite-purpose authority; cannot mint ordinary dispatch permits."""
    def __init__(self, device, lease, parent_grant, journal, cancellation):
        _require(type(device) is DeviceAuthority and device.device_kind == 'android'
            and type(lease) is NativeRecoveryLease and type(parent_grant) is ParentGrant
            and type(journal) is QualificationJournal and callable(cancellation), 'Live recovery authority required')
        self._device, self._lease, self._parent_grant = device, lease, parent_grant
        self._journal, self._cancellation = journal, cancellation
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._closed = False
        self._grants = {}
        self._deadlines = {}
        self._grant_channels = {}
        self._sequence = 0
        self._purposes = set()
        self._channels = {}
        self._started_ns = device._authority._require_parent_grant(parent_grant)
        _require(lease._grant is parent_grant, 'Exact recovery parent grant required')
        self.check()
        row = device._authority.store.device(device._device_fingerprint)
        binding = journal.binding
        _require(row['grant_id'] != parent_grant.grant_id
            and row['project_id'] == parent_grant.project_id
            and row['controller_id'] == parent_grant.controller_id
            and binding['deviceScopeDigest'] == device._device_fingerprint
            and binding['priorOwnershipGeneration'] == device.generation
            and binding['hostIncarnation'] == row['host_incarnation']
            and binding['helperIncarnation'] == row['helper_incarnation'], 'Recovery ownership differs')

    def check(self):
        _require(not self._closed and self._pid == os.getpid()
            and self._thread == threading.get_ident(), 'Live recovery issuer required')
        self._journal.check()
        _require(self._journal.state not in {'blocked', 'released'}, 'Recovery attempt is terminal')
        _require(not self._cancellation(), 'Recovery cancelled')
        self._device.require_native_recovery_lease(self._lease)
        now = self._device._authority._require_parent_grant(self._parent_grant)
        _require(now - self._started_ns < MAX_ATTEMPT_NS, 'Recovery attempt expired')
        return now

    def issue(self, purpose, channel):
        self.check()
        _require(purpose in _PENDING and purpose not in self._purposes
            and self._journal.state == _PENDING[purpose], 'Recovery purpose rejected')
        # Only the integration-owned authenticated channel can supply status.
        from .android_recovery_qualification import _RecoveryChannel
        _require(type(channel) is _RecoveryChannel, 'Authenticated recovery channel required')
        channel.require_owned(issuer=self)
        role = 'old' if purpose.startswith('old-') else 'probe'
        _require(channel.role == role, 'Recovery purpose differs from producer')
        status = channel.read_status()
        received = self.check()
        channel.require_owned(issuer=self)
        configuration = channel.configuration
        binding = self._journal.binding
        expected = {key:value for key,value in binding.items()
            if key not in {'hostIncarnation', 'helperIncarnation', 'providerIncarnation'}}
        expected['phase'] = 'session' if role == 'old' else 'probe'
        _require(configuration == expected, 'Recovery configuration differs')
        configuration_digest = _digest(configuration)
        _require(type(status) is dict and status.get('recoveryQualificationVersion') == 1
            and type(status.get('recoveryQualificationVersion')) is int
            and status.get('recoveryQualificationConfigurationDigest') == configuration_digest,
            'Recovery helper protocol differs')
        for key, value in (('hostIncarnation', binding['hostIncarnation']),
                           ('helperIncarnation', channel.helper_incarnation),
                           ('providerIncarnation', channel.provider_incarnation)):
            _require(status.get(key) == value, 'Recovery helper identity differs')
        native = status.get('nativeIncarnation')
        clock = status.get('nativeClockId')
        native_time = status.get('nativeTimeMs')
        process = status.get('nativeProcessIdentity')
        _require(type(native) is str and _ID.fullmatch(native) is not None
            and clock == 'android-elapsed-realtime'
            and type(native_time) is int and 0 <= native_time <= MAX_NS // 1_000_000
            and type(process) is dict and set(process) == {'pid', 'startTicks'}
            and type(process['pid']) is int and 1 <= process['pid'] <= MAX_SEQUENCE
            and type(process['startTicks']) is str
            and re.fullmatch(r'[1-9][0-9]{0,19}', process['startTicks']) is not None,
            'Recovery native identity unavailable')
        identity = (channel, native, process['pid'], process['startTicks'],
                    channel.helper_incarnation, channel.provider_incarnation, configuration_digest)
        if role in self._channels:
            _require(self._channels[role] == identity, 'Recovery producer changed')
        else:
            if role == 'old':
                _require(channel.helper_incarnation == binding['helperIncarnation']
                    and channel.provider_incarnation == binding['providerIncarnation'], 'Original producer required')
            else:
                _require('old' in self._channels and native != self._channels['old'][1]
                    and channel.helper_incarnation != binding['helperIncarnation']
                    and channel.provider_incarnation != binding['providerIncarnation'], 'Fresh recovery producer required')
            self._channels[role] = identity
        rate = QUALIFIED_NATIVE_CLOCKS[('android', clock)]
        host_deadline = min(self._parent_grant.local_deadline_ns,
            received + MAX_PHASE_NS, self._started_ns + MAX_ATTEMPT_NS)
        remaining = max(0, host_deadline - received) // 1_000_000
        duration = remaining * (1_000_000 - rate['maxRateErrorPpm']) // 1_000_000
        _require(duration > rate['mappingUncertaintyMs'], 'Recovery native clock mapping is too uncertain')
        deadline = native_time + duration - rate['mappingUncertaintyMs']
        _require(deadline <= MAX_NS // 1_000_000, 'Recovery deadline overflow')
        self._sequence += 1
        _require(self._sequence <= MAX_SEQUENCE, 'Recovery sequence exhausted')
        wire = dict(schemaVersion=1, purpose=purpose, operationId='recovery_' + uuid.uuid4().hex,
            sequence=self._sequence, configurationDigest=configuration_digest,
            attemptId=binding['attemptId'], sessionId=binding['sessionId'],
            priorOwnershipGeneration=binding['priorOwnershipGeneration'],
            recoveryOwnershipGeneration=binding['priorOwnershipGeneration'] + 1,
            hostIncarnation=binding['hostIncarnation'], helperIncarnation=channel.helper_incarnation,
            providerIncarnation=channel.provider_incarnation, nativeIncarnation=native,
            nativeClockId=clock, nativeDeadlineMs=deadline,
            payloadDigest=_digest({'configurationDigest':configuration_digest, 'purpose':purpose}))
        grant = RecoveryGrant(_encoded(wire), self, purpose, self._sequence)
        self._purposes.add(purpose)
        self._grants[id(grant)] = grant
        self._deadlines[id(grant)] = host_deadline
        self._grant_channels[id(grant)] = channel
        self.check()
        return grant

    def require_grant(self, grant):
        now = self.check()
        _require(type(grant) is RecoveryGrant and self._grants.get(id(grant)) is grant
            and grant._issuer is self and self._journal.state == _PENDING[grant._purpose]
            and now < self._deadlines[id(grant)],
            'Live recovery grant required')
        self._grant_channels[id(grant)].require_owned(issuer=self)

    def close(self):
        self._closed = True
        self._grants.clear()
        self._deadlines.clear()
        self._grant_channels.clear()
        self._channels.clear()
