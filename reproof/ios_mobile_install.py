"""Permitted fixed install commands. Tool acknowledgment is not device recovery."""
from contextlib import ExitStack
from dataclasses import dataclass
import json
import math
import os
import sys
import time

from . import contracts
from .execution.wire import MAX_FRAME_BYTES
from .ios_device_guardian import _IOSProcessOwner
from .ios_device_tools import IOSDeviceQueryDefinition, IOSDeviceToolError, _require
from .ios_mobile_native import IOSMobileNativeOwner, prepared_app, require_native_dispatch
from .live.clock_sync import SuspendInclusiveClock
from .repair_android_operation import (_identity_info, _open_child_directory,
    _read_json_at, _replace_at, _write_new_at)


_ROLES = {'install-candidate':'candidate', 'restore-original':'original'}


@dataclass(frozen=True, slots=True)
class IOSInstallObservation:
    command: str
    native_binding_digest: str
    payload_digest: str
    evidence_digest: str

    def public(self):
        return {'kind':'ios-install-command', 'command':self.command,
            'nativeBindingDigest':self.native_binding_digest, 'payloadDigest':self.payload_digest,
            'evidenceDigest':self.evidence_digest, 'toolReportedSuccess':True,
            'hostClientStopped':True, 'installedArtifactVerified':False,
            'deviceCleanupConfirmed':False, 'executionAuthority':'none'}


class _DispatchWatch:
    """Revalidate on the process owner's calling thread; never raise past its reaper."""
    def __init__(self, installer, kind, permit, cancellation, deadline):
        self.installer, self.kind, self.permit = installer, kind, permit
        self.cancellation, self.deadline = cancellation, deadline
        self.ready = self.release = None
        self.granted = self.failed = False
        self.record = None
        self.work = None

    def is_set(self):
        if self.failed:
            return True
        try:
            self.installer._bounds(self.kind, self.permit, self.cancellation, self.deadline)
            if self.ready is not None and not self.granted:
                try: ready = os.read(self.ready, 2)
                except BlockingIOError: return False
                _require(ready == b'R')
                # The native process has checked its fixed tool and original
                # descriptors, but cannot fork an SDK child before this grant.
                self.installer._verify_app(self.kind)
                self.installer._transition(self.work, self.record, 'dispatching')
                self.installer._bounds(self.kind, self.permit, self.cancellation, self.deadline)
                _require(os.write(self.release, b'G') == 1)
                self.granted = True
            return False
        except Exception:
            self.failed = True
            return True


