"""Portable safety and regression tests. Every child process is a guarded mock.

Run with: python3 -m unittest discover -s diagnostics/offline-vision -p test_replay.py
No Apple tools, applications, devices, simulators, or network services are run.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPEC = importlib.util.spec_from_file_location("offline_vision_replay_under_test", HERE / "replay.py")
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)

# Independent literals catch accidental or coordinated changes to implementation pins.
PINS = (
    ("ObserveFrame.swift", 1819, "18ad97694079c779f39111b0f31d0ab6336e5644d61860263c604503c28573c9"),
    ("fixtures/03-invalid-attempt-001.jpg", 93904, "3248da8f3e908bf19bbbbecadeb147f876ce6fe26f36dca38440f5668eb98f50"),
    ("fixtures/04-created-attempt-001.jpg", 81847, "ac74d667cc9aa523790993c790dbeadf72f450b012a6d8feb32dbbe0abbeec23"),
)
HOST_ENV = {
    "GITHUB_ACTIONS": "true", "RUNNER_OS": "macOS", "RUNNER_ENVIRONMENT": "github-hosted",
    "GITHUB_REPOSITORY": "ictechgy/reproof", "GITHUB_EVENT_NAME": "pull_request",
    "GITHUB_RUN_ATTEMPT": "1", "DEVELOPER_DIR": "/Applications/Xcode_16.4.app/Contents/Developer",
}
SYSTEM_VERSION = b"ProductName:\tmacOS\nProductVersion:\t15.5\nBuildVersion:\t24F74\n"
XCODE_VERSION = b"Xcode 16.4\nBuild version 16F6\n"
HOST_RESULT = {
    "status": "verified", "xcodeVersion": "16.4", "xcodeBuild": "16F6", "productName": "macOS",
    "productVersion": "15.5", "buildVersion": "24F74", "architecture": "arm64",
}
PRIVATE = "/Users/private-owner/token-secret-do-not-report"


def setUpModule():
    # Defense in depth across class fixture setup/cleanup as well as test bodies.
    # Class-level mocks nest inside these guards; no cleanup may remove both.
    for name in ("run", "Popen"):
        patch = mock.patch.object(subprocess, name, side_effect=AssertionError("Suite-wide process guard"))
        guard = patch.start()
        unittest.addModuleCleanup(patch.stop)
        unittest.addModuleCleanup(guard.assert_not_called)


def observations(fixture, confidence=0.9):
    return [{"text": word, "confidence": confidence} for word in fixture["required"]]


def payload(fixture, confidence=0.9):
    return json.dumps({"observations": observations(fixture, confidence)}).encode("utf-8")


def completed(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class GuardedProcessCase(unittest.TestCase):
    """All tests deny child creation unless the test installs a strict fake."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._run_patch = mock.patch.object(subprocess, "run", side_effect=AssertionError("Unmocked child process"))
        cls.process = cls._run_patch.start()
        cls.addClassCleanup(cls._run_patch.stop)
        # A regression bypassing run must still never create a real child.
        cls._popen_patch = mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Real child process forbidden"))
        cls.popen = cls._popen_patch.start()
        cls.addClassCleanup(cls._popen_patch.stop)

    def setUp(self):
        self.assertIs(subprocess.run, self.process, "Class-level process guard must remain installed")
        self.assertIs(subprocess.Popen, self.popen, "Fallback child guard must remain installed")
        self.process.reset_mock(return_value=True, side_effect=True)
        self.process.side_effect = AssertionError("Unmocked child process")
        self.popen.reset_mock()
        self.temporary = tempfile.TemporaryDirectory(prefix="offline-vision-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name).resolve()
        self.environment = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "DEVELOPER_DIR": replay.DEVELOPER_DIR,
            "HOME": str(self.work), "TMPDIR": str(self.work), "LC_ALL": "C",
        }
        clock_patch = mock.patch.object(replay.time, "monotonic", return_value=100.0)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def tearDown(self):
        self.assertIs(subprocess.run, self.process)
        self.assertIs(subprocess.Popen, self.popen)
        self.popen.assert_not_called()

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(replay.DiagnosticError) as raised:
            function(*args, **kwargs)
        self.assertEqual(str(raised.exception), code)

    def check_call(self, argv, kwargs, expected, timeout, environment=None):
        self.assertEqual(argv, expected)
        self.assertIs(type(argv), list)
        self.assertEqual(set(kwargs), {"cwd", "env", "stdin", "stdout", "stderr", "timeout", "check"})
        self.assertEqual(kwargs["timeout"], timeout)
        self.assertIs(kwargs["check"], False)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)
        work = kwargs["cwd"]
        self.assertIsInstance(work, Path)
        self.assertEqual(kwargs["env"], self.environment if environment is None else environment)

    def copy_inputs(self):
        destination = self.work / "inputs"
        destination.mkdir()
        (destination / "fixtures").mkdir()
        for relative, _, _ in PINS:
            shutil.copyfile(HERE / relative, destination / relative)
        return destination

    @contextlib.contextmanager
    def authorized_host(self, **changes):
        environment = dict(HOST_ENV)
        environment.update(changes)
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(replay.platform, "system", return_value="Darwin"), \
                mock.patch.object(replay.platform, "machine", return_value="arm64"), \
                mock.patch.object(Path, "is_dir", return_value=True):
            yield

    def install_metadata(self, xcode=XCODE_VERSION, system=SYSTEM_VERSION, runtime_environment=False):
        results = [xcode, system]
        commands = [["/usr/bin/xcodebuild", "-version"], ["/usr/bin/sw_vers"]]
        seen = []

        def fake(argv, **kwargs):
            index = len(seen)
            self.assertLess(index, 2, "Metadata must not retry")
            environment = dict(os.environ) if runtime_environment else None
            if environment is not None:
                environment["DEVELOPER_DIR"] = replay.DEVELOPER_DIR
            self.check_call(argv, kwargs, commands[index], 10, environment)
            seen.append(argv)
            result = results[index]
            if isinstance(result, BaseException):
                raise result
            return result if isinstance(result, subprocess.CompletedProcess) else completed(result)

        self.process.side_effect = fake
        return seen

    def install_pipeline(self, first=None, second=None, compiler=None, metadata=False):
        """Validate actual copied bytes and reject every unexpected fake command."""
        seen = []
        observer_results = [first, second]
        inspections = []

        def fake(argv, **kwargs):
            work = kwargs["cwd"]
            inherited = dict(os.environ)
            inherited["DEVELOPER_DIR"] = replay.DEVELOPER_DIR
            offset = 2 if metadata else 0
            index = len(seen)
            if metadata and index < 2:
                command = [["/usr/bin/xcodebuild", "-version"], ["/usr/bin/sw_vers"]][index]
                self.check_call(argv, kwargs, command, 10, inherited)
                seen.append(list(argv))
                return completed([XCODE_VERSION, SYSTEM_VERSION][index])
            if index == offset:
                expected = ["/usr/bin/xcrun", "swiftc", "-O", "-framework", "Vision", "-framework",
                            "Foundation", str(work / "ObserveFrame.swift"), "-o", str(work / "observe-frame")]
                self.check_call(argv, kwargs, expected, 90, inherited)
                # Validate all copies before the compiler can touch any of them.
                for relative, size, digest in PINS:
                    path = work / Path(relative).name
                    data = path.read_bytes()
                    self.assertEqual(len(data), size)
                    self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
                    self.assertEqual(data, (HERE / relative).read_bytes())
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
                    self.assertFalse(path.is_symlink())
                    self.assertNotEqual(path, HERE / relative)
                    inspections.append((str(path), digest))
                seen.append(list(argv))
                if isinstance(compiler, BaseException):
                    raise compiler
                result = completed() if compiler is None else compiler
                if result.returncode == 0:
                    (work / "observe-frame").write_bytes(b"fake executable, never executed")
                    (work / "observe-frame").chmod(0o700)
                return result
            observer_index = index - offset - 1
            self.assertIn(observer_index, (0, 1), "Exactly two observer calls; no retry")
            fixture = replay.FIXTURES[observer_index]
            expected = [str(work / "observe-frame"), str(work / fixture["file"])]
            self.check_call(argv, kwargs, expected, 15, inherited)
            data = Path(argv[1]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), fixture["sha256"])
            self.assertEqual(stat.S_IMODE(Path(argv[1]).stat().st_mode), 0o400)
            seen.append(list(argv))
            result = observer_results[observer_index]
            if isinstance(result, BaseException):
                raise result
            return completed(payload(fixture)) if result is None else result

        self.process.side_effect = fake
        return seen, inspections

    def run_pipeline(self, *, metadata=False, output=None, directory=HERE, **options):
        seen, inspections = self.install_pipeline(metadata=metadata, **options)
        stdout = io.StringIO()
        output = self.work / "report.json" if output is None else output
        host = self.authorized_host() if metadata else mock.patch.object(replay, "verify_host", return_value=dict(HOST_RESULT))
        with host:
            report = replay.run(output, directory=directory, stdout=stdout)
        self.assertEqual(json.loads(stdout.getvalue()), report)
        self.assertIs(report["nativeAcceptance"], False)
        return report, seen, inspections, stdout


