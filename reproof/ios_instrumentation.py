"""UIKit automatic-recording preparation and its closed capture contract."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import difflib
import json
from pathlib import Path
import plistlib
import posixpath
import re
import shutil
import tempfile
import uuid

from .core import digest, require
from .resources import resource_root
from .ios_cases import CASES, case_from_fixture
from .storage import read_json, sha_file, write_json

ROOT = resource_root()
TEMPLATES = ROOT / 'reproloop/ios_instrumentation_templates'
SUPPORT = Path('ReproLoopInstrumentation')
MARKER = 'ios-instrumentation-receipt.json'
SHA = re.compile(r'[0-9a-f]{64}\Z')
BUILD_ID = re.compile(r'[A-Za-z0-9_-]{8,128}\Z')
RUNTIME_FILES = ('RLAutomaticRecorder.swift', 'ReproRuntimeIdentity.swift', 'RLSanitationRuntime.swift',
                 'RLAutoBootstrap.m', 'RLAutoConfig.swift', 'RLSanitationConfig.swift')


def _profile_document():
    return {'schemaVersion': 1, 'kind': 'uikit-runtime-v1', 'applicationId': 'io.reproloop.sample.ios',
        'project': 'ReproLoop.xcodeproj', 'target': 'ReproSample',
        'cases': ['counter', 'duplicate-submit', 'reset'], 'textTargets': ['counter.name'],
        'numericTargets': ['counter.count'], 'tapTargets': ['counter.add', 'counter.next', 'counter.reset'],
        'backTarget': 'counter.back', 'screenTargets': {'counter.screen.main': 'main', 'counter.screen.details': 'details'},
        'startState': {'screen': 'main', 'nodes': {'counter.name': '', 'counter.count': '0'}}}


@dataclass(frozen=True)
class IosAutoProfile:
    _json: str

    @property
    def data(self):return json.loads(self._json)

    @property
    def digest(self):return digest(self.data)


def validate_ios_auto_profile(document):
    if type(document) is dict and type(document.get('schemaVersion')) is int and document['schemaVersion'] == 2:
        from .ios_observation import validate_observation_document
        validate_observation_document(document)
        return IosAutoProfile(json.dumps(document, sort_keys=True, separators=(',', ':')))
    # This first UIKit adapter shares the existing finite iOS replay contract.
    # A recording cannot widen the target/value/case policy or select build code.
    require(isinstance(document, dict) and type(document.get('schemaVersion')) is int
            and document == _profile_document(), 'Unsupported iOS automatic instrumentation profile')
    return IosAutoProfile(json.dumps(document, sort_keys=True, separators=(',', ':')))


def sample_ios_auto_profile():return validate_ios_auto_profile(_profile_document())


def _sanitation_policy(value):
    from .ios_sanitation import IOSSanitationPolicy, validate_ios_sanitation_policy
    if value is None:return None
    return validate_ios_sanitation_policy(value.data if isinstance(value, IOSSanitationPolicy) else value)


def _sanitation_config(policy):
    lines = ['#if DEBUG || REPRO_OBSERVATIONS', 'import Foundation', '', 'enum RLSanitationConfig {']
    if policy is None:
        lines += ['    static let policyJSON: String? = nil', '    static let policyDigest: String? = nil']
    else:
        encoded = base64.b64encode(json.dumps(policy.data, sort_keys=True, separators=(',', ':')).encode()).decode()
        lines += [f'    static let policyJSON: String? = String(data: Data(base64Encoded: "{encoded}")!, encoding: .utf8)!',
                  f'    static let policyDigest: String? = "{policy.digest}"']
    return ('\n'.join(lines + ['}', '#endif', ''])).encode()


def _read_plist(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 4 * 1024 * 1024,
            'Invalid iOS instrumentation property list')
    with path.open('rb') as handle:return plistlib.load(handle)


def profile_from_source(source):
    path = Path(source) / SUPPORT / 'Profile.plist'
    if not path.exists():return None
    return validate_ios_auto_profile(_read_plist(path))


def profile_from_app(app):
    info = _read_plist(Path(app) / 'Info.plist')
    if 'ReproAutoProfile' not in info and 'ReproAutoProfileDigest' not in info:return None
    profile = validate_ios_auto_profile(info.get('ReproAutoProfile'))
    require(info.get('ReproAutoProfileDigest') == profile.digest
            and info.get('CFBundleIdentifier') == profile.data['applicationId'],
            'iOS app automatic instrumentation identity changed')
    return profile


def app_logs_enabled(app):
    info = _read_plist(Path(app) / 'Info.plist')
    if 'ReproAppLogSchemaVersion' not in info:return False
    require(type(info['ReproAppLogSchemaVersion']) is int and info['ReproAppLogSchemaVersion'] == 1
            and profile_from_app(app) is not None, 'Unsupported automatic iOS app log capability')
    return True


def _uuid(value):
    if not isinstance(value, str):return False
    try:return str(uuid.UUID(value)) == value
    except ValueError:return False


def validate_ios_auto_marker(marker, profile, *, run_id, build_id, fixture, min_started_at=None):
    require(profile.data == _profile_document(), 'Configured observations do not define a legacy iOS fixture capture')
    required = {'schemaVersion', 'runId', 'sessionId', 'profileDigest', 'buildId', 'fixture', 'startedAtMs', 'finalized'}
    require(isinstance(marker, dict) and required <= set(marker) and set(marker) <= required | {'endSequence'}
            and type(marker['schemaVersion']) is int and marker['schemaVersion'] == 1
            and _uuid(run_id) and marker['runId'] == run_id and _uuid(marker['sessionId'])
            and marker['profileDigest'] == profile.digest and marker['buildId'] == build_id
            and isinstance(build_id, str) and BUILD_ID.fullmatch(build_id)
            and marker['fixture'] == fixture and type(marker['startedAtMs']) is int and marker['startedAtMs'] >= 0
            and type(marker['finalized']) is bool, 'iOS automatic session identity differs from its launch')
    case_from_fixture(fixture)
    if min_started_at is not None:
        require(type(min_started_at) is int and marker['startedAtMs'] >= min_started_at, 'Stale iOS automatic session')
    if marker['finalized']:
        require(type(marker.get('endSequence')) is int and 0 <= marker['endSequence'] <= 10000,
                'Missing automatic final sequence')
    else:require('endSequence' not in marker, 'Unfinalized automatic session has a final sequence')
    return marker


def validate_ios_auto_diagnostics(document, capture, profile, *, run_id=None, build_id=None):
    require(profile.data == _profile_document(), 'Configured observations do not define legacy iOS capture diagnostics')
    from .ios_core import compile_ios_capture
    compile_ios_capture(capture, case_from_fixture(capture.get('fixture')).oracle())
    keys = {'schemaVersion', 'platform', 'runId', 'sessionId', 'profileDigest', 'buildId', 'endSequence', 'actions'}
    require(isinstance(document, dict) and set(document) == keys
            and type(document['schemaVersion']) is int and document['schemaVersion'] == 1
            and document['platform'] == 'ios' and _uuid(document['runId'])
            and (run_id is None or document['runId'] == run_id)
            and document['sessionId'] == capture['sessionId'] and _uuid(document['sessionId'])
            and document['profileDigest'] == profile.digest
            and isinstance(document['buildId'], str) and BUILD_ID.fullmatch(document['buildId'])
            and (build_id is None or document['buildId'] == build_id)
            and type(document['endSequence']) is int and document['endSequence'] == capture['endSequence'],
            'Automatic iOS diagnostics have a different recording identity')
    actions = document['actions']
    expected = [event for event in capture['events'] if event['action'] in {'tap', 'navigate_back'}]
    require(isinstance(actions, list) and 0 < len(actions) <= 500 and len(actions) == len(expected),
            'Automatic iOS diagnostics do not cover every action')
    action_keys = {'eventId', 'target', 'before', 'after', 'beforeScreen', 'afterScreen', 'outcome'}
    for action, event in zip(actions, expected):
        require(isinstance(action, dict) and set(action) == action_keys
                and action['eventId'] == event['id'] and action['target'] == event['target']
                and isinstance(action['outcome'], str) and action['outcome'] in {'returned', 'threw'}
                and isinstance(action['beforeScreen'], str) and action['beforeScreen'] in {'main', 'details'}
                and isinstance(action['afterScreen'], str) and action['afterScreen'] in {'main', 'details'},
                'Automatic iOS diagnostic action is invalid')
        for state in (action['before'], action['after']):
            require(isinstance(state, dict) and set(state) == {'counter.count'}
                    and isinstance(state['counter.count'], str) and re.fullmatch(r'[0-9]{1,9}', state['counter.count']),
                    'Automatic iOS diagnostic state is outside the numeric policy')
    require(len(json.dumps(document).encode()) <= 1024 * 1024, 'Automatic iOS diagnostics exceed the limit')
    return document


def _load_project(path):
    try:return _read_plist(path)
    except plistlib.InvalidFileException:
        from .repair import run_command
        return json.loads(run_command(['/usr/bin/plutil', '-convert', 'json', '-o', '-', '--', str(path)], '.', timeout=15))


def _setting_list(value):
    require(value is None or isinstance(value, (str, list)), 'Unsupported iOS build setting shape')
    if value is None:return []
    return list(value) if isinstance(value, list) else [value]


def _configure_project(document, profile):
    from .ios_observation import is_observation_profile
    general = is_observation_profile(profile)
    info_file = profile.data['build']['infoPlist'] if general else 'Sample/Info.plist'
    debug_name = profile.data['build']['debugConfiguration'] if general else 'Debug'
    project_base = Path(profile.data['project']).parent.as_posix()
    info_setting = posixpath.relpath(info_file, project_base) if general else info_file
    generated_info = posixpath.relpath((SUPPORT / 'Info.plist').as_posix(), project_base) if general else (SUPPORT / 'Info.plist').as_posix()
    objects = document.get('objects', {})
    matches = [obj for obj in objects.values() if obj.get('isa') == 'PBXNativeTarget' and obj.get('name') == profile.data['target']]
    require(len(matches) == 1 and matches[0].get('productType') == 'com.apple.product-type.application',
            'Select the supported iOS application target')
    app = matches[0]
    configs = [objects[key] for key in objects[app['buildConfigurationList']]['buildConfigurations']]
    names = [item['name'] for item in configs]
    require((len(names) == len(set(names)) and debug_name in names and len(names) >= 2) if general
        else set(names) == {'Debug', 'Release'}, 'Automatic iOS integration requires distinct recording and release configurations')
    for config in configs:
        settings = config.setdefault('buildSettings', {})
        require(settings.get('PRODUCT_BUNDLE_IDENTIFIER') == profile.data['applicationId']
                and settings.get('INFOPLIST_FILE') == info_setting, 'Unsupported iOS application build identity')
        if config['name'] == debug_name:
            settings['INFOPLIST_FILE'] = generated_info
            settings['SWIFT_ACTIVE_COMPILATION_CONDITIONS'] = _setting_list(settings.get('SWIFT_ACTIVE_COMPILATION_CONDITIONS')) + ['$(inherited)', 'REPRO_OBSERVATIONS' if general else 'DEBUG']
            settings['GCC_PREPROCESSOR_DEFINITIONS'] = _setting_list(settings.get('GCC_PREPROCESSOR_DEFINITIONS')) + ['$(inherited)', 'REPRO_AUTO_DEBUG=1']
        else:
            settings['EXCLUDED_SOURCE_FILE_NAMES'] = _setting_list(settings.get('EXCLUDED_SOURCE_FILE_NAMES')) + list(RUNTIME_FILES)
    phases = [objects[key] for key in app['buildPhases'] if objects[key]['isa'] == 'PBXSourcesBuildPhase']
    require(len(phases) == 1, 'Ambiguous iOS application sources phase')
    group = objects[objects[document['rootObject']]['mainGroup']]
    for name in RUNTIME_FILES:
        relative = (SUPPORT / 'Runtime' / name).as_posix()
        if general:relative = posixpath.relpath(relative, project_base)
        file_id = digest({'file': relative})[:24].upper()
        build_id = digest({'buildFile': relative})[:24].upper()
        require(file_id not in objects and build_id not in objects, 'Automatic iOS project references already exist')
        objects[file_id] = {'isa': 'PBXFileReference', 'lastKnownFileType': 'sourcecode.c.objc' if name.endswith('.m') else 'sourcecode.swift',
                            'path': relative, 'sourceTree': 'SOURCE_ROOT'}
        objects[build_id] = {'isa': 'PBXBuildFile', 'fileRef': file_id}
        phases[0]['files'].append(build_id)
        group.setdefault('children', []).append(file_id)
    return document


def prepare_ios_instrumentation(source, output, *, profile=None, sanitation_policy=None):
    sanitation_policy = _sanitation_policy(sanitation_policy)
    if profile is not None:
        profile = validate_ios_auto_profile(profile.data if isinstance(profile, IosAutoProfile) else profile)
        from .ios_observation import is_observation_profile, prepare_observation
        if is_observation_profile(profile):
            return prepare_observation(source, output, profile, sanitation_policy=sanitation_policy)
    from .ios_storage import copy_ios_source, tree_manifest
    source, output = Path(source), Path(output)
    require(source.is_dir() and not source.is_symlink() and not output.exists() and not output.is_symlink(),
            'Use a valid source and a new iOS instrumentation output')
    source, output = source.resolve(), output.resolve()
    require(not output.is_relative_to(source), 'iOS instrumentation output must be outside the original source')
    require(not (source / SUPPORT).exists() and not (source / MARKER).exists(), 'iOS source is already prepared for automatic instrumentation')
    before = tree_manifest(source, True)
    for name in before:
        if name.endswith(('.swift', '.m', '.h')):
            text = (source / name).read_text()
            require(not re.search(r'\bRecorder\s*\.\s*shared\b|RLAutomaticRecorder|RLAutoConfig', text),
                    'Existing recorder integration requires the manual recording path')
    profile = sample_ios_auto_profile()
    project = Path(profile.data['project']) / 'project.pbxproj'
    document = _configure_project(_load_project(source / project), profile)
    generated_configs = {'RLAutoConfig.swift', 'RLSanitationConfig.swift'}
    require(all((TEMPLATES / name).is_file() for name in RUNTIME_FILES if name not in generated_configs),
            'UIKit automatic runtime templates are unavailable')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.' + output.name + '.', dir=output.parent))
    try:
        workspace = staging / 'source'
        require(copy_ios_source(source, workspace) == before, 'iOS source changed during preparation')
        runtime = workspace / SUPPORT / 'Runtime'
        runtime.mkdir(parents=True)
        for name in RUNTIME_FILES:
            if name not in generated_configs:shutil.copyfile(TEMPLATES / name, runtime / name)
        # Swift raw literals preserve JSON without turning input into code.
        raw = json.dumps(profile.data, sort_keys=True, separators=(',', ':'))
        (runtime / 'RLAutoConfig.swift').write_text('#if DEBUG\nimport Foundation\n\nenum RLAutoConfig {\n'
            f'    static let profileJSON = #"{raw}"#\n    static let profileDigest = "{profile.digest}"\n}}\n#endif\n')
        (runtime / 'RLSanitationConfig.swift').write_bytes(_sanitation_config(sanitation_policy))
        with (workspace / SUPPORT / 'Profile.plist').open('wb') as handle:plistlib.dump(profile.data, handle)
        info = _read_plist(source / 'Sample/Info.plist')
        require(not any(key.startswith('ReproSanitation') for key in info),
                'iOS app already declares a sanitation runtime')
        info.update(ReproAutoProfile=profile.data, ReproAutoProfileDigest=profile.digest,
                    ReproAppLogSchemaVersion=1, ReproRuntimeIdentitySchemaVersion=2 if sanitation_policy is not None else 1)
        if sanitation_policy is not None:
            info.update(ReproSanitationPolicy=sanitation_policy.data,
                        ReproSanitationPolicyDigest=sanitation_policy.digest)
        with (workspace / SUPPORT / 'Info.plist').open('wb') as handle:plistlib.dump(info, handle)
        with (workspace / project).open('wb') as handle:plistlib.dump(document, handle, sort_keys=True)
        after = tree_manifest(workspace, True)
        require(all(after.get(name) == checksum for name, checksum in before.items() if name != project.as_posix()),
                'iOS automatic preparation changed an existing product input')
        changed = sorted(name for name in after if after[name] != before.get(name))
        patch = ''.join(''.join(difflib.unified_diff(
            (source / name).read_text().splitlines(True) if name in before else [],
            (workspace / name).read_text().splitlines(True), fromfile='a/' + name, tofile='b/' + name, n=0)) for name in changed)
        (staging / 'patch.diff').write_text(patch)
        write_json(staging / 'auto-profile.json', profile.data)
        receipt = {'schemaVersion': 1, 'kind': 'ios-uikit-automatic-preparation', 'profileDigest': profile.digest,
            'originalFiles': before, 'originalSourceDigest': digest(before), 'preparedFiles': after,
            'preparedSourceDigest': digest(after), 'productSourcesUnchanged': True, 'changedFiles': changed,
            'patchSha256': sha_file(staging / 'patch.diff'), 'behaviorVerified': False}
        if sanitation_policy is not None:receipt['sanitationPolicyDigest'] = sanitation_policy.digest
        write_json(staging / 'instrumentation.json', receipt)
        write_json(workspace / MARKER, receipt)
        require(tree_manifest(source, True) == before, 'Original iOS source changed during preparation')
        staging.rename(output)
        return {'status': 'prepared', 'platform': 'ios', 'mode': 'automatic', 'source': output / 'source',
                'profile': output / 'auto-profile.json', 'report': output / 'instrumentation.json',
                'patch': output / 'patch.diff', 'productSourcesUnchanged': True, 'behaviorVerified': False}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_ios_preparation(source):
    from .ios_storage import tree_manifest
    source = Path(source)
    receipt = read_json(source / MARKER)
    profile = profile_from_source(source)
    from .ios_observation import is_observation_profile, validate_observation_preparation
    if is_observation_profile(profile): return validate_observation_preparation(source, profile)
    require(profile is not None and isinstance(receipt, dict)
            and receipt == read_json(source.parent / 'instrumentation.json')
            and receipt.get('patchSha256') == sha_file(source.parent / 'patch.diff'),
            'iOS preparation marker differs from its external receipt')
    current = tree_manifest(source, True)
    original = receipt.get('originalFiles')
    project = profile.data['project'] + '/project.pbxproj'
    require(receipt.get('schemaVersion') == 1 and receipt.get('kind') == 'ios-uikit-automatic-preparation'
            and receipt.get('profileDigest') == profile.digest
            and receipt.get('preparedFiles') == current and receipt.get('preparedSourceDigest') == digest(current)
            and isinstance(original, dict) and bool(original) and receipt.get('originalSourceDigest') == digest(original)
            and receipt.get('productSourcesUnchanged') is True
            and all(current.get(name) == checksum for name, checksum in original.items() if name != project),
            'Prepared iOS source no longer matches its instrumentation receipt')
    return receipt
