import copy
import unittest

from reproof.core import ContractError, digest
from reproof.live.recordings import derive_recording, validate_recording


def recording(*, events=None, **changes):
    events = events if events is not None else [
        {"id": "e1", "action": "tap", "payload": {"x": 0.25, "y": 0.5},
         "status": "injected", "offsetMs": 100, "sourceCommandId": "command-1",
         "frameId": 1, "geometryVersion": 1, "controllerMode": "manual",
         "timing": "best-effort"},
        {"id": "e2", "action": "text", "payload": {"variable": "text_1"},
         "status": "injected", "offsetMs": 300, "sourceCommandId": "command-2",
         "frameId": 2, "geometryVersion": 1, "controllerMode": "manual",
         "timing": "best-effort"},
    ]
    result = {
        "schemaVersion": 1,
        "kind": "live-gesture-recording",
        "id": "recording-1",
        "sessionId": "session-1",
        "deviceId": "device-1",
        "providerKind": "demo",
        "status": "complete",
        "replayable": True,
        "startingState": {"kind": "provider-reset"},
        "geometry": {"width": 400, "height": 800, "orientation": "portrait"},
        "events": events,
        "variables": ["text_1"],
        "media": "frame-references-only",
        "startedAt": 1000,
        "endedAt": 2000,
    }
    result.update(changes)
    result["digest"] = digest({k: v for k, v in result.items() if k != "digest"})
    return result


class RecordingContractTests(unittest.TestCase):
    def test_valid_recording_is_deep_copied_and_optional_identity_preserved(self):
        source = recording(applicationIdentity={"bundleId": "com.example.app"})
        checked = validate_recording(source)
        self.assertEqual(checked, source)
        self.assertIsNot(checked, source)
        self.assertIsNot(checked["events"], source["events"])
        checked["events"][0]["payload"]["x"] = 0.75
        self.assertEqual(source["events"][0]["payload"]["x"], 0.25)

    def test_omitted_application_identity_is_valid(self):
        self.assertNotIn("applicationIdentity", validate_recording(recording()))

    def test_corrupt_digest_and_unknown_or_private_fields_are_rejected(self):
        for change in ({"digest": "0" * 64}, {"_secret": "value"}, {"events": [{"_secret": "value"}]}):
            with self.subTest(change=change):
                value = recording()
                value.update(change)
                with self.assertRaises(ContractError):
                    validate_recording(value)

    def test_replayable_requires_complete_nonempty_provider_reset_and_frame_refs(self):
        for change in (
            {"status": "invalid", "replayable": True},
            {"events": [], "replayable": True},
            {"startingState": {"kind": "unknown"}, "replayable": True},
            {"media": "embedded-images", "replayable": True},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ContractError):
                    validate_recording(recording(**change))

    def test_complete_recording_may_conservatively_be_non_replayable(self):
        checked = validate_recording(recording(replayable=False))
        self.assertFalse(checked["replayable"])

    def test_gesture_geometry_and_event_limits_are_strict(self):
        bad_event = copy.deepcopy(recording()["events"][0])
        bad_event["payload"] = {"x": 1.1, "y": 0.5}
        with self.assertRaises(ContractError):
            validate_recording(recording(events=[bad_event]))
        too_many = [copy.deepcopy(recording()["events"][0]) for _ in range(501)]
        for n, event in enumerate(too_many):
            event["id"] = f"e{n}"
            event["offsetMs"] = n
        with self.assertRaises(ContractError):
            validate_recording(recording(events=too_many))
        bad_offset = copy.deepcopy(recording()["events"])
        bad_offset[1]["offsetMs"] = 99
        with self.assertRaises(ContractError):
            validate_recording(recording(events=bad_offset))
        bad_timing = copy.deepcopy(recording()["events"])
        bad_timing[0]["timing"] = "provider-detail"
        with self.assertRaises(ContractError):
            validate_recording(recording(events=bad_timing))

    def test_text_must_be_declared_variable_and_raw_text_is_rejected(self):
        raw = copy.deepcopy(recording()["events"])
        raw[1]["payload"] = {"value": "secret text"}
        with self.assertRaises(ContractError):
            validate_recording(recording(events=raw))
        unused = recording(variables=["text_1", "unused"])
        with self.assertRaises(ContractError):
            validate_recording(unused)
        missing = recording(variables=[])
        with self.assertRaises(ContractError):
            validate_recording(missing)

    def test_derive_scales_offsets_and_does_not_mutate_source(self):
        source = recording()
        original = copy.deepcopy(source)
        derived = derive_recording(source, speed=2.0, new_id="recording-2")
        self.assertEqual(derived["id"], "recording-2")
        self.assertEqual([event["offsetMs"] for event in derived["events"]], [50, 150])
        self.assertEqual(derived["provenance"]["sourceRecordingId"], source["id"])
        self.assertEqual(derived["provenance"]["sourceDigest"], source["digest"])
        self.assertEqual(derived["provenance"]["transform"]["eventIds"], ["e1", "e2"])
        self.assertEqual(source, original)
        self.assertEqual(derived["digest"], digest({k: v for k, v in derived.items() if k != "digest"}))
        self.assertEqual(validate_recording(derived), derived)

    def test_derive_preserves_initial_delay_at_speed_one_and_prunes_variables(self):
        source = recording()
        derived = derive_recording(source, event_ids=["e1"], speed=1.0, new_id="recording-2")
        self.assertEqual([event["offsetMs"] for event in derived["events"]], [100])
        self.assertEqual(derived["variables"], [])
        self.assertFalse(derived["replayable"])
        self.assertEqual(derived["reason"], "edited_sequence_requires_validation")

    def test_chained_edited_derivation_keeps_conservative_reason(self):
        edited = derive_recording(recording(), event_ids=["e2"], new_id="recording-2")
        chained = derive_recording(edited, speed=2.0, new_id="recording-3")
        self.assertFalse(chained["replayable"])
        self.assertEqual(chained["reason"], "edited_sequence_requires_validation")
        self.assertEqual(chained["variables"], ["text_1"])

    def test_edited_sequence_is_false_replayable_and_rejects_bad_selection(self):
        source = recording()
        edited = derive_recording(source, event_ids=["e2"], new_id="recording-2")
        self.assertFalse(edited["replayable"])
        self.assertEqual(edited["reason"], "edited_sequence_requires_validation")
        for ids in (["unknown"], ["e1", "e1"], ["e2", "e1"]):
            with self.subTest(ids=ids):
                with self.assertRaises(ContractError):
                    derive_recording(source, event_ids=ids)

    def test_speed_boundaries(self):
        for speed in (0.25, 4.0):
            derived = derive_recording(recording(), speed=speed)
            self.assertEqual(derived["provenance"]["transform"]["speed"], speed)
        for speed in (0.249, 4.001, True, float("inf")):
            with self.subTest(speed=speed):
                with self.assertRaises(ContractError):
                    derive_recording(recording(), speed=speed)


if __name__ == "__main__":
    unittest.main()