class PinnedInputTests(GuardedProcessCase):
    def test_actual_source_and_fixture_hashes_and_sizes_match_independent_pins(self):
        for relative, size, digest in PINS:
            with self.subTest(relative=relative):
                data = (HERE / relative).read_bytes()
                self.assertEqual(len(data), size)
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        self.assertEqual((replay.OBSERVER_BYTES, replay.OBSERVER_SHA256), PINS[0][1:])
        for fixture, (_, size, digest) in zip(replay.FIXTURES, PINS[1:]):
            self.assertEqual((fixture["bytes"], fixture["sha256"]), (size, digest))
        source, images = replay.validate_inputs(HERE)
        self.assertEqual(source, (HERE / PINS[0][0]).read_bytes())
        self.assertEqual(images, [(HERE / pin[0]).read_bytes() for pin in PINS[1:]])
        self.process.assert_not_called()

    def test_provenance_cannot_promote_original_native_evidence(self):
        provenance = json.loads((HERE / "provenance.json").read_text())
        self.assertFalse(provenance["original_native_evidence_pass"])
        self.assertFalse(provenance["native_ios_evidence_pass"])
        self.assertTrue(provenance["reconstruction"])
        self.assertFalse(provenance["recovered_previous_draft"])
        self.assertEqual(provenance["source_run_id"], 40068410548)
        for entry, pin in zip([provenance["observer"], *provenance["fixtures"]], PINS):
            self.assertEqual((entry["path"], entry["size_bytes"], entry["sha256"]), pin)

    def test_each_hash_mismatch_is_rejected_before_any_process(self):
        for index, (relative, _, _) in enumerate(PINS):
            with self.subTest(relative=relative):
                directory = self.copy_inputs()
                path = directory / relative
                data = bytearray(path.read_bytes())
                data[0] ^= 1
                path.write_bytes(data)
                report = replay.run(self.work / ("hash-%s.json" % index), directory=directory, stdout=io.StringIO())
                self.assertEqual(report["outcome"], "input_hash_mismatch")
                self.assertEqual(report["inputValidation"], "failed")
                self.assertEqual(report["attempts"], [])
                self.process.assert_not_called()
                shutil.rmtree(directory)

    def test_each_size_mismatch_is_rejected_before_any_process(self):
        for index, (relative, _, _) in enumerate(PINS):
            with self.subTest(relative=relative):
                directory = self.copy_inputs()
                path = directory / relative
                path.write_bytes(path.read_bytes() + b"x")
                report = replay.run(self.work / ("size-%s.json" % index), directory=directory, stdout=io.StringIO())
                self.assertEqual(report["outcome"], "input_size_mismatch")
                self.process.assert_not_called()
                shutil.rmtree(directory)

    def test_missing_input_rejected_before_host_metadata(self):
        directory = self.copy_inputs()
        (directory / PINS[2][0]).unlink()
        report = replay.run(self.work / "missing.json", directory=directory, stdout=io.StringIO())
        self.assertEqual(report["outcome"], "input_read_failed")
        self.process.assert_not_called()

    def test_symlink_file_rejected_even_when_target_is_pinned(self):
        path = self.work / "source-link"
        path.symlink_to(HERE / PINS[0][0])
        self.assert_error("input_path_invalid", replay.pinned_bytes, path, PINS[0][1], PINS[0][2])
        self.process.assert_not_called()

    def test_symlink_parent_rejected_before_any_process(self):
        path = self.work / "directory-link"
        path.symlink_to(HERE, target_is_directory=True)
        report = replay.run(self.work / "symlink.json", directory=path, stdout=io.StringIO())
        self.assertEqual(report["outcome"], "input_path_invalid")
        self.process.assert_not_called()

    def test_non_regular_input_rejected_without_blocking(self):
        directory = self.work / "not-a-file"
        directory.mkdir()
        with self.assertRaises(replay.DiagnosticError) as raised:
            replay.pinned_bytes(directory, 0, hashlib.sha256(b"").hexdigest())
        self.assertIn(str(raised.exception), ("input_path_invalid", "input_read_failed"))
        self.process.assert_not_called()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable on this platform")
    def test_fifo_input_rejected_without_blocking(self):
        path = self.work / "pipe"
        os.mkfifo(path)
        self.assert_error("input_path_invalid", replay.pinned_bytes, path, 0, hashlib.sha256(b"").hexdigest())
        self.process.assert_not_called()

    def test_immutable_copy_is_exclusive_and_read_only(self):
        path = self.work / "copy"
        replay.immutable_copy(path, b"exact bytes")
        self.assertEqual(path.read_bytes(), b"exact bytes")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
        with self.assertRaises(FileExistsError):
            replay.immutable_copy(path, b"replacement")
        self.assertEqual(path.read_bytes(), b"exact bytes")


