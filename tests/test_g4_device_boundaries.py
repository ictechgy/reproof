"""Preparation must own the same native exclusion as the later session."""
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof.live.authority import HostAuthority
from reproof.live.model import Lab, LiveError
from tests.test_live_authority_integration import Clock, FencedProvider, parent_grant
from tests.test_recording_recovery import collection_policy, project_document


class DeviceReservationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.authority = HostAuthority(self.root / "authority.sqlite3", clock=self.clock,
                                       lease_directory=self.root / "leases")
        self.grant = parent_grant(self.authority, self.clock)
        self.project = project_document()
        self.project["id"] = "integration-project"
        self.project["applications"][0]["platform"] = "android"
        self.labs = []
        self.providers = []

    def tearDown(self):
        for lab in self.labs:
            lab.close_all()
        self.authority.close()
        self.temp.cleanup()

    def lab(self, *, legacy=False):
        def factory():
            provider = FencedProvider()
            self.providers.append(provider)
            return provider

        descriptor = {"id": "device", "name": "Synthetic native descriptor", "platform": "android",
                      "kind": "android-live", "factory": factory,
                      "capabilities": {"actions": ["tap"], "inputMode": "gesture-batch",
                                       "applicationIdentity": {"bundle": "com.example.app",
                                                               "artifactDigest": "0" * 64}},
                      "_authority": {"deviceKind": "android", "physicalId": "g4-synthetic-device"}}
        if legacy:
            descriptor.pop("_authority")
            descriptor["capabilities"]["authorityMode"] = "legacy-offline-v1"
        lab = Lab([descriptor], self.root / f"lab-{len(self.labs)}", authority=self.authority,
                  parent_grant=self.grant)
        self.labs.append(lab)
        registration = lab.register_recording_project(self.project, collection_policy(),
                                                       capacity_bytes=32 * 1024 * 1024,
                                                       journal_headroom_bytes=256 * 1024)
        return lab, registration

    def reserve(self, lab, registration):
        return lab.reserve_release_device("device", "owner", "reservation", registration,
                                           application_id="ios_app", build_id="original")

    def external_lock_state(self):
        command = """import sys
from reproof.storage import Lease
from reproof.core import ContractError
try:
    with Lease('g4-synthetic-device', sys.argv[1]):
        print('available')
except ContractError:
    print('busy')
"""
        result = subprocess.run([sys.executable, "-c", command, str(self.root / "leases")],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_reservation_holds_the_native_lock_in_another_process(self):
        lab, registration = self.lab()
        reservation = self.reserve(lab, registration)
        self.assertEqual(self.external_lock_state(), "busy")
        self.assertEqual(self.providers, [])
        lab.release_device_reservation(reservation)
        self.assertEqual(self.external_lock_state(), "available")

    def test_another_lab_cannot_reserve_the_same_physical_device(self):
        first, first_registration = self.lab()
        self.reserve(first, first_registration)
        second, second_registration = self.lab()
        with self.assertRaises((ContractError, LiveError)):
            self.reserve(second, second_registration)
        self.assertEqual(self.providers, [])

    def test_reservation_transfers_to_startup_without_releasing_the_lock(self):
        lab, registration = self.lab()
        reservation = self.reserve(lab, registration)
        self.assertEqual(self.external_lock_state(), "busy")
        session = lab.create_release_session(
            "device", "owner", "browser", registration, application_id="ios_app",
            build_id="original", preparation_receipts=[], device_reservation=reservation)
        self.assertEqual(session["state"], "active")
        self.assertEqual(self.external_lock_state(), "busy")
        lab.close_session(session["id"], "owner")
        self.assertTrue(self.providers[0].closed)
        self.assertEqual(self.external_lock_state(), "available")

    def test_expired_grant_is_rejected_before_preparation_can_start(self):
        lab, registration = self.lab()
        self.clock.now += 120_000_000_000
        with self.assertRaises((ContractError, LiveError)):
            self.reserve(lab, registration)
        self.assertEqual(self.providers, [])

    def test_legacy_native_mode_cannot_claim_a_prepared_release_reservation(self):
        lab, registration = self.lab(legacy=True)
        with self.assertRaises(LiveError):
            self.reserve(lab, registration)
        self.assertEqual(self.providers, [])

    def test_expired_reservation_cannot_start_a_provider(self):
        lab, registration = self.lab()
        reservation = self.reserve(lab, registration)
        self.clock.now += 120_000_000_000
        session = lab.create_release_session(
            "device", "owner", "browser", registration, application_id="ios_app",
            build_id="original", preparation_receipts=[], device_reservation=reservation)
        self.assertEqual(session["state"], "failed")
        self.assertEqual(self.providers, [])

    def test_unconfirmed_startup_release_keeps_device_quarantined(self):
        lab, registration = self.lab()
        reservation = self.reserve(lab, registration)
        with patch.object(lab._recording_store, "begin_recording", side_effect=RuntimeError), \
                patch.object(self.authority.store, "release_device", side_effect=ContractError("unavailable")):
            session = lab.create_release_session(
                "device", "owner", "browser", registration, application_id="ios_app",
                build_id="original", preparation_receipts=[], device_reservation=reservation)
        self.assertEqual(session["state"], "failed")
        self.assertEqual(self.providers, [])
        self.assertEqual(lab.list_devices()[0]["state"], "quarantined")
        closed = lab.close_session(session['id'], 'owner')
        self.assertEqual(closed['state'], 'failed')
        self.assertEqual(lab.list_devices()[0]['state'], 'quarantined')

    def test_startup_failure_does_not_publish_availability_before_release_confirmation(self):
        lab, registration = self.lab()
        reservation = self.reserve(lab, registration)
        observed = []
        actual_release = self.authority.store.release_device
        def release(*args, **kwargs):
            observed.append(lab.list_devices()[0]['state'])
            return actual_release(*args, **kwargs)
        with patch.object(lab._recording_store, 'begin_recording', side_effect=RuntimeError), \
                patch.object(self.authority.store, 'release_device', side_effect=release):
            session = lab.create_release_session(
                'device', 'owner', 'browser', registration,
                application_id='ios_app', build_id='original',
                preparation_receipts=[], device_reservation=reservation)
        self.assertEqual(session['state'], 'failed')
        self.assertEqual(observed, ['busy'])
        self.assertEqual(lab.list_devices()[0]['state'], 'available')

    def test_unconfirmed_reservation_release_keeps_device_quarantined(self):
        lab, registration = self.lab()
        reservation = self.reserve(lab, registration)
        with patch.object(self.authority.store, "release_device", side_effect=ContractError("unavailable")):
            with self.assertRaises(LiveError) as caught:
                lab.release_device_reservation(reservation)
        self.assertEqual(caught.exception.code, "cleanup_uncertain")
        self.assertEqual(lab.list_devices()[0]["state"], "quarantined")

    def test_unconfirmed_active_session_release_keeps_device_quarantined(self):
        lab, registration = self.lab()
        reservation = self.reserve(lab, registration)
        session = lab.create_release_session(
            "device", "owner", "browser", registration, application_id="ios_app",
            build_id="original", preparation_receipts=[], device_reservation=reservation)
        with patch.object(self.authority.store, "release_device", side_effect=ContractError("unavailable")):
            result = lab.close_session(session["id"], "owner")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(lab.list_devices()[0]["state"], "quarantined")

    def test_expired_native_ownership_cannot_start_locator_collection(self):
        lab, registration = self.lab()
        reservation = self.reserve(lab, registration)
        session = lab.create_release_session(
            "device", "owner", "browser", registration, application_id="ios_app",
            build_id="original", preparation_receipts=[], device_reservation=reservation)
        lab.devices["device"]["capabilities"]["locatorKinds"] = ["accessibility-id"]
        calls = []
        def lookup(_target):
            calls.append("lookup")
            return {}
        self.providers[0].resolve_locator = lookup
        self.clock.now += 120_000_000_000
        with self.assertRaises(LiveError):
            lab.resolve_locator(session["id"], "owner", {"kind": "accessibility-id", "value": "checkout"})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
