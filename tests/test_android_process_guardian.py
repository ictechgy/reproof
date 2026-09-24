"""Actual native guardian processes retaining original producer/device locks."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import tempfile
import threading
import time
import unittest

from reproof.storage import Lease

SDK=Path(os.environ.get('MACOSX_SDK_PATH') or subprocess.run(
    ['xcrun','--sdk','macosx','--show-sdk-path'],capture_output=True,text=True,check=True
).stdout.strip())
ROOT=Path(__file__).resolve().parents[1]
def sha(body):return hashlib.sha256(body).hexdigest()


class AndroidProcessGuardianTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary=tempfile.TemporaryDirectory(prefix='owned-android-guardian-');cls.addClassCleanup(temporary.cleanup)
        cls.tools=Path(temporary.name).resolve();cls.guardian=cls.tools/'guardian';cls.adb=cls.tools/'adb'
        result=subprocess.run(['/usr/bin/clang','-std=c11','-fblocks','-Wall','-Wextra','-Werror','-isysroot',str(SDK),
            str(ROOT/'native/android-process-guardian/main.c'),'-framework','CoreFoundation','-o',str(cls.guardian)],
            stdin=subprocess.DEVNULL,capture_output=True,timeout=20)
        if result.returncode:raise RuntimeError(result.stderr.decode())
        source=cls.tools/'client.c'
        source.write_text('#include <stdio.h>\n#include <string.h>\n#include <unistd.h>\n'
            'int main(int argc,char **argv){FILE *p=fopen("child.pid","w");if(!p)return 2;fprintf(p,"%d",getpid());fclose(p);'
            'if(argc==4&&!strcmp(argv[1],"dump")){FILE *f=fopen(argv[3],"r");int hold=f&&fgetc(f)==104;if(f)fclose(f);'
            'if(hold){alarm(10);for(;;)pause();}}'
            'if(argc>6&&!strcmp(argv[6],"hold")){alarm(10);for(;;)pause();}puts("owned client output");return 0;}\n')
        result=subprocess.run(['/usr/bin/clang','-Wall','-Wextra','-Werror',str(source),'-o',str(cls.adb)],
            stdin=subprocess.DEVNULL,capture_output=True,timeout=15)
        if result.returncode:raise RuntimeError('owned client probe compilation failed')

    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='an-',dir='/private/tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.operation=self.root/'operation';self.operation.mkdir(mode=0o700)
        self.staging=self.operation/'staging';self.staging.mkdir(mode=0o700)
        self.work=self.root/'control';self.work.mkdir(mode=0o700)
        self.lease=Lease('owned-guardian-'+self.root.name,self.root/'leases');self.lease.__enter__()
        self.addCleanup(lambda:self.lease.__exit__(None,None,None))
        self.producer=os.open(self.operation/'producer.lock',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
        fcntl.flock(self.producer,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.addCleanup(lambda:os.close(self.producer) if self.producer is not None else None)
        self.process=None;self.descriptors=[];self.writer=None
        self.addCleanup(self.collect)

    def collect(self):
        if self.writer is not None:os.close(self.writer);self.writer=None
        if self.process is not None:
            if self.process.poll() is None:
                try:os.kill(self.process.pid,signal.SIGCONT)
                except ProcessLookupError:pass
                try:self.process.wait(timeout=4)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
            self.process.stdout.close();self.process.stderr.close()
        for descriptor in self.descriptors:os.close(descriptor)
        self.descriptors=[]

    def launch(self,command='done',gateway=None,controller_exit=False,inspector=False,inspector_operation='dump',
               inspector_apk=None,policy=None,**changes):
        from reproof.adb_endpoint import adb_client_sandbox
        endpoint=self.root/'unused.sock' if gateway is None else gateway.socket_path
        if policy is None:
            policy='(version 1)(allow default)(deny network*)' if gateway is None else adb_client_sandbox(self.adb,self.staging,endpoint)
        arguments=['/usr/bin/sandbox-exec','-p',policy,str(self.adb),
            '-L','localfilesystem:'+str(endpoint),'-s','owned-device','shell',command]
        if inspector:
            apk=self.staging/'candidate.apk';apk.write_bytes(command.encode() if inspector_apk is None else inspector_apk);apk.chmod(0o600)
            arguments=['/usr/bin/sandbox-exec','-p',policy,str(self.adb),inspector_operation,'badging',str(apk)]
        encoded=plistlib.dumps(arguments,fmt=plistlib.FMT_BINARY)
        value={'schemaVersion':'1','operationId':'owned-operation','requestDigest':'a'*64,'contextDigest':'b'*64,
            'scopeDigest':self.lease.key,'definitionDigest':'c'*64,'workPath':str(self.work),
            'commandDigest':sha(encoded),'childWorkPath':str(self.staging),'maxOutputBytes':'1048576',
            'nativeBindingDigest':'d'*64,'ownershipGeneration':'1','hostIncarnation':'owned_host',
            'helperIncarnation':'owned_helper','adbPath':str(self.adb),'adbSha256':sha(self.adb.read_bytes()),
            'sandboxSha256':sha(Path('/usr/bin/sandbox-exec').read_bytes()),'stdinDigest':sha(b'')}
        if inspector:
            value.update(toolKind='apk-inspector',packageInspectorPath=str(self.adb),
                packageInspectorSha256=sha(self.adb.read_bytes()),packageInspectorSupport=[],
                apkPath=str(apk),apkSha256=sha(apk.read_bytes()),apkBytes=str(apk.stat().st_size))
        value.update(changes)
        for name,body in (('request.plist',plistlib.dumps(value,fmt=plistlib.FMT_BINARY)),('command.plist',encoded),
                          ('stdin.bin',b''),('start.json',b''),('termination.json',b''),('native-result.plist',b'')):
            path=self.work/name;path.write_bytes(body);path.chmod(0o600)
        def opened(path,flags):
            descriptor=os.open(path,flags|os.O_NOFOLLOW);self.descriptors.append(descriptor);return descriptor
        reader,self.writer=os.pipe();self.descriptors.append(reader)
        fds=[opened(self.work/'request.plist',os.O_RDONLY),opened(self.work,os.O_RDONLY|os.O_DIRECTORY),
            self.producer,self.lease.file.fileno(),reader,opened(self.work/'command.plist',os.O_RDONLY),
            opened(self.work/'native-result.plist',os.O_RDWR),opened(self.work/'start.json',os.O_RDWR),
            opened(self.work/'termination.json',os.O_RDWR),opened(self.operation,os.O_RDONLY|os.O_DIRECTORY),
            opened(self.lease.directory,os.O_RDONLY|os.O_DIRECTORY),opened(self.work/'stdin.bin',os.O_RDONLY)]
        arguments=[str(self.guardian),*map(str,fds)];inherited=tuple(fds)
        if controller_exit:
            import sys
            child_code=('import os,subprocess,sys,time\nfrom pathlib import Path\n'
                'fds=tuple(map(int,sys.argv[3:]));p=subprocess.Popen([sys.argv[1],*sys.argv[3:]],pass_fds=fds,start_new_session=True)\n'
                'marker=Path(sys.argv[2]);end=time.monotonic()+3\n'
                'while time.monotonic()<end and not marker.exists() and p.poll() is None:time.sleep(.01)\n'
                'os._exit(73 if marker.exists() and p.poll() is None else 74)\n')
            arguments=[sys.executable,'-I','-c',child_code,str(self.guardian),str(self.staging/'child.pid'),*map(str,fds)]
            inherited=(*fds,self.writer)
        self.process=subprocess.Popen(arguments,pass_fds=inherited,start_new_session=True,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        return self.process

    def test_inspector_parent_exit_collects_its_child_before_releasing_original_locks(self):
        self.launch(command='hold',inspector=True,controller_exit=True)
        os.close(self.writer);self.writer=None
        os.close(self.producer);self.producer=None
        self.lease.__exit__(None,None,None)
        output,error=self.process.communicate(timeout=5)
        self.assertEqual((self.process.returncode,output,error),(73,b'',b''))
        child=int((self.staging/'child.pid').read_text())
        with self.assertRaises(ProcessLookupError):os.kill(child,0)
        for path in (self.operation/'producer.lock',self.lease.directory/(self.lease.key+'.lock')):
            probe=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
            try:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(probe)

    def test_inspector_only_accepts_the_fixed_dump_badging_operation(self):
        self.launch(inspector=True,inspector_operation='package')
        output,error=self.process.communicate(timeout=3)
        self.assertEqual((self.process.returncode,output,error),(64,b'',b''))
        self.assertFalse((self.staging/'child.pid').exists())

    def test_inspector_rejects_a_changed_apk_hash_before_child_spawn(self):
        self.launch(inspector=True,apkSha256='0'*64)
        output,error=self.process.communicate(timeout=3)
        self.assertEqual((self.process.returncode,output,error),(64,b'',b''))
        self.assertFalse((self.staging/'child.pid').exists())

    def child_pid(self):
        deadline=time.monotonic()+3;path=self.staging/'child.pid'
        while time.monotonic()<deadline and not path.exists() and self.process.poll() is None:time.sleep(.01)
        self.assertTrue(path.exists());return int(path.read_text())

    def test_normal_result_keeps_locks_until_ack_and_does_not_claim_device_cleanup(self):
        process=self.launch();self.child_pid()
        result=json.loads(process.stdout.readline());self.assertTrue(result['hostClientStopped'])
        self.assertFalse(result['deviceCleanupConfirmed']);self.assertIsNone(process.poll())
        payload=plistlib.loads((self.work/'native-result.plist').read_bytes())
        self.assertEqual(payload['stdout'],b'owned client output\n')
        os.write(self.writer,b'\x01');self.assertEqual(process.wait(timeout=3),0)

    def test_parent_pipe_loss_stops_and_reaps_a_real_client(self):
        process=self.launch('hold');child=self.child_pid()
        os.kill(child,0)
        os.close(self.writer);self.writer=None
        self.assertEqual(process.wait(timeout=3),75)
        with self.assertRaises(ProcessLookupError):os.kill(child,0)

    def test_stopped_guardian_retains_both_original_locks_after_parent_copies_close(self):
        process=self.launch('hold');self.child_pid();os.kill(process.pid,signal.SIGSTOP)
        os.close(self.producer);self.producer=None
        self.lease.__exit__(None,None,None)
        os.close(self.writer);self.writer=None
        for path in (self.operation/'producer.lock',self.lease.directory/(self.lease.key+'.lock')):
            descriptor=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
            try:
                with self.assertRaises(BlockingIOError):fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(descriptor)
        os.kill(process.pid,signal.SIGCONT);self.assertEqual(process.wait(timeout=3),75)
        for path in (self.operation/'producer.lock',self.lease.directory/(self.lease.key+'.lock')):
            descriptor=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
            try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(descriptor)

    def test_changed_tool_digest_is_rejected_before_client_spawn(self):
        process=self.launch(adbSha256='0'*64)
        self.assertEqual(process.wait(timeout=3),64)
        self.assertFalse((self.staging/'child.pid').exists())

    def test_actual_sdk_retains_original_locks_when_its_guardian_is_killed(self):
        from reproof.adb_endpoint import AdbEndpoint,AdbGateway
        from tests.test_adb_endpoint import OwnedAdbServer,ADB
        server=OwnedAdbServer(self.root);release=threading.Event();entered=threading.Event()
        original=server.request
        def held(connection):
            result=original(connection)
            if result==b'host:version':entered.set();release.wait(5)
            return result
        server.request=held
        gateway=AdbGateway(AdbEndpoint(server.path),'owned-device');gateway.__enter__()
        try:
            self.adb=ADB;process=self.launch(gateway=gateway)
            self.assertTrue(entered.wait(3));self.assertIsNone(process.poll())
            os.close(self.producer);self.producer=None;self.lease.__exit__(None,None,None)
            process.kill();self.assertEqual(process.wait(timeout=3),-signal.SIGKILL)
            paths=(self.operation/'producer.lock',self.lease.directory/(self.lease.key+'.lock'))
            for path in paths:
                probe=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
                try:
                    with self.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                finally:os.close(probe)
            release.set();gateway.close()
            deadline=time.monotonic()+3
            for path in paths:
                while True:
                    probe=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
                    try:
                        try:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                        except BlockingIOError:
                            self.assertLess(time.monotonic(),deadline);time.sleep(.01)
                    finally:os.close(probe)
        finally:
            release.set();gateway.close();server.close()

    def test_actual_python_parent_exit_collects_the_client_before_releasing_locks(self):
        process=self.launch('hold',controller_exit=True)
        os.close(self.writer);self.writer=None
        os.close(self.producer);self.producer=None;self.lease.__exit__(None,None,None)
        stdout,stderr=process.communicate(timeout=5)
        self.assertEqual(process.returncode,73);self.assertEqual((stdout,stderr),(b'',b''))
        child=int((self.staging/'child.pid').read_text())
        with self.assertRaises(ProcessLookupError):os.kill(child,0)
        for path in (self.operation/'producer.lock',self.lease.directory/(self.lease.key+'.lock')):
            descriptor=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
            try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(descriptor)