class WorkflowTests(GuardedProcessCase):
    def setUp(self):
        super().setUp()
        self.workflow = (ROOT / ".github/workflows/offline-vision-diagnostic.yml").read_text()
        self.active = "\n".join(line for line in self.workflow.splitlines() if not line.lstrip().startswith("#"))

    def test_only_opened_pull_requests_can_trigger_workflow(self):
        self.assertIn("on:\n  pull_request:\n    types: [opened]", self.active)
        for trigger in ("workflow_dispatch:", "push:", "schedule:", "pull_request_target:", "workflow_run:", "synchronize"):
            self.assertNotIn(trigger, self.active)
        self.assertIn("'diagnostics/offline-vision/**'", self.active)
        self.assertIn("'.github/workflows/offline-vision-diagnostic.yml'", self.active)

    def test_public_same_repository_gate_and_read_only_checkout(self):
        self.assertIn("github.event.repository.private == false &&", self.active)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", self.active)
        self.assertIn("permissions:\n  contents: read", self.active)
        self.assertIn("persist-credentials: false", self.active)
        self.assertRegex(self.active, r"uses: actions/checkout@[0-9a-f]{40}\n")
        self.assertNotIn("write-all", self.active)
        self.assertNotIn("secrets.", self.active)

    def test_pinned_host_timeout_and_one_replay_step(self):
        self.assertIn("runs-on: macos-15", self.active)
        self.assertIn("timeout-minutes: 5", self.active)
        self.assertIn("DEVELOPER_DIR: " + HOST_ENV["DEVELOPER_DIR"], self.active)
        self.assertEqual(self.active.count("python3 diagnostics/offline-vision/replay.py --output"), 1)
        self.assertIn('"$RUNNER_TEMP/offline-vision-report.json"', self.active)
        self.assertLess(self.active.index("-m unittest"), self.active.index("replay.py --output"))


