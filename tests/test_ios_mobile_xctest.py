"""Owned XCTest protocol fixtures; no Apple service or physical device."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from reproloop import contracts
from reproloop.execution.artifacts import BlobSet
from reproloop.ios_device_tools import IOSDeviceToolError
from reproloop.ios_mobile_operation import IOSMobileOperationStore
from tests import test_ios_device_guardian as guardians
from tests.test_ios_xctest_artifact import MACHO_BUNDLE


def xctest_lifetime_child(root, descendant=False):
    root=Path(root)
    case=IOSMobileXCTestTests(methodName='runTest');case.setUp()
    case.marker=root/'helper.json';case.release=root/'release'
    extra="import fcntl\n"+f"sentinel=open({str(root/'helper.lock')!r},'w');fcntl.flock(sentinel.fileno(),fcntl.LOCK_EX)"
    if descendant:
        extra += ("\nif os.fork()==0:\n"
            f"    held=open({str(root/'grandchild.lock')!r},'w');fcntl.flock(held.fileno(),fcntl.LOCK_EX)\n"
            "    while True:time.sleep(.01)")
    case.compile_tool(extra)
    case.tools=replace(case.tools,sha256=hashlib.sha256(case.tool.read_bytes()).hexdigest())
    selected=replace(case.operations.definition,xctest_definition_digest=case.tools.definition_digest)
    case.operations=IOSMobileOperationStore(case.g.c.runs,selected,case.root/'lifetime-operations')
    with case.owned() as (owner,runner):
        launch=case.prepare(runner);permit=case.permit(launch)
        session=case.start(runner,launch,permit,deadline=time.monotonic()+3)
        locks=[str(launch._work.parent/'producer.lock'),str(case.g.n.authority.lease_directory/(selected.scope_digest+'.lock'))]
        (root/'locks.json').write_text(json.dumps(locks))
        while session.poll():time.sleep(.01)
    os._exit(76)


def xctest_guardian_quarantine_child(root):
    """Run a Mach-O xcodebuild double until its guardian is killed by parent."""
    root=Path(root)
    case=IOSMobileXCTestTests(methodName='runTest');case.setUp()
    case.marker=root/'helper.json';case.release=root/'release'
    extra=("import fcntl,os\n"
        f"sentinel=open({str(root/'helper.lock')!r},'w');fcntl.flock(sentinel.fileno(),fcntl.LOCK_EX)\n"
        # The SDK double deliberately closes both streams while retaining its
        # lock, reproducing a guardian-only death that fools group-empty code.
        "os.close(1);os.close(2)")
    case.compile_tool(extra)
    case.tools=replace(case.tools,sha256=hashlib.sha256(case.tool.read_bytes()).hexdigest())
    selected=replace(case.operations.definition,xctest_definition_digest=case.tools.definition_digest)
    case.operations=IOSMobileOperationStore(case.g.c.runs,selected,case.root/'quarantine-operations')
    with case.owned() as (owner,runner):
        launch=case.prepare(runner);permit=case.permit(launch)
        session=case.start(runner,launch,permit,deadline=time.monotonic()+3)
        locks=[str(launch._work.parent/'producer.lock'),str(case.g.n.authority.lease_directory/(selected.scope_digest+'.lock'))]
        (root/'locks.json').write_text(json.dumps(locks))
        while not (root/'inspect').exists():time.sleep(.01)
        try:session.poll()
        except guardians.IOSDeviceToolError:pass
        try:
            second=case.prepare(runner,iteration=2);case.start(runner,second,case.permit(second))
            callback_blocked=False
        except guardians.IOSDeviceToolError:
            callback_blocked=True
        status={'active':session.active_processes,'public':session.public(),
            'close':session.close(deadline_monotonic=time.monotonic()+.2),
            'callbackBlocked':callback_blocked}
        (root/'status.json').write_text(json.dumps(status,sort_keys=True))
        while not (root/'release').exists():time.sleep(.01)
    os._exit(76)


class IOSMobileXCTestTests(unittest.TestCase):
    def setUp(self):
        from reproloop.ios_mobile_xctest import IOSXCTestTools
        from reproloop.ios_xctest_template import IOSXCTestTemplate
        self.g=guardians.IOSDeviceGuardianTests(methodName='runTest')
        self.addCleanup(self.g.doCleanups);self.g.setUp()
        self.root=self.g.c.root
        self.developer=self.root/'Developer';(self.developer/'usr/bin').mkdir(parents=True)
        self.tool=self.developer/'usr/bin/xcodebuild'
        self.marker=self.root/'xctest-marker.json';self.release=self.root/'xctest-release'
        self.compile_tool()
        template_path=Path(__file__).parent/'fixtures/ios-xctest-template/ReproLive_iphoneos.xctestrun'
        template=IOSXCTestTemplate(template_path.resolve(),hashlib.sha256(template_path.read_bytes()).hexdigest())
        self.tools=IOSXCTestTools(self.tool,hashlib.sha256(self.tool.read_bytes()).hexdigest(),
            self.developer,self.g.guardian(),template)
        fixture=guardians.native.preparation.fixtures.IOSArtifactTransferTests(methodName='runTest')
        self.addCleanup(fixture.doCleanups);fixture.setUp()
        bodies=[]
        for role,bundle in (('helper-host','io.reproloop.live.host'),('helper-runner','io.reproloop.live.tests.xctrunner')):
            app=fixture.make_flat_app()
            info=plistlib.loads((app/'Info.plist').read_bytes());info['CFBundleIdentifier']=bundle
            (app/'Info.plist').write_bytes(plistlib.dumps(info))
            if role=='helper-runner':
                test=app/'PlugIns/ReproLiveTests.xctest';test.mkdir(parents=True)
                info={'CFBundleIdentifier':'com.example.helper.tests','CFBundleExecutable':'ReproLiveTests',
                    'CFBundleVersion':'1','CFBundleShortVersionString':'1.0',
                    'ReproLiveProtocolVersion':2,'ReproLiveHelperVersion':2}
                (test/'Info.plist').write_bytes(plistlib.dumps(info))
                executable=test/'ReproLiveTests'
                executable.write_bytes(MACHO_BUNDLE);executable.chmod(0o700)
            bodies.append((role+'.ipa',fixture.make_ipa(app,fixture.root/(role+'.ipa')).read_bytes()))
        self.baselines=BlobSet((('original.ipa',self.g.c.body),*bodies))
        selected=replace(self.g.operations.definition,baseline_digest=self.baselines.digest,
            helper_bundles=(('helper-host','io.reproloop.live.host'),('helper-runner','io.reproloop.live.tests.xctrunner')),
            xctest_definition_digest=self.tools.definition_digest)
        self.operations=IOSMobileOperationStore(self.g.c.runs,selected,self.root/'xctest-operations')
        self.addCleanup(self.operations.close)

    def compile_tool(self, extra=''):
        body='''import json,os,pathlib,plistlib,sys,time
args=sys.argv[1:]
assert args[0]=='test-without-building'
config=pathlib.Path(args[args.index('-xctestrun')+1])
document=plistlib.loads(config.read_bytes())
target=document['ReproLiveTests']
assert 'candidate/App.app' not in target['TestHostPath']
assert 'helper-runner/App.app' in target['TestHostPath']
assert target['EnvironmentVariables']['REPRO_LIVE_PROTOCOL_VERSION']=='2'
work=pathlib.Path(os.environ['HOME']).parent
assert os.environ.get('OWNED_UNRELATED_VALUE') is None
assert json.loads((work/'state.json').read_bytes())['state']=='dispatching'
pathlib.Path(MARKER).write_text(json.dumps({'args':args,'testHost':target['TestHostPath'],'guardian':os.getppid()}))
EXTRA
while not pathlib.Path(RELEASE).exists():time.sleep(.01)
'''.replace('MARKER',repr(str(self.marker))).replace('RELEASE',repr(str(self.release))).replace('EXTRA',extra)
        source=self.root/'xcodebuild-double.c'
        source.write_text('#include <stdlib.h>\n#include <unistd.h>\nint main(int argc,char **argv){\n'
            'char **a=calloc((size_t)argc+3,sizeof(char*));if(!a)return 70;\n'
            'a[0]='+json.dumps(sys.executable)+';a[1]="-c";a[2]='+json.dumps(body)+';\n'
            'for(int i=1;i<argc;i++)a[i+2]=argv[i];execv(a[0],a);return 71;}\n')
        result=subprocess.run(['/usr/bin/clang','-Wall','-Wextra','-Werror',str(source),'-o',str(self.tool)],
            capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,'Owned XCTest double compilation failed')
        self.tool.chmod(0o700)

    @contextmanager
    def owned(self):
        from reproloop.ios_mobile_xctest import IOSXCTestRunner
        with self.operations.admit(self.g.c.context,self.g.c.artifacts,self.baselines) as operation:
            for role in self.operations._roles:
                self.operations.prepare(operation,role,cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10)
            with self.operations.native_owner(operation,self.g.n.device) as owner:
                runner=IOSXCTestRunner(self.tools,self.g.definition(),owner)
                self.addCleanup(runner.close)
                yield owner,runner

    def prepare(self,runner,iteration=1):
        return runner.prepare(role='candidate',iteration=iteration,
            application_id=self.g.c.selected.application_id,profile_digest='a'*64,
            actions=('tap','launch','terminate'),cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10)

    def permit(self,launch):
        device=self.g.n.device
        admission=device.admit_operation(operation_id='xctest-start',payload_digest=contracts.digest(launch.payload),
            session_id='owned-session',sequence=1)
        return device.prepare_dispatch(admission,provider_incarnation=launch.payload['providerIncarnation'])

    def start(self,runner,launch,permit,**changes):
        return runner.start(launch,permit=permit,cancellation=changes.get('cancellation',threading.Event()),
            deadline_monotonic=changes.get('deadline',time.monotonic()+8))

    def test_fixed_helper_launch_retains_original_locks_and_private_configuration(self):
        with self.owned() as (owner,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            pairs=[];original_pipe=os.pipe
            def pipe():
                pair=original_pipe();pairs.append(pair);return pair
            with patch('reproloop.ios_mobile_xctest.os.pipe',side_effect=pipe):
                session=self.start(runner,launch,permit)
            self.addCleanup(session.close)
            with self.assertRaises(OSError):os.fstat(pairs[1][0])
            self.g.wait_for(self.marker.exists)
            self.assertTrue(session.poll())
            report=session.public()
            self.assertEqual(report['nativeBindingDigest'],owner.binding_digest)
            self.assertFalse(report['deviceCleanupConfirmed'])
            self.assertFalse(report['installedArtifactVerified'])
            self.assertNotIn(self.g.c.selected.udid,json.dumps(report)+repr(launch)+repr(session))
            self.release.write_text('finish owned helper')
            self.assertTrue(session.wait(deadline_monotonic=time.monotonic()+5))
            self.assertEqual(runner.active_processes,0)
            self.assertGreater(self.g.c.runs.status(self.g.c.context.operation_id)['reservedBytes'],0)

    def test_missing_and_changed_permits_cannot_start_helper(self):
        with self.owned() as (_,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            for invalid in (None,replace(permit,payload_digest='0'*64),replace(permit,ownership_generation=permit.ownership_generation+1)):
                with self.assertRaises(IOSDeviceToolError):self.start(runner,launch,invalid)
            self.assertFalse(self.marker.exists())

    def test_cancelled_start_and_mutated_config_are_rejected_before_launch(self):
        with self.owned() as (_,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            cancel=threading.Event();cancel.set()
            with self.assertRaises(IOSDeviceToolError):self.start(runner,launch,permit,cancellation=cancel)
            launch._work.joinpath('session.xctestrun').write_bytes(b'changed')
            with self.assertRaises(IOSDeviceToolError):self.start(runner,launch,permit)
            self.assertFalse(self.marker.exists())

    def test_active_helper_blocks_second_start_and_owner_exit_reaps_it(self):
        with self.owned() as (_,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            session=self.start(runner,launch,permit)
            self.g.wait_for(self.marker.exists)
            with self.assertRaises(IOSDeviceToolError):self.start(runner,launch,permit)
        self.assertEqual(runner.active_processes,0)
        self.assertTrue(session.close())

    def test_copied_launch_and_changed_helper_cannot_dispatch(self):
        with self.owned() as (_,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            with self.assertRaises(IOSDeviceToolError):self.start(runner,replace(launch),permit)
            root=launch._work.parent/'helper-runner/App.app'
            (root/'changed.txt').write_bytes(b'changed helper')
            with self.assertRaises(IOSDeviceToolError):self.start(runner,launch,permit)
            self.assertFalse(self.marker.exists())

    def test_revocation_after_native_readiness_prevents_helper_spawn(self):
        with self.owned() as (_,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            original=runner._state
            def revoke(selected,before,after):
                original(selected,before,after)
                if after=='dispatching':self.g.n.device.revoke_dispatches()
            with patch.object(runner,'_state',side_effect=revoke):
                with self.assertRaises(IOSDeviceToolError):self.start(runner,launch,permit)
            self.assertEqual(runner.active_processes,0)
            self.assertFalse(self.marker.exists())

    def test_shutdown_from_another_thread_reaps_helper_and_releases_export(self):
        with self.owned() as (owner,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            session=self.start(runner,launch,permit);self.g.wait_for(self.marker.exists)
            results=[]
            worker=threading.Thread(target=lambda:results.append(runner.close()))
            worker.start();worker.join(5)
            self.assertFalse(worker.is_alive());self.assertEqual(results,[True])
            self.assertEqual(runner.active_processes,0)
            self.assertEqual(len(owner.operations._native_exports),0)
            self.assertTrue(session.close())

    def test_close_collects_terminal_guardian_after_delayed_output_threads(self):
        from reproloop.ios_mobile_xctest import _Collector
        release_collectors = threading.Event()
        class DelayedCollector(_Collector):
            def _read(self):
                super()._read()
                release_collectors.wait(10)
        with self.owned() as (owner, runner):
            launch = self.prepare(runner)
            with patch('reproloop.ios_mobile_xctest._Collector', DelayedCollector):
                session = self.start(runner, launch, self.permit(launch))
            self.addCleanup(session.close)
            self.addCleanup(release_collectors.set)
            self.g.wait_for(self.marker.exists)
            self.release.write_text('finish owned helper before delayed collection')
            self.assertEqual(session._process.wait(timeout=5), 0)
            observed = []
            close_processes = session._processes.close
            def close_after_exit(**kwargs):
                result = close_processes(**kwargs)
                observed.append(result)
                # The first observation is genuinely incomplete until these
                # real output threads have consumed EOF and returned.
                release_collectors.set()
                return result
            with patch.object(session._processes, 'close', side_effect=close_after_exit):
                closed = session.close(deadline_monotonic=time.monotonic()+5)
            self.assertFalse(observed[0])
            self.assertTrue(all(not item.thread.is_alive() for item in session._collectors))
            self.assertTrue(closed)
            self.assertEqual(session.active_processes, 0)
            self.assertEqual(len(owner.operations._native_exports), 0)
            self.assertTrue(owner._command_lock.acquire(blocking=False))
            owner._command_lock.release()

    def test_pipe_failure_closes_exports_and_releases_command_lock(self):
        with self.owned() as (owner,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            original=os.pipe;calls=0
            def pipe():
                nonlocal calls
                calls+=1
                if calls==2:raise OSError('owned pipe creation failure')
                return original()
            with patch('reproloop.ios_mobile_xctest.os.pipe',side_effect=pipe):
                with self.assertRaises(IOSDeviceToolError):self.start(runner,launch,permit)
            self.assertEqual(len(owner.operations._native_exports),0)
            self.assertTrue(owner._command_lock.acquire(blocking=False));owner._command_lock.release()
            self.assertEqual(runner.active_processes,0)

    def test_collector_start_failure_does_not_wedge_cleanup(self):
        with self.owned() as (owner,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            original=threading.Thread.start;calls=0
            def start(thread):
                nonlocal calls
                if thread.name=='repro-apksigner-output':
                    calls+=1
                    if calls==2:raise RuntimeError('owned collector creation failure')
                return original(thread)
            with patch.object(threading.Thread,'start',start):
                with self.assertRaises(IOSDeviceToolError):self.start(runner,launch,permit)
            self.assertEqual(len(owner.operations._native_exports),0)
            self.assertEqual(runner.active_processes,0)
            self.assertTrue(owner._command_lock.acquire(blocking=False));owner._command_lock.release()

    def test_generated_output_over_limit_is_discarded_after_stop_and_inputs_remain(self):
        with self.owned() as (owner,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            session=self.start(runner,launch,permit);self.g.wait_for(self.marker.exists)
            result=launch._work/'result.xcresult';result.mkdir()
            for index in range(70):
                with (result/str(index)).open('wb') as output:output.truncate(1024*1024)
            with self.assertRaises(IOSDeviceToolError):session.poll()
            self.assertEqual(runner.active_processes,0)
            self.assertFalse(result.exists())
            retained=sum(p.stat().st_size for p in launch._work.rglob('*') if p.is_file())
            self.assertLess(retained,64*1024*1024)
            self.assertTrue((launch._work/'stage.json').exists())
            self.assertTrue((launch._work.parent/'original/App.app').is_dir())
            self.assertTrue(session.close())
            self.assertGreater(owner.operation.run.store.status(owner.operation.context.operation_id)['reservedBytes'],retained)

    def test_output_cleanup_deadline_can_be_retried(self):
        with self.owned() as (_,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            session=self.start(runner,launch,permit);self.g.wait_for(self.marker.exists)
            result=launch._work/'result.xcresult';result.mkdir()
            for index in range(70):
                with (result/str(index)).open('wb') as output:output.truncate(1024*1024)
            started=time.monotonic()
            self.assertFalse(session.close(deadline_monotonic=started))
            self.assertLess(time.monotonic()-started,.3)
            self.assertTrue(session.close(deadline_monotonic=time.monotonic()+5))
            self.assertFalse(result.exists())

    def test_post_grant_output_symlink_is_rejected_before_helper_exec(self):
        with self.owned() as (_,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            foreign=self.root/'unrelated-output';foreign.mkdir()
            original=runner._state
            def replace_output(selected,before,after):
                original(selected,before,after)
                if after=='dispatching':(selected._work/'result.xcresult').symlink_to(foreign,target_is_directory=True)
            with patch.object(runner,'_state',side_effect=replace_output):
                session=self.start(runner,launch,permit)
                with self.assertRaises(IOSDeviceToolError):session.wait(deadline_monotonic=time.monotonic()+5)
            self.assertFalse(self.marker.exists())
            self.assertEqual(list(foreign.iterdir()),[])
            # Remove only the test-injected link, then permit host cleanup retry.
            (launch._work/'result.xcresult').unlink()
            self.assertTrue(session.close())

    def test_live_result_directory_churn_does_not_abort_bounded_helper(self):
        with self.owned() as (_,runner):
            launch=self.prepare(runner);permit=self.permit(launch)
            session=self.start(runner,launch,permit);self.g.wait_for(self.marker.exists)
            result=launch._work/'result.xcresult';result.mkdir()
            stop=threading.Event();errors=[]
            def churn():
                try:
                    while not stop.is_set():
                        first=result/'first';second=result/'second'
                        first.mkdir();(first/'data').write_bytes(b'owned small result')
                        first.rename(second);(second/'data').unlink();second.rmdir()
                except Exception as error:errors.append(type(error).__name__)
            worker=threading.Thread(target=churn);worker.start()
            try:
                for _ in range(40):self.assertTrue(session.poll());time.sleep(.005)
            finally:stop.set();worker.join(3)
            self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
            self.release.write_text('finish owned helper')
            self.assertTrue(session.wait(deadline_monotonic=time.monotonic()+5))

    def test_unsafe_or_disconnected_tunnel_does_not_prepare_a_launch(self):
        control=self.root/'tunnel-control.json';control.write_text('{}')
        extra="if args[2]=='details':result['connectionProperties'].update(json.loads(pathlib.Path("+repr(str(control))+").read_bytes()))"
        self.g.write_tool(extra)
        selected=replace(self.operations.definition,query_definition_digest=self.g.definition().definition_digest)
        self.operations=IOSMobileOperationStore(self.g.c.runs,selected,self.root/'tunnel-operations')
        self.addCleanup(self.operations.close)
        with self.owned() as (_,runner):
            for changes in ([{'tunnelIPAddress':address} for address in ('::','::1','ff02::1','2001:4860:4860::8888')]
                    +[{'tunnelState':'disconnected'},{'pairingState':'unpaired'},{'transportType':'wireless'}]):
                control.write_text(json.dumps(changes))
                with self.assertRaises(IOSDeviceToolError):self.prepare(runner)
            self.assertFalse(self.marker.exists())
            control.write_text('{}')
            self.assertEqual(self.prepare(runner).payload['role'],'candidate')

    def exercise_lifetime(self,kind):
        root=self.root/('lifetime-'+kind);root.mkdir(mode=0o700)
        descendant=kind.startswith('descendant')
        worker=multiprocessing.get_context('spawn').Process(target=xctest_lifetime_child,args=(root,descendant))
        try:
            worker.start()
            self.g.wait_for(lambda:all((root/name).is_file() and (root/name).stat().st_size>0
                for name in ('helper.json','locks.json')) and (root/'helper.lock').exists())
            locks=[Path(value) for value in json.loads((root/'locks.json').read_bytes())]
            if descendant:
                self.g.wait_for(lambda:(root/'grandchild.lock').exists() and not self.g.lock_available(root/'grandchild.lock'))
            self.assertTrue(all(not self.g.lock_available(path) for path in (*locks,root/'helper.lock')))
            if kind=='guardian':
                os.kill(json.loads((root/'helper.json').read_bytes())['guardian'],signal.SIGKILL)
                worker.kill();worker.join(3)
                self.assertTrue(all(not self.g.lock_available(path) for path in (*locks,root/'helper.lock')))
                (root/'release').write_text('finish owned helper')
            elif kind.endswith('deadline'):
                os.kill(worker.pid,signal.SIGSTOP)
                self.g.wait_for(lambda:self.g.lock_available(root/'helper.lock'))
                self.assertTrue(all(not self.g.lock_available(path) for path in locks))
                worker.kill();worker.join(3)
            else:
                worker.kill();worker.join(3)
            self.g.wait_for(lambda:all(self.g.lock_available(path) for path in (*locks,root/'helper.lock')))
            if descendant:self.g.wait_for(lambda:self.g.lock_available(root/'grandchild.lock'))
            self.assertTrue((locks[0].parent/'original/App.app').exists())
        finally:
            (root/'release').write_text('finish owned helper')
            if worker.is_alive():worker.kill();worker.join(3)
            worker.close()

    def test_parent_death_reaps_xctest_helper(self):self.exercise_lifetime('parent')

    def test_guardian_death_keeps_original_locks_in_helper(self):self.exercise_lifetime('guardian')

    def test_guardian_sigkill_quarantines_live_macho_session_until_native_recovery(self):
        root=self.root/'guardian-quarantine';root.mkdir(mode=0o700)
        worker=multiprocessing.get_context('spawn').Process(target=xctest_guardian_quarantine_child,args=(root,))
        try:
            worker.start()
            self.g.wait_for(lambda:all((root/name).is_file() and (root/name).stat().st_size>0
                for name in ('helper.json','locks.json')) and (root/'helper.lock').exists())
            locks=[Path(value) for value in json.loads((root/'locks.json').read_bytes())]
            self.assertTrue(all(not self.g.lock_available(path) for path in (*locks,root/'helper.lock')))
            os.kill(json.loads((root/'helper.json').read_bytes())['guardian'],signal.SIGKILL)
            (root/'inspect').write_text('inspect')
            self.g.wait_for(lambda:(root/'status.json').is_file() and (root/'status.json').stat().st_size>0)
            status=json.loads((root/'status.json').read_bytes())
            self.assertGreater(status['active'],0)
            self.assertFalse(status['public']['hostClientStopped'])
            self.assertFalse(status['close'])
            self.assertTrue(status['callbackBlocked'])
            self.assertTrue(all(not self.g.lock_available(path) for path in (*locks,root/'helper.lock')))
            (root/'release').write_text('finish owned helper')
            self.g.wait_for(lambda:all(self.g.lock_available(path) for path in (*locks,root/'helper.lock')))
        finally:
            (root/'release').write_text('finish owned helper')
            if worker.is_alive():worker.kill();worker.join(5)
            worker.close()

    def test_native_deadline_works_while_parent_is_suspended(self):self.exercise_lifetime('deadline')

    def test_parent_death_reaps_descendant_with_inherited_locks(self):self.exercise_lifetime('descendant-parent')

    def test_native_deadline_reaps_descendant_with_inherited_locks(self):self.exercise_lifetime('descendant-deadline')

    def fake_devicectl(self):
        fake=self.root/'fake-devicectl'
        fake.write_text('#!/bin/sh\nexec sleep 60\n');fake.chmod(0o700)
        return fake

    def group_members(self,pgid):
        return subprocess.run(('pgrep','-g',str(pgid)),capture_output=True,text=True).stdout.split()

    def test_tunnel_hold_watchdog_reaps_the_monitor_when_the_owner_pipe_dies(self):
        from reproloop.ios_mobile_xctest import _spawn_tunnel_hold
        hold=_spawn_tunnel_hold(self.fake_devicectl(),'00000000-0000000000000000',self.root,time.monotonic()+60)
        self.assertIsNotNone(hold)
        self.g.wait_for(lambda:len(self.group_members(hold.process.pid))==2)
        # Closing the last write end is exactly what runner death does.
        os.close(hold.live_write);hold.live_write=None
        self.assertEqual(hold.process.wait(timeout=6),0)
        self.assertEqual(self.group_members(hold.process.pid),[])

    def test_tunnel_hold_release_stops_the_monitor(self):
        from reproloop.ios_mobile_xctest import _spawn_tunnel_hold,_stop_tunnel_hold
        hold=_spawn_tunnel_hold(self.fake_devicectl(),'00000000-0000000000000000',self.root,time.monotonic()+60)
        self.assertIsNotNone(hold)
        self.g.wait_for(lambda:len(self.group_members(hold.process.pid))==2)
        _stop_tunnel_hold(hold)
        self.assertEqual(hold.process.wait(timeout=6),0)
        self.assertEqual(self.group_members(hold.process.pid),[])
