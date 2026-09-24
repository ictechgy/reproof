"""Fixed scoped SDK dispatch under the original Android native ownership."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import selectors
import signal
import subprocess
import threading
import time

from . import contracts
from .adb_endpoint import ScopedAdbClient
from .android_native_calls import (
    MAX_OUTPUT_BYTES, _LIMITS, _state, prepare_call, prepare_inspector_call, validate_workspace,
)
from .ios_provisioning_cms import _public_file_digest
from .repair_android_operation import (
    AndroidOperationError, _require, _open_child_directory,
    _open_regular_at, _read_fd, _replace_at,
)
from .repair_android_signing import _ProcessOwner, _ProcessResult
from .repair_signing_recovery import _OwnedProcess


@dataclass(frozen=True, slots=True)
class AndroidGuardianTools:
    path: Path
    sha256: str

    def __post_init__(self):
        object.__setattr__(self, 'path', Path(self.path))
        self.verify()

    def verify(self):
        try:
            contracts.validate_digest(self.sha256)
            _require(self.path.is_absolute() and self.path.resolve(strict=True) == self.path
                and os.access(self.path, os.X_OK) and _public_file_digest(self.path) == self.sha256,
                'android_native_guardian_changed')
        except (OSError, RuntimeError, TypeError, ValueError, contracts.ContractError):
            raise AndroidOperationError('android_native_guardian_changed') from None

    @property
    def definition_digest(self):
        return contracts.digest({'kind': 'android-process-guardian-v1',
                                 'path': str(self.path), 'sha256': self.sha256})


def _collected(process):
    return process.poll() is not None and _ProcessOwner._group_empty(process.pid)


def _bounds(cancellation, deadline):
    _require(callable(getattr(cancellation, 'is_set', None))
        and type(deadline) in (int, float) and math.isfinite(deadline), 'android_native_bounds')
    _require(not cancellation.is_set() and time.monotonic() < deadline, 'android_native_cancelled')


def _require_client(operations, descriptors, client):
    _require(type(client) is ScopedAdbClient and client.adb == operations.config.tools.adb
        and client.adb_sha256 == operations.config.tools.adb_digest
        and client.serial == operations.config.serial
        and client.work_root == operations._root(descriptors.operation_id) / 'staging'
        and client.endpoint.definition_digest == operations.config.adb_endpoint.definition_digest,
        'android_native_binding')


def _read(slot, name, record):
    descriptor = _open_regular_at(slot, name, expected=record['files'][name])
    try:
        return _read_fd(descriptor, _LIMITS[name])
    finally:
        os.close(descriptor)


def _native_result(slot, record, descriptors, process, envelope):
    _require(hashlib.sha256(_read(slot, 'request.plist', record)).hexdigest() == record['requestDigest'],
             'android_native_result')
    _require(type(envelope) is dict and set(envelope) == {
        'schemaVersion', 'contextDigest', 'status', 'hostClientStopped', 'deviceCleanupConfirmed'}
        and type(envelope['schemaVersion']) is int and envelope['schemaVersion'] == 1
        and envelope['contextDigest'] == descriptors.context_digest
        and envelope['status'] in ('succeeded', 'failed')
        and envelope['hostClientStopped'] is True and envelope['deviceCleanupConfirmed'] is False,
        'android_native_result')
    result = plistlib.loads(_read(slot, 'native-result.plist', record))
    _require(type(result) is dict and set(result) == {'returnCode', 'stdout', 'stderr', 'bounded'}
        and type(result['returnCode']) is int and -128 <= result['returnCode'] <= 255
        and type(result['bounded']) is bool
        and all(type(result[name]) is bytes and len(result[name]) <= record['maximumOutputBytes']
                for name in ('stdout', 'stderr')), 'android_native_result')
    state = 'succeeded' if result['returnCode'] == 0 and result['bounded'] else 'failed'
    _require(envelope['status'] == state, 'android_native_result')
    for name, expected in (('start.json', 'started'), ('termination.json', state)):
        value = json.loads(_read(slot, name, record))
        _require(value == {
            'schemaVersion': 1, 'operationId': descriptors.operation_id,
            'requestDigest': descriptors.request_digest, 'contextDigest': descriptors.context_digest,
            'scopeDigest': descriptors.scope_digest, 'definitionDigest': descriptors.configuration_digest,
            'ownerPid': process.pid, 'state': expected,
            'recordMeaning': 'owner-start' if name == 'start.json' else 'exit-intent',
        }, 'android_native_result')
    return result


def _retire(operations, descriptors, call, directory, slot, record, result, guardian):
    # Only the dispatcher calls this, after collecting the exact child and its
    # gateway. Recovery JSON cannot enter this path or authorize cost release.
    with operations._mutex:
        operations.require_native_descriptors(descriptors)
        intent = operations._intent(descriptors.operation_id, descriptors.operation_directory_fd)
        validate_workspace(descriptors.operation_directory_fd, intent)
        state = _state(directory, intent)
        _require(state['slots'][call.slot] == record, 'android_native_result')
        record['state'] = 'retiring'
        state['slots'][call.slot] = record
        _replace_at(directory, 'state.json', state)
        for name in _LIMITS:
            descriptor = _open_regular_at(slot, name, expected=record['files'][name])
            try:
                os.unlink(name, dir_fd=slot)
            finally:
                os.close(descriptor)
        os.fsync(slot)
        state['historyDigest'] = contracts.digest({
            'previous': state['historyDigest'], 'sequence': call.sequence,
            'requestDigest': call.request_digest, 'guardianDigest': guardian.sha256,
            'returnCode': result['returnCode'], 'bounded': result['bounded'],
            'hostClientStopped': True, 'deviceCleanupConfirmed': False,
        })
        state['slots'][call.slot] = None
        _replace_at(directory, 'state.json', state)


def run_native_adb(operations, descriptors, guardian, client, arguments, *, cancellation,
                   deadline_monotonic, input_bytes=b'', slot='command', max_output_bytes=MAX_OUTPUT_BYTES,
                   _started=None,_recovery_probe=None):
    """Execute a synchronous SDK call within the issuing operation phase."""
    _bounds(cancellation, deadline_monotonic)
    operations.require_native_descriptors(descriptors)
    _require(type(guardian) is AndroidGuardianTools, 'android_native_binding')
    _require_client(operations, descriptors, client)
    guardian.verify()
    command, gateway = client.prepare_command(arguments)
    return _run_guardian(operations,descriptors,guardian,client,command,gateway,
        cancellation=cancellation,deadline_monotonic=deadline_monotonic,input_bytes=input_bytes,
        slot=slot,max_output_bytes=max_output_bytes,_started=_started,_recovery_probe=_recovery_probe)


def run_native_inspector(operations,descriptors,guardian,apk,*,cancellation,deadline_monotonic):
    _bounds(cancellation,deadline_monotonic)
    operations.require_native_descriptors(descriptors)
    _require(type(guardian) is AndroidGuardianTools,'android_native_binding')
    guardian.verify()
    return _run_guardian(operations,descriptors,guardian,None,None,None,
        cancellation=cancellation,deadline_monotonic=deadline_monotonic,inspector_apk=Path(apk))


def _run_guardian(operations,descriptors,guardian,client,command,gateway,*,cancellation,
                  deadline_monotonic,input_bytes=b'',slot='command',max_output_bytes=MAX_OUTPUT_BYTES,
                  _started=None,inspector_apk=None,_recovery_probe=None):
    process = control = None
    private = []
    selector = selectors.DefaultSelector()
    gateway_collected = gateway is None
    acknowledged = False
    interrupted = False
    parsed = None
    buffers = [bytearray(), bytearray()]
    reader = writer = None
    try:
        if _recovery_probe is not None:
            from .android_recovery_helper import _RecoveryHelperProbe
            _require(type(_recovery_probe) is _RecoveryHelperProbe and slot=='instrumentation',
                     'android_recovery_helper_probe')
            _recovery_probe.require_binding(operations,descriptors,client)
        call = (prepare_inspector_call(operations,descriptors,inspector_apk,max_output_bytes=max_output_bytes)
                if inspector_apk is not None else prepare_call(operations, descriptors, command,
                    input_bytes=input_bytes,slot=slot,max_output_bytes=max_output_bytes))
        intent = operations._intent(descriptors.operation_id, descriptors.operation_directory_fd)
        directory = _open_child_directory(descriptors.operation_directory_fd, 'native-calls',
                                         expected=intent['nativeCalls']['identity'])
        private.append(directory)
        slot_fd = _open_child_directory(directory, slot, expected=intent['nativeCalls']['slots'][slot])
        private.append(slot_fd)
        record = _state(directory, intent)['slots'][slot]
        _require(record['sequence'] == call.sequence and record['requestDigest'] == call.request_digest
                 and record['state'] == 'prepared', 'android_native_binding')
        def opened(name, writable=False):
            descriptor = _open_regular_at(slot_fd, name, expected=record['files'][name], writable=writable)
            private.append(descriptor)
            return descriptor
        reader, writer = os.pipe()
        private.append(reader)
        fds = (opened('request.plist'), slot_fd, descriptors.producer_fd, descriptors.device_fd,
            reader, opened('command.plist'), opened('native-result.plist', True),
            opened('start.json', True), opened('termination.json', True),
            descriptors.operation_directory_fd, descriptors.device_directory_fd, opened('stdin.bin'))
        with operations._mutex:
            operations.require_native_descriptors(descriptors)
            _bounds(cancellation, deadline_monotonic)
            guardian.verify()
            process = subprocess.Popen((str(guardian.path), *map(str, fds)),
                pass_fds=fds, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, close_fds=True, cwd=operations._root(descriptors.operation_id)/'staging',
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
            control = _OwnedProcess(process, writer)
            writer = None
            operations._native_controls[control] = call
            if _started is not None:
                _started.set()
        if _recovery_probe is not None:
            _recovery_probe.run(process)
        for index, stream in enumerate((process.stdout, process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, index)
        stop_deadline = None
        while True:
            now = time.monotonic()
            if cancellation.is_set() or now >= deadline_monotonic or operations._closed:
                interrupted = True
            if interrupted and stop_deadline is None:
                control.close_liveness()
                if process.poll() is None:
                    process.send_signal(signal.SIGCONT)
                stop_deadline = now + 3
            if stop_deadline is not None and now >= stop_deadline:
                break
            for selected, _ in selector.select(.02):
                chunk = os.read(selected.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(selected.fileobj)
                else:
                    buffers[selected.data].extend(chunk[:max(0, 4097-len(buffers[selected.data]))])
                    if len(buffers[selected.data]) > 4096:
                        interrupted = True
            if parsed is None and not interrupted and b'\n' in buffers[0]:
                try:
                    _require(not buffers[1] and bytes(buffers[0]).count(b'\n') == 1, 'android_native_result')
                    envelope = json.loads(buffers[0])
                    operations.require_native_descriptors(descriptors)
                    validate_workspace(descriptors.operation_directory_fd, intent)
                    parsed = _native_result(slot_fd, record, descriptors, process, envelope)
                    if gateway is not None:
                        client.finish_gateway(gateway, deadline_monotonic=deadline_monotonic)
                    gateway_collected = True
                    acknowledged = control.acknowledge()
                    _require(acknowledged, 'android_native_result')
                except (OSError, RuntimeError, ValueError, TypeError, plistlib.InvalidFileException):
                    interrupted = True
                    parsed = None
            if process.poll() is not None and not selector.get_map():
                break
        # Another owner can close liveness while select() is draining EOF.
        # Recheck cancellation at the final observation boundary as well.
        interrupted = interrupted or cancellation.is_set() or time.monotonic() >= deadline_monotonic or operations._closed
        terminated = _collected(process)
        normal = (terminated and acknowledged and parsed is not None and not interrupted
                  and process.returncode == (0 if parsed['returnCode'] == 0 and parsed['bounded'] else 1))
        if normal:
            _retire(operations, descriptors, call, directory, slot_fd, record, parsed, guardian)
            return _ProcessResult(parsed['returnCode'], parsed['stdout'], parsed['stderr'], True, parsed['bounded'], False)
        return _ProcessResult(process.returncode, b'', b'', terminated, False, interrupted)
    finally:
        if writer is not None:
            os.close(writer)
        if control is not None:
            control.close_liveness()
        if not gateway_collected:
            try:
                client.finish_gateway(gateway, deadline_monotonic=time.monotonic() + 2)
            except RuntimeError:
                pass
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGCONT)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        selector.close()
        for descriptor in reversed(private):
            os.close(descriptor)
        if process is not None:
            process.stdout.close()
            process.stderr.close()
        if control is not None and _collected(process):
            with operations._condition:
                operations._native_controls.pop(control, None)
                operations._condition.notify_all()


class _DispatcherCancellation:
    def __init__(self, parent, closing, stopped=None):
        _require(callable(getattr(parent, 'is_set', None)), 'android_native_bounds')
        self.parent, self.closing, self.stopped = parent, closing, stopped

    def is_set(self):
        return self.parent.is_set() or self.closing.is_set() or self.stopped is not None and self.stopped.is_set()


class _NativeInstrumentation:
    def __init__(self, dispatcher, client, arguments, cancellation, deadline):
        self.dispatcher = dispatcher
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.result = None
        self.error = None
        def execute():
            try:
                self.result = dispatcher.run(client, arguments,
                    cancellation=_DispatcherCancellation(cancellation, dispatcher.closed, self.stopped),
                    deadline_monotonic=deadline, slot='instrumentation', _started=self.ready)
            except Exception:
                self.error = AndroidOperationError('android_native_instrumentation_failed')
            finally:
                self.ready.set()
                with dispatcher.operations._condition:
                    dispatcher.operations._condition.notify_all()
        self.thread = threading.Thread(target=execute, name='android-native-instrumentation')

    @property
    def returncode(self):
        if self.thread.is_alive():
            return None
        return self.result.returncode if self.result is not None and self.result.returncode is not None else 127

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired('android-native-instrumentation', timeout)
        return self.returncode


class _AndroidNativeDispatcher:
    """An explicitly issued callback bridge; requests cannot select this owner."""
    def __init__(self, operations, descriptors, guardian):
        self.operations, self.descriptors, self.guardian = operations, descriptors, guardian
        self.closed = threading.Event()
        self.gates = {name: threading.Lock() for name in ('command', 'instrumentation')}
        self.jobs = set()
        self.lock = threading.RLock()

    @contextmanager
    def _authorized(self):
        key = (id(self.descriptors), threading.get_ident())
        with self.operations._mutex:
            _require(not self.closed.is_set()
                and self.operations._native_dispatchers.get(id(self)) is self,
                'android_native_dispatcher_closed')
            self.operations._native_dispatch_threads[key] = self.operations._native_dispatch_threads.get(key, 0) + 1
        try:
            self.operations.require_native_descriptors(self.descriptors)
            yield
        finally:
            with self.operations._mutex:
                remaining = self.operations._native_dispatch_threads[key] - 1
                if remaining:
                    self.operations._native_dispatch_threads[key] = remaining
                else:
                    self.operations._native_dispatch_threads.pop(key)
                self.operations._condition.notify_all()

    @property
    def active_processes(self):
        with self.operations._mutex:
            controls = sum(call._descriptors is self.descriptors
                for call in self.operations._native_controls.values())
            dispatches = sum(key[0] == id(self.descriptors)
                for key in self.operations._native_dispatch_threads)
        with self.lock:
            return controls + dispatches + sum(job.thread.is_alive() for job in self.jobs)

    def run(self, client, arguments, *, cancellation, deadline_monotonic,
            input_bytes=b'', slot='command', _started=None):
        cancellation = _DispatcherCancellation(cancellation, self.closed)
        with self._authorized():
            _bounds(cancellation, deadline_monotonic)
            _require(slot in self.gates, 'android_native_input')
            gate = self.gates[slot]
            while not gate.acquire(timeout=min(.05, max(0, deadline_monotonic-time.monotonic()))):
                _bounds(cancellation, deadline_monotonic)
            try:
                return run_native_adb(self.operations, self.descriptors, self.guardian, client, arguments,
                    cancellation=cancellation, deadline_monotonic=deadline_monotonic,
                    input_bytes=input_bytes, slot=slot, _started=_started)
            finally:
                gate.release()

    def start(self, client, arguments, *, cancellation, deadline_monotonic):
        with self._authorized():
            _bounds(cancellation, deadline_monotonic)
            job = _NativeInstrumentation(self, client, arguments, cancellation, deadline_monotonic)
            with self.lock:
                _require(not self.closed.is_set(), 'android_native_dispatcher_closed')
                self.jobs.add(job)
                job.thread.start()
            if not job.ready.wait(min(5, max(0, deadline_monotonic-time.monotonic()))):
                job.stopped.set()
                raise AndroidOperationError('android_native_instrumentation_failed')
            if job.error is not None:
                raise job.error
            return job

    def inspect_apk(self,apk,*,cancellation,deadline_monotonic):
        cancellation=_DispatcherCancellation(cancellation,self.closed)
        with self._authorized():
            _bounds(cancellation,deadline_monotonic)
            gate=self.gates['command']
            while not gate.acquire(timeout=min(.05,max(0,deadline_monotonic-time.monotonic()))):
                _bounds(cancellation,deadline_monotonic)
            try:
                return run_native_inspector(self.operations,self.descriptors,self.guardian,apk,
                    cancellation=cancellation,deadline_monotonic=deadline_monotonic)
            finally:
                gate.release()

    def call_helper(self, client, path, body, *, cancellation, deadline_monotonic, **kwargs):
        cancellation = _DispatcherCancellation(cancellation, self.closed)
        with self._authorized():
            _bounds(cancellation, deadline_monotonic)
            _require_client(self.operations, self.descriptors, client)
            return client.call_helper(path, body, cancellation=cancellation,
                deadline_monotonic=deadline_monotonic, **kwargs)

    def _job(self, job):
        with self.lock:
            _require(type(job) is _NativeInstrumentation and job.dispatcher is self and job in self.jobs,
                     'android_native_instrumentation_owner')

    def collect(self, job):
        self._job(job)
        try:
            job.wait(timeout=8)
        except subprocess.TimeoutExpired:
            job.stopped.set()
            try:
                job.wait(timeout=3)
            except subprocess.TimeoutExpired:
                return False
        with self.lock:
            self.jobs.discard(job)
        result = job.result
        return (result is not None and result.terminated and result.bounded
                and not result.interrupted and result.returncode == 0)

    def stop(self, job, deadline):
        self._job(job)
        job.stopped.set()
        try:
            job.wait(timeout=max(0, deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            return False
        with self.lock:
            self.jobs.discard(job)
        return self.active_processes == 0

    def close(self, deadline):
        self.closed.set()
        with self.lock:
            for job in self.jobs:
                job.stopped.set()
        with self.operations._condition:
            while self.active_processes:
                for control, call in tuple(self.operations._native_controls.items()):
                    if call._descriptors is self.descriptors:
                        control.close_liveness()
                        if _collected(control.process):
                            self.operations._native_controls.pop(control, None)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.operations._condition.wait(min(remaining, .05))
        return True


@contextmanager
def native_dispatcher(operations, descriptors, guardian):
    operations.require_native_descriptors(descriptors)
    _require(type(guardian) is AndroidGuardianTools, 'android_native_guardian_changed')
    guardian.verify()
    dispatcher = _AndroidNativeDispatcher(operations, descriptors, guardian)
    with operations._mutex:
        _require(not any(item.descriptors is descriptors for item in operations._native_dispatchers.values()),
                 'android_native_dispatcher_owner')
        operations._native_dispatchers[id(dispatcher)] = dispatcher
    try:
        yield dispatcher
    finally:
        clean = dispatcher.close(time.monotonic()+3)
        with operations._mutex:
            operations._native_dispatchers.pop(id(dispatcher), None)
        _require(clean, 'android_native_owner_uncollected')
