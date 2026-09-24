"""The actual SDK runs under the native guardian inside a live operation phase."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from reproof.adb_endpoint import ScopedAdbClient
from reproof.repair_android_operation import AndroidOperationStore, AndroidOperationError
from tests import test_android_native_call_storage as support
from tests.test_adb_endpoint import ADB, sha
from tests.test_android_process_guardian import SDK, ROOT


class AndroidNativeProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix='owned-native-adb-')
        cls.addClassCleanup(temporary.cleanup)
        cls.guardian_path = Path(temporary.name).resolve() / 'guardian'
        result = subprocess.run(['/usr/bin/clang', '-std=c11', '-fblocks', '-Wall', '-Wextra', '-Werror',
            '-isysroot', str(SDK), str(ROOT / 'native/android-process-guardian/main.c'),
            '-framework', 'CoreFoundation', '-o', str(cls.guardian_path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        if result.returncode:
            raise RuntimeError('Owned Android guardian compilation failed')

    def setUp(self):
        self.fixture = support.AndroidNativeCallStorageTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.config = replace(self.fixture.config, tools=replace(
            self.fixture.config.tools, adb=ADB, adb_digest=sha(ADB)))
        self.operations = AndroidOperationStore(self.fixture.runs, self.config, self.f.root / 'native-sdk-operations')
        self.addCleanup(lambda: self.operations.close(deadline_monotonic=time.monotonic() + 1))
        self.fixture.operations = self.operations
        self.fixture.config = self.config

    def client(self, operation):
        client = ScopedAdbClient(self.config.tools.adb, self.config.tools.adb_digest, self.config.adb_endpoint,
            serial=self.config.serial, work_root=operation.staging_root,
            sandbox_sha256=self.config.adb_endpoint.sandbox_sha256)
        self.addCleanup(client.close)
        return client

    def guardian(self):
        from reproof.android_native_process import AndroidGuardianTools
        return AndroidGuardianTools(self.guardian_path, sha(self.guardian_path))

    def test_sdk_result_gateway_collection_and_slot_reuse_are_bound_to_the_live_phase(self):
        from reproof.android_native_process import run_native_adb
        from reproof.android_native_calls import validate_workspace
        with self.operations.admit(self.f.context, self.fixture.fixture.blobs) as operation:
            client = self.client(operation)
            with self.fixture.phase(operation) as (phase, descriptors):
                previous = '0' * 64
                for sequence in (1, 2):
                    result = run_native_adb(self.operations, descriptors, self.guardian(), client,
                        ('shell', 'echo owned-output'), cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic() + 5)
                    self.assertEqual(result.stdout, b'owned-output\n')
                    self.assertEqual(result.returncode, 0)
                    self.assertTrue(result.terminated and result.bounded)
                    self.assertFalse(result.interrupted)
                    self.assertEqual(client.active_processes, 0)
                    intent = self.operations._intent(operation.operation_id)
                    self.assertEqual(validate_workspace(descriptors.operation_directory_fd, intent), 'idle')
                    state = json.loads((operation.staging_root.parent / 'native-calls/state.json').read_bytes())
                    self.assertEqual(state['nextSequence'], sequence + 1)
                    self.assertNotEqual(state['historyDigest'], previous)
                    previous = state['historyDigest']
                self.operations.complete_phase(phase, 'a' * 64)

    def test_expired_call_starts_no_gateway_or_guardian(self):
        from reproof.android_native_process import run_native_adb
        with self.operations.admit(self.f.context, self.fixture.fixture.blobs) as operation:
            client = self.client(operation)
            with self.fixture.phase(operation) as (phase, descriptors):
                with self.assertRaises(AndroidOperationError):
                    run_native_adb(self.operations, descriptors, self.guardian(), client,
                        ('shell', 'echo owned'), cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic() - 1)
                self.assertEqual(client.active_processes, 0)
                self.assertFalse(self.fixture.server.requests)
                self.assertFalse(list((operation.staging_root.parent / 'native-calls').rglob('command.plist')))
                self.operations.complete_phase(phase, 'a' * 64)

    def test_cancellation_collects_actual_sdk_and_preserves_unknown_device_outcome(self):
        from reproof.android_native_process import run_native_adb
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        original = self.fixture.server.request
        def paused(connection):
            request = original(connection)
            if request == b'host:tport:serial:' + self.config.serial.encode():
                entered.set()
                release.wait(3)
            return request
        self.fixture.server.request = paused
        cancellation = threading.Event()
        def cancel():
            if entered.wait(3):
                cancellation.set()
        cancel_thread = threading.Thread(target=cancel)
        cancel_thread.start()
        self.addCleanup(lambda: cancel_thread.join(4))
        with self.operations.admit(self.f.context, self.fixture.fixture.blobs) as operation:
            client = self.client(operation)
            with self.fixture.phase(operation) as (phase, descriptors):
                result = run_native_adb(self.operations, descriptors, self.guardian(), client,
                    ('shell', 'echo owned'), cancellation=cancellation,
                    deadline_monotonic=time.monotonic() + 5)
                self.assertTrue(entered.is_set())
                self.assertTrue(result.terminated and result.interrupted)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(client.active_processes, 0)
                state = json.loads((operation.staging_root.parent / 'native-calls/state.json').read_bytes())
                self.assertIsNotNone(state['slots']['command'])
                self.operations.complete_phase(phase, 'a' * 64)
            self.assertEqual(self.operations.status(operation.operation_id)['state'], 'native-call-unresolved')
            self.assertGreater(self.fixture.runs.status(operation.operation_id)['reservedBytes'], 0)
        release.set()

    def test_changed_guardian_is_rejected(self):
        from reproof.android_native_process import AndroidGuardianTools
        with self.assertRaises(AndroidOperationError):
            AndroidGuardianTools(self.guardian_path, '0' * 64)

    def test_explicit_dispatcher_runs_pinned_device_commands_on_a_callback_thread(self):
        from reproof.android_native_process import native_dispatcher
        from reproof.repair_android import PinnedAdbDevice
        with self.operations.admit(self.f.context, self.fixture.fixture.blobs) as operation:
            with self.fixture.phase(operation) as (phase, descriptors):
                with native_dispatcher(self.operations, descriptors, self.guardian()) as dispatcher:
                    device = PinnedAdbDevice(self.config.serial, self.config.tools, package=self.config.package,
                        work_root=operation.staging_root, cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic()+10, endpoint=self.config.adb_endpoint,
                        native_dispatcher=dispatcher)
                    self.addCleanup(lambda: device.close(deadline_monotonic=time.monotonic()+2))
                    values = []
                    def callback():
                        try:
                            self.operations.require_native_descriptors(descriptors)
                        except AndroidOperationError:
                            values.append('direct-rejected')
                        values.append(device.shell('echo', 'owned-output'))
                    thread = threading.Thread(target=callback)
                    thread.start()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(values, ['direct-rejected', 'owned-output\n'])
                    self.assertTrue(device.effects_settled)
                    self.assertTrue(device.close(deadline_monotonic=time.monotonic()+2))
                    self.operations.complete_phase(phase, 'a'*64)
                with self.assertRaises(AndroidOperationError):
                    dispatcher.run(self.client(operation), ('shell', 'echo stale'),
                        cancellation=threading.Event(), deadline_monotonic=time.monotonic()+1)

    def test_pinned_instrumentation_is_collected_through_its_native_dispatcher(self):
        from reproof.android_native_process import native_dispatcher
        from reproof.repair_android import PinnedAdbDevice
        from reproof.live.android_live import HELPER
        with self.operations.admit(self.f.context, self.fixture.fixture.blobs) as operation:
            with self.fixture.phase(operation) as (phase, descriptors):
                with native_dispatcher(self.operations, descriptors, self.guardian()) as dispatcher:
                    device = PinnedAdbDevice(self.config.serial, self.config.tools, package=self.config.package,
                        work_root=operation.staging_root, cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic()+10, endpoint=self.config.adb_endpoint,
                        native_dispatcher=dispatcher)
                    self.addCleanup(lambda: device.close(deadline_monotonic=time.monotonic()+2))
                    process = device.start_live_instrumentation(HELPER)
                    self.assertTrue(device.collect_live_instrumentation(process))
                    self.assertTrue(device.effects_settled)
                    state = json.loads((operation.staging_root.parent/'native-calls/state.json').read_bytes())
                    self.assertIsNone(state['slots']['instrumentation'])
                    self.assertGreaterEqual(state['nextSequence'], 3)
                    self.assertTrue(device.close(deadline_monotonic=time.monotonic()+2))
                    self.operations.complete_phase(phase, 'a'*64)

    def test_configured_mobile_adapter_issues_its_dispatcher_from_the_actual_phase(self):
        from reproof.repair_android import AndroidTrustedMobileAdapter
        from reproof.device import DeviceError
        config = replace(self.config, native_guardian=self.guardian())
        operations = AndroidOperationStore(self.fixture.runs, config, self.f.root/'configured-native-operations')
        self.addCleanup(lambda: operations.close(deadline_monotonic=time.monotonic()+2))
        adapter = AndroidTrustedMobileAdapter(config, operations=operations)
        self.addCleanup(lambda: adapter.close(deadline_monotonic=time.monotonic()+2))
        with operations.admit(self.f.context, self.fixture.fixture.blobs) as operation:
            context = replace(self.f.context, _operation_binding=operation)
            scope = self.f.lab.begin_retained_device_scope('device', config.owner, 'configured-native-scope',
                self.f.registration, application_id=config.application_id, build_id=config.original_build_id)
            try:
                handle = scope._reservation._authority_handle
                adapter._scope = scope
                adapter._native_binding = operations.bind_native(operation, context,
                    ownership_generation=handle.generation, host_incarnation=handle._authority.host_incarnation,
                    helper_incarnation=handle.helper_incarnation, provider_incarnation='configured_native_provider')
                adapter._candidate_path = operation.candidate_path
                with adapter._journal_phase(context, 'install') as phase:
                    device = adapter._new_device(threading.Event(), time.monotonic()+5)
                    self.addCleanup(lambda: device.close(deadline_monotonic=time.monotonic()+2))
                    self.assertIsNotNone(device._native_dispatcher)
                    self.assertEqual(device.shell('echo', 'owned-output'), 'owned-output\n')
                    operations.complete_phase(phase, 'a'*64)
                with self.assertRaises(DeviceError):
                    device.shell('echo', 'stale-phase')
                intent = json.loads((operation.staging_root.parent/'intent.json').read_bytes())
                self.assertEqual(intent['configuration']['nativeGuardianDigest'], config.native_guardian.definition_digest)
                self.assertEqual(intent['configuration']['nativeToolOwnershipVersion'],2)
                self.assertEqual(intent['configuration']['fixtureReservationVersion'],1)
            finally:
                self.f.lab.release_retained_device_scope(scope)

    def test_legacy_tool_ownership_cannot_be_adopted_as_the_current_recovery_contract(self):
        from unittest.mock import patch
        config=replace(self.config,native_guardian=self.guardian())
        path=self.f.root/'legacy-ownership-operations'
        original=AndroidOperationStore._configuration_record
        def legacy(store):
            record=original(store);record.pop('nativeToolOwnershipVersion',None);return record
        with patch.object(AndroidOperationStore,'_configuration_record',legacy):
            old=AndroidOperationStore(self.fixture.runs,config,path)
            with old.admit(self.f.context,self.fixture.fixture.blobs) as operation:
                pass
            old.close(deadline_monotonic=time.monotonic()+1)
        current=AndroidOperationStore(self.fixture.runs,config,path,create=False)
        self.addCleanup(lambda:current.close(deadline_monotonic=time.monotonic()+1))
        self.assertEqual(current.status(operation.operation_id)['state'],'record-invalid')
        with self.assertRaises(AndroidOperationError):
            with current.recovery(operation.operation_id,operation.request_digest):
                pass
        self.assertGreater(self.fixture.runs.status(operation.operation_id)['reservedBytes'],0)

    def test_closing_live_instrumentation_collects_sdk_and_keeps_device_outcome_unresolved(self):
        from reproof.android_native_process import native_dispatcher
        from reproof.repair_android import PinnedAdbDevice
        from reproof.live.android_live import HELPER
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.fixture.server.request
        def paused(connection):
            request = original(connection)
            if request == b'host:tport:serial:' + self.config.serial.encode():
                entered.set()
                release.wait(4)
            return request
        self.fixture.server.request = paused
        with self.operations.admit(self.f.context, self.fixture.fixture.blobs) as operation:
            with self.fixture.phase(operation) as (phase, descriptors):
                with native_dispatcher(self.operations, descriptors, self.guardian()) as dispatcher:
                    device = PinnedAdbDevice(self.config.serial, self.config.tools, package=self.config.package,
                        work_root=operation.staging_root, cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic()+10, endpoint=self.config.adb_endpoint,
                        native_dispatcher=dispatcher)
                    self.addCleanup(lambda: device.close(deadline_monotonic=time.monotonic()+2))
                    process = device.start_live_instrumentation(HELPER)
                    self.assertTrue(entered.wait(3))
                    with self.assertRaises(AndroidOperationError):
                        self.operations.complete_phase(phase, 'a'*64)
                    self.assertTrue(device.close(deadline_monotonic=time.monotonic()+3))
                    self.assertIsNotNone(process.poll())
                    self.assertFalse(device.effects_settled)
                    self.assertEqual(dispatcher.active_processes, 0)
                    self.operations.complete_phase(phase, 'a'*64)
            self.assertEqual(self.operations.status(operation.operation_id)['state'], 'native-call-unresolved')
        release.set()

    def test_phase_dispatcher_close_collects_an_inflight_callback(self):
        from reproof.android_native_process import native_dispatcher
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.fixture.server.request
        def paused(connection):
            request = original(connection)
            if request == b'host:tport:serial:' + self.config.serial.encode():
                entered.set()
                release.wait(4)
            return request
        self.fixture.server.request = paused
        results = []
        with self.operations.admit(self.f.context, self.fixture.fixture.blobs) as operation:
            client = self.client(operation)
            with self.fixture.phase(operation) as (_, descriptors):
                with native_dispatcher(self.operations, descriptors, self.guardian()) as dispatcher:
                    def callback():
                        results.append(dispatcher.run(client, ('shell', 'echo owned'),
                            cancellation=threading.Event(), deadline_monotonic=time.monotonic()+10))
                    thread = threading.Thread(target=callback)
                    thread.start()
                    self.addCleanup(lambda: thread.join(5))
                    self.assertTrue(entered.wait(3))
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].terminated and results[0].interrupted, results[0])
                self.assertEqual(client.active_processes, 0)
            self.assertEqual(self.operations.status(operation.operation_id)['state'], 'native-call-unresolved')
        release.set()

    def test_helper_requests_require_a_live_phase_even_after_sdk_work_has_ended(self):
        from reproof.android_native_process import native_dispatcher
        from reproof.repair_android import PinnedAdbDevice
        from reproof.device import DeviceError
        with self.operations.admit(self.f.context, self.fixture.fixture.blobs) as operation:
            with self.fixture.phase(operation) as (phase, descriptors):
                with native_dispatcher(self.operations, descriptors, self.guardian()) as dispatcher:
                    device = PinnedAdbDevice(self.config.serial, self.config.tools, package=self.config.package,
                        work_root=operation.staging_root, cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic()+10, endpoint=self.config.adb_endpoint,
                        native_dispatcher=dispatcher)
                    self.addCleanup(lambda: device.close(deadline_monotonic=time.monotonic()+2))
                    self.assertEqual(device.call_live_helper('/status', {}, token='owned-token', timeout=1), {'alive':True})
                    self.operations.complete_phase(phase, 'a'*64)
            before = len(self.fixture.server.requests)
            with self.assertRaises(DeviceError):
                device.call_live_helper('/status', {}, token='owned-token', timeout=1)
            self.assertEqual(len(self.fixture.server.requests), before)

    def test_phase_close_revokes_an_inflight_helper_callback(self):
        from reproof.android_native_process import native_dispatcher
        from reproof.repair_android import PinnedAdbDevice
        from reproof.device import DeviceError
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.fixture.server.request
        def paused(connection):
            request = original(connection)
            if request == b'tcp:8766':
                entered.set()
                release.wait(4)
            return request
        self.fixture.server.request = paused
        results = []
        with self.operations.admit(self.f.context, self.fixture.fixture.blobs) as operation:
            with self.fixture.phase(operation) as (phase, descriptors):
                with native_dispatcher(self.operations, descriptors, self.guardian()) as dispatcher:
                    device = PinnedAdbDevice(self.config.serial, self.config.tools, package=self.config.package,
                        work_root=operation.staging_root, cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic()+10, endpoint=self.config.adb_endpoint,
                        native_dispatcher=dispatcher)
                    self.addCleanup(lambda: device.close(deadline_monotonic=time.monotonic()+2))
                    def callback():
                        try:
                            results.append(device.call_live_helper('/status', {}, token='owned-token', timeout=3))
                        except DeviceError:
                            results.append('rejected')
                    thread = threading.Thread(target=callback)
                    thread.start()
                    self.addCleanup(lambda: thread.join(5))
                    self.assertTrue(entered.wait(2))
                    with self.assertRaises(AndroidOperationError):
                        self.operations.complete_phase(phase, 'a'*64)
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results, ['rejected'])
                self.assertFalse(device.effects_settled)
            self.assertEqual(self.operations.status(operation.operation_id)['state'], 'native-recovery-required')
        release.set()
