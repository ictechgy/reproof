"""Pinned APK inspection under the original Android native owner."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from reproof.execution.artifacts import BlobSet
from reproof.repair_android import PinnedAdbDevice
from reproof.repair_android_operation import AndroidOperationStore, AndroidOperationError
from tests import test_android_native_process as support

_SDK_HOME=Path(os.environ.get('ANDROID_HOME', Path.home()/'Library/Android/sdk'))
AAPT=Path(os.environ['AAPT_PATH']) if os.environ.get('AAPT_PATH') else \
    next(iter(sorted(_SDK_HOME.glob('build-tools/*/aapt'))), _SDK_HOME/'build-tools'/'aapt')
APK=Path(__file__).resolve().parents[1]/'artifacts/product-delivery/d1-android-package-r1/built/original.apk'


class AndroidNativeInspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AndroidNativeProcessTests.setUpClass()
        cls.addClassCleanup(support.AndroidNativeProcessTests.doClassCleanups)

    def setUp(self):
        self.f=support.AndroidNativeProcessTests(methodName='runTest')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.config=replace(self.f.config, tools=replace(self.f.config.tools,
            package_inspector=AAPT, package_inspector_digest=hashlib.sha256(AAPT.read_bytes()).hexdigest()))
        self.operations=AndroidOperationStore(self.f.fixture.runs,self.config,self.f.f.root/'inspector-operations')
        self.addCleanup(lambda:self.operations.close(deadline_monotonic=time.monotonic()+2))
        self.f.fixture.operations=self.operations;self.f.fixture.config=self.config
        self.body=APK.read_bytes()
        self.f.f.context=replace(self.f.f.context,artifact_digest=hashlib.sha256(self.body).hexdigest())
        self.context=self.f.f.context

    def test_actual_aapt_is_dispatched_natively_and_retired_without_a_gateway(self):
        from reproof.android_native_process import native_dispatcher
        from reproof.android_native_calls import validate_workspace
        with self.operations.admit(self.context,BlobSet((('candidate.apk',self.body),))) as operation:
            with self.f.fixture.phase(operation) as (phase,descriptors):
                with native_dispatcher(self.operations,descriptors,self.f.guardian()) as dispatcher:
                    device=PinnedAdbDevice(self.config.serial,self.config.tools,package=self.config.package,
                        work_root=operation.staging_root,cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic()+10,endpoint=self.config.adb_endpoint,
                        native_dispatcher=dispatcher)
                    self.addCleanup(lambda:device.close(deadline_monotonic=time.monotonic()+2))
                    before=len(self.f.fixture.server.requests)
                    with patch.object(device._owner,'run',side_effect=AssertionError('Plain inspector owner used')):
                        identity=device.apk_identity(operation.candidate_path)
                    self.assertEqual(identity,{'package':'com.example.reproinventory','versionCode':1})
                    self.assertEqual(len(self.f.fixture.server.requests),before)
                    self.assertTrue(device.effects_settled)
                    self.assertEqual(validate_workspace(descriptors.operation_directory_fd,
                        self.operations._intent(operation.operation_id)),'idle')
                    self.operations.complete_phase(phase,'a'*64)

    def test_unstaged_apk_is_rejected_before_inspector_spawn(self):
        from reproof.android_native_process import native_dispatcher
        with self.operations.admit(self.context,BlobSet((('candidate.apk',self.body),))) as operation:
            with self.f.fixture.phase(operation) as (phase,descriptors):
                with native_dispatcher(self.operations,descriptors,self.f.guardian()) as dispatcher:
                    with patch('subprocess.Popen',side_effect=AssertionError('Unstaged inspector spawned')):
                        with self.assertRaises(AndroidOperationError):
                            dispatcher.inspect_apk(APK,cancellation=threading.Event(),
                                deadline_monotonic=time.monotonic()+2)
                    self.operations.complete_phase(phase,'a'*64)

    def test_sdk_support_library_is_pinned_in_the_operation_definition(self):
        self.assertEqual(len(self.config.tools.inspector_support),1)
        path,digest=self.config.tools.inspector_support[0]
        self.assertEqual(path,AAPT.parent/'lib64/libc++.dylib')
        self.assertEqual(digest,hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(self.operations._configuration['packageInspectorSupportDigest'],
                         self.config.tools.inspector_support_digest)

    def test_inspector_os_policy_blocks_unselected_reads_writes_network_and_fork(self):
        from reproof.android_native_process import native_dispatcher
        root=self.f.f.root/'inspector-canary';self.assertFalse(root.exists());root.mkdir(mode=0o700)
        outside=root/'outside.txt';outside.write_text('owned inspector canary')
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
        self.addCleanup(listener.close);port=listener.getsockname()[1]
        source=root/'probe.c';tool=root/'inspector'
        source.write_text('#include <stdio.h>\n#include <fcntl.h>\n#include <unistd.h>\n'
            '#include <sys/socket.h>\n#include <sys/wait.h>\n#include <netinet/in.h>\n'
            'int main(int argc,char **argv){if(argc!=4)return 2;int fd=open(argv[3],O_RDONLY);int input=fd>=0;if(fd>=0)close(fd);'
            'fd=open('+json.dumps(str(outside))+',O_RDONLY);int outside=fd>=0;if(fd>=0)close(fd);'
            'char output[4096];snprintf(output,sizeof(output),"%s.probe",argv[3]);fd=open(output,O_WRONLY|O_CREAT,0600);'
            'int writeok=fd>=0;if(fd>=0)close(fd);'
            'struct sockaddr_in peer={.sin_family=AF_INET,.sin_port=htons('+str(port)+'),.sin_addr.s_addr=htonl(INADDR_LOOPBACK)};'
            'fd=socket(AF_INET,SOCK_STREAM,0);int network=fd>=0&&connect(fd,(struct sockaddr*)&peer,sizeof(peer))==0;if(fd>=0)close(fd);'
            'pid_t child=fork();if(child==0)_exit(0);int forkok=child>0;if(child>0)waitpid(child,0,0);'
            'printf("%d %d %d %d %d\\n",input,outside,writeok,network,forkok);return 0;}\n')
        built=subprocess.run(['/usr/bin/clang','-Wall','-Wextra','-Werror',str(source),'-o',str(tool)],
            stdin=subprocess.DEVNULL,capture_output=True,timeout=20)
        self.assertEqual(built.returncode,0,built.stderr.decode())
        baseline=root/'baseline.apk';baseline.write_bytes(self.body)
        positive=subprocess.run([str(tool),'dump','badging',str(baseline)],
            stdin=subprocess.DEVNULL,capture_output=True,timeout=3)
        self.assertEqual(positive.stdout.strip(),b'1 1 1 1 1')
        listener.settimeout(1);accepted,_=listener.accept();accepted.close()
        config=replace(self.config,tools=replace(self.config.tools,package_inspector=tool,
            package_inspector_digest=hashlib.sha256(tool.read_bytes()).hexdigest()))
        operations=AndroidOperationStore(self.f.fixture.runs,config,self.f.f.root/'inspector-canary-operations')
        self.addCleanup(lambda:operations.close(deadline_monotonic=time.monotonic()+2))
        self.f.fixture.operations=operations;self.f.fixture.config=config
        with operations.admit(self.context,BlobSet((('candidate.apk',self.body),))) as operation:
            with self.f.fixture.phase(operation) as (phase,descriptors):
                with native_dispatcher(operations,descriptors,self.f.guardian()) as dispatcher:
                    result=dispatcher.inspect_apk(operation.candidate_path,cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic()+5)
                    self.assertEqual(result.returncode,0)
                    self.assertEqual(result.stdout.strip(),b'1 0 0 0 0')
                    self.assertFalse(operation.candidate_path.with_suffix('.apk.probe').exists())
                    operations.complete_phase(phase,'a'*64)

    def test_changed_owned_support_copy_is_rejected(self):
        from reproof.device import DeviceError
        root=self.f.f.root/'inspector-copy';self.assertFalse(root.exists());root.mkdir(mode=0o700)
        tool=root/'aapt';tool.write_bytes(AAPT.read_bytes());tool.chmod(0o700)
        library=root/'lib64/libc++.dylib';library.parent.mkdir(mode=0o700)
        library.write_bytes((AAPT.parent/'lib64/libc++.dylib').read_bytes())
        tools=replace(self.config.tools,package_inspector=tool)
        library.write_bytes(b'owned changed library')
        with self.assertRaises(DeviceError):tools.verify()
