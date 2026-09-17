from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from reproloop.core import ContractError
from reproloop.live.authority import HostAuthority, ProviderResult
from reproloop.live.clock_sync import ClockReading
from reproloop.live.state_store import FORMAT_VERSION, READER_VERSION, WRITER_VERSION, StateStore
from reproloop.storage import Lease


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


class FakeClock:
    clock_id = "test-suspend-clock"

    def __init__(self, nanoseconds=1_000_000, boot_digest="d" * 64):
        self.nanoseconds = nanoseconds
        self.boot_digest = boot_digest

    def read(self):
        return ClockReading(self.clock_id, self.boot_digest, self.nanoseconds, 0)

    def advance(self, nanoseconds):
        self.nanoseconds += nanoseconds


class AuthorityTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_path = self.root / "authority" / "state.sqlite3"
        self.lease_directory = self.root / "legacy-leases"
        self.clock = FakeClock()
        self.authority = HostAuthority(
            self.state_path,
            clock=self.clock,
            lease_directory=self.lease_directory,
            operation_cache_size=1,
        )

    def tearDown(self):
        self.authority.close()
        self.temporary.cleanup()

    def grant(self, *, grant_id="parent-grant", renewal_sequence=1, lifetime_ns=1_000_000):
        received = self.authority.clock_sync.sample()
        self.clock.advance(10)
        sent = self.authority.clock_sync.sample()
        coordinator_send = received.nanoseconds - 100
        coordinator_receive = sent.nanoseconds - 80
        mapping = self.authority.clock_sync.record_exchange(
            coordinator_clock_id="coordinator-clock",
            coordinator_send_ns=coordinator_send,
            host_received=received,
            host_sent=sent,
            coordinator_receive_ns=coordinator_receive,
            max_drift_ppm=100,
        )
        return self.authority.issue_parent_grant(
            mapping,
            grant_id=grant_id,
            project_id="project-one",
            controller_id="controller-one",
            renewal_sequence=renewal_sequence,
            coordinator_deadline_ns=coordinator_receive + lifetime_ns,
        )

    def claim(self, **kwargs):
        values = {
            "device_kind": "android",
            "physical_id": "synthetic-device-one",
            "display_alias": "lab-left",
            "helper_incarnation": "helper-one",
            "parent_grant": self.grant(),
        }
        values.update(kwargs)
        return self.authority.claim_device(**values)