class HostTests(GuardedProcessCase):
    def test_exact_host_records_bounded_system_and_architecture_metadata(self):
        seen = self.install_metadata()
        with self.authorized_host():
            report = replay.verify_host(self.work, self.environment)
        self.assertEqual(report, HOST_RESULT)
        self.assertEqual(len(seen), 2)

    def test_x86_64_is_also_an_allowed_architecture(self):
        self.install_metadata()
        with self.authorized_host(), mock.patch.object(replay.platform, "machine", return_value="x86_64"):
            self.assertEqual(replay.verify_host(self.work, self.environment)["architecture"], "x86_64")

    def test_non_darwin_guard_precedes_all_processes(self):
        with self.authorized_host(), mock.patch.object(replay.platform, "system", return_value="Linux"):
            self.assert_error("github_macos_host_required", replay.verify_host, self.work, self.environment)
        self.process.assert_not_called()

    def test_each_host_environment_guard_rejects_missing_or_wrong_value(self):
        for key in HOST_ENV:
            if key == "DEVELOPER_DIR":
                continue
            for value in (None, "wrong", ""):
                with self.subTest(key=key, value=value), self.authorized_host():
                    if value is None:
                        os.environ.pop(key)
                    else:
                        os.environ[key] = value
                    self.assert_error("first_github_hosted_pr_run_required", replay.verify_host, self.work, self.environment)
                    self.process.assert_not_called()

    def test_rerun_attempt_two_is_rejected_before_processes(self):
        with self.authorized_host(GITHUB_RUN_ATTEMPT="2"):
            self.assert_error("first_github_hosted_pr_run_required", replay.verify_host, self.work, self.environment)
        self.process.assert_not_called()

    def test_developer_dir_must_be_exact_and_exist(self):
        for directory in ("/Applications/Xcode.app/Contents/Developer", HOST_ENV["DEVELOPER_DIR"] + "/", ""):
            with self.subTest(directory=directory), self.authorized_host(DEVELOPER_DIR=directory):
                self.assert_error("pinned_xcode_unavailable", replay.verify_host, self.work, self.environment)
        with self.authorized_host(), mock.patch.object(Path, "is_dir", return_value=False):
            self.assert_error("pinned_xcode_unavailable", replay.verify_host, self.work, self.environment)
        self.process.assert_not_called()

    def test_unknown_or_private_architecture_is_rejected_before_processes(self):
        for architecture in ("aarch64", "i386", PRIVATE, "arm64\n"):
            with self.subTest(architecture=architecture), self.authorized_host(), \
                    mock.patch.object(replay.platform, "machine", return_value=architecture):
                self.assert_error("host_architecture_invalid", replay.verify_host, self.work, self.environment)
        self.process.assert_not_called()

    def test_xcode_version_and_build_must_both_match(self):
        for version in (b"Xcode 16.3\nBuild version 16F6\n", b"Xcode 16.4\nBuild version 16F7\n",
                        b"Xcode 16.4\nBuild version 16F6\nextra", b"Xcode 16.4 Build version 16F6"):
            with self.subTest(version=version):
                self.process.reset_mock()
                self.install_metadata(xcode=version)
                with self.authorized_host():
                    self.assert_error("pinned_xcode_version_mismatch", replay.verify_host, self.work, self.environment)
                self.assertEqual(self.process.call_count, 1)

    def test_sw_vers_is_strictly_bounded_and_structured(self):
        invalid = [b"", b"x" * 1025, SYSTEM_VERSION + PRIVATE.encode(),
                   b"ProductName:\tLinux\nProductVersion:\t15.5\nBuildVersion:\t24F74\n",
                   SYSTEM_VERSION.replace(b"15.5", b"123.5"), SYSTEM_VERSION.replace(b"15.5", b"15"),
                   SYSTEM_VERSION.replace(b"24F74", b"24F123456"), SYSTEM_VERSION.replace(b"24F74", b"private/path"),
                   SYSTEM_VERSION.replace(b"macOS", b"\xff"), "not bytes"]
        for value in invalid:
            with self.subTest(value=value):
                self.process.reset_mock()
                self.install_metadata(system=value)
                with self.authorized_host():
                    self.assert_error("host_metadata_invalid", replay.verify_host, self.work, self.environment)
                self.assertEqual(self.process.call_count, 2)

    def test_metadata_launch_timeout_nonzero_and_output_failures_are_sanitized(self):
        cases = [(OSError(PRIVATE), "host_metadata_launch_failed"),
                 (subprocess.TimeoutExpired(PRIVATE, 10, stderr=PRIVATE), "host_metadata_timeout"),
                 (completed(b"", PRIVATE, 1), "host_metadata_failed"),
                 (completed(b"x" * 1025), "host_metadata_invalid"),
                 (completed(b"\xff"), "host_metadata_invalid"),
                 (completed("string"), "host_metadata_invalid")]
        for result, code in cases:
            with self.subTest(code=code):
                self.process.reset_mock()
                self.install_metadata(xcode=result)
                self.assert_error(code, replay.metadata_command, ["/usr/bin/xcodebuild", "-version"], self.work, self.environment)
                self.assertEqual(self.process.call_count, 1)

    def test_other_macos_major_versions_fail_before_compile_or_observers(self):
        for version in (b"14.7", b"26.0"):
            with self.subTest(version=version):
                self.process.reset_mock()
                seen = self.install_metadata(system=SYSTEM_VERSION.replace(b"15.5", version), runtime_environment=True)
                stdout = io.StringIO()
                with self.authorized_host():
                    report = replay.run(self.work / (version.decode() + ".json"), stdout=stdout)
                self.assertEqual(len(seen), 2)
                self.assertEqual(report["outcome"], "pinned_macos_version_mismatch")
                self.assertEqual(report["host"], {"status": "failed"})
                self.assertEqual(report["compiler"]["outcome"], "not_started")
                self.assertEqual(report["observerCalls"], 0)
                self.assertEqual(report["attempts"], [])
                self.assertFalse(report["diagnosticPassed"])
                self.assertFalse(report["nativeAcceptance"])
                self.assertEqual(json.loads(stdout.getvalue()), report)

    def test_host_errors_cannot_publish_raw_metadata_stderr_or_exception_text(self):
        cases = [(OSError(PRIVATE), "host_metadata_launch_failed"),
                 (subprocess.TimeoutExpired(PRIVATE, 10, output=PRIVATE, stderr=PRIVATE), "host_metadata_timeout"),
                 (completed(PRIVATE.encode(), PRIVATE.encode(), 1), "host_metadata_failed"),
                 (completed(PRIVATE.encode(), PRIVATE.encode()), "pinned_xcode_version_mismatch"),
                 (completed((PRIVATE * 100).encode(), PRIVATE.encode()), "host_metadata_invalid")]
        for index, (result, code) in enumerate(cases):
            with self.subTest(code=code):
                self.process.reset_mock()
                seen = self.install_metadata(xcode=result, runtime_environment=True)
                stdout = io.StringIO()
                with self.authorized_host():
                    report = replay.run(self.work / ("private-host-%s.json" % index), stdout=stdout)
                self.assertEqual(len(seen), 1)
                self.assertEqual(report["outcome"], code)
                self.assertEqual(report["host"], {"status": "failed"})
                self.assertEqual(report["attempts"], [])
                self.assertEqual(report["observerCalls"], 0)
                self.assertNotIn(PRIVATE, stdout.getvalue())
                self.assertNotIn(str(self.work), stdout.getvalue())
                self.assertFalse(report["nativeAcceptance"])

    def test_metadata_deadline_is_checked_after_return_including_boundary(self):
        for end, succeeds in ((109.999, True), (110.0, False), (110.001, False)):
            with self.subTest(end=end):
                self.install_metadata()
                self.clock.side_effect = [100.0, end]
                if succeeds:
                    self.assertEqual(replay.metadata_command(["/usr/bin/xcodebuild", "-version"], self.work, self.environment), XCODE_VERSION.decode())
                else:
                    self.assert_error("host_metadata_deadline_exceeded", replay.metadata_command,
                                      ["/usr/bin/xcodebuild", "-version"], self.work, self.environment)
                self.clock.side_effect = None


