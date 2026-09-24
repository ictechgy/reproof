"""End-to-end protocol supervision with an explicit VM lifecycle double."""
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from reproof.contracts.versions import digest
from reproof.execution.artifacts import ArtifactValidationAuthority, BlobSet
from reproof.execution.backend import ExecutionDenied, QualificationAuthority, REQUIRED_PROBES
from reproof.execution.guest import GuestError, serve_one
from reproof.execution.journal import RunStore
from reproof.execution.resources import RESOURCE_FILES, provision
from reproof.execution.runtime import MacOSVirtualizationBackend
from reproof.execution.wire import accept_bootstrap
from tests.test_execution_protocol import build_request, build_route, validation_plan
from tests.test_execution_resources import resource_inputs


class VMDouble:
    instances = []
    stop_confirmed = True
    wait_for_cancel = False
    started_recipe = threading.Event()

    def __init__(self, bundle, directory, *, deadline, cancel):
        self.bundle, self.cancel = bundle, cancel
        self.channel, self.peer = socket.socketpair()
        self.observed = []
        self.instances.append(self)
        def serve():
            channel = accept_bootstrap(self.peer, deadline=deadline)
            outer = self
            class ExecutorDouble:
                def execute(self, source, recipe, cancelled):
                    outer.observed.append(source.digest)
                    VMDouble.started_recipe.set()
                    if VMDouble.wait_for_cancel:
                        cancelled.wait(3)
                        raise GuestError("cancelled")
                    return ({"exitCode": 0, "outputTruncated": False, "logDigest": "c" * 64},
                            BlobSet((("product.bin", b"candidate artifact"),)))
            serve_one(channel, catalog=bundle.metadata["catalog"], agent_digest=bundle.metadata["agentDigest"],
                      executor=ExecutorDouble())
        self.thread = threading.Thread(target=serve)
        self.thread.start()

    def wait_ready(self):
        pass

    @property
    def evidence(self):
        return {"configured": True, "started": True, "connected": True,
                "stopped": self.stop_confirmed}

    def stop(self, **_):
        try:
            self.channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.channel.close()
        self.thread.join(4)
        self.peer.close()
        return self.stop_confirmed


class ExecutionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        metadata, paths = resource_inputs(self.root)
        self.bundle = provision(self.root / "bundle", metadata=metadata, resources=paths)
        self.authority = QualificationAuthority()
        self.store = RunStore(self.root / "store", environment_digest=self.bundle.environment_digest, disk_limit=1024)
        self.backend = MacOSVirtualizationBackend("apple-vm", self.authority, self.bundle, self.store)
        self.source = BlobSet((("src/product.py", b"frozen source"),))
        self.request = build_request()
        self.request.update(environmentDigest=self.bundle.environment_digest, inputDigest=self.source.digest,
                            recipeId="build", cleanupPolicyId="dispose-overlay")
        now = int(time.time() * 1000)
        probes = sorted(REQUIRED_PROBES["build-guest"])
        receipts = [self.authority.record_probe(probe_id=item, backend_id="apple-vm", execution_class="build-guest",
                     environment_digest=self.bundle.environment_digest, outcome="pass", evidence_digest="d" * 64,
                     observed_at_ms=now) for item in probes]
        self.qualification = self.authority.issue_backend_qualification(
            {"schemaVersion": 1, "id": "test-double-qualification", "backendId": "apple-vm", "executionClass": "build-guest",
             "environmentDigest": self.bundle.environment_digest, "issuedAtMs": now, "expiresAtMs": now + 60000,
             "probeIds": probes}, receipts, evaluated_at_ms=now)
        route = build_route()
        route.update(environmentDigest=self.bundle.environment_digest, recipeId="build", cleanupPolicyId="dispose-overlay")
        self.route = self.authority.register_execution_route(route)
        self.plan = self.authority.register_validation_plan(validation_plan())
        VMDouble.instances = []
        VMDouble.stop_confirmed = True
        VMDouble.wait_for_cancel = False
        VMDouble.started_recipe = threading.Event()
        patcher = mock.patch("reproof.execution.runtime.NativeVM", VMDouble)
        patcher.start()
        self.addCleanup(patcher.stop)

    def authorization(self):
        return self.authority.authorize(self.request, qualification=self.qualification,
            execution_route=self.route, validation_plan=self.plan, evaluated_at_ms=int(time.time() * 1000))

    def test_local_authorization_sealed_input_and_cleanup_are_required(self):
        with self.assertRaises(ExecutionDenied):
            self.backend.execute(self.request, {}, self.source)
        self.assertFalse(VMDouble.instances)
        result = self.backend.execute(self.request, self.authorization(), self.source)
        self.assertEqual(result.status, "candidate-output")
        self.assertEqual(result.artifacts.entries, (("product.bin", b"candidate artifact"),))
        self.assertFalse(result.verified)
        self.assertTrue(result.cleanup_confirmed)
        self.assertEqual(VMDouble.instances[0].observed, [self.source.digest])
        self.assertEqual(list((self.root / "store/runs").iterdir()), [])

    def test_changed_input_or_request_cannot_dispatch_and_replay_is_denied(self):
        authorization = self.authorization()
        with self.assertRaises(ExecutionDenied):
            self.backend.execute(self.request, authorization, BlobSet((("src/product.py", b"changed"),)))
        changed = {**self.request, "recipeId": "candidate-hook"}
        with self.assertRaises(ExecutionDenied):
            self.backend.execute(changed, authorization, self.source)
        self.assertFalse(VMDouble.instances)
        self.backend.execute(self.request, authorization, self.source)
        with self.assertRaises(ExecutionDenied):
            self.backend.execute(self.request, authorization, self.source)
        self.assertEqual(len(VMDouble.instances), 1)

    def test_shutdown_unknown_quarantines_and_hides_candidate_outputs(self):
        VMDouble.stop_confirmed = False
        result = self.backend.execute(self.request, self.authorization(), self.source)
        self.assertEqual(result.status, "quarantined")
        self.assertIsNone(result.artifacts)
        self.assertFalse(result.cleanup_confirmed)
        self.assertGreater(self.store.status(self.request["operationId"])["reservedBytes"], 0)
        self.request["operationId"] = "second"
        with self.assertRaises(ExecutionDenied):
            self.backend.execute(self.request, self.authorization(), self.source)

    def test_new_state_directory_cannot_bypass_a_quarantined_machine(self):
        VMDouble.stop_confirmed = False
        self.assertEqual(self.backend.execute(self.request, self.authorization(), self.source).status, "quarantined")
        another_store = RunStore(self.root / "another-store", environment_digest=self.bundle.environment_digest,
                                  disk_limit=1024)
        another_backend = MacOSVirtualizationBackend("apple-vm", self.authority, self.bundle, another_store)
        self.request["operationId"] = "bypass-attempt"
        with self.assertRaises(ExecutionDenied):
            another_backend.execute(self.request, self.authorization(), self.source)
        self.assertEqual(len(VMDouble.instances), 1)

    def test_durable_cancel_interrupts_receive_stops_guest_and_stays_cancelled(self):
        VMDouble.wait_for_cancel = True
        outcomes = []
        authorization = self.authorization()
        thread = threading.Thread(target=lambda: outcomes.append(self.backend.execute(self.request, authorization, self.source)))
        thread.start()
        self.assertTrue(VMDouble.started_recipe.wait(2))
        self.backend.cancel(self.request["operationId"], digest(self.request))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes[0].status, "cancelled")
        self.assertIsNone(outcomes[0].artifacts)
        self.assertEqual(self.store.status(self.request["operationId"])["state"], "cancelled")

    def test_base_image_changed_after_registration_is_denied_before_boot(self):
        path = self.bundle.path("disk")
        path.chmod(0o600)
        path.write_bytes(b"tampered image")
        with self.assertRaises(ExecutionDenied):
            self.backend.execute(self.request, self.authorization(), self.source)
        self.assertFalse(VMDouble.instances)

    def test_guest_success_without_native_start_evidence_is_not_published(self):
        with mock.patch.object(VMDouble, "evidence", new_callable=mock.PropertyMock,
                return_value={"configured": True, "started": False, "connected": True, "stopped": True}):
            result = self.backend.execute(self.request, self.authorization(), self.source)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.artifacts)

    def test_authority_expiring_during_transfer_prevents_recipe_dispatch(self):
        authorization = self.authorization()
        original = self.authority.check_authorization
        checks = []
        def recheck(*args, **kwargs):
            checks.append(True)
            if len(checks) == 5:
                raise ExecutionDenied("Expired during input transfer")
            return original(*args, **kwargs)
        with mock.patch.object(self.authority, "check_authorization", side_effect=recheck):
            result = self.backend.execute(self.request, authorization, self.source)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "authorization-denied")
        self.assertEqual(VMDouble.instances[0].observed, [])
        self.assertTrue(result.cleanup_confirmed)

    def test_desktop_route_requires_a_project_bound_validated_artifact(self):
        metadata = self.bundle.metadata
        metadata["environment"]["executionClass"] = "desktop-guest"
        metadata["catalog"][0]["executionClass"] = "desktop-guest"
        bundle = provision(self.root / "desktop-bundle", metadata=metadata,
                           resources={key: self.bundle.path(key) for key in RESOURCE_FILES})
        store = RunStore(self.root / "desktop-state", environment_digest=bundle.environment_digest, disk_limit=1024)
        validator = ArtifactValidationAuthority()
        validator.register("desktop-package", paths=["app.zip"], max_bytes=1024,
                           checker=lambda blobs: dict(blobs.entries)["app.zip"] == b"format fixture")
        backend = MacOSVirtualizationBackend("apple-vm", self.authority, bundle, store, artifact_authority=validator)
        inputs = BlobSet((("app.zip", b"format fixture"),))
        request = {**self.request, "executionClass": "desktop-guest", "inputKind": "validated-artifact",
                   "environmentDigest": bundle.environment_digest, "inputDigest": inputs.digest}
        now = int(time.time() * 1000)
        probe_ids = sorted(REQUIRED_PROBES["desktop-guest"])
        receipts = [self.authority.record_probe(probe_id=item, backend_id="apple-vm", execution_class="desktop-guest",
                    environment_digest=bundle.environment_digest, outcome="pass", evidence_digest="d" * 64,
                    observed_at_ms=now) for item in probe_ids]
        qualification = self.authority.issue_backend_qualification({"schemaVersion": 1,
            "id": "desktop-test-double-qualification", "backendId": "apple-vm", "executionClass": "desktop-guest",
            "environmentDigest": bundle.environment_digest, "issuedAtMs": now, "expiresAtMs": now + 60000,
            "probeIds": probe_ids}, receipts, evaluated_at_ms=now)
        route = build_route()
        route.update(id="desktop-route", executionClass="desktop-guest", inputKind="validated-artifact",
                     environmentDigest=bundle.environment_digest, recipeId="build", cleanupPolicyId="dispose-overlay")
        authorization = self.authority.authorize(request, qualification=qualification,
            execution_route=self.authority.register_execution_route(route), validation_plan=self.plan,
            evaluated_at_ms=now)
        with self.assertRaises(ExecutionDenied):
            backend.execute(request, authorization, inputs)
        with self.assertRaises(ExecutionDenied):
            backend.execute(request, authorization, {"validated": True})
        capability = validator.validate(inputs, policy_id="desktop-package", project_digest=request["projectDigest"],
                                         execution_class="desktop-guest")
        result = backend.execute(request, authorization, capability)
        self.assertEqual(result.status, "candidate-output")
        self.assertFalse(result.verified)
