-- Frozen authority journal schema v2, before pending-final-cleanup semantics.
CREATE TABLE authority_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
CREATE TABLE devices (
                device_fingerprint TEXT PRIMARY KEY,
                device_kind TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                status TEXT NOT NULL CHECK (status IN ('owned', 'released', 'expired', 'quarantined')),
                host_incarnation TEXT NOT NULL,
                helper_incarnation TEXT NOT NULL,
                grant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                controller_id TEXT NOT NULL,
                renewal_sequence INTEGER NOT NULL CHECK (renewal_sequence >= 1),
                parent_deadline_ns INTEGER NOT NULL CHECK (parent_deadline_ns >= 0),
                mapping_id TEXT NOT NULL,
                quarantine_reason TEXT,
                updated_ns INTEGER NOT NULL CHECK (updated_ns >= 0)
            ) WITHOUT ROWID;
CREATE TABLE replay_watermarks (
                device_fingerprint TEXT NOT NULL REFERENCES devices(device_fingerprint),
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                highest_sequence INTEGER NOT NULL CHECK (highest_sequence >= 0),
                PRIMARY KEY (device_fingerprint, generation, controller_id)
            ) WITHOUT ROWID;
CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY,
                operation_fingerprint TEXT NOT NULL UNIQUE,
                device_fingerprint TEXT NOT NULL REFERENCES devices(device_fingerprint),
                protocol_version INTEGER NOT NULL CHECK (protocol_version = 1),
                generation INTEGER NOT NULL CHECK (generation >= 1),
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                controller_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 1),
                payload_digest TEXT NOT NULL,
                host_incarnation TEXT NOT NULL,
                helper_incarnation TEXT NOT NULL,
                deadline_ns INTEGER NOT NULL CHECK (deadline_ns >= 0),
                admitted_ns INTEGER NOT NULL CHECK (admitted_ns >= 0),
                status TEXT NOT NULL CHECK (status IN ('queued', 'uncertain', 'succeeded', 'rejected', 'expired', 'recovered')),
                provider_incarnation TEXT,
                result_digest TEXT,
                terminal_ns INTEGER,
                UNIQUE (device_fingerprint, generation, controller_id, sequence)
            ) WITHOUT ROWID;
CREATE TABLE operation_receipts (
                receipt_id TEXT PRIMARY KEY,
                receipt_fingerprint TEXT NOT NULL UNIQUE,
                operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                generation INTEGER NOT NULL,
                provider_incarnation TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('succeeded', 'rejected', 'unknown')),
                result_digest TEXT NOT NULL,
                observed_ns INTEGER NOT NULL CHECK (observed_ns >= 0),
                binding TEXT NOT NULL CHECK (binding IN ('current', 'late'))
            ) WITHOUT ROWID;
CREATE TABLE reconciliations (
                reconciliation_id TEXT PRIMARY KEY,
                reconciliation_fingerprint TEXT NOT NULL UNIQUE,
                device_fingerprint TEXT NOT NULL REFERENCES devices(device_fingerprint),
                prior_generation INTEGER NOT NULL,
                prior_host_incarnation TEXT NOT NULL,
                prior_helper_incarnation TEXT NOT NULL,
                fresh_helper_incarnation TEXT NOT NULL,
                prior_helper_exit_digest TEXT NOT NULL,
                pointer_cleanup_digest TEXT NOT NULL,
                fresh_handshake_digest TEXT NOT NULL,
                observed_ns INTEGER NOT NULL CHECK (observed_ns >= 0)
            ) WITHOUT ROWID;
CREATE TABLE reconciliation_dispositions (
                reconciliation_id TEXT NOT NULL REFERENCES reconciliations(reconciliation_id),
                operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                terminal_status TEXT NOT NULL CHECK (terminal_status IN ('not-dispatched', 'succeeded', 'rejected', 'recovered')),
                result_digest TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                disposition_fingerprint TEXT NOT NULL,
                PRIMARY KEY (reconciliation_id, operation_id)
            ) WITHOUT ROWID;
CREATE TABLE legacy_adoptions (
                adoption_id TEXT PRIMARY KEY,
                artifact_digest TEXT NOT NULL,
                semantics TEXT NOT NULL CHECK (semantics IN ('lock-only', 'unverified-history')),
                adoption_fingerprint TEXT NOT NULL UNIQUE
            ) WITHOUT ROWID;
INSERT INTO authority_metadata VALUES ('format_version','2'), ('minimum_reader_version','2'), ('minimum_writer_version','2');
PRAGMA application_id = 1380994113;
PRAGMA user_version = 2;