class ParseAndOracleTests(GuardedProcessCase):
    def test_valid_confidence_types_and_empty_observations(self):
        for confidence in (0, 1, 0.0, 0.9, 1.0):
            rows = [{"text": "word", "confidence": confidence}]
            self.assertEqual(replay.parse_observations(json.dumps({"observations": rows}).encode()), rows)
        self.assertEqual(replay.parse_observations(b'{"observations":[]}'), [])

    def test_exact_output_byte_bound(self):
        base = b'{"observations":[]}'
        self.assertEqual(replay.parse_observations(base + b" " * (65536 - len(base))), [])
        self.assert_error("observer_invalid_output", replay.parse_observations, base + b" " * (65537 - len(base)))

    def test_output_must_be_bytes_valid_utf8_and_json(self):
        for value in (None, "{}", bytearray(b"{}"), b"\xff", b"", b"{", b"{} trailing", b"[" * 2000):
            with self.subTest(value=repr(value)[:80]):
                self.assert_error("observer_invalid_output", replay.parse_observations, value)

    def test_exact_top_level_schema(self):
        for value in ([], None, True, 0, "observations", {}, {"observations": None},
                      {"observations": {}}, {"observations": [], "extra": 1}):
            with self.subTest(value=value):
                self.assert_error("observer_invalid_output", replay.parse_observations, json.dumps(value).encode())

    def test_exact_row_schema_and_typed_fields(self):
        bad_rows = [None, [], "text", {}, {"text": "x"}, {"confidence": 1},
                    {"text": "x", "confidence": 1, "extra": 0}, {"text": None, "confidence": 1},
                    {"text": 1, "confidence": 1}, {"text": False, "confidence": 1},
                    {"text": "x", "confidence": "0.9"}, {"text": "x", "confidence": None},
                    {"text": "x", "confidence": True}, {"text": "x", "confidence": False},
                    {"text": "x", "confidence": []}, {"text": "x", "confidence": {}}]
        for row in bad_rows:
            with self.subTest(row=row):
                self.assert_error("observer_invalid_output", replay.parse_observations, json.dumps({"observations": [row]}).encode())

    def test_observation_count_boundary(self):
        row = {"text": "x", "confidence": 1}
        self.assertEqual(len(replay.parse_observations(json.dumps({"observations": [row] * 128}).encode())), 128)
        self.assert_error("observer_invalid_output", replay.parse_observations, json.dumps({"observations": [row] * 129}).encode())

    def test_text_length_boundary_including_unicode(self):
        for text in ("x" * 512, "é" * 512):
            self.assertEqual(replay.parse_observations(json.dumps({"observations": [{"text": text, "confidence": 1}]}).encode())[0]["text"], text)
        self.assert_error("observer_invalid_output", replay.parse_observations,
                          json.dumps({"observations": [{"text": "x" * 513, "confidence": 1}]}).encode())

    def test_confidence_range_nan_infinity_and_huge_integers_rejected(self):
        literals = ["-0.00001", "1.00001", "NaN", "Infinity", "-Infinity", "1e999", "-1e999", "9" * 4000, "-" + "9" * 4000, "9" * 5000]
        for value in literals:
            with self.subTest(value=value[:30]):
                raw = ('{"observations":[{"text":"x","confidence":' + value + '}]}').encode()
                self.assert_error("observer_invalid_output", replay.parse_observations, raw)

    def test_duplicate_keys_rejected_at_every_object_level(self):
        for raw in (b'{"observations":[],"observations":[]}',
                    b'{"observations":[{"text":"x","text":"y","confidence":1}]}',
                    b'{"observations":[{"text":"x","confidence":0,"confidence":1}]}',
                    b'{"observations":[],"observ\\u0061tions":[]}'):
            self.assert_error("observer_invalid_output", replay.parse_observations, raw)

    def test_required_words_use_case_and_whitespace_normalized_exact_match(self):
        for fixture in replay.FIXTURES:
            rows = [{"text": "  \t" + word.lower().replace(" ", "\n  ") + " \n", "confidence": 0.9}
                    for word in fixture["required"]]
            self.assertEqual(replay.oracle(rows, fixture), {"passed": True, "missingRequired": [], "observedForbidden": []})

    def test_required_threshold_inclusive_at_point_nine(self):
        for fixture in replay.FIXTURES:
            self.assertTrue(replay.oracle(observations(fixture, 0.9), fixture)["passed"])
            verdict = replay.oracle(observations(fixture, 0.899999999), fixture)
            self.assertFalse(verdict["passed"])
            self.assertEqual(verdict["missingRequired"], fixture["required"])

    def test_substrings_and_punctuation_do_not_satisfy_required_words(self):
        for suffix in ("!", " EXTRA", "S"):
            fixture = replay.FIXTURES[0]
            rows = [{"text": word + suffix, "confidence": 1} for word in fixture["required"]]
            self.assertEqual(replay.oracle(rows, fixture)["missingRequired"], fixture["required"])

    def test_every_exact_forbidden_word_blocks_at_any_confidence(self):
        for fixture in replay.FIXTURES:
            for forbidden in fixture["forbidden"]:
                for confidence in (0, 0.000001, 0.8999, 0.9, 1):
                    with self.subTest(fixture=fixture["name"], forbidden=forbidden, confidence=confidence):
                        rows = observations(fixture) + [{"text": " \t" + forbidden.lower().replace(" ", "  ") + "\n", "confidence": confidence}]
                        verdict = replay.oracle(rows, fixture)
                        self.assertFalse(verdict["passed"])
                        self.assertEqual(verdict["missingRequired"], [])
                        self.assertEqual(verdict["observedForbidden"], [forbidden])

    def test_forbidden_substrings_are_not_exact_matches(self):
        for fixture in replay.FIXTURES:
            rows = observations(fixture) + [{"text": word + " EXTRA", "confidence": 1} for word in fixture["forbidden"]]
            self.assertTrue(replay.oracle(rows, fixture)["passed"])


class StageSanitizationTests(GuardedProcessCase):
    def test_only_exact_allowlisted_stage_protocol_is_retained(self):
        valid = ["reproof_observer stage=%s elapsed_ms=%s" % (stage, index)
                 for index, stage in enumerate(sorted(replay.STAGES))]
        bad = [PRIVATE, "TOKEN=super-secret", "reproof_observer stage=unknown elapsed_ms=2",
               "reproof_observer stage=started elapsed_ms=-1", "reproof_observer stage=started elapsed_ms=1.5",
               "reproof_observer stage=started elapsed_ms=1234567890", "reproof_observer stage=started elapsed_ms=1 " ,
               " reproof_observer stage=started elapsed_ms=1", "reproof_observer stage=started elapsed_ms=1 " + PRIVATE,
               "reproof_observer stage=started elapsed_ms=NaN", "reproof_observer stage=started elapsed_ms=١"]
        result = replay.observer_stages("\n".join(bad + valid))
        self.assertEqual(result, [{"stage": stage, "elapsedMs": index} for index, stage in enumerate(sorted(replay.STAGES))])
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_stage_timing_accepts_exact_nine_digit_bound(self):
        self.assertEqual(replay.observer_stages(b"reproof_observer stage=started elapsed_ms=999999999"),
                         [{"stage": "started", "elapsedMs": 999999999}])

    def test_stage_count_is_bounded_to_sixteen(self):
        line = "reproof_observer stage=started elapsed_ms=1\n"
        self.assertEqual(len(replay.observer_stages(line * 100)), 16)

    def test_stage_input_is_bounded_to_sixteen_kib(self):
        suffix = "\nreproof_observer stage=started elapsed_ms=1\n"
        for value in ("x" * 16384 + suffix, ("x" * 16384 + suffix).encode()):
            self.assertEqual(replay.observer_stages(value), [])

    def test_non_text_and_non_ascii_junk_are_not_published(self):
        for value in (None, 1, {}, [], bytearray(b"anything"), b"\xff\xfe" + PRIVATE.encode()):
            self.assertEqual(replay.observer_stages(value), [])


