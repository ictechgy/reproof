"""A fixed attempt budget survives process death and rejects reused runs."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from reproof import contracts
from reproof.qualification import QualificationEngine, QualificationError, ScenarioRegistry
from tests.g4_support import G4Environment
from tests.test_recording_recovery import collection_policy, open_store, project_document


def open_offline_engine(root):
    budget, evidence, recordings, _ = open_store(root / "recordings")
    project = project_document()
    registration = recordings.register_project(project, collection_policy())
    fixture_root = Path(__file__).parent / "fixtures" / "release"
    def read(name):
        return json.loads((fixture_root / f"{name}.json").read_text())
    original, spec, policy, qualification = map(read, (
        "evidence", "scenario", "execution-policy", "qualification"))
    spec["fixtures"] = []
    qualification.update(projectDigest=contracts.digest(project),
                         recordingDigest=contracts.digest(original),
                         specificationDigest=contracts.digest(spec),
                         runtimePolicyDigest=contracts.digest(policy), fixtureRules=[])
    registry = ScenarioRegistry(root / "specs")
    approved = registry.register(registration, {
        "original": original, "recordingDigest": contracts.digest(original)},
        spec, qualification, policy)
    engine = QualificationEngine(root / "qualification", registry)
    return engine, approved, (engine, registry, recordings, evidence, budget)


def crash_child(root):
    engine, approved, _ = open_offline_engine(root)
    def execute(_):
        with (root / "callback-entered").open("x") as marker:
            marker.write("entered\n"); marker.flush(); os.fsync(marker.fileno())
        os._exit(37)
    engine.run_original(approved, execute, campaign_id="crash")


class QualificationAttemptBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.env = G4Environment()
        self.approved = self.env.approve(self.env.record_original())
        self.engine = QualificationEngine(self.env.root / "qualification", self.env.registry)

    def tearDown(self):
        self.engine.close(); self.env.close()

    def replay(self, _):
        return self.env.service.replay(
            self.env.registry.original_execution(self.approved),
            registration=self.env.registration, device_id="device", owner="owner",
            controller_id="attempt", preparations=self.env.preparations())

    def test_attempt_is_durable_before_the_executor_starts(self):
        observed = []
        def execute(number):
            with closing(sqlite3.connect(self.env.root / "qualification" / "qualification.sqlite3")) as db:
                observed.append(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
            return self.replay(number)
        result = self.engine.run_original(self.approved, execute)
        self.assertEqual(observed, [1, 2, 3])
        self.assertEqual(result["verdict"], "reproduced")

    def test_result_without_prior_admission_cannot_fill_a_slot(self):
        campaign = self.engine.begin_original(self.approved)
        result = self.replay(1)
        with self.assertRaises(QualificationError):
            self.engine.record_attempt(campaign, result)
        self.assertEqual(self.engine.get(campaign)["attempts"], [])

    def test_one_run_cannot_fill_three_attempt_slots(self):
        results = []
        def execute(number):
            if not results:
                results.append(self.replay(number))
            return results[0]
        result = self.engine.run_original(self.approved, execute)
        self.assertEqual(result["verdict"], "quarantined")
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(sum(item["valid"] for item in result["attempts"]), 1)

    def test_a_run_completed_before_admission_is_not_a_fresh_attempt(self):
        old = self.replay(1)
        result = self.engine.run_original(self.approved, lambda _: old)
        self.assertEqual(result["verdict"], "quarantined")
        self.assertFalse(result["attempts"][0]["valid"])

    def test_second_writer_cannot_recover_an_active_engine(self):
        other = None
        try:
            with self.assertRaises(QualificationError):
                other = QualificationEngine(self.engine.root, self.env.registry)
        finally:
            if other is not None:
                other.close()

    def test_process_death_retains_an_unknown_attempt_and_closes_budget(self):
        with tempfile.TemporaryDirectory(prefix="g4-attempt-crash-") as directory:
            root = Path(directory)
            child = subprocess.run([sys.executable, "-m", __name__, "--crash-child", str(root)],
                                   cwd=Path(__file__).resolve().parents[1],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 37, child.stderr)
            self.assertTrue((root / "callback-entered").exists())
            engine, approved, stores = open_offline_engine(root)
            try:
                campaign = engine.begin_original(approved, campaign_id="crash")
                result = engine.get(campaign)
                self.assertEqual(len(result["attempts"]), 1)
                self.assertEqual(result["attempts"][0]["attempt"], 1)
                self.assertFalse(result["attempts"][0]["valid"])
                self.assertEqual(result["verdict"], "quarantined")
                calls = []
                engine.run_original(approved, lambda n: calls.append(n), campaign_id="crash")
                self.assertEqual(calls, [])
            finally:
                for store in stores:
                    store.close()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--crash-child"]:
        crash_child(Path(sys.argv[2]))
    else:
        unittest.main()
