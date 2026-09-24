"""Native client files consume reserved space and remain unresolved after a crash."""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from reproof.adb_endpoint import AdbEndpoint
from reproof.execution.journal import RunStore, RunDenied
from reproof.repair_android_operation import AndroidOperationStore, AndroidOperationError
from tests import test_android_mobile_operation_integration as support
from tests.test_adb_endpoint import OwnedAdbServer, sha


class AndroidNativeCallStorageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.PersistentAndroidAdapterTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.fixture
        temporary = tempfile.TemporaryDirectory(prefix='anc-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.endpoint_root = Path(temporary.name).resolve()
        self.server = OwnedAdbServer(self.endpoint_root, serial=self.f.config.serial)
        self.addCleanup(self.server.close)
        self.config = replace(self.f.config, adb_endpoint=AdbEndpoint(
            self.server.path, sandbox_sha256=sha('/usr/bin/sandbox-exec')))
        self.runs = RunStore(self.f.root / 'native-runs', environment_digest='e' * 64,
                             disk_limit=128 * 1024**2)
        self.operations = AndroidOperationStore(self.runs, self.config, self.f.root / 'native-operations')
        self.addCleanup(lambda: self.operations.close(deadline_monotonic=time.monotonic() + 1))

    @contextmanager
    def phase(self, operation):
        scope = self.f.lab.begin_retained_device_scope('device', self.config.owner,
            'native-call-scope', self.f.registration, application_id=self.config.application_id,
            build_id=self.config.original_build_id)
        try:
            device = scope._reservation._authority_handle
            binding = self.operations.bind_native(operation, self.f.context,
                ownership_generation=device.generation, host_incarnation=device._authority.host_incarnation,
                helper_incarnation=device.helper_incarnation, provider_incarnation='native_call_provider')
            with self.operations.phase(operation, self.f.context, binding, 'install') as phase:
                with self.operations.borrow_native_descriptors(
                    operation, self.f.context, binding, phase, device) as descriptors:
                    yield phase, descriptors
        finally:
            self.f.lab.release_retained_device_scope(scope)

    def command(self):
        from reproof.adb_endpoint import adb_client_sandbox
        work = self.operations.operations / self.f.context.operation_id / 'staging'
        return ('/usr/bin/sandbox-exec', '-p',
            adb_client_sandbox(self.config.tools.adb, work, self.server.path),
            str(self.config.tools.adb), '-L', 'localfilesystem:' + str(self.server.path),
            '-s', self.config.serial, 'shell', 'echo owned')

    def test_native_workspace_is_reserved_before_any_command(self):
        from reproof.android_native_calls import RESERVED_BYTES, validate_workspace
        with self.operations.admit(self.f.context, self.fixture.blobs) as operation:
            intent = json.loads((operation.staging_root.parent / 'intent.json').read_bytes())
            payload = sum(item['bytes'] for item in intent['files'].values())
            self.assertEqual(operation.reserved_bytes, payload + 512 * 1024 + RESERVED_BYTES)
            self.assertEqual(self.runs.status(operation.operation_id)['reservedBytes'], operation.reserved_bytes)
            descriptor = os.open(operation.staging_root.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.assertEqual(validate_workspace(descriptor, intent), 'idle')
            finally:
                os.close(descriptor)

    def test_insufficient_reservation_rejects_before_native_payload_creation(self):
        limited = RunStore(self.f.root / 'limited-native-runs', environment_digest='e' * 64,
                           disk_limit=1024 * 1024)
        operations = AndroidOperationStore(limited, self.config, self.f.root / 'limited-native-operations')
        self.addCleanup(lambda: operations.close(deadline_monotonic=time.monotonic() + 1))
        with self.assertRaises(RunDenied):
            with operations.admit(self.f.context, self.fixture.blobs):
                self.fail('Native storage exceeded its reservation')
        self.assertFalse(list(operations.operations.rglob('command.plist')))

    def test_pending_call_is_bound_and_preserved_for_recovery(self):
        from reproof.android_native_calls import prepare_call
        with self.operations.admit(self.f.context, self.fixture.blobs) as operation:
            with self.phase(operation) as (phase, descriptors):
                call = prepare_call(self.operations, descriptors, self.command(), input_bytes=b'owned-input')
                import plistlib
                request = plistlib.loads((call.work_root / 'request.plist').read_bytes())
                self.assertEqual(request['nativeBindingDigest'], descriptors.binding_digest)
                self.assertEqual(request['ownershipGeneration'], str(descriptors.ownership_generation))
                self.assertEqual(request['contextDigest'], operation.context_digest)
                self.assertEqual((call.work_root / 'stdin.bin').read_bytes(), b'owned-input')
                with self.assertRaises(AndroidOperationError):
                    prepare_call(self.operations, descriptors, self.command(), input_bytes=b'')
                self.operations.complete_phase(phase, 'a' * 64)
            status = self.operations.status(operation.operation_id)
            self.assertEqual(status['state'], 'native-call-unresolved')
            self.assertGreater(self.runs.status(operation.operation_id)['reservedBytes'], 0)
            self.assertTrue((call.work_root / 'request.plist').is_file())
        with self.operations.recovery(operation.operation_id, operation.request_digest) as inspection:
            self.assertEqual(inspection.state, 'native-call-unresolved')

    def test_oversized_or_foreign_commands_leave_workspace_idle(self):
        from reproof.android_native_calls import prepare_call, validate_workspace
        with self.operations.admit(self.f.context, self.fixture.blobs) as operation:
            with self.phase(operation) as (phase, descriptors):
                for command, body in ((self.command(), b'x' * (64 * 1024 + 1)),
                                      (('/bin/sh', '-c', 'echo owned'), b'')):
                    with self.assertRaises(AndroidOperationError):
                        prepare_call(self.operations, descriptors, command, input_bytes=body)
                intent = self.operations._intent(operation.operation_id)
                self.assertEqual(validate_workspace(descriptors.operation_directory_fd, intent), 'idle')
                self.operations.complete_phase(phase, 'a' * 64)

    def test_replaced_native_directory_is_not_adopted_by_status_or_recovery(self):
        with self.operations.admit(self.f.context, self.fixture.blobs) as operation:
            native = operation.staging_root.parent / 'native-calls'
            native.rename(native.with_name('original-native-calls'))
            native.mkdir(mode=0o700)
            self.assertEqual(self.operations.status(operation.operation_id)['state'], 'record-invalid')
        with self.assertRaises(AndroidOperationError):
            with self.operations.recovery(operation.operation_id, operation.request_digest):
                pass

    def test_partial_input_write_keeps_the_intent_and_reserved_bytes(self):
        from reproof import android_native_calls as native
        with self.operations.admit(self.f.context, self.fixture.blobs) as operation:
            with self.phase(operation) as (phase, descriptors):
                write = native._write_bytes
                def interrupted(directory, name, body):
                    if name == 'command.plist':
                        raise OSError('owned storage failure')
                    return write(directory, name, body)
                with patch.object(native, '_write_bytes', side_effect=interrupted):
                    with self.assertRaises(OSError):
                        native.prepare_call(self.operations, descriptors, self.command())
                self.operations.complete_phase(phase, 'a' * 64)
            self.assertEqual(self.operations.status(operation.operation_id)['state'], 'native-call-unresolved')
            self.assertGreater(self.runs.status(operation.operation_id)['reservedBytes'], 0)
            root = operation.staging_root.parent / 'native-calls' / 'command'
            self.assertEqual({path.name for path in root.iterdir()}, {'request.plist'})

    def test_malformed_pending_metadata_is_reported_as_invalid(self):
        from reproof.android_native_calls import prepare_call
        from reproof.execution.wire import canonical
        with self.operations.admit(self.f.context, self.fixture.blobs) as operation:
            with self.phase(operation) as (phase, descriptors):
                call = prepare_call(self.operations, descriptors, self.command())
                self.operations.complete_phase(phase, 'a' * 64)
            path = call.work_root.parent / 'state.json'
            state = json.loads(path.read_bytes())
            state['slots']['command']['phase'] = []
            path.write_bytes(canonical(state))
            self.assertEqual(self.operations.status(operation.operation_id)['state'], 'record-invalid')

    def test_pending_native_call_prevents_staged_apk_discard(self):
        from reproof.android_native_calls import prepare_call
        with self.operations.admit(self.f.context, self.fixture.blobs) as operation:
            with self.phase(operation) as (phase, descriptors):
                binding = descriptors._binding
                prepare_call(self.operations, descriptors, self.command())
                self.operations.complete_phase(phase, 'a' * 64)
            with self.operations.phase(operation, self.f.context, binding, 'cleanup') as cleanup:
                with self.assertRaises(AndroidOperationError):
                    self.operations.discard_staged(operation, cleanup)
            self.assertTrue(operation.candidate_path.is_file())
            self.assertGreater(self.runs.status(operation.operation_id)['reservedBytes'], 0)
