import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "native/android-signing-owner/SigningOwner.java"
SOURCE_C = ROOT / "native/android-signing-owner/fd_identity.c"
DOCUMENTED_JAVA = Path(
    "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home/bin/java")
BUILD_TOOLS = Path.home() / "Library/Android/sdk/build-tools/36.0.0"
APKSIGNER_JAR = BUILD_TOOLS / "lib/apksigner.jar"
AAPT2 = BUILD_TOOLS / "aapt2"
ANDROID_JAR = Path.home() / "Library/Android/sdk/platforms/android-35/android.jar"
PUBLIC_APK = (ROOT / "artifacts/product-delivery/d1-android-views-r1/"
              "original-debug/source/app/build/outputs/apk/release/"
              "app-release-unsigned.apk")
PACKAGE = "com.example.reproinventory"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _record(path):
    try:
        raw = Path(path).read_bytes()
        return json.loads(raw) if raw else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _config(work, mode, input_path, output_path, certificate, definition,
            *, extra=None):
    values = {
        "schemaVersion": "1",
        "mode": mode,
        "operationId": "owner-operation",
        "requestDigest": "a" * 64,
        "contextDigest": "b" * 64,
        "scopeDigest": "c" * 64,
        "ownerDefinitionDigest": definition,
        "inputPath": str(input_path),
        "outputPath": str(output_path),
        "inputDigest": _sha(input_path),
        "maxBytes": str(64 * 1024 * 1024),
        "packageName": PACKAGE,
        "certificateSha256": certificate,
        "schemes": "v2,v3",
        "usesPermissions": "",
        "declaredPermissions": "",
        "lockPath": str(work / "owner.lock"),
        "startPath": str(work / "start.json"),
        "terminationPath": str(work / "termination.json"),
        "workPath": str(work),
        "keyAlias": "owned-test" if mode == "sign" else "",
    }
    if extra:
        values.update(extra)
    return "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode("ascii")


