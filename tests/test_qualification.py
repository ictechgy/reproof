import copy
import unittest

from reproloop import contracts
from reproloop.qualification import QualificationEngine, QualificationError
from tests.g4_support import G4Environment


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.env=G4Environment();self.original=self.env.record_original()
        self.approved=self.env.approve(self.original)
        self.engine=QualificationEngine(self.env.root/"qualification",self.env.registry)

    def tearDown(self):
        self.engine.close();self.env.close()

    def replay(self, _attempt):
        return self.env.service.replay(
            self.env.registry.original_execution(self.approved),
            registration=self.env.registration,device_id="device",owner="owner",
            controller_id="qualification",preparations=self.env.preparations())

    def test_three_fixed_matching_original_attempts_are_reproduced(self):
        result=self.engine.run_original(self.approved,self.replay,
                                        campaign_id="campaign_success")
        self.assertEqual(result["verdict"],"reproduced")
        self.assertEqual([item["attempt"] for item in result["attempts"]],[1,2,3])
        self.assertNotIn("verified",str(result).lower())

    def test_failed_first_attempt_is_retained_and_cannot_be_replaced(self):
        campaign=self.engine.begin_original(self.approved,
                                            campaign_id="campaign_failure")
        self.env.observations._adapters["screen"].value="success"
        self.engine.run_attempt(campaign,self.approved,self.replay)
        self.env.observations._adapters["screen"].value="error"
        self.engine.run_attempt(campaign,self.approved,self.replay)
        final=self.engine.run_attempt(campaign,self.approved,self.replay)
        self.assertEqual(final["verdict"],"failed")
        self.assertEqual((final["attempts"][0]["defect"],
                          final["attempts"][0]["expected"]),(False,True))
        with self.assertRaises(QualificationError) as caught:
            self.engine.run_attempt(campaign,self.approved,self.replay)
        self.assertEqual(caught.exception.code,"budget_exhausted")

    def test_unknown_historical_preparation_stays_unknown_after_new_recipe(self):
        historical=copy.deepcopy(self.original)
        historical["original"]["preparation"]=[]
        historical["recordingDigest"]=contracts.digest(historical["original"])
        historical["objectDigest"]=historical["recordingDigest"]
        spec=copy.deepcopy(self.approved.specification)
        spec["revision"]=2;spec["originalRecordingDigest"]=historical["recordingDigest"]
        qualification=copy.deepcopy(self.approved.qualification)
        qualification["recordingDigest"]=historical["recordingDigest"]
        qualification["specificationDigest"]=contracts.digest(spec)
        approved=self.env.registry.register(
            self.env.registration,historical,spec,qualification,
            self.approved.runtime_policy,fixture_plans=(self.env.plan,))
        self.assertFalse(approved.preparation_known)
        engine=QualificationEngine(self.env.root/"qualification-unknown",self.env.registry)
        try:
            def replay(_):
                return self.env.service.replay(
                    self.env.registry.original_execution(approved),
                    registration=self.env.registration,device_id="device",owner="owner",
                    controller_id="historical",preparations=self.env.preparations())
            result=engine.run_original(approved,replay,campaign_id="campaign_unknown")
            self.assertEqual(result["verdict"],"unknown")
            self.assertTrue(all(not item["preparationKnown"]
                                for item in result["attempts"]))
        finally:engine.close()

    def test_empty_fixture_selection_does_not_prove_unknown_original_conditions(self):
        historical = copy.deepcopy(self.original)
        historical['original']['preparation'] = []
        historical['recordingDigest'] = contracts.digest(historical['original'])
        historical['objectDigest'] = historical['recordingDigest']
        spec = copy.deepcopy(self.approved.specification)
        spec.update(revision=2, originalRecordingDigest=historical['recordingDigest'], fixtures=[])
        qualification = copy.deepcopy(self.approved.qualification)
        qualification.update(recordingDigest=historical['recordingDigest'], specificationDigest=contracts.digest(spec), fixtureRules=[])
        approved = self.env.registry.register(self.env.registration, historical, spec, qualification,
            self.approved.runtime_policy, fixture_plans=())
        self.assertFalse(approved.preparation_known)
        engine = QualificationEngine(self.env.root / 'empty-preparation', self.env.registry)
        try:
            result = engine.run_original(approved, lambda _: self.env.service.replay(
                self.env.registry.original_execution(approved), registration=self.env.registration,
                device_id='device', owner='owner', controller_id='unprepared', preparations=()),
                campaign_id='unprepared_baseline')
            self.assertEqual(result['verdict'], 'unknown')
            self.assertTrue(all(not attempt['preparationKnown'] for attempt in result['attempts']))
        finally: engine.close()

    def test_cleanup_failure_quarantines_campaign_immediately(self):
        self.env.remote.control(fail_cleanup=True)
        campaign=self.engine.begin_original(self.approved,
                                            campaign_id="campaign_cleanup")
        self.engine.begin_attempt(campaign,self.approved)
        attempt=self.replay(1)
        result=self.engine.record_attempt(campaign,attempt)
        self.assertEqual(result["verdict"],"quarantined")
        self.assertEqual(len(result["attempts"]),1)
        with self.assertRaises(QualificationError):
            self.engine.record_attempt(campaign,attempt)

    def test_executor_failure_consumes_a_retained_slot_without_leaking(self):
        calls=[]
        def fail(attempt):
            calls.append(attempt)
            raise RuntimeError("failure containing " +
                               "g4-secret-value-never-persist")
        result=self.engine.run_original(
            self.approved,fail,campaign_id="campaign_executor_failure")
        self.assertEqual(calls,[1])
        self.assertEqual(result["verdict"],"quarantined")
        self.assertEqual(len(result["attempts"]),1)
        self.assertEqual(result["attempts"][0]["failureCode"],
                         "executor_failed")
        for path in (self.env.root/"qualification").rglob("*"):
            if path.is_file():
                self.assertNotIn("g4-secret-value-never-persist",
                                 path.read_bytes().decode("utf-8","ignore"))


if __name__=="__main__":unittest.main()
