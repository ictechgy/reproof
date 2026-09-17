"""Authenticated inventory is fenced by persistent physical ownership evidence."""
from pathlib import Path
import tempfile
import unittest

from reproloop.live.access import AccessError
from reproloop.live.authority import HostAuthority
from reproloop.live.configuration import issue_bounded_project_grant
from reproloop.live.inventory import enrolled_inventory_document
from tests.test_live_authority import FakeClock
from tests import test_worker_recovery as recovery_tests

document = recovery_tests.document


class InventoryAuthorityTests(unittest.TestCase):
    setUp = recovery_tests.EnrolledInventoryRecoveryTests.setUp
    enroll = recovery_tests.EnrolledInventoryRecoveryTests.enroll

    def _states(self, states):
        client, host = self.enroll("worker-a", "boot-a")
        for state in states:
            value = document(1, "boot-a", state=state or "available")
            if state is None:
                value["devices"] = []
            try:
                client.refresh_inventory(host["credential"], value)
            except AccessError as error:
                self.assertEqual(error.code, "device_reconciliation_required")
        return self.fixture.server.inventory.list_host("worker-a")[0]["state"]

    def test_quarantine_cannot_be_cleared_by_a_state_string(self):
        self.assertNotEqual(self._states(["quarantined", "available"]), "available")

    def test_missing_inventory_does_not_erase_quarantine(self):
        self.assertNotEqual(self._states(["quarantined", None, "available"]), "available")

    def test_recovering_is_not_a_recovery_receipt(self):
        self.assertNotEqual(self._states(["busy", "recovering", "available"]), "available")

    def test_unused_device_can_reconnect(self):
        self.assertEqual(self._states(["available", None, "available"]), "available")

    def _authority(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        authority = HostAuthority(root / "authority.sqlite3", clock=FakeClock(),
                                  lease_directory=root / "leases")
        self.addCleanup(authority.close)
        return authority

    def _document(self, authority, state="available"):
        return enrolled_inventory_document([{
            "id": "phone-a", "state": state,
            "_authority": {"deviceKind": "android", "physicalId": "owned-phone"},
        }], generation=1, incarnation="boot-a", authority=authority)

    def _claim(self, authority, name="helper-a"):
        return authority.claim_device(
            device_kind="android", physical_id="owned-phone",
            helper_incarnation=name,
            parent_grant=issue_bounded_project_grant(
                authority, "checkout", lifetime_seconds=60))

    def test_actual_g1_release_allows_the_held_device_to_be_reused(self):
        authority = self._authority()
        client, host = self.enroll("worker-a", "boot-a")
        claim = self._claim(authority)
        client.refresh_inventory(host["credential"], self._document(authority, "busy"))
        self.assertTrue(claim.close())
        value = client.refresh_inventory(host["credential"], self._document(authority))
        self.assertEqual(value["devices"][0]["state"], "available")

    def test_old_release_cannot_clear_a_new_ownership_generation(self):
        authority = self._authority()
        client, host = self.enroll("worker-a", "boot-a")
        first = self._claim(authority)
        self.assertTrue(first.close())
        old = self._document(authority)
        client.refresh_inventory(host["credential"], old)
        second = self._claim(authority, "helper-b")
        client.refresh_inventory(host["credential"], self._document(authority, "busy"))
        with self.assertRaises(AccessError) as stale:
            client.refresh_inventory(host["credential"], old)
        self.assertEqual(stale.exception.code, "device_reconciliation_required")
        self.assertNotEqual(self.fixture.server.inventory.list_host("worker-a")[0]["state"],
                            "available")
        self.assertTrue(second.close())
        value = client.refresh_inventory(host["credential"], self._document(authority))
        self.assertEqual(value["devices"][0]["state"], "available")

    def test_g1_unknown_requires_reconciliation_then_release(self):
        authority = self._authority()
        client, host = self.enroll("worker-a", "boot-a")
        claim = self._claim(authority)
        admission = claim.admit_operation(operation_id="operation-a", payload_digest="a" * 64,
                                          session_id="session-a", sequence=1)
        claim.prepare_dispatch(admission, provider_incarnation="provider-a")
        self.assertFalse(claim.close())
        client.refresh_inventory(host["credential"], self._document(authority, "quarantined"))
        recovered = self._claim(authority, "helper-b")
        snapshot = recovered.recovery_snapshot()
        disposition = authority.record_operation_disposition(
            snapshot, operation_id="operation-a", terminal_status="rejected",
            result_digest="b" * 64, evidence_digest="c" * 64)
        proof = authority.record_reconciliation(
            snapshot, dispositions=[disposition], prior_helper_exit_digest="d" * 64,
            pointer_cleanup_digest="e" * 64, fresh_helper_incarnation="helper-c",
            fresh_handshake_digest="f" * 64)
        recovered.reconcile(proof, parent_grant=issue_bounded_project_grant(
            authority, "checkout", lifetime_seconds=60))
        self.assertTrue(recovered.close())
        value = client.refresh_inventory(host["credential"], self._document(authority))
        self.assertEqual(value["devices"][0]["state"], "available")
