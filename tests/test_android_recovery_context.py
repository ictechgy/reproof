"""Synthetic Unix endpoints only. No ADB/server birth/device execution."""
import os
import io
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from reproof.adb_endpoint import AdbEndpoint
from reproof.core import ContractError
from reproof.live import android_recovery_context as context
from tests.test_adb_endpoint import OwnedAdbServer, frame


class PrivateContextContractTests(unittest.TestCase):
    def test_constructor_and_birth_never_accept_external_identity_or_have_effects(self):
        with patch.object(context.subprocess,'Popen') as popen, patch.object(context.socket,'socket') as sock, \
             patch.object(context.os,'mkdir') as mkdir:
            for make in (lambda:context.IsolatedAdbContext(),
                         lambda:context.IsolatedAdbContext.birth(),
                         lambda:context.IsolatedAdbContext.birth(pid=123,socket_path='/tmp/external',qualified=True)):
                with self.assertRaisesRegex(ContractError,'unqualified'):make()
            popen.assert_not_called();sock.assert_not_called();mkdir.assert_not_called()
        self.assertEqual(context._OWNED_BIRTHS,{})

    def test_bare_object_or_non_darwin_never_acquires_live_authority(self):
        candidate=object.__new__(context.IsolatedAdbContext)
        with patch.object(context.sys,'platform','linux'),self.assertRaises(ContractError):candidate._require_live()
        with patch.object(context.sys,'platform','darwin'),self.assertRaises(ContractError):candidate._require_live()

    def test_fixed_shell_policy_rejects_every_arbitrary_transport_or_command(self):
        package='io.reproof.fixture'
        allowed=[('getprop','ro.kernel.qemu'),('pidof','io.reproof.live'),
            ('am','force-stop','io.reproof.live'),('pm','path',package),('cat','/proc/123/stat'),
            ('cat','/proc/123/cmdline'),('test','!','-e','/proc/123'),
            ('sha256sum','/data/app/fixture/base.apk')]
        for args in allowed:self.assertTrue(context._shell_allowed(args,package))
        forbidden=[('install','app.apk'),('sync:',),('abb:',),('forward','tcp:0','tcp:8766'),
            ('am','force-stop','io.foreign.app'),('am','force-stop',package),('pm','clear',package),('getprop',),
            ('cat','/proc/self/stat'),('cat','/proc/123/../../secret'),('sh','-c','echo unsafe'),
            ('sha256sum','/data/app/../private/base.apk'),('pidof','io.reproof.live;id')]
        for args in forbidden:self.assertFalse(context._shell_allowed(args,package))

    def test_runner_receives_only_private_homes_and_bounded_output(self):
        candidate=object.__new__(context.IsolatedAdbContext)
        candidate._guard=Mock();candidate.work=Path('/private/work')
        candidate.home=Path('/private/home');candidate.android_user_home=Path('/private/android')
        candidate.deadline=time.monotonic()+30;candidate.cancellation=threading.Event()
        gateway=object();candidate._client=Mock()
        candidate._client.prepare_fixed.return_value=(('fixed','command'),gateway)
        candidate._client._owner.run.return_value=SimpleNamespace(
            terminated=True,bounded=True,interrupted=False,returncode=0,stdout=b'bounded')
        self.assertEqual(candidate._run_fixed('fixed',lambda:None),b'bounded')
        kwargs=candidate._client._owner.run.call_args.kwargs
        self.assertEqual(kwargs['private_homes'],(candidate.home,candidate.android_user_home))
        self.assertEqual(kwargs['max_output_bytes'],65536)
        candidate._client.finish_gateway.assert_called_once_with(gateway)
        self.assertEqual(candidate._guard.call_count,3)

    def test_shell_denial_precedes_any_guard_or_client_access(self):
        candidate=object.__new__(context.IsolatedAdbContext)
        candidate.target_package='io.reproof.fixture'
        candidate._run_fixed=Mock(side_effect=AssertionError('must not run'))
        with self.assertRaises(ContractError):candidate.run_shell(('pm','clear',candidate.target_package),lambda:None)
        with self.assertRaises(ContractError):candidate.run_shell(('pidof','io.reproof.live'),lambda:None,input_bytes=b'x')
        candidate._run_fixed.assert_not_called()

    def test_instrumentation_is_registered_before_main_thread_launch(self):
        candidate=object.__new__(context.IsolatedAdbContext)
        candidate._handles={};candidate._guard=Mock();candidate._require_handle=Mock()
        candidate.work=Path('/private/work');candidate.home=Path('/private/home')
        candidate.android_user_home=Path('/private/android');candidate._client=Mock()
        gateway=object();candidate._client.prepare_fixed.return_value=(('fixed','command'),gateway)
        process=Mock(pid=123,stdout=io.BytesIO(b''),stderr=io.BytesIO(b''))
        current=threading.get_ident()
        def launch(*args,**kwargs):
            self.assertEqual(threading.get_ident(),current)
            self.assertEqual(len(candidate._handles),1)
            self.assertTrue(next(iter(candidate._handles.values()))._launch_attempted)
            self.assertEqual(kwargs['env'],{'HOME':'private'})
            return process
        with patch.object(context,'_process_environment',return_value={'HOME':'private'}), \
             patch.object(context.subprocess,'Popen',side_effect=launch):
            handle=candidate.start_instrumentation(lambda:None)
        self.assertIs(handle._process,process)
        for collector in handle._collectors:collector.thread.join(1)
        self.assertTrue(all(not c.thread.is_alive() for c in handle._collectors))

    def test_failed_postlaunch_guard_retains_owned_handle_and_gateway(self):
        candidate=object.__new__(context.IsolatedAdbContext)
        candidate._handles={};candidate._require_handle=Mock()
        candidate._guard=Mock(side_effect=[None,None,None,ContractError('expired')])
        candidate.work=Path('/private/work');candidate.home=Path('/private/home')
        candidate.android_user_home=Path('/private/android');candidate._client=Mock()
        gateway=object();candidate._client.prepare_fixed.return_value=(('fixed','command'),gateway)
        process=Mock(pid=123)
        with patch.object(context,'_process_environment',return_value={}), \
             patch.object(context.subprocess,'Popen',return_value=process):
            with self.assertRaises(ContractError):candidate.start_instrumentation(lambda:None)
        self.assertTrue(candidate._failed)
        self.assertEqual(len(candidate._handles),1)
        retained=next(iter(candidate._handles.values()))
        self.assertIs(retained._process,process);self.assertIs(retained._gateway,gateway)
        candidate._client.finish_gateway.assert_not_called()

    def test_revocation_during_environment_validation_fences_launch(self):
        candidate=object.__new__(context.IsolatedAdbContext)
        candidate._handles={};candidate._require_handle=Mock();candidate._client=Mock()
        candidate._client.prepare_fixed.return_value=(('fixed','command'),object())
        candidate.work=Path('/private/work');candidate.home=Path('/private/home')
        candidate.android_user_home=Path('/private/android')
        revoked=[False]
        def guard(_):
            if revoked[0]:raise ContractError('revoked')
        candidate._guard=guard
        def environment(*args,**kwargs):revoked[0]=True;return {}
        with patch.object(context,'_process_environment',side_effect=environment), \
             patch.object(context.subprocess,'Popen') as launch:
            with self.assertRaises(ContractError):candidate.start_instrumentation(lambda:None)
            launch.assert_not_called()
        self.assertEqual(len(candidate._handles),1)

    def test_prelaunch_failure_never_spawns_or_loses_registered_owner(self):
        candidate=object.__new__(context.IsolatedAdbContext)
        candidate._handles={};candidate._require_handle=Mock()
        candidate._guard=Mock(side_effect=[None,ContractError('cancelled')])
        candidate._client=Mock();candidate._client.prepare_fixed.return_value=(('fixed','command'),object())
        with patch.object(context.subprocess,'Popen') as launch:
            with self.assertRaises(ContractError):candidate.start_instrumentation(lambda:None)
            launch.assert_not_called()
        self.assertEqual(len(candidate._handles),1)
        self.assertIsNone(next(iter(candidate._handles.values()))._process)

    def test_first_observed_foreign_node_never_becomes_cleanup_authority(self):
        # Model bind->replacement->first-stat without opening a socket. Even if
        # the recorded stamp was already foreign, cleanup cannot unlink it.
        with tempfile.TemporaryDirectory() as temporary:
            parent=Path(temporary).resolve();root=parent/'gateway';root.mkdir()
            foreign=root/'client.sock';foreign.write_bytes(b'foreign replacement')
            gateway=object.__new__(context._PrivateGateway)
            gateway._close_result=None;gateway._stop=threading.Event();gateway._thread=None
            gateway._lock=threading.RLock();gateway._sockets=set();gateway._workers=set()
            gateway._listener=Mock();gateway._socket_stamp=context._stamp(foreign.stat())
            gateway._parent_fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
            gateway._root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            with patch.object(context.os,'unlink',side_effect=AssertionError('must preserve')), \
                 patch.object(context.os,'rmdir',side_effect=AssertionError('must preserve')):
                with self.assertRaisesRegex(ContractError,'cleanup incomplete'):
                    gateway._close(time.monotonic()+1)
            self.assertEqual(foreign.read_bytes(),b'foreign replacement')
            self.assertTrue(gateway.workers_collected)
            self.assertTrue(gateway.cleanup_incomplete)

    def test_configuration_writer_has_no_caller_supplied_command_or_path(self):
        candidate=object.__new__(context.IsolatedAdbContext)
        candidate._run_fixed=Mock(return_value=b'')
        candidate.write_helper_configuration(b'{"bounded":true}',lambda:None)
        command=candidate._run_fixed.call_args.args[0]
        self.assertIn('run-as io.reproof.live',command)
        self.assertIn('files/live-config.json',command)
        self.assertEqual(candidate._run_fixed.call_args.kwargs['input_bytes'],b'{"bounded":true}')
        with self.assertRaises(ContractError):candidate.write_helper_configuration(b'x'*16385,lambda:None)


class PrivateGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='rq-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.server=OwnedAdbServer(self.root,serial='emulator-5554',concurrent=True)
        self.gateway_root=self.root/'g';self.gateway_root.mkdir(mode=0o700)
        self.fake=SimpleNamespace(endpoint=AdbEndpoint(self.server.path),serial='emulator-5554',
            gateway_root=self.gateway_root,_require_live=Mock())
        self.peer=patch('reproof.adb_endpoint._peer_uid',return_value=os.getuid())
        self.private_peer=patch.object(context,'_peer_uid',return_value=os.getuid())
        self.peer.start();self.private_peer.start();self.gateways=[]

    def tearDown(self):
        for gateway in self.gateways:
            try:gateway.close()
            except (OSError,RuntimeError,ValueError):pass
        self.private_peer.stop();self.peer.stop();self.server.close();self.temp.cleanup()

    def gateway(self,services=None):
        value=context._PrivateGateway(self.fake,services or context._services('getprop ro.kernel.qemu'))
        value.__enter__();self.gateways.append(value);return value

    def test_unproven_socket_origin_is_preserved_even_without_visible_mutation(self):
        gateway=self.gateway();root=gateway.socket_path.parent
        self.assertTrue(root.exists())
        for _ in range(2):
            with self.assertRaisesRegex(ContractError,'cleanup incomplete'):gateway.close()
        self.assertTrue(root.exists());self.assertTrue((root/'client.sock').exists())
        self.assertTrue(gateway.cleanup_incomplete);self.assertTrue(gateway.workers_collected)

    def test_foreign_child_preserves_all_private_namespace_and_reports_incomplete(self):
        gateway=self.gateway();root=gateway.socket_path.parent
        (root/'foreign').write_bytes(b'keep')
        with self.assertRaisesRegex(ContractError,'cleanup incomplete'):gateway.close()
        self.assertEqual((root/'foreign').read_bytes(),b'keep')
        self.assertTrue((root/'client.sock').exists());self.assertTrue(gateway.cleanup_incomplete)

    def test_same_path_directory_replacement_is_never_removed(self):
        gateway=self.gateway();root=gateway.socket_path.parent;old=root.with_name(root.name+'-old')
        root.rename(old);root.mkdir(mode=0o700);(root/'foreign').write_bytes(b'keep')
        with self.assertRaises(ContractError):gateway.close()
        self.assertEqual((root/'foreign').read_bytes(),b'keep')
        self.assertTrue((old/'client.sock').exists())

    def test_replaced_socket_is_never_unlinked(self):
        gateway=self.gateway();path=gateway.socket_path
        path.unlink();foreign=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            foreign.bind(str(path));path.chmod(0o600)
            with self.assertRaises(ContractError):gateway.close()
            self.assertTrue(path.exists())
        finally:foreign.close()

    def test_fixed_service_passes_but_sync_install_and_other_shell_do_not(self):
        gateway=self.gateway()
        for service,success in ((b'shell,v2,raw:getprop ro.kernel.qemu',True),
                                (b'sync:',False),(b'abb:package',False),
                                (b'shell,v2,raw:pm clear io.reproof.fixture',False),
                                (b'tcp:8766',False)):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(2);client.connect(str(gateway.socket_path))
                client.sendall(frame(b'host:tport:serial:emulator-5554'))
                self.assertEqual(OwnedAdbServer.read(client,4),b'OKAY');OwnedAdbServer.read(client,8)
                client.sendall(frame(service))
                self.assertEqual(OwnedAdbServer.read(client,4),b'OKAY' if success else b'FAIL')
                if success:
                    header=OwnedAdbServer.read(client,5);kind,count=struct.unpack('<BI',header)
                    self.assertEqual(kind,1);self.assertEqual(OwnedAdbServer.read(client,count),b'owned-output\n')
        commands=[x for x in self.server.requests if x.startswith((b'shell',b'sync',b'abb',b'tcp:'))]
        self.assertEqual(commands,[b'shell,v2,raw:getprop ro.kernel.qemu'])

    def test_daemon_control_foreign_selection_and_enumeration_never_reach_upstream(self):
        gateway=self.gateway()
        for service in (b'host:kill',b'host:devices',b'host:transport:other-device',b'host:connect:127.0.0.1:5555'):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(2);client.connect(str(gateway.socket_path));client.sendall(frame(service))
                self.assertEqual(OwnedAdbServer.read(client,4),b'FAIL')
        self.assertEqual(self.server.requests,[])
