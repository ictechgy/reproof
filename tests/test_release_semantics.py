"""Independent replay, identity, and malformed-wire contract regressions."""
import copy
import json
from pathlib import Path
import unittest

from reproloop import contracts

FIXTURES = Path(__file__).parent / "fixtures" / "release"


def specimen(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


class ReleaseSemanticsTests(unittest.TestCase):
    def test_coverage_requires_explicit_requirements_and_trusted_current_time(self):
        observation = specimen("observation")
        observation.update(clockUncertaintyMs=0, coverage="continuous", samplesMs=[])
        requirement = {"class": "continuous", "windowMs": observation["intervalMs"],
                       "maxUncertaintyMs": 0, "maxAgeMs": 500,
                       "scope": observation["scope"], "properties": observation["properties"]}
        self.assertEqual(contracts.observation_result(
            observation, requirement, evaluatedAtMs=observation["intervalMs"]["end"]), "covered")
        for request in ("continuous", requirement):
            with self.subTest(request=type(request).__name__):
                with self.assertRaises(contracts.ContractError):
                    contracts.observation_result(observation, request)

    def test_frozen_relative_windows_bind_to_each_replay_without_mutating_spec(self):
        from reproloop.contracts.observation import bind_coverage_requirement
        relative = {"class": "continuous", "windowMs": {"start": 100, "end": 500},
                    "maxUncertaintyMs": 0, "maxAgeMs": 500, "scope": "root",
                    "properties": ["text"]}
        before = contracts.digest(relative)
        first = bind_coverage_requirement(relative, anchor_ms=1_000_000)
        second = bind_coverage_requirement(relative, anchor_ms=2_000_000)
        self.assertEqual(first["windowMs"], {"start": 1_000_100, "end": 1_000_500})
        self.assertEqual(second["windowMs"], {"start": 2_000_100, "end": 2_000_500})
        self.assertEqual(contracts.digest(relative), before)

    def test_specification_rejects_absolute_recording_dates_and_unbounded_predicate_values(self):
        for mutation in ("absolute-time", "huge-number", "huge-text"):
            value = specimen("scenario")
            assertion = value["assertions"][0]
            if mutation == "absolute-time":
                assertion["coverage"]["windowMs"] = {"start": 1_789_124_000_000,
                                                       "end": 1_789_124_003_000}
            else:
                assertion["predicate"]["value"] = 10**100 if mutation == "huge-number" else "x" * 4097
            with self.subTest(mutation=mutation):
                with self.assertRaises(contracts.ContractError):
                    contracts.validate_specification(value)

    def test_qualification_cannot_drop_used_fixtures_or_assertion_observations(self):
        for field in ("fixtureRules", "observationRequirements"):
            value = specimen("qualification")
            value[field] = []
            with self.subTest(field=field):
                with self.assertRaises(contracts.ContractError):
                    contracts.validate_qualification_bindings(
                        value, specimen("project"), specimen("evidence"), specimen("scenario"))

    def test_qualification_rejects_unknown_predicate_observations_and_event_mappings(self):
        for mutation in ("observation", "event"):
            scenario = specimen("scenario")
            if mutation == "observation":
                scenario["assertions"][0]["predicate"]["observationId"] = "unregistered"
            else:
                scenario["actions"][0]["eventId"] = "unrecorded"
                scenario["waits"] = []
            qualification = specimen("qualification")
            qualification["specificationDigest"] = contracts.digest(scenario)
            with self.subTest(mutation=mutation):
                with self.assertRaises(contracts.ContractError):
                    contracts.validate_qualification_bindings(
                        qualification, specimen("project"), specimen("evidence"), scenario)

    def test_candidate_approval_binds_the_exact_build_manifest(self):
        candidate = {"schemaVersion": 1, "qualificationDigest": "1" * 64,
                     "sourceBuildId": "candidate", "candidateBuildDigest": "4" * 64,
                     "originalRecordingDigest": "2" * 64, "specificationDigest": "3" * 64}
        approval = contracts.issue_substitution_approval(
            qualification_digest="1" * 64, recording_digest="2" * 64,
            specification_digest="3" * 64, candidate_build_id="candidate",
            candidate_build_digest="4" * 64)
        contracts.check_candidate_substitution(candidate, approval)
        candidate["candidateBuildDigest"] = "5" * 64
        with self.assertRaises(contracts.ContractError):
            contracts.check_candidate_substitution(candidate, approval)

    def test_locator_and_coordinate_targets_cannot_conflict(self):
        from reproloop.contracts.evidence import validate_input
        with self.assertRaises(contracts.ContractError):
            validate_input({"action": "tap", "parameters": {"x": 0.5, "y": 0.5},
                            "target": {"kind": "accessibility-id", "value": "submit"}})

    def test_coordinate_and_pointer_inputs_require_current_geometry(self):
        from reproloop.contracts.evidence import validate_input
        geometry = {"width": 390, "height": 844, "rotation": 0, "version": 1}
        for action, parameters in (("tap", {"x": 0.25, "y": 0.75}),
                                   ("pointer", {"phase": "down", "pointerId": 0, "x": 0.25, "y": 0.75})):
            value = {"action": action, "parameters": parameters, "geometry": geometry}
            with self.subTest(action=action):
                validate_input(value)
                value.pop("geometry")
                with self.assertRaises(contracts.ContractError):
                    validate_input(value)

    def test_launch_and_terminate_are_typed_application_operations(self):
        from reproloop.contracts.evidence import validate_input
        for action in ("launch", "terminate"):
            validate_input({"action": action, "parameters": {"applicationId": "ios_app"}})
            with self.assertRaises(contracts.ContractError):
                validate_input({"action": action, "parameters": {"command": "anything"}})

    def test_secret_product_paths_are_not_editable(self):
        for path in ("release.keystore", "keys/signing.p12", "keys/private.pem",
                     "config/.envrc", "config/credentials", "src/invalid\x01.py"):
            value = specimen("project")
            value["editablePaths"] = [path]
            with self.subTest(path=path):
                with self.assertRaises(contracts.ContractError):
                    contracts.validate_project_revision(value)

    def test_malformed_enum_types_raise_safe_contract_errors(self):
        for name, validator, key in (("observation", contracts.validate_observation, "coverage"),
                                     ("observation", contracts.validate_observation, "scope")):
            for bad in ([], {}):
                value = specimen(name)
                value[key] = bad
                with self.subTest(key=key, bad=type(bad).__name__):
                    with self.assertRaises(contracts.ContractError):
                        validator(value)

    def test_numeric_bounds_do_not_overflow_on_untrusted_json_integers(self):
        with self.assertRaises(contracts.ContractError):
            contracts.bounded_number(10**400, "number")

    def test_a_dispatch_claim_needs_a_receipt_for_the_same_operation(self):
        for mutation in ("missing", "unrelated"):
            value = specimen("evidence")
            event = value["events"][0]
            if mutation == "missing":
                event["receipt"] = None
            else:
                event["receipt"]["operationId"] = "another-operation"
            with self.subTest(mutation=mutation):
                with self.assertRaises(contracts.ContractError):
                    contracts.validate_original_evidence(value)
        value = specimen("evidence")
        event = value["events"][0]
        event.update(dispatch="unknown", receipt=None,
                     provenance={"kind": "observed", "source": "host"})
        contracts.validate_original_evidence(value)


if __name__ == "__main__":
    unittest.main()
