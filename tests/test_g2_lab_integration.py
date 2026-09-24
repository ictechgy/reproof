import base64
import copy
import hashlib
import json
import struct
from pathlib import Path
import tempfile
import threading
import unittest

from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.authority import HostAuthority
from reproof.live.model import Lab, LiveError
from reproof.live.media import encode_frame
from reproof.live.providers import IosProvider
from tests.test_clock_sync import FakeClock
from tests.test_live_authority_integration import Clock, FencedProvider, parent_grant
from tests.test_recording_recovery import collection_policy, preparation, project_document


class ReleaseProvider:
    def __init__(self, *, blocked=False, classified=False):
        self.blocked = blocked
        self.classified = classified
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.log_calls = 0
        self.observe_calls = 0
        self.closed = False

    def start(self, session, lab):
        self.session = session
        self.lab = lab
        body = b"<svg>approved-test-frame</svg>"
        classification = None
        if self.classified:
            classification = lab.classify_recording_sample(
                session["id"], kind="pixels", body=body,
                native_incarnation="native_one", acquisition_sequence=1,
                sample_id="sample_one", decision="approved")
        lab.publish_frame(session["id"], body, "image/svg+xml", 400, 800,
                          "portrait", acquisition_sequence=1,
                          classification=classification)

    def execute(self, action, payload):
        self.calls.append((action, copy.deepcopy(payload)))
        if self.blocked:
            self.entered.set()
            self.release.wait(5)
        return {"ok": True, "timing": "best-effort"}

    def collect_app_logs(self):
        self.log_calls += 1
        return {"sessionId": "12345678-1234-1234-1234-123456789abc",
                "events": [{"message": "must-not-leak"}]}

    def observe(self):
        self.observe_calls += 1
        return {"label": "must-not-leak"}

    def close(self):
        self.closed = True


class CollectingFrameSink:
    def __init__(self):
        self.frames = []

    def accept_frame(self, publication, body):
        self.frames.append((publication, bytes(body)))


class ReleaseLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = ReleaseProvider()
        self.factory_calls = 0

        def factory():
            self.factory_calls += 1
            return self.provider

        self.device = {
            "id": "test",
            "name": "Test",
            "platform": "ios",
            "kind": "demo",
            "factory": factory,
            "capabilities": {
                "actions": ["tap", "long_press", "swipe", "text", "home"],
                "inputMode": "gesture-batch",
                "media": "demo-svg",
                "automaticAppLogs": True,
                "applicationIdentity": {
                    "bundle": "com.example.app",
                    "artifactDigest": "0" * 64,
                },
                "recordingTextTarget": {
                    "kind": "accessibility-id",
                    "value": "account",
                },
            },
        }
        self.clock = FakeClock(1_000_000_000)
        self.lab = Lab([self.device], Path(self.temp.name),
                       recording_clock_sync=ClockSynchronizer(self.clock),
                       recording_wall_clock_ms=lambda: 5_000_000)

    def tearDown(self):
        self.lab.close_all()
        self.temp.cleanup()

    def register(self, mode="test-data", project=None):
        return self.lab.register_recording_project(
            project or project_document(), collection_policy(mode),
            capacity_bytes=32 * 1024 * 1024,
            journal_headroom_bytes=512 * 1024,
        )

    def create(self, registration, *, frame_sink=None):
        return self.lab.create_release_session(
            "test", "owner", "browser_one", registration,
            application_id="ios_app", build_id="original",
            preparation_receipts=preparation(), frame_sink=frame_sink,
        )

    def use_unconditional_native_descriptor(self):
        self.lab.close_all()
        self.device["kind"] = "ios-simulator"
        self.device["capabilities"]["authorityMode"] = "legacy-offline-v1"
        self.lab = Lab(
            [self.device], Path(self.temp.name) / "native-descriptor",
            recording_clock_sync=ClockSynchronizer(self.clock),
            recording_wall_clock_ms=lambda: 5_000_000,
        )

    def command(self, session, *, sequence=1, action="tap", payload=None):
        frame = self.lab.frame(session["id"])
        return {
            "controllerId": session["controllerId"],
            "epoch": session["epoch"],
            "sequence": sequence,
            "commandId": f"command-{sequence}",
            "frameId": frame["id"],
            "geometryVersion": frame["geometryVersion"],
            "action": action,
            "payload": {"x": 0.5, "y": 0.5} if payload is None else payload,
        }

    def test_release_session_binds_build_before_provider_and_freezes_lab_input(self):
        registration = self.register()
        session = self.create(registration)
        command = self.command(session)
        typed = {"action": "tap",
                 "target": {"kind": "accessibility-id", "value": "checkout"},
                 "parameters": {}}
        receipt = self.lab.input(session["id"], "owner", command,
                                 recording_input=typed)
        self.assertEqual(receipt["status"], "injected")
        frozen = self.lab.stop_release_recording(
            session["id"], "owner", session["controllerId"], session["epoch"])
        self.assertEqual(frozen["status"], "frozen-complete")
        self.assertEqual(frozen["original"]["events"][0]["operationId"],
                         "operation_" + hashlib.sha256(
                             (session["id"] + "\0input-command-1").encode()).hexdigest()[:40])
        self.assertEqual(len(frozen["original"]["media"]), 1)
        recorded = frozen["original"]["events"][0]["input"]
        self.assertEqual(recorded["parameters"], command["payload"])
        self.assertNotIn("target", recorded)

    def test_caller_recording_coordinates_cannot_replace_dispatched_coordinates(self):
        session = self.create(self.register())
        command = self.command(session)
        frame = self.lab.frame(session["id"])
        claimed = {
            "action": "tap", "parameters": {"x": 0.1, "y": 0.9},
            "geometry": {
                "width": frame["width"], "height": frame["height"],
                "rotation": 0, "version": frame["geometryVersion"],
            },
        }
        self.lab.input(
            session["id"], "owner", command, recording_input=claimed)
        frozen = self.lab.stop_release_recording(
            session["id"], "owner", session["controllerId"], session["epoch"])
        recorded = frozen["original"]["events"][0]["input"]
        self.assertEqual(recorded["parameters"], command["payload"])

    def test_release_frame_stream_metadata_binds_durable_object_and_timing(self):
        session = self.create(self.register())
        frame = self.lab.frame(session["id"])
        metadata = {key: value for key, value in frame.items() if key != "imageBase64"}
        metadata["type"] = "frame"
        image = base64.b64decode(frame["imageBase64"])
        packet = encode_frame(metadata, image)
        metadata_bytes, image_bytes = struct.unpack(">II", packet[:8])
        decoded = json.loads(packet[8:8 + metadata_bytes])
        self.assertEqual(image_bytes, len(image))
        self.assertEqual(decoded["objectDigest"], metadata["objectDigest"])
        self.assertEqual(self.lab._evidence_store.read(decoded["objectDigest"]), image)

    def test_provider_monotonic_frame_keeps_interval_separate_from_display_epoch(self):
        session = self.create(self.register())
        received = self.lab._recording_clock_sync.sample()
        self.clock.advance(10_000_000)
        sent = self.lab._recording_clock_sync.sample()
        mapping = self.lab._recording_clock_sync.record_exchange(
            coordinator_clock_id="native-clock", coordinator_send_ns=900_000_000,
            host_received=received, host_sent=sent,
            coordinator_receive_ns=930_000_000,
            coordinator_uncertainty_ns=1_000_000, max_drift_ppm=1000)
        binding = self.lab.bind_recording_provider_clock(
            session["id"], mapping, provider_boot_digest="b" * 64,
            native_incarnation="native_one")
        self.assertTrue(self.lab.publish_frame(
            session["id"], b"provider-clock-frame", "image/png", 400, 800,
            "portrait", acquisition_sequence=2,
            provider_clock_binding=binding, provider_monotonic_ns=950_000_000,
            native_incarnation="native_one"))
        frame = self.lab.frame(session["id"])
        self.assertEqual(frame["providerMonotonicNs"], 950_000_000)
        self.assertEqual(frame["providerClockId"], "native-clock")
        self.assertLess(frame["presentationOffsetMs"], frame["capturedAt"])
        timeline = self.lab._recording_store.media_timeline(session["releaseRecordingId"])
        self.assertLessEqual(timeline[-1]["timing"]["earliestOffsetMs"],
                             timeline[-1]["timing"]["latestOffsetMs"])

    def test_ios_bridge_without_native_mapping_records_timing_interruption(self):
        session = self.create(self.register())
        bridge = IosProvider.__new__(IosProvider)
        bridge.profile = None
        bridge.device_authority = None
        bridge.native_handshake = None
        bridge.sid = session["id"]
        bridge.lab = self.lab
        self.clock.advance(5_000_000_000)
        bridge.bridge("frame", {
            "imageBase64": base64.b64encode(b"delayed-native-frame").decode(),
            "mime": "image/png", "width": 400, "height": 800,
            "orientation": "portrait", "capturedAt": 5_000_010,
            "nativeFrameId": 2,
        })
        frame = self.lab.frame(session["id"])
        self.assertEqual(frame["captureTimingSource"], "native-unmapped")
        frozen = self.lab.stop_release_recording(
            session["id"], "owner", session["controllerId"], session["epoch"])
        self.assertEqual(frozen["status"], "frozen-incomplete")
        self.assertIn(
            "native_timing_unknown",
            {item["reason"] for item in frozen["original"]["interruptions"]},
        )

    def test_wrong_build_is_rejected_before_provider_construction(self):
        project = project_document()
        project["builds"][0]["artifactDigest"] = "f" * 64
        registration = self.register(project=project)
        with self.assertRaises(LiveError):
            self.create(registration)
        self.assertEqual(self.factory_calls, 0)

    def test_wrong_application_platform_is_rejected_before_provider_construction(self):
        project = project_document()
        project["applications"][0]["platform"] = "android"
        registration = self.register(project=project)
        with self.assertRaises(LiveError):
            self.create(registration)
        self.assertEqual(self.factory_calls, 0)

    def test_unconditional_native_provider_rejects_sample_bound_capture_before_factory(self):
        self.use_unconditional_native_descriptor()
        registration = self.register("sample-bound")
        with self.assertRaises(LiveError):
            self.create(registration)
        self.assertEqual(self.factory_calls, 0)

    def test_unconditional_native_provider_rejects_suppressed_capture_before_factory(self):
        self.use_unconditional_native_descriptor()
        registration = self.register("suppressed")
        with self.assertRaises(LiveError):
            self.create(registration)
        self.assertEqual(self.factory_calls, 0)

    def test_unconditional_native_provider_rejects_disabled_pixels_before_factory(self):
        self.use_unconditional_native_descriptor()
        project = project_document()
        project["evidencePolicy"]["pixels"] = False
        registration = self.register(project=project)
        with self.assertRaises(LiveError):
            self.create(registration)
        self.assertEqual(self.factory_calls, 0)

    def test_provider_construction_failure_freezes_started_release_recording(self):
        registration = self.register()

        def fail_factory():
            raise RuntimeError("synthetic provider construction failure")

        self.lab.devices["test"]["factory"] = fail_factory
        with self.assertRaises(RuntimeError):
            self.create(registration)
        recording_id = next(iter(self.lab.release_recording_owners))
        frozen = self.lab.release_recording(recording_id, "owner")
        self.assertEqual(frozen["status"], "frozen-incomplete")
        self.assertIn(
            "provider_start_failed",
            {item["reason"] for item in frozen["original"]["interruptions"]},
        )

    def test_input_is_not_injected_when_admission_journal_is_unavailable(self):
        session = self.create(self.register())
        command = self.command(session)
        typed = {"action": "tap",
                 "target": {"kind": "accessibility-id", "value": "checkout"},
                 "parameters": {}}
        self.lab._recording_store.close()
        with self.assertRaises(LiveError) as rejected:
            self.lab.input(session["id"], "owner", command, recording_input=typed)
        self.assertEqual(rejected.exception.code, "recording_unavailable")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.lab.get_session(session["id"])["state"], "failed")

    def test_blocked_provider_stop_freezes_unknown_and_late_success_is_lifecycle_only(self):
        self.provider.blocked = True
        session = self.create(self.register())
        command = self.command(session)
        typed = {"action": "tap",
                 "target": {"kind": "accessibility-id", "value": "checkout"},
                 "parameters": {}}
        errors = []

        def invoke():
            try:
                self.lab.input(session["id"], "owner", command, recording_input=typed)
            except LiveError as error:
                errors.append(error.code)

        worker = threading.Thread(target=invoke)
        worker.start()
        self.assertTrue(self.provider.entered.wait(5))
        frozen = self.lab.stop_release_recording(
            session["id"], "owner", session["controllerId"], session["epoch"])
        before = frozen["recordingDigest"]
        self.provider.release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        after = self.lab.release_recording(frozen["recordingId"], "owner")
        self.assertEqual(after["recordingDigest"], before)
        self.assertEqual(after["original"]["events"][0]["dispatch"], "unknown")
        self.assertEqual(after["lifecycleReceipts"][-1]["kind"], "ack")
        self.assertEqual(after["lifecycleReceipts"][-1]["status"], "complete")
        self.assertEqual(errors, ["injection_unknown"])

    def test_raw_text_never_enters_recording_store_or_frozen_original(self):
        session = self.create(self.register())
        secret = "raw-sensitive-runtime-value"
        command = self.command(session, action="text", payload={"value": secret})
        typed = {
            "action": "text",
            "target": {"kind": "accessibility-id", "value": "account"},
            "parameters": {"variableId": "account_name"},
        }
        self.lab.input(session["id"], "owner", command, recording_input=typed)
        frozen = self.lab.stop_release_recording(
            session["id"], "owner", session["controllerId"], session["epoch"])
        self.assertNotIn(secret, json.dumps(frozen, sort_keys=True))
        self.assertEqual(
            frozen["original"]["events"][0]["input"]["target"],
            self.device["capabilities"]["recordingTextTarget"],
        )
        self.assertEqual(self.provider.calls[-1][1], {"value": secret})
        self.lab.close_session(session["id"], "owner")
        self.lab.close_evidence_store()
        for path in (Path(self.temp.name) / "evidence-v1").rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), path)

    def test_sample_classification_cannot_be_reused_for_later_pixels(self):
        self.provider.classified = True
        sink = CollectingFrameSink()
        session = self.create(self.register("sample-bound"), frame_sink=sink)
        current = self.lab.frame(session["id"])
        self.assertEqual(base64.b64decode(current["imageBase64"]),
                         b"<svg>approved-test-frame</svg>")
        token = self.lab.classify_recording_sample(
            session["id"], kind="pixels", body=b"other",
            native_incarnation="native_one", acquisition_sequence=2,
            sample_id="sample_two", decision="sensitive")
        self.lab.publish_frame(session["id"], b"other", "image/svg+xml", 400, 800,
                               "portrait", acquisition_sequence=2,
                               classification=token)
        self.assertEqual(self.lab.frame(session["id"])["id"], current["id"])
        self.assertEqual([body for _, body in sink.frames],
                         [b"<svg>approved-test-frame</svg>"])
        frozen = self.lab.stop_release_recording(
            session["id"], "owner", session["controllerId"], session["epoch"])
        self.assertEqual(frozen["status"], "frozen-incomplete")

    def test_disabled_logs_are_rejected_before_provider_collection(self):
        project = project_document()
        project["evidencePolicy"]["logs"] = False
        session = self.create(self.register(project=project))
        with self.assertRaises(LiveError):
            self.lab.app_logs(session["id"], "owner")
        self.assertEqual(self.provider.log_calls, 0)

    def test_disabled_semantic_values_are_rejected_before_provider_collection(self):
        project = project_document()
        project["evidencePolicy"]["text"] = False
        session = self.create(self.register(project=project))
        with self.assertRaises(LiveError):
            self.lab.observe(session["id"], "owner")
        self.assertEqual(self.provider.observe_calls, 0)

    def test_unclassified_log_value_is_suppressed_before_any_persistence(self):
        self.provider.classified = True
        session = self.create(self.register("sample-bound"))
        with self.assertRaises(LiveError):
            self.lab.app_logs(session["id"], "owner")
        self.assertEqual(self.provider.log_calls, 1)
        self.lab.stop_release_recording(
            session["id"], "owner", session["controllerId"], session["epoch"])
        self.lab.close_session(session["id"], "owner")
        self.lab.close_evidence_store()
        raw_log = json.dumps(
            {"events": [{"message": "must-not-leak"}],
             "sessionId": "12345678-1234-1234-1234-123456789abc"},
            sort_keys=True, separators=(",", ":")).encode()
        raw_digest = hashlib.sha256(raw_log).hexdigest().encode()
        for path in (Path(self.temp.name) / "evidence-v1").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"must-not-leak", path.read_bytes(), path)
                self.assertNotIn(raw_digest, path.read_bytes(), path)