class CompileTests(GuardedProcessCase):
    def compile_with(self, result=None, *, create=True, symlink=False):
        source, executable = self.work / "ObserveFrame.swift", self.work / "observe-frame"
        replay.immutable_copy(source, (HERE / "ObserveFrame.swift").read_bytes())
        expected = ["/usr/bin/xcrun", "swiftc", "-O", "-framework", "Vision", "-framework", "Foundation", str(source), "-o", str(executable)]

        def fake(argv, **kwargs):
            self.check_call(argv, kwargs, expected, 90)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), PINS[0][2])
            if isinstance(result, BaseException):
                raise result
            if create:
                if symlink:
                    executable.symlink_to(source)
                else:
                    executable.write_bytes(b"never executed")
            return completed() if result is None else result

        self.process.side_effect = fake
        outcome = replay.compile_observer(source, executable, self.work, self.environment)
        self.assertEqual(self.process.call_count, 1)
        return outcome

    def test_one_compile_uses_exact_flags_and_ninety_second_timeout(self):
        self.assertEqual(self.compile_with(), {"outcome": "compiled", "timeoutSeconds": 90, "elapsedMs": 0})

    def test_compiler_timeout_is_fixed_code_without_retry(self):
        result = self.compile_with(subprocess.TimeoutExpired(PRIVATE, 90, output=PRIVATE, stderr=PRIVATE))
        self.assertEqual(result["outcome"], "compiler_timeout")
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_compiler_launch_failure_is_fixed_code(self):
        result = self.compile_with(OSError(PRIVATE))
        self.assertEqual(result["outcome"], "compiler_launch_failed")
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_nonzero_compiler_stdout_stderr_are_not_reported(self):
        result = self.compile_with(completed(PRIVATE.encode(), PRIVATE.encode(), 1))
        self.assertEqual(result["outcome"], "compiler_failed")
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_success_without_output_is_failure(self):
        self.assertEqual(self.compile_with(create=False)["outcome"], "compiler_output_missing")

    def test_symlink_output_is_failure(self):
        self.assertEqual(self.compile_with(symlink=True)["outcome"], "compiler_output_missing")

    def test_postreturn_compile_deadline_at_exact_boundary(self):
        self.clock.side_effect = [100.0, 190.0]
        self.assertEqual(self.compile_with()["outcome"], "compiler_deadline_exceeded")

    def test_postreturn_compile_deadline_after_boundary(self):
        self.clock.side_effect = [100.0, 190.001]
        self.assertEqual(self.compile_with()["outcome"], "compiler_deadline_exceeded")

    def test_compile_return_just_before_boundary_is_accepted(self):
        self.clock.side_effect = [100.0, 189.999]
        self.assertEqual(self.compile_with()["outcome"], "compiled")


class ObserverDeadlineTests(GuardedProcessCase):
    def observe_with(self, result=None):
        executable = self.work / "observe-frame"
        fixture = replay.FIXTURES[0]
        image = self.work / fixture["file"]

        def fake(argv, **kwargs):
            self.check_call(argv, kwargs, [str(executable), str(image)], 15)
            if isinstance(result, BaseException):
                raise result
            return completed(payload(fixture)) if result is None else result

        self.process.side_effect = fake
        attempt = replay.observe(executable, image, fixture, self.work, self.environment)
        self.assertEqual(self.process.call_count, 1)
        self.assertEqual(attempt["attempt"], 1)
        self.assertEqual(attempt["processTimeoutSeconds"], 15)
        return attempt

    def test_valid_observation_before_both_deadlines_passes(self):
        self.clock.side_effect = [100.0, 114.0, 114.999]
        self.assertTrue(self.observe_with()["passed"])

    def test_postreturn_exact_fifteen_second_boundary_rejected_before_parse(self):
        self.clock.side_effect = [100.0, 115.0]
        with mock.patch.object(replay, "parse_observations") as parse:
            result = self.observe_with()
        parse.assert_not_called()
        self.assertEqual(result["outcome"], "observer_deadline_exceeded")
        self.assertFalse(result["passed"])

    def test_postreturn_overrun_rejected(self):
        self.clock.side_effect = [100.0, 115.001]
        self.assertEqual(self.observe_with()["processOutcome"], "deadline_exceeded")

    def test_successful_parse_at_exact_boundary_is_rejected(self):
        self.clock.side_effect = [100.0, 101.0, 115.0]
        result = self.observe_with()
        self.assertEqual(result["outcome"], "observer_deadline_exceeded")
        self.assertTrue(result["oracle"]["passed"])
        self.assertFalse(result["passed"])

    def test_successful_parse_after_boundary_is_rejected(self):
        self.clock.side_effect = [100.0, 101.0, 115.001]
        self.assertFalse(self.observe_with()["passed"])

    def test_invalid_parse_at_exact_boundary_becomes_deadline_failure(self):
        self.clock.side_effect = [100.0, 101.0, 115.0]
        self.assertEqual(self.observe_with(completed(b"invalid"))["outcome"], "observer_deadline_exceeded")

    def test_invalid_parse_before_boundary_stays_invalid_output(self):
        self.clock.side_effect = [100.0, 101.0, 114.999]
        self.assertEqual(self.observe_with(completed(b"invalid"))["outcome"], "observer_invalid_output")

    def test_timeout_keeps_only_allowlisted_stages(self):
        stderr = PRIVATE.encode() + b"\nreproof_observer stage=vision_started elapsed_ms=2\n"
        result = self.observe_with(subprocess.TimeoutExpired(PRIVATE, 15, output=PRIVATE, stderr=stderr))
        self.assertEqual(result["outcome"], "observer_timeout")
        self.assertEqual(result["processOutcome"], "timeout")
        self.assertEqual(result["observerStages"], [{"stage": "vision_started", "elapsedMs": 2}])
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_nonzero_is_not_parsed_and_does_not_expose_stdout_or_stderr(self):
        with mock.patch.object(replay, "parse_observations") as parse:
            result = self.observe_with(completed(PRIVATE.encode(), PRIVATE.encode(), 3))
        parse.assert_not_called()
        self.assertEqual(result["outcome"], "observer_failed")
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_launch_exception_is_sanitized(self):
        result = self.observe_with(OSError(PRIVATE))
        self.assertEqual(result["outcome"], "observer_launch_failed")
        self.assertNotIn(PRIVATE, json.dumps(result))


