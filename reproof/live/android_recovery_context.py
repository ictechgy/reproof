"""UNQUALIFIED private ADB dispatch boundary with retained launcher ownership.

Only the separate effectful qualification launcher can issue a context. An
arbitrary PID, socket, Popen object or JSON declaration cannot create one.
Ordinary recovery remains disabled; real Darwin isolation is not yet qualified.
"""
from __future__ import annotations

import hashlib
import http.client
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

from reproof.adb_endpoint import AdbEndpoint, AdbEndpointError, AdbGateway, ScopedAdbClient, _frame
from reproof.core import ContractError
from reproof.execution.artifacts import open_regular
from reproof.protected_validation import _peer_uid
from reproof.repair_android_signing import _ProcessOwner, _Collector, _process_environment
from .android_recovery_authority import _directory

_HELPER = 'io.reproof.live'
_OWNED_BIRTHS = {}  # Private launcher registry; no import or restart enrollment.
_MAX_OUTPUT = 65536


def _require(value, message='Private ADB context unavailable'):
    if not value:
        raise ContractError(message)


def _inode(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns


def _shell_allowed(args, package):
    """Finite typed commands; no command supplied by a recording or HTTP caller."""
    if type(args) is not tuple or not args or not all(type(x) is str for x in args):
        return False
    if args == ('getprop', 'ro.kernel.qemu') or args == ('pidof', _HELPER):
        return True
    if args in {('am', 'force-stop', _HELPER),
                ('pm', 'path', _HELPER), ('pm', 'path', package)}:
        return True
    if len(args) == 2 and args[0] == 'cat':
        return re.fullmatch(r'/proc/[1-9][0-9]{0,9}/(?:stat|cmdline)', args[1]) is not None
    if len(args) == 4 and args[:3] == ('test', '!', '-e'):
        return re.fullmatch(r'/proc/[1-9][0-9]{0,9}', args[3]) is not None
    if len(args) == 2 and args[0] == 'sha256sum':
        return (len(args[1]) <= 1024 and '..' not in args[1].split('/')
            and re.fullmatch(r'/data/app/[A-Za-z0-9_./+=~-]+\.apk', args[1]) is not None)
    return False


def _services(command):
    encoded = command.encode('utf-8')
    _require(len(encoded) <= 4096 and b'\0' not in encoded)
    return frozenset(prefix + encoded for prefix in (b'shell:', b'shell,v2,raw:'))


class _PrivateGateway(AdbGateway):
    """Finite relay in a dedicated namespace outside client-writable work.

    Creation assumes one trusted writer and no hostile same-UID mutation. This
    is not isolation from arbitrary same-UID processes. Cleanup still rejects
    observed replacement/foreign entries and never recursively removes content.
    """
    def __init__(self, context, services):
        context._require_live()
        _require(type(services) is frozenset and services and len(services) <= 2
            and all(type(x) is bytes and 0 < len(x) <= 8192 for x in services))
        super().__init__(context.endpoint, context.serial)
        self._context = context
        self._services = services
        self._parent_fd = self._root_fd = None
        self._namespace = None
        self._socket_stamp = None
        self._bind_attempted = False
        self.cleanup_incomplete = False
        self._close_result = None

    def __enter__(self):
        self._context._require_live()
        try:
            self._parent_fd = _directory(self._context.gateway_root)
            info = os.fstat(self._parent_fd)
            _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
            self._require_outside_work()
            name = 'g-' + uuid.uuid4().hex[:16]
            os.mkdir(name, 0o700, dir_fd=self._parent_fd)
            self._namespace = self._context.gateway_root / name
            self._root_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._parent_fd)
            self._require_namespace()
            _require(not self._namespace_entries())
            self.socket_path = self._namespace / 'client.sock'
            _require(len(os.fsencode(self.socket_path)) < 104, 'Private ADB socket path too long')
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._bind_attempted = True
            self._listener.bind(str(self.socket_path))
            os.chmod('client.sock', 0o600, dir_fd=self._root_fd, follow_symlinks=False)
            self._socket_stamp = _stamp(os.stat('client.sock', dir_fd=self._root_fd, follow_symlinks=False))
            self._require_namespace()
            _require(set(self._namespace_entries()) == {'client.sock'})
            _require(stat.S_ISSOCK(os.stat('client.sock', dir_fd=self._root_fd, follow_symlinks=False).st_mode))
            self._listener.listen(8)
            self._listener.settimeout(.05)
            self._thread = threading.Thread(target=self._accept, name='repro-private-adb-gateway', daemon=True)
            self._thread.start()
            self._context._require_live()
            return self
        except BaseException:
            try:self.close()
            except (OSError, RuntimeError, ContractError):pass
            raise

    def _namespace_entries(self):
        # Only empty / exactly-one-socket matters; never enumerate foreign trees.
        with os.scandir(self._root_fd) as entries:
            first = next(entries, None)
            second = next(entries, None)
            _require(second is None, 'Private gateway contains foreign entries')
            return [] if first is None else [first.name]

    def _require_outside_work(self):
        # Compare actual ancestry, not lexical paths (including macOS aliases).
        work = _directory(self._context.work)
        current = None
        try:
            current = os.dup(self._parent_fd)
            identity = _inode(os.fstat(work))
            for _ in range(128):
                here = _inode(os.fstat(current))
                _require(here != identity, 'Private gateway is inside client-writable work')
                parent = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                try:
                    above = _inode(os.fstat(parent))
                except BaseException:
                    os.close(parent)
                    raise
                os.close(current)
                current = parent
                if here == above:
                    return
            raise ContractError('Private gateway ancestry exceeded bound')
        finally:
            if current is not None:os.close(current)
            os.close(work)

    def _require_namespace(self):
        _require(self._namespace is not None and self._root_fd is not None)
        parent = _directory(self._namespace.parent)
        try:
            _require(_inode(os.fstat(parent)) == _inode(os.fstat(self._parent_fd)))
            opened = os.fstat(self._root_fd)
            bound = os.stat(self._namespace.name, dir_fd=self._parent_fd, follow_symlinks=False)
            _require(_inode(opened) == _inode(bound) and stat.S_ISDIR(bound.st_mode)
                and bound.st_uid == os.getuid() and stat.S_IMODE(bound.st_mode) == 0o700)
        finally:
            os.close(parent)

    def _remove_owned_namespace(self):
        if self._namespace is None:
            return
        self._require_namespace()
        entries = self._namespace_entries()
        if self._socket_stamp is None:
            _require(not self._bind_attempted and not entries)
        else:
            _require(entries == ['client.sock'] and self._listener is not None
                and self._listener.fileno() >= 0)
            node = os.stat('client.sock', dir_fd=self._root_fd, follow_symlinks=False)
            _require(stat.S_ISSOCK(node.st_mode) and node.st_uid == os.getuid()
                and stat.S_IMODE(node.st_mode) == 0o600 and _stamp(node) == self._socket_stamp)
            # Keep listener/vnode and directory descriptors alive until unlink.
            os.unlink('client.sock', dir_fd=self._root_fd)
        self._require_namespace()
        _require(not self._namespace_entries())
        os.rmdir(self._namespace.name, dir_fd=self._parent_fd)

    def _connect(self):
        self._context._require_live()
        connection = super()._connect()
        try:self._context._require_live()
        except BaseException:
            with self._lock:self._sockets.discard(connection)
            connection.close()
            raise
        return connection

    def _handle(self, client):
        upstream = None
        relaying = False
        try:
            self._context._require_live()
            client.settimeout(.1)
            _require(_peer_uid(client) == os.getuid())
            service = self._service(client)
            serial = self.serial.encode('ascii')
            queries = frozenset((b'host:host-features', b'host-serial:' + serial + b':features'))
            transports = frozenset((b'host:tport:serial:' + serial, b'host:transport:' + serial))
            _require(service == b'host:version' or service in queries or service in transports)
            version = self._version()
            if service == b'host:version':
                client.sendall(b'OKAY' + _frame(version));return
            if service in queries:
                result = self._query(service)
                client.sendall(b'OKAY' + _frame(result));return
            upstream = self._connect();upstream.sendall(_frame(service))
            _require(self._read(upstream, 4) == b'OKAY')
            transport = self._read(upstream, 8) if service.startswith(b'host:tport:') else b''
            client.sendall(b'OKAY' + transport)
            command = self._service(client)
            _require(command in self._services)
            self._context._require_live()
            upstream.sendall(_frame(command));_require(self._read(upstream, 4) == b'OKAY')
            client.sendall(b'OKAY');relaying = True
            self._relay(client, upstream)
            self._context._require_live()
        except (OSError, RuntimeError, ValueError, TypeError):
            if not relaying:
                try:client.sendall(b'FAIL' + _frame(b'private ADB endpoint rejected'))
                except OSError:pass
        finally:
            with self._lock:
                self._sockets.discard(client)
                if upstream is not None:self._sockets.discard(upstream)
                self._workers.discard(threading.current_thread())
            if upstream is not None:upstream.close()
            client.close();self._slots.release()

    def _close(self, deadline):
        if self._close_result is not None:
            _require(self._close_result, 'Private ADB cleanup incomplete')
            return
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(max(0, deadline-time.monotonic()))
        clean = self._thread is None or not self._thread.is_alive()
        with self._lock:sockets=tuple(self._sockets);workers=tuple(self._workers)
        for connection in sockets:
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        for worker in workers:worker.join(max(0, deadline-time.monotonic()))
        clean = clean and all(not worker.is_alive() for worker in workers)
        self.workers_collected = clean
        try:
            _require(clean, 'Private ADB workers not collected')
            self._remove_owned_namespace()
        except (OSError, RuntimeError, ValueError):
            clean = False
        finally:
            self.cleanup_incomplete = not clean
            self._close_result = clean
            if self._listener is not None:self._listener.close()
            for field in ('_root_fd', '_parent_fd'):
                fd = getattr(self, field)
                if fd is not None:os.close(fd);setattr(self, field, None)
        _require(clean, 'Private ADB cleanup incomplete')


