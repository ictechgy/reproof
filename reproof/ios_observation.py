"""Explicit public inputs for a configured UIKit observation build.

This profile does not define a fixture, replay oracle, or repair authorization.
Ordinary app actions and starting conditions use the shared issue workflow.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re

from .core import ContractError, digest, require
from .execution.artifacts import ArtifactError, BlobSet, open_regular
from .execution.wire import safe_transfer_path
from .resources import read_resource
from .repair import run_command
from .storage import _unique_object, read_json, write_json

KIND = 'uikit-observation-v2'
SUPPORT = 'ReproofInstrumentation'
MARKER = 'ios-instrumentation-receipt.json'
RUNTIME = ('RLAutomaticRecorder.swift', 'ReproRuntimeIdentity.swift', 'RLSanitationRuntime.swift',
    'RLAutoBootstrap.m', 'RLAutoConfig.swift', 'RLSanitationConfig.swift')
GENERATED = tuple(f'{SUPPORT}/Runtime/{name}' for name in RUNTIME) + (
    f'{SUPPORT}/Profile.plist', f'{SUPPORT}/Info.plist')
_ID = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,127}\Z')
_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_. -]{0,99}\Z')
_SUFFIXES = {'.swift', '.m', '.mm', '.h', '.hpp', '.c', '.cpp', '.plist', '.pbxproj',
    '.xcscheme', '.xcconfig', '.xcworkspacedata', '.xctestplan', '.storyboard', '.xib',
    '.json', '.png', '.jpg', '.jpeg', '.pdf', '.ttf', '.otf', '.strings', '.stringsdict', '.modulemap'}


def _input_name(value):
    safe_transfer_path(value)
    require(re.fullmatch(r'[A-Za-z0-9_./ -]{1,512}', value) is not None,
            'Unsupported public iOS input path')
    parts = value.split('/')
    require(not any(part.startswith('.') or part.casefold() in {
        'artifacts', 'build', 'build-device', 'deriveddata', 'xcuserdata', 'node_modules',
        'local.properties', 'reproofinstrumentation', MARKER} for part in parts)
        and Path(value).suffix.lower() in _SUFFIXES, 'Unsupported public iOS input path')
    return value


def validate_observation_document(document):
    require(type(document) is dict and set(document) == {'schemaVersion', 'kind', 'applicationId',
        'project', 'target', 'build', 'sourceInputs', 'tapTargets', 'screenTargets'}
        and type(document['schemaVersion']) is int and document['schemaVersion'] == 2
        and document['kind'] == KIND, 'Unsupported configured UIKit profile')
    bundle = document['applicationId']
    require(type(bundle) is str and len(bundle) <= 180
        and re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+', bundle)
        and bundle not in {'io.reproof.live', 'io.reproof.driver'}, 'Invalid UIKit application identity')
    require(type(document['project']) is str and document['project'].endswith('.xcodeproj'),
            'Select a relative Xcode project')
    project_file = _input_name(document['project'] + '/project.pbxproj')
    build = document['build']
    require(type(build) is dict and set(build) == {'scheme', 'product', 'infoPlist', 'debugConfiguration'},
            'Unsupported UIKit build selection')
    for value in (document['target'], build['scheme'], build['product'], build['debugConfiguration']):
        require(type(value) is str and _NAME.fullmatch(value) and value == value.strip(),
                'Invalid UIKit build name')
    info = _input_name(build['infoPlist'])
    require(info.endswith('.plist'), 'Select an explicit source Info.plist')
    inputs = document['sourceInputs']
    require(type(inputs) is list and 1 <= len(inputs) <= 900, 'Declare the public iOS build inputs')
    for value in inputs: _input_name(value)
    require(len({name.casefold() for name in inputs}) == len(inputs)
        and {project_file, info} <= set(inputs), 'Missing or duplicated UIKit build inputs')
    require(not any('/'.join(name.split('/')[:i]).casefold() in {p.casefold() for p in inputs}
        for name in inputs for i in range(1, len(name.split('/')))), 'Conflicting UIKit input paths')
    taps = document['tapTargets']; screens = document['screenTargets']
    require(type(taps) is list and 1 <= len(taps) <= 128
        and all(type(item) is str and _ID.fullmatch(item) for item in taps)
        and len(set(taps)) == len(taps), 'Invalid UIKit click targets')
    require(type(screens) is dict and 1 <= len(screens) <= 64
        and all(type(key) is str and _ID.fullmatch(key) and type(value) is str and _ID.fullmatch(value)
                for key, value in screens.items())
        and len(set(screens.values())) == len(screens)
        and not set(taps) & set(screens), 'Invalid UIKit screen targets')
    require(len(json.dumps(document).encode()) <= 256 * 1024, 'UIKit profile size limit exceeded')
    return document


def is_observation_profile(profile):
    return profile is not None and profile.data.get('kind') == KIND


def _freeze(source, names):
    result = []; total = 0
    try:
        for name in names:
            fd = open_regular(source, name)
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                require(before.st_nlink == 1 and before.st_size <= 8 * 1024 * 1024,
                        'Unsupported linked or oversized UIKit input')
                raw = stream.read(8 * 1024 * 1024 + 1)
                after = os.fstat(stream.fileno())
                require(len(raw) == before.st_size and
                    (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink),
                    'UIKit input changed while being captured')
            total += len(raw)
            require(total <= 60 * 1024 * 1024, 'Public UIKit input size limit exceeded')
            result.append((name, raw))
        return BlobSet(tuple(result))
    except (ArtifactError, OSError):
        raise ContractError('Public UIKit input boundary rejected') from None


def _hashes(entries):
    return {name: hashlib.sha256(raw).hexdigest() for name, raw in entries}


def source_snapshot(source, profile, *, prepared=False):
    names = list(profile.data['sourceInputs']) + (list(GENERATED) if prepared else [])
    return _freeze(source, names)


def _plist(raw):
    try:
        value = plistlib.loads(raw)
        require(type(value) is dict, 'Unsupported UIKit property list')
        return value
    except (plistlib.InvalidFileException, ValueError):
        raise ContractError('Unsupported UIKit property list') from None


def _project(raw):
    try: return _plist(raw)
    except ContractError:
        from .repair import run_command
        try:
            value = json.loads(run_command(['/usr/bin/plutil', '-convert', 'json', '-o', '-', '--', '-'],
                '.', stdin=raw.decode('utf-8'), timeout=15, max_output=8 * 1024 * 1024))
            require(type(value) is dict, 'Unsupported Xcode project')
            return value
        except (ValueError, UnicodeError):
            raise ContractError('Unsupported Xcode project') from None


def prepare_observation(source, output, profile, *, sanitation_policy=None):
    from .ios_instrumentation import _configure_project, _sanitation_config, _sanitation_policy
    sanitation_policy = _sanitation_policy(sanitation_policy)
    source, output = Path(source).absolute(), Path(output).absolute()
    require(source.is_dir() and not source.is_symlink() and not output.exists() and not output.is_symlink()
        and not output.resolve().is_relative_to(source.resolve()), 'Use a source and new separate UIKit output')
    require(not (source / SUPPORT).exists() and not (source / MARKER).exists(), 'UIKit source is already prepared')
    original = source_snapshot(source, profile)
    entries = dict(original.entries); before = _hashes(original.entries)
    for name, raw in original.entries:
        if name.endswith(('.swift', '.m', '.mm', '.h')):
            require(not re.search(rb'\bRecorder\s*\.\s*shared\b|RLAutomaticRecorder|RLAutoConfig|\bREPRO_OBSERVATIONS\b', raw),
                    'Existing recorder integration requires the manual recording path')
    project = profile.data['project'] + '/project.pbxproj'
    try: document = _configure_project(_project(entries[project]), profile)
    except (KeyError, TypeError, AttributeError):
        raise ContractError('Unsupported configured UIKit project structure') from None
    entries[project] = plistlib.dumps(document, sort_keys=True)
    for name in RUNTIME:
        if name in {'RLAutoConfig.swift', 'RLSanitationConfig.swift'}:continue
        entries[f'{SUPPORT}/Runtime/{name}'] = read_resource('reproof/ios_instrumentation_templates/' + name)
    encoded = base64.b64encode(json.dumps(profile.data, sort_keys=True, separators=(',', ':')).encode()).decode()
    entries[f'{SUPPORT}/Runtime/RLAutoConfig.swift'] = ('#if REPRO_OBSERVATIONS\nimport Foundation\nenum RLAutoConfig {\n'
        f'    static let profileJSON = String(data: Data(base64Encoded: "{encoded}")!, encoding: .utf8)!\n'
        f'    static let profileDigest = "{profile.digest}"\n}}\n#endif\n').encode()
    entries[f'{SUPPORT}/Runtime/RLSanitationConfig.swift'] = _sanitation_config(sanitation_policy)
    entries[f'{SUPPORT}/Profile.plist'] = plistlib.dumps(profile.data)
    info = _plist(entries[profile.data['build']['infoPlist']])
    require(not any(key.startswith('ReproAuto') or key.startswith('ReproSanitation')
                    or key == 'ReproAppLogSchemaVersion' for key in info),
            'UIKit app already declares an automatic runtime')
    info.update(ReproAutoProfile=profile.data, ReproAutoProfileDigest=profile.digest,
                ReproAppLogSchemaVersion=1, ReproRuntimeIdentitySchemaVersion=2 if sanitation_policy is not None else 1, ReproBuildID='$(REPRO_BUILD_ID)')
    if sanitation_policy is not None:
        info.update(ReproSanitationPolicy=sanitation_policy.data,
                    ReproSanitationPolicyDigest=sanitation_policy.digest)
    entries[f'{SUPPORT}/Info.plist'] = plistlib.dumps(info)
    after = _hashes(sorted(entries.items()))
    changed = sorted(name for name in after if after[name] != before.get(name))
    patch = ''.join(''.join(difflib.unified_diff(
        dict(original.entries).get(name, b'').decode().splitlines(True), entries[name].decode().splitlines(True),
        fromfile='a/' + name, tofile='b/' + name, n=0)) for name in changed).encode()
    receipt = {'schemaVersion': 2, 'kind': 'ios-uikit-observation-preparation',
        'profileDigest': profile.digest, 'originalFiles': before, 'originalSourceDigest': digest(before),
        'preparedFiles': after, 'preparedSourceDigest': digest(after), 'changedFiles': changed,
        'productSourcesUnchanged': True, 'patchSha256': hashlib.sha256(patch).hexdigest(), 'behaviorVerified': False}
    if sanitation_policy is not None:receipt['sanitationPolicyDigest'] = sanitation_policy.digest
    require(source_snapshot(source, profile) == original, 'Original UIKit inputs changed during preparation')
    raw_receipt = (json.dumps(receipt, sort_keys=True, indent=2) + '\n').encode()
    published = [('source/' + name, raw) for name, raw in entries.items()]
    published += [('source/' + MARKER, raw_receipt), ('instrumentation.json', raw_receipt),
        ('patch.diff', patch), ('auto-profile.json', (json.dumps(profile.data, indent=2) + '\n').encode())]
    try: BlobSet(tuple(published)).write_new(output)
    except ArtifactError: raise ContractError('Use a new output with real existing parent directories') from None
    return {'status': 'prepared', 'platform': 'ios', 'mode': 'observations', 'source': output / 'source',
        'profile': output / 'auto-profile.json', 'report': output / 'instrumentation.json',
        'patch': output / 'patch.diff', 'productSourcesUnchanged': True, 'behaviorVerified': False}


def validate_observation_preparation(source, profile):
    source = Path(source)
    receipt = read_json(source / MARKER)
    current = _hashes(source_snapshot(source, profile, prepared=True).entries)
    external = _freeze(source.parent, ['instrumentation.json', 'patch.diff'])
    files = dict(external.entries)
    require(type(receipt) is dict and receipt == json.loads(files['instrumentation.json'])
        and receipt.get('schemaVersion') == 2 and receipt.get('kind') == 'ios-uikit-observation-preparation'
        and receipt.get('profileDigest') == profile.digest and receipt.get('preparedFiles') == current
        and receipt.get('preparedSourceDigest') == digest(current)
        and receipt.get('patchSha256') == hashlib.sha256(files['patch.diff']).hexdigest(),
        'Prepared UIKit inputs differ from the receipt')
    original = receipt.get('originalFiles')
    project = profile.data['project'] + '/project.pbxproj'
    require(type(original) is dict and set(original) == set(profile.data['sourceInputs'])
        and receipt.get('originalSourceDigest') == digest(original)
        and receipt.get('productSourcesUnchanged') is True
        and all(current.get(name) == checksum for name, checksum in original.items() if name != project),
        'Prepared UIKit product inputs changed')
    return receipt


def load_observation_profile(path):
    from .ios_instrumentation import validate_ios_auto_profile
    path = Path(path).absolute()
    _input_name(path.name)
    require(path.suffix == '.json', 'Select a public UIKit profile JSON file')
    try: value = json.loads(dict(_freeze(path.parent, [path.name]).entries)[path.name], object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError): raise ContractError('Invalid UIKit profile JSON') from None
    selected = validate_ios_auto_profile(value)
    require(is_observation_profile(selected), 'Select a configured UIKit observation profile')
    return selected


def build_observation_app(source, output, *, profile=None, configuration=None, sdk='simulator'):
    """Explicit local build of trusted public inputs; no device or signing access."""
    from .ios_build import xcode_environment
    from .ios_instrumentation import (IosAutoProfile, profile_from_source, profile_from_app,
                                      validate_ios_auto_profile)
    from .ios_sanitation import policy_from_app
    from .ios_storage import tree_manifest
    source, output = Path(source).absolute(), Path(output).absolute()
    require(not output.exists() and not output.is_symlink()
        and not output.resolve().is_relative_to(source.resolve()), 'Use a new separate UIKit build output')
    embedded = profile_from_source(source)
    if profile is not None:
        profile = validate_ios_auto_profile(profile.data if isinstance(profile, IosAutoProfile) else profile)
    else: profile = embedded
    require(is_observation_profile(profile), 'Select a configured UIKit observation profile')
    require(embedded is None or embedded.digest == profile.digest, 'UIKit build profile changed')
    preparation = validate_observation_preparation(source, profile) if embedded else None
    frozen = source_snapshot(source, profile, prepared=embedded is not None)
    hashes = _hashes(frozen.entries)
    build_id = digest(hashes)[:32]
    build = profile.data['build']
    configuration = build['debugConfiguration'] if configuration is None else configuration
    require(type(configuration) is str and _NAME.fullmatch(configuration)
        and configuration == configuration.strip() and sdk in {'simulator', 'device'},
        'Unsupported UIKit build selection')
    document = _project(dict(frozen.entries)[profile.data['project'] + '/project.pbxproj'])
    try:
        objects = document['objects']
        targets = [item for item in objects.values() if item.get('isa') == 'PBXNativeTarget'
                   and item.get('name') == profile.data['target']]
        require(len(targets) == 1 and targets[0].get('productType') == 'com.apple.product-type.application',
                'Select the configured UIKit application target')
        configurations = [objects[key]['name'] for key in
            objects[targets[0]['buildConfigurationList']]['buildConfigurations']]
        require(configuration in configurations, 'Selected UIKit configuration is unavailable')
    except (KeyError, TypeError, AttributeError):
        raise ContractError('Unsupported configured UIKit project structure') from None
    environment = xcode_environment()
    try: BlobSet(tuple(('source/' + name, raw) for name, raw in frozen.entries)).write_new(output)
    except ArtifactError: raise ContractError('Use a new output with real existing parent directories') from None
    workspace = output / 'source'
    sdk_name = 'iphonesimulator' if sdk == 'simulator' else 'iphoneos'
    command = ['/usr/bin/xcodebuild', 'build', '-project', str(workspace / profile.data['project']),
        '-scheme', build['scheme'], '-configuration', configuration, '-sdk', sdk_name,
        '-destination', 'generic/platform=iOS Simulator' if sdk == 'simulator' else 'generic/platform=iOS',
        '-derivedDataPath', str(output / 'DerivedData'), '-disableAutomaticPackageResolution', '-skipPackageUpdates',
        'CODE_SIGNING_ALLOWED=NO', 'CODE_SIGNING_REQUIRED=NO', 'COMPILER_INDEX_STORE_ENABLE=NO',
        'REPRO_BUILD_ID=' + build_id]
    # Public compiler output may still contain product text. The normal command
    # publishes only a static outcome; callers keep their own approved diagnostics.
    run_command(command, str(workspace), timeout=300, max_output=8 * 1024 * 1024, env_extra=environment)
    require(source_snapshot(workspace, profile, prepared=embedded is not None) == frozen
        and source_snapshot(source, profile, prepared=embedded is not None) == frozen,
        'UIKit build changed its protected public inputs')
    products = output / 'DerivedData/Build/Products'
    app = products / f'{configuration}-{sdk_name}' / (build['product'] + '.app')
    info = _plist(dict(_freeze(app, ['Info.plist']).entries)['Info.plist'])
    require(info.get('CFBundleIdentifier') == profile.data['applicationId'], 'UIKit product identity changed')
    for key in ('CFBundleShortVersionString', 'CFBundleVersion'):
        value = info.get(key)
        require(type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value),
                'UIKit product version is missing or unsupported')
    automatic = profile_from_app(app)
    sanitation = policy_from_app(app)
    recording_build = embedded is not None and configuration == build['debugConfiguration']
    if recording_build:
        require(automatic is not None and automatic.digest == profile.digest
            and info.get('ReproBuildID') == build_id, 'Configured UIKit app lost its automatic build identity')
        sanitation_digest = preparation.get('sanitationPolicyDigest') if preparation is not None else None
        require((sanitation is None and sanitation_digest is None)
            or (sanitation is not None and sanitation.digest == sanitation_digest),
            'Configured UIKit app lost its sanitation policy')
    else:
        require(automatic is None and sanitation is None and 'ReproAppLogSchemaVersion' not in info,
                'Non-recording UIKit build must exclude its observation profile')
    app_files = tree_manifest(app)
    receipt = {'schemaVersion': 2, 'kind': 'configured-uikit-build', 'platform': 'ios',
        'applicationId': profile.data['applicationId'], 'executionEnvironment': sdk,
        'configuration': configuration, 'scheme': build['scheme'], 'sourceFiles': hashes,
        'sourceDigest': digest(hashes), 'buildId': build_id, 'profileDigest': profile.digest,
        'appRelative': app.relative_to(products).as_posix(), 'artifactDigest': digest(app_files),
        'artifactBytes': sum((app / name).stat().st_size for name in app_files),
        'bundleVersion': info['CFBundleShortVersionString'], 'bundleBuild': info['CFBundleVersion'],
        'productsDigest': digest(tree_manifest(products)), 'buildCompleted': True, 'signed': False,
        'automaticObservations': recording_build, 'behaviorVerified': False,
        'xcode': run_command(['/usr/bin/xcodebuild', '-version'], str(workspace), timeout=20, env_extra=environment).strip()}
    if preparation is not None: receipt['preparationDigest'] = digest(preparation)
    if sanitation is not None: receipt['sanitationPolicyDigest'] = sanitation.digest
    write_json(output / 'receipt.json', receipt)
    return {'output': output, 'products': products, 'app': app, 'receipt': receipt}
