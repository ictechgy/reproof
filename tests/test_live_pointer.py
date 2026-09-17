import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from reproloop.core import ContractError, digest
from reproloop.live.model import Lab, LiveError, validate_gesture
from reproloop.live.recordings import derive_recording, validate_recording


class PointerProvider:
    def __init__(self):
        self.calls = []
        self.frame_calls = []
        self.session = None
        self.lab = None
        self.fail_cancel = False
        self.fail_pointer = False
        self.fail_up = False
        self.closed = False

    def start(self, session, lab):
        self.session = session
        self.lab = lab
        lab.publish_frame(session["id"], b"<svg/>", "image/svg+xml", 400, 800, "portrait")

    def execute_with_frame(self, action, payload, frame):
        self.frame_calls.append((action, copy.deepcopy(frame)))
        return self.execute(action, payload)

    def execute(self, action, payload):
        self.calls.append((action, copy.deepcopy(payload)))
        if action == "pointer" and payload["phase"] == "cancel" and self.fail_cancel:
            return {"ok": False, "timing": "best-effort"}
        if action == "pointer" and payload["phase"] != "cancel" and self.fail_pointer:
            return {"ok": False, "timing": "best-effort"}
        if action == "pointer" and payload["phase"] == "up" and self.fail_up:
            return {"ok": False, "timing": "best-effort"}
        self.lab.publish_frame(self.session["id"], b"<svg/>", "image/svg+xml", 400, 800, "portrait")
        return {"ok": True, "timing": "best-effort"}

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class LivePointerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = PointerProvider()
        self.clock = Clock()
        self.lab = Lab([{
            "id": "pointer-device", "name": "Pointer", "kind": "pointer-demo",
            "capabilities": {
                "actions": ["tap", "long_press", "swipe", "text", "home", "reset", "pointer"],
                "inputMode": "continuous-pointer", "maxPointers": 5,
                "resetContract": "pointer-fixture-v1",
            },
            "factory": lambda: self.provider,
        }], Path(self.temp.name), clock=self.clock)

    def tearDown(self):
        self.lab.close_all()
        self.temp.cleanup()

    def session(self, client="client"):
        return self.lab.create_session("pointer-device", "owner", client)

    def command(self, session, sequence, payload, action="pointer", **changes):
        frame = self.lab.frame(session["id"])
        command = {
            "controllerId": session["controllerId"], "epoch": session["epoch"], "sequence": sequence,
            "commandId": f"command-{sequence}", "frameId": frame["id"],
            "geometryVersion": frame["geometryVersion"], "action": action, "payload": payload,
        }
        command.update(changes)
        return command

    def pointer(self, session, sequence, phase, pointer_id=0, x=.25, y=.5):
        return self.lab.input(session["id"], "owner", self.command(
            session, sequence, {"phase": phase, "pointerId": pointer_id, "x": x, "y": y}))

    def test_pointer_validation_and_multi_pointer_lifecycle(self):
        validate_gesture("pointer", {"phase": "down", "pointerId": 4, "x": .1, "y": .2})
        with self.assertRaises(LiveError):
            validate_gesture("pointer", {"phase": "down", "pointerId": 5, "x": .1, "y": .2})
        session = self.session()
        self.pointer(session, 1, "down", 0)
        self.pointer(session, 2, "down", 1, .7, .8)
        public = self.lab.get_session(session["id"], "owner")
        self.assertEqual(public["activePointerIds"], [0, 1])
        with self.assertRaises(LiveError):
            self.pointer(session, 3, "down", 0)
        with self.assertRaises(LiveError):
            self.pointer(session, 3, "up", 2)
        with self.assertRaises(LiveError):
            self.lab.input(session["id"], "owner", self.command(session, 3, {"x": .5, "y": .5}, "tap"))
        self.pointer(session, 3, "cancel", 4)
        self.assertEqual(self.lab.get_session(session["id"], "owner")["activePointerIds"], [])
        self.pointer(session, 4, "cancel", 4)  # idempotent cancel
        self.assertTrue(any(item[0] == "pointer" for item in self.provider.frame_calls))

    def test_cancel_bypasses_rotated_frame_guard_but_keeps_epoch_fencing(self):
        session = self.session()
        original = self.lab.frame(session["id"])
        self.pointer(session, 1, "down")
        self.lab.publish_frame(session["id"], b"<svg/>", "image/svg+xml", 800, 400, "landscape")
        cancel = self.command(session, 2, {"phase": "cancel", "pointerId": 0, "x": 0, "y": 0},
                              frameId=original["id"], geometryVersion=original["geometryVersion"])
        self.lab.input(session["id"], "owner", cancel)
        self.assertEqual(self.lab.get_session(session["id"], "owner")["activePointerIds"], [])
        self.assertEqual([payload["phase"] for action, payload in self.provider.calls if action == "pointer"], ["down", "cancel"])
        stale = copy.deepcopy(cancel)
        stale["sequence"] = 3
        stale["commandId"] = "command-3"
        stale["epoch"] = session["epoch"] - 1
        with self.assertRaises(LiveError):
            self.lab.input(session["id"], "owner", stale)
        self.assertEqual([payload["phase"] for action, payload in self.provider.calls if action == "pointer"], ["down", "cancel"])

    def test_acknowledgement_first_pointer_state_and_provider_frame_hook(self):
        session = self.session()
        self.provider.fail_pointer = True
        with self.assertRaises(LiveError):
            self.pointer(session, 1, "down")
        # A failed pointer injection never updates active state.
        self.assertEqual(self.lab.get_session(session["id"], "owner")["activePointerIds"], [])
        # The hook receives the displayed frame identity before injection.
        self.assertEqual(self.provider.frame_calls[-1][1]["frameId"], self.lab.frame(session["id"])["id"])

    def test_handoff_cancels_pointer_before_granting_new_controller(self):
        session = self.session()
        self.pointer(session, 1, "down")
        claimed = self.lab.claim(session["id"], "owner", "new-client", session["epoch"], "manual")
        self.assertEqual(claimed["controllerId"], "new-client")
        self.assertEqual([payload.get("phase") for action, payload in self.provider.calls if action == "pointer"], ["down", "cancel"])
        self.assertEqual(self.lab.get_session(session["id"], "owner")["activePointerIds"], [])

    def test_stop_recording_cancels_held_pointer_and_marks_recording_invalid(self):
        session = self.session()
        self.lab.start_recording(session["id"], "owner", session["controllerId"], session["epoch"], reset=True)
        self.pointer(session, 1, "down")
        record = self.lab.stop_recording(session["id"], "owner", session["controllerId"], session["epoch"])
        self.assertEqual(record["status"], "invalid")
        self.assertFalse(record["replayable"])
        self.assertEqual(record["reason"], "pointer_cancelled")
        self.assertEqual(self.lab.get_session(session["id"], "owner")["activePointerIds"], [])

    def test_balanced_pointer_recording_replays(self):
        session = self.session()
        self.lab.start_recording(session["id"], "owner", session["controllerId"], session["epoch"], reset=True)
        self.pointer(session, 1, "down")
        self.pointer(session, 2, "move", x=.4)
        self.pointer(session, 3, "up", x=.4)
        record = self.lab.stop_recording(session["id"], "owner", session["controllerId"], session["epoch"])
        self.assertEqual(record["status"], "complete")
        self.assertTrue(record["replayable"])
        replay = self.lab.start_replay(session["id"], "owner", session["controllerId"], session["epoch"], record["id"])
        self.lab._session(session["id"])["replayThread"].join(timeout=3)
        current = self.lab.get_session(session["id"], "owner")
        self.assertEqual(current["replay"]["state"], "actions_replayed")
        self.assertEqual(current["controllerId"], "client")
        self.assertEqual(current["activePointerIds"], [])
        self.assertTrue(replay["id"])

    def test_replay_pointer_cancel_failure_is_persisted_as_failed(self):
        session = self.session()
        self.lab.start_recording(session["id"], "owner", session["controllerId"], session["epoch"], reset=True)
        self.pointer(session, 1, "down")
        self.pointer(session, 2, "up")
        record = self.lab.stop_recording(session["id"], "owner", session["controllerId"], session["epoch"])
        self.provider.fail_up = True
        self.provider.fail_cancel = True
        self.lab.start_replay(session["id"], "owner", session["controllerId"], session["epoch"], record["id"])
        replay_thread = self.lab._session(session["id"])["replayThread"]
        replay_thread.join(timeout=3)
        current = self.lab.get_session(session["id"], "owner")
        self.assertEqual(current["state"], "failed")
        self.assertEqual(current["replay"]["state"], "failed")
        replay_file = Path(self.temp.name) / "replays" / f'{current["replay"]["id"]}.json'
        self.assertEqual(json.loads(replay_file.read_text())["state"], "failed")

    def test_unknown_cancel_quarantines_without_handoff(self):
        session = self.session()
        self.pointer(session, 1, "down")
        self.provider.fail_cancel = True
        with self.assertRaises(LiveError):
            self.lab.claim(session["id"], "owner", "new-client", session["epoch"], "manual")
        self.assertEqual(self.lab.get_session(session["id"], "owner")["state"], "failed")
        self.assertEqual(self.lab.list_devices()[0]["state"], "quarantined")
        self.assertEqual(self.lab.get_session(session["id"], "owner")["controllerId"], "client")

    def test_close_attempts_provider_cleanup_after_pointer_cancel_failure(self):
        session = self.session()
        self.pointer(session, 1, "down")
        self.provider.fail_cancel = True
        closed = self.lab.close_session(session["id"], "owner")
        self.assertTrue(self.provider.closed)
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(self.lab.list_devices()[0]["state"], "available")

    def test_pointer_timeout_cancels_and_fences_old_epoch(self):
        session = self.session()
        self.pointer(session, 1, "down")
        old_epoch = session["epoch"]
        self.clock.advance(11)
        self.lab.reap_expired()
        current = self.lab.get_session(session["id"], "owner")
        self.assertEqual(current["state"], "active")
        self.assertGreater(current["epoch"], old_epoch)
        self.assertEqual(current["activePointerIds"], [])
        with self.assertRaises(LiveError):
            self.lab.input(session["id"], "owner", self.command(session, 2, {"phase": "move", "pointerId": 0, "x": .5, "y": .5}, epoch=old_epoch))
        self.pointer(current, 2, "down")

    def test_multifinger_activity_refreshes_all_pointer_timeouts(self):
        session = self.session()
        self.pointer(session, 1, "down", 0)
        self.clock.advance(9)
        self.pointer(session, 2, "down", 1, .7, .8)
        self.clock.advance(9)
        self.pointer(session, 3, "move", 0, .4, .5)
        self.clock.advance(9)
        self.pointer(session, 4, "move", 1, .8, .8)
        self.lab.reap_expired()
        self.assertEqual(self.lab.get_session(session["id"], "owner")["activePointerIds"], [0, 1])
        self.clock.advance(11)
        self.lab.reap_expired()
        self.assertEqual(self.lab.get_session(session["id"], "owner")["activePointerIds"], [])

    def test_derived_unbalanced_pointer_trace_is_nonreplayable(self):
        session = self.session()
        self.lab.start_recording(session["id"], "owner", session["controllerId"], session["epoch"], reset=True)
        self.pointer(session, 1, "down")
        self.pointer(session, 2, "up")
        balanced = self.lab.stop_recording(session["id"], "owner", session["controllerId"], session["epoch"])
        edited = derive_recording(balanced, event_ids=["e2"])
        self.assertFalse(edited["replayable"])
        self.assertEqual(edited["reason"], "edited_sequence_requires_validation")
        self.assertEqual(validate_recording(edited), edited)
        self.assertEqual(balanced["status"], "complete")

    def test_validator_rejects_geometry_change_and_interleaving(self):
        session = self.session()
        self.lab.start_recording(session["id"], "owner", session["controllerId"], session["epoch"], reset=True)
        self.pointer(session, 1, "down")
        self.pointer(session, 2, "move")
        self.pointer(session, 3, "up")
        record = self.lab.stop_recording(session["id"], "owner", session["controllerId"], session["epoch"])
        changed = copy.deepcopy(record)
        changed["events"][1]["geometryVersion"] += 1
        changed["digest"] = digest({key: value for key, value in changed.items() if key != "digest"})
        with self.assertRaises(ContractError):
            validate_recording(changed)
        interleaved = copy.deepcopy(record)
        interleaved["events"][1]["action"] = "tap"
        interleaved["events"][1]["payload"] = {"x": .5, "y": .5}
        interleaved["digest"] = digest({key: value for key, value in interleaved.items() if key != "digest"})
        with self.assertRaises(ContractError):
            validate_recording(interleaved)


if __name__ == "__main__":
    unittest.main()
