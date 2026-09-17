import json
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from reproloop.live.access import AccessError
from reproloop.live.enrollment import EnrollmentClient
from reproloop.live.inventory import INVENTORY_VERSION, canonical_device_digest
from reproloop.live.authority import HostAuthority
from reproloop.live.clock_sync import ClockReading
from reproloop.live.configuration import issue_bounded_project_grant
from reproloop.live.model import Lab, LiveError
from reproloop.live.worker import WorkerClient, WorkerServer, remote_devices
from tests.test_fixture_allocations import collection_policy, project_document
from tests.test_project_access import SharedHttpFixture


def document(generation, incarnation, *, alias="phone-a", state="available",
             physical="owned-synthetic-physical-a"):
    return {
        "schemaVersion": INVENTORY_VERSION, "generation": generation, "incarnation": incarnation,
        "devices": [{
            "alias": alias, "deviceKind": "android",
            "physicalDigest": canonical_device_digest("android", physical),
            "profileDigest": "a" * 64, "state": state, "ownership": None,
        }],
    }


class EnrolledInventoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SharedHttpFixture()
        self.addCleanup(self.fixture.close)

    def enroll(self, host_id, incarnation):
        issued = self.fixture.store.create_host_enrollment(
            "root-admin", host_id=host_id, project_ids=["checkout"],
            trust_groups=[], lifetime_seconds=300, credential_lifetime_seconds=300)
        client = EnrollmentClient(self.fixture.server.origin)
        enrolled = client.enroll(
            issued["token"], host_id=host_id, incarnation=incarnation)
        return client, enrolled

    def test_authenticated_refresh_hides_raw_identity_and_retains_disconnect(self):
        client, enrolled = self.enroll("worker-a", "boot-a")
        value = client.refresh_inventory(
            enrolled["credential"], document(1, "boot-a", state="busy"))
        self.assertEqual(value["devices"][0]["state"], "busy")
        disconnected = client.refresh_inventory(enrolled["credential"], {
            "schemaVersion": INVENTORY_VERSION, "generation": 1, "incarnation": "boot-a",
            "devices": [],
        })
        self.assertEqual(disconnected["devices"][0]["state"], "uncertain")
        retained = self.fixture.server.inventory.path.read_bytes()
        self.assertNotIn(b"owned-synthetic-physical-a", retained)
        self.assertNotIn("physicalDigest", repr(value))

    def test_duplicate_physical_alias_across_enrolled_hosts_is_denied(self):
        first_client, first = self.enroll("worker-a", "boot-a")
        second_client, second = self.enroll("worker-b", "boot-b")
        first_client.refresh_inventory(first["credential"], document(1, "boot-a"))
        with self.assertRaises(AccessError) as duplicate:
            second_client.refresh_inventory(
                second["credential"], document(1, "boot-b", alias="phone-b"))
        self.assertEqual(duplicate.exception.code, "duplicate_device")

    def test_stale_generation_and_changed_alias_cannot_adopt_inventory(self):
        client, enrolled = self.enroll("worker-a", "boot-a")
        client.refresh_inventory(enrolled["credential"], document(1, "boot-a"))
        with self.assertRaises(AccessError) as changed:
            client.refresh_inventory(
                enrolled["credential"], document(1, "boot-a", alias="renamed"))
        self.assertEqual(changed.exception.code, "duplicate_device")
        stale = document(2, "boot-new")
        with self.assertRaises(AccessError) as generation:
            client.refresh_inventory(enrolled["credential"], stale)
        self.assertEqual(generation.exception.code, "stale_host")

    def test_replacement_host_cannot_turn_an_old_busy_device_available(self):
        client, enrolled = self.enroll("worker-a", "boot-a")
        client.refresh_inventory(
            enrolled["credential"], document(1, "boot-a", state="busy"))
        self.fixture.store.revoke_host("root-admin", "worker-a")
        replacement_client, replacement = self.enroll("worker-a", "boot-b")
        with self.assertRaises(AccessError) as stale:
            replacement_client.refresh_inventory(
                replacement["credential"],
                document(replacement["generation"], "boot-b",
                         state="available"))
        self.assertEqual(stale.exception.code, "device_reconciliation_required")
        retained = self.fixture.server.inventory.list_host("worker-a")
        self.assertEqual(retained[0]["state"], "uncertain")
        replacement_client.refresh_inventory(
            replacement["credential"],
            document(replacement["generation"], "boot-b",
                     state="quarantined"))
        with self.assertRaises(AccessError) as unproven:
            replacement_client.refresh_inventory(
                replacement["credential"],
                document(replacement["generation"], "boot-b", state="available"))
        self.assertEqual(unproven.exception.code, "device_reconciliation_required")
        self.assertNotEqual(self.fixture.server.inventory.list_host("worker-a")[0]["state"],
                            "available")


