"""Synthetic authority/journal checks; no Android runtime qualification."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof.live.android_recovery_authority import (
    QualificationJournal, RecoveryIssuer, inspect_qualification_journal, _digest,
    MAX_PHASE_NS, MAX_ATTEMPT_NS)
from tests.test_live_authority import AuthorityTestCase, DIGEST_A, DIGEST_B, DIGEST_C


def binding(device=None):
    return dict(schemaVersion=1, kind='reproof-android-recovery-qualification',
        attemptId='attempt-one', sessionId='session-one',
        deviceScopeDigest=device._device_fingerprint if device else DIGEST_A,
        applicationId='android_app', artifactDigest=DIGEST_B,
        profileDigest=DIGEST_C, helperArtifactDigest='d'*64,
        priorOwnershipGeneration=device.generation if device else 1,
        hostIncarnation=device._authority.host_incarnation if device else 'host-one',
        helperIncarnation=device.helper_incarnation if device else 'helper-one',
        providerIncarnation='provider-one')


class RecoveryJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.journal = QualificationJournal.create(self.root/'journal', binding())

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def test_actual_runtime_application_id_is_not_android_package(self):
        from tests.test_android_launch import fixture
        profile, _ = fixture()
        selected = binding()
        selected['applicationId'] = profile.data['applicationId']
        selected['profileDigest'] = profile.digest
        selected['artifactDigest'] = profile.data['artifact']['sha256']
        self.assertNotEqual(profile.data['applicationId'], profile.data['package'])
        journal = QualificationJournal.create(self.root/'runtime-profile', selected)
        try:
            self.assertEqual(journal.binding['applicationId'], profile.data['applicationId'])
        finally:
            journal.close()
        selected['applicationId'] = profile.data['package']
        with self.assertRaises(ContractError):
            QualificationJournal.create(self.root/'package-as-id', selected)

    def test_diagnostic_restart_cannot_resume_or_enable_normal_recovery(self):
        self.journal.advance('session-bound', 'producer-start-pending', DIGEST_A)
        result = inspect_qualification_journal(self.root/'journal')
        self.assertFalse(result['resumable'])
        self.assertFalse(result['normalRecoveryEnabled'])
        self.assertEqual(result['phase'], 'producer-start-pending')
        self.assertNotIn('binding', result)
        self.journal.close()
        with self.assertRaises(ContractError):
            self.journal.advance('producer-start-pending', 'producer-running')

    def test_every_interrupted_phase_can_block_and_never_revive(self):
        from reproof.live.android_recovery_authority import _PHASES
        for stop in range(len(_PHASES)-1):
            with self.subTest(phase=_PHASES[stop]):
                journal = QualificationJournal.create(self.root/f'phase-{stop}', binding())
                try:
                    for first, second in zip(_PHASES[:stop], _PHASES[1:stop+1]):
                        journal.advance(first, second, DIGEST_A)
                    journal.block('cancelled')
                    with self.assertRaises(ContractError):
                        journal.advance('blocked', 'released', DIGEST_A)
                    self.assertEqual(inspect_qualification_journal(self.root/f'phase-{stop}')['phase'], 'blocked')
                finally:
                    journal.close()

    def test_existing_output_symlink_and_same_inode_mutation_are_preserved(self):
        with self.assertRaises(FileExistsError):
            QualificationJournal.create(self.root/'journal', binding())
        (self.root/'alias').symlink_to(self.root/'journal', target_is_directory=True)
        with self.assertRaises(OSError):
            inspect_qualification_journal(self.root/'alias')
        record = self.root/'journal/events.jsonl'
        record.write_bytes(b'foreign bytes')
        with self.assertRaises(ContractError):
            self.journal.block()
        self.journal.close()
        self.assertEqual(record.read_bytes(), b'foreign bytes')

    def test_root_replacement_is_not_written_or_deleted(self):
        path = self.root/'journal'
        path.rename(self.root/'original')
        path.mkdir()
        (path/'foreign').write_text('keep')
        with self.assertRaises(ContractError):
            self.journal.advance('session-bound', 'producer-start-pending')
        self.journal.close()
        self.assertEqual((path/'foreign').read_text(), 'keep')
        self.assertTrue((self.root/'original/events.jsonl').exists())

    def test_binding_copy_thread_and_transition_fences(self):
        result=[]
        thread=threading.Thread(target=lambda: self._thread_check(result))
        thread.start(); thread.join()
        self.assertEqual(result, [True])
        copy=self.journal.binding; copy['sessionId']='changed'
        self.assertEqual(self.journal.binding['sessionId'], 'session-one')
        with self.assertRaises(ContractError):
            self.journal.advance('session-bound', 'released', DIGEST_A)
        self.journal.advance('session-bound', 'producer-start-pending')
        with self.assertRaises(ContractError):
            self.journal.advance('session-bound', 'producer-start-pending')

    def _thread_check(self, result):
        try:self.journal.check()
        except ContractError:result.append(True)

    def test_truncated_and_noncanonical_records_are_diagnostics_failure(self):
        record=self.root/'journal/events.jsonl'
        original=record.read_bytes()
        for data in (original[:-1], original.replace(b'"schemaVersion":1', b'"schemaVersion":true'),
                     original.replace(b'"index":0', b'"index":0,"index":0')):
            record.write_bytes(data)
            with self.assertRaises(ContractError):inspect_qualification_journal(self.root/'journal')
        record.write_bytes(original)


class _SyntheticChannel:
    def __init__(self, journal, role='old'):
        self.role=role
        self.configuration={k:v for k,v in journal.binding.items()
            if k not in {'hostIncarnation','helperIncarnation','providerIncarnation'}}
        self.configuration['phase']='session' if role=='old' else 'probe'
        self.helper_incarnation=journal.binding['helperIncarnation'] if role=='old' else 'helper-probe'
        self.provider_incarnation='provider-one' if role=='old' else 'provider-probe'
        self.registered=True
        self.status=dict(recoveryQualificationVersion=1,
            recoveryQualificationConfigurationDigest=_digest(self.configuration),
            hostIncarnation=journal.binding['hostIncarnation'], helperIncarnation=self.helper_incarnation,
            providerIncarnation=self.provider_incarnation, nativeIncarnation='native-old' if role=='old' else 'native-probe',
            nativeClockId='android-elapsed-realtime', nativeTimeMs=1000,
            nativeProcessIdentity={'pid':123 if role=='old' else 124, 'startTicks':'100'})

    def require_owned(self, issuer=None):
        if not self.registered:raise ContractError('Unregistered synthetic channel')

    def read_status(self):
        self.require_owned()
        return dict(self.status)


class RecoveryIssuerTests(AuthorityTestCase):
    def setUp(self):
        super().setUp()
        self.stack=ExitStack()
        self.device=self.claim(parent_grant=self.grant(lifetime_ns=300_000_000_000))
        self.journal=QualificationJournal.create(self.root/'recovery', binding(self.device))
        self.stack.callback(self.journal.close)
        for a,b in [('session-bound','producer-start-pending'),('producer-start-pending','producer-running'),
                    ('producer-running','quarantined')]:self.journal.advance(a,b)
        self.device.revoke_dispatches()
        self.fresh=self.grant(grant_id='recovery-fresh', lifetime_ns=300_000_000_000)
        self.lease=self.stack.enter_context(self.device.borrow_native_recovery_lease(
            self.device.recovery_snapshot(), parent_grant=self.fresh))
        self.issuer=RecoveryIssuer(self.device,self.lease,self.fresh,self.journal,lambda:False)
        self.stack.callback(self.issuer.close)
        # Only the transport is synthetic; ownership, lease, grants and journal
        # are the real host implementation. Integration tests cover real class.
        module=types.ModuleType('reproof.live.android_recovery_qualification')
        module._RecoveryChannel=_SyntheticChannel
        self.stack.enter_context(patch.dict('sys.modules', {'reproof.live.android_recovery_qualification':module}))
        self.channel=_SyntheticChannel(self.journal)

    def tearDown(self):
        self.stack.close()
        super().tearDown()

    def issue(self):
        self.journal.advance('quarantined','old-retire-pending')
        return self.issuer.issue('old-retire',self.channel)

    def test_distinct_grant_is_bounded_and_not_an_ordinary_permit(self):
        grant=self.issue(); wire=grant.wire()
        self.assertEqual(wire['purpose'],'old-retire')
        self.assertEqual(wire['priorOwnershipGeneration'],1)
        self.assertEqual(wire['recoveryOwnershipGeneration'],2)
        self.assertLessEqual(wire['nativeDeadlineMs']-1000,30000)
        with self.assertRaises(ContractError):self.device.check_dispatch_permit(grant)
        with self.assertRaises(ContractError):self.device.admit_operation(operation_id='no-normal',
            payload_digest=DIGEST_A,session_id='session-one',sequence=1)
        self.journal.advance('old-retire-pending','old-terminal',DIGEST_A)
        with self.assertRaises(ContractError):grant.wire()

    def test_phase_grant_and_attempt_expiry(self):
        grant=self.issue()
        self.clock.advance(MAX_PHASE_NS)
        with self.assertRaises(ContractError):grant.wire()
        self.clock.advance(MAX_ATTEMPT_NS)
        with self.assertRaises(ContractError):self.issuer.check()

    def test_fresh_grant_expiry_cancel_and_lease_loss(self):
        self.issuer._cancellation=lambda:True
        with self.assertRaises(ContractError):self.issuer.check()
        self.issuer._cancellation=lambda:False
        self.lease._active=False
        with self.assertRaises(ContractError):self.issuer.check()
        self.lease._active=True
        self.clock.advance(400_000_000_000)
        with self.assertRaises(ContractError):self.issuer.check()

    def test_unregistered_wrong_purpose_or_native_identity_rejected(self):
        self.journal.advance('quarantined','old-retire-pending')
        self.channel.registered=False
        with self.assertRaises(ContractError):self.issuer.issue('old-retire',self.channel)
        self.channel.registered=True
        with self.assertRaises(ContractError):self.issuer.issue('fresh-probe',self.channel)
        self.channel.status['nativeClockId']='arbitrary-clock'
        with self.assertRaises(ContractError):self.issuer.issue('old-retire',self.channel)
        self.channel.status['nativeClockId']='android-elapsed-realtime'
        self.issuer.issue('old-retire',self.channel)
        self.channel.status['nativeProcessIdentity']={'pid':456,'startTicks':'100'}
        with self.assertRaises(ContractError):self.issuer.issue('old-retire',self.channel)

    def test_blocked_and_closed_owner_cannot_mint_or_reuse(self):
        grant=self.issue()
        self.journal.block('cancelled')
        with self.assertRaises(ContractError):grant.wire()
        with self.assertRaises(ContractError):self.issuer.issue('old-exit',self.channel)
        self.issuer.close()
        with self.assertRaises(ContractError):self.issuer.check()

    def test_original_binding_and_exact_grant_required(self):
        self.journal._binding['providerIncarnation']='changed'
        with self.assertRaises(ContractError):self.issuer.check()
        self.journal._binding['providerIncarnation']='provider-one'
        other=self.grant(grant_id='another-fresh',lifetime_ns=300_000_000_000)
        with self.assertRaises(ContractError):RecoveryIssuer(self.device,self.lease,other,self.journal,lambda:False)

    def test_fresh_probe_requires_new_incarnations_and_matching_configuration(self):
        from reproof.live.android_recovery_authority import _PHASES
        self.issue()
        for phase in ('old-terminal','old-exit-pending','old-exited','probe-start-pending','probe-running','fresh-probe-pending'):
            self.journal.advance(self.journal.state,phase,DIGEST_A)
        channel=_SyntheticChannel(self.journal,'probe')
        channel.status['nativeIncarnation']='native-old'
        with self.assertRaises(ContractError):self.issuer.issue('fresh-probe',channel)
        channel.status['nativeIncarnation']='native-probe'
        channel.configuration['artifactDigest']='e'*64
        with self.assertRaises(ContractError):self.issuer.issue('fresh-probe',channel)
        channel.configuration['artifactDigest']=DIGEST_B
        grant=self.issuer.issue('fresh-probe',channel)
        self.assertEqual(grant.wire()['purpose'],'fresh-probe')
        self.assertEqual(grant.wire()['helperIncarnation'],'helper-probe')
        with self.assertRaises(ContractError):self.issuer.issue('fresh-probe',channel)

    def test_fork_thread_and_channel_subclass_cannot_gain_authority(self):
        self.journal.advance('quarantined','old-retire-pending')
        class Derived(_SyntheticChannel):pass
        with self.assertRaises(ContractError):self.issuer.issue('old-retire',Derived(self.journal))
        with patch('os.getpid',return_value=os.getpid()+1000):
            with self.assertRaises(ContractError):self.issuer.check()
        result=[]
        def other_thread():
            try:self.issuer.check()
            except ContractError:result.append(True)
        thread=threading.Thread(target=other_thread);thread.start();thread.join()
        self.assertEqual(result,[True])

    def test_strict_native_identity_fields_and_deadline_mapping(self):
        self.journal.advance('quarantined','old-retire-pending')
        changes=[('recoveryQualificationVersion',True),('nativeTimeMs',True),
            ('nativeProcessIdentity',{'pid':True,'startTicks':'100'}),
            ('nativeProcessIdentity',{'pid':123,'startTicks':100}),
            ('nativeProcessIdentity',{'pid':123,'startTicks':'0'}),
            ('nativeProcessIdentity',{'pid':123,'startTicks':'01'}),
            ('nativeProcessIdentity',{'pid':123,'startTicks':'1'*21})]
        for key,value in changes:
            original=self.channel.status[key];self.channel.status[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ContractError):
                self.issuer.issue('old-retire',self.channel)
            self.channel.status[key]=original
        grant=self.issuer.issue('old-retire',self.channel)
        self.channel.registered=False
        with self.assertRaises(ContractError):grant.wire()
