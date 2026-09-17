from dataclasses import replace
import copy
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from reproloop import contracts
from reproloop.android_profile import (
    AndroidRuntimeProfile, validate_android_runtime_profile,
)
from reproloop.execution.artifacts import BlobSet
from reproloop.execution.journal import RunStore
from reproloop.fixtures import FixtureCoordinator
from reproloop.live.authority import HostAuthority
from reproloop.live.clock_sync import ClockSynchronizer
from reproloop.live.model import Lab
from reproloop.live.recording_session import TrustedProjectRegistration
from reproloop.qualification import ScenarioRegistry
from reproloop.repair_android import (
    AndroidMobileAdapterConfig, AndroidMobileTools,
)
from reproloop.repair_android_operation import (
    AndroidNativeBinding, AndroidOperationError, AndroidOperationStore,
    AndroidRecoveryInspection,
)
from reproloop.repair_mobile import MobileContext
from reproloop.scenario_runner import (
    ObservationRegistry, ScenarioRunner, VariableResolverRegistry,
)
from tests.test_clock_sync import FakeClock
from tests.test_fixture_allocations import collection_policy, project_document
from tests.test_live_authority_integration import Clock, FencedProvider
from tests.test_worker_profiles import android_document


CANDIDATE = b"candidate-apk-body"


def _sha_bytes(body):
    return hashlib.sha256(body).hexdigest()


def _sha_file(path):
    return _sha_bytes(Path(path).read_bytes())


def _configuration(root):
    root = Path(root)
    original = root / "original.apk"
    helper = root / "helper.apk"
    original.write_bytes(b"original-apk-body")
    helper.write_bytes(b"helper-apk-body")
    original.chmod(0o600); helper.chmod(0o600)
    adb = root / "adb"
    inspector = root / "aapt2"
    for path in (adb, inspector):
        path.write_bytes(b"fixed-public-tool")
        path.chmod(0o700)
    tools = AndroidMobileTools(
        adb, _sha_file(adb), inspector, _sha_file(inspector))
    project = {"id": "mobile_project", "revision": "revision_one"}
    project_digest = contracts.digest(project)
    registration = TrustedProjectRegistration(
        json.dumps(project, sort_keys=True), project_digest,
        json.dumps({"schemaVersion": 1, "captureMode": "full",
                    "retentionSeconds": {"accepted": 60, "rejected": 60,
                                         "sensitive": 60}}, sort_keys=True),
        object(), object())
    profile_data = {
        "projectId": project["id"], "projectDigest": project_digest,
        "applicationId": "inventory_app", "buildId": "original_build",
        "package": "com.example.inventory",
        "artifact": {"sha256": _sha_file(original),
                     "bytes": original.stat().st_size, "versionCode": 1},
    }
    profile = AndroidRuntimeProfile(json.dumps(
        profile_data, sort_keys=True, separators=(",", ":")))
    config = object.__new__(AndroidMobileAdapterConfig)
    values = {
        "lab": object(), "service": object(), "registration": registration,
        "device_id": "android_device", "owner": "repair_owner",
        "original_profile": profile, "original_apk": original,
        "helper_apk": helper, "helper_digest": _sha_file(helper),
        "preparations": (),
        "serial": "android-" + contracts.digest(str(root.resolve()))[:24],
        "tools": tools, "runtime_policy_digest": "9" * 64, "adb_endpoint": None,
        "native_guardian": None,
    }
    for name, value in values.items():
        object.__setattr__(config, name, value)
    return config


def _context(config):
    return MobileContext(
        "mobile-operation", "a" * 64, "b" * 64,
        config.registration.project_digest, config.application_id,
        "c" * 64, _sha_bytes(CANDIDATE), config.scope_digest,
        config.runtime_policy_digest, "private-nonce")


