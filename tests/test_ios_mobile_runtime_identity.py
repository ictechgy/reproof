from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
import hashlib
import io
import json
import plistlib
import threading
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

from reproof import contracts
from reproof.execution.artifacts import BlobSet
from reproof.ios_device_tools import IOSDeviceToolError
from reproof.ios_mobile_runtime_identity import (
    IOSRuntimeIdentityReader, _read_json_file,
)
from tests import test_ios_mobile_xctest as xctest
from tests.test_ios_observation import ordinary_project
from reproof.ios_instrumentation import validate_ios_auto_profile


class RuntimeIdentityReaderTests(unittest.TestCase):
    def identity(self, **changes):
        value = {'schemaVersion': 1, 'kind': 'ios-runtime-identity',
                 'bundleId': 'com.example.flat', 'buildId': 'build-1234',
                 'runId': '11111111-1111-4111-8111-111111111111',
                 'profileDigest': 'a' * 64, 'startedAtMs': 100}
        value.update(changes)
        return value

    def reader(self):
        reader = object.__new__(IOSRuntimeIdentityReader)
        reader._closed = False
        reader._process_stopped = False
        reader._cleanup_unknown = False
        reader.definition = SimpleNamespace(bundle='com.example.flat',
            identifier='device-id', definition_digest='b' * 64,
            work_root=self.root)
        reader.native_owner = SimpleNamespace(binding_digest='c' * 64,
            operation=SimpleNamespace(context=SimpleNamespace(digest='d' * 64)),
            _check=lambda: None, _runtime_results={})
        reader._queries = SimpleNamespace()
        info = reader.definition.work_root.lstat()
        reader._work_root_identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid)
        reader._pending = {}
        return reader

    def setUp(self):
        self.root = self._temp = tempfile.TemporaryDirectory()
        from pathlib import Path
        self.root = Path(self._temp.name).resolve()

    def tearDown(self):
        self._temp.cleanup()

    def test_bounded_owned_json_reader_rejects_symlink_and_oversize(self):
        path = self.root / 'identity.json'
        path.write_text('{"ok":true}')
        path.chmod(0o600)
        self.assertEqual(_read_json_file(path, 4096), {'ok': True})
        outside = self.root / 'outside.json'; outside.write_text('{}'); outside.chmod(0o600)
        path.unlink(); path.symlink_to(outside)
        with self.assertRaises(Exception): _read_json_file(path, 4096)

    def test_public_observation_has_runtime_grade_and_no_authority(self):
        reader = self.reader()
        payload = {'kind': 'ios-fixed-xctest-launch-v1',
                   'nativeBindingDigest': 'c' * 64,
                   'contextDigest': 'd' * 64,
                   'queryDefinitionDigest': 'b' * 64}
        details = SimpleNamespace(_native_binding_digest='c' * 64,
                                  definition_digest='b' * 64, data={'identifier':'device-id'})
        reader._queries.query = lambda *args, **kwargs: details
        with patch('reproof.ios_mobile_runtime_identity._runtime_payload',
                   return_value=(SimpleNamespace(_check_launch=lambda launch: None), payload,
                                 {'bundleId':'com.example.flat','buildId':'build-1234',
                                  'profileDigest':'a'*64,
                                  'runId':'11111111-1111-4111-8111-111111111111'})), \
                patch.object(reader, '_copy', return_value=(
                    {'info':{'outcome':'success'},'result':{}}, self.identity())):
            result = reader.read(object(), cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+5)
        public = result.public()
        self.assertEqual(public['grade'], 'app-reported-runtime-id')
        self.assertTrue(public['identityConfirmed'])
        self.assertFalse(public['installedArtifactVerified'])
        self.assertFalse(public['deviceCleanupConfirmed'])
        self.assertEqual(public['executionAuthority'], 'none')

    def test_cancelled_read_is_rejected_before_copy(self):
        reader = self.reader(); cancellation = threading.Event(); cancellation.set()
        with patch('reproof.ios_mobile_runtime_identity._runtime_payload') as payload, \
                patch.object(reader, '_copy') as copy:
            with self.assertRaises(Exception):
                reader.read(object(), cancellation=cancellation,
                            deadline_monotonic=time.monotonic()+5)
        payload.assert_not_called(); copy.assert_not_called()


class RealIOSRuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.case = xctest.IOSMobileXCTestTests(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.mode = self.case.root / 'runtime-copy-mode.json'
        self.run_file = self.case.root / 'runtime-copy-launch.json'
        self.outside = self.case.root / 'outside-runtime-identity.json'
        self._embed_runtime_profile()
        self._configure_copy_tool()

    def _embed_runtime_profile(self):
        source = self.case.root / 'ordinary-project'
        profile_document = ordinary_project(source)
        profile_document['applicationId'] = 'com.example.flat'
        profile = validate_ios_auto_profile(profile_document)
        body = self.case.g.c.body
        source_entries = {}
        source_info = {}
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            for entry in archive.infolist():
                source_entries[entry.filename] = archive.read(entry)
                source_info[entry.filename] = entry
        info_name = next(name for name in source_entries
                         if name.endswith('/Info.plist') and '/Frameworks/' not in name
                         and '/PlugIns/' not in name)
        info = plistlib.loads(source_entries[info_name])
        info.update(ReproAutoProfile=profile.data, ReproAutoProfileDigest=profile.digest,
                    ReproRuntimeIdentitySchemaVersion=1, ReproBuildID='build-1234')
        source_entries[info_name] = plistlib.dumps(info)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in source_entries.items():
                archive.writestr(source_info[name], value)
        body = output.getvalue()
        self.case.g.c.body = body
        self.case.g.c.artifacts = BlobSet((('candidate.ipa', body),))
        self.case.g.c.context = replace(self.case.g.c.context,
            artifact_digest=hashlib.sha256(body).hexdigest())

    def _configure_copy_tool(self):
        self.case.g.write_tool(f"""
if args[:3] == ['device','copy','from']:
    mode=json.loads(pathlib.Path({str(self.mode)!r}).read_bytes()).get('mode','valid')
    identity_destination=pathlib.Path(args[args.index('--destination')+1])
    if mode == 'symlink':
        identity_destination.symlink_to(pathlib.Path({str(self.outside)!r}))
    elif mode == 'missing':
        pass
    elif mode == 'oversized':
        identity_destination.write_bytes(b'x'*4097)
    else:
        runtime=json.loads(pathlib.Path({str(self.run_file)!r}).read_bytes())
        identity_destination.write_text(json.dumps({{'schemaVersion':1,'kind':'ios-runtime-identity',
            'bundleId':runtime['bundleId'],'buildId':runtime['buildId'],
            'runId':runtime['runId'],'profileDigest':runtime['profileDigest'],'startedAtMs':1}}))
    if mode == 'extra':
        identity_destination.parent.joinpath('unknown-runtime-file').write_text('extra')
""")
        self.mode.write_text(json.dumps({'mode':'valid'}))
        selected = replace(self.case.operations.definition,
            query_definition_digest=self.case.g.definition().definition_digest)
        from reproof.ios_mobile_operation import IOSMobileOperationStore
        self.case.operations = IOSMobileOperationStore(self.case.g.c.runs, selected,
            self.case.root / 'runtime-identity-operations')
        self.case.addCleanup(self.case.operations.close)

    @contextmanager
    def running(self):
        with self.case.owned() as (owner, runner):
            launch = self.case.prepare(runner)
            self.run_file.write_text(json.dumps(launch.payload['runtimeIdentity']))
            permit = self.case.permit(launch)
            session = self.case.start(runner, launch, permit)
            self.case.g.wait_for(self.case.marker.exists)
            reader = IOSRuntimeIdentityReader(self.case.g.definition(), owner)
            try:
                yield owner, runner, launch, session, reader
            finally:
                self.case.release.write_text('finish runtime identity')
                session.wait(deadline_monotonic=time.monotonic()+5)
                reader.close()

    def test_real_owned_launch_reads_and_registers_runtime_identity(self):
        with self.running() as (owner, _runner, launch, _session, reader):
            observation = reader.read(launch, cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+8)
            public = observation.public()
            self.assertEqual(public['bundleId'], 'com.example.flat')
            self.assertEqual(public['buildId'], 'build-1234')
            self.assertEqual(public['grade'], 'app-reported-runtime-id')
            self.assertTrue(public['identityConfirmed'])
            self.assertFalse(public['installedArtifactVerified'])
            self.assertFalse(public['deviceCleanupConfirmed'])
            self.assertEqual(owner._runtime_results[contracts.digest(launch.payload)], observation)
            self.assertNotIn(self.case.g.c.selected.udid, json.dumps(public)+repr(observation))
            self.assertNotIn(str(self.case.root), json.dumps(public)+repr(observation))

    def test_delayed_launch_marker_replaces_missing_copy(self):
        self._delayed_marker('missing')

    def test_delayed_launch_marker_replaces_stale_copy(self):
        self._delayed_marker('stale')

    def _delayed_marker(self, mode):
            with self.running() as (owner, _runner, launch, _session, reader):
                valid = json.dumps(launch.payload['runtimeIdentity'])
                if mode == 'missing':
                    self.mode.write_text(json.dumps({'mode': 'missing'}))
                else:
                    self.run_file.write_text(json.dumps(dict(launch.payload['runtimeIdentity'],
                        runId='22222222-2222-4222-8222-222222222222')))
                def publish():
                    self.run_file.write_text(valid)
                    self.mode.write_text(json.dumps({'mode': 'valid'}))
                timer = threading.Timer(.35, publish)
                timer.start()
                try:
                    observed = reader.read(launch, cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic()+5)
                finally:
                    timer.join()
                self.assertEqual(observed.run_id, launch.payload['runtimeIdentity']['runId'])
                self.assertFalse(reader._pending)
                self.assertEqual(reader.active_processes, 0)
                self.assertIs(owner._runtime_results[contracts.digest(launch.payload)], observed)

    def test_copied_launch_and_stale_runtime_fields_are_rejected(self):
        with self.running() as (_owner, _runner, launch, _session, reader):
            with self.assertRaises(IOSDeviceToolError):
                reader.read(replace(launch), cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic()+8)
            for field, value in (('runId','22222222-2222-4222-8222-222222222222'),
                                 ('buildId','wrong-build'), ('profileDigest','0'*64)):
                with self.subTest(field=field):
                    changed = dict(launch.payload['runtimeIdentity'], **{field:value})
                    self.run_file.write_text(json.dumps(changed))
                    with self.assertRaises(IOSDeviceToolError):
                        reader.read(launch, cancellation=threading.Event(),
                            deadline_monotonic=time.monotonic()+8)

    def test_oversized_symlink_and_unknown_scratch_are_rejected_and_retryable(self):
        with self.running() as (_owner, _runner, launch, _session, reader):
            for mode in ('oversized', 'symlink'):
                with self.subTest(mode=mode):
                    self.mode.write_text(json.dumps({'mode':mode}))
                    with self.assertRaises(IOSDeviceToolError):
                        reader.read(launch, cancellation=threading.Event(),
                            deadline_monotonic=time.monotonic()+8)
            self.mode.write_text(json.dumps({'mode':'extra'}))
            with self.assertRaises(IOSDeviceToolError):
                reader.read(launch, cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic()+8)
            self.assertFalse(reader.close(deadline_monotonic=time.monotonic()+1))
            for work in tuple(reader._pending):
                for item in tuple(work.iterdir()):
                    if item.name == 'unknown-runtime-file' or item.is_symlink():
                        item.unlink()
            self.assertTrue(reader.close(deadline_monotonic=time.monotonic()+3))

    def test_cancellation_is_rejected_before_runtime_copy(self):
        with self.running() as (_owner, _runner, launch, _session, reader):
            cancelled = threading.Event(); cancelled.set()
            with self.assertRaises(IOSDeviceToolError):
                reader.read(launch, cancellation=cancelled,
                    deadline_monotonic=time.monotonic()+8)

    def test_closed_xctest_cannot_issue_new_runtime_evidence(self):
        with self.running() as (owner, _runner, launch, session, reader):
            self.case.release.write_text('finish before observation')
            self.assertTrue(session.wait(deadline_monotonic=time.monotonic()+5))
            with patch.object(reader._queries, 'query') as query:
                with self.assertRaises(IOSDeviceToolError):
                    reader.read(launch, cancellation=threading.Event(),
                        deadline_monotonic=time.monotonic()+8)
            query.assert_not_called()
            self.assertNotIn(contracts.digest(launch.payload), owner._runtime_results)


if __name__ == '__main__': unittest.main()
