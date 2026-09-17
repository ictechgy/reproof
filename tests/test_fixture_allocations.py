import copy
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reproloop.fixtures import (AdapterCapabilities, FixtureCoordinator,
                                FixtureError, LoopbackFixtureAdapter)
from reproloop.live.model import Lab
from reproloop.live.clock_sync import ClockSynchronizer
from tests.test_clock_sync import FakeClock


CAPABILITIES = {"remoteFencing": True, "terminalStatus": True,
                "idempotencyRetentionMs": 60_000}


def project_document():
    fixture = lambda identifier, operation: {
        "id": identifier, "kind": "cleanup" if operation == "cleanup" else "fixture",
        "productFile": f"fixtures/{identifier}.json", "operation": operation,
        "endpointId": "fixture_service", "exclusive": True,
        "capabilities": copy.deepcopy(CAPABILITIES),
    }
    return {
        "schemaVersion": 1, "id": "checkout", "revision": "r1",
        "trustGroup": "qa", "applications": [
            {"id": "ios_app", "platform": "ios", "bundle": "com.example.app"}],
        "builds": [
            {"id": "original", "applicationId": "ios_app", "revision": "a",
             "artifactDigest": "0" * 64, "sourceDigest": "1" * 64,
             "provenance": "trusted-build"},
            {"id": "candidate", "applicationId": "ios_app", "revision": "b",
             "artifactDigest": "9" * 64, "sourceDigest": "8" * 64,
             "provenance": "trusted-build"}],
        "variables": [{"id": "secret_text", "type": "secret-reference",
                       "secret": True}],
        "fixtures": [fixture("seed_account", "prepare"),
                     fixture("check_account", "check"),
                     fixture("cleanup_account", "cleanup")],
        "observations": ["screen"],
        "evidencePolicy": {"pixels": True, "text": True,
                           "accessibility": True, "logs": False,
                           "fixtures": True, "unknownSensitive": "deny",
                           "aiEligible": False},
        "editablePaths": ["src/Checkout.swift"],
        "recipes": [{"id": "regression_ui", "kind": "regression",
                     "productFile": "checks/ui.json", "operation": "regression"}],
        "executionClasses": ["mobile-device"],
    }


def collection_policy():
    return {"schemaVersion": 1, "captureMode": "test-data",
            "retentionSeconds": {key: 86_400 for key in
                                 ("original", "intermediate", "derivative", "export")}}


def _read_state(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"operations": {}, "dispatches": {}, "failCleanup": False}


def _fixture_server(port, state_path, stop, started):
    lock = threading.Lock()

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value):
            body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            try:
                self.send_response(status);self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)));self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                value = json.loads(self.rfile.read(size))
            except Exception:
                self.reply(400, {});return
            with lock:
                state = _read_state(state_path)
                if self.path == "/control":
                    state["failCleanup"] = value.get("failCleanup") is True
                    Path(state_path).write_text(json.dumps(state))
                    self.reply(200, {"ok": True});return
                operation_id = value.get("operationId")
                if self.path == "/status":
                    item = state["operations"].get(operation_id)
                    if item is None:
                        item = {"operationId": operation_id,
                                "generation": value.get("generation", 1),
                                "status": "unknown", "completedAtMs": None,
                                "retentionExpiresAtMs": None,
                                "fence": value.get("generation", 1)}
                    elif (item["retentionExpiresAtMs"] is not None
                          and item["retentionExpiresAtMs"] <= int(time.time() * 1000)):
                        item = dict(item, status="expired")
                    self.reply(200, {key: item[key] for key in
                                     ("operationId", "generation", "status",
                                      "completedAtMs", "retentionExpiresAtMs", "fence")})
                    return
                if self.path != "/operations":
                    self.reply(404, {});return
                old = state["operations"].get(operation_id)
                identity = (value.get("generation"), value.get("payloadDigest"),
                            value.get("idempotencyKey"), value.get("operation"))
                if old is not None:
                    if tuple(old["identity"]) != identity:
                        self.reply(409, {});return
                    self.reply(200, {key: old[key] for key in
                                     ("operationId", "generation", "status",
                                      "completedAtMs", "retentionExpiresAtMs", "fence")})
                    return
                now = int(time.time() * 1000)
                delay = value.get("payload", {}).get("delayMs", 0)
                retention = value.get("payload", {}).get("retentionMs", 60_000)
                status = ("failed" if value.get("operation") == "cleanup"
                          and state["failCleanup"] else "running" if delay else "complete")
                item = {"operationId": operation_id, "generation": value["generation"],
                        "status": status,
                        "completedAtMs": None if status == "running" else now,
                        "retentionExpiresAtMs": None if status == "running" else now + retention,
                        "fence": value["generation"], "identity": list(identity)}
                state["operations"][operation_id] = item
                state["dispatches"][operation_id] = 1
                Path(state_path).write_text(json.dumps(state))

            if delay:
                time.sleep(delay / 1000)
                with lock:
                    state = _read_state(state_path);now = int(time.time() * 1000)
                    item = state["operations"][operation_id]
                    item.update(status="complete", completedAtMs=now,
                                retentionExpiresAtMs=now + retention)
                    Path(state_path).write_text(json.dumps(state))
            self.reply(200, {key: item[key] for key in
                             ("operationId", "generation", "status", "completedAtMs",
                              "retentionExpiresAtMs", "fence")})

    server = Server(("127.0.0.1", port), Handler);server.timeout = .05
    started.set()
    while not stop.is_set():server.handle_request()
    server.server_close()