def _open_file(path):
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def _launch(command, work, config, key, password, *, hold_lock=True,
            competing_lock=False, lock_mode=0o600,
            signing_material=True):
    for name in ("owner.lock", "start.json", "termination.json"):
        (work / name).touch(mode=0o600, exist_ok=False)
    lock_fd = os.open(work / "owner.lock", os.O_RDWR | os.O_NOFOLLOW)
    os.chmod(work / "owner.lock", lock_mode)
    competing_fd = None
    if competing_lock:
        competing_fd = os.open(work / "owner.lock", os.O_RDWR | os.O_NOFOLLOW)
        fcntl.flock(competing_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if hold_lock:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    start_fd = os.open(work / "start.json", os.O_RDWR | os.O_NOFOLLOW)
    termination_fd = os.open(work / "termination.json", os.O_RDWR | os.O_NOFOLLOW)
    directory_fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    config_file = tempfile.TemporaryFile(dir=work)
    config_file.write(config)
    config_file.flush()
    os.fsync(config_file.fileno())
    config_file.seek(0)
    config_read = config_file.fileno()
    live_read, live_write = os.pipe()
    key_fd = os.open(key, os.O_RDONLY | os.O_NOFOLLOW) if signing_material else 0
    if signing_material:
        password_read, password_write = os.pipe()
        os.write(password_write, password + b"\n" + password + b"\n")
        os.close(password_write)
    else:
        password_read = 0
    inherited = (config_read, lock_fd, live_read, start_fd, termination_fd,
                 directory_fd, key_fd, password_read)
    passed = tuple(value for value in inherited if value >= 3)
    process = subprocess.Popen(
        [*command, *(str(value) for value in inherited)], cwd=work,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, close_fds=True, pass_fds=passed,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
    config_file.close()
    for descriptor in (live_read, start_fd, termination_fd,
                       directory_fd, key_fd, password_read):
        if descriptor < 3:
            continue
        os.close(descriptor)
    return process, lock_fd, live_write, competing_fd


def _crash_parent(command, root, apk, key, password, certificate, definition):
    root = Path(root)
    work = root / "parent-loss"
    work.mkdir(mode=0o700)
    selected = work / "candidate.apk"
    shutil.copyfile(apk, selected)
    selected.chmod(0o600)
    output = work / "signed.apk"
    config = _config(work, "liveness-probe", selected, output,
                     certificate, definition)
    process, lock_fd, live_write, _ = _launch(
        tuple(command), work, config, key, password,
        signing_material=False)
    deadline = time.monotonic() + 10
    while _record(work / "start.json") is None and time.monotonic() < deadline:
        time.sleep(.01)
    (root / "owner-ready").write_text(str(process.pid))
    while not (root / "crash-now").exists() and time.monotonic() < deadline:
        time.sleep(.01)
    # Deliberately keep the lock and liveness writer open. os._exit closes the
    # parent copies; the single owner JVM must retain and then release the lock.
    del lock_fd, live_write
    os._exit(73)


@unittest.skipUnless(
    SOURCE.is_file() and SOURCE_C.is_file() and DOCUMENTED_JAVA.exists()
    and APKSIGNER_JAR.is_file() and AAPT2.is_file() and ANDROID_JAR.is_file()
    and PUBLIC_APK.is_file(), "cached Android signing owner inputs unavailable")
class AndroidSigningOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.java = DOCUMENTED_JAVA.resolve(strict=True)
        cls.javac = cls.java.with_name("javac")
        cls.keytool = cls.java.with_name("keytool")
        cls.jar_tool = cls.java.with_name("jar")
        cls.java_home = cls.java.parents[1]
        cls.classes = cls.root / "classes"
        cls.classes.mkdir(mode=0o700)
        cls.native = cls.root / "native"
        cls.native.mkdir(mode=0o700)
        library = cls.native / "libreproloop_signing_owner_fd.dylib"
        native = subprocess.run([
            "/usr/bin/clang", "-dynamiclib", "-O2", "-Wall", "-Wextra",
            "-Werror", "-I", str(cls.java_home / "include"), "-I",
            str(cls.java_home / "include/darwin"), str(SOURCE_C), "-o",
            str(library),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False)
        if native.returncode != 0:
            raise RuntimeError("fixed FD validator did not compile: " +
                               native.stderr.decode("utf-8", "replace"))
        compiled = subprocess.run([
            str(cls.javac), "-Xlint:all", "-Werror", "-cp",
            str(APKSIGNER_JAR), "-d", str(cls.classes), str(SOURCE),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False)
        if compiled.returncode != 0:
            raise RuntimeError("fixed signing owner did not compile: " +
                               compiled.stderr.decode("utf-8", "replace"))
        cls.owner_jar = cls.root / "reproloop-android-signing-owner.jar"
        packaged = subprocess.run([
            str(cls.jar_tool), "--create", "--file", str(cls.owner_jar),
            "-C", str(cls.classes), ".",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False)
        if packaged.returncode != 0:
            raise RuntimeError("fixed signing owner JAR did not package: " +
                               packaged.stderr.decode("utf-8", "replace"))
        cls.command = (
            str(cls.java), "--add-opens=java.base/java.io=ALL-UNNAMED",
            "-Xmx256m", "-Djava.library.path=" + str(cls.native),
            "-cp", str(cls.owner_jar) + os.pathsep + str(APKSIGNER_JAR),
            "io.reproloop.signing.SigningOwner")
        cls.password = secrets.token_hex(18).encode("ascii")
        cls.keystore = cls.root / "owned-test.p12"
        variable = "REPROLOOP_OWNER_TEST_PASSWORD"
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                       variable: cls.password.decode("ascii")}
        arguments = [
            str(cls.keytool), "-genkeypair", "-storetype", "PKCS12",
            "-keystore", str(cls.keystore), "-storepass:env", variable,
            "-keypass:env", variable, "-alias", "owned-test",
            "-keyalg", "RSA", "-keysize", "2048", "-validity", "1",
            "-dname", "CN=ReproLoop Signing Owner Test",
        ]
        if cls.password.decode("ascii") in arguments:
            raise RuntimeError("test password entered argv")
        generated = subprocess.run(
            arguments, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30, check=False)
        if generated.returncode != 0:
            raise RuntimeError("owned signing key generation failed")
        cls.keystore.chmod(0o600)
        exported = subprocess.run([
            str(cls.keytool), "-exportcert", "-storetype", "PKCS12",
            "-keystore", str(cls.keystore), "-storepass:env", variable,
            "-alias", "owned-test",
        ], env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30, check=False)
        if exported.returncode != 0:
            raise RuntimeError("owned certificate export failed")
        cls.certificate = hashlib.sha256(exported.stdout).hexdigest()
        cls.definition = hashlib.sha256(
            _sha(cls.owner_jar).encode() + _sha(library).encode()
            + _sha(cls.java).encode() + _sha(APKSIGNER_JAR).encode()
        ).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def work(self, name):
        work = self.root / name
        work.mkdir(mode=0o700)
        selected = work / "candidate.apk"
        shutil.copyfile(PUBLIC_APK, selected)
        selected.chmod(0o600)
        return work, selected

    def run_owner(self, mode, name, *, input_path=None, extra=None):
        work, selected = self.work(name)
        if input_path is not None:
            shutil.copyfile(input_path, selected)
            selected.chmod(0o600)
        output = work / "signed.apk"
        config = _config(work, mode, selected, output,
                         self.certificate, self.definition, extra=extra)
        process, lock_fd, live_write, _ = _launch(
            self.command, work, config, self.keystore, self.password,
            signing_material=mode == "sign")
        # Close without LOCK_UN: the inherited open-file description remains
        # locked only if the JVM retained it.
        os.close(lock_fd)
        lock_fd = None
        lock_held_before_ack = None
        try:
            first = process.stdout.readline()
            if first:
                competitor = os.open(work / "owner.lock", os.O_RDWR | os.O_NOFOLLOW)
                try:
                    try:
                        fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        lock_held_before_ack = False
                        fcntl.flock(competitor, fcntl.LOCK_UN)
                    except BlockingIOError:
                        lock_held_before_ack = True
                finally:
                    os.close(competitor)
            try:
                os.write(live_write, b"\x01")
            except BrokenPipeError:
                pass
            remaining, stderr = process.communicate(timeout=30)
            stdout = first + remaining
        finally:
            os.close(live_write)
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
        return work, process, stdout, stderr, lock_held_before_ack

    def permission_apk(self, name, element, *, supported=True):
        root = self.root / name
        root.mkdir(mode=0o700)
        manifest = root / "AndroidManifest.xml"
        manifest.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
            f'package="{PACKAGE}">\n'
            f'  <{element} android:name="android.permission.CAMERA" />\n'
            '  <uses-sdk android:minSdkVersion="26" android:targetSdkVersion="35" />\n'
            '  <application android:label="Owner Test" />\n'
            '</manifest>\n')
        apk = root / "unsigned.apk"
        built = subprocess.run([
            str(AAPT2), "link", "-o", str(apk), "--manifest", str(manifest),
            "-I", str(ANDROID_JAR),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False)
        if not supported:
            self.assertNotEqual(built.returncode, 0)
            self.assertFalse(apk.exists())
            return None
        self.assertEqual(built.returncode, 0, built.stderr)
        apk.chmod(0o600)
        return apk

    def test_owner_signs_and_independently_inspects_in_one_fixed_jvm(self):
        work, process, stdout, stderr, held = self.run_owner("sign", "actual-sign")
        self.assertEqual(process.returncode, 0,
                         (stderr, _record(work / "start.json"),
                          _record(work / "termination.json")))
        self.assertEqual(stderr, b"")
        self.assertTrue(held)
        result = json.loads(stdout)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["packageName"], PACKAGE)
        self.assertEqual(result["schemes"], ["v2", "v3"])
        self.assertEqual(result["usesPermissions"], [])
        self.assertEqual(result["declaredPermissions"], [])
        self.assertEqual(result["certificateSha256"], self.certificate)
        self.assertEqual(result["inputDigest"], _sha(PUBLIC_APK))
        self.assertEqual(result["outputDigest"], _sha(work / "signed.apk"))
        self.assertEqual(_record(work / "start.json")["state"], "started")
        self.assertEqual(_record(work / "start.json")["recordMeaning"], "owner-start")
        self.assertEqual(_record(work / "termination.json")["state"], "succeeded")
        self.assertEqual(_record(work / "termination.json")["recordMeaning"],
                         "exit-intent")
        class_bytes = (self.classes / "io/reproloop/signing/SigningOwner.class").read_bytes()
        self.assertNotIn(b"ProcessBuilder", class_bytes)
        source_text = SOURCE.read_text()
        self.assertNotIn("ProcessBuilder", source_text)
        self.assertNotIn("Runtime.getRuntime().exec", source_text)

        inspected, check, output, errors, inspect_held = self.run_owner(
            "inspect", "actual-inspect", input_path=work / "signed.apk")
        self.assertEqual(check.returncode, 0, errors)
        self.assertTrue(inspect_held)
        self.assertEqual(json.loads(output)["outputDigest"], _sha(work / "signed.apk"))
        self.assertEqual(_record(inspected / "termination.json")["state"], "succeeded")

    def test_parent_exit_keeps_flock_until_the_single_jvm_records_loss_and_exits(self):
        root = self.root / "parent-loss-case"
        root.mkdir(mode=0o700)
        context = multiprocessing.get_context("spawn")
        parent = context.Process(target=_crash_parent, args=(
            self.command, root, PUBLIC_APK, self.keystore, self.password,
            self.certificate, self.definition))
        parent.start()
        deadline = time.monotonic() + 10
        while not (root / "owner-ready").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((root / "owner-ready").is_file())
        (root / "crash-now").touch()
        parent.join(10)
        self.assertEqual(parent.exitcode, 73)
        lock_path = root / "parent-loss/owner.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.fail("single JVM retained the owner lock after exit deadline")
                    time.sleep(.02)
            self.assertEqual(_record(root / "parent-loss/termination.json")["state"],
                             "parent-loss-exit-intent")
            self.assertEqual(_record(root / "parent-loss/termination.json")["recordMeaning"],
                             "exit-intent")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_extra_authority_boolean_and_forged_nonempty_record_never_succeed(self):
        work, process, stdout, _, _ = self.run_owner(
            "sign", "extra-field", extra={"qualified": "true"})
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(stdout, b"")
        record = _record(work / "termination.json")
        self.assertIsNotNone(record)
        self.assertNotEqual(record.get("state"), "succeeded")

        work, selected = self.work("forged-record")
        for name in ("owner.lock", "start.json", "termination.json"):
            (work / name).touch(mode=0o600, exist_ok=False)
        (work / "termination.json").write_text(
            '{"qualified":true,"state":"succeeded"}')
        config = _config(work, "sign", selected, work / "signed.apk",
                         self.certificate, self.definition)
        # _launch creates its own files, so invoke against a fresh sibling while
        # retaining the non-empty forged record behavior through an explicit FD.
        lock_fd = os.open(work / "owner.lock", os.O_RDWR | os.O_NOFOLLOW)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        descriptors = []
        try:
            config_read, config_write = os.pipe(); descriptors += [config_read]
            os.write(config_write, config); os.close(config_write)
            live_read, live_write = os.pipe(); descriptors += [live_read, live_write]
            password_read, password_write = os.pipe(); descriptors += [password_read]
            os.write(password_write, self.password + b"\n" + self.password + b"\n")
            os.close(password_write)
            start_fd = os.open(work / "start.json", os.O_RDWR | os.O_NOFOLLOW)
            term_fd = os.open(work / "termination.json", os.O_RDWR | os.O_NOFOLLOW)
            directory_fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            key_fd = os.open(self.keystore, os.O_RDONLY | os.O_NOFOLLOW)
            descriptors += [start_fd, term_fd, directory_fd, key_fd]
            inherited = (config_read, lock_fd, live_read, start_fd, term_fd,
                         directory_fd, key_fd, password_read)
            child = subprocess.run(
                [*self.command, *(str(value) for value in inherited)], cwd=work,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, close_fds=True, pass_fds=inherited,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                timeout=10, check=False)
            self.assertNotEqual(child.returncode, 0)
            self.assertEqual(child.stdout, b"")
        finally:
            for descriptor in descriptors:
                try: os.close(descriptor)
                except OSError: pass
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_inspect_rejects_wrong_input_digest_before_success(self):
        work, process, stdout, stderr, held = self.run_owner(
            "inspect", "wrong-inspect-digest",
            extra={"inputDigest": "f" * 64})
        self.assertNotEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, b"")
        self.assertIsNone(held)
        self.assertEqual(_record(work / "termination.json")["state"], "failed")

    def test_runtime_permission_aliases_are_measured_and_cannot_hide_extra_access(self):
        for index, element in enumerate(("uses-permission-sdk-23",)):
            with self.subTest(element=element):
                apk = self.permission_apk(f"permission-{index}", element)
                work, process, stdout, stderr, held = self.run_owner(
                    "sign", f"permission-ok-{index}", input_path=apk,
                    extra={"usesPermissions": "android.permission.CAMERA"})
                self.assertEqual(process.returncode, 0, stderr)
                self.assertTrue(held)
                self.assertEqual(json.loads(stdout)["usesPermissions"],
                                 ["android.permission.CAMERA"])
                rejected, failure, output, _, _ = self.run_owner(
                    "sign", f"permission-reject-{index}", input_path=apk)
                self.assertNotEqual(failure.returncode, 0)
                self.assertEqual(output, b"")
                self.assertEqual(_record(rejected / "termination.json")["state"],
                                 "failed")
        # The pinned Android 35 compiler rejects the legacy sdk-m spelling.
        # The owner likewise treats every unrecognized uses-permission* element
        # as unsupported instead of omitting it from the measured permission set.
        self.permission_apk("permission-sdk-m", "uses-permission-sdk-m",
                            supported=False)
        source = SOURCE.read_text()
        self.assertIn('element.startsWith("uses-permission")', source)

    def test_private_modes_and_exclusive_flock_are_required_before_started(self):
        cases = (
            ("public-lock", {"lock_mode": 0o644}, None),
            ("competing-lock", {"hold_lock": False,
                                "competing_lock": True}, None),
            ("public-directory", {}, 0o755),
        )
        for name, options, directory_mode in cases:
            with self.subTest(case=name):
                work, selected = self.work(name)
                if directory_mode is not None:
                    work.chmod(directory_mode)
                config = _config(
                    work, "sign", selected, work / "signed.apk",
                    self.certificate, self.definition)
                process, lock_fd, live_write, competitor = _launch(
                    self.command, work, config, self.keystore,
                    self.password, **options)
                try:
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertNotEqual(process.returncode, 0, stderr)
                    self.assertEqual(stdout, b"")
                    self.assertIsNone(_record(work / "start.json"))
                finally:
                    os.close(live_write)
                    if competitor is not None:
                        fcntl.flock(competitor, fcntl.LOCK_UN)
                        os.close(competitor)
                    try: fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError: pass
                    os.close(lock_fd)

    def test_oversized_single_config_line_is_rejected_without_started_record(self):
        work, selected = self.work("oversized-config")
        config = _config(
            work, "sign", selected, work / "signed.apk",
            self.certificate, self.definition,
            extra={"oversized": "x" * (64 * 1024 + 1)})
        process, lock_fd, live_write, _ = _launch(
            self.command, work, config, self.keystore, self.password)
        try:
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout, b"")
            self.assertIsNone(_record(work / "start.json"))
            self.assertEqual(_record(work / "termination.json")["state"],
                             "configuration-rejected")
        finally:
            os.close(live_write)
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_in_process_sink_rejects_before_signed_output_exceeds_bound(self):
        maximum = PUBLIC_APK.stat().st_size
        work, process, stdout, stderr, held = self.run_owner(
            "sign", "bounded-output", extra={"maxBytes": str(maximum)})
        self.assertNotEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, b"")
        self.assertIsNone(held)
        output = work / "signed.apk"
        self.assertTrue(output.is_file())
        self.assertLessEqual(output.stat().st_size, maximum)
        record = _record(work / "termination.json")
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["stage"], "output-bound")

    def test_inspect_and_probe_require_absent_signing_material_descriptors(self):
        for mode in ("inspect", "liveness-probe"):
            with self.subTest(mode=mode):
                work, selected = self.work("injected-material-" + mode)
                config = _config(
                    work, mode, selected, work / "signed.apk",
                    self.certificate, self.definition)
                process, lock_fd, live_write, _ = _launch(
                    self.command, work, config, self.keystore,
                    self.password, signing_material=True)
                try:
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertNotEqual(process.returncode, 0, stderr)
                    self.assertEqual(stdout, b"")
                    self.assertIsNone(_record(work / "start.json"))
                finally:
                    os.close(live_write)
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)

        work, selected = self.work("injected-key-alias")
        config = _config(
            work, "inspect", selected, work / "signed.apk",
            self.certificate, self.definition,
            extra={"keyAlias": "must-not-be-present"})
        process, lock_fd, live_write, _ = _launch(
            self.command, work, config, self.keystore,
            self.password, signing_material=False)
        try:
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout, b"")
        finally:
            os.close(live_write)
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


if __name__ == "__main__":
    unittest.main()
