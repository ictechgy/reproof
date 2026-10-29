"""Source-only disposable birth tests. No ADB, emulator, Mac, or key creation."""
from dataclasses import replace
from contextlib import contextmanager
import hashlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from reproof.core import ContractError
from reproof.live import android_recovery_birth as birth


class _Socket:
    def __init__(self, *args):
        self.closed = False
    def bind(self, address):
        if type(address) is str:
            Path(address).write_bytes(b'synthetic-listener')
    def listen(self, _):pass
    def settimeout(self, _):pass
    def getsockname(self):return ('127.0.0.1', 45678)
    def close(self):self.closed = True


class _Child:
    calls = []
    fail_second = False
    def __init__(self):self.process = None;self.collected = False
    def start(self, argv, environment, work, **kwargs):
        self.calls.append((argv, dict(environment), Path(work), kwargs))
        self.process = SimpleNamespace(pid=100+len(self.calls), poll=lambda:None)
        if 'nodaemon' in argv:
            selected = next(item for item in argv if item.startswith('localfilesystem:'))
            Path(selected.split(':', 1)[1]).write_bytes(b'synthetic-endpoint')
        if self.fail_second and len(self.calls) == 2:
            raise ContractError('synthetic partial launch')
    def bounded(self):return True
    def collect(self, _):self.collected = True;return True


class AndroidRecoveryBirthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rq-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.official_layout = dict(birth._AOSP_R2_FILE_SIZES)
        # Lifecycle tests exercise real small files, not a multi-GiB image.
        # The separate official-layout test retains every published size.
        properties = b'AndroidVersion.ApiLevel=36\nSystemImage.Abi=arm64-v8a\nSystemImage.TagId=default\nPkg.Revision=2\n'
        self.synthetic_layout = {name: len(properties) if name == 'source.properties' else len(b'synthetic')
                                 for name in self.official_layout}
        for name, value in (('_AOSP_R2_FILE_SIZES', self.synthetic_layout),
                            ('_AOSP_R2_EXPANDED_BYTES', sum(self.synthetic_layout.values()))):
            selected_patch = patch.object(birth, name, value)
            selected_patch.start()
            self.addCleanup(selected_patch.stop)
        self._owners_before = set(birth._OWNERS)
        self.addCleanup(self.remove_owners)

    def remove_owners(self):
        for key in set(birth._OWNERS)-self._owners_before:
            owner = birth._OWNERS.pop(key)
            for fd in owner._fds:
                try:birth.os.close(fd)
                except OSError:pass
            owner._fds.clear()

    def file(self, relative, raw=b'synthetic'):
        path = self.root/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return birth.PinnedFile(str(path), hashlib.sha256(raw).hexdigest(), len(raw))

    def plan(self):
        adb = self.file('tools/adb')
        adb = replace(adb, sha256=birth.ADB_DARWIN_SHA256)
        aapt = self.file('tools/aapt')
        emulator = self.file('emulator/emulator')
        qemu = self.file('emulator/qemu/qemu-system-aarch64')
        for item in (emulator, qemu):Path(item.path).chmod(0o700)
        image = [self.file('image/'+name) for name in self.synthetic_layout if name != 'source.properties']
        image.append(self.file('image/source.properties',
            b'AndroidVersion.ApiLevel=36\nSystemImage.Abi=arm64-v8a\nSystemImage.TagId=default\nPkg.Revision=2\n'))
        return birth.BirthPlan(str(self.root/'owned'), adb, aapt,
            birth.PinnedFile('/usr/bin/sandbox-exec', 'c'*64, 1),
            birth.PinnedTree(str(self.root/'emulator'), (emulator, qemu), (emulator.path, qemu.path)),
            birth.PinnedTree(str(self.root/'image'), tuple(image)),
            self.file('apks/sample.apk'), self.file('apks/helper.apk'))

    def test_planning_has_no_effects_and_declares_actual_future_side_effects(self):
        plan = self.plan()
        with patch.object(birth.subprocess, 'Popen') as process, patch.object(birth.socket, 'socket') as sock:
            selected = replace(plan, console_port=5560)
        process.assert_not_called();sock.assert_not_called()
        self.assertIn('private-adb-auth-keys-may-be-generated', selected.effects)
        self.assertIn('initial-install-io.reproof.sample', selected.effects)
        self.assertFalse(Path(plan.output).exists())

    def test_official_r2_declared_layout_and_bytes_without_seed_image(self):
        plan = self.plan()
        official = replace(plan.image, files=tuple(replace(item, size=self.official_layout[
            str(Path(item.path).relative_to(plan.image.root))]) for item in plan.image.files))
        with patch.object(birth, '_AOSP_R2_FILE_SIZES', self.official_layout), \
             patch.object(birth, '_AOSP_R2_EXPANDED_BYTES', 2_039_419_809), \
             patch.object(birth.subprocess, 'Popen') as process:
            selected = replace(plan, image=official)
            self.assertEqual(len(selected.image.files), 20)
            self.assertEqual(sum(item.size for item in selected.image.files), 2_039_419_809)
            self.assertIn('data/empty_data_disk', self.official_layout)
            self.assertNotIn('userdata.img', self.official_layout)
            changed = replace(official.files[0], size=official.files[0].size+1)
            with self.assertRaises(ContractError):
                replace(plan, image=replace(official, files=(changed,)+official.files[1:]))
            with self.assertRaises(ContractError):
                replace(plan, image=replace(official, files=official.files[:-1]))
        process.assert_not_called()

    def test_r2_rejects_all_imported_userdata_and_unlisted_paths(self):
        plan = self.plan()
        for name in ('userdata.img', 'userdata-qemu.img', 'snapshots.img',
                     'snapshots/default_boot/snapshot.pb', 'config.ini', 'adbkey', 'extra.txt'):
            with self.subTest(name=name):
                seed = self.file('image/'+name)
                with self.assertRaises(ContractError):
                    replace(plan, image=replace(plan.image, files=plan.image.files+(seed,)))

    def test_wrong_or_duplicate_revision_rejected_before_output_or_process(self):
        plan = self.plan()
        properties = Path(plan.image.root)/'source.properties'
        original = properties.read_bytes()
        for raw in (original.replace(b'Pkg.Revision=2', b'Pkg.Revision=1'),
                    original+b'Pkg.Revision=2\n', original+b'Pkg.Revision=3\n'):
            properties.write_bytes(raw)
            with patch.object(birth.sys, 'platform', 'darwin'), \
                 patch.object(birth, 'require_pinned_adb'), \
                 patch.object(birth.PinnedFile, 'verify'), \
                 patch.object(birth.PinnedTree, 'verify'), \
                 patch.object(birth.subprocess, 'Popen') as process:
                with self.assertRaises(ContractError):
                    birth.launch_qualification_birth(plan, cancellation=threading.Event())
                process.assert_not_called()
            self.assertFalse(Path(plan.output).exists())

    def test_no_arbitrary_process_socket_or_approval_import(self):
        for make in (lambda:birth.OwnedBirth(), lambda:birth.OwnedBirth({'pid':1}),
                     lambda:birth.launch_qualification_birth({'approved':True}, cancellation=threading.Event())):
            with self.assertRaises(ContractError):make()

    def test_fixed_release_paths_ports_and_data_seed_policy(self):
        plan = self.plan()
        for changes in ({'console_port':5037}, {'console_port':5555}, {'console_port':True},
                        {'lifetime_seconds':99999}, {'adb':replace(plan.adb, sha256='f'*64)},
                        {'output':str(self.root/'image'/'nested')}, {'output':str(self.root/'comma,output')},
                        {'output':str(self.root/('x'*80))}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):replace(plan, **changes)
        seed = self.file('image/userdata-qemu.img')
        with self.assertRaises(ContractError):replace(plan, image=replace(plan.image, files=plan.image.files+(seed,)))

    def test_closed_tree_rejects_extra_and_changed_files(self):
        plan = self.plan();plan.emulator.verify()
        extra = Path(plan.emulator.root)/'foreign';extra.write_bytes(b'not selected')
        with self.assertRaises(ContractError):plan.emulator.verify()
        extra.unlink();Path(plan.emulator.files[0].path).write_bytes(b'changed')
        with self.assertRaises(ContractError):plan.emulator.verify()

    def test_environment_is_allowlisted_private_and_discovery_disabled(self):
        with patch.dict(birth.os.environ, {'ADBHOST':'foreign','http_proxy':'secret','HOME':'/ambient'}):
            env = birth._environment('/private/home','/private/android','/private/avd','/private/work',45678)
        self.assertEqual([env[key] for key in ('ADB_USB','ADB_MDNS','ADB_EMU')], ['0']*3)
        self.assertEqual(env['ANDROID_ADB_SERVER_PORT'], '45678')
        self.assertEqual(env['HOME'], '/private/home')
        self.assertNotIn('ADBHOST', env);self.assertNotIn('http_proxy', env)

    def test_sandbox_no_foreign_adb_exec_or_global_network_exception(self):
        plan = self.plan();root = Path(plan.output)
        emu = birth._sandbox(plan,root,root/'server.sock',root/'report.sock',45678,emulator=True)
        server = birth._sandbox(plan,root,root/'server.sock',root/'report.sock',45678,emulator=False)
        self.assertIn('(deny network*)', emu);self.assertIn('(deny process-exec)', emu)
        self.assertNotIn(plan.adb.path, emu);self.assertNotIn('localhost:5037', emu+server)
        self.assertIn('localhost:45678', emu);self.assertIn('localhost:5555', server)
        self.assertIn(str(root/'report.sock'), emu)
        self.assertNotIn('remote ip "*', emu+server)

    def _launch(self, fail=False):
        plan = self.plan();actual_verify = birth.PinnedFile.verify
        _Child.calls = [];_Child.fail_second = fail
        def verify(selected, **kwargs):
            if selected.path == '/usr/bin/sandbox-exec':return Path(selected.path)
            return actual_verify(selected, **kwargs)
        def bootstrap(owner):
            owner._context = SimpleNamespace(_handles={}, _client=SimpleNamespace(close=Mock(return_value=True)), _closed=False)
            return owner._context
        def query(owner, service, **kwargs):
            if service == b'host:version':return b'0029'
            if service.startswith(b'host:emulator:'):return b''
            return ('emulator-'+str(owner.plan.console_port)+'\tdevice\n').encode()
        def shell(owner, command):
            if command == 'getprop ro.boot.qemu.avd_name':return owner.avd_name.encode()
            return b'1\n'
        with patch.object(birth.os, 'fstatvfs', return_value=SimpleNamespace(f_bavail=100*1024**3, f_frsize=1)), \
             patch.object(birth.sys,'platform','darwin'), patch.object(birth,'require_pinned_adb'), \
             patch.object(birth.PinnedFile,'verify',verify), patch.object(birth,'_Child',_Child), \
             patch.object(birth.socket,'socket',_Socket), \
             patch.object(birth,'_pin_server_socket',return_value=SimpleNamespace(_identity=('synthetic',))), \
             patch.object(birth,'_server_query',side_effect=query), patch.object(birth,'_accept_exact') as callback, \
             patch.object(birth,'_bootstrap_context',side_effect=bootstrap), \
             patch.object(birth,'_fixed_query',side_effect=shell), patch.object(birth,'_provision') as provision:
            if fail:
                with self.assertRaises(birth.BirthIncomplete) as caught:
                    birth.launch_qualification_birth(plan,cancellation=threading.Event())
                owner = caught.exception.owner
                self.assertFalse(owner._ready);provision.assert_not_called()
            else:
                owner = birth.launch_qualification_birth(plan,cancellation=threading.Event())
                self.assertTrue(owner._ready)
                self.assertEqual([call.args[1] for call in provision.call_args_list], ['io.reproof.sample','io.reproof.live'])
                self.assertEqual(callback.call_args_list[0].args[2], b'5554')
                self.assertTrue(callback.call_args_list[1].kwargs['framed'])
        return owner, plan

    def test_complete_synthetic_lifecycle_mints_context_only_after_provisioning(self):
        owner, plan = self._launch()
        self.assertIs(owner.context, owner._context)
        argv = _Child.calls[1][0]
        self.assertIn('-port', argv);self.assertNotIn('-ports', argv)
        self.assertEqual(argv[argv.index('-port')+1], '5554')
        self.assertNotIn('-wipe-data', argv);self.assertIn('-no-snapshot', argv)
        config = (Path(plan.output)/'avd'/(owner.avd_name+'.avd')/'config.ini').read_text()
        self.assertIn('disk.dataPartition.size=6144M', config)
        self.assertEqual(_Child.calls[0][0][-2:], ('server','nodaemon'))
        result = owner.collect()
        self.assertTrue(result['processesCollected']);self.assertTrue(result['privateDataRetained'])
        self.assertFalse(result['runtimeQualified']);self.assertTrue(Path(plan.output).exists())
        self.assertTrue(all(child.collected for child in owner._children))
        with self.assertRaises(ContractError):owner.context

    def test_sdk_anchor_and_sysdir_are_explicit_without_host_sdk_access(self):
        with patch.dict(birth.os.environ, {'ANDROID_HOME':'/foreign/sdk',
                                         'ANDROID_SDK_ROOT':'/foreign/other'}):
            owner, plan = self._launch()
        argv, environment, _, _ = _Child.calls[1]
        self.assertEqual(argv[argv.index('-sysdir')+1], plan.image.root)
        sdk = Path(plan.output)/'sdk'
        self.assertEqual(environment['ANDROID_HOME'], str(sdk))
        self.assertEqual(environment['ANDROID_SDK_ROOT'], str(sdk))
        self.assertNotIn('ANDROID_HOME', _Child.calls[0][1])
        self.assertEqual({p.name for p in sdk.iterdir()}, {'platforms','platform-tools'})
        self.assertEqual(list((sdk/'platforms').iterdir()), [])
        self.assertEqual(list((sdk/'platform-tools').iterdir()), [])
        self.assertIn('image.sysdir.1='+plan.image.root, birth._avd_config(owner))
        self.assertIn('disk.dataPartition.size=6144M', birth._avd_config(owner))
        self.assertNotIn('/foreign/', str(_Child.calls))
        policy = argv[2]
        self.assertNotIn('(subpath '+birth.json.dumps(str(sdk))+')', policy)
        self.assertNotIn(str(sdk/'platform-tools'/'adb'), str(_Child.calls))
        self.assertTrue(owner.collect()['processesCollected'])

    def _reject_prepared_change_before_process(self, change):
        prepare = birth._prepare_directories
        def changed(owner):
            prepare(owner)
            change(owner)
        with patch.object(birth, '_prepare_directories', changed):
            with self.assertRaises(birth.BirthIncomplete) as caught:
                self._launch()
        self.assertEqual(_Child.calls, [])
        self.assertEqual(caught.exception.owner._listeners, [])
        self.assertTrue(caught.exception.owner.abort()['processesCollected'])

    def test_foreign_sdk_tool_rejected_before_any_process_or_listener(self):
        self._reject_prepared_change_before_process(
            lambda owner: (owner.root/'sdk/platform-tools/adb').write_bytes(b'foreign'))

    def test_changed_avd_config_rejected_before_any_process_or_listener(self):
        self._reject_prepared_change_before_process(
            lambda owner: (owner.root/'avd'/(owner.avd_name+'.avd')/'config.ini').write_text(
                'image.sysdir.1=/foreign/image\n'))

    def test_replaced_sdk_directory_rejected_before_any_process_or_listener(self):
        def replace_directory(owner):
            selected = owner.root/'sdk/platforms'
            selected.rename(owner.root/'retained-platforms')
            selected.mkdir()
        self._reject_prepared_change_before_process(replace_directory)

    def test_incomplete_sdk_ownership_rejected_before_any_process_or_listener(self):
        self._reject_prepared_change_before_process(
            lambda owner: owner._sdk_fds.clear())

    def test_sdk_symlink_rejected_before_any_process_or_listener(self):
        def replace_directory(owner):
            selected = owner.root/'sdk/platform-tools'
            selected.rmdir()
            selected.symlink_to(owner.plan.emulator.root, target_is_directory=True)
        self._reject_prepared_change_before_process(replace_directory)

    def test_partial_child_start_retains_both_owners_and_never_mints_context(self):
        owner, _ = self._launch(fail=True)
        self.assertEqual(len(owner._children), 2)
        self.assertTrue(all(child.process is not None for child in owner._children))
        self.assertTrue(owner.collect()['processesCollected'])
        self.assertTrue(all(child.collected for child in owner._children))

    def test_wrong_inventory_or_avd_name_never_reaches_install(self):
        owner = SimpleNamespace(require_live=Mock(), plan=SimpleNamespace(console_port=5554), avd_name='unique')
        with patch.object(birth,'_server_query',return_value=b'emulator-5556\tdevice\n'), self.assertRaises(ContractError):
            birth._assert_avd(owner)
        with patch.object(birth,'_server_query',return_value=b'emulator-5554\tdevice\n'), \
             patch.object(birth,'_fixed_query',side_effect=[b'1',b'foreign']), self.assertRaises(ContractError):
            birth._assert_avd(owner)

    def test_callback_requires_exact_port_and_eof(self):
        class Connection:
            def __init__(self, data):self.data=data
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def settimeout(self,_):pass
            def recv(self,count):result=self.data[:count];self.data=self.data[count:];return result
        owner = SimpleNamespace(require_live=Mock(), deadline=birth.time.monotonic()+30)
        for data in (b'5555',b'5554extra',b'555'):
            listener = Mock();listener.accept.return_value=(Connection(data),None)
            with self.subTest(data=data), self.assertRaises(ContractError):birth._accept_exact(owner,listener,b'5554')
        listener = Mock();listener.accept.return_value=(Connection(b'5554'),None)
        birth._accept_exact(owner,listener,b'5554')

    def test_file_verification_cancellation_and_path_rebound(self):
        selected = self.file('large', b'x'*(2*1024*1024))
        cancellation = Mock()
        cancellation.is_set.side_effect = [False, False, True]
        with self.assertRaisesRegex(ContractError, 'cancelled'):
            selected.verify(cancellation=cancellation)
        actual_open = birth.open_regular
        calls = []
        def replacing(parent, name):
            calls.append(name)
            if len(calls) == 2:
                path = Path(parent)/name
                path.rename(path.with_suffix('.old'))
                path.write_bytes(b'x'*(2*1024*1024))
            return actual_open(parent, name)
        with patch.object(birth, 'open_regular', replacing), self.assertRaises(ContractError):
            selected.verify()

    def test_tree_scanner_rejects_before_materializing_unbounded_entries(self):
        plan = self.plan()
        consumed = []
        def entries():
            for index in range(100000):
                consumed.append(index)
                yield SimpleNamespace(name=str(index), stat=lambda **kwargs:SimpleNamespace(st_mode=birth.stat.S_IFREG, st_nlink=1))
        @contextmanager
        def scan(fd):
            yield entries()
        with patch.object(birth.os, 'scandir', scan), self.assertRaises(ContractError):
            plan.emulator.verify()
        self.assertEqual(len(consumed), 4097)

    def test_actual_parent_alias_to_input_rejected_before_mkdir(self):
        plan = self.plan()
        owner = SimpleNamespace(plan=plan, root=Path(plan.output), _fds=[], avd_name='unique')
        actual_directory = birth._directory
        def alias(path):
            if Path(path) == owner.root.parent:
                return actual_directory(plan.image.root)
            return actual_directory(path)
        try:
            with patch.object(birth, '_directory', alias), patch.object(birth.os, 'mkdir') as mkdir, self.assertRaises(ContractError):
                birth._prepare_directories(owner)
            mkdir.assert_not_called()
        finally:
            for fd in owner._fds:birth.os.close(fd)

    def test_native_handles_refuse_parent_collection_without_revoking_owner(self):
        owner, _ = self._launch()
        owner._context._handles = {'owned-helper':object()}
        with self.assertRaisesRegex(ContractError, 'native helper'):
            owner.collect()
        self.assertFalse(owner._closed)
        owner._context._handles = {}
        self.assertTrue(owner.collect()['processesCollected'])
        self.assertEqual(owner._fds, [])

    def test_non_darwin_or_cancellation_rejected_before_any_effect(self):
        plan = self.plan();cancel = threading.Event();cancel.set()
        with patch.object(birth.os,'mkdir') as mkdir, patch.object(birth.subprocess,'Popen') as process:
            with self.assertRaises(ContractError):birth.launch_qualification_birth(plan,cancellation=cancel)
            with patch.object(birth.sys,'platform','linux'), self.assertRaises(ContractError):
                birth.launch_qualification_birth(plan,cancellation=threading.Event())
        mkdir.assert_not_called();process.assert_not_called()

    def test_wide_tree_keeps_only_two_directory_handles_open(self):
        files = tuple(self.file('wide/d'+str(i)+'/f') for i in range(180))
        tree = birth.PinnedTree(str(self.root/'wide'), files)
        original_directory, original_close = birth._directory, birth.os.close
        live = set(); peak = []
        def directory(path):
            fd = original_directory(path);live.add(fd);peak.append(len(live));return fd
        def close(fd):
            live.discard(fd);return original_close(fd)
        with patch.object(birth, '_directory', directory), patch.object(birth.os, 'close', close):
            tree.verify()
        self.assertLessEqual(max(peak), 2);self.assertFalse(live)
        calls = []
        def failing(path):
            if calls:raise OSError('synthetic directory failure')
            fd = original_directory(path);calls.append(fd);return fd
        with patch.object(birth, '_directory', failing), self.assertRaises(OSError):tree.verify()
        with self.assertRaises(OSError):birth.os.fstat(calls[0])

    def test_service_rejected_before_socket_or_write(self):
        owner = SimpleNamespace(plan=SimpleNamespace(console_port=5554))
        with patch.object(birth.socket, 'socket') as create, self.assertRaises(ContractError):
            birth._server_query(owner, b'host:kill')
        create.assert_not_called()

    def test_response_deadline_prevents_slow_drip(self):
        connection = Mock();connection.recv.return_value=b'x'
        owner=SimpleNamespace(require_live=Mock(), deadline=100)
        with patch.object(birth.time, 'monotonic', side_effect=[1, 101]), self.assertRaises(ContractError):
            birth._read(connection, 2, owner=owner, deadline=100)
        self.assertEqual(connection.recv.call_count, 1)

    def test_disk_floor_denied_before_output_created(self):
        plan=self.plan();owner=SimpleNamespace(plan=plan, root=Path(plan.output), _fds=[], avd_name='unique')
        try:
            with patch.object(birth.os, 'fstatvfs', return_value=SimpleNamespace(f_bavail=1, f_frsize=1)), \
                 patch.object(birth.os, 'mkdir') as mkdir, self.assertRaisesRegex(ContractError, 'disk'):
                birth._prepare_directories(owner)
            mkdir.assert_not_called()
        finally:
            for fd in owner._fds:birth.os.close(fd)

    def test_actual_bootstrap_checks_owner_before_ready_and_after_death(self):
        from reproof.live import android_recovery_context as contexts
        owner, plan = self._launch()
        owner._ready=False
        process=object.__new__(birth.subprocess.Popen)
        process._child_created=False;process.pid=12345;process.poll=lambda:None
        owner.server=process;owner.server_pid=process.pid
        owner.endpoint=SimpleNamespace(verify=Mock(), _identity=('synthetic',))
        owner.socket_identity=owner.endpoint._identity
        hashes={Path(plan.adb.path):plan.adb.sha256, Path(plan.aapt.path):plan.aapt.sha256,
                Path('/usr/bin/sandbox-exec'):plan.sandbox.sha256}
        client=SimpleNamespace(close=Mock(return_value=True))
        with patch.object(birth.sys, 'platform', 'darwin'), patch.object(contexts, '_PrivateClient', return_value=client), \
             patch('reproof.ios_provisioning_cms._public_file_digest', side_effect=lambda p:hashes[p]):
            context=birth._bootstrap_context(owner)
            context._require_live()
            with self.assertRaises(ContractError):owner.context
            owner._ready=True
            self.assertIs(owner.context, context)
            contexts._OWNED_BIRTHS.pop(id(context))
            with self.assertRaises(ContractError):context._require_live()
            contexts._OWNED_BIRTHS[id(context)]=owner
            owner.emulator.poll=lambda:1
            with self.assertRaises(ContractError):context._require_live()
        self.assertTrue(owner.collect()['processesCollected'])

    def test_actual_provision_fixed_initial_install_and_retained_failure(self):
        owner, plan = self._launch()
        commands=[];starts=[]
        client=SimpleNamespace(prepare_fixed=lambda cmd:(commands.append(cmd) or
            (('/usr/bin/sandbox-exec','-p','(version 1)',plan.adb.path,'shell','-T',cmd), object())),
            finish_gateway=Mock(), close=Mock(return_value=True))
        owner._context=SimpleNamespace(_handles={}, _client=client,
            apk_identity=Mock(return_value={'package':'io.reproof.sample'}),
            home=owner.root/'home', android_user_home=owner.root/'android', work=owner.root/'client')
        class InstallChild:
            fail=False
            def __init__(self):
                self.process=None
                self.collectors=[SimpleNamespace(data=b'Success\n', thread=SimpleNamespace(join=Mock(), is_alive=lambda:False))]
            def start(self, argv, env, work, *, stdin):
                starts.append((argv, env, stdin.read()))
                self.process=SimpleNamespace(pid=3456, poll=lambda:0, returncode=0)
                if self.fail:raise ContractError('synthetic install interruption')
            def bounded(self):return True
            def collect(self, deadline):return True
        with patch.object(birth, '_assert_avd') as identity, patch.object(birth, '_Child', InstallChild), \
             patch.object(birth._ProcessOwner, '_group_empty', return_value=True):
            birth._provision(owner, 'io.reproof.sample', plan.sample_apk)
            self.assertEqual(identity.call_count, 2)
            self.assertEqual(commands, ['cmd package install -t -S '+str(plan.sample_apk.size)])
            self.assertNotIn(' -r ', commands[0]);self.assertEqual(starts[0][2], b'synthetic')
            self.assertIn(plan.sample_apk.path, starts[0][0][2])
            self.assertNotIn(plan.helper_apk.path, starts[0][0][2])
            owner._context.apk_identity.return_value={'package':'foreign.app'}
            with self.assertRaises(ContractError):birth._provision(owner, 'io.reproof.sample', plan.sample_apk)
            self.assertEqual(len(starts), 1)
            owner._context.apk_identity.return_value={'package':'io.reproof.sample'}
            InstallChild.fail=True
            with self.assertRaises(ContractError):birth._provision(owner, 'io.reproof.sample', plan.sample_apk)
            self.assertIsNotNone(owner._children[-1].process)
            owner.cancellation.set()
            with self.assertRaises(ContractError):birth._provision(owner, 'io.reproof.sample', plan.sample_apk)
            self.assertEqual(len(starts), 2)
        self.assertTrue(owner.collect()['processesCollected'])

    def _abort_context(self, owner, *, process=True):
        from reproof.live import android_recovery_context as contexts
        context=object.__new__(contexts.IsolatedAdbContext)
        context._birth=owner;context._pid=owner._pid;context._closed=False;context._failed=True
        context._client=SimpleNamespace(close=Mock(return_value=True))
        handle=contexts._OwnedInstrumentation(context, (), object())
        if process:
            child=object.__new__(birth.subprocess.Popen)
            child._child_created=False;child.pid=3456;child.poll=lambda:None
            child.stdout=Mock();child.stderr=Mock()
            handle._process=child
        context._handles={id(handle):handle}
        contexts._OWNED_BIRTHS[id(context)]=owner
        self.addCleanup(contexts._OWNED_BIRTHS.pop, id(context), None)
        owner._context=context
        return context,handle

    def test_abort_after_expiry_and_cancellation_collects_local_clients_only(self):
        owner,_=self._launch();context,handle=self._abort_context(owner)
        owner.deadline=0;owner.cancellation.set()
        with patch.object(birth._ProcessOwner, '_terminate', return_value=True) as terminate, \
             patch.object(birth, '_fixed_query') as query, patch.object(birth, '_server_query') as server:
            result=owner.abort()
        terminate.assert_called_once_with(handle._process, deadline_monotonic=unittest.mock.ANY)
        self.assertTrue(result['abortOnly']);self.assertTrue(result['processesCollected'])
        self.assertFalse(result['recoverySucceeded']);self.assertFalse(result['authorityReleased'])
        self.assertTrue(result['privateDataRetained']);self.assertFalse(context._handles)
        self.assertTrue(context._closed);self.assertEqual(owner._fds, [])
        query.assert_not_called();server.assert_not_called()
        self.assertTrue(all(child.collected for child in owner._children))

    def test_abort_partial_bootstrap_and_unlaunched_instrumentation(self):
        owner,_=self._launch(fail=True)
        self.assertTrue(owner.abort()['processesCollected'])
        # Separate owner uses the same fixture files but a fresh output directory.
        Path(owner.plan.output).rename(self.root/'retained-old')
        owner,_=self._launch();context,handle=self._abort_context(owner, process=False)
        del context._client
        with patch.object(birth._ProcessOwner, '_terminate') as terminate:
            result=owner.abort()
        terminate.assert_not_called();self.assertTrue(result['processesCollected'])
        self.assertFalse(context._handles)

    def test_abort_failure_still_collects_other_children_and_keeps_handles_for_retry(self):
        owner,_=self._launch();context,handle=self._abort_context(owner)
        context._client.close.side_effect=ContractError('synthetic gateway residue')
        with patch.object(birth._ProcessOwner, '_terminate', return_value=False):
            result=owner.abort()
        self.assertTrue(result['cleanupIncomplete']);self.assertFalse(result['processesCollected'])
        self.assertIs(context._handles[id(handle)],handle);self.assertTrue(owner._fds)
        self.assertTrue(all(child.collected for child in owner._children))
        context._client.close.side_effect=None;context._client.close.return_value=True
        with patch.object(birth._ProcessOwner, '_terminate', return_value=True):
            self.assertTrue(owner.abort()['processesCollected'])

    def test_abort_never_signals_reaped_pid_or_accepts_imported_handle(self):
        owner,_=self._launch();context,handle=self._abort_context(owner)
        handle._process.poll=lambda:0
        with patch.object(birth._ProcessOwner, '_group_empty', return_value=False), \
             patch.object(birth.os, 'killpg') as signal:
            self.assertFalse(owner.abort()['processesCollected'])
        signal.assert_not_called()
        context._handles={123:SimpleNamespace(pid=3456)}
        with patch.object(birth._ProcessOwner, '_terminate') as terminate, self.assertRaises(ContractError):
            owner.abort()
        terminate.assert_not_called()
        context._handles={}
        owner._pid=-1
        with self.assertRaises(ContractError):owner.abort()

    def test_abort_ambiguous_fd_close_is_never_retried_after_reuse(self):
        owner,_=self._launch(fail=True)
        selected=min(owner._fds);actual_close=birth.os.close
        def close(fd):
            actual_close(fd)
            if fd == selected:raise InterruptedError('synthetic uncertain close')
        with patch.object(birth.os, 'close', close):
            first=owner.abort()
        self.assertTrue(first['cleanupIncomplete']);self.assertFalse(owner._fds)
        foreign=birth.os.open(self.root/'foreign', birth.os.O_WRONLY|birth.os.O_CREAT, 0o600)
        try:
            self.assertEqual(foreign, selected)
            second=owner.abort()
            self.assertTrue(second['cleanupIncomplete'])
            birth.os.write(foreign, b'still-owned-by-caller')
        finally:actual_close(foreign)

    def test_successful_abort_is_idempotent(self):
        owner,_=self._launch(fail=True)
        self.assertEqual(owner.abort(), owner.abort())

    def test_normal_collect_close_exception_then_abort_never_reuses_fd(self):
        owner,_=self._launch();context,handle=self._abort_context(owner, process=False)
        context._handles.clear()
        selected=min(owner._fds);actual_close=birth.os.close
        def close(fd):
            actual_close(fd)
            if fd == selected:raise InterruptedError('synthetic close consumed descriptor')
        with patch.object(birth.os, 'close', close), self.assertRaises(InterruptedError):
            owner.collect()
        self.assertFalse(owner._fds)
        foreign=birth.os.open(self.root/'foreign-after-collect', birth.os.O_WRONLY|birth.os.O_CREAT, 0o600)
        try:
            self.assertEqual(foreign, selected)
            result=owner.abort()
            self.assertTrue(result['cleanupIncomplete']);self.assertFalse(result['processesCollected'])
            birth.os.write(foreign, b'not-a-birth-owned-descriptor')
            self.assertFalse(owner.collect()['processesCollected'])
        finally:actual_close(foreign)

    def test_normal_collect_child_failure_retains_context_for_abort(self):
        from reproof.live.android_recovery_context import _OWNED_BIRTHS
        owner,_=self._launch();context,handle=self._abort_context(owner, process=False)
        context._handles.clear()
        last=owner._children[-1]
        last.collect=Mock(side_effect=[ContractError('synthetic child collection failure'),True])
        with self.assertRaises(ContractError):owner.collect()
        self.assertIs(_OWNED_BIRTHS[id(context)], owner)
        self.assertTrue(context._closed);self.assertTrue(owner._fds)
        self.assertTrue(owner.abort()['processesCollected'])
        self.assertTrue(owner._children[0].collected)
        self.assertEqual(last.collect.call_count,2)

    def test_shared_darwin_runtime_expression_grants_reads_only(self):
        from reproof import adb_endpoint
        reads = adb_endpoint._darwin_runtime_reads()
        for name in ('/System/Cryptexes/OS', '/private/preboot/Cryptexes/OS',
                     '/System/Volumes/Preboot/Cryptexes/OS', '/dev/fd'):
            self.assertIn('(subpath '+birth.json.dumps(name)+')', reads)
        self.assertIn('(literal "/")', reads)
        for capability in ('network', 'file-write', 'process-exec', 'process-fork'):
            self.assertNotIn(capability, reads)
        denials = adb_endpoint._darwin_keychain_denials()
        self.assertIn('(deny file-read-data file-read-xattr ', denials)
        for root in ('', '/System/Volumes/Preboot/Cryptexes/OS',
                     '/System/Cryptexes/OS', '/private/preboot/Cryptexes/OS'):
            self.assertIn('(subpath '+birth.json.dumps(root+'/System/Library/Keychains')+')', denials)

    def test_producer_and_client_share_loader_reads_without_network_or_exec_widening(self):
        from reproof import adb_endpoint
        plan = self.plan()
        root = Path(plan.output)
        endpoint, callback = root/'server.sock', root/'report.sock'
        for emulator in (False, True):
            policy = birth._sandbox(plan, root, endpoint, callback, 42001, emulator=emulator)
            self.assertIn('(deny network*)(deny process-exec)', policy)
            self.assertIn(adb_endpoint._darwin_keychain_denials(), policy)
            for directory in adb_endpoint._DARWIN_RUNTIME_DIRECTORIES:
                self.assertIn('(subpath '+birth.json.dumps(directory)+')', policy)
            self.assertNotIn('(allow network*)', policy)
            self.assertNotIn('(allow process-exec (subpath', policy)
            self.assertNotIn('(allow file-write* (subpath "/System', policy)
        client = adb_endpoint.adb_client_sandbox(plan.adb.path, root/'client', endpoint)
        self.assertIn('(deny network*)(deny mach-lookup)(deny process-fork)', client)
        self.assertIn(adb_endpoint._darwin_keychain_denials(), client)
        self.assertIn('(allow network-outbound (literal '+birth.json.dumps(str(endpoint))+'))', client)
