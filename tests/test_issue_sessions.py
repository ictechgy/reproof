import json
import threading
import time
import unittest
from unittest.mock import patch

from reproloop.fixtures import FixtureError
from reproloop.live.issue_sessions import IssueSessionError, IssueSessionService
from reproloop.live.jobs import IssueSessionJobs
from reproloop.live.model import LiveError
from tests.g4_support import G4Environment, ScenarioProvider


class IssueSessionTests(unittest.TestCase):
    def setUp(self):self.env=G4Environment()
    def tearDown(self):self.env.close()

    def test_fixture_allocation_is_persisted_before_prepare_can_dispatch(self):
        service=self.env.service
        original=service.fixtures.prepare
        seen=[]
        def prepare(plan,allocation,**kwargs):
            record=json.loads((service.root/'mobile_recovery_association.json').read_bytes())
            self.assertEqual(record['fixtures'][0]['allocationId'],allocation.allocation_id)
            self.assertEqual(record['fixtures'][0]['generation'],allocation.generation)
            seen.append(allocation.allocation_id)
            return original(plan,allocation,**kwargs)
        with patch.object(service.fixtures,'prepare',side_effect=prepare):
            handle=service.start_prepared_recording(device_id='device',owner='owner',controller_id='browser',
                registration=self.env.registration,application_id='ios_app',build_id='original',
                preparations=self.env.preparations(),timeout_seconds=1,issue_id='mobile_recovery_association')
        self.assertEqual(len(seen),1)
        service.stop(handle)

    def test_restart_loads_custom_mobile_issue_ids(self):
        service=self.env.service
        handle=service.start_prepared_recording(device_id='device',owner='owner',controller_id='browser',
            registration=self.env.registration,application_id='ios_app',build_id='original',
            preparations=self.env.preparations(),timeout_seconds=1,issue_id='mobile_recovery_restart')
        service.stop(handle)
        reloaded=IssueSessionService(self.env.lab,service.fixtures,root=service.root)
        self.assertEqual(reloaded.get(handle.issue_id)['fixtures'],service.get(handle.issue_id)['fixtures'])

    def test_reservation_identity_is_persisted_before_local_reserve(self):
        service=self.env.service;original=service.fixtures.reserve;seen=[]
        def reserve(plan,**kwargs):
            record=json.loads((service.root/'mobile_reserve_intent.json').read_bytes())
            self.assertEqual(record['fixtureReservationVersion'],1)
            self.assertEqual(record['fixtureReservations'][0],
                {'fixtureId':plan.fixture_id,'allocationId':kwargs['allocation_id']})
            seen.append(kwargs['allocation_id'])
            return original(plan,**kwargs)
        with patch.object(service.fixtures,'reserve',side_effect=reserve):
            handle=service.start_prepared_recording(device_id='device',owner='owner',controller_id='browser',
                registration=self.env.registration,application_id='ios_app',build_id='original',
                preparations=self.env.preparations(),timeout_seconds=1,issue_id='mobile_reserve_intent')
        self.assertEqual(len(seen),1);service.stop(handle)

    def asynchronous_provider(self, delay):
        environment = self.env
        class DelayedProvider(ScenarioProvider):
            def start(self, session, lab):
                self.session = session; self.lab = lab; self.closed = False
                self.stopped = threading.Event()
                def frame():
                    if not self.stopped.wait(delay): self.render()
                self.thread = threading.Thread(target=frame)
                self.thread.start()
            def close(self):
                self.stopped.set(); self.thread.join(1); self.closed = True
        environment.lab.devices['device']['factory'] = lambda: DelayedProvider(environment.control)

    def test_prepared_recording_waits_for_the_actual_first_frame(self):
        self.asynchronous_provider(.15)
        started = time.monotonic()
        handle = self.env.service.start_prepared_recording(
            device_id='device', owner='owner', controller_id='browser', registration=self.env.registration,
            application_id='ios_app', build_id='original', preparations=self.env.preparations(), timeout_seconds=1)
        self.assertGreaterEqual(time.monotonic() - started, .15)
        self.assertEqual(self.env.lab.get_session(handle.session_id, 'owner')['state'], 'active')
        self.assertEqual(self.env.service.get(handle.issue_id)['state'], 'recording')
        self.env.service.stop(handle)

    def test_first_frame_timeout_cleans_the_reserved_device_and_fixture(self):
        self.asynchronous_provider(10)
        started = time.monotonic()
        with self.assertRaises(IssueSessionError) as caught:
            self.env.service.start_prepared_recording(
                device_id='device', owner='owner', controller_id='browser', registration=self.env.registration,
                application_id='ios_app', build_id='original', preparations=self.env.preparations(), timeout_seconds=.15)
        self.assertGreaterEqual(time.monotonic() - started, .15)
        self.assertLess(time.monotonic() - started, 2)
        issue = self.env.service.get(caught.exception.issue_id)
        self.assertEqual(issue['reason'], 'startup_failed')
        self.assertEqual(issue['deviceCleanup'], 'complete')
        self.assertEqual(issue['cleanup'][0]['status'], 'complete')
        self.assertEqual(self.env.lab.list_devices()[0]['state'], 'available')

    def test_first_frame_wait_honors_is_set_only_cancellation(self):
        self.asynchronous_provider(10)
        cancelled = threading.Event()
        class Cancellation:
            def is_set(self): return cancelled.is_set()
        timer = threading.Timer(.1, cancelled.set); timer.start()
        try:
            with self.assertRaises(IssueSessionError) as caught:
                self.env.service.start_prepared_recording(
                    device_id='device', owner='owner', controller_id='browser', registration=self.env.registration,
                    application_id='ios_app', build_id='original', preparations=self.env.preparations(),
                    timeout_seconds=1, cancellation=Cancellation())
            self.assertEqual(caught.exception.code, 'cancelled')
            issue = self.env.service.get(caught.exception.issue_id)
            self.assertEqual(issue['deviceCleanup'], 'complete')
            self.assertEqual(issue['cleanup'][0]['status'], 'complete')
        finally: timer.cancel(); timer.join()

    def test_device_and_fixture_are_reserved_before_delayed_preparation_finishes(self):
        result={};error={}
        def start():
            try:
                result["handle"]=self.env.service.start_prepared_recording(
                    device_id="device",owner="owner",controller_id="browser",
                    registration=self.env.registration,application_id="ios_app",
                    build_id="original",
                    preparations=self.env.preparations({"delayMs":250}),
                    timeout_seconds=1)
            except Exception as caught:error["value"]=caught
        thread=threading.Thread(target=start);thread.start()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            state=json.loads(self.env.remote.state.read_text())
            if state["operations"]:break
            time.sleep(.01)
        self.assertEqual(self.env.lab.list_devices()[0]["state"],"reserved")
        with self.assertRaises(LiveError) as busy:
            self.env.lab.create_session("device","other","other")
        self.assertEqual(busy.exception.code,"device_busy")
        with self.assertRaises(FixtureError):
            self.env.fixtures.reserve(self.env.plan,owner="other",device_id="other")
        thread.join(3);self.assertNotIn("value",error)
        self.env.service.stop(result["handle"])

    def test_stop_freezes_original_before_append_only_cleanup(self):
        original=self.env.record_original()
        digest=original["recordingDigest"]
        self.assertEqual(2,len(original["original"]["preparation"]))
        self.assertTrue(all(item["status"]=="complete"
                            for item in original["original"]["preparation"]))
        self.assertEqual(contracts_digest(original["original"]),digest)
        self.assertGreaterEqual(len(original["lifecycleReceipts"]),3)
        loaded=self.env.lab.release_recording(original["recordingId"],"owner")
        self.assertEqual(loaded["recordingDigest"],digest)
        self.assertEqual(loaded["original"],original["original"])

    def test_trusted_job_facade_runs_the_real_issue_orchestrator(self):
        original=self.env.record_original();approved=self.env.approve(original)
        jobs=IssueSessionJobs(self.env.service)
        result=jobs.run_approved_replay(
            self.env.registry.original_execution(approved),
            registration=self.env.registration,device_id="device",owner="owner",
            controller_id="job_facade",preparations=self.env.preparations())
        self.assertEqual(result.public()["verdict"],"observed")
        self.assertTrue(result.public()["attemptRecordingDigest"])

    def test_device_cleanup_failure_retains_fixture_until_producer_is_stopped(self):
        handle=self.env.service.start_prepared_recording(
            device_id="device",owner="owner",controller_id="browser",
            registration=self.env.registration,application_id="ios_app",
            build_id="original",preparations=self.env.preparations())
        self.env.control["fail_close"]=True
        stopped=self.env.service.stop(handle)
        self.assertEqual(stopped["issue"]["state"],"quarantined")
        self.assertEqual(stopped["issue"]["cleanup"][0]["status"],"unknown")
        self.assertEqual(self.env.lab.list_devices()[0]["state"],"quarantined")
        with self.assertRaises(FixtureError):
            self.env.fixtures.reserve(self.env.plan,owner="other",device_id="other")

    def test_timed_out_preparation_is_not_admitted_as_recording(self):
        with self.assertRaises(IssueSessionError) as caught:
            self.env.service.start_prepared_recording(
                device_id="device",owner="owner",controller_id="browser",
                registration=self.env.registration,application_id="ios_app",
                build_id="original",
                preparations=self.env.preparations({"delayMs":250}),
                timeout_seconds=.05)
        issue=self.env.service.get(caught.exception.issue_id)
        self.assertIsNone(issue["recordingId"])
        self.assertIn(issue["state"],{"failed","quarantined"})
        self.assertEqual(self.env.lab.list_devices()[0]["state"],"available")

    def test_preparation_failure_keeps_device_cleanup_unknown_visible(self):
        release=self.env.lab.release_device_reservation
        def fail_release(_reservation):
            raise LiveError("cleanup_failed","Synthetic cleanup failure")
        self.env.lab.release_device_reservation=fail_release
        try:
            with self.assertRaises(IssueSessionError) as caught:
                self.env.service.start_prepared_recording(
                    device_id="device",owner="owner",controller_id="browser",
                    registration=self.env.registration,application_id="ios_app",
                    build_id="original",
                    preparations=self.env.preparations({"delayMs":250}),
                    timeout_seconds=.05)
        finally:
            self.env.lab.release_device_reservation=release
        issue=self.env.service.get(caught.exception.issue_id)
        self.assertEqual(issue["state"],"quarantined")
        self.assertEqual(issue["deviceCleanup"],"unknown")
        self.assertEqual(self.env.lab.list_devices()[0]["state"],"reserved")


def contracts_digest(value):
    from reproloop import contracts
    return contracts.digest(value)


if __name__=="__main__":unittest.main()
