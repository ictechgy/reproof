"""Portable fake/control tests. Never invoke native programs or real endpoints."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "scripts/android-startup-one-shot-qa.py"
spec = importlib.util.spec_from_file_location("startup_one_shot", PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

class StartupOneShotTest(unittest.TestCase):
    def test_bad_private_paths_have_no_output_temp_or_device_side_effects(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            private = root / "reproof-startup-private-reproof-ci-123-1"
            output = root / "reproof-startup-evidence/android"
            app = private / "android-build/runtime-fixture/build/outputs/apk/debug/runtime-fixture-debug.apk"
            helper = private / "android-build/live/build/outputs/apk/debug/live-debug.apk"
            env = {"RUNNER_TEMP": str(root), "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
            for tmp, selected_app in ((root / "outside", app), (private / "tmp", root / "other.apk")):
                with patch.dict(os.environ, dict(env, TMPDIR=str(tmp)), clear=True), \
                     patch.object(runner, "environment_guard"), patch.object(runner, "one_shot_guard"), \
                     patch.object(runner.tempfile, "TemporaryDirectory") as make_temp, \
                     patch.object(runner, "AdbDevice") as device, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.main(["--serial", "emulator-5554", "--owned-emulator-name", "reproof-ci-123-1",
                        "--app-apk", str(selected_app), "--helper-apk", str(helper), "--output", str(output)]), 1)
                    make_temp.assert_not_called()
                    device.assert_not_called()
                    self.assertFalse(output.parent.exists())
                    self.assertFalse(private.exists())

    def test_simultaneous_cleanup_failures_all_attempted_and_preserve_failure(self):
        calls = []
        class Lab:
            sessions = {"one": {}, "two": {}}
            devices = {"owned": {"state": "quarantined"}}
            def close_session(self, sid, owner):
                calls.append(sid)
                raise RuntimeError("secret session payload")
            def stop_maintenance(self):
                calls.append("maintenance")
                raise RuntimeError("secret maintenance payload")
        class Device:
            serial = "emulator-5554"
            def adb_call(self, *args, **kwargs):
                calls.append("forward")
                raise RuntimeError("secret forward payload")
        class Authority:
            def close(self):
                calls.append("authority")
                raise RuntimeError("secret authority payload")
        result = {"passed": False, "failure": "android_startup_timeout"}
        result["cleanup"] = runner.cleanup(Lab(), Authority(), Device(), None)
        self.assertEqual(calls, ["one", "two", "maintenance", "forward", "authority"])
        self.assertEqual(result["failure"], "android_startup_timeout")
        self.assertFalse(result["passed"])
        self.assertFalse(result["cleanup"]["confirmed"])
        self.assertFalse(any(result["cleanup"]["stages"].values()))
        self.assertEqual(len(result["cleanup"]["errors"]), 5)
        self.assertNotIn("secret", json.dumps(result))

    def test_fixed_code_redaction(self):
        for code in runner.ERROR_CODES:
            self.assertEqual(runner.sanitized_error(runner.LiveError(code, "secret-token raw payload")), code)
        for error in (RuntimeError("secret-token"), runner.LiveError("secret-token", "payload")):
            self.assertEqual(runner.sanitized_error(error), "unclassified_failure")

    def test_production_startup_codes_are_all_allowlisted(self):
        from reproof.live.model import ANDROID_STARTUP_DIAGNOSTICS
        self.assertTrue(set(ANDROID_STARTUP_DIAGNOSTICS) <= runner.ERROR_CODES)

    def test_opened_same_repo_branch_attempt_only(self):
        event = {"action": "opened", "pull_request": {
            "head": {"ref": "codex/android-startup-kvm-one-shot-validation", "repo": {"full_name": "ictechgy/reproof"}},
            "base": {"repo": {"full_name": "ictechgy/reproof"}}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_PATH": str(path)}
            with patch.dict(os.environ, env, clear=True):
                path.write_text(json.dumps(event))
                runner.one_shot_guard()
                for action in ("synchronize", "reopened", "edited"):
                    path.write_text(json.dumps(dict(event, action=action)))
                    with self.assertRaises(RuntimeError): runner.one_shot_guard()
                path.write_text(json.dumps(event))
                for key, value in (("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_EVENT_NAME", "workflow_dispatch")):
                    with patch.dict(os.environ, {key: value}):
                        with self.assertRaises(RuntimeError): runner.one_shot_guard()
                event["pull_request"]["head"]["repo"]["full_name"] = "fork/reproof"
                path.write_text(json.dumps(event))
                with self.assertRaises(RuntimeError): runner.one_shot_guard()
                event["pull_request"]["head"]["repo"]["full_name"] = "ictechgy/reproof"
                event["pull_request"]["head"]["ref"] = "main"
                path.write_text(json.dumps(event))
                with self.assertRaises(RuntimeError): runner.one_shot_guard()

    def test_scope_guard_rejects_remote_and_other_serial(self):
        env = {"CI": "true", "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
               "GITHUB_REPOSITORY": "ictechgy/reproof", "REPROOF_OWNED_ANDROID_EMULATOR": "1",
               "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "ANDROID_SERIAL": "emulator-5554"}
        with patch.dict(os.environ, env, clear=True), patch.object(runner.sys, "platform", "linux"):
            runner.environment_guard("emulator-5554", "reproof-ci-123-1")
            for serial, name in (("physical", "reproof-ci-123-1"), ("emulator-5554", "other")):
                with self.assertRaises(RuntimeError): runner.environment_guard(serial, name)
            for key in ("ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT", "ADB_SERVER_PORT"):
                with patch.dict(os.environ, {key: "external"}):
                    with self.assertRaises(RuntimeError): runner.environment_guard("emulator-5554", "reproof-ci-123-1")

    def test_apks_restricted_to_exact_private_pinned_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            private = root / "reproof-startup-private-reproof-ci-123-1"
            app = private / "android-build/runtime-fixture/build/outputs/apk/debug/runtime-fixture-debug.apk"
            helper = private / "android-build/live/build/outputs/apk/debug/live-debug.apk"
            env = {"RUNNER_TEMP": str(root), "TMPDIR": str(private / "tmp"), "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(runner.owned_apk_paths(app, helper), (app, helper))
                for a, h in ((root / "app.apk", helper), (app, root / "helper.apk")):
                    with self.assertRaises(RuntimeError): runner.owned_apk_paths(a, h)
                for key, value in (("TMPDIR", str(root / "tmp")), ("GITHUB_RUN_ATTEMPT", "2")):
                    with patch.dict(os.environ, {key: value}):
                        with self.assertRaises(RuntimeError): runner.owned_apk_paths(app, helper)
                app.parent.mkdir(parents=True)
                external = root / "external.apk"
                external.write_bytes(b"not owned")
                app.symlink_to(external)
                with self.assertRaises(RuntimeError): runner.owned_apk_paths(app, helper)

    def test_output_canonical_nonexisting_owned_and_no_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            expected = root / "reproof-startup-evidence/android"
            with patch.dict(os.environ, {"RUNNER_TEMP": str(root)}, clear=True):
                self.assertEqual(runner.owned_output_path(str(expected)), expected)
                for raw in (str(root / "elsewhere"), str(expected) + "/", str(root) + "/./reproof-startup-evidence/android"):
                    with self.assertRaises(RuntimeError): runner.owned_output_path(raw)
                expected.mkdir(parents=True)
                with self.assertRaises(RuntimeError): runner.owned_output_path(str(expected))
                expected.rmdir()
                expected.symlink_to(root / "missing")
                with self.assertRaises(RuntimeError): runner.owned_output_path(str(expected))
                expected.unlink()
                expected.parent.rmdir()
                external = root / "external"
                external.mkdir()
                expected.parent.symlink_to(external, target_is_directory=True)
                with self.assertRaises(RuntimeError): runner.owned_output_path(str(expected))
            for invalid_temp in (str(root / "absent"), "relative", str(root) + "/."):
                with patch.dict(os.environ, {"RUNNER_TEMP": invalid_temp}, clear=True):
                    with self.assertRaises(RuntimeError): runner.owned_output_path(str(expected))

    def test_no_ui_input_calls_and_guard_before_mkdir(self):
        tree = ast.parse(PATH.read_text())
        calls = [n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        for forbidden in ("input", "inject", "tap", "text", "replay", "start_replay"):
            self.assertNotIn(forbidden, calls)
        self.assertIn('"actions": ["launch"]', PATH.read_text())
        main = PATH.read_text().split("def main(argv=None):", 1)[1]
        self.assertLess(main.index("owned_output_path(args.output)"), main.index("output.mkdir"))
        self.assertLess(main.index("owned_output_path(args.output)"), main.index("preflight(args)"))

    def test_each_cleanup_failure_preserves_later_attempts(self):
        for failed in ("session", "maintenance", "forward", "authority"):
            calls = []
            def attempt(name):
                calls.append(name)
                if name == failed: raise RuntimeError("secret-token")
            class Lab:
                sessions = {"owned": {}}
                devices = {"owned": {"state": "available"}}
                def close_session(self, sid, owner):
                    attempt("session")
                    return {"state": "closed"}
                def stop_maintenance(self): attempt("maintenance")
            class Device:
                serial = "emulator-5554"
                def adb_call(self, *args, **kwargs):
                    attempt("forward")
                    return b""
            class Authority:
                def close(self): attempt("authority")
            result = runner.cleanup(Lab(), Authority(), Device(), None)
            self.assertFalse(result["confirmed"], failed)
            self.assertIn("authority", calls)
            self.assertEqual(calls[:2], ["session", "maintenance"])
            self.assertNotIn("secret-token", json.dumps(result))

    def test_forward_cleanup_only_captured_exact_mapping(self):
        removed = []
        class Device:
            serial = "emulator-5554"
            def adb_call(self, *args, **kwargs):
                if args == ("forward", "--remove", "tcp:12345"):
                    removed.append(args)
                    return b""
                return b"" if removed else b"emulator-5554 tcp:12345 tcp:8766\n"
        class Provider: port = 12345
        self.assertTrue(runner.cleanup(None, None, Device(), Provider())["confirmed"])
        self.assertEqual(len(removed), 1)
        removed.clear()
        Provider.port = 23456
        self.assertFalse(runner.cleanup(None, None, Device(), Provider())["confirmed"])
        self.assertFalse(removed)

    def test_profile_launch_only_and_fresh_frame(self):
        from types import SimpleNamespace
        import base64
        class Ready:
            def is_set(self): return True
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, helper = root / "app.apk", root / "helper.apk"
            app.write_bytes(b"synthetic app")
            helper.write_bytes(b"synthetic helper")
            profile, _ = runner.runtime_profile(app, helper)
            self.assertEqual(profile.data["capabilities"]["actions"], ["launch"])
            class Provider:
                general_profile = True
                device_authority = object()
                native_handshake = SimpleNamespace(protocol_version=2, helper_version=2)
                handshake_ready = helper_ready = Ready()
                target_launched = True
                installed_identity_evidence = {"installedArtifactDigest": runner.sha_file(app)}
            class Lab:
                count = 0
                def frame(self, sid, owner):
                    self.count += 1
                    return {"id": self.count, "mime": "image/jpeg", "width": 10, "height": 10,
                            "imageBase64": base64.b64encode(b"\xff\xd8synthetic\xff\xd9").decode()}
                def get_session(self, sid, owner): return {"state": "active", "lastSequence": 0}
            with patch.object(runner, "AndroidLiveProvider", Provider):
                result = runner.validate_startup(Lab(), {"id": "owned"}, Provider(), app, root)
            self.assertTrue(result["freshFrame"])
            self.assertEqual(result["nativeInputs"], 0)
            self.assertTrue((root / "first-frame.jpg").exists())

if __name__ == "__main__": unittest.main()