class LoopbackService:
    def __init__(self, root):
        self.state = Path(root) / "remote-state.json"
        if not self.state.exists():
            self.state.write_text(json.dumps({"operations": {}, "dispatches": {},
                                              "failCleanup": False}))
        sock = socket.socket();sock.bind(("127.0.0.1", 0));self.port = sock.getsockname()[1]
        sock.close();self.start()

    def start(self):
        self.stop = multiprocessing.Event();self.started = multiprocessing.Event()
        self.process = multiprocessing.Process(target=_fixture_server,
            args=(self.port, str(self.state), self.stop, self.started))
        self.process.start()
        if not self.started.wait(3):raise RuntimeError("fixture server did not start")

    def restart(self):
        self.close();self.start()

    def control(self, *, fail_cleanup):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        body = json.dumps({"failCleanup": fail_cleanup})
        connection.request("POST", "/control", body=body,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse();response.read();connection.close()
        if response.status != 200:raise RuntimeError("control failed")

    def close(self):
        if getattr(self, "process", None) is None:return
        self.stop.set();self.process.join(3)
        if self.process.is_alive():self.process.terminate();self.process.join(3)
        self.process = None


class FixtureAllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();root = Path(self.temp.name)
        self.remote = LoopbackService(root);self.lab = Lab(
            [], root / "lab", recording_clock_sync=ClockSynchronizer(FakeClock()),
            recording_wall_clock_ms=lambda: int(time.time() * 1000))
        self.registration = self.lab.register_recording_project(
            project_document(), collection_policy(), capacity_bytes=32 * 1024 * 1024,
            journal_headroom_bytes=512 * 1024)
        self.coordinator = FixtureCoordinator(root / "fixture-store")
        adapter = LoopbackFixtureAdapter(
            "fixture_service", f"http://127.0.0.1:{self.remote.port}",
            capabilities=AdapterCapabilities(True, True, 60_000))
        self.plan = self.coordinator.register_plan(
            self.registration, application_id="ios_app", fixture_id="seed_account",
            adapter=adapter, check_recipe_ids=("check_account",),
            cleanup_recipe_id="cleanup_account")

    def tearDown(self):
        self.coordinator.close();self.lab.close_all();self.remote.close();self.temp.cleanup()

    def test_exclusive_contention_is_held_until_confirmed_cleanup(self):
        first = self.coordinator.reserve(self.plan, owner="owner-a", device_id="device-a")
        prepared = self.coordinator.prepare(self.plan, first, payload={"account": 1},
                                            operation_id="operation_prepare_a")
        self.assertEqual(prepared.status, "complete");self.assertEqual(len(prepared.receipts), 2)
        with self.assertRaises(FixtureError) as caught:
            self.coordinator.reserve(self.plan, owner="owner-b", device_id="device-b")
        self.assertEqual(caught.exception.code, "fixture_busy")
        cleaned = self.coordinator.cleanup(self.plan, first,
                                           operation_id="operation_cleanup_a")
        self.assertEqual(cleaned["status"], "complete")
        second = self.coordinator.reserve(self.plan, owner="owner-b", device_id="device-b")
        self.assertEqual(second.generation, first.generation + 1)
        with self.assertRaises(FixtureError) as stale:
            self.coordinator.cleanup(self.plan, first, operation_id="operation_stale")
        self.assertEqual(stale.exception.code, "stale_generation")

    def test_adapter_refuses_non_loopback_and_dynamic_paths(self):
        capabilities=AdapterCapabilities(True,True,60_000)
        for endpoint in ("https://example.com:443", "http://127.0.0.1:9/dynamic",
                         "http://user@127.0.0.1:9"):
            with self.assertRaises(FixtureError):
                LoopbackFixtureAdapter("fixture_service",endpoint,
                                       capabilities=capabilities)

    def test_duplicate_identity_is_not_dispatched_and_changed_payload_conflicts(self):
        allocation = self.coordinator.reserve(self.plan, owner="owner", device_id="device")
        first = self.coordinator.prepare(self.plan, allocation, payload={"account": 1},
                                         operation_id="operation_duplicate")
        second = self.coordinator.prepare(self.plan, allocation, payload={"account": 1},
                                          operation_id="operation_duplicate")
        self.assertEqual(first.receipts[0]["payloadDigest"], second.receipts[0]["payloadDigest"])
        with self.assertRaises(FixtureError) as caught:
            self.coordinator.prepare(self.plan, allocation, payload={"account": 2},
                                     operation_id="operation_duplicate")
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual(_read_state(self.remote.state)["dispatches"]["operation_duplicate"], 1)

    def test_late_prepare_after_cleanup_remains_quarantined_until_new_cleanup(self):
        allocation = self.coordinator.reserve(self.plan, owner="owner", device_id="device")
        prepared = self.coordinator.prepare(self.plan, allocation,
            payload={"delayMs": 250}, operation_id="operation_delayed", timeout_seconds=.05)
        self.assertEqual(prepared.status, "unknown")
        first_cleanup = self.coordinator.cleanup(
            self.plan, allocation, operation_id="operation_early_cleanup")
        self.assertEqual(first_cleanup["status"], "complete")
        self.assertEqual(self.coordinator.status(allocation)["state"], "quarantined")
        time.sleep(.3);self.coordinator.reconcile(self.plan, allocation, timeout_seconds=.2)
        self.assertEqual(self.coordinator.status(allocation)["state"], "quarantined")
        final = self.coordinator.cleanup(
            self.plan, allocation, operation_id="operation_final_cleanup")
        self.assertEqual(final["status"], "complete")
        self.assertEqual(self.coordinator.status(allocation)["state"], "available")

    def test_failed_cleanup_prevents_reuse(self):
        allocation = self.coordinator.reserve(self.plan, owner="owner", device_id="device")
        self.assertEqual(self.coordinator.prepare(self.plan, allocation, payload={},
                         operation_id="operation_expiring").status, "complete")
        self.remote.control(fail_cleanup=True)
        cleanup = self.coordinator.cleanup(
            self.plan, allocation, operation_id="operation_cleanup_failed")
        self.assertEqual(cleanup["status"], "failed")
        self.assertEqual(self.coordinator.status(allocation)["state"], "quarantined")
        with self.assertRaises(FixtureError):
            self.coordinator.reserve(self.plan, owner="other", device_id="other")

    def test_expired_idempotency_status_is_unknown_and_quarantined(self):
        allocation = self.coordinator.reserve(self.plan, owner="owner", device_id="device")
        self.assertEqual(self.coordinator.prepare(
            self.plan, allocation, payload={"retentionMs": 60_000},
            operation_id="operation_retention").status, "complete")
        # First establish a retained result, then advance the coordinator past
        # its advertised expiry. A 2 ms lifetime can elapse during the initial
        # HTTP response and incorrectly turns setup into the expiry scenario.
        expiry = _read_state(self.remote.state)["operations"]["operation_retention"]["retentionExpiresAtMs"]
        with patch('reproloop.fixtures.time.time', return_value=(expiry+1000)/1000):
            duplicate=self.coordinator.prepare(
                self.plan,allocation,payload={"retentionMs":60_000},
                operation_id="operation_retention")
            self.assertEqual(duplicate.status,"unknown")
            self.assertEqual(
                _read_state(self.remote.state)["dispatches"]["operation_retention"],1)
            result = self.coordinator.reconcile(self.plan, allocation, timeout_seconds=.2)
        self.assertTrue(any(item["status"] == "unknown"
                            for item in result["operations"]))
        self.assertEqual(self.coordinator.status(allocation)["state"], "quarantined")

    def test_remote_and_local_restart_do_not_imply_remote_effect_ended(self):
        allocation = self.coordinator.reserve(self.plan, owner="owner", device_id="device")
        self.assertEqual(self.coordinator.prepare(self.plan, allocation, payload={},
                         operation_id="operation_before_restart").status, "complete")
        self.remote.restart();self.coordinator.close()
        self.coordinator = FixtureCoordinator(Path(self.temp.name) / "fixture-store")
        adapter = LoopbackFixtureAdapter(
            "fixture_service", f"http://127.0.0.1:{self.remote.port}",
            capabilities=AdapterCapabilities(True, True, 60_000))
        self.plan = self.coordinator.register_plan(
            self.registration, application_id="ios_app", fixture_id="seed_account",
            adapter=adapter, check_recipe_ids=("check_account",),
            cleanup_recipe_id="cleanup_account")
        with self.assertRaises(FixtureError):
            self.coordinator.reserve(self.plan, owner="other", device_id="other")
        recovered=self.coordinator.recover_allocation(
            self.plan,allocation_id=allocation.allocation_id,owner="owner")
        self.assertEqual(self.coordinator.status(recovered)["state"],"quarantined")
        self.coordinator.reconcile(self.plan,recovered,timeout_seconds=.2)
        self.assertEqual(self.coordinator.status(recovered)["state"],"quarantined")
        cleanup=self.coordinator.cleanup(
            self.plan,recovered,operation_id="operation_recovery_cleanup")
        self.assertEqual(cleanup["status"],"complete")
        self.assertEqual(self.coordinator.status(recovered)["state"],"available")

    def recovery_cleanup(self,allocation,**changes):
        values={'allocation_id':allocation.allocation_id,'generation':allocation.generation,
            'owner':'owner','device_id':'device','prepare_operation_id':'operation_bound_prepare',
            'payload_digest':self.coordinator.payload_digest({}),
            'cleanup_operation_id':'operation_bound_recovery_cleanup','timeout_seconds':.5}
        values.update(changes)
        return self.coordinator.recover_cleanup(self.plan,**values)

    def test_bound_recovery_reconciles_and_cleans_the_original_slot(self):
        allocation=self.coordinator.reserve(self.plan,owner='owner',device_id='device')
        self.coordinator.prepare(self.plan,allocation,payload={},operation_id='operation_bound_prepare')
        self.coordinator.retain_for_cleanup(allocation)
        result=self.recovery_cleanup(allocation)
        self.assertEqual(result['status'],'complete')
        self.assertFalse(result['historyOnly'])
        self.assertEqual(self.coordinator.status(allocation)['state'],'available')

    def test_historical_cleanup_does_not_touch_a_newer_allocation(self):
        old=self.coordinator.reserve(self.plan,owner='owner',device_id='device')
        self.coordinator.prepare(self.plan,old,payload={},operation_id='operation_bound_prepare')
        self.coordinator.cleanup(self.plan,old,operation_id='operation_old_cleanup')
        current=self.coordinator.reserve(self.plan,owner='other-owner',device_id='other-device')
        self.coordinator.prepare(self.plan,current,payload={},operation_id='operation_current_prepare')
        before=_read_state(self.remote.state)['dispatches']
        result=self.recovery_cleanup(old)
        self.assertEqual(result['status'],'complete')
        self.assertTrue(result['historyOnly'])
        self.assertEqual(self.coordinator.status(current)['state'],'ready')
        self.assertEqual(_read_state(self.remote.state)['dispatches'],before)
        for changed in ({'owner':'other-owner'},{'device_id':'other-device'}):
            with self.assertRaises(FixtureError):self.recovery_cleanup(old,**changed)

    def test_bound_recovery_rejects_wrong_device_or_prepare_identity(self):
        allocation=self.coordinator.reserve(self.plan,owner='owner',device_id='device')
        self.coordinator.prepare(self.plan,allocation,payload={},operation_id='operation_bound_prepare')
        self.coordinator.retain_for_cleanup(allocation)
        for changes in ({'device_id':'other-device'},{'prepare_operation_id':'unrelated_prepare'}):
            with self.assertRaises(FixtureError):self.recovery_cleanup(allocation,**changes)
        self.assertEqual(self.coordinator.status(allocation)['state'],'quarantined')

    def test_bound_recovery_refuses_a_live_ready_allocation(self):
        allocation=self.coordinator.reserve(self.plan,owner='owner',device_id='device')
        self.coordinator.prepare(self.plan,allocation,payload={},operation_id='operation_bound_prepare')
        with self.assertRaises(FixtureError):self.recovery_cleanup(allocation)
        self.assertEqual(self.coordinator.status(allocation)['state'],'ready')

    def test_legacy_fixture_schema_migrates_without_losing_pending_operations(self):
        allocation=self.coordinator.reserve(self.plan,owner='owner',device_id='device')
        self.coordinator.prepare(self.plan,allocation,payload={},operation_id='operation_bound_prepare')
        root=self.coordinator.root;self.coordinator.close()
        with closing(sqlite3.connect(root/'allocations.sqlite3')) as database:
            database.execute('DROP TABLE allocation_history')
            database.execute("UPDATE metadata SET value=1 WHERE key='version'")
            database.commit()
        self.coordinator=FixtureCoordinator(root)
        self.assertEqual(self.coordinator._db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()[0],2)
        row=self.coordinator._db.execute('SELECT * FROM allocations WHERE allocation_id=?',(allocation.allocation_id,)).fetchone()
        self.assertEqual(row['generation'],allocation.generation)
        self.assertEqual(row['state'],'quarantined')
        self.assertEqual(self.coordinator._db.execute('SELECT COUNT(*) FROM operations WHERE allocation_id=?',
            (allocation.allocation_id,)).fetchone()[0],2)

    def test_named_reservation_is_findable_before_prepare_and_cannot_be_reused(self):
        allocation=self.coordinator.reserve(self.plan,owner='owner',device_id='device',allocation_id='allocation_owned_intent')
        found=self.coordinator.lookup_recovery_allocation(self.plan,allocation_id=allocation.allocation_id,
            owner='owner',device_id='device')
        self.assertEqual(found['generation'],allocation.generation)
        self.assertEqual(found['state'],'reserved')
        with self.assertRaises(FixtureError):
            self.coordinator.reserve(self.plan,owner='owner',device_id='device',allocation_id=allocation.allocation_id)

    def test_bound_unstarted_reservation_is_released_without_remote_cleanup(self):
        allocation=self.coordinator.reserve(self.plan,owner='owner',device_id='device',allocation_id='allocation_unstarted')
        before=_read_state(self.remote.state)['dispatches']
        result=self.recovery_cleanup(allocation,allow_unstarted=True)
        self.assertEqual(result['status'],'complete')
        self.assertTrue(result['unstarted'])
        self.assertEqual(self.coordinator.status(allocation)['state'],'available')
        self.assertEqual(_read_state(self.remote.state)['dispatches'],before)
        with self.assertRaises(FixtureError):
            self.coordinator.prepare(self.plan,allocation,payload={},operation_id='late_unstarted_prepare')

    def test_unstarted_flag_cannot_bypass_an_unrelated_prepare_operation(self):
        allocation=self.coordinator.reserve(self.plan,owner='owner',device_id='device')
        self.coordinator.prepare(self.plan,allocation,payload={},operation_id='unrelated_prepare')
        self.coordinator.retain_for_cleanup(allocation)
        with self.assertRaises(FixtureError):self.recovery_cleanup(allocation,allow_unstarted=True)

    def test_named_reservation_lookup_refuses_foreign_owner_and_device(self):
        allocation=self.coordinator.reserve(self.plan,owner='owner',device_id='device')
        for owner,device in (('other','device'),('owner','other')):
            with self.assertRaises(FixtureError):
                self.coordinator.lookup_recovery_allocation(self.plan,allocation_id=allocation.allocation_id,
                    owner=owner,device_id=device)

    def test_absent_reservation_seal_prevents_a_late_reserve(self):
        result=self.coordinator.seal_unstarted_reservation(self.plan,allocation_id='allocation_never_started',
            owner='owner',device_id='device')
        self.assertEqual(result['status'],'complete')
        with self.assertRaises(FixtureError):
            self.coordinator.reserve(self.plan,owner='owner',device_id='device',allocation_id='allocation_never_started')
        again=self.coordinator.seal_unstarted_reservation(self.plan,allocation_id='allocation_never_started',
            owner='owner',device_id='device')
        self.assertEqual(result,again)

    def test_absent_reservation_seal_cannot_replace_an_existing_reservation(self):
        allocation=self.coordinator.reserve(self.plan,owner='owner',device_id='device')
        with self.assertRaises(FixtureError):
            self.coordinator.seal_unstarted_reservation(self.plan,allocation_id=allocation.allocation_id,
                owner='owner',device_id='device')
        self.assertEqual(self.coordinator.status(allocation)['state'],'reserved')

    def test_reservation_seals_respect_the_history_limit(self):
        with patch('reproloop.fixtures.MAX_OPERATIONS',1):
            self.coordinator.seal_unstarted_reservation(self.plan,allocation_id='allocation_first_seal',
                owner='owner',device_id='device')
            with self.assertRaises(FixtureError):
                self.coordinator.seal_unstarted_reservation(self.plan,allocation_id='allocation_second_seal',
                    owner='owner',device_id='device')
        self.assertIsNone(self.coordinator.lookup_recovery_allocation(self.plan,allocation_id='allocation_second_seal',
            owner='owner',device_id='device'))

    def test_sealed_reservation_cannot_be_adopted_by_another_owner(self):
        self.coordinator.seal_unstarted_reservation(self.plan,allocation_id='allocation_bound_seal',
            owner='owner',device_id='device')
        with self.assertRaises(FixtureError):
            self.coordinator.seal_unstarted_reservation(self.plan,allocation_id='allocation_bound_seal',
                owner='other-owner',device_id='device')

    def test_unstarted_reservation_and_absence_seal_survive_store_reopen(self):
        allocation=self.coordinator.reserve(self.plan,owner='owner',device_id='device',allocation_id='allocation_reopen')
        self.coordinator.seal_unstarted_reservation(self.plan,allocation_id='allocation_reopen_absent',
            owner='owner',device_id='device')
        root=self.coordinator.root;self.coordinator.close()
        self.coordinator=FixtureCoordinator(root)
        self.plan=self.coordinator.register_plan(self.registration,application_id='ios_app',fixture_id='seed_account',
            adapter=LoopbackFixtureAdapter('fixture_service',f'http://127.0.0.1:{self.remote.port}',
                capabilities=AdapterCapabilities(True,True,60_000)),check_recipe_ids=('check_account',),cleanup_recipe_id='cleanup_account')
        found=self.coordinator.lookup_recovery_allocation(self.plan,allocation_id=allocation.allocation_id,
            owner='owner',device_id='device')
        self.assertEqual(found['state'],'quarantined')
        self.assertEqual(self.recovery_cleanup(allocation,allow_unstarted=True)['status'],'complete')
        with self.assertRaises(FixtureError):
            self.coordinator.reserve(self.plan,owner='owner',device_id='device',allocation_id='allocation_reopen_absent')
        self.assertEqual(_read_state(self.remote.state)['dispatches'],{})


if __name__ == "__main__":unittest.main()
