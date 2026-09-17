"""Independent parent checks for authority race and binding behavior."""

from contextlib import closing
import sqlite3
import threading
from unittest.mock import patch

from reproloop.core import ContractError
from reproloop.live.authority import ProviderResult
from reproloop.storage import Lease
from tests.test_live_authority import AuthorityTestCase, DIGEST_A, DIGEST_B


class ParentAuthorityBoundaryTests(AuthorityTestCase):
    def test_idempotent_admission_survives_time_and_parent_renewal(self):
        device = self.claim()
        fields = dict(
            operation_id="stable-operation", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        first = device.admit_operation(**fields)
        self.clock.advance(100)
        device.renew(self.grant(renewal_sequence=2, lifetime_ns=2_000_000))
        duplicate = device.admit_operation(**fields)
        self.assertFalse(duplicate.is_new)
        self.assertEqual(duplicate.operation_fingerprint, first.operation_fingerprint)
        self.assertEqual(duplicate.deadline_ns, first.deadline_ns)

    def test_expired_renewal_is_persisted_despite_the_rejection_exception(self):
        device = self.claim()
        self.clock.nanoseconds = device.parent_deadline_ns + 1
        renewed = self.grant(renewal_sequence=2)
        with self.assertRaises(ContractError):
            device.renew(renewed)
        with closing(sqlite3.connect(self.state_path)) as connection:
            status = connection.execute("SELECT status FROM devices").fetchone()[0]
        self.assertIn(status, {"expired", "quarantined"})

    def test_new_clock_mapping_cannot_renew_old_clock_ownership(self):
        device = self.claim()
        self.clock.boot_digest = "e" * 64
        renewed = self.grant(renewal_sequence=2)
        with self.assertRaises(ContractError):
            device.renew(renewed)
        with closing(sqlite3.connect(self.state_path)) as connection:
            status = connection.execute("SELECT status FROM devices").fetchone()[0]
        self.assertIn(status, {"expired", "quarantined"})

    def test_close_racing_with_claim_cannot_leak_a_canonical_lock(self):
        row_created = threading.Event()
        release = threading.Event()
        close_finished = threading.Event()
        handles = []
        failures = []
        original_claim = self.authority.store.claim_device

        def delayed_claim(**kwargs):
            row = original_claim(**kwargs)
            row_created.set()
            if not release.wait(5):
                raise RuntimeError("owned claim test timed out")
            return row

        def claim():
            try:
                handles.append(self.claim())
            except ContractError:
                pass
            except Exception as error:
                failures.append(type(error).__name__)

        def close():
            try:
                self.authority.close()
            except Exception as error:
                failures.append(type(error).__name__)
            finally:
                close_finished.set()

        claim_thread = threading.Thread(target=claim, daemon=True)
        close_thread = threading.Thread(target=close, daemon=True)
        try:
            with patch.object(self.authority.store, "claim_device", side_effect=delayed_claim):
                claim_thread.start()
                self.assertTrue(row_created.wait(2))
                close_thread.start()
                close_finished.wait(0.05)
                release.set()
                claim_thread.join(2)
                close_thread.join(2)
                self.assertFalse(claim_thread.is_alive())
                self.assertFalse(close_thread.is_alive())
            self.assertEqual(failures, [])
            with Lease("synthetic-device-one", self.lease_directory):
                pass
        finally:
            release.set()
            claim_thread.join(2)
            if close_thread.ident is not None:
                close_thread.join(2)
            for handle in handles:
                handle.close()

    def test_a_later_queued_operation_cannot_overtake_an_earlier_one(self):
        device = self.claim()
        first = device.admit_operation(
            operation_id="queued-first", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        second = device.admit_operation(
            operation_id="queued-second", payload_digest=DIGEST_B,
            session_id="session-one", sequence=2,
        )
        calls = []
        with self.assertRaises(ContractError):
            device.dispatch_operation(
                second, provider_incarnation="provider-one",
                callback=lambda permit: calls.append(permit.operation_id),
            )
        self.assertEqual(calls, [])
        for admission, receipt_id in ((first, "first-receipt"), (second, "second-receipt")):
            result = device.dispatch_operation(
                admission, provider_incarnation="provider-one",
                callback=lambda _permit, receipt_id=receipt_id: ProviderResult(
                    receipt_id, "succeeded", DIGEST_B,
                ),
            )
            self.assertEqual(result.status, "succeeded")

    def test_renewal_does_not_extend_an_already_admitted_operation(self):
        device = self.claim(parent_grant=self.grant(lifetime_ns=100))
        admission = device.admit_operation(
            operation_id="short-operation", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        device.renew(self.grant(renewal_sequence=2, lifetime_ns=1_000_000))
        self.clock.nanoseconds = admission.deadline_ns + 1
        self.assertLess(self.clock.nanoseconds, device.parent_deadline_ns)
        calls = []
        with self.assertRaises(ContractError):
            device.dispatch_operation(
                admission, provider_incarnation="provider-one",
                callback=lambda permit: calls.append(permit.operation_id),
            )
        self.assertEqual(calls, [])
        self.assertEqual(device.status, "owned")
        later = device.admit_operation(
            operation_id="later-operation", payload_digest=DIGEST_B,
            session_id="session-one", sequence=2,
        )
        result = device.dispatch_operation(
            later, provider_incarnation="provider-one",
            callback=lambda _permit: ProviderResult("later-receipt", "succeeded", DIGEST_B),
        )
        self.assertEqual(result.status, "succeeded")

    def test_admission_cannot_be_dispatched_by_another_device_owner(self):
        first = self.claim()
        second = self.claim(
            physical_id="synthetic-device-two", display_alias="lab-right",
            parent_grant=self.grant(grant_id="second-parent"),
        )
        admission = first.admit_operation(
            operation_id="first-device-operation", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        calls = []
        with self.assertRaises(ContractError):
            second.dispatch_operation(
                admission, provider_incarnation="provider-two",
                callback=lambda permit: calls.append(permit.operation_id),
            )
        self.assertEqual(calls, [])
        result = first.dispatch_operation(
            admission, provider_incarnation="provider-one",
            callback=lambda _permit: ProviderResult("first-receipt", "succeeded", DIGEST_B),
        )
        self.assertEqual(result.status, "succeeded")

    def test_blocked_callback_does_not_hold_authority_or_admission_lock(self):
        device = self.claim()
        admission = device.admit_operation(
            operation_id="blocked-operation", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        entered = threading.Event()
        release = threading.Event()
        checked = threading.Event()
        results = []
        failures = []

        def callback(_permit):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("owned test callback timed out")
            return ProviderResult("blocked-receipt", "succeeded", DIGEST_B)

        def dispatch():
            try:
                device.dispatch_operation(
                    admission, provider_incarnation="provider-one", callback=callback,
                )
            except Exception as error:
                failures.append(type(error).__name__)

        def inspect_and_admit():
            try:
                results.append(device.status)
                try:
                    device.admit_operation(
                        operation_id="overlapping-operation", payload_digest=DIGEST_B,
                        session_id="session-one", sequence=2,
                    )
                except ContractError:
                    results.append("rejected")
            finally:
                checked.set()

        dispatch_thread = threading.Thread(target=dispatch, daemon=True)
        inspect_thread = threading.Thread(target=inspect_and_admit, daemon=True)
        dispatch_thread.start()
        try:
            self.assertTrue(entered.wait(2))
            inspect_thread.start()
            self.assertTrue(checked.wait(1), "Authority access waited for provider callback")
            self.assertEqual(results, ["quarantined", "rejected"])
        finally:
            release.set()
            dispatch_thread.join(2)
            if inspect_thread.ident is not None:
                inspect_thread.join(2)
        self.assertFalse(dispatch_thread.is_alive())
        self.assertFalse(inspect_thread.is_alive())
        self.assertEqual(failures, [])

    def test_renewal_cannot_change_project_or_controller(self):
        device = self.claim()
        for project_id, controller_id in (
            ("project-two", "controller-one"),
            ("project-one", "controller-two"),
        ):
            with self.subTest(project=project_id, controller=controller_id):
                received = self.authority.clock_sync.sample()
                self.clock.advance(10)
                sent = self.authority.clock_sync.sample()
                mapping = self.authority.clock_sync.record_exchange(
                    coordinator_clock_id="coordinator-clock",
                    coordinator_send_ns=received.nanoseconds - 100,
                    host_received=received, host_sent=sent,
                    coordinator_receive_ns=sent.nanoseconds - 80,
                )
                with self.assertRaises(ContractError):
                    renewed = self.authority.issue_parent_grant(
                        mapping, grant_id="parent-grant", project_id=project_id,
                        controller_id=controller_id, renewal_sequence=2,
                        coordinator_deadline_ns=sent.nanoseconds + 1_000_000,
                    )
                    device.renew(renewed)
        device.renew(self.grant(renewal_sequence=3, lifetime_ns=1_000_000))
