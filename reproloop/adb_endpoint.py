"""Fixed smart-socket boundary for a selected, operator-owned ADB endpoint.

The gateway rejects daemon control and foreign transport selection. It does
not start an ADB server or qualify the endpoint's authentication environment.
"""
from __future__ import annotations

from dataclasses import dataclass,field
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import select
import socket
import tempfile
import threading
import time

from . import contracts
from .protected_validation import _peer_uid,_socket_namespace
from .repair_android_signing import _ProcessOwner
from .repair_signing_configuration import _path


class AdbEndpointError(RuntimeError):
    def __init__(self):
        self.code='adb_endpoint_unavailable'
        super().__init__('Selected ADB endpoint or client boundary is unavailable')


def _require(value):
    if not value:raise AdbEndpointError()


def _frame(body):
    _require(type(body) is bytes and 0<len(body)<=65535)
    return f'{len(body):04x}'.encode('ascii')+body


@dataclass(frozen=True,slots=True)
class AdbEndpoint:
    socket_path: Path = field(repr=False)
    server_version: int = 41
    sandbox_sha256: str | None = None
    _identity: tuple = field(init=False,repr=False)

    def __post_init__(self):
        try:
            path=_path(str(self.socket_path));_require(len(os.fsencode(path))<104 and type(self.server_version) is int and self.server_version==41)
            descriptor,identity=_socket_namespace(path);os.close(descriptor)
            if self.sandbox_sha256 is not None:contracts.validate_digest(self.sandbox_sha256)
            object.__setattr__(self,'socket_path',path);object.__setattr__(self,'_identity',identity)
        except (OSError,RuntimeError,ValueError,TypeError):raise AdbEndpointError() from None

    def verify(self):
        try:
            descriptor,identity=_socket_namespace(self.socket_path);os.close(descriptor)
            _require(identity==self._identity)
            if self.sandbox_sha256 is not None:
                from .ios_provisioning_cms import _public_file_digest
                _require(_public_file_digest(Path('/usr/bin/sandbox-exec'))==self.sandbox_sha256)
        except (OSError,RuntimeError,ValueError,TypeError):raise AdbEndpointError() from None

    @property
    def definition_digest(self):
        return contracts.digest({'socketPathDigest':contracts.digest(str(self.socket_path)),
            'socketIdentity':list(self._identity),'serverVersion':self.server_version,'sandboxSha256':self.sandbox_sha256})