def _real_configuration(root):
    root = Path(root)
    original = root / "real-original.apk"
    helper = root / "real-helper.apk"
    original.write_bytes(b"real-original-apk")
    helper.write_bytes(b"real-helper-apk")
    original.chmod(0o600); helper.chmod(0o600)
    adb = root / "real-adb"
    inspector = root / "real-aapt2"
    for path in (adb, inspector):
        path.write_bytes(b"fixed-real-boundary-tool")
        path.chmod(0o700)
    tools = AndroidMobileTools(
        adb, _sha_file(adb), inspector, _sha_file(inspector))
    clock = Clock()
    authority = HostAuthority(
        root / "real-authority.sqlite3", clock=clock,
        lease_directory=root / "real-leases")
    received = authority.clock_sync.sample(); clock.now += 10
    sent = authority.clock_sync.sample()
    mapping = authority.clock_sync.record_exchange(
        coordinator_clock_id="real_boundary_clock",
        coordinator_send_ns=received.nanoseconds,
        host_received=received, host_sent=sent,
        coordinator_receive_ns=sent.nanoseconds, max_drift_ppm=0)
    grant = authority.issue_parent_grant(
        mapping, grant_id="real_boundary_grant",
        project_id="real_mobile_project",
        controller_id="real_mobile_controller", renewal_sequence=1,
        coordinator_deadline_ns=sent.nanoseconds + 600_000_000_000)
    project = project_document()
    project["id"] = "real_mobile_project"
    project["applications"][0].update(
        platform="android", bundle="com.example.realboundary")
    project["builds"][0]["artifactDigest"] = _sha_file(original)
    profile_data = android_document(_sha_file(original))
    profile_data.update(
        projectId=project["id"], projectDigest=contracts.digest(project),
        applicationId="ios_app", package="com.example.realboundary")
    profile_data["artifact"].update(
        bytes=original.stat().st_size, versionCode=1)
    profile_data["capabilities"]["logAdapter"] = None
    profile_data["capabilities"]["observations"] = [
        "pixels", "accessibility"]
    profile = validate_android_runtime_profile(profile_data)
    serial = "real-" + contracts.digest(str(root.resolve()))[:24]
    descriptor = {
        "id": "real_device", "name": "Real service boundary",
        "platform": "android", "kind": "android-live",
        "factory": FencedProvider,
        "capabilities": {
            "actions": ["tap", "text"], "inputMode": "gesture-batch",
            "locatorKinds": ["accessibility-id"],
            "recordingTextTarget": {
                "kind": "accessibility-id", "value": "account"},
            "applicationIdentity": profile.application_identity,
            "applicationProfile": profile.data,
            "applicationProfileDigest": profile.digest,
        },
        "_authority": {"deviceKind": "android", "physicalId": serial},
    }
    lab = Lab(
        [descriptor], root / "real-lab", authority=authority,
        parent_grant=grant,
        recording_clock_sync=ClockSynchronizer(FakeClock()),
        recording_wall_clock_ms=lambda: int(time.time() * 1000))
    registration = lab.register_recording_project(
        project, collection_policy(), capacity_bytes=256 * 1024 * 1024,
        journal_headroom_bytes=512 * 1024)
    fixtures = FixtureCoordinator(root / "real-fixtures")
    registry = ScenarioRegistry(root / "real-specs")
    runner = ScenarioRunner(
        lab, registry, VariableResolverRegistry(registration),
        ObservationRegistry(registration),
        wall_clock_ms=lambda: int(time.time() * 1000))
    service = lab.create_issue_session_service(
        fixtures, root=root / "real-issues",
        scenario_registry=registry, scenario_runner=runner)
    try:
        config = AndroidMobileAdapterConfig(
            lab=lab, service=service, registration=registration,
            device_id="real_device", owner="repair_owner",
            original_profile=profile, original_apk=original,
            helper_apk=helper, helper_digest=_sha_file(helper),
            preparations=(), serial=serial, tools=tools,
            runtime_policy_digest="8" * 64)
    except BaseException:
        registry.close(); fixtures.close(); lab.close_all(); authority.close()
        raise
    return config, (registry, fixtures, lab, authority)


