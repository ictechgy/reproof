"""Recorded original facts must describe the operation actually dispatched."""
import threading
import unittest
from unittest import mock

from reproloop.live.model import LiveError
from reproloop.live.recording_session import RecordingStore, RecordingStoreError
from tests import test_g2_lab_integration as fixture
from tests.test_recording_recovery import project_document


class RecordingLabBoundaryTests(unittest.TestCase):
    setUp = fixture.ReleaseLabTests.setUp
    tearDown = fixture.ReleaseLabTests.tearDown
    register = fixture.ReleaseLabTests.register
    create = fixture.ReleaseLabTests.create
    command = fixture.ReleaseLabTests.command

    def test_concurrent_project_registration_uses_one_store_owner(self):
        first_entered = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()
        counter_lock = threading.Lock()
        calls, evidence_handles, results, errors = [], [], [], []
        original_init = RecordingStore.__init__

        def observed_init(instance, *args, **kwargs):
            with counter_lock:
                calls.append(instance)
                evidence_handles.append(args[1])
                ordinal = len(calls)
            if ordinal == 1:
                first_entered.set()
                if not release.wait(5):
                    raise RuntimeError("Parent did not release storage startup")
            else:
                second_entered.set()
            return original_init(instance, *args, **kwargs)

        def register():
            try:
                results.append(self.register())
            except Exception as error:
                errors.append(type(error).__name__)

        threads = [threading.Thread(target=register, daemon=True) for _ in range(2)]
        try:
            with mock.patch.object(RecordingStore, "__init__", observed_init):
                threads[0].start()
                self.assertTrue(first_entered.wait(5))
                threads[1].start()
                second_entered.wait(0.2)
                release.set()
                for thread in threads:
                    thread.join(5)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(calls), 1)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.create(results[0])
        finally:
            release.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(5)
            for evidence in evidence_handles:
                if evidence is not self.lab._evidence_store:
                    evidence.close()
                    evidence.budget.close()

    def test_application_platform_mismatch_is_rejected_before_provider_start(self):
        project = project_document()
        project["applications"][0]["platform"] = "android"
        registration = self.register(project=project)
        with self.assertRaises(LiveError):
            self.create(registration)
        self.assertEqual(self.factory_calls, 0)

    def test_original_coordinates_cannot_differ_from_dispatched_coordinates(self):
        session = self.create(self.register())
        command = self.command(session)
        frame = self.lab.frame(session["id"])
        typed = {"action": "tap", "parameters": {"x": 0.1, "y": 0.9},
                 "geometry": {"width": frame["width"], "height": frame["height"],
                              "rotation": 0, "version": frame["geometryVersion"]}}
        try:
            self.lab.input(session["id"], "owner", command, recording_input=typed)
        except LiveError:
            self.assertEqual(self.provider.calls, [])
            return
        frozen = self.lab.stop_release_recording(
            session["id"], "owner", session["controllerId"], session["epoch"])
        recorded = frozen["original"]["events"][0]["input"]
        self.assertEqual(recorded["parameters"], command["payload"])

    def test_unobserved_caller_locator_cannot_replace_actual_original_tap(self):
        session = self.create(self.register())
        command = self.command(session)
        typed = {"action": "tap", "parameters": {},
                 "target": {"kind": "accessibility-id", "value": "unobserved_target"}}
        try:
            self.lab.input(session["id"], "owner", command, recording_input=typed)
        except LiveError:
            self.assertEqual(self.provider.calls, [])
            return
        frozen = self.lab.stop_release_recording(
            session["id"], "owner", session["controllerId"], session["epoch"])
        recorded = frozen["original"]["events"][0]["input"]
        # This provider exposes pixels and coordinate injection; it supplies
        # no locator/hit-test evidence that could justify the caller's label.
        self.assertNotIn("target", recorded)
        self.assertEqual(recorded["parameters"], command["payload"])

    def test_failed_admission_journal_prevents_provider_input(self):
        session = self.create(self.register())
        command = self.command(session)
        frame = self.lab.frame(session["id"])
        typed = {"action": "tap", "parameters": dict(command["payload"]),
                 "geometry": {"width": frame["width"], "height": frame["height"],
                              "rotation": 0, "version": frame["geometryVersion"]}}
        recorder = self.lab.sessions[session["id"]]["releaseRecorder"]
        with mock.patch.object(recorder, "admit_input",
                               side_effect=RecordingStoreError("Injected journal failure")) as admission:
            with self.assertRaises(LiveError):
                self.lab.input(session["id"], "owner", command, recording_input=typed)
            admission.assert_called_once()
        self.assertEqual(self.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
