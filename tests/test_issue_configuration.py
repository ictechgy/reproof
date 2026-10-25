import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import unittest
from unittest import mock

from reproof import contracts
from reproof.live.access import AccessController, AccessStore
from reproof.live.issue_configuration import (
    EnvironmentVariableResolver, LoopbackObservationAdapter, compose_issue_workflow, load_issue_configuration,
)
from reproof.scenario_runner import ObservationRequest
from tests.g4_support import G4Environment, runtime_policy


def configuration(env):
    return {"schemaVersion": 1, "kind": "reproof-issue-runtime", "projects": [{
        "projectId": "checkout", "projectDigest": env.registration.project_digest,
        "runtimePolicy": runtime_policy(), "validationRecipeIds": ["regression_ui"],
        "fixtures": [{"applicationId": "ios_app", "fixtureId": "seed_account", "endpointId": "fixture_service",
            "baseUrl": f"http://127.0.0.1:{env.remote.port}", "checkRecipeIds": ["check_account"],
            "cleanupRecipeId": "cleanup_account", "payload": {}}],
        "variables": [{"variableId": "secret_text", "environment": "REPRO_QA_OWNED_TEXT"}],
        "observations": [{"observationId": "screen", "providerIncarnation": "owned_observation",
            "baseUrl": "http://127.0.0.1:12345", "coverage": ["snapshot"]}],
    }]}


class IssueConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.env = G4Environment(); self.addCleanup(self.env.close)
        self.path = self.env.root / "issue-configuration.json"

    def load(self, document):
        self.path.write_text(json.dumps(document))
        return load_issue_configuration(self.path)

    def test_local_composition_registers_adapters_without_resolving_or_disclosing_secrets(self):
        store = AccessStore(self.env.root / "coordinator-v2"); self.addCleanup(store.close)
        store.bootstrap_administrator("admin"); store.register_project("admin", self.env.project)
        access = AccessController(store); access.bind_project(self.env.registration)
        with mock.patch.object(EnvironmentVariableResolver, "resolve", side_effect=AssertionError("premature resolution")):
            bundle = compose_issue_workflow(self.env.lab, access, self.load(configuration(self.env)), root=self.env.root / "configured-issues")
        self.addCleanup(bundle.close)
        runtime = bundle.workflow.runtimes["checkout"]
        self.assertEqual(len(runtime.preparations), 1)
        self.assertEqual(runtime.service.runner.observations.adapter("screen", "snapshot").provider_incarnation, "owned_observation")
        with mock.patch.dict(os.environ, {"REPRO_QA_OWNED_TEXT": "owned-secret-value"}):
            value, secret = runtime.service.runner.variables.resolve("secret_text", {})
        self.assertEqual(value, "owned-secret-value"); self.assertTrue(secret)
        self.assertNotIn("owned-secret-value", self.path.read_text())

    def test_configuration_rejects_executable_fields_external_endpoints_and_duplicate_projects(self):
        for change in (
            lambda d: d.update(command="arbitrary-command"),
            lambda d: d.update(schemaVersion=True),
            lambda d: d["projects"].append(copy.deepcopy(d["projects"][0])),
            lambda d: d["projects"][0]["variables"][0].update(value="plaintext-secret"),
            lambda d: d["projects"][0]["observations"][0].update(baseUrl="http://example.invalid:80"),
            lambda d: d["projects"][0]["observations"][0].update(coverage=["imagined"]),
        ):
            document = configuration(self.env); change(document)
            with self.subTest(change=change), self.assertRaises(contracts.ContractError): self.load(document)

    def test_stale_registration_cannot_compose_a_runtime(self):
        store = AccessStore(self.env.root / "coordinator-v2"); self.addCleanup(store.close)
        store.bootstrap_administrator("admin"); store.register_project("admin", self.env.project)
        access = AccessController(store); access.bind_project(self.env.registration)
        document = configuration(self.env); document["projects"][0]["projectDigest"] = "f" * 64
        with self.assertRaises(contracts.ContractError):
            compose_issue_workflow(self.env.lab, access, self.load(document), root=self.env.root / "configured-issues")

    def test_environment_types_fail_without_printing_values(self):
        for kind, supplied, expected in (("boolean", "false", False), ("integer", "42", 42), ("string", "owned", "owned")):
            with mock.patch.dict(os.environ, {"REPRO_QA_OWNED_TEXT": supplied}):
                self.assertEqual(EnvironmentVariableResolver("REPRO_QA_OWNED_TEXT", kind).resolve({}), expected)
        with mock.patch.dict(os.environ, {"REPRO_QA_OWNED_TEXT": "owned-not-a-number"}):
            with self.assertRaises(contracts.ContractError) as error:
                EnvironmentVariableResolver("REPRO_QA_OWNED_TEXT", "integer").resolve({})
            self.assertNotIn("owned-not-a-number", str(error.exception))