class ExecutionTests(GuardedProcessCase):
    def test_full_guarded_run_metadata_compile_then_two_distinct_fresh_observers(self):
        report, seen, inspections, _ = self.run_pipeline(metadata=True)
        self.assertEqual(len(seen), 5)
        self.assertEqual([Path(call[0]).name for call in seen], ["xcodebuild", "sw_vers", "xcrun", "observe-frame", "observe-frame"])
        self.assertEqual([Path(call[1]).name for call in seen[-2:]], ["03-invalid-attempt-001.jpg", "04-created-attempt-001.jpg"])
        self.assertEqual(len({call[1] for call in seen[-2:]}), 2)
        self.assertEqual(len(inspections), 3)
        self.assertEqual(report["observerCalls"], 2)
        self.assertEqual([attempt["name"] for attempt in report["attempts"]], ["03-invalid", "04-created"])
        self.assertTrue(report["diagnosticPassed"])
        self.assertEqual(report["outcome"], "completed")
        self.assertEqual(report["host"], HOST_RESULT)
        self.assertEqual(report["output"]["status"], "written")
        self.assertEqual(json.loads((self.work / "report.json").read_text()), report)
        for path, _ in inspections:
            self.assertFalse(Path(path).exists(), "Owned temporary files must be cleaned up")

    def test_ordinary_host_environment_and_caches_are_preserved_but_not_reported(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "secret", "AWS_SECRET_ACCESS_KEY": "secret", "DYLD_INSERT_LIBRARIES": PRIVATE,
                                          "PATH": PRIVATE, "HOME": PRIVATE, "SDKROOT": PRIVATE}):
            report, seen, _, stdout = self.run_pipeline()
        self.assertTrue(report["diagnosticPassed"])
        self.assertEqual(len(seen), 3)
        self.assertNotIn(PRIVATE, stdout.getvalue())
        for call in self.process.call_args_list:
            self.assertEqual(call.kwargs["env"]["PATH"], PRIVATE)
            self.assertEqual(call.kwargs["env"]["HOME"], PRIVATE)
            self.assertEqual(call.kwargs["env"]["SDKROOT"], PRIVATE)
            self.assertEqual(call.kwargs["env"]["DEVELOPER_DIR"], replay.DEVELOPER_DIR)
            self.assertNotIn("environment", report)

    def test_first_timeout_still_runs_second_once_without_retry(self):
        report, seen, _, _ = self.run_pipeline(first=subprocess.TimeoutExpired("fake-observer", 15, stderr=b"reproof_observer stage=started elapsed_ms=0"))
        self.assertEqual(len(seen), 3)
        self.assertEqual(report["observerCalls"], 2)
        self.assertEqual([attempt["outcome"] for attempt in report["attempts"]], ["observer_timeout", "passed"])
        self.assertFalse(report["diagnosticPassed"])

    def test_first_launch_failure_still_runs_second_once(self):
        report, seen, _, _ = self.run_pipeline(first=OSError(PRIVATE))
        self.assertEqual(len(seen), 3)
        self.assertEqual([attempt["outcome"] for attempt in report["attempts"]], ["observer_launch_failed", "passed"])
        self.assertFalse(report["diagnosticPassed"])

    def test_first_nonzero_still_runs_second_once(self):
        report, seen, _, _ = self.run_pipeline(first=completed(PRIVATE.encode(), PRIVATE.encode(), 7))
        self.assertEqual(len(seen), 3)
        self.assertEqual([attempt["outcome"] for attempt in report["attempts"]], ["observer_failed", "passed"])
        self.assertFalse(report["diagnosticPassed"])

    def test_first_invalid_parse_still_runs_second_once(self):
        report, seen, _, _ = self.run_pipeline(first=completed(b"bad json"))
        self.assertEqual(len(seen), 3)
        self.assertEqual([attempt["outcome"] for attempt in report["attempts"]], ["observer_invalid_output", "passed"])
        self.assertFalse(report["diagnosticPassed"])

    def test_first_oracle_mismatch_still_runs_second_once(self):
        report, seen, _, _ = self.run_pipeline(first=completed(b'{"observations":[]}'))
        self.assertEqual(len(seen), 3)
        self.assertEqual([attempt["outcome"] for attempt in report["attempts"]], ["oracle_mismatch", "passed"])
        self.assertFalse(report["diagnosticPassed"])

    def test_second_failure_does_not_repeat_first_or_second(self):
        report, seen, _, _ = self.run_pipeline(second=subprocess.TimeoutExpired("fake", 15))
        self.assertEqual(len(seen), 3)
        self.assertEqual([attempt["outcome"] for attempt in report["attempts"]], ["passed", "observer_timeout"])
        self.assertFalse(report["diagnosticPassed"])

    def test_low_confidence_forbidden_word_makes_diagnostic_fail(self):
        fixture = replay.FIXTURES[1]
        rows = observations(fixture) + [{"text": "NAME REQUIRED", "confidence": 0}]
        report, seen, _, _ = self.run_pipeline(second=completed(json.dumps({"observations": rows}).encode()))
        self.assertEqual(len(seen), 3)
        self.assertEqual(report["attempts"][1]["oracle"]["observedForbidden"], ["NAME REQUIRED"])
        self.assertFalse(report["diagnosticPassed"])

    def test_compile_failure_prevents_all_observer_calls(self):
        report, seen, _, _ = self.run_pipeline(compiler=completed(PRIVATE.encode(), PRIVATE.encode(), 1))
        self.assertEqual(len(seen), 1)
        self.assertEqual(report["outcome"], "compiler_failed")
        self.assertEqual(report["observerCalls"], 0)
        self.assertEqual(report["attempts"], [])
        self.assertFalse(report["diagnosticPassed"])

    def test_compile_timeout_prevents_all_observer_calls(self):
        report, seen, _, _ = self.run_pipeline(compiler=subprocess.TimeoutExpired(PRIVATE, 90))
        self.assertEqual(len(seen), 1)
        self.assertEqual(report["outcome"], "compiler_timeout")
        self.assertEqual(report["observerCalls"], 0)

    def test_reports_never_expose_raw_stderr_paths_or_environment(self):
        stderr = PRIVATE.encode() + b"\nGITHUB_TOKEN=secret\nreproof_observer stage=started elapsed_ms=0\n"
        report, _, _, stdout = self.run_pipeline(first=completed(payload(replay.FIXTURES[0]), stderr),
                                                second=completed(b"invalid " + PRIVATE.encode(), stderr))
        encoded = stdout.getvalue()
        self.assertNotIn(PRIVATE, encoded)
        self.assertNotIn("GITHUB_TOKEN", encoded)
        self.assertNotIn(str(self.work), encoded)
        self.assertEqual(report["attempts"][0]["observerStages"], [{"stage": "started", "elapsedMs": 0}])
        self.assertEqual(report["attempts"][1]["outcome"], "observer_invalid_output")

    def test_native_acceptance_false_independently_of_diagnostic_result(self):
        self.assertIs(replay.new_report()["nativeAcceptance"], False)
        for index, first in enumerate((None, completed(b'{"observations":[]}'))):
            report, _, _, _ = self.run_pipeline(first=first, output=self.work / ("native-%s.json" % index))
            self.assertIs(report["diagnosticPassed"], index == 0)
            self.assertIs(report["nativeAcceptance"], False)


