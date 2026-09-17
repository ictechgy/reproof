"""Explicit Views observations, independent of fixtures and repair authorization."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import zipfile

from .android_profile import relative_path
from .android_sources import freeze_source, validate_source_inputs
from .core import ContractError, digest, require
from .execution.artifacts import ArtifactError, open_regular
from .storage import MAX_APK, _unique_object

KIND = 'views-observation-v2'
APK_PROFILE = 'assets/reproloop-observation.json'
MAX_PROFILE = 256 * 1024
_ID = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,127}\Z')


@dataclass(frozen=True)
class AndroidObservationProfile:
    _json: str

    @property
    def data(self):
        return json.loads(self._json)

    @property
    def digest(self):
        return digest(self.data)

    @property
    def component_name(self):
        package, activity = self.data['package'], self.data['activity']
        qualified = package + activity if activity.startswith('.') else (
            activity if '.' in activity else package + '.' + activity)
        short = qualified[len(package):] if qualified.startswith(package + '.') else qualified
        return package + '/' + short


def is_observation_profile(profile):
    return isinstance(profile, AndroidObservationProfile)


def validate_observation_profile(document):
    required = {'schemaVersion', 'kind', 'package', 'activity', 'build',
                'sourceInputs', 'tapTargets', 'screenTargets'}
    require(type(document) is dict and required <= set(document)
        and set(document) <= required | {'instrumentation'}
        and type(document['schemaVersion']) is int and document['schemaVersion'] == 2
        and document['kind'] == KIND, 'Unsupported configured Views profile')
    package, activity = document['package'], document['activity']
    require(type(package) is str and len(package) <= 180
        and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', package)
        and package not in {'io.reproloop.live', 'io.reproloop.driver'}, 'Invalid Views application identity')
    require(type(activity) is str and len(activity) <= 240
        and re.fullmatch(r'\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*', activity),
        'Invalid Views activity')
    build = document['build']
    require(type(build) is dict and set(build) == {'task', 'apk'}
        and type(build['task']) is str
        and re.fullmatch(r':(?:[A-Za-z][A-Za-z0-9_-]*:)*assemble(?:[A-Za-z][A-Za-z0-9]*)?Debug', build['task']),
        'Select one Views Debug assemble task')
    module = PurePosixPath(*build['task'].split(':')[1:-1])
    relative_path(build['apk'], '.apk')
    require(build['apk'].startswith((module / 'build/outputs/apk').as_posix() + '/'),
        'Select the APK output of the configured module')
    inputs = validate_source_inputs(document['sourceInputs'])
    require({'settings.gradle.kts', (module / 'build.gradle.kts').as_posix(),
             (module / 'src/main/AndroidManifest.xml').as_posix()} <= set(inputs),
        'Missing required Views public build inputs')
    taps, screens = document['tapTargets'], document['screenTargets']
    require(type(taps) is list and 1 <= len(taps) <= 64
        and all(type(value) is str and _ID.fullmatch(value) for value in taps)
        and len(set(taps)) == len(taps), 'Invalid Views tap targets')
    require(type(screens) is dict and 1 <= len(screens) <= 64
        and all(type(key) is str and _ID.fullmatch(key) and type(value) is str and _ID.fullmatch(value)
                for key, value in screens.items())
        and len(set(screens.values())) == len(screens) and not set(taps) & set(screens),
        'Invalid Views screen targets')
    instrumentation = document.get('instrumentation')
    if 'instrumentation' in document:
        require(type(instrumentation) is dict and set(instrumentation) == {'kind', 'sites'}
            and instrumentation['kind'] == 'android_asm_v1', 'Invalid Views instrumentation contract')
        sites = instrumentation['sites']
        require(type(sites) is list and len(sites) == len(taps), 'Every Views tap requires one source site')
        ids, targets, lines, paths = set(), set(), set(), set()
        prefix = (module / 'src/main').as_posix() + '/'
        for site in sites:
            require(type(site) is dict and set(site) == {'id', 'path', 'line', 'target', 'kind'}
                and site['kind'] == 'tap' and type(site['id']) is str
                and re.fullmatch(r's[0-9a-f]{1,80}', site['id'])
                and type(site['line']) is int and 1 <= site['line'] <= 1_000_000
                and type(site['target']) is str and site['target'] in taps, 'Invalid Views source site')
            relative_path(site['path'], '.kt')
            require(site['path'] in inputs and any(site['path'].startswith(prefix + folder + '/')
                for folder in ('java', 'kotlin')), 'Unbound Views source site')
            ids.add(site['id']); targets.add(site['target']); lines.add(site['line']); paths.add(site['path'])
        require(len(ids) == len(targets) == len(lines) == len(taps) and len(paths) == 1,
                'Ambiguous Views source sites')
    else:
        require(not any('reproloop-instrumentation' in name.split('/') or
            'reproloop-build-logic' in name.split('/') or name.endswith('/' + APK_PROFILE)
            for name in inputs), 'Reserved Views instrumentation input')
    raw = json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False)
    require(len(json.dumps(document, indent=2).encode()) + 1 <= MAX_PROFILE, 'Views profile size limit exceeded')
    return AndroidObservationProfile(raw)


def load_observation_profile(path):
    path = Path(path).absolute()
    require(path.suffix == '.json', 'Select a public Views profile JSON file')
    try:
        raw = dict(freeze_source(path.parent, [path.name]).entries)[path.name]
        require(len(raw) <= MAX_PROFILE, 'Views profile size limit exceeded')
        return validate_observation_profile(json.loads(raw, object_pairs_hook=_unique_object))
    except (ValueError, UnicodeError, RecursionError):
        raise ContractError('Invalid Views profile JSON') from None


def profile_from_apk(apk):
    """Read only the bounded embedded adapter declaration, without extracting files."""
    apk = Path(apk).absolute()
    require(apk.suffix == '.apk', 'Select a public APK artifact')
    try:
        descriptor = open_regular(apk.parent, apk.name)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(before.st_nlink == 1 and 0 < before.st_size <= MAX_APK,
                    'Invalid Views APK input')
            with zipfile.ZipFile(stream) as archive:
                matches = [entry for entry in archive.infolist() if entry.filename == APK_PROFILE]
                if not matches:
                    return None
                require(len(matches) == 1 and 0 < matches[0].file_size <= MAX_PROFILE
                    and not matches[0].flag_bits & 1, 'Invalid embedded Views profile')
                with archive.open(matches[0]) as member:
                    raw = member.read(MAX_PROFILE + 1)
                require(len(raw) == matches[0].file_size, 'Invalid embedded Views profile size')
            after = os.fstat(stream.fileno())
            require((before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink),
                    'Views APK changed during inspection')
        profile = validate_observation_profile(json.loads(raw, object_pairs_hook=_unique_object))
        require('instrumentation' in profile.data, 'Views APK requires its prepared observation profile')
        return profile
    except (ArtifactError, OSError, ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError, RecursionError):
        raise ContractError('Views APK observation metadata is invalid') from None


def render_runtime_config(profile, sites):
    def literal(value):
        return json.dumps(value, ensure_ascii=False).replace('$', '\\$')
    return ('package io.reproloop.autotrace\n\ninternal object ReproConfig {\n'
        '    const val RECORD_MODE = "observe"\n'
        f'    const val APPLICATION_ID = {literal(profile.data["package"])}\n'
        f'    const val PROFILE_DIGEST = "{profile.digest}"\n'
        '    const val APP_LOGS_ENABLED = true\n'
        f'    const val SCREEN_TARGETS_JSON = {literal(json.dumps(profile.data["screenTargets"], separators=(",", ":")))}\n'
        f'    const val SITES_JSON = {literal(json.dumps(sites, separators=(",", ":")))}\n' + '}\n')


def build_observation_app(source, output, *, profile, gradle, java_home, sdk_home, timeout=300):
    """Build trusted explicit Debug inputs; device execution is a separate operation."""
    from .android_artifact import stage_apk
    from .instrumentation import MARKER, validate_instrumented_source
    from .repair import build_android, snapshot_source
    from .storage import write_json
    profile = validate_observation_profile(profile.data if is_observation_profile(profile) else profile)
    source, output = Path(source).absolute(), Path(output).absolute()
    require(source.is_dir() and not source.is_symlink() and not output.exists() and not output.is_symlink()
        and not output.resolve().is_relative_to(source.resolve()), 'Use a new separate Views build output')
    prepared = 'instrumentation' in profile.data
    require((source / MARKER).exists() == prepared, 'Views source and preparation profile differ')
    preparation = validate_instrumented_source(source, profile) if prepared else None
    frozen = freeze_source(source, profile.data['sourceInputs'])
    from .android_sources import source_hashes
    hashes = source_hashes(frozen)
    require(output.parent.is_dir() and not output.parent.is_symlink(), 'Views build output needs a real parent')
    staging = Path(tempfile.mkdtemp(prefix='.' + output.name + '.', dir=output.parent))
    try:
        workspace = staging / 'source'
        frozen.write_new(workspace)
        android_user_home = staging / 'android-user-home'
        android_user_home.mkdir(mode=0o700)
        build = profile.data['build']
        apk, proof = build_android(workspace, gradle=gradle, java_home=java_home, sdk_home=sdk_home,
            timeout=timeout, task=build['task'], apk_relative=build['apk'], app_profile=profile,
            android_user_home=android_user_home)
        require(proof.get('buildCompleted') is True and proof.get('sourceDigest') == digest(hashes)
            and proof.get('sourceFiles') == hashes
            and snapshot_source(workspace, source_inputs=profile.data['sourceInputs'], isolated=True) == hashes
            and freeze_source(source, profile.data['sourceInputs']) == frozen,
            'Views build changed its protected public inputs')
        app = stage_apk(apk, staging / 'app.apk', expected_digest=proof['apkSha256'],
                        expected_bytes=apk.stat().st_size)
        automatic = profile_from_apk(app)
        require(automatic is not None and automatic.digest == profile.digest if prepared else automatic is None,
                'Views APK differs from the selected observation contract')
        receipt = {'schemaVersion': 2, 'kind': 'configured-views-build', 'platform': 'android',
            'package': profile.data['package'], 'activity': profile.data['activity'],
            'sourceFiles': hashes, 'sourceDigest': digest(hashes), 'profileDigest': profile.digest,
            'apkRelative': 'app.apk', 'artifactDigest': proof['apkSha256'], 'artifactBytes': app.stat().st_size,
            'buildCompleted': True, 'automaticObservations': prepared, 'behaviorVerified': False,
            'buildProof': proof}
        if preparation is not None:
            receipt['preparationDigest'] = digest(preparation)
            write_json(staging / 'instrumentation.json', preparation)
        write_json(staging / 'receipt.json', receipt)
        write_json(staging / 'observation-profile.json', profile.data)
        staging.rename(output)
        return {'output': output, 'app': output / 'app.apk', 'receipt': receipt}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def cli_main(argv):
    import argparse
    from .cli import print_summary, toolchain
    from .build_instrumentation import prepare_build_instrumentation
    from .repair import CommandError
    from .resources import ResourceError
    parser = argparse.ArgumentParser(prog='reproloop', description='Configured Android Views observations')
    parser.add_argument('command', choices=['android-instrument', 'android-app-build'])
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--observation-profile', type=Path, required=True)
    parser.add_argument('--gradle')
    parser.add_argument('--java-home')
    parser.add_argument('--sdk-home')
    args = parser.parse_args(argv)
    try:
        profile = load_observation_profile(args.observation_profile)
        if args.command == 'android-instrument':
            print_summary(prepare_build_instrumentation(args.source, profile, args.output))
        else:
            built = build_observation_app(args.source, args.output, profile=profile, **toolchain(args))
            print_summary({'status': 'built', 'app': built['app'], 'receipt': args.output / 'receipt.json',
                           'automaticObservations': built['receipt']['automaticObservations']})
        return 0
    except (ContractError, CommandError, ResourceError, OSError):
        print_summary({'status': 'failed', 'error': 'Configured Views preparation or build failed'})
        return 1
