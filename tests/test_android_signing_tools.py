import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

from reproof.android_signing_tools import (
    AndroidSigningBuildTools, AndroidSigningOwnerBuild,
    AndroidSigningToolsError, build_android_signing_owner,
    load_android_signing_owner,
)
from reproof.repair_signing_recovery import SigningOwnerTools
from reproof.resources import read_resource


JDK_HOME = Path(
    "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home")
CLANG = Path("/usr/bin/clang")
APKSIGNER_JAR = Path.home() / (
    "Library/Android/sdk/build-tools/36.0.0/lib/apksigner.jar")
SOURCE_NAMES = (
    "native/android-signing-owner/SigningOwner.java",
    "native/android-signing-owner/fd_identity.c",
)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _actual_tools(*, apksigner=APKSIGNER_JAR, apksigner_digest=None,
                  clang=CLANG, clang_digest=None):
    home = JDK_HOME.resolve(strict=True)
    java = home / "bin/java"
    javac = home / "bin/javac"
    jar = home / "bin/jar"
    apksigner = Path(apksigner).resolve(strict=True)
    clang = Path(clang).resolve(strict=True)
    return AndroidSigningBuildTools(
        home, java, _sha(java), javac, _sha(javac), jar, _sha(jar),
        clang, _sha(clang) if clang_digest is None else clang_digest,
        apksigner,
        _sha(apksigner) if apksigner_digest is None else apksigner_digest,
    )


@unittest.skipUnless(
    JDK_HOME.exists() and CLANG.is_file() and APKSIGNER_JAR.is_file(),
    "cached Android signing build tools unavailable")
class AndroidSigningToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.output = self.root / "owner-tools"
        self.tools = _actual_tools()

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, *, output=None, tools=None, cancellation=None,
              deadline=None):
        return build_android_signing_owner(
            self.output if output is None else output,
            self.tools if tools is None else tools,
            cancellation=threading.Event() if cancellation is None else cancellation,
            deadline_monotonic=(time.monotonic() + 45
                                if deadline is None else deadline))

    def test_actual_offline_build_packages_all_nested_classes_and_loads(self):
        result = self.build()
        self.assertIs(type(result), AndroidSigningOwnerBuild)
        self.assertIs(type(result.tools), SigningOwnerTools)
        self.assertEqual(
            load_android_signing_owner(
                self.output, result.manifest_digest).definition_digest,
            result.tools.definition_digest)
        self.assertIsNotNone(ctypes.CDLL(str(result.tools.jni_library)))
        manifest_path = self.output / "tools-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(hashlib.sha256(
            manifest_path.read_bytes()).hexdigest(), result.manifest_digest)
        self.assertEqual(set(self.output.iterdir()), {
            self.output / "reproof-android-signing-owner.jar",
            self.output / "libreproof_signing_owner_fd.dylib",
            manifest_path,
        })
        with zipfile.ZipFile(result.tools.owner_jar) as archive:
            names = set(archive.namelist())
        required = {
            "io/reproof/signing/SigningOwner.class",
            "io/reproof/signing/SigningOwner$Config.class",
            "io/reproof/signing/SigningOwner$Rejected.class",
            "io/reproof/signing/SigningOwner$TextCheck.class",
            "io/reproof/signing/SigningOwner$ManifestIdentity.class",
            "io/reproof/signing/SigningOwner$BoundedSink.class",
        }
        self.assertTrue(required <= names)
        serialized = json.dumps(manifest, sort_keys=True)
        for rejected in ("password", "keystore", "qualified", "secret"):
            self.assertNotIn(rejected, serialized.lower())
        self.assertEqual(
            set(manifest["sources"]), set(SOURCE_NAMES))
        for name in SOURCE_NAMES:
            self.assertEqual(
                manifest["sources"][name]["sha256"],
                hashlib.sha256(read_resource(name)).hexdigest())
        self.assertEqual(list(self.root.glob(".owner-tools.*")), [])

    def test_existing_output_and_relative_or_mismatched_tools_fail_before_spawn(self):
        self.output.mkdir(mode=0o700)
        with mock.patch(
                "reproof.android_signing_tools.subprocess.Popen") as spawn:
            with self.assertRaises(AndroidSigningToolsError) as caught:
                self.build()
            spawn.assert_not_called()
        self.assertEqual(caught.exception.code, "android_signing_tools_output")
        with self.assertRaises(AndroidSigningToolsError):
            AndroidSigningBuildTools(
                Path("relative-jdk"), Path("java"), "0" * 64,
                Path("javac"), "0" * 64, Path("jar"), "0" * 64,
                Path("clang"), "0" * 64, Path("apksigner.jar"), "0" * 64)
        with self.assertRaises(AndroidSigningToolsError) as caught:
            _actual_tools(clang_digest="0" * 64)
        self.assertEqual(caught.exception.code, "android_signing_tools_tool")

        fresh = self.root / "fresh"
        collision = self.root / ".fresh.fixed"
        collision.mkdir(mode=0o700)
        payload = collision / "preserve"
        payload.write_bytes(b"owned elsewhere")
        token = mock.Mock(hex="fixed")
        with mock.patch(
                "reproof.android_signing_tools.uuid.uuid4",
                return_value=token), mock.patch(
                "reproof.android_signing_tools.subprocess.Popen") as spawn:
            with self.assertRaises(AndroidSigningToolsError):
                self.build(output=fresh)
            spawn.assert_not_called()
        self.assertEqual(payload.read_bytes(), b"owned elsewhere")

    def test_changed_tool_is_rejected_again_immediately_before_dispatch(self):
        copied = self.root / "apksigner.jar"
        shutil.copyfile(APKSIGNER_JAR, copied)
        copied.chmod(0o600)
        tools = _actual_tools(apksigner=copied)
        copied.write_bytes(copied.read_bytes() + b"changed")
        with mock.patch(
                "reproof.android_signing_tools.subprocess.Popen") as spawn:
            with self.assertRaises(AndroidSigningToolsError) as caught:
                self.build(tools=tools)
            spawn.assert_not_called()
        self.assertEqual(caught.exception.code, "android_signing_tools_tool")
        self.assertFalse(self.output.exists())

    def test_failure_cancellation_and_timeout_are_static_and_leave_no_process(self):
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch(
                "reproof.android_signing_tools.subprocess.Popen") as spawn:
            with self.assertRaises(AndroidSigningToolsError) as caught:
                self.build(cancellation=cancelled)
            spawn.assert_not_called()
        self.assertEqual(caught.exception.code, "android_signing_tools_cancelled")

        fake = self.root / "fake" / "clang"
        fake.parent.mkdir(mode=0o700)
        pid_file = self.root / "fake-clang.pid"
        fake.write_text(
            "#!/usr/bin/python3\n"
            "import os,subprocess,time\n"
            "child=subprocess.Popen(['/bin/sleep','30'])\n"
            f"open({str(pid_file)!r},'w').write(str(os.getpid())+' '+str(child.pid))\n"
            "time.sleep(30)\n")
        fake.chmod(0o700)
        tools = _actual_tools(clang=fake)
        started = time.monotonic()
        with self.assertRaises(AndroidSigningToolsError) as caught:
            self.build(tools=tools, deadline=time.monotonic() + 1)
        self.assertEqual(caught.exception.code, "android_signing_tools_timeout")
        self.assertLess(time.monotonic() - started, 3)
        for pid in map(int, pid_file.read_text().split()):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.assertFalse(self.output.exists())
        self.assertNotIn(str(fake), str(caught.exception))

    def test_load_requires_exact_manifest_and_rejects_output_tamper(self):
        result = self.build()
        with self.assertRaises(AndroidSigningToolsError):
            load_android_signing_owner(self.output, "0" * 64)
        unexpected = self.output / "unexpected"
        unexpected.write_bytes(b"preserve")
        with self.assertRaises(AndroidSigningToolsError):
            load_android_signing_owner(self.output, result.manifest_digest)
        self.assertEqual(unexpected.read_bytes(), b"preserve")
        unexpected.unlink()
        original_read = read_resource
        with mock.patch(
                "reproof.android_signing_tools.read_resource",
                side_effect=lambda name: original_read(name) + b"changed"):
            with self.assertRaises(AndroidSigningToolsError):
                load_android_signing_owner(
                    self.output, result.manifest_digest)
        owner_jar = self.output / "reproof-android-signing-owner.jar"
        owner_jar.chmod(0o600)
        with owner_jar.open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaises(AndroidSigningToolsError) as caught:
            load_android_signing_owner(self.output, result.manifest_digest)
        self.assertEqual(caught.exception.code,
                         "android_signing_tools_manifest")

    def test_nonzero_and_oversized_tool_output_are_static_and_collected(self):
        failed = self.root / "failed" / "clang"
        failed.parent.mkdir(mode=0o700)
        failed.write_text(
            "#!/usr/bin/python3\n"
            "import sys\n"
            "sys.stderr.write('untrusted compiler detail')\n"
            "raise SystemExit(7)\n")
        failed.chmod(0o700)
        with self.assertRaises(AndroidSigningToolsError) as caught:
            self.build(tools=_actual_tools(clang=failed))
        self.assertEqual(caught.exception.code, "android_signing_tools_process")
        self.assertNotIn("untrusted", str(caught.exception))
        self.assertFalse(self.output.exists())

        noisy = self.root / "noisy" / "clang"
        noisy.parent.mkdir(mode=0o700)
        pid_file = self.root / "noisy.pid"
        noisy.write_text(
            "#!/usr/bin/python3\n"
            "import os,time\n"
            f"open({str(pid_file)!r},'w').write(str(os.getpid()))\n"
            "os.write(1,b'x'*200000)\n"
            "time.sleep(30)\n")
        noisy.chmod(0o700)
        with self.assertRaises(AndroidSigningToolsError) as caught:
            self.build(tools=_actual_tools(clang=noisy))
        self.assertEqual(caught.exception.code,
                         "android_signing_tools_process_output")
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".owner-tools.*")), [])

    def test_script_builds_with_only_explicit_paths_and_digests(self):
        output = self.root / "script-output"
        script = Path(__file__).resolve().parents[1] / (
            "scripts/build-android-signing-owner.py")
        arguments = [
            sys.executable, str(script), "--output-new", str(output),
            "--jdk-home", str(self.tools.jdk_home),
            "--java", str(self.tools.java),
            "--java-sha256", self.tools.java_sha256,
            "--javac", str(self.tools.javac),
            "--javac-sha256", self.tools.javac_sha256,
            "--jar", str(self.tools.jar),
            "--jar-sha256", self.tools.jar_sha256,
            "--clang", str(self.tools.clang),
            "--clang-sha256", self.tools.clang_sha256,
            "--apksigner-jar", str(self.tools.apksigner_jar),
            "--apksigner-jar-sha256", self.tools.apksigner_jar_sha256,
            "--timeout-seconds", "45",
        ]
        process = subprocess.run(
            arguments, cwd=self.root, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"LANG": "C", "LC_ALL": "C"}, timeout=50, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, b"")
        report = json.loads(process.stdout)
        self.assertEqual(set(report), {
            "schemaVersion", "status", "manifestDigest", "outputDigest",
            "ownerDefinitionDigest"})
        self.assertEqual(report["status"], "built")
        loaded = load_android_signing_owner(
            output, report["manifestDigest"])
        self.assertEqual(loaded.definition_digest,
                         report["ownerDefinitionDigest"])

    def test_base_exception_collects_the_exact_owned_process_before_reraise(self):
        fake = self.root / "interrupt" / "clang"
        fake.parent.mkdir(mode=0o700)
        fake.write_text(
            "#!/usr/bin/python3\n"
            "import subprocess,time\n"
            "subprocess.Popen(['/bin/sleep','30'])\n"
            "time.sleep(30)\n")
        fake.chmod(0o700)
        captured = []
        actual_spawn = subprocess.Popen
        leaked = True
        def capture(*args, **kwargs):
            process = actual_spawn(*args, **kwargs)
            captured.append(process)
            return process
        try:
            with mock.patch(
                    "reproof.android_signing_tools.subprocess.Popen",
                    side_effect=capture), mock.patch(
                    "reproof.android_signing_tools._drain",
                    side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.build(tools=_actual_tools(clang=fake))
            self.assertEqual(len(captured), 1)
            leaked = captured[0].poll() is None
            self.assertFalse(leaked)
            with self.assertRaises(ProcessLookupError):
                os.killpg(captured[0].pid, 0)
        finally:
            if captured and captured[0].poll() is None:
                os.killpg(captured[0].pid, signal.SIGKILL)
                captured[0].wait(timeout=3)

    def test_cancel_after_final_tool_and_infinite_deadline_never_publish(self):
        cancellation = threading.Event()
        calls = 0
        actual_run = __import__(
            "reproof.android_signing_tools",
            fromlist=["_run_fixed"])._run_fixed
        def cancel_after_jar(*args, **kwargs):
            nonlocal calls
            result = actual_run(*args, **kwargs)
            calls += 1
            if calls == 3:
                cancellation.set()
            return result
        with mock.patch(
                "reproof.android_signing_tools._run_fixed",
                side_effect=cancel_after_jar):
            with self.assertRaises(AndroidSigningToolsError) as caught:
                self.build(cancellation=cancellation)
        self.assertEqual(caught.exception.code,
                         "android_signing_tools_cancelled")
        self.assertEqual(calls, 3)
        self.assertFalse(self.output.exists())

        with mock.patch(
                "reproof.android_signing_tools.subprocess.Popen",
                side_effect=AssertionError("infinite deadline dispatched")) as spawn:
            with self.assertRaises(AndroidSigningToolsError) as caught:
                self.build(deadline=float("inf"))
            spawn.assert_not_called()
        self.assertEqual(caught.exception.code,
                         "android_signing_tools_configuration")

    def test_completed_process_rechecks_cancellation_before_success(self):
        cancellation = threading.Event()
        completed = subprocess.Popen(
            ["/usr/bin/true"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        original_poll = completed.poll
        def cancelling_poll():
            result = original_poll()
            if result is not None:
                cancellation.set()
            return result
        completed.poll = mock.Mock(side_effect=cancelling_poll)
        with mock.patch(
                "reproof.android_signing_tools.subprocess.Popen",
                return_value=completed):
            with self.assertRaises(AndroidSigningToolsError) as caught:
                self.build(cancellation=cancellation)
        self.assertEqual(caught.exception.code,
                         "android_signing_tools_cancelled")
        self.assertIsNotNone(completed.returncode)
        self.assertFalse(self.output.exists())

    def test_group_writable_output_parent_is_rejected_before_dispatch(self):
        self.root.chmod(0o770)
        try:
            with mock.patch(
                    "reproof.android_signing_tools.subprocess.Popen",
                    side_effect=AssertionError("unsafe parent dispatched")) as spawn:
                with self.assertRaises(AndroidSigningToolsError) as caught:
                    self.build()
                spawn.assert_not_called()
            self.assertEqual(caught.exception.code,
                             "android_signing_tools_output")
        finally:
            self.root.chmod(0o700)

    def test_script_sigint_collects_only_its_owned_compiler_group(self):
        fake = self.root / "cli-interrupt" / "clang"
        fake.parent.mkdir(mode=0o700)
        pid_file = self.root / "cli-interrupt.pids"
        fake.write_text(
            "#!/usr/bin/python3\n"
            "import os,subprocess,time\n"
            "child=subprocess.Popen(['/bin/sleep','30'])\n"
            f"open({str(pid_file)!r},'w').write(str(os.getpid())+' '+str(child.pid))\n"
            "time.sleep(30)\n")
        fake.chmod(0o700)
        tools = _actual_tools(clang=fake)
        output = self.root / "interrupted-cli-output"
        script = Path(__file__).resolve().parents[1] / (
            "scripts/build-android-signing-owner.py")
        arguments = [
            sys.executable, str(script), "--output-new", str(output),
            "--jdk-home", str(tools.jdk_home),
            "--java", str(tools.java), "--java-sha256", tools.java_sha256,
            "--javac", str(tools.javac), "--javac-sha256", tools.javac_sha256,
            "--jar", str(tools.jar), "--jar-sha256", tools.jar_sha256,
            "--clang", str(tools.clang), "--clang-sha256", tools.clang_sha256,
            "--apksigner-jar", str(tools.apksigner_jar),
            "--apksigner-jar-sha256", tools.apksigner_jar_sha256,
            "--timeout-seconds", "45",
        ]
        process = subprocess.Popen(
            arguments, cwd=self.root, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, env={"LANG": "C", "LC_ALL": "C"})
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(pid_file.is_file())
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=3)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(stderr, b"")
        report = json.loads(stdout)
        self.assertEqual(report["code"], "android_signing_tools_cancelled")
        for pid in map(int, pid_file.read_text().split()):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