class ReportOutputTests(GuardedProcessCase):
    def test_exclusive_output_reservation_failure_runs_no_process(self):
        output = self.work / "existing.json"
        output.write_text("do not overwrite")
        stdout = io.StringIO()
        with mock.patch.object(replay, "validate_inputs") as validate:
            report = replay.run(output, stdout=stdout)
        self.assertEqual(report["outcome"], "output_reservation_failed")
        self.assertEqual(report["output"]["status"], "reservation_failed")
        self.assertEqual(output.read_text(), "do not overwrite")
        self.assertEqual(json.loads(stdout.getvalue()), report)
        self.assertEqual(report["observerCalls"], 0)
        self.assertFalse(report["nativeAcceptance"])
        validate.assert_not_called()
        self.process.assert_not_called()

    def test_missing_output_parent_fails_before_input_validation_or_processes(self):
        report = replay.run(self.work / "missing" / "report.json", stdout=io.StringIO())
        self.assertEqual(report["outcome"], "output_reservation_failed")
        self.assertEqual(report["inputValidation"], "not_started")
        self.process.assert_not_called()

    def test_symlink_output_cannot_overwrite_its_target(self):
        target = self.work / "target"
        target.write_text("unchanged")
        output = self.work / "link"
        output.symlink_to(target)
        report = replay.run(output, stdout=io.StringIO())
        self.assertEqual(report["outcome"], "output_reservation_failed")
        self.assertEqual(target.read_text(), "unchanged")
        self.process.assert_not_called()

    def test_write_flush_and_close_failures_preserve_completed_observations_stdout_and_no_rerun(self):
        for failing, expected in (({"write"}, "write_failed"), ({"flush"}, "write_failed"),
                                  ({"close"}, "close_failed"), ({"write", "close"}, "write_and_close_failed"),
                                  ({"flush", "close"}, "write_and_close_failed")):
            for error_type in (OSError, ValueError):
                with self.subTest(failing=failing, error_type=error_type.__name__):
                    self.process.reset_mock()
                    output = self.work / "fake-report.json"
                    sink = mock.Mock()
                    sink.write.side_effect = error_type(PRIVATE) if "write" in failing else None
                    sink.flush.side_effect = error_type(PRIVATE) if "flush" in failing else None
                    sink.close.side_effect = error_type(PRIVATE) if "close" in failing else None
                    original_open = Path.open

                    def fake_open(path, *args, **kwargs):
                        if path == output:
                            self.assertEqual(args, ("x",))
                            self.assertEqual(kwargs, {"encoding": "utf-8"})
                            return sink
                        return original_open(path, *args, **kwargs)

                    with mock.patch.object(Path, "open", fake_open):
                        report, seen, _, stdout = self.run_pipeline(output=output)
                    self.assertEqual(report["output"]["status"], expected)
                    self.assertEqual(len(seen), 3)
                    self.assertEqual(self.process.call_count, 3)
                    self.assertEqual(report["observerCalls"], 2)
                    self.assertEqual(len(report["attempts"]), 2)
                    self.assertTrue(all(attempt["passed"] for attempt in report["attempts"]))
                    self.assertTrue(report["diagnosticPassed"])
                    self.assertFalse(report["nativeAcceptance"])
                    self.assertEqual(report["outcome"], "completed")
                    self.assertNotIn(PRIVATE, stdout.getvalue())
                    sink.write.assert_called_once()
                    sink.close.assert_called_once()

    def test_cleanup_failure_preserves_both_completed_observations_and_fails_cli(self):
        for use_main in (False, True):
            with self.subTest(use_main=use_main):
                self.process.reset_mock()
                owned = tempfile.TemporaryDirectory(prefix="offline-vision-cleanup-test-")
                self.addCleanup(owned.cleanup)

                class BrokenCleanup:
                    def __enter__(self):
                        return owned.name

                    def __exit__(self, *_args):
                        raise OSError(PRIVATE)

                stdout = io.StringIO()
                output = self.work / ("cleanup-main.json" if use_main else "cleanup-run.json")
                seen, _ = self.install_pipeline()
                with mock.patch.object(replay.tempfile, "TemporaryDirectory", return_value=BrokenCleanup()), \
                        mock.patch.object(replay, "verify_host", return_value=dict(HOST_RESULT)):
                    if use_main:
                        with mock.patch.object(replay.sys, "stdout", stdout):
                            self.assertEqual(replay.main(["--output", str(output)]), 1)
                        report = json.loads(stdout.getvalue())
                    else:
                        report = replay.run(output, stdout=stdout)
                self.assertEqual(json.loads(stdout.getvalue()), report)
                self.assertEqual(len(seen), 3)
                self.assertEqual(self.process.call_count, 3)
                self.assertEqual(report["outcome"], "local_io_failed")
                self.assertTrue(report["diagnosticPassed"])
                self.assertFalse(report["nativeAcceptance"])
                self.assertEqual(report["observerCalls"], 2)
                self.assertEqual([attempt["outcome"] for attempt in report["attempts"]], ["passed", "passed"])
                self.assertEqual(report["output"]["status"], "written")
                self.assertEqual(json.loads(output.read_text()), report)
                self.assertNotIn(PRIVATE, stdout.getvalue())

    def test_local_io_exception_is_sanitized(self):
        with mock.patch.object(replay, "execute", side_effect=OSError(PRIVATE)):
            stdout = io.StringIO()
            report = replay.run(self.work / "io-error.json", stdout=stdout)
        self.assertEqual(report["outcome"], "local_io_failed")
        self.assertNotIn(PRIVATE, stdout.getvalue())
        self.process.assert_not_called()

    def test_unexpected_exception_is_sanitized(self):
        with mock.patch.object(replay, "execute", side_effect=RuntimeError(PRIVATE)):
            stdout = io.StringIO()
            report = replay.run(self.work / "unexpected.json", stdout=stdout)
        self.assertEqual(report["outcome"], "unexpected_local_failure")
        self.assertNotIn(PRIVATE, stdout.getvalue())
        self.process.assert_not_called()

    def test_main_exit_status_requires_diagnostic_success_and_written_output(self):
        for diagnostic, output, expected in ((True, "written", 0), (False, "written", 1),
                                             (True, "write_failed", 1), (True, "close_failed", 1),
                                             (False, "reservation_failed", 1)):
            with self.subTest(diagnostic=diagnostic, output=output), mock.patch.object(replay, "run", return_value={
                    "outcome": "completed", "diagnosticPassed": diagnostic, "output": {"status": output}}) as run:
                self.assertEqual(replay.main(["--output", "new-report.json"]), expected)
                run.assert_called_once_with(Path("new-report.json"))
        self.process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
