"""Exercise an installed wheel outside the checkout, without test imports."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def _command(argv, *, cwd, timeout=90):
    environment = {key: value for key, value in os.environ.items()
                   if key in {'PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TMPDIR'}}
    environment.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1',
                       PYTHONDONTWRITEBYTECODE='1')
    return subprocess.run(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)


def _builder(exact_executable=None):
    candidates = ((exact_executable,) if exact_executable else
                  (sys.executable, shutil.which('python3.11'), shutil.which('python3.12')))
    for executable in dict.fromkeys(filter(None, candidates)):
        checked = _command([executable, '-I', '-c',
            'from setuptools.command.bdist_wheel import bdist_wheel'], cwd=ROOT)
        if checked.returncode == 0:
            return executable
    raise AssertionError('An installed setuptools wheel builder is required; no dependency is downloaded')


class InstalledDistributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='repro-installed-distribution-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.source = cls.root / 'build-input'; cls.source.mkdir()
        cls.builder = _builder(getattr(cls, 'builder_executable', None))
        # Only the public package and explicitly declared release resources enter
        # this owned build copy. The installer never receives repository tests.
        paths = [p.relative_to(ROOT).as_posix() for p in (ROOT / 'reproof').rglob('*.py')
                 if '__pycache__' not in p.parts and '_assets' not in p.parts]
        paths += ['pyproject.toml', 'README.md', 'LICENSE']
        for name in ('setup.py', 'distribution-resources.json', 'MANIFEST.in'):
            if (ROOT / name).is_file(): paths.append(name)
        if (ROOT / 'distribution-resources.json').is_file():
            paths += json.loads((ROOT / 'distribution-resources.json').read_text())['files']
        else:
            paths += ['live-web/' + name for name in ('index.html', 'app.js', 'boot.js',
                'issue.js', 'video.js', 'pointer.js', 'stream.js', 'styles.css')]
        for name in set(paths):
            destination = cls.source / name; destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        # These are owned synthetic markers, never real credentials or app data.
        for name in ('.env', 'artifacts/private.txt', 'live-ios/build-device/metadata.txt',
                     'android/local.properties', 'reproof/ios_instrumentation_templates/auth.json'):
            path = cls.source / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('EXCLUDED_PRIVATE_MARKER')
        wheels = cls.root / 'wheels'; wheels.mkdir()
        built = _command([cls.builder, '-I', '-c',
            'import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])',
            str(wheels)], cwd=cls.source)
        if built.returncode:
            raise AssertionError('Owned offline wheel build failed: ' + built.stderr[-2000:])
        artifacts = list(wheels.glob('*.whl'))
        if len(artifacts) != 1: raise AssertionError('Exactly one wheel is required')
        cls.wheel = artifacts[0]
        cls.venv = cls.root / 'installation'
        created = _command([sys.executable, '-I', '-m', 'venv', '--without-pip', str(cls.venv)], cwd=cls.root)
        if created.returncode: raise AssertionError('Owned isolated Python environment could not be created')
        cls.python = cls.venv / 'bin/python'
        installed = _command([sys.executable, '-I', '-m', 'pip', '--isolated', '--python', str(cls.python),
            '--disable-pip-version-check', '--no-cache-dir', 'install', '--no-index', '--no-deps',
            '--no-compile', str(cls.wheel)], cwd=cls.root)
        if installed.returncode:
            raise AssertionError('Owned offline wheel installation failed: ' + installed.stderr[-2000:])
        cls.command = cls.venv / 'bin/reproof'
        cls.work = cls.root / 'working-directory'; cls.work.mkdir()

    def test_installed_console_serves_every_runtime_asset(self):
        log_path = self.root / 'console.log'
        with log_path.open('xb') as log:
            process = subprocess.Popen([str(self.command), 'live-serve', '--demo', '--port', '0',
                '--output', str(self.root / 'live-state')], cwd=self.work, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True,
                env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONNOUSERSITE': '1',
                     'PYTHONDONTWRITEBYTECODE': '1'})
            try:
                deadline = time.monotonic() + 10; match = None
                while time.monotonic() < deadline and process.poll() is None:
                    match = re.search(r'Reproof Live: http://127\.0\.0\.1:(\d+)', log_path.read_text())
                    if match: break
                    time.sleep(.02)
                self.assertIsNotNone(match, 'Installed CLI did not start its owned loopback console')
                for path in ('/', '/app.js', '/boot.js', '/issue.js', '/video.js',
                             '/pointer.js', '/stream.js', '/styles.css'):
                    connection = http.client.HTTPConnection('127.0.0.1', int(match[1]), timeout=5)
                    try:
                        connection.request('GET', path)
                        try: response = connection.getresponse()
                        except http.client.RemoteDisconnected:
                            self.fail('Installed console cannot serve runtime asset ' + path)
                        self.assertEqual(response.status, 200, path)
                        self.assertGreater(len(response.read()), 100, path)
                    finally:
                        connection.close()
            finally:
                if process.poll() is None: os.killpg(process.pid, signal.SIGINT)
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)

    def test_installed_check_confirms_native_templates_and_avoids_checkout(self):
        result = _command([str(self.command), 'installation-check'], cwd=self.work)
        self.assertEqual(result.returncode, 0, 'Installed resource check must succeed without the checkout')
        checked = json.loads(result.stdout)
        self.assertEqual(checked['status'], 'ready')
        self.assertEqual(checked['distribution'], 'installed')
        self.assertFalse(checked['actualVM']); self.assertFalse(checked['actualMobile'])
        required = {'live-web/index.html', 'ios/Sample/CounterViewController.swift',
            'android/sdk/src/main/java/io/reproof/sdk/ReproRecorder.kt',
            'native/macos-execution/main.swift', 'guest/reproof_agent/main.py',
            'reproof/ios_instrumentation_templates/RLAutomaticRecorder.swift',
            'tools/kotlin-instrumenter/src/main/java/io/reproof/instrumenter/KotlinInstrumenter.java'}
        self.assertLessEqual(required, set(checked['resources']))
        origin = _command([str(self.python), '-I', '-c',
            'import reproof; print(reproof.__file__)'], cwd=self.work)
        self.assertEqual(origin.returncode, 0)
        self.assertTrue(Path(origin.stdout.strip()).resolve().is_relative_to(self.venv.resolve()))

    def test_installed_ios_launch_contract_and_helper_are_complete(self):
        result = _command([str(self.python), '-I', '-c',
            'from reproof.ios_launch import ApprovedIosLaunch, validate_ios_launch; '
            'from reproof.resources import resource_root; '
            'source=(resource_root()/"live-ios/Tests/LiveControlTests.swift").read_text(); '
            'assert "APPROVED IOS LAUNCH PURE CONTRACT" in source; '
            'assert "approvedLaunchConfiguration" in source; '
            'print("installed iOS launch contract present")'], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        self.assertIn('installed iOS launch contract present', result.stdout)

    def test_installed_source_snapshot_cli_roundtrip_without_checkout(self):
        source = self.root / 'snapshot-input'; source.mkdir()
        (source / 'file.swift').write_bytes(b'synthetic source only')
        selection = self.root / 'source-selection.json'
        selection.write_text(json.dumps({'schemaVersion': 1, 'kind': 'reproof-source-selection',
                                         'paths': ['file.swift']}))
        snapshot = self.root / 'sealed-source'
        output = self.root / 'materialized-source'
        invocations = [
            ['capture', '--source', str(source), '--selection', str(selection), '--output', str(snapshot)],
            ['verify', '--snapshot', str(snapshot)],
            ['materialize', '--snapshot', str(snapshot), '--output', str(output)],
        ]
        results = []
        for arguments in invocations:
            result = _command([str(self.command), 'source-snapshot', *arguments], cwd=self.work)
            self.assertEqual(result.returncode, 0, result.stderr[-1000:])
            results.append(json.loads(result.stdout))
        self.assertEqual(len({result['digest'] for result in results}), 1)
        self.assertEqual((output / 'file.swift').read_bytes(), b'synthetic source only')
        self.assertTrue(all(result['provenance'] == 'unverified-local-content' for result in results))

    def test_installed_ios_snapshot_preparation_without_checkout(self):
        from tests.test_ios_observation import ordinary_project
        source = self.root / 'ios-snapshot-input'
        document = ordinary_project(source)
        selection = self.root / 'ios-snapshot-selection.json'
        selection.write_text(json.dumps({'schemaVersion': 1, 'kind': 'reproof-source-selection',
                                         'paths': document.pop('sourceInputs')}))
        snapshot = self.root / 'ios-original-snapshot'
        result = _command([str(self.command), 'source-snapshot', 'capture', '--source', str(source),
            '--selection', str(selection), '--output', str(snapshot)], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        reference = json.loads(result.stdout)
        document.update(schemaVersion=3, kind='uikit-observation-v3', sourceSnapshot={
            'schemaVersion': 1, 'kind': 'reproof-local-source-snapshot',
            **{key: reference[key] for key in ('digest', 'files', 'bytes')}})
        profile = self.root / 'ios-snapshot-profile.json'; profile.write_text(json.dumps(document))
        prepared = self.root / 'ios-prepared-snapshot'
        result = _command([str(self.command), 'ios-instrument', '--source', str(snapshot),
            '--output', str(prepared), '--profile', str(profile)], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stdout[-1000:] + result.stderr[-1000:])
        self.assertEqual(json.loads(result.stdout)['status'], 'prepared')
        checked = _command([str(self.command), 'source-snapshot', 'verify',
                            '--snapshot', str(prepared / 'snapshot')], cwd=self.work)
        self.assertEqual(checked.returncode, 0, checked.stderr[-1000:])
        receipt = json.loads((prepared / 'instrumentation.json').read_text())
        self.assertEqual(receipt['preparedSnapshot']['digest'], json.loads(checked.stdout)['digest'])
        self.assertFalse(receipt['behaviorVerified'])
        self.assertIn('uikit-observation-v3', (prepared / 'snapshot/data/ReproofInstrumentation/Runtime/RLAutomaticRecorder.swift').read_text())

    def test_installed_local_snapshot_proposal_without_checkout(self):
        from tests.test_project_repair import source_fixture
        from tests.test_local_unified_diff import patch
        project, sources, artifacts = source_fixture()
        project['applications'][0]['platform'] = 'android'
        source = self.root / 'g9-original'; sources.write_new(source)
        artifact = self.root / 'g9-original.apk'; artifact.write_bytes(artifacts.entries[0][1])
        selected = self.root / 'g9-selection.json'
        selected.write_text(json.dumps([path for path, _ in sources.entries]))
        project_file = self.root / 'g9-project.json'; project_file.write_text(json.dumps(project))
        before = dict(sources.entries)[project['editablePaths'][0]].decode()
        diff = self.root / 'g9-proposal.diff'
        diff.write_text(patch(project['editablePaths'][0], before, before.replace('if !ready', 'if ready')))
        program = r'''import hashlib, json, pathlib, sys
from unittest import mock
from reproof.agents import ProjectUnifiedDiffAgent
from reproof.execution.artifacts import BlobSet
from reproof.project_repair import proposal_packet
from reproof.repair_journal import RepairJournal
from reproof.snapshot_repair_source import SnapshotRepairSource
from reproof.source_snapshot import capture
root = pathlib.Path(sys.argv[1]); project = json.loads((root/'g9-project.json').read_text())
paths = json.loads((root/'g9-selection.json').read_text())
with mock.patch('subprocess.Popen', side_effect=AssertionError('no execution')):
    summary = capture(root/'g9-original', paths, root/'g9-snapshot')
    ref = dict(schemaVersion=1, kind='reproof-local-source-snapshot',
               **{key:summary[key] for key in ('digest','files','bytes')})
    source = SnapshotRepairSource(project, root/'g9-snapshot', snapshot_reference=ref,
        protected_paths=['tests/CheckoutTests.swift'], original_artifact=root/'g9-original.apk')
    frozen = source.freeze('original')
    agent = ProjectUnifiedDiffAgent(root/'g9-proposal.diff', base_source_digest=frozen.digest)
    with_journal = RepairJournal(root/'g9-journal')
    try:
        job = with_journal.create(project_id=project['id'], owner_id='owner', issue_id='issue',
            request_id='request', request_digest='a'*64,
            reservation_bytes=source.snapshot_reservation_bytes+2*1024*1024)
        with_journal.claim(job['id'])
        cancellation = with_journal.cancellation(job['id'])
        packet = proposal_packet(project, BlobSet(source.editable_entries(frozen)),
            {'actions':[], 'assertions':[], 'waits':[]}, provider_id=agent.provider_id, external=False)
        packet['sourceDigest'] = frozen.digest
        edits = agent.propose_project(packet, cancellation=cancellation)
        with with_journal.begin_snapshot_output(job['id'], 'candidate',
                reservation_bytes=source.snapshot_reservation_bytes) as output:
            candidate = source.apply(frozen, edits, output.path,
                output_parent_identity=output.parent_identity, cancel=cancellation)
            source.require_candidate(candidate)
            output.write_sidecar('proposal', json.dumps({'verified':False,'candidateSnapshot':candidate.reference}).encode())
            output.complete(candidate.reference)
        with_journal.finish(job['id'], 'proposal-ready', result={'verified':False})
        print(json.dumps({'changed':candidate.digest != frozen.digest,'files':candidate.reference['files'],
                          'verified':False,'candidateKind':with_journal.get(job['id'])['outputs']['candidate']['kind']}))
    finally:
        with_journal.close()
'''
        result = _command([str(self.python), '-I', '-c', program, str(self.root)], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report, {'changed': True, 'files': len(sources.entries), 'verified': False,
                                  'candidateKind': 'source-snapshot-v1'})

    def test_installed_android_snapshot_preparation_without_checkout(self):
        source = self.root / 'android-snapshot-input'; source.mkdir()
        files = {
            'settings.gradle': "pluginManagement {}\ninclude ':app'\n",
            'app/build.gradle': "plugins { id 'com.android.application' }\n",
            'app/src/main/AndroidManifest.xml': '<manifest/>\n',
            'app/src/main/java/example/MainActivity.kt': 'package com.example.inventory\nclass MainActivity\n',
        }
        for name, raw in files.items():
            path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(raw)
        selection = self.root / 'android-snapshot-selection.json'
        selection.write_text(json.dumps({'schemaVersion': 1, 'kind': 'reproof-source-selection', 'paths': sorted(files)}))
        snapshot = self.root / 'android-original-snapshot'
        result = _command([str(self.command), 'source-snapshot', 'capture', '--source', str(source),
            '--selection', str(selection), '--output', str(snapshot)], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        reference = json.loads(result.stdout)
        activity = 'app/src/main/java/example/MainActivity.kt'
        document = {'schemaVersion': 3, 'kind': 'views-observation-v3',
            'package': 'com.example.inventory', 'activity': '.MainActivity',
            'activitySource': activity, 'build': {'task': ':app:assembleDebug',
                'apk': 'app/build/outputs/apk/debug/app-debug-unsigned.apk', 'dsl': 'groovy'},
            'tapTargets': ['save'], 'screenTargets': {'inventory_root': 'inventory'},
            'sourceSnapshot': {'schemaVersion': 1, 'kind': 'reproof-local-source-snapshot',
                **{key: reference[key] for key in ('digest', 'files', 'bytes')}}}
        profile = self.root / 'android-snapshot-profile.json'; profile.write_text(json.dumps(document))
        prepared = self.root / 'android-prepared-snapshot'
        # The installed preparation pipeline and fixed templates are real. Only
        # selected-file PSI analysis is doubled; this lane cannot run a compiler.
        program = """