class AdbGateway:
    """One bounded SDK execution; every upstream connection is explicitly selected."""
    def __init__(self,endpoint,serial):
        _require(type(endpoint) is AdbEndpoint and type(serial) is str and 0<len(serial)<=256
                 and not any(character.isspace() or ord(character)<32 for character in serial))
        endpoint.verify();self.endpoint=endpoint;self.serial=serial
        self._stop=threading.Event();self._lock=threading.RLock();self._workers=set();self._sockets=set()
        self._slots=threading.BoundedSemaphore(8);self._temporary=None;self._listener=None;self._thread=None
        self._close_lock=threading.Lock()

    def __enter__(self):
        try:
            self._temporary=tempfile.TemporaryDirectory(prefix='ag-',dir='/private/tmp')
            self.socket_path=Path(self._temporary.name).resolve()/'client.sock'
            self._listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            self._listener.bind(str(self.socket_path));self.socket_path.chmod(0o600)
            self._listener.listen(8);self._listener.settimeout(.05)
            self._thread=threading.Thread(target=self._accept,name='repro-adb-gateway',daemon=True);self._thread.start()
            return self
        except BaseException:
            self.close();raise

    def _read(self,connection,count):
        result=bytearray();deadline=time.monotonic()+10
        while len(result)<count:
            _require(not self._stop.is_set() and time.monotonic()<deadline)
            try:body=connection.recv(count-len(result))
            except socket.timeout:continue
            _require(bool(body));result.extend(body)
        return bytes(result)

    def _service(self,connection):
        header=self._read(connection,4);_require(re.fullmatch(b'[0-9a-fA-F]{4}',header) is not None)
        count=int(header,16);_require(0<count<=65535)
        return self._read(connection,count)

    def _connect(self):
        self.endpoint.verify();connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            connection.settimeout(.1);connection.connect(str(self.endpoint.socket_path))
            _require(_peer_uid(connection)==os.getuid());self.endpoint.verify()
            with self._lock:self._sockets.add(connection)
            return connection
        except BaseException:connection.close();raise

    def _query(self,service):
        connection=self._connect()
        try:
            connection.sendall(_frame(service));_require(self._read(connection,4)==b'OKAY')
            return self._service(connection)
        finally:
            with self._lock:self._sockets.discard(connection)
            connection.close()

    def _version(self):
        value=self._query(b'host:version')
        _require(value==f'{self.endpoint.server_version:04x}'.encode('ascii'))
        return value

    def _relay(self,left,right):
        left.setblocking(False);right.setblocking(False)
        peers={left:right,right:left};buffers={left:bytearray(),right:bytearray()}
        readable={left,right};shutdown=set();total=0;deadline=time.monotonic()+900
        while readable or any(buffers.values()):
            _require(not self._stop.is_set() and time.monotonic()<deadline)
            reads=[item for item in readable if len(buffers[peers[item]])<65536]
            writes=[item for item in peers if buffers[item]]
            ready,writing,_=select.select(reads,writes,[],.05)
            for connection in ready:
                try:body=connection.recv(16384)
                except BlockingIOError:continue
                if not body:readable.discard(connection)
                else:
                    total+=len(body);_require(total<=520*1024**2);buffers[peers[connection]].extend(body)
            for connection in writing:
                try:count=connection.send(buffers[connection])
                except BlockingIOError:continue
                _require(count>0);del buffers[connection][:count]
            for connection,peer in peers.items():
                if peer not in readable and not buffers[connection] and connection not in shutdown:
                    try:connection.shutdown(socket.SHUT_WR)
                    except OSError:pass
                    shutdown.add(connection)

    def _handle(self,client):
        upstream=None;relaying=False
        try:
            client.settimeout(.1);_require(_peer_uid(client)==os.getuid())
            service=self._service(client);serial=self.serial.encode('utf-8')
            queries={b'host:devices',b'host:host-features',*(b'host-serial:'+serial+b':'+suffix
                for suffix in (b'features',b'get-state',b'get-serialno',b'get-devpath'))}
            transports={b'host:tport:serial:'+serial,b'host:transport:'+serial}
            _require(service==b'host:version' or service in queries or service in transports)
            version=self._version()
            if service==b'host:version':client.sendall(b'OKAY'+_frame(version));return
            if service in queries:
                result=self._query(service)
                if service==b'host:devices':
                    rows=[line for line in result.splitlines() if line.split(b'\t',1)[0]==serial]
                    _require(len(rows)<=1)
                    result=b''.join(line+b'\n' for line in rows)
                client.sendall(b'OKAY'+f'{len(result):04x}'.encode()+result);return
            upstream=self._connect();upstream.sendall(_frame(service))
            _require(self._read(upstream,4)==b'OKAY')
            transport=self._read(upstream,8) if service.startswith(b'host:tport:') else b''
            client.sendall(b'OKAY'+transport)
            command=self._service(client)
            _require(command==b'tcp:8766' or command.startswith((b'shell:',b'shell,',b'exec:',b'sync:',b'abb:',b'abb_exec:')))
            upstream.sendall(_frame(command));_require(self._read(upstream,4)==b'OKAY')
            client.sendall(b'OKAY');relaying=True;self._relay(client,upstream)
        except (OSError,RuntimeError,ValueError,TypeError):
            if not relaying:
                try:client.sendall(b'FAIL'+_frame(b'registered ADB endpoint rejected'))
                except OSError:pass
        finally:
            with self._lock:
                self._sockets.discard(client)
                if upstream is not None:self._sockets.discard(upstream)
                self._workers.discard(threading.current_thread())
            if upstream is not None:upstream.close()
            client.close();self._slots.release()

    def _accept(self):
        while not self._stop.is_set():
            try:client,_=self._listener.accept()
            except socket.timeout:continue
            except OSError:break
            if not self._slots.acquire(blocking=False):client.close();continue
            with self._lock:
                self._sockets.add(client)
                worker=threading.Thread(target=self._handle,args=(client,),name='repro-adb-stream',daemon=True)
                self._workers.add(worker)
            worker.start()

    def close(self,*,deadline_monotonic=None):
        deadline=time.monotonic()+3 if deadline_monotonic is None else deadline_monotonic
        acquired=self._close_lock.acquire(timeout=max(0,deadline-time.monotonic()))
        _require(acquired)
        try:self._close(deadline)
        finally:self._close_lock.release()

    def _close(self,deadline):
        self._stop.set()
        if self._listener is not None:self._listener.close()
        if self._thread is not None and self._thread.ident is not None:self._thread.join(max(0,min(1,deadline-time.monotonic())))
        _require(self._thread is None or not self._thread.is_alive())
        with self._lock:sockets=tuple(self._sockets);workers=tuple(self._workers)
        for connection in sockets:
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        for worker in workers:worker.join(max(0,deadline-time.monotonic()))
        _require(all(not worker.is_alive() for worker in workers))
        if self._temporary is not None:self._temporary.cleanup();self._temporary=None

    def __exit__(self,*_):self.close()


