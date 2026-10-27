"""Synthetic protocol/lifecycle checks only; no Android runtime qualification."""
import copy
import json
from pathlib import Path
import pickle
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reproof import contracts
from reproof.android_profile import validate_android_runtime_profile
from reproof.android_readiness import ApprovedAndroidFixture, validate_android_fixture, android_fixture_endpoint_for_policy
from reproof.fixtures import FixtureCoordinator, FixtureError
from reproof.live.android_preparation import (AndroidFixtureAdapter, AndroidPreparation,
                                             android_fixture_endpoint)
from reproof.live.authority import HostAuthority
from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.issue_sessions import FixturePreparation, IssueSessionError
from reproof.live.model import Lab, LiveError
from tests.test_clock_sync import FakeClock
from tests.test_fixture_allocations import project_document, collection_policy
from tests.test_live_authority_integration import Clock, FencedProvider
from tests.test_worker_profiles import android_document


class PreparedProvider(FencedProvider):
    def __init__(self):
        super().__init__()
        self.order=[]
        self.fail_phase=None
        self.quiescent=True
        self.valid=True
        self.commit=True
        self.corrupt=None
        self.ready_entered=None
        self.ready_release=None
        self.handoff=None
        self.challenges=[]

    def start_authorized(self, session, lab, permit):
        self.order.append('startup')
        result=super().start_authorized(session,lab,permit)
        assert session['frame'] is None and session['state']=='connecting'
        assert 'releaseRecorder' not in session
        return result

    def preparation_challenge(self, session, project_digest, build_id):
        result={'sessionId':session['id'],'projectDigest':project_digest,'buildId':build_id,
                'nonce':str(len(self.challenges))+session['providerIncarnation']}
        self.challenges.append(result)
        return copy.deepcopy(result)

    def fixture_operation(self, operation, challenge, timeout_seconds, *, status_only=False):
        phase=operation['operation']
        self.order.append(('status:' if status_only else '')+phase)
        if phase!='cleanup':
            assert 'releaseRecorder' not in self.session
            assert self.session['frame'] is None
            assert self.lab.publish_frame(self.session['id'],b'<svg>secret-before-ready</svg>',
                                          'image/svg+xml',400,800) is False
        if phase=='check' and self.ready_entered is not None:
            self.ready_entered.set()
            self.ready_release.wait(2)
        result={'challenge':copy.deepcopy(challenge),'operation':copy.deepcopy(operation),
                'phase':phase,'status':'failed' if self.fail_phase==phase else 'complete',
                'quiescent':True}
        if self.corrupt is not None:self.corrupt(result)
        return result

    def validate_android_preparation(self, handoff):
        self.order.append('validate')
        self.handoff=handoff
        return self.valid

    def commit_android_preparation(self, handoff):
        self.order.append('commit')
        assert self.session.get('releaseRecorder') is not None
        assert self.session['state']=='connecting' and self.session['frame'] is None
        if self.commit:
            def publish():
                for _ in range(100):
                    if self.session.get('_androidAdmissionOpen'):
                        self.lab.publish_frame(self.session['id'],b'<svg>ready</svg>',
                                               'image/svg+xml',400,800)
                        return
                    time.sleep(.005)
            threading.Thread(target=publish,daemon=True).start()
        return self.commit

    def quiesce_android_preparation(self, timeout_seconds):
        self.order.append('quiesce')
        return self.quiescent

    def resolve_locator(self, target):
        frame=self.lab.frame(self.session['id'])
        return {'target':copy.deepcopy(target),'x':.5,'y':.5,
                'frameId':frame['id'],'geometryVersion':frame['geometryVersion'],
                'providerIncarnation':self.lab.release_binding(self.session['id'],'owner')['providerIncarnation'],
                'observedAtMs':int(time.time()*1000)}

    def close_authorized(self, permit):
        self.order.append('close')
        return super().close_authorized(permit)


class AndroidPreparationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.clock=Clock()
        self.authority=HostAuthority(self.root/'authority.sqlite3',clock=self.clock,
                                     lease_directory=self.root/'leases')
        received=self.authority.clock_sync.sample();self.clock.now+=10
        sent=self.authority.clock_sync.sample()
        mapping=self.authority.clock_sync.record_exchange(
            coordinator_clock_id='coordinator',coordinator_send_ns=received.nanoseconds,
            host_received=received,host_sent=sent,coordinator_receive_ns=sent.nanoseconds,
            max_drift_ppm=0)
        grant=self.authority.issue_parent_grant(mapping,grant_id='grant',project_id='checkout',
            controller_id='controller',renewal_sequence=1,
            coordinator_deadline_ns=sent.nanoseconds+60_000_000_000)
        self.config_data={'schemaVersion':1,'kind':'reproof-approved-android-fixture',
            'referenceId':'seed_account','profileDigest':'1'*64,'artifactDigest':'4'*64,
            'fixtureId':'seed_account','fixtureVersion':1,'policyDigest':'a'*64,
            'helperSignerDigest':'b'*64,'launchConfigurationDigest':None}
        endpoint=android_fixture_endpoint_for_policy(fixture_id=self.config_data['fixtureId'],
            fixture_version=self.config_data['fixtureVersion'],policy_digest=self.config_data['policyDigest'])
        self.project=project_document()
        self.project['applications']=[{'id':'android_app','platform':'android','bundle':'com.example.checkout'}]
        for build in self.project['builds']:
            build['applicationId']='android_app'
        self.project['builds'][0]['artifactDigest']='4'*64
        for recipe in self.project['fixtures']:
            recipe['endpointId']=endpoint
        profile_data=android_document();profile_data['projectDigest']=contracts.digest(self.project)
        profile_data['approvedReferences']['preparations']=['seed_account']
        profile_data['capabilities']['observations']=['pixels','accessibility']
        profile_data['capabilities']['logAdapter']=None
        self.profile=validate_android_runtime_profile(profile_data)
        self.config_data['profileDigest']=self.profile.digest
        self.config=validate_android_fixture(self.config_data,self.profile)
        self.provider=PreparedProvider()
        self.device={'id':'device','name':'Synthetic Android','platform':'android','kind':'android-live',
            'factory':lambda:self.provider,
            '_authority':{'deviceKind':'android','physicalId':'synthetic-one'},
            'capabilities':{'actions':['tap'],'inputMode':'gesture-batch','locatorKinds':['resource-id'],
                'applicationIdentity':self.profile.application_identity,
                'applicationProfile':self.profile.data,'applicationProfileDigest':self.profile.digest}}
        self.lab=Lab([self.device],self.root/'lab',authority=self.authority,parent_grant=grant,
            recording_clock_sync=ClockSynchronizer(FakeClock()),
            recording_wall_clock_ms=lambda:int(time.time()*1000))
        self.registration=self.lab.register_recording_project(self.project,collection_policy(),
            capacity_bytes=32*1024*1024,journal_headroom_bytes=512*1024)
        self.fixtures=FixtureCoordinator(self.root/'fixtures')
        self.adapter=AndroidFixtureAdapter(self.config)
        self.plan=self.fixtures.register_plan(self.registration,application_id='android_app',
            fixture_id='seed_account',adapter=self.adapter,check_recipe_ids=('check_account',),
            cleanup_recipe_id='cleanup_account')
        self.service=self.lab.create_issue_session_service(self.fixtures)

    def tearDown(self):
        self.lab.close_all();self.fixtures.close();self.authority.close();self.temp.cleanup()

    def start(self, **kwargs):
        return self.service.start_android_prepared_recording(device_id='device',owner='owner',
            controller_id='browser',registration=self.registration,application_id='android_app',
            build_id='original',preparation=FixturePreparation(self.plan,{}),configuration=self.config,
            timeout_seconds=1,**kwargs)

    def test_complete_attempt_defers_recorder_frames_and_cleans_once_before_close(self):
        handle=self.start()
        self.assertEqual(self.provider.order,['startup','prepare','check','validate','commit'])
        self.assertEqual(self.lab.frame(handle.session_id)['imageBase64'],'PHN2Zz5yZWFkeTwvc3ZnPg==')
        self.assertFalse(self.lab.sessions[handle.session_id]['knownStart'])
        result=self.service.stop(handle)
        self.assertEqual(result['issue']['state'],'complete',result)
        self.assertEqual(self.provider.order[-3:],['quiesce','cleanup','close'])
        self.assertEqual(self.provider.order.count('cleanup'),1)
        self.assertEqual(self.lab.devices['device']['state'],'available')

    def test_second_attempt_same_registered_plan_fresh_challenge(self):
        first=self.start();self.service.stop(first)
        second=self.start();self.service.stop(second)
        self.assertNotEqual(self.provider.challenges[0],self.provider.challenges[1])
        self.assertEqual(self.provider.order.count('prepare'),2)

    def test_handoff_is_one_use_nonserializable_and_receipt_dict_not_capability(self):
        handle=self.start();context=self.lab.sessions[handle.session_id]['_androidPreparation']
        with self.assertRaises(TypeError):pickle.dumps(self.provider.handoff)
        with self.assertRaises(TypeError):copy.copy(self.provider.handoff)
        for value in (self.provider.handoff,context.receipts,{}):
            with self.subTest(value=type(value)),self.assertRaises(FixtureError):
                context.consume(value,self.provider,self.lab.sessions[handle.session_id])
        self.service.stop(handle)

    def test_unknown_quiescence_never_closes_helper_or_releases_device(self):
        handle=self.start();self.provider.quiescent=False
        result=self.service.stop(handle)
        self.assertEqual(result['issue']['state'],'quarantined')
        self.assertNotIn('cleanup',self.provider.order)
        self.assertNotIn('close',self.provider.order)
        self.assertEqual(self.lab.devices['device']['state'],'quarantined')
        self.lab.close_all()
        self.assertNotIn('close',self.provider.order)

    def test_changed_identity_prevents_recorder_and_activation(self):
        self.provider.valid=False
        with self.assertRaises(IssueSessionError):self.start()
        self.assertNotIn('commit',self.provider.order)
        session=next(iter(self.lab.sessions.values()))
        self.assertNotIn('releaseRecorder',session)
        self.assertIsNone(session['frame'])

    def test_recorder_failure_never_activates_and_cleanup_still_runs(self):
        with patch.object(self.lab._recording_store,'begin_recording',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(IssueSessionError):self.start()
        self.assertNotIn('commit',self.provider.order)
        self.assertIn('quiesce',self.provider.order)

    def test_cross_attempt_receipt_rejected_before_recorder(self):
        self.provider.corrupt=lambda result:result['challenge'].update(sessionId='other')
        with self.assertRaises(IssueSessionError):self.start()
        self.assertNotIn('commit',self.provider.order)
        self.assertNotIn('releaseRecorder',next(iter(self.lab.sessions.values())))

    def test_cancellation_while_check_pending_never_admits_late_ready(self):
        entered=threading.Event();release=threading.Event();cancel=threading.Event()
        self.provider.ready_entered=entered;self.provider.ready_release=release
        errors=[]
        def work():
            try:self.start(cancellation=cancel)
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=work);thread.start()
        self.assertTrue(entered.wait(1));cancel.set();release.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertTrue(errors)
        self.assertNotIn('commit',self.provider.order)
        self.assertIsNone(next(iter(self.lab.sessions.values()))['frame'])

    def test_legacy_native_path_rejected_before_provider_or_fixture(self):
        self.lab.devices['device'].pop('_authority')
        with self.assertRaises(IssueSessionError):self.start()
        self.assertEqual(self.provider.order,[])

    def test_configured_fixture_identity_must_match_registered_plan(self):
        changed=dict(self.config_data,fixtureId='other_fixture')
        config=validate_android_fixture(changed,self.profile)
        with self.assertRaises((FixtureError,IssueSessionError)):
            self.service.start_android_prepared_recording(device_id='device',owner='owner',
                controller_id='browser',registration=self.registration,application_id='android_app',
                build_id='original',preparation=FixturePreparation(self.plan,{}),configuration=config)
        self.assertEqual(self.provider.order,[])

    def test_failed_helper_activation_keeps_frames_closed(self):
        self.provider.commit=False
        with self.assertRaises(IssueSessionError):self.start()
        session=next(iter(self.lab.sessions.values()))
        self.assertIsNone(session['frame']);self.assertFalse(session['_androidAdmissionOpen'])
        self.assertFalse(self.lab.publish_frame(session['id'],b'<svg/>','image/svg+xml',400,800))

    def test_inflight_input_blocks_app_cleanup_helper_close_and_late_close_retry(self):
        handle=self.start();session=self.lab.sessions[handle.session_id]
        session['inflightCommands']['pending']='synthetic'
        result=self.lab.close_session(handle.session_id,'owner')
        self.assertEqual(result['state'],'failed')
        self.assertNotIn('cleanup',self.provider.order)
        self.assertNotIn('close',self.provider.order)
        session['inflightCommands'].clear()
        self.lab.close_session(handle.session_id,'owner')
        self.assertNotIn('close',self.provider.order)
        self.assertEqual(self.lab.devices['device']['state'],'quarantined')

    def test_pointer_cancel_precedes_app_quiesce_cleanup(self):
        handle=self.start();session=self.lab.sessions[handle.session_id]
        session['activePointers'][0]={'x':.5,'y':.5}
        def cancel(value):
            self.provider.order.append('pointer_cancel')
            value['activePointers'].clear()
        with patch.object(self.lab,'_cancel_active_pointers',side_effect=cancel):
            self.service.stop(handle)
        self.assertLess(self.provider.order.index('pointer_cancel'),self.provider.order.index('quiesce'))
        self.assertLess(self.provider.order.index('quiesce'),self.provider.order.index('cleanup'))

    def test_pointer_cancel_failure_never_dispatches_app_cleanup(self):
        handle=self.start();session=self.lab.sessions[handle.session_id]
        session['activePointers'][0]={'x':.5,'y':.5}
        with patch.object(self.lab,'_cancel_active_pointers',side_effect=LiveError('pointer_cancel_unknown','synthetic')):
            result=self.service.stop(handle)
        self.assertEqual(result['issue']['state'],'quarantined')
        self.assertNotIn('quiesce',self.provider.order)
        self.assertNotIn('cleanup',self.provider.order)

    def test_failed_app_cleanup_retains_fixture_and_device_ownership(self):
        handle=self.start();self.provider.fail_phase='cleanup'
        result=self.service.stop(handle)
        self.assertEqual(result['issue']['state'],'quarantined')
        self.assertEqual(result['issue']['cleanup'][0]['status'],'unknown')
        self.assertNotIn('close',self.provider.order)
        self.assertEqual(self.lab.devices['device']['state'],'quarantined')

    def test_restart_record_is_quarantined_not_a_new_preparation_capability(self):
        from reproof.live.issue_sessions import IssueSessionService
        handle=self.start()
        recovered=IssueSessionService(self.lab,self.fixtures,root=self.service.root)
        self.assertEqual(recovered.get(handle.issue_id)['state'],'quarantined')
        self.assertEqual(recovered._active,{})
        self.assertFalse(self.lab.sessions[handle.session_id]['knownStart'])
        self.service.stop(handle)

    def test_terminal_receipt_cannot_claim_nonquiescent_completion(self):
        self.provider.corrupt=lambda result:result.update(quiescent=False)
        with self.assertRaises(IssueSessionError):self.start()
        self.assertNotIn('commit',self.provider.order)
        self.assertNotIn('releaseRecorder',next(iter(self.lab.sessions.values())))

    def test_expired_authority_at_handoff_never_records_or_activates(self):
        original=self.provider.validate_android_preparation
        def validate(binding):
            original(binding)
            self.clock.now+=120_000_000_000
            return True
        self.provider.validate_android_preparation=validate
        with self.assertRaises((IssueSessionError,LiveError)):self.start()
        self.assertNotIn('commit',self.provider.order)
        self.assertNotIn('releaseRecorder',next(iter(self.lab.sessions.values())))

    def test_cancel_after_fresh_identity_validation_still_prevents_recording(self):
        cancellation=threading.Event()
        def validate(binding):
            cancellation.set()
            return True
        self.provider.validate_android_preparation=validate
        with self.assertRaises(IssueSessionError):self.start(cancellation=cancellation)
        self.assertNotIn('commit',self.provider.order)
        self.assertNotIn('releaseRecorder',next(iter(self.lab.sessions.values())))

    def test_handoff_fields_and_exposed_challenge_are_immutable(self):
        from dataclasses import FrozenInstanceError
        handle=self.start();handoff=self.provider.handoff
        with self.assertRaises(FrozenInstanceError):handoff.session_id='other'
        value=handoff.challenge;value['sessionId']='other'
        self.assertEqual(handoff.challenge['sessionId'],handle.session_id)
        self.service.stop(handle)

    def test_existing_issue_entrypoint_routes_app_owned_adapter_and_repeats_fresh(self):
        for _ in range(2):
            handle=self.service.start_prepared_recording(device_id='device',owner='owner',
                controller_id='browser',registration=self.registration,application_id='android_app',
                build_id='original',preparations=(FixturePreparation(self.plan,{}),),timeout_seconds=1)
            result=self.service.stop(handle)
            self.assertEqual(result['issue']['state'],'complete')
        self.assertEqual(self.provider.order.count('prepare'),2)
        self.assertNotEqual(self.provider.challenges[0],self.provider.challenges[1])

    def test_existing_entrypoint_rejects_candidate_override_before_native_start(self):
        with self.assertRaises(IssueSessionError):
            self.service.start_prepared_recording(device_id='device',owner='owner',
                controller_id='browser',registration=self.registration,application_id='android_app',
                build_id='original',preparations=(FixturePreparation(self.plan,{}),),
                _candidate_identity={'artifactDigest':'9'*64})
        self.assertEqual(self.provider.order,[])

    def test_original_scenario_replay_uses_real_registry_and_fresh_app_preparation(self):
        from reproof.qualification import ScenarioRegistry
        from reproof.scenario_runner import (ScenarioRunner, ObservationRegistry,
            VariableResolverRegistry)
        from tests.g4_support import SnapshotObservationAdapter, specification, qualification, runtime_policy
        handle=self.start();session=self.lab.get_session(handle.session_id,'owner')
        frame=self.lab.frame(handle.session_id)
        target={'kind':'resource-id','value':'checkout_button'}
        self.service.input(handle,{'controllerId':session['controllerId'],'epoch':session['epoch'],
            'sequence':1,'commandId':'first_tap','frameId':frame['id'],
            'geometryVersion':frame['geometryVersion'],'action':'tap','payload':{'x':.5,'y':.5}},
            recording_input={'action':'tap','target':target,'parameters':{}})
        original=self.service.stop(handle)['recording']
        registry=ScenarioRegistry(self.root/'scenarios')
        self.addCleanup(registry.close)
        observations=ObservationRegistry(self.registration)
        observations.register('screen',SnapshotObservationAdapter())
        runner=ScenarioRunner(self.lab,registry,VariableResolverRegistry(self.registration),observations)
        self.service.registry=registry;self.service.runner=runner
        spec=specification(original);spec['actions']=spec['actions'][:1]
        spec['actions'][0]['target']=target;spec['bindings']=[]
        q=qualification(self.project,original,spec,self.plan)
        approved=registry.register(self.registration,original,spec,q,runtime_policy(),fixture_plans=(self.plan,))
        result=self.service.replay(registry.original_execution(approved),registration=self.registration,
            device_id='device',owner='owner',controller_id='replay_browser',
            preparations=(FixturePreparation(self.plan,{}),),timeout_seconds=2)
        self.assertEqual(result.public()['verdict'],'observed')
        self.assertTrue(result.public()['attemptRecordingDigest'])
        self.assertEqual(self.provider.order.count('prepare'),2)
        self.assertNotEqual(self.provider.challenges[0],self.provider.challenges[1])

    def test_late_helper_close_after_cleanup_timeout_cannot_release_quarantine(self):
        handle=self.start();entered=threading.Event();release=threading.Event();finished=threading.Event()
        original=self.provider.close_authorized
        def delayed(permit):
            entered.set();release.wait(2)
            try:return original(permit)
            finally:finished.set()
        self.provider.close_authorized=delayed
        result=self.service.stop(handle,cleanup_timeout_seconds=.03)
        self.assertTrue(entered.is_set())
        self.assertEqual(result['issue']['state'],'quarantined')
        release.set();self.assertTrue(finished.wait(1));time.sleep(.03)
        session=self.lab.sessions[handle.session_id]
        self.assertTrue(session['_androidCleanupAbandoned'])
        self.assertEqual(session['state'],'failed')
        self.assertEqual(self.lab.devices['device']['state'],'quarantined')
        self.assertEqual(self.fixtures.status(session['_androidPreparation'].allocation)['state'],'quarantined')
        self.assertIsNotNone(session.get('deviceAuthority'))

    def test_frame_that_races_closed_admission_cannot_reactivate_session(self):
        handle=self.start();session=self.lab.sessions[handle.session_id]
        # A frame was accepted while ready; closing admission must suppress all
        # later publications and cannot change failed/connecting to active.
        session['state']='connecting'
        session['_androidAdmissionOpen']=False
        for state in ('connecting','failed','draining','closed'):
            session['state']=state
            self.assertFalse(self.lab.publish_frame(handle.session_id,b'<svg>late</svg>',
                                                    'image/svg+xml',400,800))
            self.assertEqual(session['state'],state)
        session['state']='active';session['_androidAdmissionOpen']=True
        self.service.stop(handle)

    def test_helper_cleanup_error_retains_fixture_even_for_direct_lab_close(self):
        handle=self.start();session=self.lab.sessions[handle.session_id]
        self.provider.close_authorized=lambda permit: (_ for _ in ()).throw(RuntimeError('synthetic'))
        result=self.lab.close_session(handle.session_id,'owner')
        self.assertEqual(result['state'],'failed')
        self.assertTrue(session['_androidCleanupAbandoned'])
        self.assertEqual(self.fixtures.status(session['_androidPreparation'].allocation)['state'],'quarantined')
        self.assertEqual(self.lab.devices['device']['state'],'quarantined')

    def test_frame_in_flight_rechecks_android_gate_before_publication(self):
        handle=self.start();session=self.lab.sessions[handle.session_id]
        recorder=session['releaseRecorder'];original=recorder.record_frame
        entered=threading.Event();release=threading.Event();result=[]
        def blocked(*args,**kwargs):
            publication=original(*args,**kwargs)
            entered.set();release.wait(2)
            return publication
        before=session['frameSequence']
        with patch.object(recorder,'record_frame',side_effect=blocked):
            thread=threading.Thread(target=lambda:result.append(self.lab.publish_frame(
                handle.session_id,b'<svg>racing</svg>','image/svg+xml',400,800)))
            thread.start();self.assertTrue(entered.wait(1))
            with session['lock']:
                session['_androidAdmissionOpen']=False
                session['state']='connecting'
            release.set();thread.join(2)
        self.assertEqual(result,[False])
        self.assertEqual(session['state'],'connecting')
        self.assertEqual(session['frameSequence'],before)
        session['state']='active';session['_androidAdmissionOpen']=True
        self.service.stop(handle)
