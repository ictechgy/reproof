"""Reconstructed portable controls. Native subprocesses are denied by default."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("startup_supervisor", ROOT / "scripts/ci/android-startup-one-shot.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class SupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.event = self.root / "event.json"
        self.payload = {"action": "opened", "repository": {"full_name": m.REPO}, "pull_request": {
            "head": {"ref": m.BRANCH, "repo": {"full_name": m.REPO}}}}
        self.event.write_text(json.dumps(self.payload))
        self.env = dict(CI="true", GITHUB_ACTIONS="true", RUNNER_ENVIRONMENT="github-hosted",
                        GITHUB_REPOSITORY=m.REPO, GITHUB_EVENT_NAME="pull_request", GITHUB_RUN_ATTEMPT="1",
                        GITHUB_RUN_ID="123", GITHUB_EVENT_PATH=str(self.event), RUNNER_TEMP=str(self.root))
        for attribute in ("run", "Popen"):
            blocker = patch.object(m.subprocess, attribute, side_effect=AssertionError("native execution forbidden"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def test_exact_opened_attempt_guard(self):
        self.assertEqual(m.guard(self.env)[1], "reproof-ci-123-1")
        for key, value in (("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_EVENT_NAME", "push"),
                           ("ADB_SERVER_SOCKET", "tcp:remote:5037"), ("ANDROID_SERIAL", "real-device"),
                           ("RUNNER_ENVIRONMENT", "self-hosted")):
            with self.subTest(key=key), self.assertRaises(m.Stop):
                m.guard(dict(self.env, **{key: value}))

    def test_reject_changed_event_and_fork(self):
        for action, branch, repo in (("synchronize", m.BRANCH, m.REPO), ("opened", "other", m.REPO),
                                    ("opened", m.BRANCH, "foreign/repo")):
            self.payload["action"] = action
            self.payload["pull_request"]["head"] = {"ref": branch, "repo": {"full_name": repo}}
            self.event.write_text(json.dumps(self.payload))
            with self.assertRaises(m.Stop):
                m.guard(self.env)

    def test_missing_kvm_stops_before_private_or_native_operations(self):
        private, evidence = self.root / "private", self.root / "evidence"
        with patch.object(m, "verify_source"), patch.object(m, "kvm_available", return_value=False):
            self.assertEqual(m.run(self.env, self.root, "reproof-ci-123-1", private, evidence), 1)
        self.assertFalse(private.exists())
        self.assertEqual(json.loads((evidence / "supervisor.json").read_text())["code"], "existing_kvm_access_required")

    def test_cleanup_without_process_cleans_owned_home_only(self):
        private = self.root / "private"
        (private / "android/avd").mkdir(parents=True)
        external = self.root / "unrelated"
        external.write_text("keep")
        result = m.cleanup(private, self.root / "evidence", "reproof-ci-123-1", self.env)
        self.assertTrue(result["confirmed"])
        self.assertFalse(private.exists())
        self.assertEqual(external.read_text(), "keep")

    def test_spawn_identity_uncertain_never_kills_or_claims_cleanup(self):
        private = self.root / "private"
        private.mkdir()
        m.save(private / "state.json", {"name": "reproof-ci-123-1", "spawnPending": True})
        with patch.object(m.os, "killpg", side_effect=AssertionError("must not kill")):
            result = m.cleanup(private, self.root / "evidence", "reproof-ci-123-1", self.env)
        self.assertFalse(result["confirmed"])
        self.assertTrue(private.exists())

    def test_identity_mismatch_does_not_signal_or_delete(self):
        private = self.root / "private"
        private.mkdir()
        m.save(private / "state.json", {"name": "reproof-ci-123-1", "pid": 1234, "start": "44"})
        with patch.object(m, "process_identity", return_value=("45", [b"unrelated"])), \
             patch.object(m.os, "killpg", side_effect=AssertionError("must not kill")):
            result = m.cleanup(private, self.root / "evidence", "reproof-ci-123-1", self.env)
        self.assertFalse(result["confirmed"])
        self.assertTrue(private.exists())

    def test_private_delete_failure_not_confirmed(self):
        private = self.root / "private"
        private.mkdir()
        with patch.object(m.shutil, "rmtree", side_effect=OSError("secret should not leak")):
            result = m.cleanup(private, self.root / "evidence", "reproof-ci-123-1", self.env)
        self.assertFalse(result["confirmed"])
        self.assertNotIn("secret", json.dumps(result))

    def test_build_overlay_copies_only_pinned_files_without_source_edits(self):
        source = self.root / "repo"
        (source / "android").mkdir(parents=True)
        (source / "scripts/ci").mkdir(parents=True)
        original = b'include(":live")\n'
        (source / "android/settings.gradle.kts").write_bytes(original)
        (source / "android/unreviewed.txt").write_text("not pinned")
        manifest = {"files": {"android/settings.gradle.kts": hashlib.sha256(original).hexdigest()}}
        (source / "scripts/ci/android-startup-source.json").write_text(json.dumps(manifest))
        private = self.root / "private"
        private.mkdir()
        with patch.object(m, "ROOT", source):
            build, provenance = m.prepare_build(private)
        self.assertEqual((source / "android/settings.gradle.kts").read_bytes(), original)
        self.assertEqual((build / "settings.gradle.kts").read_bytes(), original + m.SETTINGS_OVERLAY.encode())
        self.assertFalse((build / "unreviewed.txt").exists())
        self.assertEqual(provenance["settingsOverlaySha256"], hashlib.sha256(m.SETTINGS_OVERLAY.encode()).hexdigest())

    def test_tool_timeout_kills_only_its_group_and_reaps(self):
        fake = Mock(pid=4321)
        fake.communicate.side_effect = m.subprocess.TimeoutExpired("secret-command", 1)
        with patch.object(m.subprocess, "Popen", return_value=fake), patch.object(m.os, "killpg") as kill:
            with self.assertRaises(m.subprocess.TimeoutExpired):
                m.command(["fake-tool"], self.env, timeout=1)
        kill.assert_called_once_with(4321, m.signal.SIGKILL)
        fake.wait.assert_called_once_with(timeout=5)

    def test_main_exception_does_not_print_raw_error(self):
        output = io.StringIO()
        with patch.object(m, "guard", side_effect=ValueError("token-secret")), contextlib.redirect_stdout(output):
            self.assertEqual(m.main(["run"]), 1)
        self.assertNotIn("token-secret", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["code"], "preflight_or_cleanup_failed")

    def test_source_contains_no_permission_remediation_or_input(self):
        source = Path(m.__file__).read_text()
        for forbidden in ("setfacl", "chmod", "usermod", "udev", '"input"', '"tap"', '"swipe"', '"text"'):
            self.assertNotIn(forbidden, source)
        self.assertIn('"-accel", "on"', source)
        self.assertNotIn('"-accel", "off"', source)

if __name__ == "__main__":
    unittest.main()