class ObservationHttpTests(unittest.TestCase):
    def setUp(self):
        self.env = G4Environment(); self.addCleanup(self.env.close)
        self.handle = self.env.service.start_prepared_recording(device_id="device", owner="owner", controller_id="browser",
            registration=self.env.registration, application_id="ios_app", build_id="original", preparations=self.env.preparations())
        self.addCleanup(lambda: self.env.service.stop(self.handle))
        self.mode = "partial"; self.received = []; self.responses = []
        self.before_response = lambda _request: None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.received.append(request)
                owner.before_response(request)
                captured = int(time.time() * 1000)
                envelope = {"schemaVersion": 1, "id": "screen", "providerIncarnation": "owned_observation",
                    "applicationId": "ios_app", "intervalMs": {"start": captured, "end": captured},
                    "clockUncertaintyMs": 0, "scope": "root", "targets": [], "properties": ["text"],
                    "limits": {"nodes": 1, "bytes": 4096, "depth": 1}, "truncated": True, "errors": [],
                    "completeness": "partial", "coverage": "snapshot"}
                if owner.mode == "wrong-provider": envelope["providerIncarnation"] = "other_provider"
                if owner.mode == "echo-request-application": envelope["applicationId"] = request["applicationId"]
                if owner.mode == "slow": time.sleep(.2)
                body = json.dumps({"schemaVersion": 1, "requestId": request["requestId"] if owner.mode != "wrong-request" else "different",
                    "envelope": envelope, "values": {"text": "observed-error"}, "absentProperties": []}).encode()
                if owner.mode == "replay": body = owner.responses[-1]
                owner.responses.append(body)
                try:
                    self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close_server)
        self.adapter = LoopbackObservationAdapter(project_digest=self.env.registration.project_digest,
            observation_id="screen", provider_incarnation="owned_observation", base_url=f"http://127.0.0.1:{self.server.server_port}", coverage_classes=["snapshot"])

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def request(self, timeout=3, application_id="ios_app"):
        now = int(time.time() * 1000)
        return ObservationRequest("screen", "defect", application_id, {"class": "snapshot", "windowMs": {"start": now, "end": now},
            "maxUncertaintyMs": 0, "maxAgeMs": 60000, "scope": "root", "properties": ["text"]}, now,
            time.monotonic() + timeout, self.handle.session_id, "owner", self.env.lab)

    def test_actual_response_keeps_partial_coverage_and_measured_values(self):
        result = self.adapter.observe(self.request())
        self.assertEqual(result.values, {"text": "observed-error"})
        self.assertTrue(result.envelope["truncated"])
        self.assertEqual(result.envelope["completeness"], "partial")
        self.assertEqual(len(self.received), 1)
        self.assertEqual(set(self.received[0]), {"schemaVersion", "requestId", "projectDigest", "observationId",
            "applicationId", "sessionId", "anchorMs", "requirement"})
        self.assertEqual(self.received[0]["schemaVersion"], 1)
        self.assertEqual(result.absent_properties, ())
        binding = self.env.lab.release_binding(self.handle.session_id, "owner")
        self.assertNotEqual(binding["providerIncarnation"], result.envelope["providerIncarnation"])

    def test_wrong_application_is_rejected_before_http(self):
        self.mode = "echo-request-application"
        with self.assertRaises(contracts.ContractError) as error:
            self.adapter.observe(self.request(application_id="private_unselected_app"))
        self.assertEqual(self.received, [])
        self.assertNotIn("private_unselected_app", str(error.exception))

    def test_missing_local_project_or_application_binding_fails_before_http(self):
        for field in ("projectDigest", "applicationId"):
            with self.subTest(field=field):
                binding = self.env.lab.release_binding(self.handle.session_id, "owner")
                del binding[field]
                with mock.patch.object(self.env.lab, "release_binding", return_value=binding):
                    with self.assertRaises(contracts.ContractError) as error: self.adapter.observe(self.request())
                self.assertEqual(error.exception.code, "observation_binding")
                self.assertNotIn(field, str(error.exception))
                self.assertEqual(self.received, [])

    def test_each_inflight_release_binding_change_is_rejected(self):
        for field, changed in (
            ("projectId", "other_project"), ("projectRevision", 2), ("projectDigest", "f" * 64),
            ("applicationId", "other_app"), ("buildId", "other_build"), ("artifactDigest", "f" * 64),
            ("recordingId", "other_recording"), ("providerIncarnation", "other_device_provider"),
        ):
            with self.subTest(field=field):
                binding = self.env.lab.release_binding(self.handle.session_id, "owner")
                self.assertNotEqual(binding[field], changed)
                self.received.clear()
                # Reuse one mutable result to require an independent pre-call
                # snapshot, not merely a second read of the same dictionary.
                self.before_response = lambda _request: binding.update({field: changed})
                with mock.patch.object(self.env.lab, "release_binding", return_value=binding) as release_binding:
                    with self.assertRaises(contracts.ContractError) as error:
                        self.adapter.observe(self.request())
                self.assertEqual(len(self.received), 1)
                self.assertEqual(release_binding.call_args_list, [mock.call(self.handle.session_id, "owner")] * 2)
                self.assertNotIn(changed if type(changed) is str else str(changed), str(error.exception))

    def test_live_device_provider_change_is_rejected_without_conflating_observation_provider(self):
        session = self.env.lab._session(self.handle.session_id, "owner")
        original = session["providerIncarnation"]
        def change_provider(_request):
            with session["lock"]: session["providerIncarnation"] = "other_device_provider"
        self.before_response = change_provider
        try:
            with self.assertRaises(contracts.ContractError) as error: self.adapter.observe(self.request())
            self.assertEqual(error.exception.code, "observation_binding")
            self.assertEqual(len(self.received), 1)
            self.assertEqual(json.loads(self.responses[0])["envelope"]["providerIncarnation"], "owned_observation")
        finally:
            with session["lock"]: session["providerIncarnation"] = original

    def test_unavailable_binding_after_http_is_not_returned_as_evidence(self):
        binding = self.env.lab.release_binding(self.handle.session_id, "owner")
        failure = contracts.ContractError("Release binding is unavailable")
        with mock.patch.object(self.env.lab, "release_binding", side_effect=[binding, failure]):
            with self.assertRaises(contracts.ContractError) as error: self.adapter.observe(self.request())
        self.assertIs(error.exception, failure)
        self.assertEqual(len(self.received), 1)

    def test_returned_response_cannot_outlive_absolute_deadline(self):
        for elapsed in (0, .1):
            with self.subTest(elapsed=elapsed):
                request = self.request()
                with mock.patch("reproof.live.issue_configuration.time") as clock:
                    clock.monotonic.return_value = time.monotonic()
                    self.before_response = lambda _request: setattr(clock.monotonic, "return_value", request.deadline_monotonic + elapsed)
                    with self.assertRaises(contracts.ContractError): self.adapter.observe(request)
                self.assertEqual(len(self.received), 1)
                self.received.clear()

    def test_binding_recheck_cannot_extend_absolute_deadline(self):
        request = self.request()
        binding = self.env.lab.release_binding(self.handle.session_id, "owner")
        reads = []
        with mock.patch("reproof.live.issue_configuration.time") as clock:
            clock.monotonic.return_value = time.monotonic()
            def read_binding(session_id, owner):
                reads.append((session_id, owner))
                if len(reads) == 2: clock.monotonic.return_value = request.deadline_monotonic
                return copy.deepcopy(binding)
            with mock.patch.object(self.env.lab, "release_binding", side_effect=read_binding):
                with self.assertRaises(contracts.ContractError) as error: self.adapter.observe(request)
        self.assertEqual(error.exception.code, "observation_timeout")
        self.assertEqual(reads, [(self.handle.session_id, "owner")] * 2)
        self.assertEqual(len(self.received), 1)

    def test_prior_response_cannot_be_replayed_for_a_new_request(self):
        self.adapter.observe(self.request())
        self.mode = "replay"
        with self.assertRaises(contracts.ContractError): self.adapter.observe(self.request())
        self.assertEqual(len(self.received), 2)
        self.assertNotEqual(self.received[0]["requestId"], self.received[1]["requestId"])
        self.assertEqual(self.responses[0], self.responses[1])

    def test_wrong_request_or_provider_cannot_become_observation_evidence(self):
        for mode in ("wrong-request", "wrong-provider"):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(contracts.ContractError): self.adapter.observe(self.request())

    def test_observation_deadline_closes_an_actual_slow_response(self):
        self.mode = "slow"; started = time.monotonic()
        with self.assertRaises(contracts.ContractError): self.adapter.observe(self.request(.04))
        self.assertLess(time.monotonic() - started, .5)
