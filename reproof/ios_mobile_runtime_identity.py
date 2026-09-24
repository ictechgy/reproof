"""Read-only app runtime identity collection under the original native owner."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import secrets
import stat
import time

from . import contracts
from .execution.wire import decode_json
from .ios_device_tools import IOSDeviceQueryDefinition, IOSDeviceToolError, _require
from .ios_mobile_native import IOSMobileNativeOwner, prepared_app
from .ios_mobile_xctest import IOSXCTestLaunch
from .ios_runtime_identity import RUNTIME_IDENTITY_GRADE, validate_ios_runtime_identity
from .repair_android_operation import _read_fd
from .repair_android_signing import _cleanup_known_work


_SOURCE = 'Library/Application Support/ReproLoop/runtime-identity.json'
_MAX_RESULT_BYTES = 256 * 1024
_MAX_IDENTITY_BYTES = 4096
_MAX_OUTPUT_BYTES = 64 * 1024


class _IdentityNotReady(IOSDeviceToolError):
    """A collected, bounded copy found no current launch marker yet."""



@dataclass(frozen=True, slots=True)
class IOSRuntimeIdentityReadObservation:
    context_digest: str
    native_binding_digest: str
    launch_payload_digest: str
    evidence_digest: str
    bundle_id: str
    build_id: str
    profile_digest: str
    run_id: str
    started_at_ms: int
    grade: str = RUNTIME_IDENTITY_GRADE
    sanitation: object = None
    source_role: str | None = None

    def public(self):
        return {'kind': 'ios-runtime-identity-observation',
            'contextDigest': self.context_digest,
            'nativeBindingDigest': self.native_binding_digest,
            'launchPayloadDigest': self.launch_payload_digest,
            'evidenceDigest': self.evidence_digest,
            'bundleId': self.bundle_id, 'buildId': self.build_id,
            'profileDigest': self.profile_digest, 'runId': self.run_id,
            'startedAtMs': self.started_at_ms, 'grade': self.grade,
            'identityConfirmed': True, 'installedArtifactVerified': False,
            'deviceCleanupConfirmed': False, 'executionAuthority': 'none'} | (
                {'sanitation':self.sanitation.public(),'sanitationGrade':self.sanitation.grade}
                if self.sanitation is not None else {}) | (
                {'sourceRole':self.source_role} if self.source_role is not None else {})


def _read_json_file(path, maximum):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                 and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
                 and 0 < info.st_size <= maximum)
        body = _read_fd(descriptor, maximum)
        after = os.fstat(descriptor)
        _require((info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                 == (after.st_size, after.st_mtime_ns, after.st_ctime_ns))
        return decode_json(body)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _runtime_payload(reader, launch):
    _require(type(launch) is IOSXCTestLaunch)
    runner = launch._runner
    _require(runner._launches.get(id(launch)) is launch
             and id(launch) in runner._started and not runner._closed
             and runner.native_owner is reader.native_owner
             and type(runner.query) is IOSDeviceQueryDefinition
             and runner.query.definition_digest == reader.definition.definition_digest)
    with runner._lock:
        sessions = [session for session in runner._sessions if session.launch is launch]
        _require(len(sessions) == 1 and not sessions[0]._closed and not sessions[0]._finished
                 and sessions[0].host_process_running)
    payload = launch.payload
    runtime = payload.get('runtimeIdentity')
    keys={'bundleId', 'buildId', 'profileDigest', 'runId'}
    if reader.native_owner.operations.definition.sanitation_policy_digest is not None:
        keys.add('sanitationPolicyDigest')
    if reader.native_owner.operations.definition.egress_policy_digest is not None:
        keys.add('egressPolicyDigest')
    _require(type(runtime) is dict and set(runtime) == keys
             and payload.get('nativeBindingDigest') == reader.native_owner.binding_digest
             and payload.get('contextDigest') == reader.native_owner.operation.context.digest
             and payload.get('queryDefinitionDigest') == reader.definition.definition_digest)
    return runner, payload, runtime


class IOSRuntimeIdentityReader:
    def __init__(self, definition, native_owner):
        self.definition = definition
        self.native_owner = native_owner
        self._closed = False
        self._queries = None
        self._process_stopped = False
        self._cleanup_unknown = False
        self._pending = {}
        try:
            _require(type(definition) is IOSDeviceQueryDefinition
                     and type(native_owner) is IOSMobileNativeOwner
                     and definition.native_guardian is not None)
            native_owner._check(); definition.verify()
            _require(definition.definition_digest == native_owner.operations.definition.query_definition_digest
                     and definition.udid == native_owner.operations.definition.udid
                     and definition.bundle == native_owner.operations.definition.bundle_id)
            self._queries = definition.open_client(native_owner=native_owner)
            self._work_root_identity = self._queries._root_identity
            with native_owner.operations._changed:
                native_owner._check()
                native_owner.operations._native_clients.add(self)
        except Exception:
            if self._queries is not None:
                self._queries.close(deadline_monotonic=time.monotonic() + 3)
            raise IOSDeviceToolError() from None

    def __repr__(self):
        return '<IOSRuntimeIdentityReader>'

    @property
    def active_processes(self):
        return self._queries.active_processes if self._queries is not None else 0

    def _copy(self, work, cancellation, deadline):
        owner = self.native_owner
        arguments = (str(self.definition.tools.devicectl), 'device', 'copy', 'from', '--device',
            self.definition.identifier, '--domain-type', 'appDataContainer',
            '--domain-identifier', self.definition.bundle, '--source', _SOURCE,
            '--destination', str(work / 'identity.json'), '--json-output', str(work / 'result.json'))
        process = self._queries._owner.run(arguments, work=work, input_bytes=b'', pass_fds=(),
            cancellation=cancellation, deadline_monotonic=deadline,
            watched_files=((work / 'result.json', _MAX_RESULT_BYTES),
                           (work / 'identity.json', _MAX_IDENTITY_BYTES)),
            max_output_bytes=_MAX_OUTPUT_BYTES)
        self._process_stopped = process.terminated
        _require(process.terminated and process.bounded and not process.interrupted)
        owner._check()
        if process.returncode != 0:
            raise _IdentityNotReady()
        try:
            return _read_json_file(work / 'result.json', _MAX_RESULT_BYTES), \
                _read_json_file(work / 'identity.json', _MAX_IDENTITY_BYTES)
        except FileNotFoundError:
            raise _IdentityNotReady() from None

    def _cleanup_pending(self, work):
        expected = self._pending.get(work)
        if expected is None:
            return False
        try:
            root = self.definition.work_root
            root_info = root.lstat()
            selected = work.lstat()
            if ((root_info.st_dev, root_info.st_ino, root_info.st_mode, root_info.st_uid)
                    != self._work_root_identity
                    or (selected.st_dev, selected.st_ino) != expected):
                return False
        except OSError:
            return False
        if not _cleanup_known_work(root, work, {'identity.json', 'result.json'}):
            return False
        try:
            current = root.lstat()
            if ((current.st_dev, current.st_ino, current.st_mode, current.st_uid)
                    != self._work_root_identity):
                return False
        except OSError:
            return False
        self._pending.pop(work, None)
        return True

    def read(self, launch, *, cancellation, deadline_monotonic, stage='launch'):
        # Foreground readiness can precede the app's durable identity write.
        # Retry only missing/stale launch markers after collecting and removing
        # each bounded copy; never reuse a prior run's observation.
        _require(type(deadline_monotonic) in (int, float) and math.isfinite(deadline_monotonic))
        deadline = min(deadline_monotonic, time.monotonic() + 10) if stage == 'launch' else deadline_monotonic
        for attempt in range(32):
            try:
                return self._read_once(launch, cancellation=cancellation,
                    deadline_monotonic=deadline, stage=stage)
            except _IdentityNotReady:
                if (stage != 'launch' or attempt == 31 or self.active_processes != 0
                        or self._pending or self._cleanup_unknown or cancellation.is_set()
                        or time.monotonic() >= deadline):
                    raise IOSDeviceToolError() from None
                time.sleep(min(.1, max(0, deadline-time.monotonic())))
        raise IOSDeviceToolError()

    def _read_once(self, launch, *, cancellation, deadline_monotonic, stage='launch'):
        work = None
        work_identity = None
        try:
            _require(callable(getattr(cancellation, 'is_set', None))
                     and type(deadline_monotonic) in (int, float)
                     and math.isfinite(deadline_monotonic) and not self._closed
                     and not cancellation.is_set() and time.monotonic() < deadline_monotonic
                     and not self._cleanup_unknown)
            runner, payload, runtime = _runtime_payload(self, launch)
            _require(stage in ('launch','cleanup'))
            sanitation=None
            if runtime.get('sanitationPolicyDigest') is not None:
                from .ios_sanitation import policy_from_app
                sanitation=policy_from_app(prepared_app(self.native_owner,payload['role'])._source)
                _require(sanitation is not None and sanitation.digest == runtime['sanitationPolicyDigest']
                    == self.native_owner.operations.definition.sanitation_policy_digest)
            _require(stage == 'launch' or sanitation is not None)
            runner._check_launch(launch)
            self.native_owner._check()
            details = self._queries.query('details', cancellation=cancellation,
                deadline_monotonic=deadline_monotonic)
            _require(details._native_binding_digest == self.native_owner.binding_digest
                     and details.definition_digest == self.definition.definition_digest)
            _require(time.monotonic() < deadline_monotonic and not cancellation.is_set())
            work = self.definition.work_root / ('runtime-identity-' + secrets.token_hex(16))
            work.mkdir(mode=0o700)
            selected = work.lstat()
            work_identity = (selected.st_dev, selected.st_ino)
            self._pending[work] = work_identity
            self._process_stopped = False
            result, identity = self._copy(work, cancellation, deadline_monotonic)
            _require(type(result) is dict and type(result.get('info')) is dict
                     and result['info'].get('outcome') == 'success'
                     and type(result.get('result')) is dict)
            _runtime_payload(self, launch)
            runner._check_launch(launch)
            if (stage == 'launch' and type(identity) is dict
                    and type(identity.get('runId')) is str
                    and identity['runId'] != runtime['runId']):
                raise _IdentityNotReady()
            expected = validate_ios_runtime_identity(identity,
                bundle_id=runtime['bundleId'], build_id=runtime['buildId'],
                profile_digest=runtime['profileDigest'], run_id=runtime['runId'],
                sanitation_policy_digest=sanitation.digest if sanitation is not None else None,
                sanitation_counts=sanitation.counts if sanitation is not None else None,
                sanitation_stage=stage)
            self.native_owner._check()
            evidence_digest = contracts.digest({'details': details.data,
                'copy': result, 'identity': identity})
            result_observation = IOSRuntimeIdentityReadObservation(
                self.native_owner.operation.context.digest, self.native_owner.binding_digest,
                contracts.digest(payload), evidence_digest, expected.bundle_id, expected.build_id,
                expected.profile_digest, expected.run_id, expected.started_at_ms, sanitation=expected.sanitation,
                source_role=payload.get('role'))
            if not self._cleanup_pending(work):
                self._cleanup_unknown = True
                _require(False)
            work = None
            key=contracts.digest(payload)
            if stage == 'launch':self.native_owner._runtime_results[key] = result_observation
            if expected.sanitation is not None:
                self.native_owner._sanitation_results[(key,stage)] = result_observation
            return result_observation
        except BaseException as error:
            if work is not None:
                if self.active_processes == 0:
                    if not self._cleanup_pending(work):
                        self._cleanup_unknown = True
                        _require(False)
                else:
                    self._cleanup_unknown = True
            if isinstance(error, IOSDeviceToolError):
                raise
            if not isinstance(error, Exception):
                raise
            raise IOSDeviceToolError() from None

    def close(self, *, deadline_monotonic=None):
        deadline = time.monotonic() + 3 if deadline_monotonic is None else deadline_monotonic
        _require(type(deadline) in (int, float) and math.isfinite(deadline))
        self._closed = True
        stopped = self._queries.close(deadline_monotonic=deadline) if self._queries is not None else True
        cleaned = stopped and self.active_processes == 0
        if cleaned:
            for work in tuple(self._pending):
                if not self._cleanup_pending(work):
                    cleaned = False
                    break
        if stopped and cleaned:
            with self.native_owner.operations._changed:
                self.native_owner.operations._native_clients.discard(self)
                self.native_owner.operations._changed.notify_all()
        return stopped and cleaned and not self._pending


__all__ = ['IOSRuntimeIdentityReader', 'IOSRuntimeIdentityReadObservation']