class HostAuthorityTests(AuthorityTestCase):
    def test_admission_is_durable_idempotent_and_never_persists_raw_input(self):
        device = self.claim()
        first = device.admit_operation(
            operation_id="operation-one",
            payload_digest=DIGEST_A,
            session_id="session-one",
            sequence=1,
        )
        again = device.admit_operation(
            operation_id="operation-one",
            payload_digest=DIGEST_A,
            session_id="session-one",
            sequence=1,
        )
        self.assertTrue(first.is_new)
        self.assertFalse(again.is_new)
        self.assertEqual(first.operation_fingerprint, again.operation_fingerprint)

        with self.assertRaisesRegex(ContractError, "Operation identity conflict"):
            device.admit_operation(
                operation_id="operation-one",
                payload_digest=DIGEST_B,
                session_id="session-one",
                sequence=1,
            )

        persisted = b"".join(
            path.read_bytes()
            for directory in (self.state_path.parent, self.lease_directory)
            if directory.exists()
            for path in directory.iterdir()
            if path.is_file()
        )
        self.assertNotIn(b"synthetic-device-one", persisted)
        self.assertNotIn(b"lab-left", persisted)
        self.assertNotIn(b"raw text that must be parameterized", persisted)

    def test_operation_identity_is_bound_to_one_canonical_device(self):
        first_device = self.claim()
        first_device.admit_operation(
            operation_id="operation-one", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        second_device = self.claim(
            physical_id="synthetic-device-two",
            display_alias="same-friendly-alias",
            helper_incarnation="helper-two",
        )
        try:
            with self.assertRaisesRegex(ContractError, "Operation identity conflict"):
                second_device.admit_operation(
                    operation_id="operation-one", payload_digest=DIGEST_A,
                    session_id="session-one", sequence=1,
                )
        finally:
            second_device.close()

    def test_dispatch_quarantines_before_callback_and_only_receipt_unblocks(self):
        device = self.claim()
        admission = device.admit_operation(
            operation_id="operation-one", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        observed = []

        def provider(permit):
            observed.append((device.status, permit.deadline_ns, permit.operation_id))
            return ProviderResult("receipt-one", "succeeded", DIGEST_B)

        result = device.dispatch_operation(
            admission, provider_incarnation="provider-one", callback=provider
        )

        self.assertEqual(observed[0][0], "quarantined")
        self.assertLessEqual(observed[0][1], device.parent_deadline_ns)
        self.assertEqual(observed[0][2], "operation-one")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(device.status, "owned")
        stored = self.authority.store.operation("operation-one")
        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["result_digest"], DIGEST_B)

    def test_blocked_callback_holds_no_store_lock_and_device_is_already_quarantined(self):
        device = self.claim()
        admission = device.admit_operation(
            operation_id="operation-one", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        entered = threading.Event()
        release = threading.Event()
        failures = []

        def provider(_permit):
            entered.set()
            release.wait(2)
            return ProviderResult("receipt-one", "succeeded", DIGEST_B)

        def run():
            try:
                device.dispatch_operation(
                    admission, provider_incarnation="provider-one", callback=provider
                )
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(device.status, "quarantined")
        self.assertEqual(self.authority.store.operation("operation-one")["status"], "uncertain")
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(device.status, "owned")

    def test_unconfirmed_callback_stays_quarantined_until_trusted_reconciliation(self):
        device = self.claim()
        admission = device.admit_operation(
            operation_id="operation-one", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )

        def provider(_permit):
            raise RuntimeError("provider detail must not escape")

        with self.assertRaisesRegex(ContractError, "Provider outcome is uncertain") as raised:
            device.dispatch_operation(
                admission, provider_incarnation="provider-one", callback=provider
            )
        self.assertNotIn("provider detail", str(raised.exception))
        self.assertEqual(device.status, "quarantined")
        with self.assertRaisesRegex(ContractError, "Device requires reconciliation"):
            device.admit_operation(
                operation_id="operation-two", payload_digest=DIGEST_B,
                session_id="session-one", sequence=2,
            )

        snapshot = device.recovery_snapshot()
        disposition = self.authority.record_operation_disposition(
            snapshot,
            operation_id="operation-one",
            terminal_status="rejected",
            result_digest=DIGEST_B,
            evidence_digest=DIGEST_C,
        )
        reconciliation = self.authority.record_reconciliation(
            snapshot,
            dispositions=[disposition],
            prior_helper_exit_digest=DIGEST_A,
            pointer_cleanup_digest=DIGEST_B,
            fresh_helper_incarnation="helper-two",
            fresh_handshake_digest=DIGEST_C,
        )
        tampered_disposition = replace(disposition, terminal_status="succeeded")
        tampered_reconciliation = replace(
            reconciliation, dispositions=(tampered_disposition,)
        )
        with self.assertRaisesRegex(ContractError, "Trusted reconciliation required"):
            device.reconcile(
                tampered_reconciliation,
                parent_grant=self.grant(grant_id="tampered-recovery-grant"),
            )
        device.reconcile(reconciliation, parent_grant=self.grant(
            grant_id="recovery-grant", renewal_sequence=1
        ))
        self.assertEqual(device.status, "owned")
        self.assertEqual(device.generation, 2)

    def test_unknown_provider_result_remains_quarantined(self):
        device = self.claim()
        admission = device.admit_operation(
            operation_id="operation-one", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        result = device.dispatch_operation(
            admission,
            provider_incarnation="provider-one",
            callback=lambda _permit: ProviderResult("receipt-one", "unknown", DIGEST_B),
        )
        self.assertEqual(result.status, "unknown")
        self.assertEqual(device.status, "quarantined")
        self.assertEqual(self.authority.store.operation("operation-one")["status"], "uncertain")

    def test_queued_operation_expires_and_delayed_renewal_cannot_revive_grant(self):
        device = self.claim(parent_grant=self.grant(lifetime_ns=100))
        admission = device.admit_operation(
            operation_id="operation-one", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        original_deadline = admission.deadline_ns
        renewal = self.grant(renewal_sequence=2, lifetime_ns=1_000)
        device.renew(renewal)
        self.assertGreater(device.parent_deadline_ns, original_deadline)
        self.assertEqual(admission.deadline_ns, original_deadline)
        with self.assertRaisesRegex(ContractError, "Renewal sequence is stale"):
            device.renew(self.grant(renewal_sequence=1, lifetime_ns=10_000))

        self.clock.nanoseconds = device.parent_deadline_ns + 1
        with self.assertRaisesRegex(ContractError, "Operation authority expired"):
            device.dispatch_operation(
                admission,
                provider_incarnation="provider-one",
                callback=lambda _permit: ProviderResult("receipt-one", "succeeded", DIGEST_B),
            )
        self.assertEqual(self.authority.store.operation("operation-one")["status"], "expired")
        with self.assertRaisesRegex(ContractError, "Expired authority cannot be renewed"):
            device.renew(self.grant(renewal_sequence=3, lifetime_ns=10_000))

    def test_cache_eviction_and_restart_keep_replay_watermark_and_result(self):
        device = self.claim()
        for number, name, payload in ((1, "one", DIGEST_A), (2, "two", DIGEST_B)):
            admission = device.admit_operation(
                operation_id=f"operation-{name}", payload_digest=payload,
                session_id="session-one", sequence=number,
            )
            device.dispatch_operation(
                admission,
                provider_incarnation="provider-one",
                callback=lambda _permit, number=number: ProviderResult(
                    f"receipt-{number}", "succeeded", DIGEST_C
                ),
            )
        self.assertEqual(device.cached_operation_count, 1)
        replay = device.admit_operation(
            operation_id="operation-one", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        self.assertFalse(replay.is_new)
        self.assertEqual(replay.status, "succeeded")
        with self.assertRaisesRegex(ContractError, "Operation sequence was already consumed"):
            device.admit_operation(
                operation_id="replacement-operation", payload_digest=DIGEST_A,
                session_id="session-one", sequence=1,
            )
        old_grant = self.grant(grant_id="old-process-grant")
        device.close()
        self.authority.close()

        restarted = HostAuthority(
            self.state_path, clock=self.clock, lease_directory=self.lease_directory
        )
        try:
            with self.assertRaisesRegex(ContractError, "Trusted parent grant required"):
                restarted.claim_device(
                    device_kind="android", physical_id="synthetic-device-one",
                    helper_incarnation="helper-two", parent_grant=old_grant,
                )
            received = restarted.clock_sync.sample()
            self.clock.advance(10)
            sent = restarted.clock_sync.sample()
            mapping = restarted.clock_sync.record_exchange(
                coordinator_clock_id="coordinator-clock",
                coordinator_send_ns=received.nanoseconds - 100,
                host_received=received, host_sent=sent,
                coordinator_receive_ns=sent.nanoseconds - 80,
            )
            fresh = restarted.issue_parent_grant(
                mapping, grant_id="fresh-grant", project_id="project-one",
                controller_id="controller-one", renewal_sequence=1,
                coordinator_deadline_ns=sent.nanoseconds + 1_000_000,
            )
            restored = restarted.claim_device(
                device_kind="android", physical_id="synthetic-device-one",
                helper_incarnation="helper-two", parent_grant=fresh,
            )
            self.assertEqual(restored.generation, 2)
            self.assertEqual(restarted.store.operation("operation-one")["status"], "succeeded")
            next_generation = restored.admit_operation(
                operation_id="operation-three", payload_digest=DIGEST_A,
                session_id="session-two", sequence=1,
            )
            self.assertTrue(next_generation.is_new)
            restored.close()
        finally:
            restarted.close()

    def test_clock_restart_refuses_dispatch_instead_of_reviving_authority(self):
        device = self.claim()
        admission = device.admit_operation(
            operation_id="operation-one", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        self.clock.boot_digest = "e" * 64
        with self.assertRaisesRegex(ContractError, "Clock mapping is incompatible"):
            device.dispatch_operation(
                admission, provider_incarnation="provider-one",
                callback=lambda _permit: ProviderResult("receipt-one", "succeeded", DIGEST_B),
            )
        self.assertEqual(device.status, "quarantined")

    def test_parent_grant_capability_cannot_be_modified_or_reused_by_another_authority(self):
        grant = self.grant()
        changed = replace(grant, renewal_sequence=99)
        with self.assertRaisesRegex(ContractError, "Trusted parent grant required"):
            self.authority.claim_device(
                device_kind="android", physical_id="synthetic-device-one",
                helper_incarnation="helper-one", parent_grant=changed,
            )
        other = HostAuthority(
            self.root / "other-authority" / "state.sqlite3",
            clock=self.clock,
            lease_directory=self.root / "other-leases",
        )
        try:
            with self.assertRaisesRegex(ContractError, "Trusted parent grant required"):
                other.claim_device(
                    device_kind="android", physical_id="synthetic-device-one",
                    helper_incarnation="helper-one", parent_grant=grant,
                )
        finally:
            other.close()

    def test_late_result_after_reconciliation_is_history_not_new_ownership_completion(self):
        device = self.claim()
        admission = device.admit_operation(
            operation_id="operation-one", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        permit = device.prepare_dispatch(admission, provider_incarnation="provider-one")
        snapshot = device.recovery_snapshot()
        disposition = self.authority.record_operation_disposition(
            snapshot, operation_id="operation-one", terminal_status="rejected",
            result_digest=DIGEST_A, evidence_digest=DIGEST_B,
        )
        reconciliation = self.authority.record_reconciliation(
            snapshot, dispositions=[disposition],
            prior_helper_exit_digest=DIGEST_A,
            pointer_cleanup_digest=DIGEST_B,
            fresh_helper_incarnation="helper-two",
            fresh_handshake_digest=DIGEST_C,
        )
        device.reconcile(reconciliation, parent_grant=self.grant(
            grant_id="recovery-grant"
        ))
        receipt = self.authority.record_provider_result(
            operation_id=permit.operation_id,
            generation=permit.ownership_generation,
            host_incarnation=permit.host_incarnation,
            provider_incarnation=permit.provider_incarnation,
            result=ProviderResult("receipt-late", "succeeded", DIGEST_C),
        )
        self.assertEqual(receipt["binding"], "late")
        self.assertEqual(device.status, "owned")
        self.assertEqual(device.generation, 2)
        self.assertEqual(self.authority.store.operation("operation-one")["status"], "rejected")

    def test_borrowed_legacy_lease_never_reacquires_or_releases_owner_lock(self):
        device = self.claim()
        borrowed = device.borrowed_lease()
        with borrowed:
            pass
        with self.assertRaisesRegex(ContractError, "Device is already leased"):
            with Lease("synthetic-device-one", self.lease_directory):
                pass
        device.close()
        with Lease("synthetic-device-one", self.lease_directory):
            pass

    def test_recovered_operation_preserves_unknown_and_late_provider_results(self):
        device = self.claim()
        admission = device.admit_operation(
            operation_id="operation-one", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        permit = device.prepare_dispatch(admission, provider_incarnation="provider-one")
        device.confirm_operation(permit, ProviderResult("receipt-unknown", "unknown", DIGEST_A))
        snapshot = device.recovery_snapshot()
        disposition = self.authority.record_operation_disposition(
            snapshot, operation_id="operation-one", terminal_status="recovered",
            result_digest=DIGEST_B, evidence_digest=DIGEST_C,
        )
        reconciliation = self.authority.record_reconciliation(
            snapshot, dispositions=[disposition], prior_helper_exit_digest=DIGEST_A,
            pointer_cleanup_digest=DIGEST_B, fresh_helper_incarnation="helper-two",
            fresh_handshake_digest=DIGEST_C,
        )
        device.reconcile(reconciliation, parent_grant=self.grant(grant_id="recovery-grant"))
        recovered = self.authority.store.operation("operation-one")
        self.assertEqual(recovered["status"], "recovered")
        # A restored state is not an invented result for the original command.
        self.assertIsNone(recovered["result_digest"])
        self.assertEqual(recovered["provider_incarnation"], "provider-one")
        self.assertEqual(recovered["operation_fingerprint"], admission.operation_fingerprint)
        self.assertIsNotNone(recovered["terminal_ns"])
        for status in ("succeeded", "rejected", "unknown"):
            receipt = self.authority.record_provider_result(
                operation_id=permit.operation_id, generation=permit.ownership_generation,
                host_incarnation=permit.host_incarnation,
                provider_incarnation=permit.provider_incarnation,
                result=ProviderResult("receipt-late-" + status, status, DIGEST_C),
            )
            self.assertEqual(receipt["binding"], "late")
            self.assertEqual(self.authority.store.operation("operation-one"), recovered)
        self.assertEqual(device.status, "owned")
        self.assertEqual(device.generation, 2)
        with self.assertRaises(ContractError):
            device.check_dispatch_permit(permit)
        with closing(sqlite3.connect(self.state_path)) as connection:
            self.assertEqual(connection.execute(
                "SELECT status, binding, result_digest FROM operation_receipts WHERE receipt_id = ?",
                ("receipt-unknown",),
            ).fetchone(), ("unknown", "current", DIGEST_A))
            self.assertEqual(connection.execute(
                "SELECT terminal_status, result_digest, evidence_digest FROM reconciliation_dispositions"
            ).fetchone(), ("recovered", DIGEST_B, DIGEST_C))
        device.close()
        self.authority.close()
        with closing(StateStore(self.state_path)) as reopened:
            self.assertEqual(reopened.operation("operation-one"), recovered)

    def test_recovered_disposition_is_invalid_for_queued_operations(self):
        device = self.claim()
        device.admit_operation(
            operation_id="operation-one", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        device.revoke_dispatches()
        with self.assertRaisesRegex(ContractError, "Queued operation disposition is invalid"):
            self.authority.record_operation_disposition(
                device.recovery_snapshot(), operation_id="operation-one",
                terminal_status="recovered", result_digest=DIGEST_B, evidence_digest=DIGEST_C,
            )
        self.assertEqual(self.authority.store.operation("operation-one")["status"], "queued")

    def test_provider_cannot_report_recovery_as_an_execution_result(self):
        with self.assertRaisesRegex(ContractError, "Invalid provider result status"):
            ProviderResult("receipt-one", "recovered", DIGEST_A)

    def test_reconciliation_can_keep_dispatch_quarantined_until_final_cleanup(self):
        device = self.claim()
        device.revoke_dispatches()
        reconciliation = self.authority.record_reconciliation(
            device.recovery_snapshot(), dispositions=[], prior_helper_exit_digest=DIGEST_A,
            pointer_cleanup_digest=DIGEST_B, fresh_helper_incarnation="helper-cleanup",
            fresh_handshake_digest=DIGEST_C,
        )
        device.reconcile(reconciliation, parent_grant=self.grant(grant_id="cleanup-grant"),
                         cleanup_pending=True)
        self.assertEqual(device.generation, 2)
        self.assertEqual(device.status, "quarantined")
        self.assertEqual(device.recovery_snapshot().quarantine_reason, "recovery-cleanup-pending")
        device.revoke_dispatches()
        self.authority.store.quarantine_clock(
            device_fingerprint=reconciliation.device_fingerprint, generation=device.generation,
            host_incarnation=self.authority.host_incarnation, now_ns=self.clock.nanoseconds,
        )
        self.assertEqual(device.recovery_snapshot().quarantine_reason, "recovery-cleanup-pending")
        with self.assertRaises(ContractError):
            device.admit_operation(operation_id="too-early", payload_digest=DIGEST_A,
                                   session_id="session-two", sequence=1)
        another = self.authority.record_reconciliation(
            device.recovery_snapshot(), dispositions=[], prior_helper_exit_digest=DIGEST_A,
            pointer_cleanup_digest=DIGEST_B, fresh_helper_incarnation="helper-too-early",
            fresh_handshake_digest=DIGEST_C,
        )
        with self.assertRaisesRegex(ContractError, "Recovery cleanup is still pending"):
            device.reconcile(another, parent_grant=self.grant(grant_id="bypass-grant"))
        self.assertFalse(device.close())
        row = self.authority.store.device(reconciliation.device_fingerprint)
        self.assertEqual(row["status"], "quarantined")
        self.assertEqual(row["generation"], 2)

    def test_native_recovery_fences_a_racing_original_provider_receipt(self):
        device = self.claim()
        admitted = device.admit_operation(operation_id="operation-one", payload_digest=DIGEST_A,
                                         session_id="session-one", sequence=1)
        permit = device.prepare_dispatch(admitted, provider_incarnation="provider-one")
        snapshot = device.recovery_snapshot()
        self.assertEqual(snapshot.quarantine_reason, "provider-outcome-unconfirmed")
        with device.borrow_native_recovery_lease(snapshot, parent_grant=self.grant(grant_id="recovery-grant")) as borrowed:
            receipt = self.authority.record_provider_result(operation_id=permit.operation_id,
                generation=permit.ownership_generation, host_incarnation=permit.host_incarnation,
                provider_incarnation=permit.provider_incarnation,
                result=ProviderResult("receipt-racing", "succeeded", DIGEST_B))
            self.assertEqual(receipt["binding"], "late")
            self.assertTrue(device.requires_reconciliation)
            self.assertEqual(self.authority.store.operation("operation-one")["status"], "uncertain")
            self.assertIs(device.require_native_recovery_lease(borrowed), borrowed)


class StateStoreTests(AuthorityTestCase):
    def test_store_refuses_newer_format_and_minimum_reader_writer(self):
        self.authority.close()
        with closing(sqlite3.connect(self.state_path)) as connection:
            connection.execute("UPDATE authority_metadata SET value = ? WHERE key = 'minimum_reader_version'",
                               (str(READER_VERSION + 1),))
            connection.commit()
        with self.assertRaisesRegex(ContractError, "Authority store version is incompatible"):
            StateStore(self.state_path)

        other = self.root / "other" / "state.sqlite3"
        store = StateStore(other)
        store.close()
        with closing(sqlite3.connect(other)) as connection:
            connection.execute(f"PRAGMA user_version = {FORMAT_VERSION + 1}")
        with self.assertRaisesRegex(ContractError, "Authority store version is incompatible"):
            StateStore(other)

        writer = self.root / "writer" / "state.sqlite3"
        store = StateStore(writer)
        store.close()
        with closing(sqlite3.connect(writer)) as connection:
            connection.execute(
                "UPDATE authority_metadata SET value = ? WHERE key = 'minimum_writer_version'",
                (str(WRITER_VERSION + 1),),
            )
            connection.commit()
        with self.assertRaisesRegex(ContractError, "Authority store version is incompatible"):
            StateStore(writer)

    def test_store_rejects_symlink_before_sqlite_opens_it(self):
        self.authority.close()
        target = self.root / "target.sqlite3"
        target.write_bytes(b"not a database")
        linked = self.root / "linked.sqlite3"
        linked.symlink_to(target)
        with self.assertRaisesRegex(ContractError, "Unsafe authority state path"):
            StateStore(linked)

    def test_metadata_shape_is_explicit_and_bounded(self):
        with closing(sqlite3.connect(self.state_path)) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM authority_metadata"))
        self.assertEqual(metadata, {
            "format_version": str(FORMAT_VERSION),
            "minimum_reader_version": str(READER_VERSION),
            "minimum_writer_version": str(WRITER_VERSION),
        })
        self.assertLessEqual(self.authority.store.max_page_count, 16_384)

    def test_legacy_adoption_is_digest_only_and_immutable(self):
        first = self.authority.record_legacy_adoption(
            adoption_id="legacy-one", artifact_digest=DIGEST_A, semantics="lock-only"
        )
        again = self.authority.record_legacy_adoption(
            adoption_id="legacy-one", artifact_digest=DIGEST_A, semantics="lock-only"
        )
        self.assertEqual(first["adoption_fingerprint"], again["adoption_fingerprint"])
        with self.assertRaisesRegex(ContractError, "Legacy adoption identity conflict"):
            self.authority.record_legacy_adoption(
                adoption_id="legacy-one", artifact_digest=DIGEST_B,
                semantics="unverified-history",
            )


if __name__ == "__main__":
    unittest.main()
