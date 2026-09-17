"""Actual cached ADB client against owned smart-socket protocol endpoints."""
import hashlib
import json
import os
import re
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest


ADB=Path.home()/'Library/Android/sdk/platform-tools/adb'
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def frame(body):return f'{len(body):04x}'.encode()+body


class OwnedAdbServer:
    def __init__(self,root,*,serial='owned-device',concurrent=False):
        self.serial=serial
        self.shell_response=None
        self.shell_session=None;self.helper_response=None
        self.concurrent=concurrent;self.workers=[]
        self.path=root/'upstream.sock';self.version=41;self.requests=[];self.stop=threading.Event();self.installed_bytes=None
        self.installed_digests=[]
        self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.listener.bind(str(self.path))
        self.path.chmod(0o600);self.listener.listen();self.listener.settimeout(.05)
        self.thread=threading.Thread(target=self.serve);self.thread.start()

    @staticmethod
    def read(connection,count):
        result=b''
        while len(result)<count:
            body=connection.recv(count-len(result))
            if not body:raise EOFError()
            result+=body
        return result

    def request(self,connection):
        size=int(self.read(connection,4),16)
        if not 0<size<=65535:raise ValueError()
        value=self.read(connection,size);self.requests.append(value);return value

    def serve(self):
        while not self.stop.is_set():
            try:connection,_=self.listener.accept()
            except socket.timeout:continue
            except OSError:break
            if self.concurrent:
                worker=threading.Thread(target=self.handle,args=(connection,));self.workers.append(worker);worker.start()
            else:self.handle(connection)

    def handle(self,connection):
            with connection:
                try:
                    connection.settimeout(2);request=self.request(connection)
                    if request==b'host:version':connection.sendall(b'OKAY'+frame(f'{self.version:04x}'.encode()))
                    elif request==b'host:devices':connection.sendall(b'OKAY'+frame(self.serial.encode()+b'\tdevice\nother-device\tdevice\n'))
                    elif request==b'host-serial:'+self.serial.encode()+b':features':connection.sendall(b'OKAY'+frame(b'shell_v2,cmd'))
                    elif request==b'host:tport:serial:'+self.serial.encode():
                        connection.sendall(b'OKAY'+struct.pack('<Q',7));service=self.request(connection)
                        if service.startswith(b'shell,v2,'):
                            command=service.split(b':',1)[1]
                            if self.shell_session is not None:
                                connection.sendall(b'OKAY');prefix=b'';output=self.shell_session(connection,command)
                            else:
                                prefix=b'OKAY';output=b'owned-output\n' if self.shell_response is None else self.shell_response(command)
                            connection.sendall(prefix+struct.pack('<BI',1,len(output))+output+struct.pack('<BI',3,1)+b'\0')
                        elif service==b'tcp:8766':
                            connection.sendall(b'OKAY');request=b''
                            while b'\r\n\r\n' not in request:
                                chunk=connection.recv(4096)
                                if not chunk:raise EOFError()
                                request+=chunk
                                if len(request)>65536:raise ValueError()
                            headers,received=request.split(b'\r\n\r\n',1)
                            lengths=re.findall(rb'(?im)^Content-Length: *([0-9]+)\r?$',headers)
                            if len(lengths)>1:raise ValueError()
                            length=int(lengths[0]) if lengths else 0
                            if not len(received)<=length<=65536:raise ValueError()
                            received+=self.read(connection,length-len(received))
                            body=(b'{"alive":true}' if self.helper_response is None else
                                json.dumps(self.helper_response(headers,received)).encode())
                            connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
                        elif service.startswith(b'exec:cmd package '):
                            size=re.search(rb'(?:^| )-S ([0-9]+)(?: |$)',service)
                            if size is None:raise ValueError()
                            connection.sendall(b'OKAY');self.installed_bytes=self.read(connection,int(size[1]))
                            self.installed_digests.append(hashlib.sha256(self.installed_bytes).hexdigest())
                            connection.sendall(b'Success\n')
                        else:connection.sendall(b'FAIL'+frame(b'unsupported owned service'))
                    else:connection.sendall(b'FAIL'+frame(b'unsupported owned request'))
                except (EOFError,OSError,ValueError):pass

    def close(self):
        self.stop.set();self.listener.close();self.thread.join(3)
        for worker in self.workers:worker.join(3)
        if self.path.exists():self.path.unlink()
        if self.thread.is_alive():raise RuntimeError('owned ADB server did not stop')
        if any(worker.is_alive() for worker in self.workers):raise RuntimeError('owned ADB connection did not stop')


class ScopedAdbEndpointTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='ad-',dir='/private/tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.work=self.root/'work';self.work.mkdir(mode=0o700)
        self.server=OwnedAdbServer(self.root);self.addCleanup(self.server.close)

    def client(self):
        from reproloop.adb_endpoint import AdbEndpoint,ScopedAdbClient
        result=ScopedAdbClient(ADB,sha(ADB),AdbEndpoint(self.server.path),serial='owned-device',
            work_root=self.work,sandbox_sha256=sha('/usr/bin/sandbox-exec'))
        self.addCleanup(result.close);return result

    def run_client(self,client,arguments):
        return client.run(arguments,cancellation=threading.Event(),deadline_monotonic=time.monotonic()+5)

    def test_owned_helper_server_waits_for_the_declared_request_body(self):
        connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.settimeout(1);connection.connect(str(self.server.path))
        connection.sendall(frame(b'host:tport:serial:owned-device'))
        self.assertEqual(OwnedAdbServer.read(connection,4),b'OKAY')
        OwnedAdbServer.read(connection,8)
        connection.sendall(frame(b'tcp:8766'))
        self.assertEqual(OwnedAdbServer.read(connection,4),b'OKAY')
        connection.sendall(b'POST /status HTTP/1.1\r\nHost: owned\r\nContent-Length: 2\r\nConnection: close\r\n\r\n')
        connection.settimeout(.05)
        with self.assertRaises(socket.timeout):connection.recv(1)
        connection.settimeout(1);connection.sendall(b'{}')
        response=b''
        while b'{"alive":true}' not in response:
            chunk=connection.recv(4096)
            self.assertTrue(chunk);response+=chunk
        self.assertIn(b'HTTP/1.1 200 OK',response)

    def test_actual_client_lists_only_selected_device_and_reads_shell_v2(self):
        client=self.client()
        listed=self.run_client(client,('devices',))
        self.assertEqual(listed.returncode,0);self.assertIn(b'owned-device\tdevice',listed.stdout)
        self.assertNotIn(b'other-device',listed.stdout)
        shell=self.run_client(client,('shell','echo owned-output'))
        self.assertEqual(shell.returncode,0);self.assertEqual(shell.stdout,b'owned-output\n')
        self.assertTrue(shell.terminated and shell.bounded)
        self.assertFalse(any(b'kill' in item or b'start-server' in item for item in self.server.requests))

    def test_version_mismatch_never_reaches_server_kill_or_restart(self):
        client=self.client();self.server.version=42
        result=self.run_client(client,('devices',))
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(set(self.server.requests),{b'host:version'})

    def test_missing_endpoint_does_not_start_a_server_or_replace_its_socket(self):
        client=self.client();self.server.close()
        from reproloop.adb_endpoint import AdbEndpointError
        with self.assertRaises(AdbEndpointError):self.run_client(client,('devices',))
        self.assertFalse(self.server.path.exists())

    def test_server_management_and_foreign_selection_are_rejected_before_dispatch(self):
        client=self.client()
        from reproloop.adb_endpoint import AdbEndpointError
        for arguments in (('kill-server',),('start-server',),('-s','other-device','shell','true'),
                          ('-L','tcp:5037','devices'),('connect','other-device')):
            with self.subTest(arguments=arguments),self.assertRaises(AdbEndpointError):self.run_client(client,arguments)
        self.assertEqual(self.server.requests,[])

    def test_gateway_denies_direct_control_and_foreign_transport_requests(self):
        from reproloop.adb_endpoint import AdbEndpoint,AdbGateway
        with AdbGateway(AdbEndpoint(self.server.path),'owned-device') as gateway:
            for service in (b'host:kill',b'host:tport:serial:other-device',b'host:connect:127.0.0.1:5555'):
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                    connection.settimeout(2);connection.connect(str(gateway.socket_path));connection.sendall(frame(service))
                    self.assertEqual(OwnedAdbServer.read(connection,4),b'FAIL')
        self.assertEqual(self.server.requests,[])

    def test_endpoint_permissions_symlinks_and_changed_binary_are_rejected(self):
        from reproloop.adb_endpoint import AdbEndpoint,AdbEndpointError,ScopedAdbClient
        self.server.path.chmod(0o666)
        with self.assertRaises(AdbEndpointError):AdbEndpoint(self.server.path)
        self.server.path.chmod(0o600)
        alias=self.root/'alias.sock';alias.symlink_to(self.server.path)
        with self.assertRaises(AdbEndpointError):AdbEndpoint(alias)
        with self.assertRaises(AdbEndpointError):
            ScopedAdbClient(ADB,'0'*64,AdbEndpoint(self.server.path),serial='owned-device',
                work_root=self.work,sandbox_sha256=sha('/usr/bin/sandbox-exec'))

    def test_actual_os_policy_blocks_outside_files_network_and_server_process_spawn(self):
        from reproloop.adb_endpoint import adb_client_sandbox
        source=self.root/'probe.c';binary=self.root/'probe'
        source.write_text(r'''
#include <arpa/inet.h>
#include <fcntl.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
int main(int argc,char **argv) {
    if (argc==2 && !strcmp(argv[1],"--child")) return 0;
    if (argc!=4) return 64;
    int input=open(argv[1],O_RDONLY); int readable=input>=0; if(input>=0) close(input);
    int output=open(argv[2],O_WRONLY|O_CREAT|O_EXCL,0600); int writable=output>=0; if(output>=0) close(output);
    int fd=socket(AF_INET,SOCK_STREAM,0); struct sockaddr_in target={0};
    target.sin_family=AF_INET; target.sin_port=htons((unsigned short)atoi(argv[3]));
    inet_pton(AF_INET,"127.0.0.1",&target.sin_addr);
    int network=fd>=0 && connect(fd,(struct sockaddr*)&target,sizeof(target))==0; if(fd>=0) close(fd);
    pid_t child=0; char *args[]={argv[0],"--child",NULL};
    int spawned=posix_spawn(&child,argv[0],NULL,NULL,args,environ)==0;
    if(spawned) waitpid(child,NULL,0);
    printf("{\"read\":%d,\"write\":%d,\"network\":%d,\"spawn\":%d}\n",readable,writable,network,spawned);
    return 0;
}
''')
        built=subprocess.run(['/usr/bin/clang','-Wall','-Wextra','-Werror',str(source),'-o',str(binary)],
            stdin=subprocess.DEVNULL,capture_output=True,timeout=15)
        self.assertEqual(built.returncode,0)
        canary=self.root/'outside-canary';canary.write_bytes(b'owned canary')
        created=self.root/'outside-created'
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1',0));listener.listen(2)
            arguments=[str(binary),str(canary),str(created),str(listener.getsockname()[1])]
            control=subprocess.run(arguments,stdin=subprocess.DEVNULL,capture_output=True,timeout=5)
            self.assertEqual(json.loads(control.stdout),{'read':1,'write':1,'network':1,'spawn':1})
            accepted,_=listener.accept();accepted.close();created.unlink()
            policy=adb_client_sandbox(binary,self.work,self.server.path)
            checked=subprocess.run(['/usr/bin/sandbox-exec','-p',policy,*arguments],
                stdin=subprocess.DEVNULL,capture_output=True,timeout=5)
            self.assertEqual(checked.returncode,0)
            self.assertEqual(json.loads(checked.stdout),{'read':0,'write':0,'network':0,'spawn':0})
            self.assertFalse(created.exists())

    def test_gateway_close_collects_a_client_that_never_finishes_its_header(self):
        from reproloop.adb_endpoint import AdbEndpoint,AdbGateway
        gateway=AdbGateway(AdbEndpoint(self.server.path),'owned-device');gateway.__enter__()
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.connect(str(gateway.socket_path));connection.sendall(b'00')
            started=time.monotonic();gateway.close()
        self.assertLess(time.monotonic()-started,1)
        self.assertFalse(gateway.socket_path.exists())

    def test_helper_http_uses_selected_transport_without_creating_forwarded_host_ports(self):
        client=self.client()
        result=client.call_helper('/status',None,token='owned-helper-token',timeout=2,
            cancellation=threading.Event(),deadline_monotonic=time.monotonic()+5)
        self.assertEqual(result,{'alive':True})
        self.assertIn(b'tcp:8766',self.server.requests)
        self.assertFalse(any(b'forward' in item for item in self.server.requests))

    def test_pinned_device_uses_scoped_sdk_and_helper_bridge_for_managed_instrumentation(self):
        from reproloop.adb_endpoint import AdbEndpoint
        from reproloop.repair_android import AndroidMobileTools,PinnedAdbDevice
        from reproloop.live.android_live import UsbBridgeClient,HELPER
        aapt=ADB.parent.parent/'build-tools/36.0.0/aapt2'
        tools=AndroidMobileTools(ADB,sha(ADB),aapt,sha(aapt))
        endpoint=AdbEndpoint(self.server.path,sandbox_sha256=sha('/usr/bin/sandbox-exec'))
        device=PinnedAdbDevice('owned-device',tools,package='com.example.app',work_root=self.work,
            cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10,endpoint=endpoint)
        self.addCleanup(lambda:device.close(deadline_monotonic=time.monotonic()+5))
        self.assertTrue(device.scoped_endpoint_enabled)
        bridge=UsbBridgeClient(None,'owned-helper-token',_device=device)
        self.assertEqual(bridge.call('/status'),{'alive':True})
        process=device.start_live_instrumentation(HELPER)
        self.assertTrue(device.collect_live_instrumentation(process))
        self.assertTrue(device.effects_settled)
        self.assertFalse(any(b'forward' in request or b'kill' in request for request in self.server.requests))

    def test_client_close_honors_deadline_and_can_retry_gateway_collection(self):
        client=self.client();gateway=client._gateway()
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.connect(str(gateway.socket_path));connection.sendall(b'00')
            started=time.monotonic();client.close(deadline_monotonic=started)
            self.assertLess(time.monotonic()-started,.3)
        self.assertTrue(client.close(deadline_monotonic=time.monotonic()+3))
        self.assertEqual(client.active_processes,0)

    def test_gateway_start_failure_removes_its_owned_socket_directory(self):
        from unittest.mock import patch
        from reproloop.adb_endpoint import AdbEndpoint,AdbGateway
        gateway=AdbGateway(AdbEndpoint(self.server.path),'owned-device')
        try:
            with patch('reproloop.adb_endpoint.threading.Thread.start',side_effect=RuntimeError('owned startup failure')):
                with self.assertRaises(RuntimeError):gateway.__enter__()
            self.assertFalse(gateway.socket_path.parent.exists())
        finally:gateway.close()

    def test_actual_sdk_streams_selected_apk_bytes_through_the_gateway(self):
        apk=self.work/'candidate.apk';apk.write_bytes(b'owned-apk-bytes')
        result=self.run_client(self.client(),('install','-r','-t',str(apk)))
        self.assertEqual(result.returncode,0)
        self.assertIn(b'Success',result.stdout)
        self.assertEqual(self.server.installed_bytes,apk.read_bytes())
