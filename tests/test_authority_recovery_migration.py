"""Frozen v1 authority journals retain recovery history through schema migration."""
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from reproloop.core import ContractError
from reproloop.live.authority import HostAuthority, ProviderResult
from reproloop.live.state_store import FORMAT_VERSION, StateStore
from tests.test_live_authority import AuthorityTestCase, DIGEST_A, DIGEST_B, DIGEST_C


class AuthorityRecoveryMigrationTests(AuthorityTestCase):
    def legacy_journal(self, *, version=1, terminal_status="rejected"):
        device = self.claim()
        first = device.admit_operation(
            operation_id="operation-old", payload_digest=DIGEST_A,
            session_id="session-one", sequence=1,
        )
        permit = device.prepare_dispatch(first, provider_incarnation="provider-one")
        device.confirm_operation(permit, ProviderResult("receipt-old", "unknown", DIGEST_A))
        snapshot = device.recovery_snapshot()
        disposition = self.authority.record_operation_disposition(
            snapshot, operation_id="operation-old", terminal_status=terminal_status,
            result_digest=DIGEST_B, evidence_digest=DIGEST_C,
        )
        reconciliation = self.authority.record_reconciliation(
            snapshot, dispositions=[disposition], prior_helper_exit_digest=DIGEST_A,
            pointer_cleanup_digest=DIGEST_B, fresh_helper_incarnation="helper-two",
            fresh_handshake_digest=DIGEST_C,
        )
        device.reconcile(reconciliation, parent_grant=self.grant(grant_id="recovery-grant"))
        pending = device.admit_operation(
            operation_id="operation-uncertain", payload_digest=DIGEST_C,
            session_id="session-one", sequence=1,
        )
        device.admit_operation(
            operation_id="operation-queued", payload_digest=DIGEST_B,
            session_id="session-one", sequence=2,
        )
        permit = device.prepare_dispatch(pending, provider_incarnation="provider-two")
        device.confirm_operation(permit, ProviderResult("receipt-pending", "unknown", DIGEST_C))
        self.authority.record_legacy_adoption(
            adoption_id="legacy-one", artifact_digest=DIGEST_A, semantics="unverified-history",
        )
        expected = self.rows(self.state_path)
        path = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                (Path(__file__).parent / f"fixtures/authority-store-v{version}.sql").read_text()
            )
            # This import order satisfies all foreign keys in the frozen schema.
            connection.execute("PRAGMA foreign_keys = ON")
            for table in ("devices", "replay_watermarks", "operations", "operation_receipts",
                          "reconciliations", "reconciliation_dispositions", "legacy_adoptions"):
                for row in expected[table]:
                    placeholders = ",".join("?" for _ in row)
                    connection.execute(f"INSERT INTO {table} VALUES ({placeholders})", row)
            connection.commit()
        return path, expected

    @staticmethod
    def rows(path):
        with closing(sqlite3.connect(path)) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'authority_metadata' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
            return {table: list(connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2"))
                    for table in tables}

    def test_migration_preserves_pending_operations_receipts_and_reconciliation_history(self):
        path, expected = self.legacy_journal()
        with closing(StateStore(path)) as store:
            self.assertEqual(FORMAT_VERSION, 3)
            self.assertEqual(self.rows(path), expected)
            fingerprint = store.operation("operation-uncertain")["device_fingerprint"]
            device, unfinished = store.recovery_state(fingerprint)
            self.assertEqual(device["generation"], 2)
            self.assertEqual([row["status"] for row in unfinished], ["uncertain", "queued"])
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], FORMAT_VERSION)
            self.assertEqual(list(connection.execute("PRAGMA foreign_key_check")), [])
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM operations WHERE operation_id = 'operation-old'")
        with closing(StateStore(path)):
            self.assertEqual(self.rows(path), expected)

    def test_migrated_journal_can_recover_uncertain_work_without_rewriting_older_history(self):
        path, expected = self.legacy_journal()
        self.authority.close()
        # Reproduce an upgrade at the original authority root, whose canonical
        # device lease deliberately refuses a different journal path.
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(self.state_path)) as target:
            source.backup(target)
        path = self.state_path
        self.authority = HostAuthority(path, clock=self.clock, lease_directory=self.lease_directory)
        device = self.claim(parent_grant=self.grant(grant_id="restart-grant"))
        snapshot = device.recovery_snapshot()
        dispositions = [self.authority.record_operation_disposition(
            snapshot, operation_id=operation_id,
            terminal_status="recovered" if status == "uncertain" else "not-dispatched",
            result_digest=DIGEST_B, evidence_digest=DIGEST_C,
        ) for operation_id, status in snapshot.operations]
        reconciliation = self.authority.record_reconciliation(
            snapshot, dispositions=dispositions, prior_helper_exit_digest=DIGEST_A,
            pointer_cleanup_digest=DIGEST_B, fresh_helper_incarnation="helper-three",
            fresh_handshake_digest=DIGEST_C,
        )
        device.reconcile(reconciliation, parent_grant=self.grant(grant_id="second-recovery-grant"))
        self.assertEqual(device.generation, 3)
        self.assertEqual(self.authority.store.operation("operation-uncertain")["status"], "recovered")
        self.assertIsNone(self.authority.store.operation("operation-uncertain")["result_digest"])
        self.assertEqual(self.authority.store.operation("operation-queued")["status"], "rejected")
        actual = self.rows(path)
        for table in ("operation_receipts", "replay_watermarks", "legacy_adoptions"):
            self.assertEqual(actual[table], expected[table])
        for table in ("reconciliations", "reconciliation_dispositions"):
            for row in expected[table]:
                self.assertIn(row, actual[table])
        old_operation = next(row for row in expected["operations"] if row[0] == "operation-old")
        self.assertIn(old_operation, actual["operations"])

    def test_failure_after_table_rebuild_rolls_back_schema_metadata_and_history(self):
        path, expected = self.legacy_journal()
        migrate = StateStore._migrate_v1
        def interrupted(store, connection):
            migrate(store, connection)
            raise sqlite3.OperationalError("injected migration interruption")
        with patch.object(StateStore, "_migrate_v1", interrupted):
            with self.assertRaises(ContractError):
                StateStore(path)
        self.assertEqual(self.rows(path), expected)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(dict(connection.execute("SELECT key, value FROM authority_metadata")), {
                "format_version": "1", "minimum_reader_version": "1", "minimum_writer_version": "1",
            })
        with closing(StateStore(path)):
            self.assertEqual(self.rows(path), expected)

    def test_process_exit_during_migration_keeps_v1_journal_reopenable(self):
        path, expected = self.legacy_journal()
        script = """
import os
import sys
from reproloop.live.state_store import StateStore
class InterruptedStore(StateStore):
    def _migrate_v1(self, connection):
        super()._migrate_v1(connection)
        os._exit(71)
InterruptedStore(sys.argv[1])
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
        )
        self.assertEqual(completed.returncode, 71, completed.stderr.decode())
        self.assertEqual(self.rows(path), expected)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(list(connection.execute("PRAGMA foreign_key_check")), [])
        with closing(StateStore(path)):
            self.assertEqual(self.rows(path), expected)

    def test_unknown_legacy_writer_requirement_is_rejected_without_migration(self):
        path, expected = self.legacy_journal()
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("UPDATE authority_metadata SET value = '99' WHERE key = 'minimum_writer_version'")
            connection.commit()
        with self.assertRaisesRegex(ContractError, "Authority store version is incompatible"):
            StateStore(path)
        self.assertEqual(self.rows(path), expected)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_custom_legacy_schema_is_preserved_and_refused_before_rebuild(self):
        path, expected = self.legacy_journal()
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE INDEX operator_history_index ON operations(status)")
            connection.commit()
        with self.assertRaisesRegex(ContractError, "Authority store schema is incompatible"):
            with closing(StateStore(path)):
                pass
        self.assertEqual(self.rows(path), expected)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNotNone(connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'operator_history_index'"
            ).fetchone())

    def test_broken_legacy_foreign_keys_are_rejected_before_rebuild(self):
        path, _ = self.legacy_journal()
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("DELETE FROM operations WHERE operation_id = 'operation-old'")
            connection.commit()
        before = self.rows(path)
        with self.assertRaisesRegex(ContractError, "Authority store references are incompatible"):
            with closing(StateStore(path)):
                pass
        self.assertEqual(self.rows(path), before)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_v2_recovered_history_migrates_with_a_reader_and_writer_barrier(self):
        path, expected = self.legacy_journal(version=2, terminal_status="recovered")
        with closing(StateStore(path)):
            self.assertEqual(self.rows(path), expected)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(dict(connection.execute("SELECT key, value FROM authority_metadata")), {
                "format_version": "3", "minimum_reader_version": "3", "minimum_writer_version": "3",
            })
            self.assertEqual(list(connection.execute("PRAGMA foreign_key_check")), [])

    def test_v2_migration_interruption_preserves_its_original_version_and_history(self):
        path, expected = self.legacy_journal(version=2, terminal_status="recovered")
        migrate = StateStore._migrate_v2
        def interrupted(store, connection):
            migrate(store, connection)
            raise sqlite3.OperationalError("owned migration interruption")
        with patch.object(StateStore, "_migrate_v2", interrupted):
            with self.assertRaises(ContractError):
                with closing(StateStore(path)):
                    pass
        self.assertEqual(self.rows(path), expected)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(dict(connection.execute("SELECT key, value FROM authority_metadata")), {
                "format_version": "2", "minimum_reader_version": "2", "minimum_writer_version": "2",
            })
