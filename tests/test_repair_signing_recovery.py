import hashlib
from dataclasses import replace
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from reproof import contracts
from reproof.execution.artifacts import BlobSet
from reproof.execution.journal import RunDenied, RunStore
from reproof.repair_android_signing import (
    AndroidSigningIdentity, AndroidSigningMaterialResolver,
)
from reproof.repair_signing import (
    SigningContext, SigningFailureObservation, SigningObservation,
)
from reproof.repair_signing_recovery import (
    MIN_OPERATION_BYTES, SigningOperationStore, SigningOwnerTools,
    SigningRecoveryError, _read_blob,
)
from tests import test_android_signing_owner as owner_tests


APKSIGNER_JAR = owner_tests.APKSIGNER_JAR
PUBLIC_APK = owner_tests.PUBLIC_APK


DISK_BYTES = MIN_OPERATION_BYTES


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _context():
    return SigningContext(
        "sign-operation", "a" * 64, "b" * 64, "inventory_app",
        "c" * 64, _sha(PUBLIC_APK), "d" * 64, "private-nonce")


def _identity(certificate):
    return AndroidSigningIdentity(
        "owned-signing", "inventory_app", "com.example.reproinventory",
        certificate, ("v2", "v3"), ())


def _tools(java, owner_jar, jni_library):
    return SigningOwnerTools(
        Path(java), _sha(java), Path(owner_jar), _sha(owner_jar),
        Path(jni_library), _sha(jni_library), APKSIGNER_JAR,
        _sha(APKSIGNER_JAR))


def _crash_recovery_boundary(boundary, run_root, private_root, scope,
                             java, owner_jar, jni_library, certificate,
                             keystore, password):
    run_store = RunStore(
        run_root, environment_digest="e" * 64,
        disk_limit=2 * DISK_BYTES)
    tools = _tools(java, owner_jar, jni_library)
    identity = _identity(certificate)
    store = SigningOperationStore(
        run_store, scope, tools, identity, Path(private_root))
    context = _context()
    request_digest = "f" * 64
    if boundary == "intent":
        with mock.patch.object(run_store, "admit", side_effect=lambda *a, **k: os._exit(73)):
            with store.admit(context, request_digest, DISK_BYTES):
                pass
    with store.admit(context, request_digest, DISK_BYTES) as operation:
        if boundary == "admission":
            os._exit(73)
        resolver = AndroidSigningMaterialResolver()
        resolver.register(
            identity, keystore=Path(keystore), key_alias="owned-test",
            store_password=password, key_password=password)
        material = resolver.open(identity)
        if boundary == "phase":
            original = store._prepare_phase

            def crash_after_phase(*args, **kwargs):
                result = original(*args, **kwargs)
                os._exit(73)

            patcher = mock.patch.object(store, "_prepare_phase",
                                        side_effect=crash_after_phase)
        elif boundary == "spawn":
            original = __import__("subprocess").Popen

            def crash_after_spawn(*args, **kwargs):
                result = original(*args, **kwargs)
                os._exit(73)

            patcher = mock.patch(
                "reproof.repair_signing_recovery.subprocess.Popen",
                side_effect=crash_after_spawn)
        elif boundary == "start":
            def crash_after_start(*args, **kwargs):
                started = Path(private_root) / "operations/sign-operation/sign/start.json"
                deadline = time.monotonic() + 10
                while (not started.exists() or started.stat().st_size == 0) \
                        and time.monotonic() < deadline:
                    time.sleep(.01)
                os._exit(73)

            patcher = mock.patch.object(store, "_collect",
                                        side_effect=crash_after_start)
        elif boundary in {"before-cleanup", "after-cleanup"}:
            original = store._cleanup_phase

            def crash_cleanup(*args, **kwargs):
                if boundary == "after-cleanup":
                    original(*args, **kwargs)
                os._exit(73)

            patcher = mock.patch.object(store, "_cleanup_phase",
                                        side_effect=crash_cleanup)
        else:
            os._exit(74)
        with patcher:
            store.execute(
                operation, context,
                BlobSet((("candidate.apk", PUBLIC_APK.read_bytes()),)),
                mode="sign", material=material,
                cancellation=threading.Event(),
                deadline=time.monotonic() + 20)
    os._exit(75)


