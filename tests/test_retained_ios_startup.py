"""Bounded retained iOS startup capabilities stay inside the owning Lab."""
import copy
from pathlib import Path
import tempfile
import unittest

from reproloop import contracts
from reproloop.fixtures import FixtureCoordinator
from reproloop.live.authority import HostAuthority, canonical_device_fingerprint
from reproloop.live.model import Lab, LiveError
from tests.test_fixture_allocations import collection_policy, project_document
from tests.test_live_authority_integration import Clock, FencedProvider, parent_grant


class RetainedIOSStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.authority = HostAuthority(
            self.root / "authority.sqlite3", clock=self.clock,
            lease_directory=self.root / "leases")
        self.grant = parent_grant(self.authority, self.clock)
        project = project_document()
        project["id"] = "integration-project"
        self.provider = FencedProvider()
        self.descriptor = {
            "id": "device", "name": "Retained iPhone", "platform": "ios",
            "kind": "ios-physical", "factory": lambda: self.provider,
            "capabilities": {
                "actions": ["tap"], "inputMode": "gesture-batch",
                "media": "demo-svg",
                "applicationIdentity": {
                    "bundle": "com.example.app", "artifactDigest": "0" * 64,
                },
            },
            "_authority": {"deviceKind": "ios-physical", "physicalId": "retained-iphone"},
        }
        self.lab = Lab([self.descriptor], self.root / "lab",
                       authority=self.authority, parent_grant=self.grant)
        self.registration = self.lab.register_recording_project(
            project, collection_policy(), capacity_bytes=128 * 1024 * 1024,
            journal_headroom_bytes=256 * 1024)
        self.extra_labs = []

    def tearDown(self):
        for lab in [self.lab, *self.extra_labs]:
            try:
                lab.close_all()
            except Exception:
                pass
        self.authority.close()
        self.temp.cleanup()

    def reserve_scope(self, lab=None, registration=None):
        lab = lab or self.lab
        registration = registration or self.registration
        reservation = lab.reserve_release_device(
            "device", "owner", "reservation-" + str(len(lab._retained_device_scopes)),
            registration, application_id="ios_app", build_id="original")
        return lab.retain_device_reservation(reservation)

    def native_payload(self, *, provider_incarnation="ios-xctest-" + "a" * 24,
                       scope_digest=None, application_id="ios_app"):
        return {
            "kind": "ios-fixed-xctest-launch-v1",
            "contextDigest": "1" * 64,
            "nativeBindingDigest": "2" * 64,
            "scopeDigest": scope_digest or canonical_device_fingerprint(
                "ios-physical", "retained-iphone"),
            "applicationId": application_id,
            "providerIncarnation": provider_incarnation,
        }

    def bind(self, scope, *, provider=None, logical=None, native=None,
              provider_incarnation="ios-xctest-" + "a" * 24):
        identity = copy.deepcopy(self.descriptor["capabilities"]["applicationIdentity"])
        return self.lab.bind_retained_startup(
            scope, owner="owner", provider=provider or self.provider,
            logical_payload=logical or {
                "kind": "ios-physical", "applicationIdentity": identity,
            },
            native_payload=native or self.native_payload(
                provider_incarnation=provider_incarnation),
            provider_incarnation=provider_incarnation)

    def start(self, scope, binding, *, provider_factory=None, lab=None,
              registration=None):
        lab = lab or self.lab
        registration = registration or self.registration
        return lab.create_release_session(
            "device", "owner", "attempt", registration,
            application_id="ios_app", build_id="original", preparation_receipts=[],
            device_scope=scope,
            _provider_factory=provider_factory or (lambda: self.provider),
            _startup_binding=binding)

    def finish(self, scope, session, lab=None):
        lab = lab or self.lab
        lab.close_session(session["id"], "owner")
        lab.release_retained_device_scope(scope)

    def test_exact_payload_digest_and_provider_incarnation_are_admitted(self):
        scope = self.reserve_scope()
        binding = self.bind(scope)
        session = self.start(scope, binding)
        startup = self.provider.permits[0][1]
        expected = contracts.digest(binding.native_payload)
        self.assertEqual(session["state"], "active")
        self.assertEqual(self.lab.sessions[session["id"]]["providerIncarnation"],
                         binding.provider_incarnation)
        self.assertEqual(startup.provider_incarnation, binding.provider_incarnation)
        self.assertEqual(startup.payload_digest, expected)
        self.assertEqual(self.authority.store.operation(startup.operation_id)["payload_digest"], expected)
        self.assertNotIn("_startupBinding", session)
        self.assertNotIn("_startupBinding", self.lab.get_session(session["id"], "owner"))
        self.finish(scope, session)

    def test_exact_scope_effect_shares_sequence_without_qualification_flags(self):
        scope = self.reserve_scope()
        payload = {"kind": "ios-fixed-install-v1", "scopeDigest": "3" * 64}
        permit = self.lab.prepare_retained_scope_exact_effect(
            scope, owner="owner", kind="ios-fixed-install-v1",
            payload_digest=contracts.digest(payload), provider_incarnation="ios-installer")
        self.assertEqual(permit.sequence, 1)
        self.assertEqual(permit.payload_digest, contracts.digest(payload))
        self.assertNotIn("qualified", self.lab.devices["device"])
        self.lab.confirm_retained_scope_effect(
            scope, permit, owner="owner", status="succeeded",
            result_digest=contracts.digest({"installed": True}))
        binding = self.bind(scope)
        session = self.start(scope, binding)
        self.assertEqual(self.provider.permits[0][1].sequence, 2)
        self.finish(scope, session)

    def test_copy_is_rejected_without_consuming_the_original_capability(self):
        scope = self.reserve_scope()
        binding = self.bind(scope)
        with self.assertRaises(LiveError):
            self.start(scope, copy.copy(binding))
        self.assertEqual(self.provider.permits, [])
        session = self.start(scope, binding)
        self.finish(scope, session)

    def test_reuse_is_rejected_after_one_successful_consumption(self):
        scope = self.reserve_scope()
        binding = self.bind(scope)
        session = self.start(scope, binding)
        self.finish(scope, session)
        with self.assertRaises(LiveError):
            self.start(scope, binding)
        self.assertEqual(len(self.provider.permits), 2)  # startup and cleanup only

    def test_logical_selection_mismatch_has_no_native_effect(self):
        scope = self.reserve_scope()
        binding = self.bind(scope, logical={
            "kind": "ios-physical-other",
            "applicationIdentity": copy.deepcopy(
                self.descriptor["capabilities"]["applicationIdentity"]),
        })
        with self.assertRaises(LiveError) as caught:
            self.start(scope, binding)
        self.assertEqual(caught.exception.code, "recording_identity")
        self.assertEqual(self.provider.permits, [])
        self.lab.release_retained_device_scope(scope)

    def test_wrong_factory_is_rejected_before_authority_admission_and_cap_is_burned(self):
        scope = self.reserve_scope()
        binding = self.bind(scope)
        wrong = FencedProvider()
        with self.assertRaises(LiveError) as caught:
            self.start(scope, binding, provider_factory=lambda: wrong)
        self.assertEqual(caught.exception.code, "native_protocol_mismatch")
        self.assertEqual(self.provider.permits, [])
        self.assertEqual(wrong.permits, [])
        with self.assertRaises(LiveError):
            self.start(scope, binding)
        self.assertEqual(self.lab.devices['device']['state'],'quarantined')
        with self.assertRaises(LiveError):
            self.lab.release_retained_device_scope(scope)

    def test_foreign_and_stale_bindings_are_rejected(self):
        scope = self.reserve_scope()
        binding = self.bind(scope)
        other_provider = FencedProvider()
        other_descriptor = dict(self.descriptor, factory=lambda: other_provider)
        other = Lab([other_descriptor], self.root / "other-lab",
                    authority=self.authority, parent_grant=self.grant)
        self.extra_labs.append(other)
        other_registration = other.register_recording_project(
            self.registration.project, collection_policy(),
            capacity_bytes=128 * 1024 * 1024, journal_headroom_bytes=256 * 1024)
        with self.assertRaises(LiveError):
            self.start(scope, binding, lab=other, registration=other_registration)
        self.assertEqual(other_provider.permits, [])
        self.lab.release_retained_device_scope(scope)
        with self.assertRaises(LiveError):
            self.start(scope, binding)

    def test_malformed_native_payload_is_rejected_at_binding(self):
        scope = self.reserve_scope()
        for field in ("contextDigest", "nativeBindingDigest", "scopeDigest"):
            malformed = self.native_payload()
            malformed[field] = "bad"
            with self.subTest(field=field), self.assertRaises(LiveError):
                self.bind(scope, native=malformed)
        wrong_kind = self.native_payload()
        wrong_kind["kind"] = "startup"
        with self.assertRaises(LiveError):
            self.bind(scope, native=wrong_kind)
        self.lab.release_retained_device_scope(scope)

    def test_issue_service_threads_private_binding_to_lab(self):
        fixtures = FixtureCoordinator(self.root / "fixtures")
        self.addCleanup(fixtures.close)
        service = self.lab.create_issue_session_service(
            fixtures, root=self.root / "issues")
        scope = self.reserve_scope()
        binding = self.bind(scope)
        handle = service.start_prepared_recording(
            device_id="device", owner="owner", controller_id="attempt",
            registration=self.registration, application_id="ios_app",
            build_id="original", preparations=[], device_scope=scope,
            _provider_factory=lambda: self.provider, _startup_binding=binding)
        self.assertEqual(self.provider.permits[0][1].payload_digest,
                         contracts.digest(binding.native_payload))
        service.stop(handle)
        self.lab.release_retained_device_scope(scope)

    def test_ordinary_startup_keeps_generic_payload_and_random_incarnation(self):
        session = self.lab.create_session("device", "owner", "ordinary")
        startup = self.provider.permits[0][1]
        self.assertEqual(session["state"], "active")
        self.assertNotEqual(self.lab.sessions[session["id"]]["providerIncarnation"],
                            "ios-xctest-" + "a" * 24)
        self.assertEqual(startup.payload_digest, contracts.digest({
            "kind": "startup",
            "payload": {
                "kind": "ios-physical",
                "applicationIdentity": self.descriptor["capabilities"]["applicationIdentity"],
            },
        }))
        self.lab.close_session(session["id"], "owner")


if __name__ == "__main__":
    unittest.main()
