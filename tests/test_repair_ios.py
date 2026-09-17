"""Persistent iOS trusted-adapter service integration over owned doubles."""
from dataclasses import replace
import json
import threading
import time
import unittest
from types import SimpleNamespace

from reproloop import contracts
from reproloop.execution.artifacts import ArtifactValidationAuthority, BlobSet
from reproloop.execution.backend import REQUIRED_PROBES
from reproloop.execution.journal import RunStore
from reproloop.repair_mobile import ProtectedMobileSupervisor
from reproloop.repair_signing import TrustedSigningSupervisor
from reproloop.repair_execution import RepairExecutionError
from reproloop.repair_ios import IOSTrustedMobileAdapter
from reproloop.repair_mobile import MobileFailureObservation, MobileInstallationObservation
from reproloop.repair_callbacks import invoke_fixed
from tests.ios_service_support import IOSServiceFixture
from tests.g9_execution_support import SyntheticRepairExecution
from tests.test_execution_protocol import build_route


class IOSServiceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.fixture = IOSServiceFixture()
        self.adapter = None
        self._manager = None

    def tearDown(self):
        manager = getattr(self, "_manager", None)
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass
        if self.adapter is not None:
            try:
                self.adapter.close(deadline_monotonic=time.monotonic() + 20)
            except Exception:
                pass
        self.fixture.close()

    def bounds(self, **changes):
        value = {"cancellation": threading.Event(),
                 "deadline_monotonic": time.monotonic() + 90}
        value.update(changes)
        return value

    def open_adapter(self):
        self.adapter = IOSTrustedMobileAdapter(
            self.fixture.config, operations=self.fixture.operations)
        return self.adapter

    def fixed(self, callback, *args, cancellation=None):
        value, returned = invoke_fixed(
            callback, *args, cancellation=cancellation or threading.Event(),
            deadline_monotonic=time.monotonic() + 120)
        self.assertTrue(returned)
        return value

    def admitted(self):
        manager = self.fixture.admit()
        operation = manager.__enter__()
        context = replace(self.fixture.context, _operation_binding=operation)
        self._manager = manager
        return context

    def test_constructor_rejects_missing_or_wrong_sanitation_policy(self):
        for value in (None, object()):
            with self.subTest(value=type(value).__name__):
                config = replace(self.fixture.config, sanitation=value)
                with self.assertRaises(RepairExecutionError):
                    IOSTrustedMobileAdapter(config, operations=self.fixture.operations)

    def test_trusted_adapter_exposes_bound_scope_and_callbacks(self):
        adapter = self.open_adapter()
        trusted = adapter.trusted_adapter(adapter_id="ios-service-adapter")
        self.assertEqual(trusted.scope_digest, self.fixture.config.scope_digest)
        self.assertEqual(trusted.device_id, self.fixture.config.device_id)
        for callback, method in ((trusted.install, adapter.install),
                                 (trusted.replay, adapter.replay),
                                 (trusted.cleanup, adapter.cleanup)):
            self.assertIs(callback.__self__, method.__self__)
            self.assertIs(callback.__func__, method.__func__)

    def test_install_three_service_replays_fresh_sanitation_and_original_restore(self):
        adapter = self.open_adapter()
        context = self.admitted()
        installed = self.fixed(adapter.install, context, self.fixture.candidate)
        self.assertIsInstance(installed, MobileInstallationObservation)
        results = []
        for number in (1, 2, 3):
            results.append(self.fixed(adapter.replay, context, self.fixture.execution, number))
        self.assertTrue(all(not isinstance(result, MobileFailureObservation) for result in results))
        self.assertEqual(len({result.run_id for result in results}), 3)
        cleaned = self.fixed(adapter.cleanup, context)
        self.assertTrue(cleaned.termination_confirmed, cleaned)
        self.assertTrue(cleaned.fixture_cleanup_confirmed, cleaned)
        self.assertTrue(cleaned.sanitation_confirmed, cleaned)
        self.assertTrue(cleaned.ownership_released, cleaned)
        self.assertEqual(self.fixture.stage_path.read_text(), "cleanup")
        self.assertEqual(self.fixture.env.lab.list_devices()[0]["state"], "available")
        self.assertGreaterEqual(len(self.fixture.doubles), 4)
        self.assertTrue(all(double.activated for double in self.fixture.doubles))
        context._operation_binding.run.finish("succeeded", stopped=True)
        status = self.fixture.runs.status(context.operation_id)
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["reservedBytes"], 0)

    def test_wrong_context_cleanup_cannot_quarantine_active_scope(self):
        adapter = self.open_adapter()
        context = self.admitted()
        installed = self.fixed(adapter.install, context, self.fixture.candidate)
        self.assertIsInstance(installed, MobileInstallationObservation)
        foreign = replace(context, nonce="foreign-context")
        rejected = adapter.cleanup(foreign, **self.bounds())
        self.assertFalse(rejected.ownership_released)
        self.assertEqual(self.fixture.env.lab.list_devices()[0]["state"], "reserved")
        cleaned = self.fixed(adapter.cleanup, context)
        self.assertTrue(cleaned.ownership_released, cleaned)
        context._operation_binding.run.finish("succeeded", stopped=True)
        self.assertEqual(self.fixture.runs.status(context.operation_id)["state"], "succeeded")

    def test_malformed_sanitation_keeps_native_scope_quarantined(self):
        adapter = self.open_adapter()
        context = self.admitted()
        installed = adapter.install(context, self.fixture.candidate, **self.bounds())
        self.assertIsInstance(installed, MobileInstallationObservation)
        self.fixture.mode_path.write_text("malformed")
        try:
            result = adapter.replay(context, self.fixture.execution, 1, **self.bounds())
        except Exception:
            result = None
        cleaned = adapter.cleanup(context, **self.bounds())
        self.assertFalse(cleaned.sanitation_confirmed)
        self.assertFalse(cleaned.ownership_released)
        self.assertEqual(self.fixture.env.lab.list_devices()[0]["state"], "quarantined")

    def test_cancelled_replay_retains_scope_and_candidate(self):
        adapter = self.open_adapter()
        context = self.admitted()
        installed = adapter.install(context, self.fixture.candidate, **self.bounds())
        self.assertIsInstance(installed, MobileInstallationObservation)
        cancellation = threading.Event()
        self.fixture.cancel_event = cancellation
        try:
            result = adapter.replay(context, self.fixture.execution, 1,
                                    **self.bounds(cancellation=cancellation))
        except Exception:
            result = None
        self.assertTrue(result is None or isinstance(result, MobileFailureObservation))
        cleaned = adapter.cleanup(context, **self.bounds())
        self.assertFalse(cleaned.ownership_released)
        self.assertEqual(self.fixture.env.lab.list_devices()[0]["state"], "quarantined")

    def test_fixture_cleanup_failure_keeps_scope_quarantined(self):
        adapter = self.open_adapter()
        context = self.admitted()
        installed = adapter.install(context, self.fixture.candidate, **self.bounds())
        self.assertIsInstance(installed, MobileInstallationObservation)
        self.fixture.env.remote.control(fail_cleanup=True)
        try:
            result = adapter.replay(context, self.fixture.execution, 1, **self.bounds())
        except Exception:
            result = None
        cleaned = adapter.cleanup(context, **self.bounds())
        self.assertFalse(cleaned.fixture_cleanup_confirmed)
        self.assertFalse(cleaned.ownership_released)
        self.assertEqual(self.fixture.env.lab.list_devices()[0]["state"], "quarantined")

    def test_protected_supervisor_runs_real_ios_adapter_and_finalizes_accounting(self):
        fixture = self.fixture
        fixture.env.approved = fixture.approved
        fixture.env.observations._adapters["screen"].value = "success"
        source_blobs = BlobSet((("product.bin", b"candidate artifact"),))
        runtime_env = SimpleNamespace(
            root=fixture.root / "synthetic-runtime",
            registration=fixture.registration, source_blobs=source_blobs)
        runtime_env.root.mkdir(mode=0o700)
        runtime = SyntheticRepairExecution(runtime_env)
        self.addCleanup(runtime.close)

        runtime.signed_blobs = fixture.candidate
        signed_artifacts = ArtifactValidationAuthority()
        signed_artifacts.register(
            "signed-ios-ipa", paths=("candidate.ipa",), max_bytes=len(fixture.candidate.entries[0][1]) + 1,
            checker=lambda blobs: blobs == runtime.signed_blobs)
        runtime.signer = TrustedSigningSupervisor(
            runtime.builder, authority=runtime.authority, policy=runtime.signing_policy,
            policy_document=json.loads(runtime.signer._policy_document), signer=runtime.sign,
            inspector=runtime.inspect, artifact_authority=signed_artifacts,
            artifact_policy_id="signed-ios-ipa", scope_digest=contracts.digest({"iosSigning": str(runtime.root)}),
            store=RunStore(runtime.root / "ios-sign-state",
                           environment_digest=contracts.digest({"iosSigning": str(runtime.root)}),
                           disk_limit=1))
        runtime.signing_policy = runtime.signer.policy

        route_document = build_route()
        route_document.update(
            id="ios-supervisor-route", projectDigest=fixture.registration.project_digest,
            executionClass="mobile-device", inputKind="validated-artifact",
            backendId="ios-service-double",
            environmentDigest=fixture.operations.run_store.environment_digest,
            recipeId="mobile-replay", artifactPolicyId="signed-ios-ipa",
            cleanupPolicyId="ios-service-cleanup", platform="ios",
            applicationId=fixture.config.application_id,
            signingPolicyId=runtime.signer.policy.policy_id)
        route = runtime.authority.register_execution_route(route_document)
        now = int(time.time() * 1000)
        probes = sorted(REQUIRED_PROBES["mobile-device"])
        receipts = [runtime.authority.record_probe(
            probe_id=probe, backend_id="ios-service-double", execution_class="mobile-device",
            environment_digest=fixture.operations.run_store.environment_digest, outcome="pass",
            evidence_digest=contracts.digest("explicit iOS service test double"), observed_at_ms=now)
                    for probe in probes]
        qualification = runtime.authority.issue_backend_qualification({
            "schemaVersion": 1, "id": "ios-supervisor-qualification",
            "backendId": "ios-service-double", "executionClass": "mobile-device",
            "environmentDigest": fixture.operations.run_store.environment_digest,
            "issuedAtMs": now, "expiresAtMs": now + 600000, "probeIds": probes,
            "signingPolicyId": runtime.signer.policy.policy_id}, receipts, evaluated_at_ms=now)
        adapter = IOSTrustedMobileAdapter(fixture.config, operations=fixture.operations)
        self.adapter = adapter
        trusted = adapter.trusted_adapter(adapter_id="ios-supervisor-adapter")
        supervisor = ProtectedMobileSupervisor(
            authority=runtime.authority, qualification=qualification, route=route,
            validation_plan=runtime.plan, signer=runtime.signer, validators=runtime.validators,
            adapter=trusted, runner=fixture.env.runner, store=fixture.operations.run_store,
            timeout_seconds=120, operations=fixture.operations)
        signed = runtime.signed_build()
        progress = []
        proof = supervisor.verify(
            signed, fixture.approved, operation_id="ios-supervisor-operation",
            cancellation=threading.Event(), boundary=lambda: None,
            progress=lambda phase, data: progress.append(phase))
        document = supervisor.require_verified(proof, signed, fixture.approved)
        self.assertEqual(len(document["attempts"]), 3)
        self.assertEqual(fixture.operations.run_store.status("ios-supervisor-operation")["state"], "succeeded")
        self.assertEqual(fixture.operations.run_store.status("ios-supervisor-operation")["reservedBytes"], 0)
        self.assertEqual(fixture.env.lab.list_devices()[0]["state"], "available")
        self.assertEqual(signed.artifact_digest, fixture.context.artifact_digest)


if __name__ == "__main__":
    unittest.main()
