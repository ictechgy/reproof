import multiprocessing
from pathlib import Path
import tempfile
import unittest

from reproloop.core import ContractError
from reproloop.live.access import AccessError, AccessStore
from reproloop.live.authority import HostAuthority
from reproloop.live.clock_sync import ClockReading
from reproloop.live.configuration import issue_bounded_project_grant
from tests.test_fixture_allocations import project_document


def consume_once(root, token, barrier, output):
    store = AccessStore(root)
    try:
        barrier.wait(timeout=5)
        value = store.consume_host_enrollment(
            token, host_id="mac-one", incarnation="boot-one")
        output.put(("ok", value["generation"]))
    except Exception as error:
        output.put(("error", getattr(error, "code", type(error).__name__)))
    finally:
        store.close()


class AuthorityClock:
    def __init__(self):
        self.now = 1_000_000_000

    def read(self):
        return ClockReading("g5-authority-clock", "d" * 64, self.now, 0)


class HostEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "coordinator-v2"
        self.store = AccessStore(self.root)
        self.store.bootstrap_administrator("admin")
        self.store.register_project("admin", project_document())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enrollment(self, **overrides):
        values = dict(host_id="mac-one", project_ids=["checkout"], trust_groups=["qa"],
                      lifetime_seconds=300, credential_lifetime_seconds=600)
        values.update(overrides)
        return self.store.create_host_enrollment("admin", **values)

    def test_one_time_enrollment_is_consumed_atomically_across_processes(self):
        token = self.enrollment()["token"]
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        output = context.Queue()
        processes = [context.Process(
            target=consume_once, args=(self.root, token, barrier, output)) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        results = [output.get(timeout=2), output.get(timeout=2)]
        self.assertEqual(sum(item[0] == "ok" for item in results), 1)
        self.assertEqual(sum(item[0] == "error" for item in results), 1)

    def test_replay_expiry_scope_and_stale_host_credentials_fail(self):
        issued = self.enrollment()
        enrolled = self.store.consume_host_enrollment(
            issued["token"], host_id="mac-one", incarnation="boot-one")
        host = self.store.authenticate_host(enrolled["credential"])
        self.assertEqual(host.project_ids, ("checkout",))
        self.store.authorize_host(host, project_id="checkout", trust_group="qa")
        with self.assertRaises(AccessError):
            self.store.consume_host_enrollment(
                issued["token"], host_id="mac-one", incarnation="boot-one")
        with self.assertRaises(AccessError):
            self.store.authorize_host(host, project_id="unknown", trust_group="qa")

        self.store.revoke_host("admin", "mac-one")
        with self.assertRaises(AccessError):
            self.store.authenticate_host(enrolled["credential"])
        replacement = self.enrollment()
        current = self.store.consume_host_enrollment(
            replacement["token"], host_id="mac-one", incarnation="boot-two")
        self.assertEqual(current["generation"], enrolled["generation"] + 1)
        with self.assertRaises(AccessError):
            self.store.authenticate_host(
                current["credential"], expected_incarnation="boot-one")

    def test_expired_enrollment_and_wrong_host_do_not_consume_it(self):
        clock = [1_000]
        self.store.close()
        self.store = AccessStore(self.root, clock=lambda: clock[0])
        issued = self.enrollment(lifetime_seconds=60)
        with self.assertRaises(AccessError):
            self.store.consume_host_enrollment(
                issued["token"], host_id="other-host", incarnation="boot")
        clock[0] += 61
        with self.assertRaises(AccessError):
            self.store.consume_host_enrollment(
                issued["token"], host_id="mac-one", incarnation="boot")
        replacement = self.enrollment(lifetime_seconds=60)
        self.assertEqual(replacement["generation"], issued["generation"] + 1)

    def test_unconsumed_enrollment_requires_explicit_revocation_before_reissue(self):
        issued = self.enrollment()
        with self.assertRaises(ContractError):
            self.enrollment()
        self.store.revoke_host_enrollment("admin", issued["enrollmentId"])
        with self.assertRaises(AccessError) as raised:
            self.store.consume_host_enrollment(
                issued["token"], host_id="mac-one", incarnation="boot-one")
        self.assertEqual(raised.exception.code, "enrollment_revoked")
        replacement = self.enrollment()
        self.assertEqual(replacement["generation"], issued["generation"] + 1)

    def test_host_revocation_also_revokes_a_pending_replacement(self):
        first = self.enrollment(credential_lifetime_seconds=60)
        enrolled = self.store.consume_host_enrollment(
            first["token"], host_id="mac-one", incarnation="boot-one")
        clock = [enrolled["expiresAt"] + 1]
        self.store.close()
        self.store = AccessStore(self.root, clock=lambda: clock[0])
        replacement = self.enrollment()
        self.store.revoke_host("admin", "mac-one")
        with self.assertRaises(AccessError) as raised:
            self.store.consume_host_enrollment(
                replacement["token"], host_id="mac-one", incarnation="boot-two")
        self.assertEqual(raised.exception.code, "enrollment_revoked")

    def test_device_assignment_cannot_exceed_enrolled_host_scope(self):
        issued = self.enrollment(trust_groups=[])
        enrolled = self.store.consume_host_enrollment(
            issued["token"], host_id="mac-one", incarnation="boot-one")
        with self.assertRaises(ContractError):
            self.store.assign_device(
                "admin", "host-device", trust_group="qa", host_id="mac-one")
        assignment = self.store.assign_device(
            "admin", "host-device", project_id="checkout", host_id="mac-one")
        self.assertEqual(assignment["hostId"], "mac-one")
        self.assertEqual(assignment["projectId"], "checkout")
        self.assertTrue(enrolled["credential"].startswith("rph."))
        self.assertEqual(
            self.store.assignment_project_ids("host-device"), ("checkout",))
        self.store.revoke_host("admin", "mac-one")
        with self.assertRaises(AccessError):
            self.store.assignment_project_ids("host-device")
        replacement = self.enrollment(trust_groups=[])
        current = self.store.consume_host_enrollment(
            replacement["token"], host_id="mac-one", incarnation="boot-two")
        self.assertEqual(current["generation"], enrolled["generation"] + 1)
        with self.assertRaises(ContractError):
            self.store.assign_device(
                "admin", "host-device", project_id="checkout", host_id="mac-one")
        receipt = {
            "schemaVersion": 1,
            "kind": "device-sanitation-receipt",
            "deviceId": "host-device",
            "previousAssignment": {
                "projectId": "checkout", "trustGroup": None,
                "hostId": "mac-one", "hostGeneration": 1,
                "hostIncarnation": "boot-one",
            },
            "completedAtMs": 1234,
            "limitations": [],
        }
        reassigned = self.store.assign_device(
            "admin", "host-device", project_id="checkout", host_id="mac-one",
            sanitation_receipt=receipt)
        self.assertEqual(reassigned["hostGeneration"], 2)
        self.assertEqual(reassigned["hostIncarnation"], "boot-two")

    def test_shared_composition_issues_only_a_bounded_g1_parent_grant(self):
        clock = AuthorityClock()
        authority = HostAuthority(
            Path(self.temp.name) / "host-authority-v1" / "authority.sqlite3",
            clock=clock,
            lease_directory=Path(self.temp.name) / "authority-leases")
        try:
            grant = issue_bounded_project_grant(
                authority, "checkout", lifetime_seconds=10)
            self.assertEqual(grant.project_id, "checkout")
            authority._require_parent_grant(grant)
            clock.now += 10_000_000_000
            with self.assertRaises(ContractError):
                authority._require_parent_grant(grant)
        finally:
            authority.close()


if __name__ == "__main__":
    unittest.main()
