"""Independent acceptance checks for the release data/trust boundaries."""
import copy
import json
from pathlib import Path
import unittest

from reproloop import contracts
from reproloop.contracts.versions import exact


FIXTURES = Path(__file__).parent / "fixtures" / "release"


def specimen(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


class ReleaseBoundaryTests(unittest.TestCase):
    def test_optional_fields_are_optional_and_unknown_fields_are_rejected(self):
        exact({"required": 1}, ("required",), ("optional",))
        exact({"required": 1, "optional": 2}, ("required",), ("optional",))
        for value in [{}, {"optional": 2}, {"required": 1, "extra": 3}]:
            with self.assertRaises(contracts.ContractError):
                exact(value, ("required",), ("optional",))

    def test_project_rejects_ambiguous_application_and_build_ids(self):
        for field in ("applications", "builds"):
            with self.subTest(field=field):
                value = specimen("project")
                value[field].append(copy.deepcopy(value[field][0]))
                with self.assertRaises(contracts.ContractError):
                    contracts.validate_project_revision(value)

    def test_real_source_paths_are_supported_without_a_product_prefix(self):
        value = specimen("project")
        value["editablePaths"] = ["src/main/kotlin/com/example/Checkout.kt"]
        contracts.validate_project_revision(value)

    def test_source_paths_reject_traversal_backslashes_and_secrets(self):
        for path in ("../escape.py", "/absolute.py", "src/../escape.py",
                     "src\\escape.py", "src//Main.kt", "src/./Main.kt",
                     "src/\x00Main.kt", ".env", "src/.env.production", "auth.json"):
            with self.subTest(path=path):
                value = specimen("project")
                value["editablePaths"] = [path]
                with self.assertRaises(contracts.ContractError):
                    contracts.validate_project_revision(value)

    def test_variable_defaults_match_their_declared_types(self):
        for kind, default in (("integer", "12"), ("integer", True),
                              ("boolean", 1), ("string", 1)):
            with self.subTest(kind=kind, default=default):
                value = specimen("project")
                value["variables"].append({"id": "bad_default", "type": kind,
                                           "secret": False, "default": default})
                with self.assertRaises(contracts.ContractError):
                    contracts.validate_project_revision(value)

    def test_fixture_requires_declared_external_side_effect_capabilities(self):
        value = specimen("project")
        fixture = value["fixtures"][0]
        fixture.pop("capabilities", None)
        with self.assertRaises(contracts.ContractError):
            contracts.validate_project_revision(value)

    def test_recording_accepts_current_epoch_milliseconds(self):
        value = specimen("evidence")
        value["startedAtMs"] = 1_789_124_000_000
        value["preparation"] = []
        contracts.validate_original_evidence(value)

    def test_recording_rejects_non_receipts_and_nested_unknown_data(self):
        for field, invalid in (("preparation", [None]), ("preparation", ["prepared"]),
                               ("preparation", [{"actual": True, "command": "unexpected"}]),
                               ("observations", [{"unexpected": "raw data"}]),
                               ("media", [{"path": "../outside"}])):
            with self.subTest(field=field, invalid=invalid):
                value = specimen("evidence")
                value[field] = invalid
                with self.assertRaises(contracts.ContractError):
                    contracts.validate_original_evidence(value)

    def test_scenario_does_not_accept_an_arbitrary_operation(self):
        value = specimen("scenario")
        value["actions"][0]["action"] = "shell"
        value["actions"][0]["parameters"] = {"command": "unexpected"}
        with self.assertRaises(contracts.ContractError):
            contracts.validate_specification(value)

    def test_scenario_cannot_persist_raw_text_instead_of_a_variable(self):
        value = specimen("scenario")
        value["actions"][0]["action"] = "text"
        value["actions"][0]["parameters"] = {"value": "synthetic-secret"}
        with self.assertRaises(contracts.ContractError):
            contracts.validate_specification(value)

    def test_package_object_references_are_real_digests(self):
        value = specimen("package")
        value["objects"] = ["z" * 64]
        with self.assertRaises(contracts.ContractError):
            contracts.validate_package_manifest(value)

    def test_budget_total_is_a_strict_integer(self):
        with self.assertRaises(contracts.ContractError):
            contracts.validate_attempt_budget({"original": 3, "candidate": 3, "total": 6.0})

    def test_predicate_pair_alone_cannot_establish_verified(self):
        self.assertNotEqual(contracts.classify_predicates(False, True, phase="candidate"),
                            "verified")

    def test_imported_boolean_claims_do_not_authorize_candidate_execution(self):
        value = {"schemaVersion": 1, "qualificationDigest": "1" * 64,
                 "sourceBuildId": "candidate", "substitutionAuthorized": True,
                 "originalRecordingDigest": "2" * 64, "specificationDigest": "3" * 64,
                 "regressionEvidence": True}
        with self.assertRaises(contracts.ContractError):
            contracts.validate_candidate_run(value)

    def test_predicate_pair_requires_actual_booleans(self):
        for defect, expected in ((1, 0), (0, 1), (1.0, 0.0), (False, 1)):
            with self.subTest(defect=defect, expected=expected):
                try:
                    outcome = contracts.classify_predicates(defect, expected, phase="candidate")
                except contracts.ContractError:
                    continue
                self.assertNotEqual(outcome, "match")

    def test_swipe_requires_its_endpoint_and_bounded_duration(self):
        from reproloop.contracts.evidence import validate_input
        for parameters in ({"x": 10, "y": 20},
                           {"x": 10, "y": 20, "x2": 30, "y2": 40},
                           {"x": 10, "y": 20, "x2": 30, "y2": 40,
                            "durationMs": 6000}):
            with self.subTest(parameters=parameters):
                with self.assertRaises(contracts.ContractError):
                    validate_input({"action": "swipe", "parameters": parameters})

    def test_locator_tap_does_not_require_stale_coordinates(self):
        from reproloop.contracts.evidence import validate_input
        validate_input({"action": "tap", "parameters": {},
                        "target": {"kind": "accessibility-id", "value": "checkout"}})

    def test_presence_predicate_reports_contract_error_for_missing_fields(self):
        value = specimen("scenario")
        value["assertions"][0]["predicate"] = {"kind": "property"}
        with self.assertRaises(contracts.ContractError):
            contracts.validate_specification(value)

    def test_package_contains_its_referenced_recording_and_specification(self):
        value = specimen("package")
        value["objects"] = ["f" * 64]
        with self.assertRaises(contracts.ContractError):
            contracts.validate_package_manifest(value)

    def test_observation_cannot_self_approve_its_freshness(self):
        from reproloop.contracts.observation import observation_result
        value = specimen("observation")
        try:
            result = observation_result(value, value["coverage"])
        except contracts.ContractError:
            return
        self.assertNotEqual(result, "covered")

    def test_sampled_coverage_requires_actual_samples(self):
        from reproloop.contracts.observation import observation_result
        value = specimen("observation")
        value.update(intervalMs={"start": 1000, "end": 1100},
                     clockUncertaintyMs=0, coverage="sampled", samplesMs=[],
                     errors=[], truncated=False, completeness="complete")
        requirement = {"class": "sampled", "windowMs": {"start": 1000, "end": 1100},
                       "maxUncertaintyMs": 0, "maxAgeMs": 500, "scope": value["scope"],
                       "properties": value["properties"], "samplingIntervalMs": 100}
        self.assertEqual(observation_result(value, requirement, evaluatedAtMs=1100),
                         "unknown")

    def test_observation_from_the_future_cannot_satisfy_a_current_check(self):
        from reproloop.contracts.observation import observation_result
        value = specimen("observation")
        value.update(intervalMs={"start": 1000, "end": 1100},
                     clockUncertaintyMs=0, coverage="continuous", samplesMs=[],
                     errors=[], truncated=False, completeness="complete")
        requirement = {"class": "continuous", "windowMs": {"start": 1000, "end": 1100},
                       "maxUncertaintyMs": 0, "maxAgeMs": 500, "scope": value["scope"],
                       "properties": value["properties"]}
        self.assertEqual(observation_result(value, requirement, evaluatedAtMs=1000),
                         "unknown")

    def test_qualification_checks_the_original_project_identity(self):
        from reproloop.contracts.scenario import validate_qualification_bindings
        from reproloop.contracts.versions import digest
        for field, wrong in (("projectId", "different-project"),
                             ("projectRevision", "different-revision")):
            with self.subTest(field=field):
                project = specimen("project")
                evidence = specimen("evidence")
                scenario = specimen("scenario")
                qualification = specimen("qualification")
                evidence[field] = wrong
                scenario["originalRecordingDigest"] = digest(evidence)
                qualification["recordingDigest"] = digest(evidence)
                qualification["specificationDigest"] = digest(scenario)
                with self.assertRaises(contracts.ContractError):
                    validate_qualification_bindings(qualification, project, evidence, scenario)


if __name__ == "__main__":
    unittest.main()
