import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from reproof.core import ContractError
from reproof.live.access import AccessError, AccessStore
from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.configuration import compose_shared_access, load_shared_configuration
from reproof.live.model import Lab
from reproof.live.providers import demo_device
from tests.test_clock_sync import FakeClock
from tests.test_fixture_allocations import collection_policy, project_document


class ReleaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_unknown_namespace_and_newer_writer_fail_without_rewriting(self):
        namespace = self.root / "coordinator-v2"
        namespace.mkdir()
        marker = namespace / "namespace.json"
        marker.write_text('{"schemaVersion":999,"service":"foreign"}')
        before = marker.read_bytes()
        with self.assertRaises(ContractError):
            AccessStore(namespace)
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(list(namespace.iterdir()), [marker])

        other = self.root / "newer"
        store = AccessStore(other)
        store.close()
        database = other / "access.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
        finally:
            connection.close()
        before_digest = hashlib.sha256(database.read_bytes()).hexdigest()
        with self.assertRaises(ContractError):
            AccessStore(other)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before_digest)

    def test_legacy_adoption_preserves_bytes_and_never_imports_live_state(self):
        source = self.root / "legacy-result.json"
        original = b'{"status":"verified","grant":{"token":"historical"}}\n'
        source.write_bytes(original)
        store = AccessStore(self.root / "coordinator-v2")
        try:
            adopted = store.adopt_legacy(
                "legacy-one", source, original_format="live-result-v1",
                meaning="legacy-result-only")
            self.assertEqual(source.read_bytes(), original)
            copied = Path(adopted["preservedPath"])
            self.assertEqual(copied.read_bytes(), original)
            self.assertEqual(adopted["originalDigest"], hashlib.sha256(original).hexdigest())
            self.assertEqual(adopted["meaning"], "legacy-result-only")
            self.assertEqual(store.list_hosts(), [])
            self.assertEqual(store.list_credentials(), [])
            again = store.adopt_legacy(
                "legacy-one", source, original_format="live-result-v1",
                meaning="legacy-result-only")
            self.assertEqual(again["originalDigest"], adopted["originalDigest"])
        finally:
            store.close()

    def test_admin_cli_writes_secret_only_to_private_output_file(self):
        state = self.root / "coordinator-v2"
        administrator_credential = self.root / "administrator-credential.json"
        credential = self.root / "credential.json"
        init = subprocess.run(
            [sys.executable, "-m", "reproof", "live-admin", "init",
             "--state-root", str(state), "--administrator", "admin",
             "--lifetime-seconds", "300", "--output",
             str(administrator_credential)],
            text=True, capture_output=True, timeout=10)
        self.assertEqual(init.returncode, 0, init.stderr)
        administrator_secret = json.loads(
            administrator_credential.read_text())["credential"]
        self.assertNotIn(administrator_secret, init.stdout)
        self.assertNotIn(administrator_secret, init.stderr)
        self.assertEqual(
            os.stat(administrator_credential).st_mode & 0o777, 0o600)
        issue = subprocess.run(
            [sys.executable, "-m", "reproof", "live-admin", "credential-issue",
             "--state-root", str(state), "--credential-stdin", "--identity", "admin",
             "--lifetime-seconds", "300", "--output", str(credential)],
            input=administrator_credential.read_text(), text=True,
            capture_output=True, timeout=10)
        self.assertEqual(issue.returncode, 0, issue.stderr)
        secret = json.loads(credential.read_text())["credential"]
        self.assertNotIn(secret, issue.stdout)
        self.assertNotIn(secret, issue.stderr)
        self.assertEqual(os.stat(credential).st_mode & 0o777, 0o600)

        store = AccessStore(state)
        try:
            before = len(store.list_credentials())
        finally:
            store.close()
        refused = subprocess.run(
            [sys.executable, "-m", "reproof", "live-admin", "credential-issue",
             "--state-root", str(state), "--credential-stdin", "--identity", "admin",
             "--lifetime-seconds", "300", "--output", str(credential)],
            input=administrator_credential.read_text(), text=True,
            capture_output=True, timeout=10)
        self.assertEqual(refused.returncode, 2)
        store = AccessStore(state)
        try:
            self.assertEqual(len(store.list_credentials()), before)
        finally:
            store.close()

    def test_failed_admin_bootstrap_does_not_close_bootstrap_or_leave_output(self):
        state = self.root / "coordinator-v2"
        output = self.root / "invalid-bootstrap.json"
        refused = subprocess.run(
            [sys.executable, "-m", "reproof", "live-admin", "init",
             "--state-root", str(state), "--administrator", "admin",
             "--lifetime-seconds", "1", "--output", str(output)],
            text=True, capture_output=True, timeout=10)
        self.assertEqual(refused.returncode, 2)
        self.assertFalse(output.exists())
        store = AccessStore(state)
        try:
            identity, issued = store.bootstrap_administrator_credential(
                "admin", lifetime_seconds=60)
            self.assertTrue(identity["administrator"])
            self.assertTrue(issued["token"].startswith("rpa."))
        finally:
            store.close()

    def test_admin_cli_provisions_and_revokes_a_host_scoped_assignment(self):
        state = self.root / "coordinator-v2"
        admin_file = self.root / "admin.json"
        project_file = self.root / "project.json"
        enrollment_file = self.root / "enrollment.json"
        project_file.write_text(json.dumps(project_document()))

        def execute(*arguments):
            result = subprocess.run(
                [sys.executable, "-m", "reproof", "live-admin", *arguments,
                 "--state-root", str(state), "--credential-stdin"],
                input=admin_file.read_text(), text=True,
                capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn(json.loads(admin_file.read_text())["credential"],
                             result.stdout + result.stderr)
            return result

        initialized = subprocess.run(
            [sys.executable, "-m", "reproof", "live-admin", "init",
             "--state-root", str(state), "--administrator", "admin",
             "--lifetime-seconds", "300", "--output", str(admin_file)],
            text=True, capture_output=True, timeout=10)
        self.assertEqual(initialized.returncode, 0)
        execute("project-register", "--project", str(project_file))
        execute("identity-add", "--identity", "operator")
        execute("membership-grant", "--project", "checkout",
                "--identity", "operator", "--role", "operator")
        enrolled = execute(
            "host-enrollment-create", "--host-id", "mac-one",
            "--project", "checkout", "--trust-group", "qa",
            "--lifetime-seconds", "300", "--credential-lifetime-seconds", "300",
            "--output", str(enrollment_file))
        enrollment_token = json.loads(enrollment_file.read_text())["credential"]
        self.assertNotIn(enrollment_token, enrolled.stdout + enrolled.stderr)
        store = AccessStore(state)
        try:
            host = store.consume_host_enrollment(
                enrollment_token, host_id="mac-one", incarnation="boot-one")
        finally:
            store.close()
        execute("device-assign", "--device-id", "mac-one--demo",
                "--project", "checkout", "--host-id", "mac-one")
        execute("host-revoke", "--host-id", "mac-one")
        store = AccessStore(state)
        try:
            self.assertEqual(
                store.device_assignment("mac-one--demo")["hostGeneration"],
                host["generation"])
            with self.assertRaises(AccessError):
                store.assignment_project_ids("mac-one--demo")
        finally:
            store.close()

    def test_shared_configuration_is_strict_public_and_requires_remote_tls(self):
        config = self.root / "shared.json"
        value = {
            "schemaVersion": 2,
            "kind": "reproof-shared-coordinator",
            "stateRoot": str(self.root / "coordinator-v2"),
            "listen": {
                "host": "127.0.0.1", "port": 9443,
                "origin": "http://127.0.0.1:9443",
                "tlsCertificateFile": None, "tlsPrivateKeyFile": None,
            },
            "projects": [{
                "projectFile": str(self.root / "project.json"),
                "collectionPolicyFile": str(self.root / "policy.json"),
            }],
            "browserSessionSeconds": 900,
        }
        config.write_text(json.dumps(value))
        loaded = load_shared_configuration(config)
        public = loaded.public()
        self.assertEqual(public["origin"], "http://127.0.0.1:9443")
        encoded = json.dumps(public)
        self.assertNotIn("stateRoot", encoded)
        self.assertNotIn("tls", encoded.lower())

        value["listen"].update(
            host="192.0.2.10", origin="http://192.0.2.10:9443")
        config.write_text(json.dumps(value))
        with self.assertRaises(ContractError):
            load_shared_configuration(config)

        value["listen"].update(
            origin="https://192.0.2.10:9443",
            tlsCertificateFile="/service/tls/coordinator.crt",
            tlsPrivateKeyFile="/service/tls/coordinator.key")
        config.write_text(json.dumps(value))
        self.assertEqual(load_shared_configuration(config).host, "192.0.2.10")

    def test_shared_composition_rejects_unregistered_project_before_lab_mutation(self):
        project = self.root / "project.json"
        policy = self.root / "policy.json"
        project.write_text(json.dumps(project_document()))
        policy.write_text(json.dumps(collection_policy()))
        config = self.root / "shared.json"
        config.write_text(json.dumps({
            "schemaVersion": 2,
            "kind": "reproof-shared-coordinator",
            "stateRoot": str(self.root / "coordinator-v2"),
            "listen": {
                "host": "127.0.0.1", "port": 9443,
                "origin": "http://127.0.0.1:9443",
                "tlsCertificateFile": None, "tlsPrivateKeyFile": None,
            },
            "projects": [{"projectFile": str(project),
                          "collectionPolicyFile": str(policy)}],
            "browserSessionSeconds": 900,
        }))
        store = AccessStore(self.root / "coordinator-v2")
        store.bootstrap_administrator("admin")
        lab = Lab(
            [demo_device()], self.root / "lab",
            recording_clock_sync=ClockSynchronizer(FakeClock()))
        try:
            with self.assertRaises(ContractError):
                compose_shared_access(
                    lab, load_shared_configuration(config), store=store)
            self.assertIsNone(lab._recording_store)
            store.register_project("admin", project_document())
            store.assign_device("admin", "demo", project_id="checkout")
            policy.write_text('{"unknown":true}')
            with self.assertRaises(ContractError):
                compose_shared_access(
                    lab, load_shared_configuration(config), store=store)
            self.assertIsNone(lab._recording_store)
            policy.write_text(json.dumps(collection_policy()))
            controller, registrations = compose_shared_access(
                lab, load_shared_configuration(config), store=store)
            self.assertEqual(len(registrations), 1)
            self.assertEqual(controller.registration(
                "checkout").project["id"], "checkout")
        finally:
            lab.close_all()
            store.close()


if __name__ == "__main__":
    unittest.main()