def adb_client_sandbox(adb,work,endpoint):
    directories=('/usr/lib','/System/Library','/System/Volumes/Preboot/Cryptexes/OS',
        '/System/Cryptexes/OS','/private/preboot/Cryptexes/OS','/dev/fd',str(work))
    files=('/dev/null','/dev/random','/dev/urandom',str(adb),'/',
        '/System','/System/Volumes','/System/Volumes/Preboot','/System/Volumes/Preboot/Cryptexes','/System/Cryptexes')
    readable=' '.join('(subpath '+json.dumps(path)+')' for path in directories)
    readable+=' '+' '.join('(literal '+json.dumps(path)+')' for path in files)
    return ('(version 1)(allow default)(deny network*)(deny mach-lookup)(deny process-fork)'
        '(deny file-read* file-write*)(allow file-read-metadata)(allow file-read* '+readable+')'
        '(allow file-write* (subpath '+json.dumps(str(work))+'))'
        '(allow network-outbound (literal '+json.dumps(str(endpoint))+'))'
        '(deny file-read-data file-read-xattr (subpath "/System/Library/Keychains")'
        ' (subpath "/System/Volumes/Preboot/Cryptexes/OS/System/Library/Keychains"))')


class ScopedAdbClient:
    def __init__(self,adb,adb_sha256,endpoint,*,serial,work_root,sandbox_sha256):
        from .ios_provisioning_cms import _public_file_digest
        try:
            self.adb=_path(str(adb));self.work_root=_path(str(work_root));self.endpoint=endpoint;self.serial=serial
            self.adb_sha256=adb_sha256;self.sandbox_sha256=sandbox_sha256
            _require(type(endpoint) is AdbEndpoint);endpoint.verify()
            contracts.validate_digest(adb_sha256);contracts.validate_digest(sandbox_sha256)
            _require(endpoint.sandbox_sha256 is None or endpoint.sandbox_sha256==sandbox_sha256)
            _require(_public_file_digest(self.adb)==adb_sha256 and os.access(self.adb,os.X_OK)
                and _public_file_digest(Path('/usr/bin/sandbox-exec'))==sandbox_sha256)
            _require(self.work_root.is_dir() and self.work_root.stat().st_uid==os.getuid()
                     and self.work_root.stat().st_mode&0o077==0)
            self._owner=_ProcessOwner();self._lock=threading.RLock();self._gateways=set();self._closed=threading.Event()
        except (OSError,RuntimeError,TypeError,ValueError):raise AdbEndpointError() from None

    @property
    def active_processes(self):
        with self._lock:return self._owner.active_processes+len(self._gateways)

    def prepare_command(self,arguments):
        from .ios_provisioning_cms import _public_file_digest
        _require(type(arguments) is tuple and arguments and arguments[0] in ('devices','shell','exec-out','install')
            and all(type(value) is str and '\0' not in value for value in arguments))
        self.endpoint.verify()
        _require(_public_file_digest(self.adb)==self.adb_sha256
            and _public_file_digest(Path('/usr/bin/sandbox-exec'))==self.sandbox_sha256)
        gateway=self._gateway()
        profile=adb_client_sandbox(self.adb,self.work_root,gateway.socket_path)
        command=('/usr/bin/sandbox-exec','-p',profile,str(self.adb),'-L','localfilesystem:'+str(gateway.socket_path),
            '-s',self.serial,*arguments)
        return command,gateway

    def _gateway(self):
        with self._lock:
            _require(not self._closed.is_set())
            gateway=AdbGateway(self.endpoint,self.serial);gateway.__enter__();self._gateways.add(gateway)
            return gateway

    def finish_gateway(self,gateway,*,deadline_monotonic=None):
        gateway.close(deadline_monotonic=deadline_monotonic)
        with self._lock:self._gateways.discard(gateway)

    def run(self,arguments,*,cancellation,deadline_monotonic,input_bytes=b''):
        command,gateway=self.prepare_command(arguments)
        try:
            return self._owner.run(command,work=self.work_root,input_bytes=input_bytes,pass_fds=(),
                cancellation=cancellation,deadline_monotonic=deadline_monotonic,max_output_bytes=4*1024**2)
        finally:self.finish_gateway(gateway)

    def call_helper(self,path,body,*,token,timeout,cancellation,deadline_monotonic,binary=False):
        _require(path in ('/status','/frame','/command','/stop','/inspect')
            or re.fullmatch(r'/ack/(?:[a-f0-9]{32}|[a-z][a-z0-9_-]{0,63})',path)
            or re.fullmatch(r'/frames/after/[0-9]+',path))
        _require(type(timeout) in (int,float) and 0<timeout<=30)
        deadline=min(deadline_monotonic,time.monotonic()+timeout)
        _require(not cancellation.is_set() and not self._closed.is_set() and time.monotonic()<deadline)
        gateway=self._gateway();connection=None;client=None;watcher=None;done=threading.Event();expired=threading.Event()
        try:
            connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);connection.settimeout(max(.001,deadline-time.monotonic()))
            def stop_late_io():
                while not done.wait(.02):
                    if cancellation.is_set() or self._closed.is_set() or time.monotonic()>=deadline:
                        expired.set()
                        try:connection.shutdown(socket.SHUT_RDWR)
                        except OSError:pass
                        return
            watcher=threading.Thread(target=stop_late_io,name='repro-adb-helper-deadline',daemon=True);watcher.start()
            connection.connect(str(gateway.socket_path))
            connection.sendall(_frame(b'host:tport:serial:'+self.serial.encode()))
            _require(gateway._read(connection,4)==b'OKAY');gateway._read(connection,8)
            connection.sendall(_frame(b'tcp:8766'));_require(gateway._read(connection,4)==b'OKAY')
            client=http.client.HTTPConnection('127.0.0.1',8766,timeout=timeout);client.auto_open=0;client.sock=connection
            data=None if body is None else json.dumps(body,allow_nan=False).encode()
            headers={'Authorization':'Bearer '+token,'Connection':'close'}
            if data is not None:headers['Content-Type']='application/json'
            client.request('GET' if data is None else 'POST',path,data,headers)
            response=client.getresponse();raw=response.read(4*1024**2+1)
            _require(response.status in (200,202) and len(raw)<=4*1024**2 and not expired.is_set()
                and not cancellation.is_set() and not self._closed.is_set() and time.monotonic()<deadline)
            if binary:return raw
            value=json.loads(raw);_require(type(value) is dict);return value
        except (OSError,RuntimeError,ValueError,TypeError):raise AdbEndpointError() from None
        finally:
            done.set()
            if watcher is not None:watcher.join(1)
            if client is not None:client.close()
            if connection is not None:connection.close()
            self.finish_gateway(gateway)

    def close(self,*,deadline_monotonic=None):
        deadline=time.monotonic()+5 if deadline_monotonic is None else deadline_monotonic
        self._closed.set();clean=self._owner.close(deadline_monotonic=deadline)
        with self._lock:gateways=tuple(self._gateways)
        for gateway in gateways:
            try:self.finish_gateway(gateway,deadline_monotonic=deadline)
            except (OSError,RuntimeError):clean=False
        return clean and self.active_processes==0


__all__=['AdbEndpointError','AdbEndpoint','AdbGateway','ScopedAdbClient']
