"""Actual private Lab/authority flow with inert native transports; no device execution."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof.android_profile import validate_android_runtime_profile
from reproof.live.authority import HostAuthority, ProviderResult, HELPER_VERSION, NATIVE_PROTOCOL_VERSION
from reproof.live import android_recovery_qualification as qualification
from reproof.live.android_recovery_authority import inspect_qualification_journal
from reproof.live.model import LiveError
from tests.test_live_authority import FakeClock, AuthorityTestCase
from tests.test_android_launch import fixture


def frame():
    metadata = dict(type='frame', id=1, nativeFrameId=1, geometryVersion=1,
        width=2, height=2, orientation='portrait', capturedAt=1, mime='image/jpeg')
    header = json.dumps(metadata).encode()
    image = b'\xff\xd8synthetic-not-a-real-image\xff\xd9'
    return struct.pack('>II', len(header), len(image)) + header + image


class FakeProducer:
    def __init__(self, owner, serial, helper_apk, app_apk, *, runtime_profile, helper_digest, work):
        self.owner = owner
        self.app_profile = runtime_profile
        self.target_package = runtime_profile.data['package']
        self.helper_digest = helper_digest
        self.target_launched = False
        self._native = 'native_old'
        self._process = {'pid':100, 'startTicks':'200'}
        self._counter = 1000
        self.calls = []

    def bind_authority(self, handle, incarnation):
        self.device_authority = handle
        self.provider_incarnation = incarnation

    def start_authorized(self, session, lab, permit):
        self.lab = lab
        self.host_incarnation, self.helper_incarnation = permit.host_incarnation, permit.helper_incarnation
        configuration = self.owner.bind_initial(self, session, permit)
        self.qualification_configuration = configuration
        self.configuration_digest = qualification._digest(configuration)
        self.owner.initial_dispatch(self)
        self.startup_status = self.status()
        self.native_handshake = self.device_authority.bind_native_handshake(permit,
            protocol_version=NATIVE_PROTOCOL_VERSION, helper_version=HELPER_VERSION,
            helper_incarnation=self.helper_incarnation, provider_incarnation=self.provider_incarnation,
            native_incarnation=self._native, native_clock_id='android-elapsed-realtime', native_time_ms=1000)
        self.target_launched = True
        self.owner.initial_started(self)
        return {'ok':True}

    def status(self):
        return dict(recoveryQualificationVersion=1,
            recoveryQualificationConfigurationDigest=self.configuration_digest,
            hostIncarnation=self.host_incarnation, helperIncarnation=self.helper_incarnation,
            providerIncarnation=self.provider_incarnation, nativeIncarnation=self._native,
            nativeClockId='android-elapsed-realtime', nativeTimeMs=self._counter,
            nativeProcessIdentity=copy.deepcopy(self._process))

    def read_status(self):
        return self.status()

    def recovery_call(self, grant):
        self.calls.append(grant.wire()['purpose'])

    def recovery_result(self, grant):
        wire = grant.wire()
        purpose = wire['purpose']
        self._counter += 1
        if purpose.endswith('retire'):
            kind = 'retirement'
            evidence = {key:True for key in qualification._RETIRE}
            evidence.update(admissionWatermark=1, detachedCommandId=None)
        elif purpose == 'fresh-probe':
            kind = 'probe-frame'
            raw = frame()
            evidence = dict(nativeFrameId=1, frameSha256=hashlib.sha256(raw).hexdigest(),
                frameBytes=len(raw), width=2, height=2, geometryVersion=1,
                targetForeground=True, captureStartMs=self._counter, captureEndMs=self._counter)
        else:
            kind, evidence = 'exit-accepted', {}
        receipt = dict(schemaVersion=1, status='complete', grant=wire,
            nativeProcessIdentity=copy.deepcopy(self._process), completedNativeTimeMs=self._counter,
            kind=kind, evidence=evidence)
        fault = getattr(self.owner, '_fault', None)
        if fault: fault(self, purpose, receipt)
        return {'pending':False, 'receipt':receipt}

    def recovery_frame(self, grant):
        if getattr(self.owner, '_bad_frame', False):return frame()+b'changed'
        return frame()

    def collect_exit(self, issuer, grant):
        issuer.check()
        if getattr(self.owner, '_bad_exit', False):raise ContractError('Synthetic uncollected producer')
        return dict(nativeProcessIdentity=copy.deepcopy(self._process), remoteHelperAbsent=True,
            clientCollected=True, pollerJoined=True)

    def close(self):raise ContractError('No implicit private cleanup')
    close_authorized = lambda self, permit: self.close()


class FakeProbe(FakeProducer):
    def __init__(self, old, configuration, helper_incarnation, provider_incarnation, issuer):
        self.owner = old.owner
        self.helper_incarnation, self.provider_incarnation = helper_incarnation, provider_incarnation
        self.host_incarnation = old.host_incarnation
        self.qualification_configuration = copy.deepcopy(configuration)
        self.configuration_digest = qualification._digest(configuration)
        self._native = 'native_probe'
        self._process = {'pid':101, 'startTicks':'201'}
        self._counter = 2000
        self.calls = []

    def start(self):
        self.startup_status = self.status()
        return self


class QualificationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.clock = FakeClock()
        profile, _ = fixture()
        doc = profile.data
        self.app = self.root/'app.apk'; self.app.write_bytes(b'inert synthetic APK')
        self.helper = self.root/'helper.apk'; self.helper.write_bytes(b'inert helper')
        doc['artifact']['sha256'] = hashlib.sha256(self.app.read_bytes()).hexdigest()
        doc['artifact']['bytes'] = self.app.stat().st_size
        self.profile = validate_android_runtime_profile(doc)
        self.patches = [patch.object(qualification, 'HostAuthority',
            side_effect=lambda path:HostAuthority(path, clock=self.clock, lease_directory=self.root/'leases')),
            patch('reproof.live.android_recovery_transport.QualificationAndroidProvider', FakeProducer),
            patch('reproof.live.android_recovery_transport._ProbeProducer', FakeProbe)]
        for item in self.patches:item.start()
        self.harnesses=[]

    def tearDown(self):
        for harness in self.harnesses:
            harness.authority.close()
            if harness._journal is not None:harness._journal.close()
            if not harness._closed:
                import os
                os.close(harness._root_fd);os.close(harness._parent_fd)
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    def grant(self, authority, phase):
        context = type('GrantContext', (), {'clock':self.clock, 'authority':authority})()
        return AuthorityTestCase.grant(context, grant_id='grant_'+phase, lifetime_ns=300_000_000_000)

    def make(self):
        harness = qualification.QualificationHarness.create(self.root, serial='emulator-'+str(5554+2*len(self.harnesses)),
            helper_apk=self.helper, app_apk=self.app, runtime_profile=self.profile, grant_provider=self.grant)
        self.harnesses.append(harness)
        return harness

    def quarantine(self, harness):
        harness.start()
        session = harness._session
        handle = session['deviceAuthority']
        operation = handle.admit_operation(operation_id='uncertain_input', payload_digest='a'*64,
            session_id=harness.session_id, sequence=2)
        permit = handle.prepare_dispatch(operation, provider_incarnation=session['providerIncarnation'])
        handle.confirm_operation(permit, ProviderResult('unknown_receipt', 'unknown', 'b'*64))
        session['receipts']['uncertain_input'] = {'status':'unknown'}
        harness._lab.fail(harness.session_id, 'Synthetic unknown outcome')
        return permit

    def test_actual_private_lab_authority_roundtrip_preserves_unknown_and_normal_quarantine(self):
        harness=self.make();old_permit=self.quarantine(harness)
        result=harness.recover()
        self.assertFalse(result['normalRecoveryEnabled']);self.assertTrue(result['privateAuthorityReleased'])
        self.assertEqual(harness._journal.state,'released')
        self.assertEqual(harness._lab.devices[harness.device_id]['state'],'quarantined')
        self.assertEqual(harness._session['receipts']['uncertain_input']['status'],'unknown')
        operation=harness.authority.store.operation('uncertain_input')
        self.assertIsNone(operation['result_digest']);self.assertEqual(operation['status'],'recovered')
        self.assertFalse(harness._session['knownStart'])
        with self.assertRaises(ContractError):harness._provider.device_authority.check_dispatch_permit(old_permit)
        with self.assertRaises(LiveError):harness._lab.recover_device(harness.device_id)
        with self.assertRaises(ContractError):harness.recover()

    def test_each_native_phase_failure_keeps_quarantine(self):
        for purpose in ('old-retire','old-exit','fresh-probe','probe-retire','probe-exit'):
            with self.subTest(purpose=purpose):
                harness=self.make();self.quarantine(harness)
                def fail(producer, selected, receipt):
                    if selected==purpose:raise ContractError('Synthetic interrupted phase')
                harness._fault=fail
                with self.assertRaises(ContractError):harness.recover()
                self.assertEqual(harness._journal.state,'blocked')
                self.assertEqual(harness.authority.store.device(harness._provider.device_authority._device_fingerprint)['status'],'quarantined')
                self.assertEqual(harness._lab.devices[harness.device_id]['state'],'quarantined')
                harness.authority.close()

    def test_receipt_identity_and_barrier_lies_fail_closed(self):
        for key in ('grant','nativeProcessIdentity','evidence','completedNativeTimeMs'):
            with self.subTest(key=key):
                harness=self.make();self.quarantine(harness)
                def corrupt(producer,purpose,receipt):
                    if key=='grant':receipt[key]=dict(receipt[key],sessionId='other')
                    elif key=='nativeProcessIdentity':receipt[key]=dict(pid=999,startTicks='999')
                    elif key=='evidence':receipt[key]['captureWorkersStopped']=False
                    else:receipt[key]=receipt['grant']['nativeDeadlineMs']
                harness._fault=corrupt
                with self.assertRaises(ContractError):harness.recover()
                self.assertEqual(harness._journal.state,'blocked');harness.authority.close()

    def test_frame_digest_and_exit_proof_are_independently_required(self):
        for attr in ('_bad_frame','_bad_exit'):
            harness=self.make();self.quarantine(harness);setattr(harness,attr,True)
            with self.assertRaises(ContractError):harness.recover()
            self.assertEqual(harness._journal.state,'blocked');harness.authority.close()

    def test_cancellation_and_restart_never_resume(self):
        harness=self.make();self.quarantine(harness);harness.cancel.set()
        with self.assertRaises(ContractError):harness.recover()
        info=inspect_qualification_journal(harness.root/'recovery-journal')
        self.assertEqual(info['phase'],'blocked');self.assertFalse(info['resumable'])
        self.assertFalse(info['normalRecoveryEnabled'])

    def test_provider_identity_drift_before_first_recovery_status_rejected(self):
        harness=self.make();self.quarantine(harness);harness._provider._native='native_replaced'
        with self.assertRaises(ContractError):harness.recover()
        self.assertEqual(harness._journal.state,'blocked')

    def test_cancel_or_expire_during_native_completion_cannot_publish_success(self):
        for effect in ('cancel', 'expire'):
            harness=self.make();self.quarantine(harness)
            def interrupt(producer,purpose,receipt):
                if effect=='cancel':harness.cancel.set()
                else:self.clock.advance(301_000_000_000)
            harness._fault=interrupt
            with self.assertRaises(ContractError):harness.recover()
            self.assertEqual(harness._journal.state,'blocked')
            self.assertEqual(harness._provider.calls,['old-retire'])
            harness.authority.close()

    def test_original_unknown_receipt_mutation_prevents_reconciliation(self):
        harness=self.make();self.quarantine(harness)
        def mutate(producer,purpose,receipt):
            if purpose=='probe-exit':harness._session['receipts']['uncertain_input']['status']='succeeded'
        harness._fault=mutate
        with self.assertRaises(ContractError):harness.recover()
        row=harness.authority.store.device(harness._provider.device_authority._device_fingerprint)
        self.assertEqual(row['status'],'quarantined');self.assertEqual(row['generation'],1)

    def test_cancellation_after_durable_reconcile_retains_new_generation_quarantine(self):
        harness=self.make();self.quarantine(harness)
        handle=harness._provider.device_authority
        original=handle.reconcile
        def reconcile(*args,**kwargs):
            result=original(*args,**kwargs);harness.cancel.set();return result
        with patch.object(handle,'reconcile',side_effect=reconcile),self.assertRaises(ContractError):harness.recover()
        row=harness.authority.store.device(handle._device_fingerprint)
        self.assertEqual(row['generation'],2);self.assertEqual(row['status'],'quarantined')
        self.assertEqual(harness._journal.state,'blocked')
        self.assertFalse(handle._closed)

    def test_journal_intent_precedes_authority_reconciliation(self):
        harness=self.make();self.quarantine(harness)
        original=harness.authority.record_reconciliation
        def observe(*args,**kwargs):
            self.assertEqual(harness._journal.state,'reconcile-pending')
            return original(*args,**kwargs)
        with patch.object(harness.authority,'record_reconciliation',side_effect=observe):harness.recover()
        self.assertEqual(harness._journal.state,'released')

    def test_changed_output_path_cannot_start_device(self):
        harness=self.make();moved=harness.root.with_name(harness.root.name+'-moved')
        harness.root.rename(moved);harness.root.mkdir()
        with self.assertRaises(ContractError):harness.start()
        self.assertIsNone(harness._provider)

    def test_unsupported_physical_selection_and_changed_app_rejected_before_lab(self):
        with patch.object(qualification,'Lab') as lab:
            with self.assertRaises(ContractError):qualification.QualificationHarness.create(self.root,
                serial='physical-phone',helper_apk=self.helper,app_apk=self.app,
                runtime_profile=self.profile,grant_provider=self.grant)
            self.app.write_bytes(b'changed')
            with self.assertRaises(ContractError):self.make()
            lab.assert_not_called()
