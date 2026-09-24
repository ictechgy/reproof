import base64
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace
import uuid
import tempfile
import unittest
from unittest.mock import patch

from reproof.ios_runner import IosSimulator
from reproof.live.providers import IosProvider


class AutoProfile(SimpleNamespace):
    pass


def profile():
    return AutoProfile(data={
        "schemaVersion": 1,
        "platform": "ios",
        "applicationId": "io.reproof.sample.ios",
        "fixture": {"id": "ios-counter", "version": 1, "inputs": {}},
    }, digest="a" * 64)


def app_tree(root, build_id="build-auto"):
    app = root / "ReproSample.app"
    app.mkdir()
    (app / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "io.reproof.sample.ios",
        "ReproBuildID": build_id,
        "ReproAutoProfileDigest": "a" * 64,
        "ReproAutoProfile": profile().data,
    }))
    return app


def products_tree(root):
    products = root / "products"
    products.mkdir()
    xctest = {
        "__xctestrun_metadata__": {"FormatVersion": 1},
        "ReproReplayTests": {
            "BlueprintName": "ReproReplayTests",
            "TestBundlePath": "__TESTROOT__/ReproReplayTests.xctest",
            "TestHostPath": "__TESTROOT__/ReproSample.app",
            "UITargetAppPath": "__TESTROOT__/ReproSample.app",
        },
    }
    (products / "ReproReplayTests.xctestrun").write_bytes(plistlib.dumps(xctest))
    (products / "ReproReplayTests.xctest").mkdir()
    (products / "ReproSample.app").mkdir()
    return products