class _PrivateClient(ScopedAdbClient):
    def __init__(self, context):
        self.context = context
        super().__init__(context.adb, context.adb_digest, context.endpoint,
            serial=context.serial, work_root=context.work, sandbox_sha256=context.sandbox_digest)

    def _gateway_for(self, services):
        with self._lock:
            _require(not self._closed.is_set())
            self.context._require_live()
            _require(len(self._gateways)<32,'Private ADB gateway budget exceeded')
            gateway = _PrivateGateway(self.context, services)
            gateway.__enter__();self._gateways.add(gateway)
            return gateway

    def _gateway(self):
        raise ContractError('Private ADB requires a fixed service selection')

    def prepare_fixed(self, command):
        from reproof.adb_endpoint import adb_client_sandbox
        self.context._require_live()
        gateway = self._gateway_for(_services(command))
        profile = adb_client_sandbox(self.adb, self.work_root, gateway.socket_path)
        argv = ('/usr/bin/sandbox-exec', '-p', profile, str(self.adb),
            '-L', 'localfilesystem:' + str(gateway.socket_path), '-s', self.serial,
            'shell', '-T', command)
        return argv, gateway


class _OwnedInstrumentation:
    """Registered before launch; no background worker can start a late process."""
    def __init__(self, context, arguments, gateway):
        self._context, self._gateway, self._arguments = context, gateway, arguments
        self._process = None
        self._collectors = []
        self._launch_attempted = False
        self._collected = False

    def start(self, guard):
        self._context._require_handle(self)
        self._context._guard(guard)
        _require(not self._launch_attempted)
        self._launch_attempted = True
        environment = _process_environment(self._context.work,
            private_homes=(self._context.home,self._context.android_user_home))
        self._context._guard(guard)
        # This call occurs on the authority-owning caller thread. On any partial
        # failure this already registered handle and gateway remain retained.
        self._process = subprocess.Popen(self._arguments,cwd=self._context.work,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env=environment,close_fds=True,start_new_session=True)
        self._context._guard(guard)
        self._collectors = [_Collector(self._process.stdout,_MAX_OUTPUT),
                            _Collector(self._process.stderr,_MAX_OUTPUT)]
        for collector in self._collectors:collector.thread.start()
        self._context._guard(guard)

    @property
    def pid(self):
        self._context._require_handle(self)
        _require(self._process is not None)
        return self._process.pid

    def poll(self):
        self._context._require_handle(self)
        _require(self._process is not None)
        _require(not any(c.oversized for c in self._collectors),'Private instrumentation output exceeded bound')
        return self._process.poll()

    def wait(self, timeout=None):
        self._context._require_handle(self)
        _require(self._process is not None and type(timeout) in (int,float) and 0<=timeout<=10)
        return self._process.wait(timeout=timeout)


