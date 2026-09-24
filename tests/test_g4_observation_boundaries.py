"""Actual property evidence and late cancellation constrain replay verdicts."""
import copy
import threading
import time
import unittest
from unittest.mock import patch

from reproof.scenario_runner import ObservationEvidence, StaticVariableResolver
from reproof.fixtures import FixtureError
from tests.g4_support import G4Environment, ScenarioProvider, SnapshotObservationAdapter, qualification, runtime_policy, specification


class ObservationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.env = G4Environment()
        self.original = self.env.record_original()

    def tearDown(self):
        self.env.close()

    def approve(self, observation_class="snapshot", window=0, operator=None):
        spec = specification(self.original, observation_class=observation_class)
        for assertion in spec["assertions"]:
            assertion["coverage"]["windowMs"]["end"] = window
            assertion.update(windowMs=window, stabilityMs=window)
            if operator:
                assertion["predicate"]["operator"] = operator
                assertion["predicate"].pop("value", None)
        return self.env.registry.register(
            self.env.registration, self.original, spec,
            qualification(self.env.project, self.original, spec, self.env.plan),
            runtime_policy(), fixture_plans=(self.env.plan,))

    def replay(self, approved, **kwargs):
        return self.env.service.replay(
            self.env.registry.original_execution(approved), registration=self.env.registration,
            device_id="device", owner="owner", controller_id="observations",
            preparations=self.env.preparations(), **kwargs)

    def test_missing_property_samples_cannot_borrow_complete_metadata(self):
        class Sparse(SnapshotObservationAdapter):
            def observe(self, request):
                base = super().observe(request)
                start, end = (request.requirement["windowMs"][key] for key in ("start", "end"))
                time.sleep(max(0, end / 1000 - time.time()))
                envelope = copy.deepcopy(base.envelope)
                envelope["samplesMs"] = list(range(start, end + 1, 100))
                return ObservationEvidence(envelope, {"text": [(end, "error")]})
        self.env.observations._adapters["screen"] = Sparse(classes=("sampled",))
        result = self.replay(self.approve("sampled", 300))
        self.assertEqual(result.coverage, "unknown")
        self.assertEqual(result.verdict, "unknown")
        self.assertIsNone(result.defect)

    def delayed_snapshot(self, tolerance):
        class ActualDelayedSnapshot(SnapshotObservationAdapter):
            def observe(self, request):
                time.sleep(.025)
                captured = int(time.time() * 1000)
                base = super().observe(request)
                base.envelope['intervalMs'] = {'start': captured, 'end': captured}
                return base
        self.env.observations._adapters['screen'] = ActualDelayedSnapshot()
        spec = specification(self.original)
        for assertion in spec['assertions']:
            assertion['coverage']['maxUncertaintyMs'] = tolerance
        approved = self.env.registry.register(self.env.registration, self.original, spec,
            qualification(self.env.project, self.original, spec, self.env.plan), runtime_policy(), fixture_plans=(self.env.plan,))
        return self.replay(approved)

    def test_snapshot_predicate_uses_the_explicitly_approved_timing_tolerance(self):
        result = self.delayed_snapshot(1000)
        self.assertEqual(result.coverage, 'complete')
        self.assertEqual(result.verdict, 'observed')
        self.assertEqual((result.defect, result.expected), (True, False))

    def test_snapshot_outside_approved_tolerance_remains_unknown(self):
        result = self.delayed_snapshot(5)
        self.assertEqual(result.coverage, 'unknown')
        self.assertEqual(result.verdict, 'unknown')
        self.assertIsNone(result.defect)

    def test_continuous_endpoint_scalar_cannot_prove_interval_stability(self):
        class Endpoint(SnapshotObservationAdapter):
            def observe(self, request):
                time.sleep(max(0, request.requirement["windowMs"]["end"] / 1000 - time.time()))
                return super().observe(request)
        self.env.observations._adapters["screen"] = Endpoint(classes=("continuous",))
        result = self.replay(self.approve("continuous", 100))
        self.assertEqual(result.coverage, "unknown")
        self.assertIsNone(result.defect)

    def test_missing_property_is_unknown_for_absence(self):
        class Missing(SnapshotObservationAdapter):
            def observe(self, request):
                return ObservationEvidence(super().observe(request).envelope, {})
        self.env.observations._adapters["screen"] = Missing()
        result = self.replay(self.approve(operator="absent"))
        self.assertIsNone(result.defect)
        self.assertIsNone(result.expected)
        self.assertEqual(result.verdict, "unknown")

    def test_cancel_during_the_only_observation_remains_cancelled(self):
        cancel = threading.Event()
        class Cancelling(SnapshotObservationAdapter):
            def observe(self, request):
                value = super().observe(request)
                cancel.set()
                return value
        self.env.observations._adapters["screen"] = Cancelling()
        result = self.replay(self.approve(), cancellation=cancel)
        self.assertTrue(cancel.is_set())
        self.assertEqual(result.verdict, "cancelled")
        self.assertFalse(result.valid)
        self.assertEqual(result.cleanup, "complete")

    def test_secret_with_json_escapes_is_rejected_before_persistence(self):
        secret = 'test-secret-"quoted"-\n-line'
        self.env.variables._resolvers["secret_text"] = StaticVariableResolver(secret)
        self.env.observations._adapters["screen"] = SnapshotObservationAdapter(secret)
        result = self.replay(self.approve())
        self.assertEqual(result.verdict, "unknown")
        self.assertTrue(all(item["digest"] is None for item in result.observations))

    def test_explicit_absence_can_be_observed(self):
        class Absent(SnapshotObservationAdapter):
            def observe(self, request):
                return ObservationEvidence(super().observe(request).envelope, {}, ("text",))
        self.env.observations._adapters["screen"] = Absent()
        result = self.replay(self.approve(operator="absent"))
        self.assertEqual(result.verdict, "observed")
        self.assertTrue(result.defect)
        self.assertTrue(all(item["digest"] for item in result.observations))

    def test_complete_sample_values_prove_sampled_stability(self):
        class Samples(SnapshotObservationAdapter):
            def observe(self, request):
                base = super().observe(request)
                start, end = (request.requirement["windowMs"][key] for key in ("start", "end"))
                time.sleep(max(0, end / 1000 - time.time()))
                envelope = copy.deepcopy(base.envelope)
                envelope["samplesMs"] = list(range(start, end + 1, 100))
                return ObservationEvidence(envelope, {"text": [(t, "error") for t in envelope["samplesMs"]]})
        self.env.observations._adapters["screen"] = Samples(classes=("sampled",))
        result = self.replay(self.approve("sampled", 300))
        self.assertEqual(result.verdict, "observed")
        self.assertEqual((result.defect, result.expected), (True, False))

    def test_continuous_value_at_stability_start_is_not_discarded(self):
        class Changes(SnapshotObservationAdapter):
            def observe(self, request):
                start, end = (request.requirement["windowMs"][key] for key in ("start", "end"))
                time.sleep(max(0, end / 1000 - time.time()))
                return ObservationEvidence(super().observe(request).envelope,
                    {"text": [(start, "success"), (start + 250, "error"), (end, "error")]})
        self.env.observations._adapters["screen"] = Changes(classes=("continuous",))
        spec = specification(self.original, observation_class="continuous")
        for assertion in spec["assertions"]:
            assertion["coverage"]["windowMs"]["end"] = 300
            assertion.update(windowMs=300, stabilityMs=100)
        approved = self.env.registry.register(
            self.env.registration, self.original, spec,
            qualification(self.env.project, self.original, spec, self.env.plan),
            runtime_policy(), fixture_plans=(self.env.plan,))
        result = self.replay(approved)
        self.assertEqual(result.verdict, "observed")
        self.assertEqual((result.defect, result.expected), (False, False))

    def test_variable_resolution_obeys_the_replay_deadline(self):
        class Slow:
            def resolve(self, context):
                time.sleep(.4)
                return "value"
        self.env.variables._resolvers["secret_text"] = Slow()
        before = len(self.env.control["calls"])
        started = time.monotonic()
        result = self.replay(self.approve(), timeout_seconds=.1)
        self.assertLess(time.monotonic() - started, .35)
        self.assertEqual(result.verdict, "unknown")
        self.assertEqual(len(self.env.control["calls"]), before)

    def test_slow_locator_cannot_dispatch_input_after_the_deadline(self):
        original = ScenarioProvider.resolve_locator
        def slow(provider, target):
            time.sleep(.4)
            return original(provider, target)
        before = len(self.env.control["calls"])
        started = time.monotonic()
        with patch.object(ScenarioProvider, "resolve_locator", slow):
            result = self.replay(self.approve(), timeout_seconds=.1)
        self.assertLess(time.monotonic() - started, .35)
        self.assertEqual(result.verdict, "unknown")
        self.assertEqual(len(self.env.control["calls"]), before)

    def test_secret_in_observation_metadata_is_rejected_before_persistence(self):
        secret = "private_account_marker"
        class Metadata(SnapshotObservationAdapter):
            def observe(self, request):
                evidence = super().observe(request)
                evidence.envelope["targets"] = [secret]
                return evidence
        self.env.variables._resolvers["secret_text"] = StaticVariableResolver(secret)
        self.env.observations._adapters["screen"] = Metadata()
        result = self.replay(self.approve())
        self.assertEqual(result.verdict, "unknown")
        self.assertTrue(all(item["digest"] is None for item in result.observations))

    def test_values_exceeding_the_declared_capture_bound_are_unknown(self):
        class Oversized(SnapshotObservationAdapter):
            def observe(self, request):
                evidence = super().observe(request)
                evidence.envelope["limits"]["bytes"] = 1
                return evidence
        self.env.observations._adapters["screen"] = Oversized()
        result = self.replay(self.approve())
        self.assertEqual(result.verdict, "unknown")
        self.assertTrue(all(item["digest"] is None for item in result.observations))

    def test_unreturned_input_is_bounded_and_retains_both_allocations(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = ScenarioProvider.execute_operation
        def slow(provider, *args, **kwargs):
            entered.set();release.wait(.5)
            try:
                return original(provider, *args, **kwargs)
            finally:
                finished.set()
        try:
            with patch.object(ScenarioProvider, "execute_operation", slow):
                started = time.monotonic()
                result = self.replay(self.approve(), timeout_seconds=.1)
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic() - started, .35)
            self.assertEqual(result.verdict, "quarantined")
            self.assertFalse(result.valid)
            self.assertEqual(self.env.lab.list_devices()[0]["state"], "quarantined")
            with self.assertRaises(FixtureError):
                self.env.fixtures.reserve(self.env.plan, owner="next", device_id="second")
            issue = next(item for item in self.env.service.list()
                         if item.get("recordingDigest") == result.attempt_recording_digest)
            recording = self.env.lab.release_recording(issue["recordingId"], "owner")
            self.assertEqual(len(recording["original"]["events"]), 1)
            self.assertEqual(recording["original"]["events"][0]["dispatch"], "unknown")
        finally:
            release.set();finished.wait(2)


if __name__ == "__main__":
    unittest.main()