def _crash_after_cleanup_capability(run_root, private_root, scope,
                                    java, owner_jar, jni_library,
                                    certificate):
    run_store = RunStore(
        run_root, environment_digest="e" * 64,
        disk_limit=2 * DISK_BYTES)
    store = SigningOperationStore(
        run_store, scope, _tools(java, owner_jar, jni_library),
        _identity(certificate), Path(private_root))
    with store.recovery("sign-operation", "f" * 64):
        os._exit(73)


class SigningRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        owner_tests.AndroidSigningOwnerTests.setUpClass()
        cls.owner = owner_tests.AndroidSigningOwnerTests
        cls.java = cls.owner.java
        cls.owner_jar = cls.owner.owner_jar
        cls.jni_library = (cls.owner.native /
                           "libreproof_signing_owner_fd.dylib")
        cls.certificate = cls.owner.certificate
        cls.keystore = cls.owner.keystore
        cls.password = cls.owner.password
        cls.tools = _tools(cls.java, cls.owner_jar, cls.jni_library)
        cls.identity = _identity(cls.certificate)

    @classmethod
    def tearDownClass(cls):
        owner_tests.AndroidSigningOwnerTests.tearDownClass()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.run_root = self.root / "run-store"
        self.private_root = self.root / "signing-operations"
        self.scope = contracts.digest({"signingScope": str(self.root)})
        self.run_store = RunStore(
            self.run_root, environment_digest="e" * 64,
            disk_limit=2 * DISK_BYTES)
        self.store = SigningOperationStore(
            self.run_store, self.scope, self.tools, self.identity,
            self.private_root)
        self.context = _context()
        self.request_digest = "f" * 64

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def material(self):
        resolver = AndroidSigningMaterialResolver()
        resolver.register(
            self.identity, keystore=self.keystore, key_alias="owned-test",
            store_password=self.password, key_password=self.password)
        self.addCleanup(resolver.close)
        return resolver.open(self.identity)

    def callback(self, action, timeout=30):
        result = []
        failure = []
        def invoke():
            try:
                result.append(action())
            except BaseException as error:
                failure.append(error)
        worker = threading.Thread(target=invoke)
        worker.start(); worker.join(timeout)
        self.assertFalse(worker.is_alive(), "signing callback did not return")
        if failure:
            raise failure[0]
        self.assertEqual(len(result), 1)
        return result[0], worker

    def crash_at(self, boundary):
        process = multiprocessing.get_context("spawn").Process(
            target=_crash_recovery_boundary, args=(
                boundary, self.run_root, self.private_root, self.scope,
                self.java, self.owner_jar, self.jni_library,
                self.certificate, self.keystore, self.password))
        process.start(); process.join(15)
        self.assertEqual(process.exitcode, 73)

    def crash_after_admission(self):
        self.crash_at("admission")

    def test_actual_sign_then_material_free_inspect_cleans_exact_phases(self):
        source = PUBLIC_APK.read_bytes()
        admission_thread = threading.current_thread()
        with self.store.admit(
                self.context, self.request_digest, DISK_BYTES) as operation:
            signed, signing_thread = self.callback(lambda: self.store.execute(
                operation, self.context, BlobSet((("candidate.apk", source),)),
                mode="sign", material=self.material(),
                cancellation=threading.Event(),
                deadline=time.monotonic() + 20))
            self.assertIs(type(signed), SigningObservation)
            signed_body = signed.artifacts.entries[0][1]
            inspection = SigningContext(
                self.context.operation_id, self.context.repair_plan_digest,
                self.context.project_digest, self.context.application_id,
                self.context.source_digest, self.context.unsigned_artifact_digest,
                self.context.signing_policy_digest, "inspection-nonce",
                hashlib.sha256(signed_body).hexdigest())
            checked, inspection_thread = self.callback(lambda: self.store.execute(
                operation, inspection, signed.artifacts, mode="inspect",
                material=None, cancellation=threading.Event(),
                deadline=time.monotonic() + 20))
            self.assertTrue(checked.valid)
            self.assertIsNot(signing_thread, admission_thread)
            self.assertIsNot(inspection_thread, admission_thread)
            self.assertIsNot(signing_thread, inspection_thread)
            operation.run.finish("succeeded", stopped=True)
        state = json.loads((self.private_root / "operations/sign-operation/state.json").read_text())
        self.assertEqual({key: value["state"] for key, value in state["phases"].items()},
                         {"sign": "cleaned", "inspect": "cleaned"})
        self.assertFalse((self.private_root / "operations/sign-operation/sign").exists())
        self.assertFalse((self.private_root / "operations/sign-operation/inspect").exists())
        status = self.store.status("sign-operation")
        self.assertEqual(status["state"], "terminal")
        public = json.dumps(status, sort_keys=True)
        self.assertNotIn(str(self.private_root), public)
        self.assertNotIn("ownerPid", public)
        self.assertNotIn(self.password.decode("ascii"), public)

    def test_operation_capability_is_lifetime_fenced_before_spawn(self):
        source = BlobSet((("candidate.apk", PUBLIC_APK.read_bytes()),))
        with self.store.admit(
                self.context, self.request_digest, DISK_BYTES) as operation:
            pass
        material = self.material()
        try:
            with self.assertRaises(SigningRecoveryError):
                self.store.execute(
                    operation, self.context, source, mode="sign",
                    material=material, cancellation=threading.Event(),
                    deadline=time.monotonic() + 5)
        finally:
            material.close()
        self.assertFalse((self.private_root /
                          "operations/sign-operation/sign").exists())

    def test_concurrent_duplicate_phase_dispatch_is_rejected_under_producer_lock(self):
        source = BlobSet((("candidate.apk", PUBLIC_APK.read_bytes()),))
        producer_owned = threading.Event()
        release = threading.Event()
        results = []
        failures = []
        with self.store.admit(
                self.context, self.request_digest, DISK_BYTES) as operation:
            original = self.store._prepare_phase
            def paused_prepare(*args, **kwargs):
                producer_owned.set()
                if not release.wait(5):
                    raise AssertionError("duplicate phase barrier timed out")
                return original(*args, **kwargs)
            def invoke(material):
                try:
                    results.append(self.store.execute(
                        operation, self.context, source, mode="sign",
                        material=material, cancellation=threading.Event(),
                        deadline=time.monotonic() + 20))
                except SigningRecoveryError as error:
                    failures.append(error.code)
            with mock.patch.object(
                    self.store, "_prepare_phase", side_effect=paused_prepare):
                first = threading.Thread(target=invoke, args=(self.material(),))
                first.start()
                self.assertTrue(producer_owned.wait(5))
                second = threading.Thread(target=invoke, args=(self.material(),))
                second.start(); second.join(5)
                self.assertFalse(second.is_alive())
                release.set(); first.join(25)
                self.assertFalse(first.is_alive())
            self.assertEqual(failures,
                             ["signing_recovery_producer_live"])
            self.assertEqual(len(results), 1)
            self.assertIs(type(results[0]), SigningObservation)
            operation.run.finish("succeeded", stopped=True)

    def test_close_seals_late_dispatch_and_is_deadline_bounded(self):
        source = BlobSet((("candidate.apk", PUBLIC_APK.read_bytes()),))
        before_spawn = threading.Event()
        release = threading.Event()
        failures = []
        with self.store.admit(
                self.context, self.request_digest, DISK_BYTES) as operation:
            original = self.store._prepare_phase
            def paused_prepare(*args, **kwargs):
                before_spawn.set()
                if not release.wait(5):
                    raise AssertionError("close dispatch barrier timed out")
                return original(*args, **kwargs)
            def invoke():
                try:
                    self.store.execute(
                        operation, self.context, source, mode="sign",
                        material=self.material(), cancellation=threading.Event(),
                        deadline=time.monotonic() + 20)
                except SigningRecoveryError as error:
                    failures.append(error.code)
            with mock.patch.object(
                    self.store, "_prepare_phase", side_effect=paused_prepare), \
                    mock.patch(
                        "reproof.repair_signing_recovery.subprocess.Popen") as spawn:
                worker = threading.Thread(target=invoke)
                worker.start(); self.assertTrue(before_spawn.wait(5))
                started = time.monotonic()
                self.assertFalse(self.store.close(
                    deadline_monotonic=started + .05))
                self.assertLess(time.monotonic() - started, .5)
                release.set(); worker.join(5)
                self.assertFalse(worker.is_alive())
                spawn.assert_not_called()
        self.assertEqual(failures, ["signing_recovery_operation_invalid"])
        self.assertTrue(self.store.close(
            deadline_monotonic=time.monotonic() + 1))

    def test_close_cannot_report_clean_during_popen_registration(self):
        source = BlobSet((("candidate.apk", PUBLIC_APK.read_bytes()),))
        entering_spawn = threading.Event()
        release = threading.Event()
        results = []
        failures = []
        actual_spawn = subprocess.Popen
        def gated_spawn(*args, **kwargs):
            entering_spawn.set()
            if not release.wait(5):
                raise AssertionError("Popen registration barrier timed out")
            return actual_spawn(*args, **kwargs)
        with self.store.admit(
                self.context, self.request_digest, DISK_BYTES) as operation:
            def invoke():
                try:
                    results.append(self.store.execute(
                        operation, self.context, source, mode="sign",
                        material=self.material(), cancellation=threading.Event(),
                        deadline=time.monotonic() + 20))
                except BaseException as error:
                    failures.append(error)
            with mock.patch(
                    "reproof.repair_signing_recovery.subprocess.Popen",
                    side_effect=gated_spawn):
                worker = threading.Thread(target=invoke)
                worker.start(); self.assertTrue(entering_spawn.wait(5))
                started = time.monotonic()
                self.assertFalse(self.store.close(
                    deadline_monotonic=started + .05))
                self.assertLess(time.monotonic() - started, .5)
                release.set(); worker.join(25)
                self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(results), 1)
            self.assertIs(type(results[0]), SigningFailureObservation)
            self.assertEqual(results[0].code, "cancelled")
            operation.run.finish("failed", stopped=True)
        self.assertTrue(self.store.close(
            deadline_monotonic=time.monotonic() + 1))

    def test_close_during_live_jvm_returns_clean_cancelled_failure(self):
        source = BlobSet((("candidate.apk", PUBLIC_APK.read_bytes()),))
        collecting = threading.Event()
        release = threading.Event()
        results = []
        failures = []
        original = self.store._collect
        def gated_collect(*args, **kwargs):
            collecting.set()
            if not release.wait(5):
                raise AssertionError("live JVM close barrier timed out")
            return original(*args, **kwargs)
        with self.store.admit(
                self.context, self.request_digest, DISK_BYTES) as operation:
            def invoke():
                try:
                    results.append(self.store.execute(
                        operation, self.context, source, mode="sign",
                        material=self.material(), cancellation=threading.Event(),
                        deadline=time.monotonic() + 20))
                except BaseException as error:
                    failures.append(error)
            with mock.patch.object(
                    self.store, "_collect", side_effect=gated_collect):
                worker = threading.Thread(target=invoke)
                worker.start(); self.assertTrue(collecting.wait(5))
                self.assertGreater(self.store.active_processes, 0)
                self.assertFalse(self.store.close(
                    deadline_monotonic=time.monotonic() + .05))
                release.set(); worker.join(10)
                self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(results), 1)
            self.assertIs(type(results[0]), SigningFailureObservation)
            self.assertEqual(results[0].code, "cancelled")
            self.assertTrue(results[0].termination_confirmed)
            self.assertTrue(results[0].cleanup_confirmed)
            self.assertEqual(self.store.active_processes, 0)
            operation.run.finish("failed", stopped=True)

    def test_callback_cannot_report_its_own_close_as_clean(self):
        source = BlobSet((("candidate.apk", PUBLIC_APK.read_bytes()),))
        close_results = []
        failures = []
        original = self.store._prepare_phase
        def close_inside_callback(*args, **kwargs):
            close_results.append(self.store.close(
                deadline_monotonic=time.monotonic() + .05))
            return original(*args, **kwargs)
        with self.store.admit(
                self.context, self.request_digest, DISK_BYTES) as operation:
            def invoke():
                try:
                    self.store.execute(
                        operation, self.context, source, mode="sign",
                        material=self.material(), cancellation=threading.Event(),
                        deadline=time.monotonic() + 20)
                except SigningRecoveryError as error:
                    failures.append(error.code)
            with mock.patch.object(
                    self.store, "_prepare_phase",
                    side_effect=close_inside_callback), mock.patch(
                    "reproof.repair_signing_recovery.subprocess.Popen") as spawn:
                worker = threading.Thread(target=invoke)
                worker.start(); worker.join(5)
                self.assertFalse(worker.is_alive())
                spawn.assert_not_called()
        self.assertEqual(close_results, [False])
        self.assertEqual(failures, ["signing_recovery_operation_invalid"])
        self.assertEqual(self.store.active_processes, 0)
        self.assertTrue(self.store.close(
            deadline_monotonic=time.monotonic() + 1))

    def test_spawn_failure_closes_every_transient_descriptor(self):
        source = BlobSet((("candidate.apk", PUBLIC_APK.read_bytes()),))
        opened = set()
        temporary_files = []
        actual_open = os.open
        actual_pipe = os.pipe
        actual_temporary = tempfile.TemporaryFile
        def tracked_open(*args, **kwargs):
            descriptor = actual_open(*args, **kwargs)
            opened.add(descriptor)
            return descriptor
        def tracked_pipe(*args, **kwargs):
            descriptors = actual_pipe(*args, **kwargs)
            opened.update(descriptors)
            return descriptors
        def tracked_temporary(*args, **kwargs):
            selected = actual_temporary(*args, **kwargs)
            temporary_files.append(selected)
            opened.add(selected.fileno())
            return selected
        with self.store.admit(
                self.context, self.request_digest, DISK_BYTES) as operation:
            with mock.patch(
                    "reproof.repair_signing_recovery.os.open",
                    side_effect=tracked_open), mock.patch(
                    "reproof.repair_signing_recovery.os.pipe",
                    side_effect=tracked_pipe), mock.patch(
                    "reproof.repair_signing_recovery.tempfile.TemporaryFile",
                    side_effect=tracked_temporary), mock.patch(
                    "reproof.repair_signing_recovery.subprocess.Popen",
                    side_effect=OSError("injected spawn failure")):
                with self.assertRaises(OSError):
                    self.store.execute(
                        operation, self.context, source, mode="sign",
                        material=self.material(), cancellation=threading.Event(),
                        deadline=time.monotonic() + 20)
            self.assertTrue(temporary_files)
            self.assertTrue(all(item.closed for item in temporary_files))
            for descriptor in opened:
                with self.assertRaises(OSError):
                    fcntl.fcntl(descriptor, fcntl.F_GETFD)

    def test_intent_is_durable_before_admission_and_orphan_is_distinct(self):
        with mock.patch.object(
                self.run_store, "admit",
                side_effect=RunDenied("injected admission failure")):
            with self.assertRaises(RunDenied):
                with self.store.admit(
                        self.context, self.request_digest, DISK_BYTES):
                    pass
        intent = self.private_root / "operations/sign-operation/intent.json"
        self.assertTrue(intent.is_file())
        self.assertEqual(self.store.status("sign-operation")["state"],
                         "intent-orphan")
        with self.assertRaises(SigningRecoveryError):
            with self.store.recovery("sign-operation", self.request_digest):
                pass

    def test_process_crashes_recover_only_under_all_original_locks(self):
        context = multiprocessing.get_context("spawn")
        for boundary in ("admission", "phase", "spawn", "start",
                         "before-cleanup", "after-cleanup"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                run_root = root / "run-store"
                private_root = root / "private"
                scope = contracts.digest({"scope": str(root)})
                process = context.Process(target=_crash_recovery_boundary, args=(
                    boundary, run_root, private_root, scope, self.java,
                    self.owner_jar, self.jni_library, self.certificate,
                    self.keystore, self.password))
                process.start(); process.join(25)
                self.assertEqual(process.exitcode, 73)
                run_store = RunStore(
                    run_root, environment_digest="e" * 64,
                    disk_limit=2 * DISK_BYTES)
                recovered = SigningOperationStore(
                    run_store, scope, self.tools, self.identity, private_root)
                try:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            with recovered.recovery("sign-operation", "f" * 64) as capability:
                                recovered.require_cleanup(capability, run_store)
                            break
                        except SigningRecoveryError as error:
                            if error.code != "signing_recovery_owner_live" or time.monotonic() >= deadline:
                                raise
                            time.sleep(.05)
                    self.assertEqual(recovered.status("sign-operation")["state"],
                                     "recovery-sanitized")
                finally:
                    recovered.close()

    def test_process_crash_after_intent_before_admission_remains_orphan(self):
        root = self.root / "intent-process"
        root.mkdir(mode=0o700)
        scope = contracts.digest({"scope": str(root)})
        process = multiprocessing.get_context("spawn").Process(
            target=_crash_recovery_boundary, args=(
                "intent", root / "run", root / "private", scope,
                self.java, self.owner_jar, self.jni_library,
                self.certificate, self.keystore, self.password))
        process.start(); process.join(15)
        self.assertEqual(process.exitcode, 73)
        run_store = RunStore(
            root / "run", environment_digest="e" * 64,
            disk_limit=2 * DISK_BYTES)
        recovered = SigningOperationStore(
            run_store, scope, self.tools, self.identity, root / "private")
        try:
            self.assertEqual(recovered.status("sign-operation")["state"],
                             "intent-orphan")
            with self.assertRaises(SigningRecoveryError):
                with recovered.recovery("sign-operation", "f" * 64): pass
        finally:
            recovered.close()

    def test_process_crash_after_cleanup_capability_is_safely_reissued(self):
        self.crash_after_admission()
        process = multiprocessing.get_context("spawn").Process(
            target=_crash_after_cleanup_capability, args=(
                self.run_root, self.private_root, self.scope,
                self.java, self.owner_jar, self.jni_library,
                self.certificate))
        process.start(); process.join(15)
        self.assertEqual(process.exitcode, 73)
        with self.store.recovery(
                "sign-operation", self.request_digest) as capability:
            self.store.require_cleanup(capability, self.run_store)

    def test_wrong_scope_request_json_inode_and_extra_file_remain_quarantined(self):
        self.crash_after_admission()
        with self.assertRaises(SigningRecoveryError):
            with self.store.recovery("sign-operation", "0" * 64): pass
        wrong = SigningOperationStore(
            self.run_store, "1" * 64, self.tools, self.identity,
            self.root / "wrong-scope")
        try:
            with self.assertRaises((SigningRecoveryError, RunDenied)):
                with wrong.recovery("sign-operation", self.request_digest): pass
        finally:
            wrong.close()

        operation = self.private_root / "operations/sign-operation"
        (operation / "unexpected").write_bytes(b"preserve")
        with self.assertRaises(SigningRecoveryError):
            with self.store.recovery("sign-operation", self.request_digest): pass
        self.assertEqual((operation / "unexpected").read_bytes(), b"preserve")
        (operation / "unexpected").unlink()
        state = json.loads((operation / "state.json").read_text())
        state["qualified"] = True
        (operation / "state.json").write_text(json.dumps(state))
        with self.assertRaises(SigningRecoveryError):
            with self.store.recovery("sign-operation", self.request_digest): pass

    def test_replaced_inode_key_alias_and_state_root_cannot_bypass_quarantine(self):
        self.crash_after_admission()
        operation = self.private_root / "operations/sign-operation"
        producer = operation / "producer.lock"
        producer.unlink(); producer.touch(mode=0o600)
        with self.assertRaises(SigningRecoveryError):
            with self.store.recovery("sign-operation", self.request_digest): pass
        row = self.run_store.status("sign-operation")
        self.assertEqual(row["state"], "admitted")
        self.assertEqual(row["reservedBytes"], DISK_BYTES)

        alternate_identity = AndroidSigningIdentity(
            "other-signing", self.identity.application_id,
            self.identity.package_name, self.identity.certificate_sha256,
            self.identity.signature_schemes, self.identity.permissions)
        alternate = SigningOperationStore(
            self.run_store, self.scope, self.tools, alternate_identity,
            self.private_root)
        try:
            with self.assertRaises(SigningRecoveryError):
                with alternate.recovery("sign-operation", self.request_digest): pass
        finally:
            alternate.close()

        other_run_store = RunStore(
            self.root / "other-run-store", environment_digest="e" * 64,
            disk_limit=2 * DISK_BYTES)
        other = SigningOperationStore(
            other_run_store, self.scope, self.tools, self.identity,
            self.private_root)
        try:
            with self.assertRaises((SigningRecoveryError, RunDenied)):
                with other.recovery("sign-operation", self.request_digest): pass
        finally:
            other.close()

    def test_live_producer_blocks_recovery_and_cleanup_capability_is_thread_local_one_use(self):
        self.crash_at("phase")
        producer_path = self.private_root / "operations/sign-operation/producer.lock"
        producer = os.open(producer_path, os.O_RDWR | os.O_NOFOLLOW)
        fcntl.flock(producer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(SigningRecoveryError):
                with self.store.recovery("sign-operation", self.request_digest): pass
        finally:
            fcntl.flock(producer, fcntl.LOCK_UN); os.close(producer)

        phase_path = self.private_root / "operations/sign-operation/sign/owner.lock"
        phase = os.open(phase_path, os.O_RDWR | os.O_NOFOLLOW)
        fcntl.flock(phase, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(SigningRecoveryError):
                with self.store.recovery("sign-operation", self.request_digest): pass
        finally:
            fcntl.flock(phase, fcntl.LOCK_UN); os.close(phase)

        captured = []
        with self.store.recovery("sign-operation", self.request_digest) as capability:
            def foreign_thread():
                try:
                    self.store.require_cleanup(capability, self.run_store)
                except SigningRecoveryError as error:
                    captured.append(error.code)
            worker = threading.Thread(target=foreign_thread)
            worker.start(); worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(captured, ["signing_recovery_capability_invalid"])
            self.store.require_cleanup(capability, self.run_store)
            with self.assertRaises(SigningRecoveryError):
                self.store.require_cleanup(capability, self.run_store)
        with self.assertRaises(SigningRecoveryError):
            self.store.require_cleanup(capability, self.run_store)

    def test_recovery_locks_every_phase_before_mutating_the_first(self):
        body = PUBLIC_APK.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        with self.store.admit(
                self.context, self.request_digest, DISK_BYTES) as operation:
            state = self.store._state(operation.operation_id)
            self.store._prepare_phase(
                operation, self.context, "sign", body, state, digest)
            inspection = SigningContext(
                self.context.operation_id, self.context.repair_plan_digest,
                self.context.project_digest, self.context.application_id,
                self.context.source_digest, self.context.unsigned_artifact_digest,
                self.context.signing_policy_digest, "inspection-nonce", digest)
            state = self.store._state(operation.operation_id)
            self.store._prepare_phase(
                operation, inspection, "inspect", body, state, digest)
        operation_root = self.private_root / "operations/sign-operation"
        inspect_lock = os.open(
            operation_root / "inspect/owner.lock", os.O_RDWR | os.O_NOFOLLOW)
        fcntl.flock(inspect_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(SigningRecoveryError):
                with self.store.recovery(
                        "sign-operation", self.request_digest):
                    pass
        finally:
            fcntl.flock(inspect_lock, fcntl.LOCK_UN)
            os.close(inspect_lock)
        self.assertTrue((operation_root / "sign").is_dir())
        state = json.loads((operation_root / "state.json").read_text())
        self.assertEqual(state["phases"]["sign"]["state"], "prepared")

    def test_recovery_rejects_fifo_lock_without_blocking(self):
        self.crash_after_admission()
        producer = self.private_root / "operations/sign-operation/producer.lock"
        producer.unlink()
        os.mkfifo(producer, mode=0o600)
        started = time.monotonic()
        with self.assertRaises(SigningRecoveryError):
            with self.store.recovery(
                    "sign-operation", self.request_digest):
                pass
        self.assertLess(time.monotonic() - started, .5)
        row = self.run_store.status("sign-operation")
        self.assertIn(row["state"], {"admitted", "quarantined"})
        self.assertEqual(row["reservedBytes"], DISK_BYTES)

    def test_cleanup_capability_rejects_a_released_producer_lock(self):
        self.crash_after_admission()
        with self.store.recovery(
                "sign-operation", self.request_digest) as capability:
            producer_fd = next(
                descriptor for descriptor, (_, name, _) in zip(
                    capability._lock_fds, capability._lock_checks)
                if name == "producer.lock")
            fcntl.flock(producer_fd, fcntl.LOCK_UN)
            with self.assertRaises(SigningRecoveryError):
                self.store.require_cleanup(capability, self.run_store)

    def test_cleanup_capability_requires_exact_live_object_despite_fd_reuse(self):
        self.crash_after_admission()
        with self.store.recovery(
                "sign-operation", self.request_digest) as capability:
            copied = replace(capability)
            changed = replace(capability, operation_id="other-operation")
            old_fd = capability._lock_fds[0]
            with self.assertRaises(SigningRecoveryError):
                self.store.require_cleanup(copied, self.run_store)
            with self.assertRaises(SigningRecoveryError):
                self.store.require_cleanup(changed, self.run_store)
        reopened = []
        try:
            for _ in range(64):
                reopened.append(os.open("/dev/null", os.O_RDONLY))
                if reopened[-1] == old_fd:
                    break
            self.assertIn(old_fd, reopened)
            copied._active = True
            with self.assertRaises(SigningRecoveryError):
                self.store.require_cleanup(copied, self.run_store)
        finally:
            for descriptor in reopened:
                os.close(descriptor)

    def test_private_root_symlink_parent_and_oversized_output_are_rejected(self):
        actual = self.root / "actual-parent"
        actual.mkdir(mode=0o700)
        linked = self.root / "linked-parent"
        linked.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(SigningRecoveryError):
            SigningOperationStore(
                self.run_store, self.scope, self.tools, self.identity,
                linked / "private")
        self.assertFalse((actual / "private").exists())

        bounded = self.root / "bounded-output"
        bounded.mkdir(mode=0o700)
        output = bounded / "signed.apk"
        output.write_bytes(b"x" * 33)
        output.chmod(0o600)
        with self.assertRaises(SigningRecoveryError):
            _read_blob(output, 32)


if __name__ == "__main__":
    unittest.main()
