from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from reproof import contracts
from reproof.execution.artifacts import BlobSet
from reproof.execution.journal import RunStore, RunDenied
from reproof.repair_signing import SigningContext
from reproof.ios_signing_inputs import IOSSigningDefinition, IOSSigningIdentity, IOSSigningOwnerTools
from tests import test_ios_artifact_transfer as fixtures


class IOSSigningOperationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.IOSArtifactTransferTests(methodName='runTest'); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.app = self.fixture.make_flat_app()
        self.archive = self.fixture.make_ipa(self.app, self.root/'source.ipa')
        self.blobs = BlobSet((('candidate.ipa', self.archive.read_bytes()),))
        self.identity = IOSSigningIdentity('owned-key', 'ios_app', 'OWNEDTEAM1',
            (b'\x30\x0a\x04\x08'+os.urandom(8),))
        self.definition = IOSSigningDefinition(self.identity, 'owned-profiles',
            {'.': {'bundleId': 'com.example.flat', 'entitlements': {}},
             'Frameworks/FlatKit.framework': {'bundleId': 'com.example.flatkit', 'entitlements': {}}},
            {'.': {'cms': b'owned unverified profile', 'profileDigest': 'a'*64}})
        sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        # Staging/recovery tests never execute these tool stand-ins.
        self.tools = IOSSigningOwnerTools(Path('/usr/bin/true'), sha('/usr/bin/true'),
            Path('/usr/bin/true'), sha('/usr/bin/true'), sha('/usr/bin/sandbox-exec'))
        self.context = SigningContext('owned-sign', 'a'*64, 'b'*64, 'ios_app', 'c'*64,
            hashlib.sha256(self.archive.read_bytes()).hexdigest(), 'd'*64, 'e'*48)
        self.request = 'f'*64
        from reproof.ios_signing_operation import IOSSigningOperationStore
        self.run_store = RunStore(self.root/'runs', environment_digest='a'*64, disk_limit=4*1024**3)
        self.operations = IOSSigningOperationStore(self.run_store, self.tools, self.definition, self.root/'signing')
        self.addCleanup(self.operations.close)

    def test_capacity_is_reserved_before_staging_and_original_is_preserved(self):
        with self.operations.admit(self.context, self.request) as operation:
            row = self.run_store.status(self.context.operation_id)
            self.assertGreaterEqual(row['reservedBytes'], 1024**3)
            staged = self.operations.stage(operation, self.blobs)
            self.assertEqual(staged.manifest['applicationId'], 'com.example.flat')
            self.assertEqual(self.archive.read_bytes(), self.blobs.entries[0][1])
            self.assertTrue((self.operations.operation_root(self.context.operation_id)/'incoming.ipa').is_file())
        self.assertEqual(self.run_store.status(self.context.operation_id)['state'], 'quarantined')

    def test_restart_cleanup_is_live_one_use_and_can_only_fail_or_cancel(self):
        with self.operations.admit(self.context, self.request) as operation:
            self.operations.stage(operation, self.blobs)
        from reproof.ios_signing_operation import IOSSigningOperationStore
        restarted = IOSSigningOperationStore(self.run_store, self.tools, self.definition,
            self.root/'signing', create=False)
        self.addCleanup(restarted.close)
        with restarted.recovery(self.context.operation_id, self.request) as capability:
            with self.assertRaises(RunDenied):
                self.run_store.finish_signing_recovery(replace(capability), authority=restarted)
            result = self.run_store.finish_signing_recovery(capability, authority=restarted)
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['reservedBytes'], 0)
            with self.assertRaises(RunDenied):
                self.run_store.finish_signing_recovery(capability, authority=restarted)
        self.assertFalse((restarted.operation_root(self.context.operation_id)/'App.app').exists())

    def test_live_native_lock_or_replaced_directory_never_releases_capacity(self):
        with self.operations.admit(self.context, self.request) as operation:
            self.operations.stage(operation, self.blobs)
        root = self.operations.operation_root(self.context.operation_id)
        descriptor = os.open(root/'owner.lock', os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Exception):
                with self.operations.recovery(self.context.operation_id, self.request): pass
        finally:
            os.close(descriptor)
        self.assertGreater(self.run_store.status(self.context.operation_id)['reservedBytes'], 0)
        (root/'App.app').rename(root/'Original.app')
        (root/'App.app').mkdir(mode=0o700)
        marker = root/'App.app'/'preserve'; marker.write_bytes(b'unrelated owned data')
        with self.assertRaises(Exception):
            with self.operations.recovery(self.context.operation_id, self.request): pass
        self.assertEqual(marker.read_bytes(), b'unrelated owned data')
        self.assertGreater(self.run_store.status(self.context.operation_id)['reservedBytes'], 0)

    def test_cancelled_recovery_remains_cancelled_and_unknown_run_files_block_release(self):
        with self.operations.admit(self.context, self.request): pass
        marker = self.run_store.root/'runs'/self.context.operation_id/'termination.json'
        marker.write_bytes(b'{}')
        self.run_store.cancel(self.context.operation_id, self.request)
        with self.operations.recovery(self.context.operation_id, self.request) as capability:
            with self.assertRaises(RunDenied):
                self.run_store.finish_signing_recovery(capability, authority=self.operations)
        self.assertTrue(marker.exists())
        marker.unlink()
        with self.operations.recovery(self.context.operation_id, self.request) as capability:
            result = self.run_store.finish_signing_recovery(capability, authority=self.operations)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result['reservedBytes'], 0)

    def test_wrong_context_artifact_and_configuration_cannot_adopt_an_operation(self):
        with self.operations.admit(self.context, self.request) as operation:
            with self.assertRaises(Exception):
                self.operations.stage(replace(operation), self.blobs)
            with self.assertRaises(Exception):
                self.operations.stage(operation, BlobSet((('candidate.ipa', b'changed'),)))
        from reproof.ios_signing_operation import IOSSigningOperationStore
        other = IOSSigningDefinition(self.identity, 'different-reference', self.definition.bundle_policies,
            {'.': {'cms': b'owned unverified profile', 'profileDigest': 'a'*64}})
        with self.assertRaises(Exception):
            IOSSigningOperationStore(self.run_store, self.tools, other, self.root/'signing', create=False)

    def test_actual_parent_exit_during_extraction_recovers_reserved_private_data(self):
        script = r'''
from pathlib import Path
import hashlib,os,sys
from contextlib import contextmanager
from reproof.execution.artifacts import BlobSet
from reproof.execution.journal import RunStore
from reproof.repair_signing import SigningContext
from reproof.ios_signing_inputs import IOSSigningDefinition,IOSSigningIdentity,IOSSigningOwnerTools
from reproof import ios_signing_operation as module
root=Path(sys.argv[1]); archive=root/'source.ipa'; body=archive.read_bytes()
identity=IOSSigningIdentity('owned-key','ios_app','OWNEDTEAM1',(bytes.fromhex(sys.argv[2]),))
definition=IOSSigningDefinition(identity,'owned-profiles',
 {'.':{'bundleId':'com.example.flat','entitlements':{}},
  'Frameworks/FlatKit.framework':{'bundleId':'com.example.flatkit','entitlements':{}}},
 {'.':{'cms':b'owned unverified profile','profileDigest':'a'*64}})
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
tools=IOSSigningOwnerTools(Path('/usr/bin/true'),sha('/usr/bin/true'),Path('/usr/bin/true'),
 sha('/usr/bin/true'),sha('/usr/bin/sandbox-exec'))
store=RunStore(root/'runs',environment_digest='a'*64,disk_limit=4*1024**3,create=False)
operations=module.IOSSigningOperationStore(store,tools,definition,root/'signing',create=False)
context=SigningContext('owned-sign','a'*64,'b'*64,'ios_app','c'*64,sha(archive),'d'*64,'e'*48)
original=module._opened_ipa_contents
@contextmanager
def interrupted(*args,**kwargs):
 with original(*args,**kwargs): os._exit(73)
 yield
module._opened_ipa_contents=interrupted
with operations.admit(context,'f'*64) as operation:
 operations.stage(operation,BlobSet((('candidate.ipa',body),)))
raise SystemExit(74)
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.root), self.identity.certificate_chain[0].hex()],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(result.returncode, 73)
        self.assertEqual(self.run_store.status(self.context.operation_id)['state'], 'admitted')
        root = self.operations.operation_root(self.context.operation_id)
        self.assertTrue((root/'transfer/app/Info.plist').exists())
        with self.operations.recovery(self.context.operation_id, self.request) as capability:
            result = self.run_store.finish_signing_recovery(capability, authority=self.operations)
        self.assertEqual(result['reservedBytes'], 0)
        self.assertEqual(list((root/'transfer').iterdir()), [])

    def test_close_waits_for_a_late_staging_callback_after_admission_exits(self):
        from reproof import ios_signing_operation as module
        original = module._opened_ipa_contents
        entered, release = threading.Event(), threading.Event()
        failures = []
        def delayed(*args, **kwargs):
            entered.set(); release.wait(5)
            return original(*args, **kwargs)
        def stage(operation):
            try: self.operations.stage(operation, self.blobs)
            except Exception as error: failures.append(type(error).__name__)
        with mock.patch.object(module, '_opened_ipa_contents', side_effect=delayed):
            with self.operations.admit(self.context, self.request) as operation:
                thread = threading.Thread(target=stage, args=(operation,)); thread.start()
                self.assertTrue(entered.wait(2))
            try:
                self.assertFalse(self.operations.close(deadline_monotonic=time.monotonic()+.02))
                self.assertGreater(self.run_store.status(self.context.operation_id)['reservedBytes'], 0)
            finally:
                release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(failures)
        self.assertTrue(self.operations.close(deadline_monotonic=time.monotonic()+1))

    def test_recovery_retry_after_run_directory_removal_preserves_reservation(self):
        with self.operations.admit(self.context, self.request): pass
        with self.operations.recovery(self.context.operation_id, self.request) as capability:
            with mock.patch.object(self.run_store, '_write', side_effect=RunDenied('owned interrupted commit')):
                with self.assertRaises(RunDenied):
                    self.run_store.finish_signing_recovery(capability, authority=self.operations)
        self.assertGreater(self.run_store.status(self.context.operation_id)['reservedBytes'], 0)
        self.assertFalse((self.run_store.root/'runs'/self.context.operation_id).exists())
        with self.operations.recovery(self.context.operation_id, self.request) as capability:
            result = self.run_store.finish_signing_recovery(capability, authority=self.operations)
        self.assertEqual(result['reservedBytes'], 0)

    def test_missing_staged_app_and_rebound_input_cannot_be_reported_clean(self):
        with self.operations.admit(self.context, self.request) as operation:
            self.operations.stage(operation, self.blobs)
        root = self.operations.operation_root(self.context.operation_id)
        moved = self.root/'moved-owned-app'
        (root/'App.app').rename(moved)
        with self.assertRaises(Exception):
            with self.operations.recovery(self.context.operation_id, self.request): pass
        self.assertGreater(self.run_store.status(self.context.operation_id)['reservedBytes'], 0)
        moved.rename(root/'App.app')
        (root/'incoming.ipa').write_bytes(b'changed original')
        state_path = root/'state.json'; state = json.loads(state_path.read_bytes())
        state['inputDigest'] = hashlib.sha256(b'changed original').hexdigest()
        state['inputBytes'] = len(b'changed original')
        state_path.write_text(json.dumps(state))
        with self.assertRaises(Exception):
            with self.operations.recovery(self.context.operation_id, self.request): pass
        self.assertTrue((root/'App.app').is_dir())
        self.assertGreater(self.run_store.status(self.context.operation_id)['reservedBytes'], 0)

    def test_actual_native_owner_blocks_recovery_until_its_parent_pipe_closes(self):
        from reproof.resources import read_resource
        native = self.root/'native'; native.mkdir(mode=0o700)
        for name in ('main.c', 'ownership.h'):
            (native/name).write_bytes(read_resource('native/ios-signing-owner/'+name))
        binary = native/'owner'
        sdk = subprocess.run(['xcrun','--sdk','macosx','--show-sdk-path'],capture_output=True,text=True,check=True).stdout.strip()
        built = subprocess.run(['/usr/bin/clang', '-std=c11', '-fblocks', '-mmacosx-version-min=15.0',
            '-isysroot', sdk, str(native/'main.c'), '-framework', 'Security', '-framework', 'CoreFoundation',
            '-o', str(binary)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(built.returncode, 0)
        process = None; writer = None; descriptors = []
        try:
            with self.operations.admit(self.context, self.request) as operation:
                self.operations.stage(operation, self.blobs)
                root = self.operations.operation_root(operation.operation_id)
                config = {'schemaVersion': '1', 'mode': 'liveness-probe', 'operationId': operation.operation_id,
                    'requestDigest': self.request, 'contextDigest': self.context.digest,
                    'scopeDigest': self.operations.scope_digest, 'definitionDigest': self.operations.definition_digest,
                    'workPath': str(root), 'appRelativePath': 'App.app', 'certificateSha256': self.identity.certificate_sha256,
                    'teamId': self.identity.team_id, 'certificateChain': [], 'codeObjects': []}
                (root/'request.plist').write_bytes(plistlib.dumps(config, fmt=plistlib.FMT_BINARY))
                reader, writer = os.pipe(); descriptors.append(reader)
                def opened(name, flags):
                    fd = os.open(root/name, flags | os.O_NOFOLLOW); descriptors.append(fd); return fd
                directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW); descriptors.append(directory)
                producer = opened('producer.lock', os.O_RDWR); phase = opened('owner.lock', os.O_RDWR)
                fcntl.flock(producer, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(phase, fcntl.LOCK_EX | fcntl.LOCK_NB)
                args = [opened('request.plist', os.O_RDONLY), directory, producer, phase, reader,
                        0, 0, opened('start.json', os.O_RDWR), opened('termination.json', os.O_RDWR)]
                process = subprocess.Popen([str(binary), *map(str, args)],
                    pass_fds=tuple(fd for fd in args if fd >= 3), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=root)
                for fd in descriptors: os.close(fd)
                descriptors.clear()
                deadline = time.monotonic()+5
                while time.monotonic() < deadline and not (root/'start.json').stat().st_size:
                    self.assertIsNone(process.poll()); time.sleep(.01)
                self.assertGreater((root/'start.json').stat().st_size, 0)
            with self.assertRaises(Exception):
                with self.operations.recovery(self.context.operation_id, self.request): pass
            self.assertTrue((root/'App.app').exists())
            self.assertGreater(self.run_store.status(self.context.operation_id)['reservedBytes'], 0)
            os.close(writer); writer = None
            self.assertEqual(process.wait(timeout=3), 75)
            with self.operations.recovery(self.context.operation_id, self.request) as capability:
                result = self.run_store.finish_signing_recovery(capability, authority=self.operations)
            self.assertEqual(result['reservedBytes'], 0)
        finally:
            if writer is not None: os.close(writer)
            for fd in descriptors: os.close(fd)
            if process is not None and process.poll() is None:
                process.kill(); process.wait(timeout=3)
