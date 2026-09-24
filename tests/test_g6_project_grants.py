"""Per-project session grants retain one authority across reservation and startup."""
import copy
from pathlib import Path
import tempfile
import unittest

from reproof.live.authority import HostAuthority
from reproof.live.configuration import issue_bounded_project_grant
from reproof.live.model import Lab, LiveError
from reproof.ios_profile import validate_ios_profile
from tests.test_fixture_allocations import collection_policy, project_document
from tests.test_live_authority import FakeClock
from tests.test_worker_recovery import _ReservedProvider
from tests.test_worker_profiles import physical_ios_document


class ProjectGrantTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.authority = HostAuthority(root / "authority.sqlite3", clock=FakeClock(),
                                       lease_directory=root / "leases")
        self.addCleanup(self.authority.close)
        self.issued = []

        def issue(registration):
            grant = issue_bounded_project_grant(
                self.authority, registration.project["id"], lifetime_seconds=60)
            self.issued.append(grant)
            return grant

        devices = [{
            "id": name, "name": name, "kind": "ios-physical", "platform": "ios",
            "_authority": {"deviceKind": "ios-physical", "physicalId": "owned-" + name},
            "capabilities": {"authorityMode": "shared-v2", "applicationIdentity": {
                "bundle": "com.example." + name, "artifactDigest": "0" * 64}},
            "factory": _ReservedProvider,
        } for name in ("checkout", "catalog")]
        self.lab = Lab(devices, root / "lab", authority=self.authority,
                       project_grant_provider=issue)
        self.addCleanup(self.lab.close_all)
        self.registrations = {}
        for name in ("checkout", "catalog"):
            project = copy.deepcopy(project_document())
            project["id"] = name
            project["applications"][0]["bundle"] = "com.example." + name
            self.registrations[name] = self.lab.register_recording_project(
                project, collection_policy())

    def test_each_project_receives_its_own_bounded_grant(self):
        sessions = []
        for name in ("checkout", "catalog"):
            session = self.lab.create_release_session(
                name, "owner", "controller", self.registrations[name],
                application_id="ios_app", build_id="original", preparation_receipts=[])
            sessions.append(session)
        self.assertEqual([grant.project_id for grant in self.issued], ["checkout", "catalog"])
        self.assertNotEqual(self.issued[0].grant_fingerprint, self.issued[1].grant_fingerprint)
        self.assertTrue(all(session["state"] == "active" for session in sessions))

    def test_reservation_transfers_the_same_grant_into_startup(self):
        registration = self.registrations["catalog"]
        reserved = self.lab.reserve_release_device(
            "catalog", "owner", "reservation-a", registration,
            application_id="ios_app", build_id="original")
        self.assertEqual(len(self.issued), 1)
        session = self.lab.create_release_session(
            "catalog", "owner", "controller", registration,
            application_id="ios_app", build_id="original", preparation_receipts=[],
            device_reservation=reserved)
        self.assertEqual(len(self.issued), 1)
        self.assertEqual(session["state"], "active")

    def test_explicit_wrong_project_grant_is_not_replaced(self):
        wrong = issue_bounded_project_grant(self.authority, "checkout", lifetime_seconds=60)
        with self.assertRaises(LiveError) as denied:
            self.lab.create_release_session(
                "catalog", "owner", "controller", self.registrations["catalog"],
                application_id="ios_app", build_id="original", preparation_receipts=[],
                authority_grant=wrong)
        self.assertEqual(denied.exception.code, "recording_identity")
        self.assertEqual(self.issued, [])

    def test_profile_cannot_collect_a_category_the_project_disallows(self):
        registration = self.registrations["checkout"]
        document = physical_ios_document("0" * 64)
        document["projectDigest"] = registration.project_digest
        document["capabilities"]["observations"].append("logs")
        document["capabilities"]["logAdapter"] = {"id": "repro-app-log", "version": 1}
        profile = validate_ios_profile(document)
        self.lab.devices["checkout"]["capabilities"].update(
            applicationProfile=profile.data, applicationProfileDigest=profile.digest,
            applicationIdentity=profile.application_identity)
        with self.assertRaises(LiveError) as denied:
            self.lab.create_release_session(
                "checkout", "owner", "controller", registration,
                application_id="ios_app", build_id="original", preparation_receipts=[])
        self.assertEqual(denied.exception.code, "capture_suppressed")
        self.assertEqual(self.issued, [])
