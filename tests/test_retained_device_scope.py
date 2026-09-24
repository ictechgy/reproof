"""The repair owner keeps one native reservation across fresh G4 sessions."""
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from reproof.core import ContractError
from reproof import contracts
from reproof.live.authority import HostAuthority
from reproof.live.model import Lab, LiveError
from tests.test_fixture_allocations import collection_policy, project_document
from tests.g4_support import G4Environment
from tests.test_live_authority_integration import Clock, FencedProvider, parent_grant


class RetainedDeviceScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.authority = HostAuthority(self.root / "authority.sqlite3", clock=self.clock,
                                       lease_directory=self.root / "leases")
        self.grant = parent_grant(self.authority, self.clock)
        project = project_document()
        project["id"] = "integration-project"
        project["applications"][0]["platform"] = "android"
        self.providers = []

        def factory():
            provider = FencedProvider()
            self.providers.append(provider)
            return provider

        descriptor = {
            "id": "device", "name": "retained native device", "platform": "android",
            "kind": "android-live", "factory": factory,
            "capabilities": {"actions": ["tap"], "inputMode": "gesture-batch",
                              "applicationIdentity": {"bundle": "com.example.app",
                                                       "artifactDigest": "0" * 64}},
            "_authority": {"deviceKind": "android", "physicalId": "retained-device"},
        }
        self.lab = Lab([descriptor], self.root / "lab", authority=self.authority,
                       parent_grant=self.grant)
        self.registration = self.lab.register_recording_project(
            project, collection_policy(), capacity_bytes=128 * 1024 * 1024,
            journal_headroom_bytes=256 * 1024)

    def tearDown(self):
        self.lab.close_all()
        self.authority.close()
        self.tmp.cleanup()

    def external_lock_state(self):
        code = """import sys
from reproof.storage import Lease
try:
    with Lease('retained-device', sys.argv[1]): print('available')
except Exception:
    print('busy')
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.root / "leases")],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, check=True)
        return result.stdout.strip()

    def reserve_scope(self):
        reservation = self.lab.reserve_release_device(
            "device", "repair", "reservation", self.registration,
            application_id="ios_app", build_id="original")
        return self.lab.retain_device_reservation(reservation)

    def session(self, scope, number):
        return self.lab.create_release_session(
            "device", "repair", "attempt-" + str(number), self.registration,
            application_id="ios_app", build_id="original", preparation_receipts=[],
            device_scope=scope)

    def test_three_fresh_sessions_keep_one_scope_and_block_other_claims(self):
        scope = self.reserve_scope()
        self.assertEqual(self.external_lock_state(), "busy")
        for number in range(1, 4):
            current = self.session(scope, number)
            self.assertEqual(current["state"], "active")
            self.assertEqual(self.lab.list_devices()[0]["state"], "busy")
            self.lab.close_session(current["id"], "repair")
            self.assertEqual(self.lab.list_devices()[0]["state"], "reserved")
            with self.assertRaises(LiveError):
                self.lab.reserve_release_device(
                    "device", "other", "other-" + str(number), self.registration,
                    application_id="ios_app", build_id="original")
        self.assertEqual(len(self.providers), 3)
        self.assertEqual(self.external_lock_state(), "busy")
        self.lab.release_retained_device_scope(scope)
        self.assertEqual(self.external_lock_state(), "available")

    def test_candidate_identity_and_factory_are_scoped_without_mutating_inventory(self):
        scope = self.reserve_scope()
        original = self.lab.devices["device"]["capabilities"]["applicationIdentity"]
        replacement = {"bundle": "com.example.app", "artifactDigest": "0" * 64}
        provider = FencedProvider()
        session = self.lab.create_release_session(
            "device", "repair", "candidate", self.registration,
            application_id="ios_app", build_id="original", preparation_receipts=[],
            device_scope=scope, _candidate_identity=replacement,
            _provider_factory=lambda: provider)
        self.assertEqual(session["state"], "active")
        self.assertEqual(self.lab.devices["device"]["capabilities"]["applicationIdentity"], original)
        self.lab.close_session(session["id"], "repair")
        self.lab.release_retained_device_scope(scope)

    def test_wrong_candidate_identity_is_rejected_before_provider_creation(self):
        scope = self.reserve_scope()
        with self.assertRaises(LiveError) as caught:
            self.lab.create_release_session(
                "device", "repair", "candidate", self.registration,
                application_id="ios_app", build_id="original", preparation_receipts=[],
                device_scope=scope,
                _candidate_identity={"bundle": "com.example.app", "artifactDigest": "1" * 64},
                _provider_factory=lambda: self.fail("provider must not be created"))
        self.assertEqual(caught.exception.code, "recording_identity")
        self.assertEqual(self.lab.list_devices()[0]["state"], "reserved")
        self.lab.release_retained_device_scope(scope)

    def test_close_all_does_not_release_an_unfinished_retained_scope(self):
        scope = self.reserve_scope()
        self.lab.close_all()
        self.assertEqual(self.external_lock_state(), "busy")
        self.assertEqual(self.lab.list_devices()[0]["state"], "quarantined")

    def test_issue_service_reuses_scope_for_fresh_recordings(self):
        environment = G4Environment(recording_capacity_bytes=128 * 1024 * 1024)
        try:
            reservation = environment.lab.reserve_release_device(
                "device", "repair", "reservation", environment.registration,
                application_id="ios_app", build_id="original")
            scope = environment.lab.retain_device_reservation(reservation)
            for number in range(1, 4):
                handle = environment.service.start_prepared_recording(
                    device_id="device", owner="repair", controller_id="attempt-" + str(number),
                    registration=environment.registration, application_id="ios_app",
                    build_id="original", preparations=environment.preparations(),
                    device_scope=scope)
                stopped = environment.service.stop(handle)
                self.assertIn(stopped["issue"]["state"], {"complete", "failed"})
                self.assertEqual(environment.lab.list_devices()[0]["state"], "reserved")
            environment.lab.release_retained_device_scope(scope)
            self.assertEqual(environment.lab.list_devices()[0]["state"], "available")
        finally:
            environment.close()

    def test_retained_startup_failure_quarantines_without_releasing_scope(self):
        scope = self.reserve_scope()
        def fail_factory():
            raise RuntimeError("provider construction failed")
        with self.assertRaises(RuntimeError):
            self.lab.create_release_session(
                "device", "repair", "attempt", self.registration,
                application_id="ios_app", build_id="original", preparation_receipts=[],
                device_scope=scope, _provider_factory=fail_factory)
        self.assertEqual(self.lab.list_devices()[0]["state"], "quarantined")
        self.assertEqual(self.external_lock_state(), "busy")
        with self.assertRaises(LiveError) as caught:
            self.lab.release_retained_device_scope(scope)
        self.assertEqual(caught.exception.code, "cleanup_uncertain")
        self.lab.close_all()
        self.assertEqual(self.external_lock_state(), "busy")

    def test_retained_cleanup_uncertainty_quarantines_without_release(self):
        scope = self.reserve_scope()
        session = self.session(scope, 1)
        provider = self.providers[-1]
        provider.close_authorized = lambda permit: (_ for _ in ()).throw(
            RuntimeError("cleanup was not confirmed"))
        closed = self.lab.close_session(session["id"], "repair")
        self.assertEqual(closed["state"], "failed")
        self.assertEqual(self.lab.list_devices()[0]["state"], "quarantined")
        self.assertEqual(self.external_lock_state(), "busy")
        with self.assertRaises(LiveError):
            self.lab.release_retained_device_scope(scope)

    def test_scope_rejects_a_second_session_while_the_first_is_active(self):
        scope = self.reserve_scope()
        first = self.session(scope, 1)
        with self.assertRaises(LiveError):
            self.session(scope, 2)
        self.assertEqual(self.lab.list_devices()[0]["sessionId"], first["id"])
        self.lab.close_session(first["id"], "repair")
        self.lab.release_retained_device_scope(scope)

    def test_concurrent_scope_starts_have_one_atomic_winner(self):
        scope = self.reserve_scope()
        barrier = threading.Barrier(2)
        results, errors = [], []
        def start(number):
            try:
                barrier.wait()
                results.append(self.session(scope, number))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=start, args=(number,)) for number in (1, 2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.lab.list_devices()[0]["sessionId"], results[0]["id"])
        self.lab.close_session(results[0]["id"], "repair")
        self.lab.release_retained_device_scope(scope)

    def test_quarantined_scope_cannot_start_a_new_session(self):
        scope = self.reserve_scope()
        first = self.session(scope, 1)
        self.providers[-1].close_authorized = lambda permit: (_ for _ in ()).throw(
            RuntimeError("cleanup was not confirmed"))
        self.lab.close_session(first["id"], "repair")
        with self.assertRaises(LiveError):
            self.session(scope, 2)

    def test_scope_identity_is_checked_before_fixture_reservation(self):
        environment = G4Environment(recording_capacity_bytes=128 * 1024 * 1024)
        try:
            reservation = environment.lab.reserve_release_device(
                "device", "repair", "reservation", environment.registration,
                application_id="ios_app", build_id="original")
            scope = environment.lab.retain_device_reservation(reservation)
            calls = []
            reserve = environment.fixtures.reserve
            environment.fixtures.reserve = lambda *args, **kwargs: (
                calls.append(True) or reserve(*args, **kwargs))
            with self.assertRaises(Exception):
                environment.service.start_prepared_recording(
                    device_id="device", owner="repair", controller_id="attempt",
                    registration=environment.registration, application_id="ios_app",
                    build_id="original", preparations=environment.preparations(),
                    device_scope=scope,
                    _candidate_identity={"bundle": "com.example.app", "artifactDigest": "1" * 64})
            self.assertEqual(calls, [])
        finally:
            environment.close()

    def test_scope_validation_rechecks_the_parent_handle(self):
        scope = self.reserve_scope()
        self.clock.now += 120_000_000_000
        with self.assertRaises(LiveError):
            self.lab.validate_retained_device_scope(
                scope, owner="repair", device_id="device")

    def test_close_all_with_owned_authority_does_not_release_retained_scope(self):
        scope = self.reserve_scope()
        current = self.session(scope, 1)
        self.lab.close_session(current["id"], "repair")
        self.lab._owns_authority = True
        self.lab.close_all()
        self.assertEqual(self.external_lock_state(), "busy")
        self.assertEqual(self.lab.list_devices()[0]["state"], "quarantined")

    def test_scope_effects_share_the_authority_sequence_with_sessions(self):
        scope = self.reserve_scope()
        permit = self.lab.prepare_retained_scope_effect(
            scope, owner="repair", kind="install", payload={"artifact": "candidate"},
            provider_incarnation="provider_scope")
        self.assertEqual(permit.sequence, 1)
        receipt = self.lab.confirm_retained_scope_effect(
            scope, permit, owner="repair", status="succeeded",
            result_digest=contracts.digest("installed"))
        self.assertEqual(receipt.status, "succeeded")
        current = self.session(scope, 1)
        self.assertEqual(current["state"], "active")
        self.assertEqual(self.lab.sessions[current["id"]]["authoritySequence"], 2)
        self.lab.close_session(current["id"], "repair")
        self.lab.release_retained_device_scope(scope)

    def test_scope_effect_is_rejected_while_a_session_owns_the_device(self):
        scope = self.reserve_scope()
        current = self.session(scope, 1)
        with self.assertRaises(LiveError):
            self.lab.prepare_retained_scope_effect(
                scope, owner="repair", kind="install", payload={},
                provider_incarnation="provider_scope")
        self.lab.close_session(current["id"], "repair")
        self.lab.release_retained_device_scope(scope)


if __name__ == "__main__":
    unittest.main()
