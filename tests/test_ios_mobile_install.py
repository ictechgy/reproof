"""Fixed install protocol over owned Mach-O doubles; no Apple service or phone."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import signal
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

from reproof import contracts
from reproof.ios_device_tools import IOSDeviceToolError
from reproof.live.authority import ProviderResult
from tests import test_ios_device_guardian as guardians


def guarded_install_child(root, body, udid, tool, guardian_path):
    import hashlib
    from reproof.execution.artifacts import BlobSet
    from reproof.execution.journal import RunStore
    from reproof.ios_device_guardian import IOSDeviceGuardianTools
    from reproof.ios_device_tools import IOSDeviceTools, IOSDeviceQueryDefinition
    from reproof.ios_mobile_operation import IOSMobileOperationStore
    from reproof.live.authority import HostAuthority, issue_local_parent_grant
    root=Path(root);work=root/'queries';work.mkdir(mode=0o700)
    guardian=IOSDeviceGuardianTools(Path(guardian_path),hashlib.sha256(Path(guardian_path).read_bytes()).hexdigest())
    definition=IOSDeviceQueryDefinition(IOSDeviceTools(Path(tool),hashlib.sha256(Path(tool).read_bytes()).hexdigest()),
        guardians.queries.IDENTIFIER,udid,'com.example.flat',work,guardian)
    baselines=BlobSet((('original.ipa',body),))
    selected=replace(guardians.native.preparation.definition(udid,baselines),
        query_definition_digest=definition.definition_digest)
    runs=RunStore(root/'runs',environment_digest='9'*64,disk_limit=4*1024**3)
    operations=IOSMobileOperationStore(runs,selected,root/'operations')
    authority=HostAuthority(root/'authority/state.sqlite3',lease_directory=root/'device-leases')
    grant=issue_local_parent_grant(authority,lifetime_ns=600_000_000_000)
    device=authority.claim_device(device_kind='ios-physical',physical_id=udid,
        helper_incarnation='owned-install-helper',parent_grant=grant)
    with operations.admit(guardians.native.preparation.context(selected,body),
            BlobSet((('candidate.ipa',body),)),baselines) as operation:
        for role in ('candidate','original'):
            operations.prepare(operation,role,cancellation=threading.Event(),deadline_monotonic=time.monotonic()+10)
        with operations.native_owner(operation,device) as owner:
            installer=definition.open_installer(native_owner=owner)
            admission=device.admit_operation(operation_id='install-candidate',
                payload_digest=contracts.digest(installer.payload('install-candidate')),
                session_id='owned-install-session',sequence=1,
                deadline_ns=device._now()+2_000_000_000)
            permit=device.prepare_dispatch(admission,provider_incarnation='owned-installer')
            installer.run('install-candidate',permit=permit,cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+30)
    os._exit(76)


class IOSMobileInstallTests(unittest.TestCase):
    def setUp(self):
        self.g = guardians.IOSDeviceGuardianTests(methodName='runTest')
        self.addCleanup(self.g.doCleanups); self.g.setUp()
        self.g.q.script = self.g.q.script.replace('EXTRA', '''
if args[:3] == ['device','install','app']:
    app=pathlib.Path(args[5])
    record=json.loads((app.parent.parent/('command-'+KIND+'-work')/'state.json').read_bytes())
    assert record['state']=='dispatching'
    result={'installedApplications':[{'bundleID':BUNDLE}]}
EXTRA'''.replace('KIND', "('install-candidate' if app.parent.name=='candidate' else 'restore-original')"))
        self.configure()

    def configure(self, extra='pass'):
        from reproof.ios_mobile_operation import IOSMobileOperationStore
        self.g.write_tool(extra)
        selected = replace(self.g.c.selected, query_definition_digest=self.g.definition().definition_digest)
        self.g.operations = IOSMobileOperationStore(self.g.c.runs, selected,
            self.g.c.root/('install-operations-'+str(time.monotonic_ns())))
        self.addCleanup(self.g.operations.close)

    @contextmanager
    def installed_owner(self):
        with self.g.owned() as owner:
            installer = self.g.definition().open_installer(native_owner=owner)
            self.addCleanup(installer.close)
            yield owner, installer

    def permit(self, installer, kind='install-candidate', **changes):
        device = self.g.n.device
        admission = device.admit_operation(operation_id=kind,
            payload_digest=changes.get('payload_digest', contracts.digest(installer.payload(kind))),
            session_id='owned-install-session', sequence=1 if kind=='install-candidate' else 2,
            deadline_ns=changes.get('deadline_ns'))
        return device.prepare_dispatch(admission, provider_incarnation='owned-installer')

    def run_command(self, installer, permit, kind='install-candidate', **changes):
        return installer.run(kind, permit=permit,
            cancellation=changes.get('cancellation',threading.Event()),
            deadline_monotonic=changes.get('deadline',time.monotonic()+8))

    def record(self, kind='install-candidate'):
        root=self.g.operations.operations/self.g.c.context.operation_id
        return root/('command-'+kind+'-work')/'state.json'

    def calls(self):
        path=self.g.q.requests
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    def test_install_and_original_restore_use_fixed_role_and_journal_before_effect(self):
        from reproof.execution.artifacts import BlobSet
        stream=io.BytesIO(self.g.c.body)
        with zipfile.ZipFile(stream,'a') as archive:
            guardians.native.preparation.fixtures._zip_file(archive,
                'Payload/Flat1.app/candidate-only.txt',b'owned candidate change')
        candidate=stream.getvalue()
        self.g.c.artifacts=BlobSet((('candidate.ipa',candidate),))
        self.g.c.context=replace(self.g.c.context,artifact_digest=hashlib.sha256(candidate).hexdigest())
        with self.installed_owner() as (owner, installer):
            self.assertNotEqual(installer.payload('install-candidate')['appDigest'],
                installer.payload('restore-original')['appDigest'])
            for kind,role in (('install-candidate','candidate'),('restore-original','original')):
                permit=self.permit(installer,kind)
                result=self.run_command(installer,permit,kind)
                public=result.public()
                self.assertEqual(public['command'],kind)
                self.assertEqual(public['nativeBindingDigest'],owner.binding_digest)
                self.assertTrue(public['toolReportedSuccess'])
                self.assertFalse(public['deviceCleanupConfirmed'])
                self.assertFalse(public['installedArtifactVerified'])
                self.assertEqual(json.loads(self.record(kind).read_bytes())['state'],'tool-succeeded')
                call=self.calls()[-1]
                self.assertEqual(call[:5],['device','install','app','--device',guardians.queries.IDENTIFIER])
                self.assertEqual(call[5],str(self.record(kind).parent.parent/role/'App.app'))
                self.assertEqual(installer.active_processes,0)
                self.assertNotIn(self.g.c.selected.udid,json.dumps(public))
                self.assertNotIn(str(self.g.c.root),json.dumps(public))
                # Only the caller can settle this command receipt; it is not cleanup.
                self.g.n.device.confirm_operation(permit,ProviderResult('receipt-'+role,
                    'succeeded',contracts.digest(public)))
            self.assertGreater(self.g.c.runs.status(self.g.c.context.operation_id)['reservedBytes'],0)

    def test_missing_wrong_payload_and_foreign_generation_permits_cannot_launch(self):
        with self.installed_owner() as (_, installer):
            permit=self.permit(installer)
            for invalid in (None,replace(permit,payload_digest='0'*64),
                            replace(permit,ownership_generation=permit.ownership_generation+1),
                            replace(permit,deadline_ns=permit.deadline_ns+1)):
                with self.subTest(permit=type(invalid).__name__),self.assertRaises(IOSDeviceToolError):
                    self.run_command(installer,invalid)
            self.assertEqual(self.calls(),[])
            self.assertFalse(self.record().exists())

    def test_unrelated_permit_cannot_authorize_matching_command(self):
        with self.installed_owner() as (_, installer):
            permit=self.permit(installer,payload_digest='1'*64)
            with self.assertRaises(IOSDeviceToolError):self.run_command(installer,permit)
            self.assertEqual(self.calls(),[])

    def test_query_observation_and_closed_owner_cannot_authorize_install(self):
        with self.g.owned() as owner:
            query=self.g.client(owner)
            observation=query.query('details',cancellation=threading.Event(),deadline_monotonic=time.monotonic()+5)
            installer=self.g.definition().open_installer(native_owner=owner);self.addCleanup(installer.close)
            with self.assertRaises(IOSDeviceToolError):self.run_command(installer,observation)
        with self.assertRaises(IOSDeviceToolError):self.g.definition().open_installer(native_owner=owner)
        self.assertFalse(any(call[:3]==['device','install','app'] for call in self.calls()))

    def test_cancelled_expired_and_revoked_calls_preserve_files_without_dispatch(self):
        with self.installed_owner() as (_, installer):
            permit=self.permit(installer)
            cancel=threading.Event();cancel.set()
            for options in ({'cancellation':cancel},{'deadline':time.monotonic()-1}):
                with self.assertRaises(IOSDeviceToolError):self.run_command(installer,permit,**options)
            self.g.n.device.revoke_dispatches()
            with self.assertRaises(IOSDeviceToolError):self.run_command(installer,permit)
            self.assertFalse(self.record().exists())
            self.assertEqual(self.calls(),[])

    def test_changed_prepared_app_and_rewritten_state_cannot_replace_bound_input(self):
        from reproof.ios_artifact_transfer import parse_ios_artifact
        with self.installed_owner() as (_, installer):
            permit=self.permit(installer)
            root=self.record().parent.parent
            (root/'candidate/App.app/changed.txt').write_bytes(b'changed input')
            state=json.loads((root/'state.json').read_bytes())
            state['roles']['candidate']['appDigest']=parse_ios_artifact(root/'candidate/App.app').app_digest
            (root/'state.json').write_text(json.dumps(state))
            with self.assertRaises(IOSDeviceToolError):self.run_command(installer,permit)
            self.assertFalse(self.record().exists())
            self.assertEqual(self.calls(),[])

    def test_cancellation_in_flight_reaps_processes_and_retains_uncertain_command(self):
        self.configure("if args[:3]==['device','install','app']:time.sleep(30)")
        with self.installed_owner() as (_, installer):
            permit=self.permit(installer);cancel=threading.Event()
            def cancel_after_dispatch():
                deadline=time.monotonic()+5
                while time.monotonic()<deadline and not any(c[:3]==['device','install','app'] for c in self.calls()):
                    time.sleep(.01)
                cancel.set()
            worker=threading.Thread(target=cancel_after_dispatch);worker.start();self.addCleanup(worker.join)
            with self.assertRaises(IOSDeviceToolError):self.run_command(installer,permit,cancellation=cancel)
            self.assertEqual(installer.active_processes,0)
            self.assertEqual(json.loads(self.record().read_bytes())['state'],'dispatching')
            self.assertGreater(self.g.c.runs.status(self.g.c.context.operation_id)['reservedBytes'],0)
            with self.assertRaises(IOSDeviceToolError):self.run_command(installer,permit)
            self.assertEqual(sum(c[:3]==['device','install','app'] for c in self.calls()),1)

    def test_duplicate_success_cannot_repeat_mutation_even_with_new_client(self):
        with self.installed_owner() as (owner,installer):
            permit=self.permit(installer);self.run_command(installer,permit)
            other=self.g.definition().open_installer(native_owner=owner);self.addCleanup(other.close)
            with self.assertRaises(IOSDeviceToolError):self.run_command(other,permit)
            self.assertEqual(sum(c[:3]==['device','install','app'] for c in self.calls()),1)

    def test_moved_command_journal_cannot_enable_a_second_dispatch(self):
        with self.installed_owner() as (owner,installer):
            permit=self.permit(installer);self.run_command(installer,permit)
            directory=self.record().parent
            directory.rename(directory.with_name('retained-command'))
            other=self.g.definition().open_installer(native_owner=owner);self.addCleanup(other.close)
            with self.assertRaises(IOSDeviceToolError):self.run_command(other,permit)
            self.assertEqual(sum(c[:3]==['device','install','app'] for c in self.calls()),1)

    def test_native_ready_does_not_dispatch_after_permit_is_revoked(self):
        with self.installed_owner() as (_,installer):
            permit=self.permit(installer)
            transition=installer._transition
            def revoke(work,record,state):
                transition(work,record,state)
                if state=='dispatching':self.g.n.device.revoke_dispatches()
            with patch.object(installer,'_transition',side_effect=revoke):
                with self.assertRaises(IOSDeviceToolError):self.run_command(installer,permit)
            self.assertEqual(installer.active_processes,0)
            self.assertFalse(any(c[:3]==['device','install','app'] for c in self.calls()))
            self.assertEqual(json.loads(self.record().read_bytes())['state'],'dispatching')

    def test_run_cancellation_during_guardian_handshake_prevents_install(self):
        with self.installed_owner() as (_,installer):
            permit=self.permit(installer)
            transition=installer._transition
            def cancel(work,record,state):
                transition(work,record,state)
                if state=='dispatching':
                    self.g.c.runs.cancel(self.g.c.context.operation_id,self.g.c.context.request_digest)
            with patch.object(installer,'_transition',side_effect=cancel):
                with self.assertRaises(IOSDeviceToolError):self.run_command(installer,permit)
            self.assertFalse(any(c[:3]==['device','install','app'] for c in self.calls()))
            self.assertEqual(installer.active_processes,0)

    def test_actual_expired_permit_never_reaches_the_sdk(self):
        with self.installed_owner() as (_,installer):
            permit=self.permit(installer,deadline_ns=self.g.n.device._now()+50_000_000)
            time.sleep(.06)
            with self.assertRaises(IOSDeviceToolError):self.run_command(installer,permit)
            self.assertEqual(self.calls(),[])

    def test_wrong_device_stops_before_mutation_intent(self):
        self.configure("if args[:3]==['device','info','details']:result['hardwareProperties']['udid']='owned-foreign'")
        with self.installed_owner() as (_,installer):
            permit=self.permit(installer)
            with self.assertRaises(IOSDeviceToolError):self.run_command(installer,permit)
            self.assertEqual(len(self.calls()),1)
            self.assertFalse(self.record().exists())

    def test_wrong_bundle_acknowledgment_preserves_uncertain_state(self):
        self.configure("if args[:3]==['device','install','app']:result={'installedApplications':[{'bundleID':'com.example.foreign'}]}")
        with self.installed_owner() as (_,installer):
            permit=self.permit(installer)
            with self.assertRaises(IOSDeviceToolError):self.run_command(installer,permit)
            self.assertEqual(json.loads(self.record().read_bytes())['state'],'dispatching')
            self.assertEqual(self.g.n.device.status,'quarantined')

    def test_legacy_native_binding_remains_readable_without_install_authority(self):
        from reproof.repair_android_operation import _read_json_at, _replace_at
        with self.g.owned():pass
        with self.g.operations._directory(self.g.c.context.operation_id) as directory:
            record=_read_json_at(directory,'native.json')
            record['schemaVersion']=1;record.pop('preparedApps');record.pop('bindingDigest')
            record['bindingDigest']=contracts.digest(record)
            state=_read_json_at(directory,'state.json');state['nativeBindingDigest']=record['bindingDigest']
            _replace_at(directory,'native.json',record);_replace_at(directory,'state.json',state)
        status=self.g.operations.status(self.g.c.context.operation_id)
        self.assertEqual(status['nativeOwnership']['state'],'bound')
        self.assertEqual(status['executionAuthority'],'none')
        self.assertFalse(status['deviceCleanupConfirmed'])

    def exercise_native_lifetime(self,kind):
        root=self.g.c.root/('native-'+kind);root.mkdir(mode=0o700)
        marker=root/'live.json';release=root/'release';sentinel=root/'sdk.lock'
        udid=self.g.c.selected.udid+'-'+kind
        extra=("if args[:3]==['device','install','app']:\n"
            "    import fcntl\n"
            f"    sentinel=open({str(sentinel)!r},'w');fcntl.flock(sentinel.fileno(),fcntl.LOCK_EX)\n"
            f"    pathlib.Path({str(marker)!r}).write_text(json.dumps({{'guardian':os.getppid(),'child':os.getpid()}}))\n"
            f"    while not pathlib.Path({str(release)!r}).exists():time.sleep(.01)")
        self.g.write_tool(extra,udid=udid)
        worker=multiprocessing.get_context('spawn').Process(target=guarded_install_child,
            args=(root,self.g.c.body,udid,self.g.q.tool,self.g.guardian().path))
        try:
            worker.start();self.g.wait_for(lambda:marker.exists() and marker.stat().st_size>0)
            from reproof.live.authority import canonical_device_fingerprint
            locks=(root/'operations/operations/mobile-one/producer.lock',
                root/'device-leases'/(canonical_device_fingerprint('ios-physical',udid)+'.lock'))
            self.assertTrue(all(not self.g.lock_available(path) for path in (*locks,sentinel)))
            if kind=='parent':
                worker.kill();worker.join(3)
            elif kind=='guardian':
                os.kill(json.loads(marker.read_bytes())['guardian'],signal.SIGKILL)
                worker.kill();worker.join(3)
                self.assertTrue(all(not self.g.lock_available(path) for path in (*locks,sentinel)))
                release.write_text('finish owned SDK')
            else:
                # A suspended Python owner cannot poll cancellation. The native
                # deadline must still kill and reap its SDK child on its own.
                os.kill(worker.pid,signal.SIGSTOP)
                self.g.wait_for(lambda:self.g.lock_available(sentinel))
                self.assertTrue(all(not self.g.lock_available(path) for path in locks))
                worker.kill();worker.join(3)
            self.g.wait_for(lambda:all(self.g.lock_available(path) for path in (*locks,sentinel)))
            command=root/'operations/operations/mobile-one/command-install-candidate-work/state.json'
            self.assertEqual(json.loads(command.read_bytes())['state'],'dispatching')
            self.assertTrue((command.parent.parent/'original/App.app').is_dir())
        finally:
            release.write_text('finish owned SDK')
            if worker.is_alive():worker.kill();worker.join(5)
            worker.close()

    def test_parent_sigkill_reaps_install_child_and_retains_original(self):
        self.exercise_native_lifetime('parent')

    def test_guardian_sigkill_keeps_locks_in_install_child_until_exit(self):
        self.exercise_native_lifetime('guardian')

    def test_native_deadline_reaps_install_child_while_python_is_suspended(self):
        self.exercise_native_lifetime('deadline')