def _crash(boundary, root):
    root = Path(root)
    config = _configuration(root)
    context = _context(config)
    run_store = RunStore(
        root / "runs", environment_digest="e" * 64,
        disk_limit=2 * 1024 ** 3)
    with mock.patch.object(AndroidMobileAdapterConfig, "validate"):
        store = AndroidOperationStore(
            run_store, config, root / "operations")
        if boundary == "intent":
            with mock.patch.object(
                    run_store, "admit",
                    side_effect=lambda *args, **kwargs: os._exit(73)):
                with store.admit(
                        context,
                        BlobSet((("candidate.apk", CANDIDATE),))):
                    pass
        original = store._write_staged
        def staged(*args, **kwargs):
            result = original(*args, **kwargs)
            if boundary == "partial" and args[1] == "candidate.apk":
                os._exit(73)
            return result
        patcher = mock.patch.object(
            store, "_write_staged", side_effect=staged)
        with patcher, store.admit(
                context, BlobSet((("candidate.apk", CANDIDATE),))) as operation:
            if boundary == "admitted":
                os._exit(73)
            native = store.bind_native(
                operation, context, ownership_generation=7,
                host_incarnation="host_incarnation",
                helper_incarnation="helper_incarnation",
                provider_incarnation="provider_incarnation")
            if boundary == "discard":
                with store.phase(
                        operation, context, native, "install") as capability:
                    store.complete_phase(capability, "d" * 64)
                original_remove = store._remove_staged
                removed = []
                def remove(*args, **kwargs):
                    result = original_remove(*args, **kwargs)
                    removed.append(args[1])
                    if len(removed) == 1:
                        os._exit(73)
                    return result
                with store.phase(
                        operation, context, native,
                        "cleanup") as capability, mock.patch.object(
                            store, "_remove_staged", side_effect=remove):
                    store.discard_staged(operation, capability)
            original_replace = store._replace_state
            def replace_state(operation_id, state):
                phase = state["phases"].get("install")
                if (boundary == "phase-intent" and phase is not None
                        and phase["state"] == "prepared"):
                    os._exit(73)
                if (boundary == "phase-complete" and phase is not None
                        and phase["state"] == "completed"):
                    os._exit(73)
                return original_replace(operation_id, state)
            with mock.patch.object(
                    store, "_replace_state", side_effect=replace_state):
                with store.phase(
                        operation, context, native, "install") as capability:
                    if boundary == "phase":
                        os._exit(73)
                    if boundary == "phase-complete":
                        store.complete_phase(capability, "d" * 64)
    os._exit(74)


class AndroidOperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = _configuration(self.root)
        self.validate = mock.patch.object(
            AndroidMobileAdapterConfig, "validate")
        self.validate.start()
        self.addCleanup(self.validate.stop)
        self.run_store = RunStore(
            self.root / "runs", environment_digest="e" * 64,
            disk_limit=2 * 1024 ** 3)
        self.private_root = self.root / "operations"
        self.store = AndroidOperationStore(
            self.run_store, self.config, self.private_root)
        self.context = _context(self.config)
        self.artifacts = BlobSet((("candidate.apk", CANDIDATE),))

    def tearDown(self):
        self.store.close(deadline_monotonic=time.monotonic() + 2)
        self.temporary.cleanup()

    def crash(self, boundary):
        crash_root = self.root / ("crash-" + boundary)
        crash_root.mkdir(mode=0o700)
        process = multiprocessing.get_context("spawn").Process(
            target=_crash, args=(boundary, crash_root))
        process.start(); process.join(15)
        self.assertEqual(process.exitcode, 73)
        return crash_root

    def native(self, operation):
        return self.store.bind_native(
            operation, self.context, ownership_generation=7,
            host_incarnation="host_incarnation",
            helper_incarnation="helper_incarnation",
            provider_incarnation="provider_incarnation")

    def test_intent_precedes_actual_budgeted_staging_and_caps_are_exact(self):
        observed = []
        empty_before_admit = []
        actual_admit = self.run_store.admit
        def admit(*args, **kwargs):
            intent = (self.private_root /
                      "operations/mobile-operation/intent.json")
            record = json.loads(intent.read_text())
            observed.append(record)
            for name, item in record["files"].items():
                path = intent.parent / "staging" / name
                info = path.lstat()
                empty_before_admit.append((
                    name, info.st_size, info.st_ino,
                    item["identity"]["inode"]))
            return actual_admit(*args, **kwargs)
        with mock.patch.object(self.run_store, "admit", side_effect=admit):
            with self.store.admit(
                    self.context, self.artifacts) as operation:
                self.assertEqual(len(observed), 1)
                self.assertEqual(len(empty_before_admit), 3)
                self.assertTrue(all(
                    size == 0 and inode == expected_inode
                    for _, size, inode, expected_inode in empty_before_admit))
                row = self.run_store.status("mobile-operation")
                expected_payload = (len(CANDIDATE)
                    + self.config.original_apk.stat().st_size
                    + self.config.helper_apk.stat().st_size)
                self.assertGreater(row["reservedBytes"], expected_payload)
                self.assertEqual(row["reservedBytes"],
                                 operation.reserved_bytes)
                self.assertEqual(operation.candidate_path,
                                 operation.staging_root / "candidate.apk")
                self.assertEqual(operation.original_path,
                                 operation.staging_root / "original.apk")
                self.assertEqual(operation.helper_path,
                                 operation.staging_root / "helper.apk")
                for name, digest in (
                    ("candidate.apk", self.context.artifact_digest),
                    ("original.apk", self.config.original_profile.data[
                        "artifact"]["sha256"]),
                    ("helper.apk", self.config.helper_digest)):
                    path = operation.staging_root / name
                    info = path.lstat()
                    self.assertTrue(path.is_file())
                    self.assertEqual(stat_mode(info.st_mode), 0o600)
                    self.assertEqual(info.st_nlink, 1)
                    self.assertEqual(_sha_file(path), digest)
                with self.assertRaises(AndroidOperationError):
                    self.store.require_operation(replace(operation))
                self.assertIs(self.store.require_operation(operation), operation)
                native = self.native(operation)
                copied = replace(native)
                with self.assertRaises(AndroidOperationError):
                    with self.store.phase(
                            operation, self.context, copied, "install"):
                        pass
                phase_result = []
                def callback():
                    with self.store.phase(
                            operation, self.context, native,
                            "install") as capability:
                        with self.assertRaises(AndroidOperationError):
                            self.store.require_phase(replace(capability))
                        self.assertIs(
                            self.store.require_phase(capability), capability)
                        fcntl.flock(capability._producer_fd,
                                    fcntl.LOCK_UN)
                        with self.assertRaises(AndroidOperationError):
                            self.store.require_phase(capability)
                        fcntl.flock(capability._producer_fd,
                                    fcntl.LOCK_EX | fcntl.LOCK_NB)
                        phase_result.append(self.store.complete_phase(
                            capability, "d" * 64))
                worker = threading.Thread(target=callback)
                worker.start(); worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(phase_result, ["d" * 64])
                with self.store.phase(
                        operation, self.context, native, "replay",
                        replay_number=1) as capability:
                    self.store.complete_phase(capability, "e" * 64)
                with self.assertRaises(AndroidOperationError):
                    with self.store.phase(
                            operation, self.context, native, "replay",
                            replay_number=3):
                        pass
                with self.store.phase(
                        operation, self.context, native,
                        "cleanup") as capability:
                    with self.assertRaises(AndroidOperationError):
                        self.store.discard_staged(
                            operation, replace(capability))
                    self.store.discard_staged(operation, capability)
                    self.store.complete_phase(capability, "f" * 64)
                self.assertFalse(operation.candidate_path.exists())
                self.assertFalse(operation.original_path.exists())
                self.assertFalse(operation.helper_path.exists())
                self.assertGreater(
                    self.run_store.status(operation.operation_id)[
                        "reservedBytes"], 0)

    def test_staged_content_is_rehashed_before_every_native_phase(self):
        with self.store.admit(
                self.context, self.artifacts) as operation:
            native = self.native(operation)
            candidate = operation.staging_root / "candidate.apk"
            candidate.write_bytes(b"changed-candidate")
            candidate.chmod(0o600)
            with self.assertRaises(AndroidOperationError):
                with self.store.phase(
                        operation, self.context, native, "install"):
                    pass
            candidate.write_bytes(CANDIDATE)
            candidate.chmod(0o600)
            with self.store.phase(
                    operation, self.context, native,
                    "install") as capability:
                self.store.complete_phase(capability, "d" * 64)

    def test_crash_boundaries_are_distinct_and_never_release_budget(self):
        for boundary in ("intent", "partial", "admitted", "phase",
                         "phase-intent", "phase-complete", "discard"):
            with self.subTest(boundary=boundary):
                root = self.crash(boundary)
                config = _configuration(root)
                run_store = RunStore(
                    root / "runs", environment_digest="e" * 64,
                    disk_limit=2 * 1024 ** 3, create=False)
                with mock.patch.object(AndroidMobileAdapterConfig, "validate"):
                    store = AndroidOperationStore(
                        run_store, config, root / "operations",
                        create=False)
                    try:
                        status = store.status("mobile-operation")
                        if boundary == "intent":
                            self.assertEqual(status["state"], "intent-orphan")
                            with self.assertRaises(AndroidOperationError):
                                with store.recovery(
                                        "mobile-operation", "a" * 64):
                                    pass
                            continue
                        expected = {
                            "partial": "staging-incomplete",
                            "admitted": "native-recovery-required",
                            "phase": "native-recovery-required",
                            "phase-intent": "phase-intent-uncommitted",
                            "phase-complete": "phase-completion-uncommitted",
                            "discard": "staged-discard-incomplete",
                        }[boundary]
                        with store.recovery(
                                "mobile-operation", "a" * 64) as inspection:
                            self.assertIs(type(inspection),
                                          AndroidRecoveryInspection)
                            self.assertEqual(inspection.state, expected)
                            self.assertFalse(hasattr(
                                inspection, "cleanup_capability"))
                        row = run_store.status("mobile-operation")
                        self.assertIn(row["state"],
                                      {"admitted", "quarantined"})
                        self.assertGreater(row["reservedBytes"], 0)
                    finally:
                        store.close(deadline_monotonic=time.monotonic() + 1)

    def test_held_producer_wrong_root_and_inode_tamper_stay_quarantined(self):
        root = self.crash("admitted")
        config = _configuration(root)
        run_store = RunStore(
            root / "runs", environment_digest="e" * 64,
            disk_limit=2 * 1024 ** 3, create=False)
        with mock.patch.object(AndroidMobileAdapterConfig, "validate"):
            store = AndroidOperationStore(
                run_store, config, root / "operations", create=False)
            producer_path = (root /
                "operations/operations/mobile-operation/producer.lock")
            producer = os.open(producer_path, os.O_RDWR | os.O_NOFOLLOW)
            fcntl.flock(producer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(AndroidOperationError):
                    with store.recovery("mobile-operation", "a" * 64):
                        pass
            finally:
                fcntl.flock(producer, fcntl.LOCK_UN); os.close(producer)

            staged = (root /
                "operations/operations/mobile-operation/staging/candidate.apk")
            staged.unlink(); staged.write_bytes(CANDIDATE); staged.chmod(0o600)
            with self.assertRaises(AndroidOperationError):
                with store.recovery("mobile-operation", "a" * 64):
                    pass

            other = RunStore(
                root / "other-runs", environment_digest="e" * 64,
                disk_limit=2 * 1024 ** 3)
            wrong = AndroidOperationStore(
                other, config, root / "operations", create=False)
            try:
                with self.assertRaises(AndroidOperationError):
                    with wrong.recovery("mobile-operation", "a" * 64):
                        pass
            finally:
                wrong.close(deadline_monotonic=time.monotonic() + 1)

    def test_status_is_static_and_close_waits_for_callback(self):
        entered = threading.Event(); release = threading.Event()
        with self.store.admit(
                self.context, self.artifacts) as operation:
            native = self.native(operation)
            def callback():
                with self.store.phase(
                        operation, self.context, native, "install"):
                    entered.set(); release.wait(5)
            worker = threading.Thread(target=callback)
            worker.start(); self.assertTrue(entered.wait(5))
            self.assertFalse(self.store.close(
                deadline_monotonic=time.monotonic() + .05))
            status = self.store.status("mobile-operation")
            self.assertEqual(status["state"], "producer-live")
            public = json.dumps(status, sort_keys=True)
            self.assertNotIn(str(self.private_root), public)
            self.assertNotIn(self.config.serial, public)
            self.assertNotIn("host_incarnation", public)
            release.set(); worker.join(5)
            self.assertFalse(worker.is_alive())
            with self.assertRaises(AndroidOperationError):
                with self.store.phase(
                        operation, self.context, native, "cleanup"):
                    pass
        self.assertTrue(self.store.close(
            deadline_monotonic=time.monotonic() + 1))

    def test_close_tracks_staging_but_not_the_entire_worker_thread(self):
        entered = threading.Event(); release = threading.Event()
        settled = threading.Event(); stop_thread = threading.Event()
        failures = []
        actual = self.store._write_staged
        def stage(*args):
            entered.set(); release.wait(5)
            return actual(*args)
        def worker():
            try:
                with self.store.admit(self.context, self.artifacts):
                    pass
            except AndroidOperationError as error:
                failures.append(error.code)
            finally:
                settled.set()
            stop_thread.wait(5)
        with mock.patch.object(self.store, '_write_staged', side_effect=stage):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.store.close(deadline_monotonic=time.monotonic() + .03))
                self.assertGreater(self.run_store.status(self.context.operation_id)['reservedBytes'], 0)
                release.set(); self.assertTrue(settled.wait(2))
                self.assertTrue(thread.is_alive())
                self.assertTrue(self.store.close(deadline_monotonic=time.monotonic() + .1))
                self.assertEqual(failures, ['android_operation_admission'])
            finally:
                release.set(); stop_thread.set(); thread.join(5)
        self.assertEqual(self.run_store.status(self.context.operation_id)['state'], 'quarantined')

    def test_status_rejects_a_different_configuration_and_request_summary(self):
        with self.store.admit(self.context, self.artifacts):
            pass
        changed_config = copy.copy(self.config)
        object.__setattr__(changed_config, 'runtime_policy_digest', '7' * 64)
        changed = AndroidOperationStore(self.run_store,
            changed_config, self.private_root, create=False)
        try:
            self.assertEqual(changed.status(self.context.operation_id)['state'], 'record-invalid')
        finally:
            changed.close(deadline_monotonic=time.monotonic() + 1)
        state = self.store._state(self.context.operation_id)
        state['requestDigest'] = 'f' * 64
        self.store._replace_state(self.context.operation_id, state)
        self.assertEqual(self.store.status(self.context.operation_id)['state'], 'record-invalid')

    def test_failed_recovery_lock_closes_every_opened_descriptor(self):
        from reproloop import repair_android_operation as module
        with self.store.admit(self.context, self.artifacts):
            pass
        producer = self.store._producer(self.store._intent(self.context.operation_id))
        opened = []
        actual = module._open_regular_at
        def regular(*args, **kwargs):
            descriptor = actual(*args, **kwargs)
            opened.append(descriptor)
            return descriptor
        try:
            for _ in range(3):
                opened.clear()
                with mock.patch.object(module, '_open_regular_at', side_effect=regular):
                    with self.assertRaises(AndroidOperationError):
                        with self.store.recovery(self.context.operation_id, self.context.request_digest):
                            pass
                self.assertGreater(len(opened), 2)
                for descriptor in opened:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
        finally:
            os.close(producer)

    def test_source_parent_link_and_shared_file_are_rejected_before_reading(self):
        source = self.root / 'source-directory'; source.mkdir()
        original = source / 'owned.apk'; original.write_bytes(b'owned')
        link = self.root / 'source-link'; link.symlink_to(source, target_is_directory=True)
        with mock.patch('reproloop.repair_android_operation._file_digest') as read:
            with self.assertRaises(AndroidOperationError):
                self.store._source(link / original.name, 64)
            read.assert_not_called()
        alias = source / 'alias.apk'; os.link(original, alias)
        with mock.patch('reproloop.repair_android_operation._file_digest') as read:
            with self.assertRaises(AndroidOperationError):
                self.store._source(original, 64)
            read.assert_not_called()


