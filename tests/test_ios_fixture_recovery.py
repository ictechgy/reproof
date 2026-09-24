"""Bounded fixture cleanup under an exact native iOS recovery context."""

from dataclasses import replace
import gc
import threading
import time
import unittest
import weakref
from unittest.mock import patch

from reproof import contracts
from reproof.ios_fixture_recovery import (
    IOSFixtureRecoveryError,
    recover_ios_fixtures,
    require_ios_fixture_recovery,
)
from reproof.ios_mobile_callbacks import IOSNativeCallbackCoordinator
from reproof.ios_mobile_operation import IOSMobileOperationStore
from reproof.ios_native_recovery import native_recovery
from reproof.live.authority import HostAuthority
from reproof.live.issue_sessions import (
    FIXTURE_RESERVATION_VERSION,
    _operation,
    fixture_reservation_id,
)
from tests.ios_service_support import IOSServiceFixture


class IOSFixtureRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = IOSServiceFixture()
        self.addCleanup(self.fixture.close)
        old_authority = self.fixture.authority
        device = old_authority.claim_device(
            device_kind="ios-physical",
            physical_id=self.fixture.udid,
            helper_incarnation="interrupted_fixture_helper",
            parent_grant=self.fixture.grant,
        )
        with self.fixture.admit() as operation:
            for role in self.fixture.operations._roles:
                self.fixture.operations.prepare(
                    operation, role, cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 30,
                )
            with self.fixture.operations.native_owner(operation, device) as owner:
                coordinator = IOSNativeCallbackCoordinator(owner)
                with coordinator.callback(operation, "install") as token:
                    token.complete(contracts.digest("fixture-install-complete"))
                # Use the real callback journal admission and stop before a
                # process-local token or issue admission survives the restart.
                coordinator._journal_enter("replay", 1, 2)
                coordinator.close()
                admission = device.admit_operation(
                    operation_id="interrupted_fixture_dispatch",
                    payload_digest=contracts.digest("interrupted-fixture-payload"),
                    session_id="interrupted_fixture_session",
                    sequence=1,
                )
                device.prepare_dispatch(
                    admission, provider_incarnation="interrupted_fixture_provider"
                )
        self.assertFalse(device.close())
        old_authority.close()
        self.assertTrue(self.fixture.operations.close())

        self.authority = HostAuthority(
            self.fixture.root / "authority.sqlite3",
            lease_directory=self.fixture.root / "leases",
        )
        self.addCleanup(self.authority.close)
        received = self.authority.clock_sync.sample()
        sent = self.authority.clock_sync.sample()
        mapping = self.authority.clock_sync.record_exchange(
            coordinator_clock_id="ios-fixture-recovery-coordinator",
            coordinator_send_ns=received.nanoseconds,
            host_received=received,
            host_sent=sent,
            coordinator_receive_ns=sent.nanoseconds,
            max_drift_ppm=0,
        )
        self.grant = self.authority.issue_parent_grant(
            mapping,
            grant_id="ios_fixture_recovery_grant",
            project_id=self.fixture.registration.project["id"],
            controller_id="ios_fixture_recovery_controller",
            renewal_sequence=1,
            coordinator_deadline_ns=sent.nanoseconds + 600_000_000_000,
        )
        self.device = self.authority.claim_device(
            device_kind="ios-physical",
            physical_id=self.fixture.udid,
            helper_incarnation="ignored_fixture_recovery_helper",
            parent_grant=self.grant,
        )
        self.addCleanup(self.device.close)
        self.operations = IOSMobileOperationStore(
            self.fixture.runs,
            self.fixture.config.definition,
            self.fixture.operations.root,
            create=False,
        )
        self.addCleanup(self.operations.close)
        self.service = self.fixture.config.service
        self.coordinator = self.service.fixtures
        self.preparation = self.fixture.config.preparations[0]
        self.plan = self.preparation.plan
        self.issue_id = "mobile_" + contracts.digest({
            "context": self.fixture.context.digest, "attempt": 1,
        })[:40]
        self.allocation_id = fixture_reservation_id(
            self.issue_id, self.plan.fixture_id
        )

    def _recovery(self):
        return native_recovery(
            self.operations,
            self.fixture.context.operation_id,
            self.fixture.context.request_digest,
            device=self.device,
            snapshot=self.device.recovery_snapshot(),
            parent_grant=self.grant,
            cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 30,
        )

    def _issue_record(self, fixtures, *, planned=True):
        record = {
            "schemaVersion": 1,
            "issueId": self.issue_id,
            "state": "quarantined",
            "projectDigest": self.fixture.registration.project_digest,
            "deviceId": self.fixture.config.device_id,
            "applicationId": self.fixture.config.application_id,
            "buildId": "candidate_" + contracts.digest({
                "operation": self.fixture.context.operation_id,
                "request": self.fixture.context.request_digest,
            })[:32],
            "fixtures": fixtures,
        }
        if planned:
            record["fixtureReservationVersion"] = FIXTURE_RESERVATION_VERSION
            record["fixtureReservations"] = [{
                "fixtureId": self.plan.fixture_id,
                "allocationId": self.allocation_id,
            }]
        self.service._persist(record)
        return record

    def _quarantined_allocation(self):
        allocation = self.coordinator.reserve(
            self.plan,
            owner=self.fixture.config.owner,
            device_id=self.fixture.config.device_id,
            allocation_id=self.allocation_id,
        )
        outcome = self.coordinator.prepare(
            self.plan,
            allocation,
            payload=self.preparation.payload,
            operation_id=_operation(self.issue_id, self.plan.fixture_id, "prepare"),
        )
        self.assertEqual(outcome.status, "complete")
        self.coordinator.retain_for_cleanup(allocation)
        self.assertEqual(self.coordinator.status(allocation)["state"], "quarantined")
        return allocation

    def _recover(self, context):
        return recover_ios_fixtures(
            context,
            self.fixture.config,
            cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 20,
        )

    def test_active_issue_is_rejected_without_cleaning_its_fixture(self):
        allocation = self._quarantined_allocation()
        record = self._issue_record([self.coordinator.status(allocation)])
        with self.service._lock:
            self.service._active[self.issue_id] = {"record": record}
        self.addCleanup(self.service._active.pop, self.issue_id, None)
        with self._recovery() as context:
            with self.assertRaises(IOSFixtureRecoveryError) as raised:
                self._recover(context)
            self.assertEqual(raised.exception.code, "ios_fixture_recovery_issue_active")
        self.assertEqual(self.coordinator.status(allocation)["state"], "quarantined")

    def test_only_not_found_is_an_explicit_pre_admission_absence(self):
        with self._recovery() as context:
            with patch.object(self.service, "get", side_effect=RuntimeError("storage failed")):
                with self.assertRaises(IOSFixtureRecoveryError):
                    self._recover(context)
        self.assertIsNone(self.coordinator.lookup_recovery_allocation(
            self.plan,
            allocation_id=self.allocation_id,
            owner=self.fixture.config.owner,
            device_id=self.fixture.config.device_id,
        ))

    def test_legacy_missing_fixture_reference_is_not_treated_as_absent(self):
        allocation = self._quarantined_allocation()
        self._issue_record([], planned=False)
        with self._recovery() as context:
            with self.assertRaises(IOSFixtureRecoveryError) as raised:
                self._recover(context)
            self.assertEqual(raised.exception.code, "ios_fixture_recovery_issue")
        self.assertEqual(self.coordinator.status(allocation)["state"], "quarantined")

    def test_planned_absence_is_sealed_without_remote_cleanup(self):
        self._issue_record([])
        with self._recovery() as context:
            proof = self._recover(context)
            self.assertEqual((proof.allocations, proof.completed), (1, 1))
            self.assertIs(require_ios_fixture_recovery(proof, context), proof)
        found = self.coordinator.lookup_recovery_allocation(
            self.plan,
            allocation_id=self.allocation_id,
            owner=self.fixture.config.owner,
            device_id=self.fixture.config.device_id,
        )
        self.assertEqual((found["generation"], found["state"]), (0, "available"))

    def test_corrupt_planned_reservation_identity_is_rejected(self):
        record = self._issue_record([])
        record["fixtureReservations"][0]["allocationId"] = "allocation_unrelated"
        self.service._persist(record)
        with self._recovery() as context:
            with self.assertRaises(IOSFixtureRecoveryError) as raised:
                self._recover(context)
            self.assertEqual(raised.exception.code, "ios_fixture_recovery_issue")
        self.assertIsNone(self.coordinator.lookup_recovery_allocation(
            self.plan,
            allocation_id=self.allocation_id,
            owner=self.fixture.config.owner,
            device_id=self.fixture.config.device_id,
        ))

    def test_genuine_cleanup_binds_selection_receipt_and_preserves_new_generation(self):
        allocation = self._quarantined_allocation()
        self._issue_record([self.coordinator.status(allocation)])
        with self._recovery() as context:
            proof = self._recover(context)
            self.assertEqual((proof.allocations, proof.completed), (1, 1))
            self.assertEqual(len(proof.selection_digest), 64)
            self.assertEqual(len(proof.results_digest), 64)
            self.assertIs(require_ios_fixture_recovery(proof, context), proof)
            self.assertEqual(self.coordinator.status(allocation)["state"], "available")

            current = self.coordinator.reserve(
                self.plan, owner="new-owner", device_id="new-device",
                allocation_id="new_fixture_generation",
            )
            current_before = self.coordinator.status(current)
            retry = self._recover(context)
            self.assertIs(require_ios_fixture_recovery(retry, context), retry)
            self.assertEqual(self.coordinator.status(current), current_before)

    def test_proof_is_exact_context_bound_and_weakly_registered(self):
        with self._recovery() as first:
            proof = self._recover(first)
            proof_reference = weakref.ref(proof)
            self.assertIs(require_ios_fixture_recovery(proof, first), proof)
        with self._recovery() as second:
            with self.assertRaises(IOSFixtureRecoveryError):
                require_ios_fixture_recovery(proof, second)
            with self.assertRaises(IOSFixtureRecoveryError):
                require_ios_fixture_recovery(replace(proof), second)
        del proof
        gc.collect()
        self.assertIsNone(proof_reference())

    def test_recovery_requires_exact_context_and_config_types(self):
        with self._recovery() as context:
            with self.assertRaises(IOSFixtureRecoveryError):
                recover_ios_fixtures(
                    object(), self.fixture.config,
                    cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 5,
                )
            with self.assertRaises(IOSFixtureRecoveryError):
                recover_ios_fixtures(
                    context, object(), cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 5,
                )

    def test_cancelled_recovery_does_not_publish_or_seal(self):
        cancellation = threading.Event()
        cancellation.set()
        with self._recovery() as context:
            with self.assertRaises(IOSFixtureRecoveryError):
                recover_ios_fixtures(
                    context, self.fixture.config, cancellation=cancellation,
                    deadline_monotonic=time.monotonic() + 5,
                )
        self.assertIsNone(self.coordinator.lookup_recovery_allocation(
            self.plan,
            allocation_id=self.allocation_id,
            owner=self.fixture.config.owner,
            device_id=self.fixture.config.device_id,
        ))


if __name__ == "__main__":
    unittest.main()