import json,sys
from pathlib import Path
from unittest.mock import patch
from reproof.android_observation import load_observation_profile
from reproof.build_instrumentation import prepare_build_instrumentation
profile=load_observation_profile(Path(sys.argv[2]))
activity=profile.data['activitySource']
def analyzer(files, name, taps):
    assert list(files)==[activity]
    return {'schemaVersion':1,'activityPath':activity,'files':{activity:'unused'},
            'sites':[{'id':'s123abc','path':activity,'line':2,'target':'save','kind':'tap'}]}
with patch('subprocess.Popen',side_effect=AssertionError('Native execution forbidden')):
    result=prepare_build_instrumentation(Path(sys.argv[1]),profile,Path(sys.argv[3]),analyzer=analyzer)
print(json.dumps({'status':result['status'],'behaviorVerified':result['behaviorVerified']}))
"""
        result = _command([str(self.python), '-I', '-c', program, str(snapshot), str(profile), str(prepared)], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stdout[-1000:] + result.stderr[-1000:])
        self.assertEqual(json.loads(result.stdout), {'status': 'prepared', 'behaviorVerified': False})
        checked = _command([str(self.command), 'source-snapshot', 'verify', '--snapshot', str(prepared / 'snapshot')], cwd=self.work)
        self.assertEqual(checked.returncode, 0, checked.stderr[-1000:])
        embedded = prepared / 'snapshot/data/app/reproof-instrumentation/assets/reproof-observation.json'
        self.assertEqual(json.loads(embedded.read_text())['kind'], 'views-observation-v3')

    def test_changed_installed_asset_is_reported_without_executing_it(self):
        locate = _command([str(self.python), '-I', '-c',
            'from reproof.resources import resource_root; print(resource_root())'], cwd=self.work)
        self.assertEqual(locate.returncode, 0)
        path = Path(locate.stdout.strip()) / 'native/macos-execution/main.swift'
        original = path.read_bytes()
        try:
            path.write_bytes(original + b'\n// owned corruption probe\n')
            result = _command([str(self.command), 'installation-check'], cwd=self.work)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)['status'], 'resource-integrity-failed')
        finally:
            path.write_bytes(original)

    def test_distribution_excludes_local_state_and_credentials(self):
        with zipfile.ZipFile(self.wheel) as archive:
            names = archive.namelist()
            self.assertTrue(names)
            self.assertFalse(any('build-device/' in name or '/artifacts/' in name
                or name.startswith('tests/') or name.endswith(('.env', 'auth.json', 'local.properties')) for name in names))
            self.assertFalse(any(b'EXCLUDED_PRIVATE_MARKER' in archive.read(name) for name in names))
            self.assertFalse(any(name.startswith(('reproof/instrumentation_templates/',
                'reproof/ios_instrumentation_templates/', 'reproof/build_instrumentation_templates/')) for name in names))

    def test_installed_resources_can_be_exported_for_native_builds(self):
        output = self.root / 'native-build-sources'
        result = _command([str(self.command), 'export-resources', '--output-new', str(output)], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'exported')
        self.assertFalse(report['actualVM']); self.assertFalse(report['actualMobile'])
        source = output / 'native/macos-video/Sources/ReproVideo/main.swift'
        self.assertEqual(source.read_bytes(), (ROOT / 'native/macos-video/Sources/ReproVideo/main.swift').read_bytes())
        again = _command([str(self.command), 'export-resources', '--output-new', str(output)], cwd=self.work)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn('new output directory', json.loads(again.stdout)['message'])
        self.assertEqual(source.read_bytes(), (ROOT / 'native/macos-video/Sources/ReproVideo/main.swift').read_bytes())

    def test_installed_kotlin_analyzer_runs_without_writing_the_installation(self):
        source = '''package example
import android.app.Activity
import android.os.Bundle
import android.widget.Button
class ExampleScreen : Activity() {
    override fun onCreate(state: Bundle?) {
        findViewById<Button>(R.id.save).setOnClickListener { println("saved") }
    }
}
'''
        program = ('import json\nfrom reproof.kotlin_instrumenter import instrument_kotlin\n'
            + 'result=instrument_kotlin(' + repr({'Example.kt': source}) + ',"example.ExampleScreen",["save"])\n'
            + 'print(json.dumps({"file":result["activityPath"],"targets":[s["target"] for s in result["sites"]]}))')
        result = _command([str(self.python), '-I', '-c', program], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertEqual(json.loads(result.stdout), {'file': 'Example.kt', 'targets': ['save']})
        check = _command([str(self.command), 'installation-check'], cwd=self.work)
        self.assertEqual(check.returncode, 0)
        self.assertFalse(list(self.venv.glob('lib/python*/site-packages/reproof/_assets/tools/**/build')))

    def test_installed_cli_prepares_explicit_android_inputs_with_existing_buildsrc(self):
        from tests.test_android_profile import profile_document
        source = self.root / 'public-android'
        contents = {
            'settings.gradle.kts': b'pluginManagement {}\ninclude(":app")\n',
            'app/build.gradle.kts': b'plugins { id("com.android.application") }\n',
            'buildSrc/build.gradle.kts': b'plugins { `java-library` }\n',
            'buildSrc/src/main/java/PublicBuild.java': b'public class PublicBuild {}\n',
            'app/src/main/java/example/Stock.kt': b'fun unitsPerItem() = 2\n',
            'app/src/main/java/example/MainActivity.kt': b'''package io.reproof.inventory
import android.app.Activity
import android.os.Bundle
import android.widget.Button
class MainActivity : Activity() {
    override fun onCreate(state: Bundle?) {
        findViewById<Button>(R.id.commit).setOnClickListener { println("saved") }
    }
}
''',
            'app/src/main/assets/catalog.json': b'{"catalog":"owned-public-test"}\n',
        }
        for name, raw in contents.items():
            path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        profile = profile_document(); profile['sourceInputs'] = sorted(contents)
        profile_path = self.root / 'public-android-profile.json'
        profile_path.write_text(json.dumps(profile))
        output = self.root / 'prepared-android'
        result = _command([str(self.command), 'instrument', '--source', str(source),
            '--app-profile', str(profile_path), '--output', str(output)], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
        self.assertEqual(json.loads(result.stdout)['status'], 'prepared')
        for name, raw in contents.items():
            self.assertEqual((source / name).read_bytes(), raw)
            if name not in {'settings.gradle.kts', 'app/build.gradle.kts'}:
                self.assertEqual((output / 'source' / name).read_bytes(), raw)
        self.assertTrue((output / 'source/reproof-build-logic/build.gradle.kts').is_file())
        self.assertFalse(list(self.venv.glob('lib/python*/site-packages/reproof/_assets/tools/**/build')))

    def test_installed_cli_prepares_views_observations_without_fixture_policy(self):
        source = self.root / 'views-android'
        contents = {
            'settings.gradle.kts': 'pluginManagement {}\ninclude(":app")\n',
            'app/build.gradle.kts': 'plugins { id("com.android.application") }\n',
            'app/src/main/AndroidManifest.xml': '<manifest/>\n',
            'app/src/main/java/example/MainActivity.kt': '''package com.example.views
import android.app.Activity
import android.os.Bundle
import android.widget.Button
class MainActivity : Activity() {
    override fun onCreate(state: Bundle?) {
        findViewById<Button>(R.id.save).setOnClickListener { println("saved") }
    }
}
''',
        }
        for name, raw in contents.items():
            path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(raw)
        profile = {'schemaVersion': 2, 'kind': 'views-observation-v2', 'package': 'com.example.views',
            'activity': '.MainActivity', 'build': {'task': ':app:assembleDebug',
            'apk': 'app/build/outputs/apk/debug/app-debug.apk'}, 'sourceInputs': sorted(contents),
            'tapTargets': ['save'], 'screenTargets': {'views_root': 'views'}}
        profile_path = self.root / 'views-profile.json'; profile_path.write_text(json.dumps(profile))
        output = self.root / 'prepared-views'
        result = _command([str(self.command), 'android-instrument', '--source', str(source),
            '--observation-profile', str(profile_path), '--output', str(output)], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
        self.assertEqual(json.loads(result.stdout)['status'], 'prepared')
        metadata = json.loads((output / 'source/app/reproof-instrumentation/assets/reproof-observation.json').read_text())
        self.assertEqual(metadata['kind'], 'views-observation-v2')
        self.assertNotIn('fixture', metadata)
        self.assertFalse((output / 'source/app/reproof-instrumentation/runtime/io/reproof/sdk').exists())
        for name in contents:
            if '/src/' in name:
                self.assertEqual((output / 'source' / name).read_text(), contents[name])

    def test_source_distribution_rebuilds_without_the_original_checkout(self):
        archives = self.root / 'source-archives'; archives.mkdir()
        created = _command([self.builder, '-I', '-c',
            'import sys; from setuptools.build_meta import build_sdist; build_sdist(sys.argv[1])',
            str(archives)], cwd=self.source)
        self.assertEqual(created.returncode, 0, created.stderr[-1000:])
        paths = list(archives.glob('*.tar.gz')); self.assertEqual(len(paths), 1)
        expanded = self.root / 'expanded-source'; expanded.mkdir()
        with tarfile.open(paths[0]) as archive:
            self.assertFalse(any(Path(member.name).name in {'.env', 'auth.json', 'local.properties'}
                                 or '/build-device/' in member.name for member in archive.getmembers()))
            archive.extractall(expanded, filter='data')
        projects = list(expanded.iterdir()); self.assertEqual(len(projects), 1)
        rebuilt = self.root / 'rebuilt-wheels'; rebuilt.mkdir()
        built = _command([self.builder, '-I', '-c',
            'import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])',
            str(rebuilt)], cwd=projects[0])
        self.assertEqual(built.returncode, 0, built.stderr[-1500:])
        wheels = list(rebuilt.glob('*.whl')); self.assertEqual(len(wheels), 1)
        with zipfile.ZipFile(self.wheel) as original, zipfile.ZipFile(wheels[0]) as candidate:
            names = [name for name in original.namelist() if name.startswith('reproof/')]
            self.assertEqual(set(names), {name for name in candidate.namelist() if name.startswith('reproof/')})
            self.assertTrue(all(original.read(name) == candidate.read(name) for name in names))

    def test_stale_unlisted_build_asset_is_never_published(self):
        stale = self.source / 'build/lib/reproof/_assets/stale-private.txt'
        self.assertTrue(stale.parent.is_dir())
        stale.write_text('EXCLUDED_PRIVATE_MARKER')
        output = self.root / 'stale-build-wheels'; output.mkdir()
        try:
            result = _command([self.builder, '-I', '-c',
                'import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])',
                str(output)], cwd=self.source)
            self.assertNotEqual(result.returncode, 0, 'A reused build must reject undeclared staged assets')
            self.assertFalse(list(output.glob('*.whl')))
        finally:
            stale.unlink()

    def test_stale_unlisted_python_module_is_never_published(self):
        stale = self.source / 'build/lib/reproof/unlisted_private_module.py'
        stale.write_text('EXCLUDED_PRIVATE_MARKER = True\n')
        output = self.root / 'stale-module-wheels'; output.mkdir()
        try:
            result = _command([self.builder, '-I', '-c',
                'import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])',
                str(output)], cwd=self.source)
            self.assertNotEqual(result.returncode, 0, 'Undeclared cached Python code must not enter the wheel')
            self.assertFalse(list(output.glob('*.whl')))
        finally:
            stale.unlink()

    def test_newer_cached_python_cannot_replace_current_source(self):
        cached = self.source / 'build/lib/reproof/cli.py'
        original = cached.read_bytes()
        output = self.root / 'modified-module-wheels'; output.mkdir()
        try:
            cached.write_bytes(original + b'\n# EXCLUDED_PRIVATE_MARKER\n')
            os.utime(cached, (time.time() + 3600, time.time() + 3600))
            result = _command([self.builder, '-I', '-c',
                'import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])',
                str(output)], cwd=self.source)
            self.assertEqual(result.returncode, 0, result.stderr[-1000:])
            with zipfile.ZipFile(next(output.glob('*.whl'))) as archive:
                self.assertEqual(archive.read('reproof/cli.py'), (self.source / 'reproof/cli.py').read_bytes())
        finally:
            cached.write_bytes(original)

    def test_rebuild_uses_source_bytes_instead_of_a_newer_build_cache(self):
        cached = self.source / 'build/lib/reproof/_assets/live-web/index.html'
        original = cached.read_bytes()
        output = self.root / 'modified-cache-wheels'; output.mkdir()
        try:
            cached.write_text('OWNED_STALE_WEB_CACHE')
            os.utime(cached, (time.time() + 60, time.time() + 60))
            result = _command([self.builder, '-I', '-c',
                'import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])',
                str(output)], cwd=self.source)
            self.assertEqual(result.returncode, 0, result.stderr[-1000:])
            wheels = list(output.glob('*.whl')); self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as archive:
                self.assertEqual(archive.read('reproof/_assets/live-web/index.html'),
                                 (self.source / 'live-web/index.html').read_bytes())
        finally:
            cached.write_bytes(original)


if __name__ == '__main__': unittest.main()