class AndroidOperationRealBoundaryTests(unittest.TestCase):
    def test_real_lab_service_profile_config_binds_admission_and_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config, resources = _real_configuration(root)
            registry, fixtures, lab, authority = resources
            store = changed = None
            try:
                run_store = RunStore(
                    root / "real-runs", environment_digest="e" * 64,
                    disk_limit=2 * 1024 ** 3)
                store = AndroidOperationStore(
                    run_store, config, root / "real-operations")
                context = MobileContext(
                    "real-mobile-operation", "a" * 64, "b" * 64,
                    config.registration.project_digest,
                    config.application_id, "c" * 64,
                    _sha_bytes(CANDIDATE), config.scope_digest,
                    config.runtime_policy_digest, "real-private-nonce")
                with store.admit(
                        context,
                        BlobSet((("candidate.apk", CANDIDATE),))) as operation:
                    native = store.bind_native(
                        operation, context, ownership_generation=9,
                        host_incarnation="real_host_incarnation",
                        helper_incarnation="real_helper_incarnation",
                        provider_incarnation="real_provider_incarnation")
                    with store.phase(
                            operation, context, native,
                            "install") as capability:
                        store.complete_phase(capability, "d" * 64)
                with store.recovery(
                        context.operation_id,
                        context.request_digest) as inspection:
                    self.assertEqual(inspection.state,
                                     "native-recovery-required")

                changed_config = replace(
                    config, runtime_policy_digest="7" * 64)
                changed = AndroidOperationStore(
                    run_store, changed_config, root / "real-operations",
                    create=False)
                with self.assertRaises(AndroidOperationError):
                    with changed.recovery(
                            context.operation_id,
                            context.request_digest):
                        pass
            finally:
                for selected in (changed, store):
                    if selected is not None:
                        selected.close(
                            deadline_monotonic=time.monotonic() + 1)
                registry.close(); fixtures.close(); lab.close_all()
                authority.close()


def stat_mode(value):
    return value & 0o777


if __name__ == "__main__":
    unittest.main()
