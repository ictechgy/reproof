import copy
import json
import threading
import time
import unittest

from reproloop import contracts
from reproloop.qualification import QualificationError
from reproloop.live.issue_sessions import IssueSessionError
from tests.g4_support import (G4Environment, SECRET,
                              SnapshotObservationAdapter, qualification,
                              runtime_policy, specification)


class ScenarioRunnerTests(unittest.TestCase):
    def setUp(self):
        self.env=G4Environment();self.original=self.env.record_original()
        self.approved=self.env.approve(self.original)

    def tearDown(self):self.env.close()

    def test_prepared_original_approved_replay_uses_locator_and_hides_secret(self):
        execution=self.env.registry.original_execution(self.approved)
        result=self.env.service.replay(
            execution,registration=self.env.registration,device_id="device",
            owner="owner",controller_id="replay",preparations=self.env.preparations())
        public=result.public()
        self.assertEqual(public["verdict"],"observed")
        self.assertEqual((public["injection"],public["observation"]),
                         ("injected","observed"))
        self.assertEqual((public["defect"],public["expected"]),(True,False))
        self.assertEqual(public["coverage"],"complete")
        self.assertEqual(public["cleanup"],"complete")
        self.assertEqual(self.env.control["last_text"],SECRET)
        self.assertEqual(self.env.control["operation_ids"][-2:],
                         [item["operationId"] for item in public["receipts"]])
        attempt=self.env.lab.release_recording(
            next(item["recordingId"] for item in self.env.service.list()
                 if item.get("recordingDigest")==public["attemptRecordingDigest"]),"owner")
        self.assertEqual(attempt["original"]["events"][0]["operationId"],
                         public["receipts"][0]["operationId"])
        locator_digests={item["locatorEvidenceDigest"]
                         for item in public["receipts"]}
        self.assertEqual(locator_digests,
                         {item["digest"] for item in
                          attempt["original"]["observations"][:2]})
        self.assertNotIn("target",attempt["original"]["events"][0]["input"])
        for path in self.env.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET,path.read_bytes().decode("utf-8","ignore"),str(path))

    def test_swipe_long_press_and_system_actions_use_bounded_wait(self):
        handle=self.env.service.start_prepared_recording(
            device_id="device",owner="owner",controller_id="system_record",
            registration=self.env.registration,application_id="ios_app",
            build_id="original",preparations=self.env.preparations())
        session=self.env.lab.get_session(handle.session_id,"owner")
        frame=self.env.lab.frame(handle.session_id,"owner")
        geometry={"width":frame["width"],"height":frame["height"],"rotation":0,
                  "version":frame["geometryVersion"],
                  "frameDigest":frame["objectDigest"]}
        pairs=[
            ("long_press",{"x":.5,"y":.5,"durationMs":50},
             {"action":"long-press","parameters":{"x":.5,"y":.5,"durationMs":50},
              "geometry":geometry}),
            ("swipe",{"fromX":.1,"fromY":.2,"toX":.8,"toY":.9,"durationMs":50},
             {"action":"swipe","parameters":{"x":.1,"y":.2,"x2":.8,"y2":.9,
                                                "durationMs":50},"geometry":geometry}),
            ("home",{}, {"action":"home","parameters":{}}),
            ("back",{}, {"action":"back","parameters":{}}),
            ("rotate",{"orientation":"landscape-left"},
             {"action":"rotate","parameters":{"orientation":"landscape-left"}}),
            ("launch",{"applicationId":"ios_app"},
             {"action":"launch","parameters":{"applicationId":"ios_app"}}),
            ("terminate",{"applicationId":"ios_app"},
             {"action":"terminate","parameters":{"applicationId":"ios_app"}}),
        ]
        for sequence,(action,payload,typed) in enumerate(pairs,1):
            self.env.service.input(handle,{
                "controllerId":session["controllerId"],"epoch":session["epoch"],
                "sequence":sequence,"commandId":f"system_command_{sequence}",
                "frameId":frame["id"],"geometryVersion":frame["geometryVersion"],
                "action":action,"payload":payload},recording_input=typed)
            frame=self.env.lab.frame(handle.session_id,"owner")
        original=self.env.service.stop(handle)["recording"]
        spec=specification(original,revision=3)
        spec["actions"]=[{"eventId":event["id"],**copy.deepcopy(event["input"])}
                         for event in original["original"]["events"]]
        spec["waits"]=[{"afterEventId":"event_1","durationMs":100}]
        spec["bindings"]=[]
        q=qualification(self.env.project,original,spec,self.env.plan)
        approved=self.env.registry.register(
            self.env.registration,original,spec,q,runtime_policy(),
            fixture_plans=(self.env.plan,))
        started=time.monotonic()
        result=self.env.service.replay(
            self.env.registry.original_execution(approved),
            registration=self.env.registration,device_id="device",owner="owner",
            controller_id="system_replay",preparations=self.env.preparations())
        self.assertGreaterEqual(time.monotonic()-started,.09)
        self.assertEqual(result.public()["injection"],"injected")

    def test_unsupported_continuous_coverage_is_unknown(self):
        original=self.original
        spec=specification(original,revision=2,observation_class="continuous")
        q=qualification(self.env.project,original,spec,self.env.plan)
        approved=self.env.registry.register(
            self.env.registration,original,spec,q,runtime_policy(),
            fixture_plans=(self.env.plan,))
        result=self.env.service.replay(
            self.env.registry.original_execution(approved),
            registration=self.env.registration,device_id="device",owner="owner",
            controller_id="continuous",preparations=self.env.preparations())
        self.assertEqual(result.public()["verdict"],"unknown")
        self.assertEqual(result.public()["coverage"],"unknown")

    def test_unsupported_locator_capability_fails_closed_and_cleans_up(self):
        self.env.lab.devices["device"]["capabilities"]["locatorKinds"]=[]
        result=self.env.service.replay(
            self.env.registry.original_execution(self.approved),
            registration=self.env.registration,device_id="device",owner="owner",
            controller_id="unsupported_locator",
            preparations=self.env.preparations())
        self.assertEqual(result.public()["verdict"],"failed")
        self.assertEqual(result.public()["cleanup"],"complete")
        self.assertEqual(self.env.lab.list_devices()[0]["state"],"available")

    def test_changed_bytes_same_revision_are_rejected(self):
        changed=copy.deepcopy(self.approved.specification)
        changed["actions"][0]["target"]["value"]="different"
        q=qualification(self.env.project,self.original,changed,self.env.plan)
        with self.assertRaises(QualificationError) as caught:
            self.env.registry.register(
                self.env.registration,self.original,changed,q,runtime_policy(),
                fixture_plans=(self.env.plan,))
        self.assertEqual(caught.exception.code,"specification_changed")

    def test_replay_fixture_payload_must_match_original_receipts(self):
        with self.assertRaises(IssueSessionError) as caught:
            self.env.service.replay(
                self.env.registry.original_execution(self.approved),
                registration=self.env.registration,device_id="device",owner="owner",
                controller_id="changed_fixture",
                preparations=self.env.preparations({"account":"different"}))
        self.assertEqual(caught.exception.code,"fixture_binding")

    def test_candidate_requires_process_local_approval_and_changes_only_build(self):
        build=next(item for item in self.env.project['builds'] if item['id']=='candidate')
        build_digest=contracts.digest(build)
        candidate={"schemaVersion":1,
                   "qualificationDigest":self.approved.qualification_digest,
                   "sourceBuildId":"candidate","candidateBuildDigest":build_digest,
                   "originalRecordingDigest":self.approved.recording_digest,
                   "specificationDigest":self.approved.specification_digest}
        with self.assertRaises(QualificationError):
            self.env.registry.candidate_execution(self.approved,candidate,None)
        approval=contracts.issue_substitution_approval(
            qualification_digest=self.approved.qualification_digest,
            recording_digest=self.approved.recording_digest,
            specification_digest=self.approved.specification_digest,
            candidate_build_id="candidate",candidate_build_digest=build_digest)
        execution=self.env.registry.candidate_execution(
            self.approved,candidate,approval)
        self.assertEqual(execution.build_id,"candidate")
        self.assertEqual(execution.approved.qualification_digest,self.approved.qualification_digest)
        self.env.lab.devices["device"]["capabilities"]["applicationIdentity"][
            "artifactDigest"]="9"*64
        result=self.env.service.replay(
            execution,registration=self.env.registration,device_id="device",
            owner="owner",controller_id="candidate",
            preparations=self.env.preparations())
        self.assertEqual(result.public()["phase"],"candidate")
        self.assertNotEqual(result.public()["verdict"],"verified")

    def test_cancellation_before_first_action_is_retained(self):
        handle=self.env.service.start_prepared_recording(
            device_id="device",owner="owner",controller_id="cancelled",
            registration=self.env.registration,application_id="ios_app",
            build_id="original",preparations=self.env.preparations())
        current=self.env.lab.get_session(handle.session_id,"owner")
        cancel=threading.Event();cancel.set()
        result=self.env.runner.run(
            self.env.registry.original_execution(self.approved),handle.session_id,
            "owner",current["controllerId"],current["epoch"],cancellation=cancel)
        stopped=self.env.service.stop(handle,cancelled=True)
        result=self.env.runner.finalize(
            result,cleanup="complete",
            attempt_recording_digest=stopped["recording"]["recordingDigest"])
        self.assertEqual(result.public()["verdict"],"cancelled")