class AutoRunnerTests(unittest.TestCase):
    def test_capture_rejects_marker_identity_replacement_during_collection(self):
        from reproof.core import ContractError
        from reproof.ios_cases import case_spec
        from reproof.ios_device import IosPhysicalDevice
        from reproof.ios_instrumentation import sample_ios_auto_profile

        selected = sample_ios_auto_profile()
        fixture = case_spec('counter').fixture
        for adapter in (IosSimulator, IosPhysicalDevice):
            for changed in ('runId', 'profileDigest', 'buildId', 'startedAtMs'):
                with self.subTest(adapter=adapter.__name__, changed=changed), tempfile.TemporaryDirectory() as directory:
                    base = Path(directory) / 'Library/Application Support/Reproof'
                    base.mkdir(parents=True)
                    run_id, session_id = str(uuid.uuid4()), str(uuid.uuid4())
                    capture = case_spec('counter').capture()
                    capture.update(sessionId=session_id, startedAtMs=10)
                    marker = dict(schemaVersion=1, runId=run_id, sessionId=session_id,
                                  profileDigest=selected.digest, buildId='test-build', fixture=fixture,
                                  startedAtMs=10, finalized=True, endSequence=2)
                    replacement = dict(marker)
                    replacement[changed] = {'runId': str(uuid.uuid4()), 'profileDigest': 'b' * 64,
                                            'buildId': 'other-build', 'startedAtMs': 11}[changed]
                    documents = {'capture.json': capture,
                                 session_id + '/metadata.json': dict(sessionId=session_id, endSequence=2,
                                     fixture=fixture, startState=capture['startState'], startedAtMs=10, finalized=True),
                                 session_id + '/finalized.json': dict(finalized=True, endSequence=2)}
                    for relative, value in documents.items():
                        path = base / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps(value))
                    reader = adapter.__new__(adapter)
                    reader._data_container = lambda: Path(directory)
                    reader._installed_build_id = lambda: 'test-build'
                    reader.installed_build_id = 'test-build'
                    reads = []

                    def read(relative, **_kwargs):
                        relative = relative.removeprefix('Library/Application Support/Reproof/')
                        if relative == 'auto-session.json':
                            reads.append(relative)
                            return dict(marker if len(reads) == 1 else replacement)
                        return documents[relative]

                    reader.read_app_json = read
                    with self.assertRaises(ContractError):
                        reader.collect_capture(expected_run_id=run_id, auto_profile=selected,
                                               expected_fixture=fixture)

    def test_live_reset_rotates_host_run_id_in_native_payload(self):
        identity = {"bundle": "io.reproof.sample.ios", "artifactDigest": "a" * 64}
        provider = IosProvider("simulator", Path("products"), identity["bundle"], identity,
                               app=Path("sample.app"), fixture="counter", record_sdk=True)
        provider.auto_profile = profile()
        provider.auto_run_id = str(uuid.uuid4())
        provider._wait_for_auto_marker = lambda: {}
        captured = {}

        class ImmediateEvent:
            def set(self):
                return None

            def wait(self, _timeout):
                command = provider.commands.get_nowait()
                captured.update(command)
                provider.bridge("ack", {"id": command["id"], "ok": True})
                return True

        with patch("reproof.live.providers.installed_identity", return_value=identity), \
             patch("reproof.live.providers.threading.Event", side_effect=lambda: ImmediateEvent()):
            result = provider.execute("reset", {})
        self.assertTrue(result["ok"])
        self.assertRegex(captured["payload"]["autoRunId"],
                         r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertEqual(provider.auto_run_id, captured["payload"]["autoRunId"])

    def test_record_payload_and_attachment_bind_auto_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products = products_tree(root)
            app = app_tree(root)
            output = root / "run"
            simulator = IosSimulator.__new__(IosSimulator)
            simulator.execution_environment = "simulator"
            simulator.udid = "11111111-2222-3333-4444-555555555555"
            simulator.identity = "simulator-proof"
            simulator.install = lambda value: {"buildId": "build-auto"}
            simulator.stop = lambda: None
            observed = {
                "schemaVersion": 1,
                "runId": "placeholder",
                "scenarioDigest": "scenario-digest",
                "buildId": "build-auto",
                "runValid": True,
                "bugCondition": True,
                "expectedCondition": False,
                "finalNodes": {"counter.name": "", "counter.count": "2"},
                "steps": [],
                "mode": "record",
                "autoRunId": "placeholder",
                "profileDigest": "a" * 64,
            }
            scenario = {
                "scenarioDigest": "scenario-digest",
                "fixture": {"id": "ios-counter", "version": 1, "inputs": {}},
                "steps": [],
                "oracle": {
                    "bugCondition": {"target": "counter.count", "text": "2"},
                    "expectedCondition": {"target": "counter.count", "text": "1"},
                },
            }

            def fake_command(command, *_args, **_kwargs):
                if command[0] == "/usr/bin/xcodebuild":
                    return "xcodebuild success"
                return json.dumps({"totalTestCount": 1, "passedTests": 1,
                                   "failedTests": 0, "skippedTests": 0})

            def fake_attachment(*_args, **_kwargs):
                value = dict(observed)
                config = plistlib.loads((output / "run.xctestrun").read_bytes())
                encoded = config["ReproReplayTests"]["EnvironmentVariables"]["REPRO_SCENARIO_B64"]
                current_run[0] = json.loads(base64.b64decode(encoded))["runId"]
                value["runId"] = value["autoRunId"] = current_run[0]
                return value

            current_run = [None]
            with patch("reproof.ios_runner.run_command", side_effect=fake_command), \
                 patch("reproof.ios_runner.extract_attachment", side_effect=fake_attachment):
                result = simulator.run_scenario(products, app, scenario, output,
                                                 mode="record", auto_profile=profile())

            self.assertTrue(result["runValid"], result)
            self.assertEqual(result["autoRunId"], result["runId"])
            self.assertEqual(result["profileDigest"], "a" * 64)
            config = plistlib.loads((output / "run.xctestrun").read_bytes())
            environment = config["ReproReplayTests"]["EnvironmentVariables"]
            payload = json.loads(base64.b64decode(environment["REPRO_SCENARIO_B64"]))
            self.assertEqual(payload["autoProfileDigest"], "a" * 64)

    def test_auto_collection_pins_marker_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "Library/Application Support/Reproof"
            session = base / "session-1"
            session.mkdir(parents=True)
            capture = {"schemaVersion": 2, "sessionId": "session-1", "startedAtMs": 10,
                       "endSequence": 2, "fixture": profile().data["fixture"],
                       "startState": {"screen": "main", "nodes": {}},
                       "events": [], "truncated": False, "lostEvents": False}
            marker = {"schemaVersion": 1, "runId": "run-1", "sessionId": "session-1",
                      "profileDigest": "a" * 64, "buildId": "build-auto",
                      "fixture": profile().data["fixture"], "startedAtMs": 10,
                      "finalized": True, "endSequence": 2}
            diagnostics = {"schemaVersion": 1, "platform": "ios", "runId": "run-1",
                           "sessionId": "session-1", "profileDigest": "a" * 64,
                           "buildId": "build-auto", "endSequence": 2, "actions": []}
            (base / "capture.json").write_text(json.dumps(capture))
            (base / "auto-session.json").write_text(json.dumps(marker))
            (session / "metadata.json").write_text(json.dumps({"sessionId": "session-1",
                "endSequence": 2, "fixture": capture["fixture"], "startState": capture["startState"],
                "startedAtMs": 10, "finalized": True}))
            (session / "finalized.json").write_text(json.dumps({"finalized": True, "endSequence": 2}))
            (session / "diagnostics.json").write_text(json.dumps(diagnostics))

            simulator = IosSimulator.__new__(IosSimulator)
            simulator.udid = "11111111-2222-3333-4444-555555555555"
            simulator.simctl = lambda *args: str(root)
            (root / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "io.reproof.sample.ios",
                "ReproBuildID": "build-auto",
            }))

            with patch("reproof.ios_runner.validate_finalization", return_value=True), \
                 patch("reproof.ios_runner.validate_ios_auto_marker", return_value=True), \
                 patch("reproof.ios_runner.validate_ios_auto_diagnostics", side_effect=lambda value, *_args, **_kwargs: value):
                collected = simulator.collect_capture(expected_run_id="run-1", auto_profile=profile(),
                                                      expected_fixture=capture["fixture"])
                sidecar = simulator.collect_auto_diagnostics(collected, "run-1", profile(),
                                                            expected_fixture=capture["fixture"])
            self.assertEqual(collected["sessionId"], "session-1")
            self.assertEqual(sidecar["runId"], "run-1")


if __name__ == "__main__":
    unittest.main()
