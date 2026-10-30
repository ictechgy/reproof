"""Effectful, private UNQUALIFIED birth of a disposable offline Android fixture.

Planning is pure. Only launch_qualification_birth starts processes, creates
private auth material (ADB/emulator may generate it), or installs the two fixed
synthetic APKs. No ordinary Live CLI or recovery path calls this entry point.
Darwin enforcement and real emulator compatibility require separate approval
and runtime qualification. Retained private data is never recursively deleted.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

from reproof.core import ContractError
from reproof.execution.artifacts import open_regular
from reproof.execution.wire import safe_transfer_path
from .android_recovery_tool_ca import selected_public_ca, verify_public_ca
from reproof.adb_endpoint import (AdbEndpoint, _frame, adb_client_sandbox,
                                  _darwin_runtime_reads, _darwin_keychain_denials)
from reproof.repair_android_signing import _ProcessOwner
from .android_recovery_diagnostics import (IncrementalCollector, OutputCancellation,
    output_alert, inventory_state, birth_summary)
from .android_recovery_authority import _directory
from .android_recovery_private_evidence import _EvidenceSink
from .android_recovery_tool_pin import ADB_DARWIN_SHA256, require_pinned_adb

_MAX_APK = 32 * 1024 * 1024
# Official android-36/default/arm64-v8a revision 2 archive layout. These
# declared sizes constrain input selection; per-file SHA-256 verification still
# checks actual selected bytes. This is not publisher provenance attestation.
_AOSP_R2_EXPANDED_BYTES = 2_039_419_809
_AOSP_R2_FILE_SIZES = {
    'NOTICE.txt': 5324268,
    'VerifiedBootParams.textproto': 356,
    'advancedFeatures.ini': 733,
    'build.prop': 5286,
    'data/empty_data_disk': 107,
    'data/local.prop': 46,
    'data/misc/GoldfishSkinConfig': 783,
    'data/misc/emulator/config/radioconfig.xml': 1433,
    'data/misc/emulator/version.txt': 112,
    'data/misc/modem_simulator/etc/modem_simulator/files/numeric_operator.xml': 297,
    'data/misc/modem_simulator/iccprofile_for_carrierapitests.xml': 16537,
    'data/misc/modem_simulator/iccprofile_for_sim0.xml': 14647,
    'data/misc/modem_simulator/iccprofile_for_sim_tel_alaska.xml': 14474,
    'encryptionkey.img': 18874368,
    'kernel-ranchu': 14489771,
    'kernel_cmdline.txt': 16,
    'ramdisk.img': 2090397,
    'source.properties': 322,
    'system.img': 1898971136,
    'vendor.img': 99614720,
}
_OWNERS = {}
_MINT = object()


def _require(value, message='Private Android birth unavailable'):
    if not value:
        raise ContractError(message)


def _active(cancellation=None, deadline=None):
    _require(cancellation is None or not cancellation.is_set(), 'Private birth cancelled')
    _require(deadline is None or time.monotonic() < deadline, 'Private birth deadline expired')


def _absolute(value):
    _require(type(value) is str and value.startswith('/') and len(value) <= 1024
             and not any(c in value for c in ('\0', '\n', '\r', '"', '\\'))
             and '..' not in Path(value).parts)
    return Path(value)


@dataclass(frozen=True)
class PinnedFile:
    path: str
    sha256: str
    size: int

    def __post_init__(self):
        _absolute(self.path)
        _require(type(self.sha256) is str and re.fullmatch('[0-9a-f]{64}', self.sha256)
                 and type(self.size) is int and 0 < self.size <= 8 * 1024**3)

    def verify(self, *, cancellation=None, deadline=None):
        _active(cancellation, deadline)
        path = _absolute(self.path)
        digest = hashlib.sha256()
        with os.fdopen(open_regular(path.parent, path.name), 'rb') as source:
            before = os.fstat(source.fileno())
            _require(before.st_nlink == 1 and before.st_size == self.size)
            count = 0
            while chunk := source.read(1024 * 1024):
                _active(cancellation, deadline)
                count += len(chunk)
                _require(count <= self.size)
                digest.update(chunk)
            after = os.fstat(source.fileno())
            _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) ==
                     (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink))
        rebound = open_regular(path.parent, path.name)
        try:
            current = os.fstat(rebound)
            _require((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns, current.st_nlink) ==
                     (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink))
        finally:
            os.close(rebound)
        _active(cancellation, deadline)
        _require(count == self.size and digest.hexdigest() == self.sha256)
        return path


@dataclass(frozen=True)
class PinnedTree:
    root: str
    files: tuple[PinnedFile, ...]
    executables: tuple[str, ...] = ()

    def __post_init__(self):
        root = _absolute(self.root)
        _require(type(self.files) is tuple and 1 <= len(self.files) <= 4096
                 and all(type(item) is PinnedFile and root in Path(item.path).parents for item in self.files)
                 and len({item.path for item in self.files}) == len(self.files)
                 and sum(item.size for item in self.files) <= 16 * 1024**3)
        _require(type(self.executables) is tuple and len(self.executables) <= 32
                 and set(self.executables) <= {item.path for item in self.files})

    def _validate_paths(self):
        root = _absolute(self.root)
        safe_transfer_path('/'.join(root.parts[1:]))
        public_ca = selected_public_ca(self.root, self.files, self.executables)
        for item in self.files:
            relative = str(Path(item.path).relative_to(root))
            if item.path != public_ca:
                safe_transfer_path(relative)
        return public_ca

    def verify(self, *, cancellation=None, deadline=None):
        # Validate every selected path before opening any selected contents.
        public_ca = self._validate_paths()
        root = _absolute(self.root)
        fd = _directory(root)
        stack = [('', 0)]
        try:
            expected = {item.path for item in self.files}
            found = set()
            entries = directories = 0
            while stack:
                prefix, depth = stack.pop()
                current = _directory(root/prefix)
                try:
                    with os.scandir(current) as scan:
                        for entry in scan:
                            _active(cancellation, deadline)
                            entries += 1
                            _require(entries <= 8192 and depth <= 32)
                            info = entry.stat(follow_symlinks=False)
                            relative = prefix + entry.name
                            if stat.S_ISDIR(info.st_mode):
                                directories += 1
                                _require(directories <= 4096 and len(stack) < 4096)
                                stack.append((relative+'/', depth+1))
                            else:
                                _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
                                found.add(str(root/relative))
                                _require(len(found) <= 4096)
                finally:
                    os.close(current)
            _require(found == expected)
            for item in self.files:
                if item.path == public_ca:
                    verify_public_ca(self.root, item, self.files, self.executables,
                                     cancellation=cancellation, deadline=deadline)
                else:
                    item.verify(cancellation=cancellation, deadline=deadline)
            for name in self.executables:
                _require(os.access(name, os.X_OK))
            again = _directory(root)
            try:
                _require(os.fstat(fd).st_ino == os.fstat(again).st_ino
                         and os.fstat(fd).st_dev == os.fstat(again).st_dev)
            finally:
                os.close(again)
        finally:
            os.close(fd)


@dataclass(frozen=True)
class BirthPlan:
    output: str
    adb: PinnedFile
    aapt: PinnedFile
    sandbox: PinnedFile
    emulator: PinnedTree
    image: PinnedTree
    sample_apk: PinnedFile
    helper_apk: PinnedFile
    console_port: int = 5554
    lifetime_seconds: int = 900

    def __post_init__(self):
        output = _absolute(self.output)
        _require(',' not in self.output and len(os.fsencode(output/'gateways'/('g-'+'0'*16)/'client.sock')) < 104,
                 'Private birth output cannot encode the callback or gateway address')
        _require(type(self.adb) is PinnedFile and self.adb.sha256 == ADB_DARWIN_SHA256
                 and type(self.aapt) is PinnedFile and type(self.sandbox) is PinnedFile
                 and self.sandbox.path == '/usr/bin/sandbox-exec')
        _require(type(self.emulator) is PinnedTree and type(self.image) is PinnedTree
                 and str(Path(self.emulator.root) / 'emulator') in self.emulator.executables)
        _require(not self.image.executables)
        image_sizes = {str(Path(item.path).relative_to(self.image.root)): item.size
                       for item in self.image.files}
        # r2 contains data/empty_data_disk, not a prebuilt userdata.img. Only
        # this closed SDK image layout may seed a newly created private AVD.
        _require(image_sizes == _AOSP_R2_FILE_SIZES
                 and sum(image_sizes.values()) == _AOSP_R2_EXPANDED_BYTES,
                 'Only the fixed AOSP API36 arm64 revision 2 image layout is supported')
        _require(type(self.sample_apk) is PinnedFile and type(self.helper_apk) is PinnedFile
                 and max(self.sample_apk.size, self.helper_apk.size) <= _MAX_APK)
        _require(type(self.console_port) is int and 5554 <= self.console_port <= 5584
                 and self.console_port % 2 == 0 and type(self.lifetime_seconds) is int
                 and 120 <= self.lifetime_seconds <= 1800)
        selected = [self.adb.path, self.aapt.path, self.emulator.root, self.image.root,
                    self.sample_apk.path, self.helper_apk.path]
        _require(all(output != Path(name) and output not in Path(name).parents
                     and Path(name) not in output.parents for name in selected))

    @property
    def effects(self):
        return ('fresh-private-directories', 'private-adb-auth-keys-may-be-generated',
                'emulator-console-token-may-be-generated', 'owned-server-and-avd',
                'initial-install-io.reproof.sample', 'initial-install-io.reproof.live',
                'offline-only', 'private-data-retained')


def _environment(home, android, avd, work, notification_port):
    return {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'HOME': str(home),
            'ANDROID_USER_HOME': str(android), 'ANDROID_AVD_HOME': str(avd), 'TMPDIR': str(work),
            'ADB_USB': '0', 'ADB_MDNS': '0', 'ADB_EMU': '0',
            'ANDROID_ADB_SERVER_PORT': str(notification_port)}


def _sandbox(plan, root, socket_path, callback, notification_port, *, emulator):
    # Deliberately no ambient HOME, proxies, ADBHOST, discovery, or adb executable
    # permission in the emulator profile. This is not runtime enforcement proof.
    roots = [str(root)]
    roots += [plan.emulator.root, plan.image.root] if emulator else []
    executables = plan.emulator.executables if emulator else (plan.adb.path,)
    result = ('(version 1)(allow default)(deny network*)(deny process-exec)'
              '(deny file-read* file-write*)(allow file-read-metadata)'
              +_darwin_runtime_reads(tuple(roots), tuple(executables))+
              '(allow file-write* ' + ' '.join('(subpath '+json.dumps(str(root/name))+')'
                  for name in (('home','android','avd','client') if emulator else ('home','android','server-work'))) + ')'
              '(allow process-exec ' + ' '.join('(literal '+json.dumps(x)+')' for x in executables) + ')'
              +_darwin_keychain_denials())
    if emulator:
        result += ('(allow network-outbound (literal ' + json.dumps(str(callback)) + ')'
                   ' (remote ip "localhost:' + str(notification_port) + '"))')
        for port in (plan.console_port, plan.console_port + 1):
            result += '(allow network-bind network-inbound (local ip "localhost:' + str(port) + '"))'
    else:
        result += ('(allow file-write* (literal ' + json.dumps(str(socket_path)) + '))'
                   '(allow network-bind network-inbound (literal ' + json.dumps(str(socket_path)) + '))'
                   '(allow network-outbound (remote ip "localhost:' + str(plan.console_port + 1) + '"))')
    result += '(deny file-write* (subpath '+json.dumps(str(root/'gateways'))+'))'
    result += '(deny file-write* (literal '+json.dumps(str(callback))+'))'
    return result


class BirthIncomplete(ContractError):
    def __init__(self, owner):
        super().__init__('Private birth incomplete; owned resources retained for collection')
        self.owner = owner
        self.report = birth_summary(owner)


class _Child:
    def __init__(self):
        self.process = None
        self.collectors = []
        self._evidence = []

    def start(self, argv, environment, work, *, stdin=subprocess.DEVNULL):
        _require(self.process is None)
        self.process = subprocess.Popen(argv, cwd=work, env=environment, stdin=stdin,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
        for index, stream in enumerate((self.process.stdout, self.process.stderr)):
            evidence = self._evidence[index] if self._evidence else None
            collector = IncrementalCollector(stream, 65536, evidence=evidence)
            self.collectors.append(collector)
            collector.thread.start()

    def bounded(self):
        return all(not item.oversized for item in self.collectors)

    def collect(self, deadline):
        if self.process is None:
            return True
        clean = _ProcessOwner._terminate(self.process, deadline_monotonic=deadline)
        for collector in self.collectors:
            collector.thread.join(max(0, deadline-time.monotonic()))
            clean = clean and not collector.thread.is_alive()
        return clean and self.bounded() and self.process.poll() is not None


class OwnedBirth:
    def __new__(cls, token=None):
        _require(token is _MINT, 'A fresh qualification launcher is required')
        return super().__new__(cls)

    def require_live(self):
        _require(not output_alert(self._children), 'Private native output warning or read failure')
        _require(_OWNERS.get(id(self)) is self and self._pid == os.getpid() and not self._closed
                 and not self.cancellation.is_set() and time.monotonic() < self.deadline)
        _require(self.server is not None and self.server.poll() is None
                 and self.emulator is not None and self.emulator.poll() is None
                 and all(child.bounded() for child in self._children))
        current = _directory(self.root)
        try:
            _require((os.fstat(current).st_dev, os.fstat(current).st_ino) == self._root_identity)
        finally:
            os.close(current)

    @property
    def context(self):
        self.require_live()
        _require(self._ready and self._context is not None)
        return self._context

    def collect(self):
        _require(_OWNERS.get(id(self)) is self and self._pid == os.getpid())
        if self._context is not None:
            _require(not self._context._handles, 'Collect native helper ownership before birth')
        self._closed = True
        deadline = time.monotonic() + 15
        clean = self._operations.close(deadline_monotonic=deadline)
        if self._context is not None:
            client = getattr(self._context, '_client', None)
            if client is not None:
                clean = client.close(deadline_monotonic=deadline) and clean
            self._context._closed = True
        for child in reversed(self._children):
            clean = child.collect(deadline) and clean
        for listener in self._listeners:
            listener.close()
        clean = _finish_private_evidence(self) and clean
        if clean:
            clean = self._close_retained_fds()
        if clean and self._context is not None:
            from .android_recovery_context import _OWNED_BIRTHS
            _OWNED_BIRTHS.pop(id(self._context), None)
        return {'processesCollected': clean, 'privateDataRetained': True,
                'runtimeQualified': False, 'normalRecoveryEnabled': False}


    def _close_retained_fds(self):
        # Shared by normal collection and emergency abort. A close that raised
        # may already have consumed its FD; never retry the numeric value.
        first_error = None
        for fd in tuple(self._fds):
            self._fds.remove(fd)
            try:
                os.close(fd)
            except OSError as error:
                self._abort_fd_uncertain = True
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error
        return not getattr(self, '_abort_fd_uncertain', False)


    def abort(self):
        """Collect only retained local processes; never certify device recovery.

        This emergency owner teardown intentionally works after cancellation,
        expiry, or a failed context. No ADB command, native receipt, authority
        release, journal rewrite, or deletion of private AVD data occurs here.
        A reaped process is never signalled through its former numeric PID.
        """
        _require(_OWNERS.get(id(self)) is self and self._pid == os.getpid())
        from .android_recovery_context import IsolatedAdbContext, _OwnedInstrumentation, _OWNED_BIRTHS
        context = self._context
        if context is not None:
            _require(type(context) is IsolatedAdbContext and context._birth is self
                     and _OWNED_BIRTHS.get(id(context)) is self and context._pid == self._pid,
                     'Retained private context owner required')
            for key, handle in context._handles.items():
                _require(type(handle) is _OwnedInstrumentation and key == id(handle)
                         and handle._context is context
                         and (handle._process is None or type(handle._process) is subprocess.Popen),
                         'Retained local instrumentation owner required')
        self._closed = True
        if context is not None:
            context._closed = True
        deadline = time.monotonic() + 15
        clean = not getattr(self, '_abort_fd_uncertain', False)
        # Do not stop collecting other owned children after one cleanup failure.
        if context is not None:
            for handle in tuple(context._handles.values()):
                collected = False
                try:
                    process = handle._process
                    collected = process is None or _ProcessOwner._terminate(process, deadline_monotonic=deadline)
                    for collector in handle._collectors:
                        if collector.thread.ident is not None:
                            collector.thread.join(max(0, deadline-time.monotonic()))
                        collected = not collector.thread.is_alive() and collected
                    if process is not None and collected:
                        # Partial collector construction can leave unowned pipes.
                        for stream in (process.stdout, process.stderr):
                            if stream is not None:
                                stream.close()
                    if collected:
                        context._handles.pop(id(handle))
                except (OSError, RuntimeError, ValueError):
                    collected = False
                clean = collected and clean
            client = getattr(context, '_client', None)
            if client is not None:
                try:
                    clean = client.close(deadline_monotonic=deadline) and clean
                except (OSError, RuntimeError, ValueError):
                    clean = False
        try:
            clean = self._operations.close(deadline_monotonic=deadline) and clean
        except (OSError, RuntimeError, ValueError):
            clean = False
        for child in reversed(self._children):
            try:
                clean = child.collect(deadline) and clean
            except (OSError, RuntimeError, ValueError):
                clean = False
        for listener in self._listeners:
            try:
                listener.close()
            except OSError:
                clean = False
        clean = _finish_private_evidence(self) and clean
        if clean:
            try:
                clean = self._close_retained_fds()
            except OSError:
                clean = False
        return {'abortOnly': True, 'processesCollected': clean,
                'cleanupIncomplete': not clean, 'privateDataRetained': True,
                'recoverySucceeded': False, 'authorityReleased': False,
                'runtimeQualified': False, 'normalRecoveryEnabled': False}


def _read(connection, count, *, owner=None, deadline=None):
    _require(0 <= count <= 65536)
    value = bytearray()
    while len(value) < count:
        if owner is not None:
            owner.require_live()
            remaining = min(owner.deadline, deadline)-time.monotonic()
            _require(remaining > 0, 'Private startup response expired')
            connection.settimeout(min(.1, remaining))
        try:
            chunk = connection.recv(count-len(value))
        except socket.timeout:
            if owner is None:
                raise
            continue
        _require(chunk, 'Private startup response ended early')
        value.extend(chunk)
    return bytes(value)


def _server_query(owner, service, *, registration=False):
    _require(type(service) is bytes and type(registration) is bool)
    _require(service == ('host:emulator:'+str(owner.plan.console_port+1)).encode()
             if registration else service in {b'host:version', b'host:devices'})
    owner.require_live()
    owner.endpoint.verify()
    deadline = min(owner.deadline, time.monotonic()+5)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(owner.endpoint.socket_path))
        from reproof.protected_validation import _peer_uid
        _require(_peer_uid(connection) == os.getuid())
        connection.sendall(_frame(service))
        if registration:
            _require(service == ('host:emulator:'+str(owner.plan.console_port+1)).encode())
            # This ADB command intentionally has no reply. Later inventory is
            # independent success evidence; delivery alone is never sufficient.
            result = b''
        else:
            _require(service in {b'host:version', b'host:devices'})
            _require(_read(connection, 4, owner=owner, deadline=deadline) == b'OKAY')
            length = _read(connection, 4, owner=owner, deadline=deadline)
            _require(re.fullmatch(b'[0-9a-fA-F]{4}', length))
            result = _read(connection, int(length, 16), owner=owner, deadline=deadline)
    owner.require_live()
    return result


def _fixed_query(owner, command):
    context = owner._context
    owner.require_live()
    argv, gateway = context._client.prepare_fixed(command)
    try:
        result = owner._operations.run(argv, work=context.work, input_bytes=b'', pass_fds=(),
            cancellation=OutputCancellation(owner.cancellation, owner._children), deadline_monotonic=min(owner.deadline, time.monotonic()+5),
            max_output_bytes=65536, private_homes=(context.home, context.android_user_home))
        owner.require_live()
        _require(result.terminated and result.bounded and not result.interrupted and result.returncode == 0)
        return result.stdout
    finally:
        context._client.finish_gateway(gateway)


def _bootstrap_context(owner):
    from . import android_recovery_context as contexts
    context = object.__new__(contexts.IsolatedAdbContext)
    context._birth = owner
    context._pid = os.getpid()
    context._closed = context._failed = False
    context.deadline = owner.deadline
    context.cancellation = owner.cancellation
    context._server = owner.server
    context.endpoint = owner.endpoint
    context.serial = 'emulator-'+str(owner.plan.console_port)
    context.target_package = 'io.reproof.sample'
    context.adb = Path(owner.plan.adb.path)
    context.adb_digest = owner.plan.adb.sha256
    context.aapt = Path(owner.plan.aapt.path)
    context.aapt_digest = owner.plan.aapt.sha256
    context.sandbox_digest = owner.plan.sandbox.sha256
    context._binding = (context.serial, context.target_package,
                        owner.plan.helper_apk.sha256, owner.plan.sample_apk.sha256)
    context._artifacts = {Path(item.path):item.sha256 for item in
                          (owner.plan.sample_apk, owner.plan.helper_apk)}
    context._handles = {}
    for attribute, name, fd_name in (('work', 'client', '_work_fd'), ('home', 'home', '_home_fd'),
                                   ('android_user_home', 'android', '_android_fd'),
                                   ('gateway_root', 'gateways', '_gateway_fd')):
        path = owner.root / name
        setattr(context, attribute, path)
        fd = _directory(path)
        owner._fds.append(fd)
        setattr(context, fd_name, fd)
    owner._context = context
    contexts._OWNED_BIRTHS[id(context)] = owner
    context._client = contexts._PrivateClient(context)
    context._require_live()
    return context


def _provision(owner, package, artifact):
    _require(package in {'io.reproof.sample', 'io.reproof.live'})
    _require(artifact is (owner.plan.sample_apk if package == 'io.reproof.sample' else owner.plan.helper_apk))
    _assert_avd(owner)
    context = owner._context
    identity = context.apk_identity(Path(artifact.path), guard=owner.require_live)
    _require(identity.get('package') == package)
    # No -r: this is an initial install on a fresh userdata image, never repair.
    command = 'cmd package install -t -S '+str(artifact.size)
    argv, gateway = context._client.prepare_fixed(command)
    try:
        child = _new_private_child(owner, 'initial-install')
        # The fixed birth-only install may read only this selected stdin APK.
        _require(argv[:2] == ('/usr/bin/sandbox-exec', '-p'))
        argv = argv[:2] + (argv[2]+'(allow file-read* (literal '+json.dumps(artifact.path)+'))',) + argv[3:]
        artifact.verify(cancellation=owner.cancellation, deadline=owner.deadline)
        path = Path(artifact.path)
        with os.fdopen(open_regular(path.parent, path.name), 'rb') as source:
            digest = hashlib.sha256()
            count = 0
            while chunk := source.read(1024*1024):
                owner.require_live()
                count += len(chunk)
                _require(count <= artifact.size)
                digest.update(chunk)
            _require(count == artifact.size and digest.hexdigest() == artifact.sha256)
            source.seek(0)
            owner.require_live()
            child.start(argv, _environment(context.home, context.android_user_home,
                owner.root/'avd', context.work, owner.notification_port), context.work, stdin=source)
            until = min(owner.deadline, time.monotonic()+60)
            while child.process.poll() is None:
                owner.require_live()
                _require(time.monotonic() < until)
                time.sleep(.05)
        for collector in child.collectors:
            collector.thread.join(2)
        _require(child.process.returncode == 0 and child.bounded()
                 and all(not c.thread.is_alive() for c in child.collectors)
                 and bytes(child.collectors[0].data).strip() == b'Success'
                 and _ProcessOwner._group_empty(child.process.pid))
        artifact.verify(cancellation=owner.cancellation, deadline=owner.deadline)
        _assert_avd(owner)
    finally:
        context._client.finish_gateway(gateway)


def _assert_avd(owner):
    owner.require_live()
    expected = ('emulator-'+str(owner.plan.console_port)+'\tdevice\n').encode()
    _require(_server_query(owner, b'host:devices') == expected, 'Private inventory differs')
    _require(_fixed_query(owner, 'getprop ro.kernel.qemu').strip() == b'1')
    _require(_fixed_query(owner, 'getprop ro.boot.qemu.avd_name').strip() == owner.avd_name.encode())


def _accept_exact(owner, listener, expected, *, framed=False):
    until = min(owner.deadline, time.monotonic()+20)
    while True:
        owner.require_live()
        _require(time.monotonic() < until)
        try:
            connection, _ = listener.accept()
            break
        except socket.timeout:
            continue
    deadline = until
    with connection:
        connection.settimeout(2)
        if framed:
            size = _read(connection, 4, owner=owner, deadline=deadline)
            _require(re.fullmatch(b'[0-9a-fA-F]{4}', size) and int(size, 16) == len(expected))
        _require(_read(connection, len(expected), owner=owner, deadline=deadline) == expected)
        owner.require_live()
        remaining = min(owner.deadline, deadline)-time.monotonic()
        _require(remaining > 0)
        connection.settimeout(min(2, remaining))
        _require(connection.recv(1) == b'')
    owner.require_live()


def _root_matches(owner):
    current = _directory(owner.root)
    try:
        _require((os.fstat(current).st_dev, os.fstat(current).st_ino) == owner._root_identity)
    finally:
        os.close(current)


def _outside_inputs(parent, plan):
    identities = set()
    for tree in (plan.emulator, plan.image):
        selected = _directory(tree.root)
        try:
            info = os.fstat(selected)
            identities.add((info.st_dev, info.st_ino))
        finally:
            os.close(selected)
    current = os.dup(parent)
    try:
        for _ in range(128):
            info = os.fstat(current)
            identity = (info.st_dev, info.st_ino)
            _require(identity not in identities, 'Birth output overlaps a selected input tree')
            ancestor = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            try:
                above = os.fstat(ancestor)
                if (above.st_dev, above.st_ino) == identity:
                    os.close(ancestor)
                    return
            except BaseException:
                os.close(ancestor)
                raise
            os.close(current)
            current = ancestor
        raise ContractError('Birth ancestry bound exceeded')
    finally:
        os.close(current)


def _write_at(directory, name, text):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        raw = text.encode('utf-8')
        _require(len(raw) <= 8192)
        while raw:
            written = os.write(fd, raw)
            _require(written > 0)
            raw = raw[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _avd_config(owner):
    return ('avd.ini.encoding=UTF-8\nabi.type=arm64-v8a\nhw.cpu.arch=arm64\n'
            'disk.dataPartition.size=6144M\nhw.ramSize=2048\nhw.lcd.width=720\nhw.lcd.height=1280\n'
            'hw.lcd.density=320\nhw.gpu.enabled=yes\nhw.gpu.mode=swiftshader_indirect\n'
            'PlayStore.enabled=false\ntag.id=default\ntarget=android-36\n'
            'image.sysdir.1='+owner.plan.image.root+'\n')


def _avd_index(owner):
    return ('avd.ini.encoding=UTF-8\npath='+
            str(owner.root/'avd'/(owner.avd_name+'.avd'))+'\ntarget=android-36\n')


def _emulator_environment(owner):
    root = owner.root
    environment = _environment(root/'home', root/'android', root/'avd',
                               root/'client', owner.notification_port)
    # A fresh, empty SDK anchor, never the host SDK or an inherited setting.
    environment.update(ANDROID_HOME=str(root/'sdk'), ANDROID_SDK_ROOT=str(root/'sdk'))
    # Optional native crash reporting can spawn a detached handler outside our
    # owned process group. Disable it before crashhandler_init, not by granting
    # host temporary-directory access or suppressing diagnostics.
    environment['ANDROID_EMU_ENABLE_CRASH_REPORTING'] = '0'
    return environment


def _require_launch_layout(owner):
    """Static, no-process check of generated paths before any native launch."""
    _root_matches(owner)
    _require(set(owner._sdk_fds) == {'sdk', 'sdk/platforms', 'sdk/platform-tools'},
             'Private SDK anchor ownership incomplete')
    for relative, retained in owner._sdk_fds.items():
        opened = _directory(owner.root/relative)
        try:
            before, current = os.fstat(retained), os.fstat(opened)
            _require((before.st_dev, before.st_ino) == (current.st_dev, current.st_ino),
                     'Private SDK anchor changed')
            expected = {'platforms', 'platform-tools'} if relative == 'sdk' else set()
            with os.scandir(opened) as entries:
                found = set()
                for entry in entries:
                    _require(entry.name in expected and entry.name not in found,
                             'Private SDK anchor must contain no tools or images')
                    found.add(entry.name)
                _require(found == expected, 'Private SDK anchor incomplete')
        finally:
            os.close(opened)
    for relative, expected in (
            ('avd/'+owner.avd_name+'.avd/config.ini', _avd_config(owner)),
            ('avd/'+owner.avd_name+'.ini', _avd_index(owner))):
        with os.fdopen(open_regular(owner.root, relative), 'rb') as source:
            _require(source.read(8193) == expected.encode('utf-8'),
                     'Private AVD configuration changed')
    _root_matches(owner)


def _new_private_child(owner, role):
    _require(role in ('server', 'emulator', 'initial-install') and len(owner._children) < 4)
    child = _Child()
    child._diagnostic_role = role
    child._evidence = []
    slot = len(owner._children)
    owner._children.append(child)  # Retain partial creation before any Popen.
    for stream in ('stdout', 'stderr'):
        child._evidence.append(_EvidenceSink(owner.root, owner._evidence_root_fd,
            owner._evidence_directory_fd, str(slot)+'-'+stream+'.bin'))
    return child


def _finish_private_evidence(owner):
    clean = True
    for child in owner._children:
        for sink in getattr(child, '_evidence', ()):
            writers = [item for item in getattr(child, 'collectors', ())
                       if getattr(item, '_evidence', None) is sink]
            if any(item.thread.is_alive() for item in writers):
                clean = False  # Never close/reuse a live writer's descriptor.
            else:
                clean = sink.finish() and clean
    return clean


def _prepare_directories(owner):
    root = owner.root
    parent = _directory(root.parent)
    owner._fds.append(parent)
    _outside_inputs(parent, owner.plan)  # Inode ancestry before any mkdir/write.
    available = os.fstatvfs(parent)
    # Admission headroom, not a disk quota: emulator-generated userdata/cache
    # may grow. Runtime monitoring and owned-process stop are separate controls.
    floor = 8*1024**3 + 2*sum(item.size for item in owner.plan.image.files)
    _require(available.f_bavail*available.f_frsize >= floor, 'Insufficient private birth disk space')
    os.mkdir(root.name, 0o700, dir_fd=parent)
    fd = os.open(root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    owner._fds.append(fd)
    info = os.fstat(fd)
    owner._root_identity = (info.st_dev, info.st_ino)
    _root_matches(owner)
    for name in ('client', 'server-work', 'home', 'android', 'gateways', 'avd', 'sdk'):
        os.mkdir(name, 0o700, dir_fd=fd)
    os.mkdir('credentials', 0o700, dir_fd=fd)
    evidence = os.open('credentials', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    owner._fds.append(evidence)
    owner._evidence_root_fd, owner._evidence_directory_fd = fd, evidence
    owner._sdk_fds = {}
    sdk = os.open('sdk', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    owner._fds.append(sdk)
    owner._sdk_fds['sdk'] = sdk
    for name in ('platforms', 'platform-tools'):
        os.mkdir(name, 0o700, dir_fd=sdk)
        selected_sdk = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=sdk)
        owner._fds.append(selected_sdk)
        owner._sdk_fds['sdk/'+name] = selected_sdk
    avd = os.open('avd', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        os.mkdir(owner.avd_name+'.avd', 0o700, dir_fd=avd)
        selected = os.open(owner.avd_name+'.avd', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=avd)
        try:
            config = _avd_config(owner)
            _root_matches(owner)
            _write_at(selected, 'config.ini', config)
            _write_at(avd, owner.avd_name+'.ini', _avd_index(owner))
            _root_matches(owner)
        finally:
            os.close(selected)
    finally:
        os.close(avd)


def _pin_server_socket(owner):
    _root_matches(owner)
    root = _directory(owner.root)
    try:
        before = os.stat('server.sock', dir_fd=root, follow_symlinks=False)
        _require(stat.S_ISSOCK(before.st_mode) and before.st_uid == os.getuid())
        os.chmod('server.sock', 0o600, dir_fd=root, follow_symlinks=False)
        after = os.stat('server.sock', dir_fd=root, follow_symlinks=False)
        _require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
                 and stat.S_IMODE(after.st_mode) == 0o600)
    finally:
        os.close(root)
    owner.require_live()
    return AdbEndpoint(owner.root/'server.sock', sandbox_sha256=owner.plan.sandbox.sha256)


def launch_qualification_birth(plan, *, cancellation):
    """Explicit effects; callers must separately authorize future runtime use.

    This API accepts no PID, endpoint, process, credentials, JSON approval flag,
    imported AVD, or command. Current implementation/tests do not qualify runtime.
    """
    _require(type(plan) is BirthPlan and sys.platform == 'darwin'
             and callable(getattr(cancellation, 'is_set', None)) and not cancellation.is_set())
    root = Path(plan.output)
    _require(len(os.fsencode(root/'server.sock')) < 104 and len(os.fsencode(root/'report.sock')) < 104)
    deadline = time.monotonic()+plan.lifetime_seconds
    require_pinned_adb(plan.adb.path)
    for selected in (plan.aapt, plan.sandbox, plan.sample_apk, plan.helper_apk):
        selected.verify(cancellation=cancellation, deadline=deadline)
    plan.emulator.verify(cancellation=cancellation, deadline=deadline)
    plan.image.verify(cancellation=cancellation, deadline=deadline)
    properties = Path(plan.image.root)/'source.properties'
    with os.fdopen(open_regular(properties.parent, properties.name), 'rb') as stream:
        raw = stream.read(65537)
    _require(len(raw) <= 65536)
    expected = {b'AndroidVersion.ApiLevel': b'36', b'SystemImage.Abi': b'arm64-v8a',
                b'SystemImage.TagId': b'default', b'Pkg.Revision': b'2'}
    selected_properties = {}
    for line in raw.splitlines():
        key, separator, value = line.strip().partition(b'=')
        if separator and key in expected:
            _require(key not in selected_properties, 'Duplicate selected image property')
            selected_properties[key] = value
    _require(selected_properties == expected,
             'Only the selected offline AOSP API36 arm64 revision 2 image is supported')
    owner = OwnedBirth(_MINT)
    owner.plan, owner.root, owner.cancellation = plan, root, cancellation
    owner.deadline = deadline
    owner._pid, owner._closed, owner._ready = os.getpid(), False, False
    owner._children, owner._listeners, owner._fds = [], [], []
    owner._context = None
    owner.server = owner.emulator = None
    owner._operations = _ProcessOwner()
    owner.avd_name = 'reproof_'+uuid.uuid4().hex
    _OWNERS[id(owner)] = owner
    try:
        owner._stage = 'prepare'
        _prepare_directories(owner)
        _require_launch_layout(owner)
        notify = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        owner._listeners.append(notify)
        notify.bind(('127.0.0.1', 0));notify.listen(1);notify.settimeout(.1)
        owner.notification_port = notify.getsockname()[1]
        _require(type(owner.notification_port) is int
                 and 1024 <= owner.notification_port <= 65535
                 and owner.notification_port not in {5037, plan.console_port, plan.console_port+1},
                 'Private notification port must be unprivileged and distinct')
        report = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        owner._listeners.append(report)
        report.bind(str(root/'report.sock'));report.listen(1);report.settimeout(.1)
        environment = _environment(root/'home', root/'android', root/'avd', root/'client', owner.notification_port)
        server = _new_private_child(owner, 'server')
        server_profile = _sandbox(plan, root, root/'server.sock', root/'report.sock', owner.notification_port, emulator=False)
        _active(cancellation, owner.deadline)
        require_pinned_adb(plan.adb.path)
        plan.sandbox.verify(cancellation=cancellation, deadline=owner.deadline)
        owner._stage = 'server-start'
        server.start(('/usr/bin/sandbox-exec', '-p', server_profile, plan.adb.path,
                      '-L', 'localfilesystem:'+str(root/'server.sock'), 'server', 'nodaemon'), dict(environment, TMPDIR=str(root/'server-work')), root/'server-work')
        owner.server = server.process
        owner.server_pid = server.process.pid
        _require(not output_alert(owner._children), 'Private server output warning')
        emulator = _new_private_child(owner, 'emulator')
        profile = _sandbox(plan, root, root/'server.sock', root/'report.sock', owner.notification_port, emulator=True)
        plan.emulator.verify(cancellation=OutputCancellation(cancellation, owner._children),
                             deadline=owner.deadline)
        _active(cancellation, owner.deadline)
        _require_launch_layout(owner)
        _require(not output_alert(owner._children), 'Private server output warning')
        owner._stage = 'emulator-start'
        emulator.start(('/usr/bin/sandbox-exec', '-p', profile, str(Path(plan.emulator.root)/'emulator'),
                        '-avd', owner.avd_name, '-sysdir', plan.image.root, '-port', str(plan.console_port),
                        '-report-console', 'unix:'+str(root/'report.sock')+',max=10',
                        '-no-window', '-no-audio', '-no-snapshot', '-no-boot-anim',
                        # Skip optional metrics and ambient host DNS discovery.
                        # Loopback DNS is still denied by the unchanged sandbox.
                        '-no-metrics', '-dns-server', '127.0.0.1',
                        '-feature', '-BluetoothEmulation,-Uwb,-WiFiPacketStream',
                        '-gpu', 'swiftshader_indirect'), _emulator_environment(owner), root/'client')
        owner.emulator = emulator.process
        owner.require_live()
        until = min(owner.deadline, time.monotonic()+10)
        while not (root/'server.sock').exists():
            owner.require_live();_require(time.monotonic() < until);time.sleep(.05)
        owner.endpoint = _pin_server_socket(owner)
        owner.socket_identity = owner.endpoint._identity
        _require(_server_query(owner, b'host:version') == b'0029')
        owner._stage = 'callbacks'
        _accept_exact(owner, report, str(plan.console_port).encode())
        _accept_exact(owner, notify, ('host:emulator:'+str(plan.console_port+1)).encode(), framed=True)
        _server_query(owner, ('host:emulator:'+str(plan.console_port+1)).encode(), registration=True)
        _bootstrap_context(owner)
        owner._stage = 'inventory'
        until = min(owner.deadline, time.monotonic()+180)
        while True:
            owner.require_live();_require(time.monotonic() < until)
            owner._inventory_polls = getattr(owner, '_inventory_polls', 0)+1
            owner._inventory_query = 'pending'
            try:
                inventory = _server_query(owner, b'host:devices')
            except BaseException:
                owner._inventory_query = 'failed'
                raise
            owner._inventory_query = 'returned'
            owner._inventory_digest = hashlib.sha256(inventory).hexdigest()
            owner._inventory_bytes = len(inventory)
            owner._inventory_state = inventory_state(inventory, 'emulator-'+str(plan.console_port))
            _require(owner._inventory_state != 'unexpected', 'Private inventory mismatch')
            if owner._inventory_state == 'selected-device':
                owner._boot_state = 'pending'
                try:
                    boot = _fixed_query(owner, 'getprop sys.boot_completed').strip()
                except BaseException:
                    owner._boot_state = 'query-failed'
                    raise
                owner._boot_state = 'complete' if boot == b'1' else 'not-complete' if boot in (b'', b'0') else 'unexpected'
                _require(owner._boot_state != 'unexpected', 'Private boot property mismatch')
                if owner._boot_state == 'complete':
                    break
            time.sleep(.2)
        owner._stage = 'identity'
        _assert_avd(owner)
        owner._stage = 'install-sample'
        _provision(owner, 'io.reproof.sample', plan.sample_apk)
        owner._stage = 'install-helper'
        _provision(owner, 'io.reproof.live', plan.helper_apk)
        owner._stage = 'ready'
        owner._ready = True
        owner.require_live()
        return owner
    except BaseException as error:
        raise BirthIncomplete(owner) from None