class CoverageBehaviorTests(unittest.TestCase):
    def test_all_three_valued_predicate_pairs_execute(self):
        class PairObservationAdapter(SnapshotObservationAdapter):
            pair=(None,None)
            def observe(self, request):
                evidence=super().observe(request)
                values={}
                for key,value in zip(("defect_state","expected_state"),
                                     self.pair):
                    if value is not None:
                        values[key]="yes" if value else "no"
                return type(evidence)(evidence.envelope,values)

        pairs=((True,True),(True,False),(True,None),
               (False,True),(False,False),(False,None),
               (None,True),(None,False),(None,None))
        for revision,pair in enumerate(pairs,10):
            adapter=PairObservationAdapter();env=G4Environment(adapter)
            try:
                with self.subTest(pair=pair):
                    original=env.record_original()
                    spec=specification(original,revision=revision)
                    for assertion,property_name in zip(
                            spec["assertions"],
                            ("defect_state","expected_state")):
                        assertion["coverage"]["properties"]=[
                            "defect_state","expected_state"]
                        assertion["predicate"]["property"]=property_name
                        assertion["predicate"]["value"]="yes"
                    approved=env.registry.register(
                        env.registration,original,spec,
                        qualification(env.project,original,spec,env.plan),
                        runtime_policy(),fixture_plans=(env.plan,))
                    adapter.pair=pair
                    result=env.service.replay(
                        env.registry.original_execution(approved),
                        registration=env.registration,device_id="device",
                        owner="owner",controller_id=f"pair_{revision}",
                        preparations=env.preparations())
                    self.assertEqual((result.defect,result.expected),pair)
                    self.assertEqual(result.public()["verdict"],
                                     "unknown" if None in pair else "observed")
            finally:env.close()

    def test_observation_deadline_is_bounded_and_yields_unknown(self):
        class SlowObservationAdapter(SnapshotObservationAdapter):
            def observe(self, request):
                time.sleep(.4)
                return super().observe(request)

        env=G4Environment(SlowObservationAdapter())
        try:
            original=env.record_original();approved=env.approve(original)
            started=time.monotonic()
            result=env.service.replay(
                env.registry.original_execution(approved),
                registration=env.registration,device_id="device",owner="owner",
                controller_id="deadline",preparations=env.preparations(),
                timeout_seconds=.1)
            self.assertLess(time.monotonic()-started,.35)
            self.assertEqual(result.public()["verdict"],"unknown")
            self.assertEqual(result.public()["coverage"],"unknown")
        finally:env.close()

    def test_sampled_and_continuous_contracts_execute_when_registered(self):
        for observation_class in ("sampled","continuous"):
            env=G4Environment(SnapshotObservationAdapter(
                classes=(observation_class,)))
            try:
                original=env.record_original()
                approved=env.approve(original,revision=2,
                                     observation_class=observation_class)
                result=env.service.replay(
                    env.registry.original_execution(approved),
                    registration=env.registration,device_id="device",owner="owner",
                    controller_id="coverage",preparations=env.preparations())
                self.assertEqual(result.public()["coverage"],"complete")
                self.assertEqual(result.public()["verdict"],"observed")
            finally:env.close()

    def test_truncated_and_stale_snapshots_do_not_become_observed(self):
        for adapter in (SnapshotObservationAdapter(truncated=True),
                        SnapshotObservationAdapter(stale=True)):
            env=G4Environment(adapter)
            try:
                original=env.record_original();approved=env.approve(original)
                result=env.service.replay(
                    env.registry.original_execution(approved),
                    registration=env.registration,device_id="device",owner="owner",
                    controller_id="coverage",preparations=env.preparations())
                self.assertEqual(result.public()["coverage"],"unknown")
            finally:env.close()

    def test_contradictory_observation_does_not_succeed(self):
        env=G4Environment(SnapshotObservationAdapter(contradiction=True))
        try:
            original=env.record_original();approved=env.approve(original)
            result=env.service.replay(
                env.registry.original_execution(approved),
                registration=env.registration,device_id="device",owner="owner",
                controller_id="contradiction",preparations=env.preparations())
            self.assertEqual(result.public()["verdict"],"unknown")
        finally:env.close()


if __name__=="__main__":unittest.main()
