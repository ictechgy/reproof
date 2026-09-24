"""Real CLI recovery of owned signing journals; no signing keys are supplied."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from reproof import contracts
from reproof.android_signing_tools import build_android_signing_owner
from reproof.execution.journal import RunStore
from reproof.repair_android_signing import AndroidSigningIdentity
from reproof.repair_signing import SigningContext
from reproof.repair_signing_recovery import MIN_OPERATION_BYTES, SigningOperationStore
from tests.test_android_signing_tools import _actual_tools, JDK_HOME, APKSIGNER_JAR, CLANG


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(JDK_HOME.exists() and APKSIGNER_JAR.is_file() and CLANG.is_file(),
                     "cached Android signing build tools unavailable")
class AndroidSigningOperationsCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="repro-signing-cli-tools-")
        cls.addClassCleanup(temporary.cleanup)
        cls.tools_root = Path(temporary.name).resolve() / "tools"
        cls.built = build_android_signing_owner(cls.tools_root, _actual_tools(),
            cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 45)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="repro-signing-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        certificate = hashlib.sha256(str(self.root).encode()).hexdigest()
        self.identity = AndroidSigningIdentity("owned-key", "inventory",
            "com.example.inventory", certificate, ("v2", "v3"), ())
        self.scope = contracts.digest({"kind": "android-apk-signing",
                                       "certificateSha256": certificate})
        self.store = RunStore(self.root / "journal", environment_digest="e" * 64,
                              disk_limit=MIN_OPERATION_BYTES)
        self.operations = SigningOperationStore(self.store, self.scope, self.built.tools,
                                                self.identity, self.root / "owner")
        self.addCleanup(self.operations.close)
        self.context = SigningContext("owned-operation", "a" * 64, "b" * 64,
                                      "inventory", "c" * 64, "d" * 64, "f" * 64,
                                      "owned-nonce")
        self.request = "4" * 64
        with self.operations.admit(self.context, self.request, MIN_OPERATION_BYTES):
            pass
        self.config = {
            "schemaVersion": 1, "kind": "android-signing-owner-v1",
            "runStorePath": str(self.store.root), "environmentDigest": "e" * 64,
            "diskBudgetBytes": MIN_OPERATION_BYTES, "ownerRoot": str(self.operations.root),
            "toolsPath": str(self.tools_root), "toolsManifestSha256": self.built.manifest_digest,
            "definitionDigest": self.operations.definition_digest,
            "identity": {"referenceId": "owned-key", "applicationId": "inventory",
                "packageName": "com.example.inventory", "certificateSha256": certificate,
                "signatureSchemes": ["v2", "v3"], "permissions": []},
        }
        self.config_path = self.root / "signing.json"
        self.write_config(self.config)

    def write_config(self, value):
        self.config_path.write_text(json.dumps(value))
        self.config_path.chmod(0o600)

    def command(self, action, *arguments, path=None):
        selected = self.config_path if path is None else path
        result = subprocess.run([sys.executable, "-m", "reproof", "android-signing",
            action, "--config", str(selected), "--operation", self.context.operation_id,
            *map(str, arguments)], cwd=ROOT, capture_output=True, timeout=15)
        self.assertEqual(result.stderr, b"")
        self.assertNotIn(str(self.root).encode(), result.stdout)
        return result.returncode, json.loads(result.stdout)

    def recover(self, request=None):
        return self.command("recover", "--request-digest",
                            self.request if request is None else request)

    def assert_reserved(self):
        self.assertEqual(self.store.status(self.context.operation_id)["reservedBytes"],
                         MIN_OPERATION_BYTES)

    def test_status_is_read_only_and_recovery_finishes_only_as_failed(self):
        before = {str(path.relative_to(self.root)): (path.stat().st_ino, path.read_bytes())
                  for path in self.root.rglob("*") if path.is_file()}
        code, report = self.command("status")
        self.assertEqual(code, 0)
        self.assertEqual(report["operation"]["state"], "recovery-required")
        after = {str(path.relative_to(self.root)): (path.stat().st_ino, path.read_bytes())
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        code, report = self.recover()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "recovered")
        self.assertEqual(report["state"], "failed")
        self.assertEqual(report["reservedBytes"], 0)
        self.assertEqual(self.store.status(self.context.operation_id)["state"], "failed")
        code, repeated = self.recover()
        self.assertEqual(code, 0)
        self.assertEqual(repeated["status"], "already-terminal")

    def test_wrong_request_or_vm_shaped_cleanup_cannot_release_signing(self):
        code, _ = self.recover("5" * 64)
        self.assertEqual(code, 2)
        self.assert_reserved()
        (self.store.root / "runs" / self.context.operation_id / "termination.json").write_bytes(b"{}")
        code, _ = self.recover()
        self.assertEqual(code, 2)
        self.assert_reserved()

    def test_cancelled_work_stays_cancelled_after_recovery(self):
        self.store.cancel(self.context.operation_id, self.request)
        code, report = self.recover()
        self.assertEqual(code, 0)
        self.assertEqual(report["state"], "cancelled")
        self.assertEqual(report["reservedBytes"], 0)

    def test_invalid_cli_arguments_do_not_echo_potential_material(self):
        result = subprocess.run([sys.executable, "-m", "reproof", "android-signing",
            "status", "--config", str(self.config_path), "--operation", self.context.operation_id,
            "--password", "OwnedSecretMarker"], cwd=ROOT, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b"OwnedSecretMarker", result.stdout + result.stderr)

    def test_public_command_builds_pinned_tools_without_a_checkout_script(self):
        from reproof.android_signing_tools import load_android_signing_owner
        tools = _actual_tools()
        output = self.root / "public-tools"
        arguments = [sys.executable, "-m", "reproof", "android-signing", "build-tools",
                     "--output-new", str(output), "--jdk-home", str(tools.jdk_home)]
        for name in ("java", "javac", "jar", "clang", "apksigner_jar"):
            flag = name.replace("_", "-")
            arguments.extend(["--" + flag, str(getattr(tools, name)),
                              "--" + flag + "-sha256", getattr(tools, name + "_sha256")])
        result = subprocess.run(arguments, cwd=ROOT, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "built")
        loaded = load_android_signing_owner(output, report["manifestDigest"])
        self.assertEqual(loaded.definition_digest, report["ownerDefinitionDigest"])
        existing = subprocess.run(arguments, cwd=ROOT, capture_output=True, timeout=20)
        self.assertEqual(existing.returncode, 2)
        self.assertNotIn(str(self.root).encode(), existing.stdout + existing.stderr)

    def test_live_producer_is_reported_and_cannot_be_recovered(self):
        path = self.operations.operations / self.context.operation_id / "producer.lock"
        with path.open("r+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            code, report = self.command("status")
            self.assertEqual(code, 0)
            self.assertEqual(report["operation"]["state"], "producer-live")
            code, _ = self.recover()
            self.assertEqual(code, 2)
            self.assert_reserved()

    def test_missing_roots_do_not_create_empty_replacement_journals(self):
        changed = copy.deepcopy(self.config)
        changed["runStorePath"] = str(self.root / "never-created")
        self.write_config(changed)
        code, _ = self.command("status")
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "never-created").exists())
        self.assert_reserved()

    def test_extra_fields_duplicate_fields_and_secret_named_files_are_rejected(self):
        for key in ("callback", "qualified", "keystore", "password"):
            changed = copy.deepcopy(self.config)
            changed[key] = "OwnedSecretMarker"
            self.write_config(changed)
            code, report = self.recover()
            self.assertEqual(code, 2)
            self.assertNotIn("OwnedSecretMarker", json.dumps(report))
        self.config_path.write_text('{"schemaVersion":1,' + json.dumps(self.config)[1:])
        code, _ = self.recover()
        self.assertEqual(code, 2)
        code, _ = self.command("status", path=self.root / ".env")
        self.assertEqual(code, 2)
        self.assert_reserved()

    def test_changed_definition_and_wrong_scope_cannot_release_previous_work(self):
        for key, value in (("definitionDigest", "0" * 64),
                           ("environmentDigest", "0" * 64)):
            changed = copy.deepcopy(self.config)
            changed[key] = value
            self.write_config(changed)
            code, _ = self.recover()
            self.assertEqual(code, 2)
        changed = copy.deepcopy(self.config)
        changed["identity"]["certificateSha256"] = "0" * 64
        self.write_config(changed)
        code, _ = self.recover()
        self.assertEqual(code, 2)
        self.assert_reserved()

    def test_alias_paths_and_fifo_producer_fail_without_a_hang(self):
        linked = self.root / "linked-owner"
        linked.symlink_to(self.operations.root, target_is_directory=True)
        changed = copy.deepcopy(self.config)
        changed["ownerRoot"] = str(linked)
        self.write_config(changed)
        code, _ = self.recover()
        self.assertEqual(code, 2)
        self.write_config(self.config)
        producer = self.operations.operations / self.context.operation_id / "producer.lock"
        producer.rename(producer.with_suffix(".saved"))
        os.mkfifo(producer, 0o600)
        code, report = self.command("status")
        self.assertEqual(code, 0)
        self.assertEqual(report["operation"]["state"], "intent-orphan")
        code, _ = self.recover()
        self.assertEqual(code, 2)
        self.assert_reserved()


if __name__ == "__main__":
    unittest.main()
