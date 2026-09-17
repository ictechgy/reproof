import multiprocessing
from pathlib import Path
import tempfile
import unittest

from reproloop.core import ContractError
from reproloop.live.authority import HostAuthority


def _real_grant(authority, grant_id="parent-grant"):
    received = authority.clock_sync.sample()
    sent = authority.clock_sync.sample()
    mapping = authority.clock_sync.record_exchange(
        coordinator_clock_id="coordinator-clock",
        coordinator_send_ns=received.nanoseconds - 10_000,
        host_received=received,
        host_sent=sent,
        coordinator_receive_ns=sent.nanoseconds + 10_000,
        max_drift_ppm=100,
    )
    return authority.issue_parent_grant(
        mapping,
        grant_id=grant_id,
        project_id="project-one",
        controller_id="controller-one",
        renewal_sequence=1,
        coordinator_deadline_ns=sent.nanoseconds + 5_000_000_000,
    )


def _contending_process(state_path, lease_directory, result):
    authority = HostAuthority(Path(state_path), lease_directory=Path(lease_directory))
    try:
        authority.claim_device(
            device_kind="ios-physical",
            physical_id="synthetic-physical-device",
            display_alias="different-alias",
            helper_incarnation="helper-child",
            parent_grant=_real_grant(authority, "child-grant"),
        )
    except ContractError:
        result.put("blocked")
    else:
        result.put("incorrectly-acquired")
    finally:
        authority.close()


def _crashing_owner(state_path, lease_directory, ready):
    authority = HostAuthority(Path(state_path), lease_directory=Path(lease_directory))
    device = authority.claim_device(
        device_kind="android",
        physical_id="synthetic-crash-device",
        helper_incarnation="helper-before-crash",
        parent_grant=_real_grant(authority),
    )
    device.admit_operation(
        operation_id="operation-before-crash",
        payload_digest="a" * 64,
        session_id="session-one",
        sequence=1,
    )
    ready.set()
    # Deliberately skip Python cleanup to model host-process loss.  The kernel
    # releases the canonical flock; durable ownership must remain quarantined.
    import os
    os._exit(0)


class AuthorityProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.lease_directory = self.root / "legacy-leases"
        self.context = multiprocessing.get_context("spawn")

    def tearDown(self):
        self.temporary.cleanup()

    def test_existing_canonical_lock_blocks_other_process_and_state_root(self):
        authority = HostAuthority(
            self.root / "parent-state" / "state.sqlite3",
            lease_directory=self.lease_directory,
        )
        device = authority.claim_device(
            device_kind="ios-physical",
            physical_id="synthetic-physical-device",
            display_alias="first-alias",
            helper_incarnation="helper-parent",
            parent_grant=_real_grant(authority),
        )
        result = self.context.Queue()
        child = self.context.Process(
            target=_contending_process,
            args=(str(self.root / "child-state" / "state.sqlite3"),
                  str(self.lease_directory), result),
        )
        try:
            child.start()
            child.join(10)
            self.assertEqual(child.exitcode, 0)
            self.assertEqual(result.get(timeout=1), "blocked")
        finally:
            if child.is_alive():
                child.terminate()
                child.join(2)
            device.close()
            authority.close()
            result.close()

    def test_process_crash_releases_flock_but_does_not_restore_dispatch(self):
        state_path = self.root / "shared-state" / "state.sqlite3"
        ready = self.context.Event()
        child = self.context.Process(
            target=_crashing_owner,
            args=(str(state_path), str(self.lease_directory), ready),
        )
        child.start()
        self.assertTrue(ready.wait(10))
        child.join(10)
        self.assertEqual(child.exitcode, 0)

        authority = HostAuthority(state_path, lease_directory=self.lease_directory)
        device = authority.claim_device(
            device_kind="android",
            physical_id="synthetic-crash-device",
            helper_incarnation="helper-after-crash",
            parent_grant=_real_grant(authority, "after-crash-grant"),
        )
        try:
            self.assertEqual(device.status, "quarantined")
            self.assertTrue(device.requires_reconciliation)
            with self.assertRaisesRegex(ContractError, "Device requires reconciliation"):
                device.admit_operation(
                    operation_id="operation-after-crash",
                    payload_digest="b" * 64,
                    session_id="session-two",
                    sequence=1,
                )
        finally:
            device.close()
            authority.close()


if __name__ == "__main__":
    unittest.main()
