"""Behavioral tests for the frozen G0 release contracts."""
import copy, json, subprocess, sys, tempfile, unittest
from pathlib import Path
from reproof import contracts

ROOT=Path(__file__).parents[1]; FIXTURES=ROOT/"tests/fixtures/release"
def specimen(name): return json.loads((FIXTURES/f"{name}.json").read_text())
def runs():
    result=[]
    for attempt in range(1,7):
        original=attempt<=3
        result.append({"schemaVersion":1,"attempt":attempt,"phase":"original" if original else "candidate","defect":original,"expected":not original,"coverage":"complete","valid":True,"regression":"not-applicable" if original else "pass","cleanup":"complete"})
    return result

class ContractTests(unittest.TestCase):
    def test_complete_wire_examples_validate(self):
        project=specimen("project"); evidence=specimen("evidence"); scenario=specimen("scenario"); qualification=specimen("qualification")
        contracts.validate_project_revision(project); contracts.validate_original_evidence(evidence); contracts.validate_specification(scenario); contracts.validate_observation(specimen("observation")); contracts.validate_package_manifest(specimen("package")); contracts.validate_qualification_bindings(qualification,project,evidence,scenario)
        contracts.validate_candidate_run(specimen("candidate")); contracts.validate_build_identity(specimen("candidate-build")); contracts.validate_execution_policy(specimen("execution-policy"))
    def test_unknown_nested_fields_and_nonfinite_values_fail(self):
        value=specimen("observation"); value["limits"]["extra"]=1
        with self.assertRaises(contracts.ContractError): contracts.validate_observation(value)
        with self.assertRaises(contracts.ContractError): contracts.bounded_number(float("nan"),"number")
        with self.assertRaises(contracts.ContractError): contracts.bounded_int(True,"integer")
    def test_coverage_uses_trusted_window_freshness_and_sampling(self):
        obs=specimen("observation"); req=specimen("scenario")["assertions"][0]["coverage"]; req["windowMs"]={"start":1789124000005,"end":1789124002995}
        self.assertEqual(contracts.observation_result(obs,req,evaluatedAtMs=1789124003005),"covered")
        stale=copy.deepcopy(req); stale["maxAgeMs"]=10
        self.assertEqual(contracts.observation_result(obs,stale,evaluatedAtMs=1789124004000),"unknown")
        gap=copy.deepcopy(obs); gap["samplesMs"]=[1789124000000,1789124003000]
        self.assertEqual(contracts.observation_result(gap,req,evaluatedAtMs=1789124003005),"unknown")
        truncated=copy.deepcopy(obs); truncated.update(truncated=True,completeness="partial")
        self.assertEqual(contracts.observation_result(truncated,req,evaluatedAtMs=1789124003005),"unknown")
        continuous=copy.deepcopy(obs); continuous["coverage"]="continuous"; continuous.pop("samplesMs")
        self.assertEqual(contracts.observation_result(continuous,req,evaluatedAtMs=1789124003005),"unknown")
    def test_slow_provider_passes_its_explicit_approved_bound(self):
        obs=specimen("observation"); req=specimen("scenario")["assertions"][0]["coverage"]; req["windowMs"]={"start":1789124000005,"end":1789124002995}; req["samplingIntervalMs"]=4000; obs["samplesMs"]=[1789124000000,1789124003000]
        self.assertEqual(contracts.observation_result(obs,req,evaluatedAtMs=1789124003005),"covered")
    def test_pair_truth_table_never_returns_verified(self):
        self.assertEqual(contracts.classify_predicates(True,False,phase="original"),"match")
        self.assertEqual(contracts.classify_predicates(False,True,phase="candidate"),"match")
        for pair in ((True,True),(False,False),(False,True),(True,False),(None,True)):
            self.assertNotEqual(contracts.classify_predicates(*pair,phase="original"),"verified")
    def test_fixed_budget_rejects_gaps_replacements_and_invalid_middle(self):
        budget={"original":3,"candidate":3,"total":6}; contracts.validate_run_sequence(budget,runs())
        variants=[]
        missing=runs(); missing.pop(1); variants.append(missing)
        duplicate=runs(); duplicate[2]["attempt"]=2; variants.append(duplicate)
        extra=runs()+[copy.deepcopy(runs()[-1])]; variants.append(extra)
        invalid=runs(); invalid[4]["valid"]=False; variants.append(invalid)
        transient=runs(); transient[4]["defect"]=True; transient[4]["expected"]=True; variants.append(transient)
        for value in variants:
            with self.assertRaises(contracts.ContractError): contracts.validate_run_sequence(budget,value)
    def test_imported_data_cannot_mint_candidate_authority(self):
        candidate={"schemaVersion":1,"qualificationDigest":"1"*64,"sourceBuildId":"candidate","candidateBuildDigest":"4"*64,"originalRecordingDigest":"2"*64,"specificationDigest":"3"*64}
        approval=contracts.issue_substitution_approval(qualification_digest="1"*64,recording_digest="2"*64,specification_digest="3"*64,candidate_build_id="candidate",candidate_build_digest="4"*64)
        contracts.check_candidate_substitution(candidate,approval)
        with self.assertRaises(contracts.ContractError): contracts.check_candidate_substitution(candidate,json.loads(json.dumps(candidate)))
        changed=copy.deepcopy(candidate); changed["sourceBuildId"]="other"
        with self.assertRaises(contracts.ContractError): contracts.check_candidate_substitution(changed,approval)
    def test_late_receipt_is_separate_and_cannot_extend_sealed_recording(self):
        evidence=specimen("evidence"); before=contracts.digest(evidence)
        receipt={"schemaVersion":1,"receiptId":"late_ack","recordingDigest":before,"operationId":"operation_one","generation":1,"sequence":2,"kind":"ack","status":"complete","observedAtMs":1789124004000}
        contracts.validate_lifecycle_receipt(receipt); self.assertEqual(before,contracts.digest(evidence))
        changed=copy.deepcopy(evidence); changed["events"].append(copy.deepcopy(changed["events"][0])); changed["events"][-1]["sequence"]=2; changed["endSequence"]=2
        with self.assertRaises(contracts.ContractError): contracts.validate_original_evidence(changed)