class ReleaseLabAuthorityTests(unittest.TestCase):
    def test_recording_event_preserves_real_authority_operation_and_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = Clock()
            authority = HostAuthority(root / "authority.sqlite3", clock=clock,
                                      lease_directory=root / "leases")
            grant = parent_grant(authority, clock)
            provider = FencedProvider()
            descriptor = {
                "id": "authority-device", "name": "Authority device",
                "platform": "android", "kind": "android-live",
                "capabilities": {
                    "actions": ["tap"], "inputMode": "gesture-batch",
                    "applicationIdentity": {"bundle": "com.example.app",
                                              "artifactDigest": "0" * 64},
                },
                "factory": lambda: provider,
                "_authority": {"deviceKind": "android", "physicalId": "physical-g2"},
            }
            lab = Lab([descriptor], root / "live", authority=authority,
                      parent_grant=grant, recording_wall_clock_ms=lambda: 5_000_000)
            try:
                project = project_document()
                project["id"] = "integration-project"
                project["applications"][0]["platform"] = "android"
                receipt = preparation()
                receipt[0]["projectId"] = "integration-project"
                registration = lab.register_recording_project(
                    project, collection_policy(), capacity_bytes=32 * 1024 * 1024,
                    journal_headroom_bytes=512 * 1024)
                session = lab.create_release_session(
                    "authority-device", "owner", "browser_one", registration,
                    application_id="ios_app", build_id="original",
                    preparation_receipts=receipt)
                frame = lab.frame(session["id"])
                command = {"controllerId": session["controllerId"], "epoch": session["epoch"],
                           "sequence": 1, "commandId": "command-one",
                           "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
                           "action": "tap", "payload": {"x": 0.5, "y": 0.5}}
                typed = {"action": "tap",
                         "target": {"kind": "accessibility-id", "value": "checkout"},
                         "parameters": {}}
                lab.input(session["id"], "owner", command, recording_input=typed)
                frozen = lab.stop_release_recording(
                    session["id"], "owner", session["controllerId"], session["epoch"])
                event = frozen["original"]["events"][0]
                operation = authority.store.operation(event["operationId"])
                self.assertIsNotNone(operation)
                self.assertEqual(event["generation"], operation["generation"])
                self.assertEqual(event["operationId"], operation["operation_id"])
                self.assertEqual(event["receipt"]["providerIncarnation"],
                                 provider.provider_incarnation)
                lab.close_session(session["id"], "owner")
            finally:
                lab.close_all()
                authority.close()


if __name__ == "__main__":
    unittest.main()