class _Clock:
    def __init__(self, name):
        self.name = name;self.now = 1_000_000_000

    def read(self):
        return ClockReading(self.name, "d" * 64, self.now, 0)


class _ReservedProvider:
    def bind_authority(self, authority, provider_incarnation):
        self.authority = authority

    def start_authorized(self, session, lab, permit):
        try:
            self.authority.check_dispatch_permit(permit)
            self.session = session;self.lab = lab
            lab.publish_frame(session["id"], b"<svg/>", "image/svg+xml", 320, 640)
            return {"ok": True}
        except Exception as error:
            self.error = repr(error)
            raise

    def close_authorized(self, permit):
        self.authority.check_dispatch_permit(permit)
        return {"ok": True}


class RemotePhysicalReservationTests(unittest.TestCase):
    def test_canonical_worker_reservation_is_transferred_into_startup(self):
        with tempfile.TemporaryDirectory(prefix="g6-remote-reservation-") as directory:
            root = Path(directory)
            worker_authority = HostAuthority(
                root / "worker-authority.sqlite3", clock=_Clock("worker-clock"),
                lease_directory=root / "worker-leases")
            parent_authority = HostAuthority(
                root / "parent-authority.sqlite3", clock=_Clock("parent-clock"),
                lease_directory=root / "parent-leases")
            project = project_document();policy = collection_policy()
            provider = _ReservedProvider()
            worker = Lab([{
                "id": "physical-worker-device", "name": "Owned synthetic iPhone adapter",
                "platform": "ios", "kind": "ios-physical",
                "_authority": {"deviceKind": "ios-physical",
                               "physicalId": "owned-synthetic-device"},
                "capabilities": {"actions": ["tap"], "inputMode": "gesture-batch",
                    "authorityMode": "shared-v2",
                    "applicationIdentity": {"bundle": "com.example.app",
                                             "artifactDigest": "0" * 64}},
                "factory": lambda: provider,
            }], root / "worker", authority=worker_authority,
                parent_grant=None, delegated_authority_only=True)
            worker_registration = worker.register_recording_project(project, policy)
            server = WorkerServer(
                worker, "g6-reservation-token-0123456789abcdef",
                registered_projects=[worker_registration])
            thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
            parent = None
            try:
                client = WorkerClient(server.origin, "g6-reservation-token-0123456789abcdef")
                grant = issue_bounded_project_grant(
                    parent_authority, "checkout", lifetime_seconds=60)
                parent = Lab(remote_devices(client, "worker"), root / "parent",
                             authority=parent_authority, parent_grant=grant)
                registration = parent.register_recording_project(project, policy)
                reservation = parent.reserve_release_device(
                    "worker--physical-worker-device", "owner", "remote_reservation",
                    registration, application_id="ios_app", build_id="original")
                self.assertEqual(worker.list_devices()[0]["state"], "reserved")
                session = parent.create_release_session(
                    "worker--physical-worker-device", "owner", "browser", registration,
                    application_id="ios_app", build_id="original",
                    preparation_receipts=[], device_reservation=reservation)
                deadline = time.monotonic() + 5
                while session["state"] == "connecting" and time.monotonic() < deadline:
                    time.sleep(.03)
                    session = parent.get_session(session["id"], "owner")
                self.assertEqual(session["state"], "active",
                                 repr((session, [worker.peek_session(key) for key in worker.sessions],
                                       getattr(provider, "error", None))))
                self.assertEqual(worker.list_devices()[0]["state"], "busy")
                closed = parent.close_session(
                    session["id"], "owner", session["controllerId"], session["epoch"])
                self.assertEqual(closed["state"], "closed")
                self.assertEqual(worker.list_devices()[0]["state"], "available")
            finally:
                if parent is not None:parent.close_all()
                server.close_operations();server.shutdown();server.server_close();thread.join(5)
                worker_authority.close();parent_authority.close()


class TwoWorkerProcessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SharedHttpFixture()
        self.temporary = tempfile.TemporaryDirectory(prefix="g6-worker-processes-")
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=3)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        self.fixture.close()
        self.temporary.cleanup()

    def _start_worker(self, number, *, expected_error=None):
        host_id = f"worker-{number}"
        incarnation = f"boot-{number}"
        enrollment = self.fixture.store.create_host_enrollment(
            "root-admin", host_id=host_id, project_ids=["checkout"],
            trust_groups=[], lifetime_seconds=300,
            credential_lifetime_seconds=300)
        token = f"g6-process-transport-{number}-0123456789abcdef"
        root = Path(self.temporary.name) / host_id
        configuration = {
            "coordinator": self.fixture.server.origin,
            "enrollmentToken": enrollment["token"],
            "transportToken": token,
            "hostId": host_id, "incarnation": incarnation,
            "output": str(root / "output"),
            "authorityRoot": str(root / "authority"),
            "deviceAlias": f"phone-{number}",
            "physicalId": getattr(self, "physical_override", "owned-synthetic-" + uuid.uuid4().hex),
            "project": project_document(),
            "collectionPolicy": collection_policy(),
        }
        process = subprocess.Popen(
            [sys.executable, getattr(self, "worker_fixture", "tests/fixtures/g6_worker_process.py")],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        self.processes.append(process)
        process.stdin.write(json.dumps(configuration, separators=(",", ":")))
        process.stdin.close()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            events = selector.select(timeout=15)
            self.assertTrue(events, "worker fixture did not publish its origin")
            line = process.stdout.readline()
        finally:
            selector.close()
        if process.poll() is not None and not line:
            error = process.stderr.read(4096)
            self.fail(f"worker fixture exited early: {error}")
        public = json.loads(line)
        self.assertNotIn("enrollmentToken", line)
        self.assertNotIn("transportToken", line)
        self.assertNotIn(configuration["physicalId"], line)
        if expected_error is not None:
            self.assertEqual(public.get('error',{}).get('code'), expected_error)
            self.assertEqual(process.wait(timeout=8), 2)
            return None, public
        self.assertEqual(public["sameMacProtocol"], True)
        self.assertEqual(public["syntheticAdapter"], True)
        self.assertEqual(public["physicalDeviceAcceptance"], False)
        self.assertEqual(public["twoMacAcceptance"], False)
        if getattr(self, "actual_worker_cli", False):
            self.assertIs(public["actualWorkerCli"], True)
        return WorkerClient(public["worker"], token), public

    def test_two_enrolled_processes_inventory_ownership_profiles_and_transfers(self):
        clients = [self._start_worker(number) for number in ("one", "two")]
        remote = []
        for (client, public), worker_id in zip(clients, ("worker_one", "worker_two")):
            devices = remote_devices(client, worker_id)
            self.assertEqual(len(devices), 1)
            profile = devices[0]["capabilities"]["applicationProfile"]
            self.assertEqual(profile["schemaVersion"], 2)
            self.assertEqual(profile["applicationId"], "ios_app")
            inventory = self.fixture.server.inventory.list_host(public["hostId"])
            self.assertEqual(inventory[0]["state"], "available")
            body = ("artifact-from-" + worker_id).encode()
            published = client.upload_artifact_bytes(
                body, project_id="checkout", kind="manifest",
                metadata={"run": "same-mac-protocol"},
                retention_class="original",
                retain_until_ms=int(time.time() * 1000) + 60_000, chunk_bytes=7)
            self.assertEqual(published["state"], "published")
            self.assertEqual(client.download_artifact(
                published["objectId"]), body)
            remote.extend(devices)

        with tempfile.TemporaryDirectory(prefix="g6-parent-process-") as directory:
            root = Path(directory)
            parent_authority = HostAuthority(
                root / "authority.sqlite3", clock=(None if getattr(self, "actual_worker_cli", False)
                                                  else _Clock("parent-process-clock")),
                lease_directory=root / "leases")
            parent = None
            try:
                grant = issue_bounded_project_grant(
                    parent_authority, "checkout", lifetime_seconds=60)
                parent = Lab(remote, root / "lab", authority=parent_authority,
                             parent_grant=grant)
                registration = parent.register_recording_project(
                    project_document(), collection_policy())
                selected = remote[0]["id"]
                reservation = parent.reserve_release_device(
                    selected, "owner", "process_reservation", registration,
                    application_id="ios_app", build_id="original")
                with self.assertRaises(LiveError) as busy:
                    parent.reserve_release_device(
                        selected, "other", "contested_reservation", registration,
                        application_id="ios_app", build_id="original")
                self.assertEqual(busy.exception.code, "device_busy")
                session = parent.create_release_session(
                    selected, "owner", "browser", registration,
                    application_id="ios_app", build_id="original",
                    preparation_receipts=[], device_reservation=reservation)
                deadline = time.monotonic() + 8
                while session["state"] == "connecting" and time.monotonic() < deadline:
                    time.sleep(0.05)
                    session = parent.get_session(session["id"], "owner")
                self.assertEqual(session["state"], "active")
                closed = parent.close_session(
                    session["id"], "owner", session["controllerId"],
                    session["epoch"])
                self.assertEqual(closed["state"], "closed")
            finally:
                if parent is not None:
                    parent.close_all()
                parent_authority.close()


if __name__ == "__main__":
    unittest.main()
