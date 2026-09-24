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
        self.mode = "partial"; self.received = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.received.append(request)
                captured = int(time.time() * 1000)
                envelope = {"schemaVersion": 1, "id": "screen", "providerIncarnation": "owned_observation",
                    "applicationId": "ios_app", "intervalMs": {"start": captured, "end": captured},
                    "clockUncertaintyMs": 0, "scope": "root", "targets": [], "properties": ["text"],
                    "limits": {"nodes": 1, "bytes": 4096, "depth": 1}, "truncated": True, "errors": [],
                    "completeness": "partial", "coverage": "snapshot"}
                if owner.mode == "wrong-provider": envelope["providerIncarnation"] = "other_provider"
                if owner.mode == "slow": time.sleep(.2)
                body = json.dumps({"schemaVersion": 1, "requestId": request["requestId"] if owner.mode != "wrong-request" else "different",
                    "envelope": envelope, "values": {"text": "observed-error"}, "absentProperties": []}).encode()
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

    def request(self, timeout=3):
        now = int(time.time() * 1000)
        return ObservationRequest("screen", "defect", "ios_app", {"class": "snapshot", "windowMs": {"start": now, "end": now},
            "maxUncertaintyMs": 0, "maxAgeMs": 60000, "scope": "root", "properties": ["text"]}, now,
            time.monotonic() + timeout, self.handle.session_id, "owner", self.env.lab)

    def test_actual_response_keeps_partial_coverage_and_measured_values(self):
        result = self.adapter.observe(self.request())
        self.assertEqual(result.values, {"text": "observed-error"})
        self.assertTrue(result.envelope["truncated"])
        self.assertEqual(result.envelope["completeness"], "partial")
        self.assertEqual(len(self.received), 1)
        self.assertNotIn("owner", self.received[0])

    def test_wrong_request_or_provider_cannot_become_observation_evidence(self):
        for mode in ("wrong-request", "wrong-provider"):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(contracts.ContractError): self.adapter.observe(self.request())

    def test_observation_deadline_closes_an_actual_slow_response(self):
        self.mode = "slow"; started = time.monotonic()
        with self.assertRaises(contracts.ContractError): self.adapter.observe(self.request(.04))
        self.assertLess(time.monotonic() - started, .5)