class GateIntegrationTests(unittest.TestCase):
    def test_release_checker_runs_full_g0_and_blocks_future(self):
        proc=subprocess.run([sys.executable,"scripts/release-check.py","--goal","G0","--effects","filesystem,process"],cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(proc.returncode,0,proc.stderr); report=json.loads(proc.stdout); self.assertEqual(report["status"],"pass"); self.assertEqual(len(report["checks"]),1); self.assertGreaterEqual(report["checks"][0]["testsRun"],22)
        proc=subprocess.run([sys.executable,"scripts/release-check.py","--goal","G1","--effects","filesystem,process"],cwd=ROOT,text=True,capture_output=True)
        self.assertNotEqual(proc.returncode,0); self.assertEqual(json.loads(proc.stdout)["status"],"blocked")

class ReleaseCheckHarnessTests(unittest.TestCase):
    def invoke(self,inventory,effects="filesystem"):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"inventory.json"; path.write_text(json.dumps(inventory))
            return subprocess.run([sys.executable,"scripts/release-check.py","--inventory",str(path),"--goal","X","--effects",effects],cwd=ROOT,text=True,capture_output=True)
    def test_registry_rejects_missing_fields_effects_and_commands(self):
        base={"version":1,"goals":{"X":{"available":True,"checks":[{"id":"tiny","command":["python3","-m","unittest","tests.test_release_boundaries.ReleaseBoundaryTests.test_optional_fields_are_optional_and_unknown_fields_are_rejected"],"environment":"local-python","effects":["filesystem"],"timeoutSeconds":5,"evidence":"unittest-summary","gate":"software"}]}}}
        self.assertEqual(self.invoke(base).returncode,0)
        for key in ("command","environment","effects","timeoutSeconds","evidence","gate"):
            bad=copy.deepcopy(base); bad["goals"]["X"]["checks"][0].pop(key)
            self.assertNotEqual(self.invoke(bad).returncode,0)
        self.assertNotEqual(self.invoke(base,effects="process").returncode,0)

    def test_child_can_import_existing_test_helpers(self):
        inventory={"version":1,"goals":{"X":{"available":True,"checks":[{
            "id":"test-helper-import",
            "command":["python3","-m","unittest",
                       "tests.test_storage.StorageTests.test_duplicate_json_keys_rejected"],
            "environment":"local-python","effects":["filesystem"],"timeoutSeconds":5,
            "evidence":"unittest-summary","gate":"software"}]}}}
        result=self.invoke(inventory)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=="__main__": unittest.main()