class IOSMobileInstaller:
    def __init__(self, definition, native_owner):
        self._closed = False
        self._processes = _IOSProcessOwner()
        self._queries = None
        self._work_identities = {}
        self.native_owner, self.definition = native_owner, definition
        try:
            _require(type(definition) is IOSDeviceQueryDefinition
                and type(native_owner) is IOSMobileNativeOwner and definition.native_guardian is not None)
            self._verify()
            self._queries = definition.open_client(native_owner=native_owner)
            with native_owner.operations._changed:
                native_owner._check()
                native_owner.operations._native_clients.add(self)
        except Exception:
            if self._queries is not None: self._queries.close()
            raise IOSDeviceToolError() from None

    def __repr__(self):
        return '<IOSMobileInstaller>'

    @property
    def active_processes(self):
        return self._processes.active_processes + (self._queries.active_processes if self._queries else 0)

    def _verify(self):
        _require(not self._closed)
        owner = self.native_owner
        owner._check()
        self.definition.verify()
        _require(self.definition.definition_digest == owner.operations.definition.query_definition_digest
            and self.definition.udid == owner.operations.definition.udid
            and self.definition.bundle == owner.operations.definition.bundle_id
            and json.loads(owner._record)['schemaVersion'] == 2)
        # The guardian interprets the same boot-local, suspend-inclusive clock.
        clock = owner.device._authority.clock_sync._clock
        _require(sys.platform == 'darwin' and type(clock) is SuspendInclusiveClock
            and clock.clock_id.startswith('mach-continuous-'))

    def payload(self, kind):
        try:
            _require(type(kind) is str and kind in _ROLES)
            self._verify()
            owner = self.native_owner
            intent, _ = owner.operations._records(owner.operation.context.operation_id, owner._directory)
            return {'kind':'ios-fixed-install-v1', 'command':kind, 'role':_ROLES[kind],
                'contextDigest':owner.operation.context.digest, 'nativeBindingDigest':owner.binding_digest,
                'queryDefinitionDigest':self.definition.definition_digest,
                'bundleId':self.definition.bundle, 'scopeDigest':owner.operations.definition.scope_digest,
                'artifactDigest':intent['roles'][_ROLES[kind]]['sha256'],
                'appDigest':json.loads(owner._record)['preparedApps'][_ROLES[kind]]}
        except Exception:
            raise IOSDeviceToolError() from None

    def _bounds(self, kind, permit, cancellation, deadline):
        _require(not self._closed and callable(getattr(cancellation,'is_set',None))
            and type(deadline) in (int,float) and math.isfinite(deadline)
            and time.monotonic() < deadline and not cancellation.is_set())
        owner = self.native_owner
        owner._check()
        if owner._recovery_context is None and owner.operation.run.cancelled():
            from .ios_mobile_callbacks import require_cleanup_callback
            _require(kind=='restore-original')
            require_cleanup_callback(owner)
        return require_native_dispatch(owner,permit,contracts.digest(self.payload(kind)))

    def _verify_app(self, kind):
        self.payload(kind)
        return prepared_app(self.native_owner,_ROLES[kind])

    def _transition(self, work, expected, state):
        directory = _open_child_directory(self.native_owner.command_directory,work.name,
            expected=self._work_identities[work])
        try:
            _require(_read_json_at(directory,'state.json') == expected
                and _read_json_at(directory,'intent.json') == dict(expected,state='attempted'))
            value = dict(expected,state=state)
            _replace_at(directory,'state.json',value)
            expected.update(value)
        finally:
            os.close(directory)

    def run(self, kind, *, permit, cancellation, deadline_monotonic):
        acquired = False
        try:
            self._bounds(kind,permit,cancellation,deadline_monotonic)
            owner = self.native_owner
            acquired = owner._command_lock.acquire(blocking=False)
            _require(acquired)
            self._verify_app(kind)
            name = 'command-'+kind+'-work'
            _require(kind not in owner._command_attempts and name not in os.listdir(owner.command_directory))
            watch = _DispatchWatch(self,kind,permit,cancellation,deadline_monotonic)
            # A current, exact physical identity is required before any effect.
            self._queries.query('details',cancellation=watch,deadline_monotonic=deadline_monotonic)
            self._bounds(kind,permit,cancellation,deadline_monotonic)
            payload = self.payload(kind)
            record = {'schemaVersion':1, 'operationId':owner.operation.context.operation_id,
                'nativeBindingDigest':owner.binding_digest,'payload':payload,
                'dispatchOperationId':permit.operation_id,'permitFingerprint':permit.operation_fingerprint,
                'state':'attempted'}
            # Exclusive mkdir permanently fences retries, including death
            # before either metadata record has been published.
            owner._command_attempts.add(kind)
            os.mkdir(name,mode=0o700,dir_fd=owner.command_directory);os.fsync(owner.command_directory)
            work = owner.command_root/name
            directory = _open_child_directory(owner.command_directory,name)
            try:
                self._work_identities[work] = _identity_info(os.fstat(directory))
                _write_new_at(directory,'intent.json',record)
                _write_new_at(directory,'state.json',record)
            finally:
                os.close(directory)
            watch.record = record
            watch.work = work
            with ExitStack() as stack:
                borrowed = stack.enter_context(owner.borrow_descriptors())
                pipes = []
                for _ in range(4):
                    pair = os.pipe();pipes.append(pair)
                    if len(pipes)<4:
                        for fd in pair:stack.callback(os.close,fd)
                    else:
                        stack.callback(os.close,pair[1])
                (live_read,live_write),(ready_read,ready_write),(grant_read,grant_write), \
                    (completion_read,completion_write) = pipes
                watch.ready,watch.release = ready_read,grant_write
                os.set_blocking(ready_read,False)
                descriptors = (borrowed.producer_fd,borrowed.operation_directory_fd,
                               borrowed.device_fd,borrowed.device_directory_fd,live_read,ready_write,grant_read,
                               completion_write)
                now = self._bounds(kind,permit,cancellation,deadline_monotonic)
                # Start from an earlier authority clock sample so conversion
                # cannot extend the caller's monotonic deadline.
                native_deadline = min(permit.deadline_ns,owner.native_deadline_ns,
                    now+int(max(0,deadline_monotonic-time.monotonic())*1_000_000_000))
                extra=(str(owner.command_directory),) if owner._recovery_context is not None else ()
                if extra:descriptors+=(owner.command_directory,)
                command = (str(self.definition.native_guardian.path),*map(str,descriptors[:5]),
                    owner.operations.definition.scope_digest,str(self.definition.tools.devicectl),
                    self.definition.tools.sha256,kind,self.definition.identifier,self.definition.bundle,str(work),
                    str(ready_write),str(grant_read),str(native_deadline),*extra,str(completion_write))
                owner.require_descriptors(borrowed)
                try:
                    result = self._processes.run(command,work=work,input_bytes=b'',pass_fds=descriptors,
                        cancellation=watch,deadline_monotonic=deadline_monotonic,
                        watched_files=((work/'result.json',MAX_FRAME_BYTES),),max_output_bytes=65536,
                        completion_read=completion_read,live_write=live_write)
                finally:
                    if completion_read is not None:
                        try:os.close(completion_read)
                        except OSError:pass
                        completion_read=None
            _require(watch.granted and not watch.failed and result.terminated and result.bounded
                and not result.interrupted and result.returncode == 0)
            self._bounds(kind,permit,cancellation,deadline_monotonic)
            self._verify_app(kind)
            evidence = self._queries._read_result(work)
            apps = evidence.get('installedApplications')
            _require(type(apps) is list and len(apps) == 1 and type(apps[0]) is dict
                and apps[0].get('bundleID') == self.definition.bundle)
            observation = IOSInstallObservation(kind,owner.binding_digest,
                contracts.digest(payload),contracts.digest(evidence))
            self._transition(work,record,'tool-succeeded')
            owner._install_intent_digests[kind] = contracts.digest(dict(record,state='attempted'))
            owner._install_results[kind] = observation
            return observation
        except BaseException as error:
            if not isinstance(error,Exception):
                self._closed = True
                self._processes.close(deadline_monotonic=time.monotonic()+3)
                raise
            raise IOSDeviceToolError() from None
        finally:
            if acquired: self.native_owner._command_lock.release()

    def observe_installed(self, observation, *, cancellation, deadline_monotonic):
        from .ios_mobile_identity import observe_installed
        return observe_installed(self,observation,cancellation=cancellation,deadline_monotonic=deadline_monotonic)

    def close(self, *, deadline_monotonic=None):
        deadline = time.monotonic()+3 if deadline_monotonic is None else deadline_monotonic
        _require(type(deadline) in (int,float) and math.isfinite(deadline))
        self._closed = True
        stopped = self._processes.close(deadline_monotonic=deadline)
        queries_stopped = self._queries.close(deadline_monotonic=deadline) if self._queries else True
        if stopped and queries_stopped:
            with self.native_owner.operations._changed:
                self.native_owner.operations._native_clients.discard(self)
                self.native_owner.operations._changed.notify_all()
        return stopped and queries_stopped