class IsolatedAdbContext:
    """Only the explicit retained qualification launcher can mint this context."""
    def __new__(cls, *args, **kwargs):
        raise ContractError('Direct ADB context construction is unsupported; use the qualification launcher')

    @classmethod
    def birth(cls, *args, **kwargs):
        # Deliberately before path creation, tool execution, sockets or discovery.
        raise ContractError('Direct ADB context birth is unsupported; use the qualification launcher')

    def _require_live(self):
        _require(sys.platform == 'darwin', 'Private ADB requires the reviewed Darwin sandbox')
        birth = _OWNED_BIRTHS.get(id(self))
        _require(birth is not None and birth is self._birth and birth._context is self
            and self._pid == os.getpid() and not self._closed and not getattr(self,'_failed',False)
            and time.monotonic() < self.deadline and not self.cancellation.is_set(),
            'Live owned ADB birth required')
        from .android_recovery_birth import OwnedBirth
        _require(type(birth) is OwnedBirth, 'Live qualification launcher owner required')
        birth.require_live()
        _require(type(birth.server) is subprocess.Popen and birth.server is self._server
            and birth.server.poll() is None and birth.server.pid == birth.server_pid,
            'Owned ADB server epoch ended')
        for path, fd in ((self.work,self._work_fd),(self.home,self._home_fd),
                         (self.android_user_home,self._android_fd),(self.gateway_root,self._gateway_fd)):
            opened=_directory(path)
            try:
                info=os.fstat(opened)
                _require(_inode(info)==_inode(os.fstat(fd)) and info.st_uid==os.getuid()
                    and stat.S_IMODE(info.st_mode)==0o700, 'Private ADB namespace changed')
            finally:os.close(opened)
        self.endpoint.verify()
        _require(self.endpoint._identity == birth.socket_identity, 'Owned ADB socket epoch changed')
        for path, expected in ((self.adb,self.adb_digest),(self.aapt,self.aapt_digest),
                               (Path('/usr/bin/sandbox-exec'),self.sandbox_digest)):
            from reproof.ios_provisioning_cms import _public_file_digest
            _require(_public_file_digest(path)==expected, 'Private ADB tool changed')

    def require_bound(self, serial, target_package, helper_digest, app_digest):
        self._require_live()
        _require((serial,target_package,helper_digest,app_digest)==self._binding,
            'Private ADB context binding differs')

    def _guard(self, guard):
        _require(callable(guard))
        guard();self._require_live()

    def _run_fixed(self, command, guard, *, timeout=5, input_bytes=b'', allow_absent=False):
        self._guard(guard)
        _require(type(timeout) in (int,float) and 0<timeout<=10
            and type(input_bytes) is bytes and len(input_bytes)<=16384)
        argv,gateway=self._client.prepare_fixed(command)
        try:
            self._guard(guard)
            result=self._client._owner.run(argv,work=self.work,input_bytes=input_bytes,pass_fds=(),
                cancellation=self.cancellation,deadline_monotonic=min(self.deadline,time.monotonic()+timeout),
                max_output_bytes=_MAX_OUTPUT,private_homes=(self.home,self.android_user_home))
            self._guard(guard)
            _require(result.terminated and result.bounded and not result.interrupted
                and (result.returncode==0 or allow_absent and result.returncode==1 and result.stdout==b''))
            return result.stdout
        finally:self._client.finish_gateway(gateway)

    def run_shell(self,args_tuple,guard,timeout=5,input_bytes=b'',allow_absent=False):
        _require(_shell_allowed(args_tuple,self.target_package) and input_bytes==b''
            and type(allow_absent) is bool)
        _require(not allow_absent or args_tuple==('pidof',_HELPER))
        return self._run_fixed(shlex.join(args_tuple),guard,timeout=timeout,allow_absent=allow_absent)

    def write_helper_configuration(self,raw,guard):
        _require(type(raw) is bytes and 0<len(raw)<=16384)
        command=shlex.join(('run-as',_HELPER,'sh','-c',
            'umask 077 && mkdir -p files && cat > files/live-config.json'))
        return self._run_fixed(command,guard,input_bytes=raw)

    def apk_identity(self,path,guard):
        self._guard(guard)
        path=Path(path).absolute()
        _require(path in self._artifacts, 'Unselected APK inspector input')
        expected=self._artifacts[path]
        from .android_recovery_transport import _digest_file
        _require(_digest_file(path,150*1024*1024)==expected)
        from reproof.adb_endpoint import adb_client_sandbox
        profile=adb_client_sandbox(self.aapt,self.work,self.endpoint.socket_path)
        profile += '(allow file-read* (literal ' + json.dumps(str(path)) + '))'
        argv=('/usr/bin/sandbox-exec','-p',profile,str(self.aapt),'dump','badging',str(path))
        self._guard(guard)
        result=self._client._owner.run(argv,work=self.work,input_bytes=b'',pass_fds=(),
            cancellation=self.cancellation,deadline_monotonic=min(self.deadline,time.monotonic()+10),
            max_output_bytes=_MAX_OUTPUT,private_homes=(self.home,self.android_user_home))
        self._guard(guard)
        _require(_digest_file(path,150*1024*1024)==expected and result.terminated and result.bounded
            and not result.interrupted and result.returncode==0)
        rows=re.findall(rb"^package: name='([^']+)' versionCode='([0-9]+)'[^\r\n]*$",result.stdout,re.M)
        _require(len(rows)==1)
        return {'package':rows[0][0].decode('ascii'),'versionCode':int(rows[0][1])}

    def call_helper(self,path,body,token,timeout,binary,guard):
        self._guard(guard)
        _require(path in {'/status','/inspect','/frame','/command','/qualification-recovery',
                         '/qualification-recovery/status'}
            or re.fullmatch(r'/ack/(?:[a-f0-9]{32}|[a-z][a-z0-9_-]{0,63})',path)
            or re.fullmatch(r'/frames/after/[0-9]+',path)
            or re.fullmatch(r'/qualification-recovery/recovery_[a-f0-9]{32}(?:/frame)?',path))
        _require(type(token) is str and 20<=len(token)<=128 and re.fullmatch(r'[A-Za-z0-9_-]+',token)
            and type(timeout) in (int,float) and 0<timeout<=30 and type(binary) is bool)
        data=None if body is None else json.dumps(body,allow_nan=False,separators=(',',':')).encode()
        _require(data is None or len(data)<=16384)
        gateway=self._client._gateway_for(frozenset((b'tcp:8766',)))
        connection=client=None
        done=threading.Event();expired=threading.Event();watcher=None
        deadline=min(self.deadline,time.monotonic()+timeout)
        try:
            connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            connection.settimeout(max(.001,deadline-time.monotonic()))
            def cancel_io():
                while not done.wait(.02):
                    if self.cancellation.is_set() or time.monotonic()>=deadline:
                        expired.set()
                        try:connection.shutdown(socket.SHUT_RDWR)
                        except OSError:pass
                        return
            watcher=threading.Thread(target=cancel_io,daemon=True);watcher.start()
            connection.connect(str(gateway.socket_path))
            connection.sendall(_frame(b'host:tport:serial:'+self.serial.encode()))
            _require(gateway._read(connection,4)==b'OKAY');gateway._read(connection,8)
            connection.sendall(_frame(b'tcp:8766'));_require(gateway._read(connection,4)==b'OKAY')
            client=http.client.HTTPConnection('127.0.0.1',8766,timeout=timeout)
            client.auto_open=0;client.sock=connection
            headers={'Authorization':'Bearer '+token,'Connection':'close'}
            if data is not None:headers['Content-Type']='application/json'
            self._guard(guard)
            client.request('GET' if data is None else 'POST',path,data,headers)
            response=client.getresponse();limit=4*1024*1024 if binary else _MAX_OUTPUT
            raw=response.read(limit+1)
            self._guard(guard)
            _require(response.status in {200,202} and len(raw)<=limit and not expired.is_set()
                and time.monotonic()<deadline)
            if binary:return raw
            value=json.loads(raw);_require(type(value) is dict)
            return value
        finally:
            done.set()
            if watcher is not None:watcher.join(1)
            if client is not None:client.close()
            if connection is not None:connection.close()
            self._client.finish_gateway(gateway)

    def start_instrumentation(self,guard):
        self._guard(guard)
        _require(not self._handles, 'Private instrumentation owner already exists')
        argv,gateway=self._client.prepare_fixed('am instrument -w -r '+_HELPER+'/.LiveInstrumentation')
        handle=_OwnedInstrumentation(self,argv,gateway)
        self._handles[id(handle)]=handle
        try:
            handle.start(guard)
            self._guard(guard)
            return handle
        except BaseException:
            # Retain the handle/gateway even if launch or its postcheck failed.
            # No worker can create a process later; cleanup is explicit only.
            self._failed=True
            raise

    def _require_handle(self,handle):
        self._require_live()
        _require(type(handle) is _OwnedInstrumentation and self._handles.get(id(handle)) is handle
            and handle._context is self, 'Owned instrumentation handle required')

    def collect_instrumentation(self,handle,guard):
        self._guard(guard);self._require_handle(handle)
        _require(handle._process is not None and not handle._collected,
            'Private instrumentation birth or collection unconfirmed')
        deadline=min(self.deadline,time.monotonic()+5)
        if handle._process.poll() is None:
            self._guard(guard)
            clean=_ProcessOwner._terminate(handle._process,deadline_monotonic=deadline)
        else:
            clean=_ProcessOwner._group_empty(handle._process.pid)
        for collector in handle._collectors:
            collector.thread.join(max(0,deadline-time.monotonic()))
        self._guard(guard)
        _require(clean and handle._process.poll() is not None and len(handle._collectors)==2
            and all(not c.thread.is_alive() and not c.oversized for c in handle._collectors),
            'Private instrumentation collection unconfirmed')
        self._client.finish_gateway(handle._gateway)
        self._guard(guard)
        handle._collected=True
        self._handles.pop(id(handle))
        return True
