"""Journal-bound native dispatch for fixed iOS signing/verification adapters."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import selectors
import signal
import stat
import subprocess
import threading
import time

from . import contracts
from .repair_android_signing import _ProcessResult
from .repair_signing_recovery import _OwnedProcess, _open_owned_regular, _read_at, _replace_at, SigningRecoveryError


def _require(value):
    if not value: raise SigningRecoveryError('ios_native_process_unavailable')


def _sha(value):
    return hashlib.sha256(value).hexdigest()


class _IOSOwnedProcessFacade:
    """A concrete private adapter, never selected by a request or candidate."""
    def __init__(self, session):
        _require(type(session) is _IOSNativeSession)
        self.session = session
        self.closed = False

    def validate_root(self, path):
        _require(not self.closed and Path(path).is_relative_to(self.session.root/'checks'))
        self.session.operations._require_operation(self.session.operation)

    @property
    def active_processes(self):
        return self.session.active_for(self)

    def run(self, arguments, **kwargs):
        _require(not self.closed)
        return self.session.verify_process(self, arguments, **kwargs)

    def close(self, *, deadline_monotonic):
        self.closed = True
        return self.session.close_client(self, deadline_monotonic)


class _IOSNativeSession:
    def __init__(self, operations, operation, descriptor, handles, context, provisioning_digest, role):
        from .ios_signing_operation import IOSSigningOperationStore
        _require(type(operations) is IOSSigningOperationStore and not operations.recovery_only and operations.tools.guardian is not None
                 and role in {'sign', 'inspect'})
        operations._require_operation(operation)
        self.operations, self.operation = operations, operation
        self.descriptor, self.handles, self.context = descriptor, handles, context
        self.root = operations.operation_root(operation.operation_id)
        self._changed = threading.Condition(threading.RLock())
        self._controls = {}
        self._busy = False
        self.intent, state = operations._records(descriptor, operation.operation_id, operation.request_digest)
        execution = state['execution']
        if execution is None:
            _require(role == 'sign')
            execution = {'role': role, 'contextDigest': context.digest, 'policyDigest': context.signing_policy_digest,
                'provisioningDigest': provisioning_digest, 'sequence': 0, 'historyDigest': '0'*64,
                'active': None, 'signedDigest': None, 'signedBytes': 0, 'complete': False}
        else:
            _require(role == 'inspect' and execution['role'] == 'sign' and execution['complete']
                and execution['signedDigest'] == context.signed_artifact_digest
                and execution['policyDigest'] == context.signing_policy_digest
                and execution['provisioningDigest'] == provisioning_digest and execution['active'] is None)
            execution.update(role=role, contextDigest=context.digest, complete=False)
            _require(state['phase'] == 'cleaned')
            state.update(phase='intent', inputDigest=None, inputBytes=0, appIdentity=None, appDigest=None, recovery=None)
        state['execution'] = execution
        _replace_at(descriptor, 'state.json', state)

    def facade(self):
        return _IOSOwnedProcessFacade(self)

    def active_for(self, client):
        with self._changed:
            self._purge_stopped()
            return sum(selected is client for selected in self._controls.values())

    def _purge_stopped(self):
        for control in tuple(self._controls):
            process = control.process
            if process is not None and process.returncode is not None and self._group_empty(process):
                self._controls.pop(control, None)
                with self.operations._changed:
                    self.operations._native_controls.discard(control); self.operations._changed.notify_all()

    def close_client(self, client, deadline):
        with self._changed:
            for control, selected in self._controls.items():
                if selected is client: control.close_liveness()
            while self.active_for(client) and time.monotonic() < deadline:
                self._changed.wait(min(.05, max(0, deadline-time.monotonic())))
            return not self.active_for(client)

    def _write(self, name, body):
        from .ios_signing_operation import _write_bytes
        descriptor = _open_owned_regular(self.descriptor, name,
            expected=self.intent['fileIdentities'][name], writable=True)
        try: _write_bytes(descriptor, body)
        finally: os.close(descriptor)

    def _read(self, name, maximum):
        descriptor = _open_owned_regular(self.descriptor, name, expected=self.intent['fileIdentities'][name])
        try:
            info = os.fstat(descriptor)
            _require(info.st_size <= maximum)
            data = bytearray()
            while len(data) < info.st_size:
                body = os.read(descriptor, min(65536, info.st_size-len(data)))
                _require(body); data.extend(body)
            after = os.fstat(descriptor)
            _require((info.st_size, info.st_mtime_ns, info.st_ctime_ns) ==
                     (after.st_size, after.st_mtime_ns, after.st_ctime_ns))
            return bytes(data)
        finally: os.close(descriptor)

    def _prepare(self, kind, command_digest):
        self.operations._require_operation(self.operation); self.operations.tools.verify()
        _, state = self.operations._records(self.descriptor, self.operation.operation_id, self.operation.request_digest)
        execution = state['execution']
        _require(execution is not None and execution['contextDigest'] == self.context.digest
                 and execution['active'] is None and execution['sequence'] < 40000)
        execution['sequence'] += 1
        native_context = contracts.digest({'context': self.context.digest, 'sequence': execution['sequence'],
                                           'commandDigest': command_digest, 'kind': kind})
        execution['active'] = {'kind': kind, 'contextDigest': native_context, 'commandDigest': command_digest}
        _replace_at(self.descriptor, 'state.json', state)
        for name in ('request.plist', 'arguments.plist', 'native-result.plist', 'start.json', 'termination.json'):
            self._write(name, b'')
        return {'schemaVersion': '1', 'operationId': self.operation.operation_id,
            'requestDigest': self.operation.request_digest, 'contextDigest': native_context,
            'scopeDigest': self.operations.scope_digest, 'definitionDigest': self.operations.definition_digest,
            'workPath': str(self.root)}

    def _stopped(self, result, native_context):
        _, state = self.operations._records(self.descriptor, self.operation.operation_id, self.operation.request_digest)
        execution = state['execution']
        _require(execution['active']['contextDigest'] == native_context and result.terminated)
        execution['historyDigest'] = contracts.digest({'previous': execution['historyDigest'],
            'active': execution['active'], 'returnCode': result.returncode,
            'stdoutDigest': _sha(result.stdout), 'stderrDigest': _sha(result.stderr),
            'bounded': result.bounded, 'interrupted': result.interrupted})
        execution['active'] = None
        _replace_at(self.descriptor, 'state.json', state)
        self._write('arguments.plist', b''); self._write('native-result.plist', b'')

    @staticmethod
    def _group_empty(process):
        try: os.killpg(process.pid, 0); return False
        except ProcessLookupError: return True
        except OSError: return False

    def _dispatch(self, client, binary, config, data_fds, cancellation, deadline, watched_files, outer_profile=None):
        _require(not cancellation.is_set() and time.monotonic() < deadline)
        encoded = plistlib.dumps(config, fmt=plistlib.FMT_BINARY)
        _require(len(encoded) <= 2*1024*1024)
        self._write('request.plist', encoded)
        private = []; process = None; control = None; selector = selectors.DefaultSelector()
        raw = [bytearray(), bytearray()]; bounded = True; interrupted = False; abort_at = None; acknowledged = False
        def opened(name, writable=False):
            fd = _open_owned_regular(self.descriptor, name,
                expected=self.intent['fileIdentities'][name], writable=writable)
            private.append(fd); return fd
        try:
            reader, writer = os.pipe(); private.append(reader)
            control = _OwnedProcess(None, writer)
            arguments = [opened('request.plist'), self.descriptor, *self.handles, reader,
                         *data_fds, opened('start.json', True), opened('termination.json', True)]
            inherited = tuple(set(fd for fd in arguments if fd >= 3) | set(getattr(self, '_extra_fds', ())))
            with self.operations._changed:
                self.operations._require_operation(self.operation)
                self.operations._native_controls.add(control)
            with self._changed: self._controls[control] = client
            command = [str(binary), *map(str, arguments)]
            if outer_profile is not None: command = ['/usr/bin/sandbox-exec', '-p', outer_profile, *command]
            process = subprocess.Popen(command, cwd=self.root,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=inherited, close_fds=True, start_new_session=True,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'TMPDIR': str(self.root)})
            control.process = process
            for fd in private: os.close(fd)
            private.clear()
            for index, stream in enumerate((process.stdout, process.stderr)):
                os.set_blocking(stream.fileno(), False); selector.register(stream, selectors.EVENT_READ, index)
            while process.poll() is None or selector.get_map():
                stopped = (cancellation.is_set() or self.operations._closed or not self.operation._active
                           or time.monotonic() >= deadline)
                for path, maximum in watched_files:
                    try: info = Path(path).lstat()
                    except FileNotFoundError: continue
                    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum: bounded = False
                if stopped or not bounded:
                    interrupted = stopped
                    control.close_liveness()
                    if abort_at is None: abort_at = time.monotonic()
                if abort_at is not None and time.monotonic()-abort_at > .5 and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                if abort_at is not None and time.monotonic()-abort_at > 3.5 and process.poll() is None:
                    break
                for key, _ in selector.select(.01):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj); continue
                    room = max(0, 65536-len(raw[key.data])); raw[key.data].extend(chunk[:room])
                    if len(chunk) > room: bounded = False
                if not acknowledged and b'\n' in raw[0]:
                    try:
                        response = json.loads(raw[0])
                        _require(set(response) == {'schemaVersion', 'contextDigest', 'status', 'signedCodeObjects'}
                            and type(response['schemaVersion']) is int and response['schemaVersion'] == 1
                            and response['contextDigest'] == config['contextDigest']
                            and response['status'] in {'succeeded', 'failed'}
                            and type(response['signedCodeObjects']) is int and 0 <= response['signedCodeObjects'] <= 512)
                    except Exception:
                        bounded = False; control.close_liveness()
                    else:
                        acknowledged = control.acknowledge()
                if process.poll() is not None and not selector.get_map(): break
            terminated = process.poll() is not None and self._group_empty(process)
            result = _ProcessResult(process.returncode, bytes(raw[0]), bytes(raw[1]), terminated, bounded, interrupted)
            if terminated and not interrupted and acknowledged:
                for name, expected_state in (('start.json', 'started'),
                    ('termination.json', 'succeeded' if process.returncode == 0 else 'failed')):
                    record = json.loads(self._read(name, 4096))
                    _require(record == {'schemaVersion': 1, 'operationId': self.operation.operation_id,
                        'requestDigest': self.operation.request_digest, 'contextDigest': config['contextDigest'],
                        'scopeDigest': self.operations.scope_digest, 'definitionDigest': self.operations.definition_digest,
                        'ownerPid': process.pid, 'state': expected_state,
                        'recordMeaning': 'owner-start' if name == 'start.json' else 'exit-intent'})
            return result
        finally:
            if control is not None: control.close_liveness()
            if process is not None and process.poll() is None:
                try: process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=3)
            selector.close()
            for fd in private: os.close(fd)
            if process is not None:
                process.stdout.close(); process.stderr.close()
            if control is not None and (process is None or self._group_empty(process)):
                with self._changed: self._controls.pop(control, None); self._changed.notify_all()
                with self.operations._changed:
                    self.operations._native_controls.discard(control); self.operations._changed.notify_all()

    def verify_process(self, client, arguments, *, work, input_bytes, pass_fds, cancellation,
                       deadline_monotonic, watched_files=(), max_output_bytes=65536):
        _require(type(arguments) is tuple and arguments[:2] == ('/usr/bin/sandbox-exec', '-p')
            and input_bytes == b'' and 0 < max_output_bytes <= 1024*1024
            and Path(work).is_relative_to(self.root/'checks'))
        with self._changed:
            _require(not self._busy); self._busy = True
        descriptors = []
        try:
            command = plistlib.dumps(list(arguments), fmt=plistlib.FMT_BINARY)
            _require(len(command) <= 128*1024)
            config = self._prepare('verify', _sha(command))
            config.update(commandDigest=_sha(command), childWorkPath=str(Path(work).resolve()), maxOutputBytes=str(max_output_bytes))
            self._write('arguments.plist', command)
            for name in ('arguments.plist', 'native-result.plist'):
                descriptors.append(_open_owned_regular(self.descriptor, name,
                    expected=self.intent['fileIdentities'][name], writable=name == 'native-result.plist'))
            self._extra_fds = pass_fds
            control_result = self._dispatch(client, self.operations.tools.guardian, config, descriptors,
                cancellation, deadline_monotonic, watched_files)
            if control_result.terminated and not control_result.interrupted and control_result.bounded:
                value = plistlib.loads(self._read('native-result.plist', 2*1024*1024+4096))
                _require(type(value) is dict and set(value) == {'returnCode', 'stdout', 'stderr', 'bounded'}
                    and type(value['returnCode']) is int and type(value['stdout']) is bytes and type(value['stderr']) is bytes
                    and type(value['bounded']) is bool
                    and max(len(value['stdout']), len(value['stderr'])) <= max_output_bytes)
                result = _ProcessResult(value['returnCode'], value['stdout'], value['stderr'], True,
                                       value['bounded'], False)
            else: result = control_result
            if result.terminated: self._stopped(result, config['contextDigest'])
            return result
        finally:
            self._extra_fds = ()
            for fd in descriptors: os.close(fd)
            with self._changed: self._busy = False; self._changed.notify_all()

    def sign_process(self, material, *, cancellation, deadline_monotonic, app_digest):
        payload = {'mode': 'sign', 'appRelativePath': 'App.app',
            'certificateSha256': self.operations.definition.identity.certificate_sha256,
            'teamId': self.operations.definition.identity.team_id,
            'certificateChain': list(self.operations.definition.identity.certificate_chain),
            'codeObjects': self.operations.definition.code_objects}
        digest = contracts.digest({'payloadDigest': _sha(plistlib.dumps(payload, fmt=plistlib.FMT_BINARY)),
                                   'appDigest': app_digest})
        config = self._prepare('sign', digest); config.update(payload)
        directories = ('/usr/lib', '/System/Library', '/dev/fd', '/System/Volumes/Preboot/Cryptexes/OS',
                       '/System/Cryptexes/OS', str(self.root))
        files = ('/', '/System', '/System/Volumes', '/System/Volumes/Preboot',
            '/System/Volumes/Preboot/Cryptexes', '/System/Cryptexes', '/dev/null',
            '/dev/random', '/dev/urandom', str(self.operations.tools.signer))
        readable = (' '.join('(subpath '+json.dumps(path)+')' for path in directories) + ' '
            + ' '.join('(literal '+json.dumps(path)+')' for path in files))
        profile = ('(version 1) (allow default) (deny network*) (deny mach-lookup) '
            '(deny process-fork process-exec) (allow process-exec (literal '+json.dumps(str(self.operations.tools.signer))+')) '
            '(deny file-read* file-write*) (allow file-read* '+readable+') (allow file-read-metadata) '
            '(deny file-read-data file-read-xattr (subpath "/System/Library/Keychains") '
            '(subpath "/System/Volumes/Preboot/Cryptexes/OS/System/Library/Keychains")) '
            '(allow file-write* (subpath '+json.dumps(str(self.root/'App.app'))+') '
            '(literal '+json.dumps(str(self.root))+') (literal '+json.dumps(str(self.root/'start.json'))+') '
            '(literal '+json.dumps(str(self.root/'termination.json'))+'))')
        reader, writer = os.pipe()
        try:
            os.write(writer, material.password); os.close(writer); writer = None
            result = self._dispatch(None, self.operations.tools.signer, config, (material.descriptor, reader),
                cancellation, deadline_monotonic, (), outer_profile=profile)
            if result.terminated: self._stopped(result, config['contextDigest'])
            return result
        finally:
            os.close(reader)
            if writer is not None: os.close(writer)
