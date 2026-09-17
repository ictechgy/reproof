"""Prepare reviewable, debug-only source instrumentation in a new workspace."""
from __future__ import annotations
import difflib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from xml.parsers import expat

from .android_profile import validate_app_profile
from .core import ContractError, digest, require
from .repair import copy_source, snapshot_source
from .storage import read_json, write_json, sha_file

from .resources import resource_root

ROOT = resource_root()
TEMPLATES = ROOT / 'reproloop/instrumentation_templates/android'
MARKER = 'instrumentation-receipt.json'
RECEIVER = 'io.reproloop.autotrace.AutoExportReceiver'
ANDROID = 'http://schemas.android.com/apk/res/android'


def render_runtime_config(profile, sites):
    def literal(value):return json.dumps(value, ensure_ascii=False).replace('$', '\\$')
    return ('package io.reproloop.autotrace\n\ninternal object ReproConfig {\n'
        '    const val RECORD_MODE = "record"\n'
        f'    const val PROFILE_JSON = {literal(json.dumps(profile.native(), separators=(",", ":")))}\n'
        f'    const val PROFILE_DIGEST = "{profile.digest}"\n'
        f'    const val APP_LOGS_ENABLED = {str(profile.data.get("appLogs") == 1).lower()}\n'
        f'    const val SCREEN_TARGETS_JSON = {literal(json.dumps(profile.data.get("screenTargets", {}), separators=(",", ":")))}\n'
        f'    const val SITES_JSON = {literal(json.dumps(sites, separators=(",", ":")))}\n' + '}\n')


def prepare_instrumentation(source, app_profile, output, *, mode='build'):
    require(mode in {'build', 'source'}, 'Unsupported instrumentation mode')
    if mode == 'build':
        from .build_instrumentation import prepare_build_instrumentation
        return prepare_build_instrumentation(source, app_profile, output)
    return dict(instrument_project(source, app_profile, output), mode='source')


def merge_debug_manifest(text):
    """Insert only the receiver; preserve existing XML bytes and namespaces."""
    raw = text.encode('utf-8')
    require(len(raw) <= 1024*1024, 'Debug manifest is too large')
    parser = expat.ParserCreate(namespace_separator='|')
    depth = 0
    root_range, app_range = [], []
    def start(name, attrs):
        nonlocal depth
        depth += 1
        if depth == 1:
            require(name == 'manifest', 'Invalid debug manifest root')
            root_range.append(parser.CurrentByteIndex)
        if depth == 2 and name == 'application':
            require(not app_range, 'Ambiguous debug application overlay')
            require(attrs.get('http://schemas.android.com/tools|node') not in {'remove', 'removeAll', 'replace'},
                    'Unsupported application manifest merge directive')
            app_range.append(parser.CurrentByteIndex)
        if name == 'receiver':
            require(attrs.get(ANDROID + '|name') != RECEIVER, 'Instrumentation receiver already exists')
    def end(name):
        nonlocal depth
        if depth == 2 and name == 'application':app_range.append(parser.CurrentByteIndex)
        if depth == 1:root_range.append(parser.CurrentByteIndex)
        depth -= 1
    def reject_doctype(*args):raise ContractError('DTD is not supported in an instrumentation manifest')
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = lambda *args: 0
    try:parser.Parse(raw, True)
    except expat.ExpatError:raise ContractError('Invalid debug manifest XML') from None
    require(len(root_range) == 2, 'Incomplete debug manifest')
    fragment = (f'\n        <receiver xmlns:android="{ANDROID}" android:name="{RECEIVER}" '
                'android:exported="true" android:permission="android.permission.DUMP">\n'
                '            <intent-filter><action android:name="io.reproloop.EXPORT_CAPTURE" /></intent-filter>\n'
                '        </receiver>\n').encode()
    if app_range:
        begin, close = app_range
        if raw[close:close+13] == b'</application':
            return (raw[:close] + fragment + raw[close:]).decode('utf-8')
        marker = raw.rfind(b'/>', begin, close)
        require(marker >= begin, 'Unsupported debug application tag')
        return (raw[:marker] + b'>' + fragment + b'</application>' + raw[close:]).decode('utf-8')
    begin, close = root_range
    application = b'\n    <application>' + fragment + b'    </application>\n'
    if raw[close:close+10] == b'</manifest':return (raw[:close] + application + raw[close:]).decode('utf-8')
    marker = raw.rfind(b'/>', begin, close)
    require(marker >= begin, 'Unsupported debug manifest tag')
    return (raw[:marker] + b'>' + application + b'</manifest>' + raw[close:]).decode('utf-8')


def instrument_project(source, app_profile, output, *, analyzer=None):
    require('sourceInputs' not in app_profile.data, 'Explicit public inputs require build instrumentation mode')
    input_path, output_path = Path(source), Path(output)
    require(input_path.is_dir() and not input_path.is_symlink(), 'Invalid instrumentation source directory')
    require(not output_path.exists() and not output_path.is_symlink(), 'Instrumentation output already exists')
    source, output = input_path.resolve(), output_path.resolve()
    require(not output.is_relative_to(source), 'Instrumentation output must be outside the original source')
    require(app_profile.data.get('captureMode', 'report_view') == 'report_view' and not (source / MARKER).exists(),
            'Source is already instrumented; start from the original source')
    config = app_profile.data
    require(config['build']['task'].endswith('Debug'), 'Automatic instrumentation currently requires a concrete Debug variant')
    module = PurePosixPath(*config['build']['task'].split(':')[1:-1])
    module_path = Path(*module.parts)
    for variant in ('main', 'debug', 'release'):
        for language in ('java', 'kotlin'):
            require(not (source / module_path / f'src/{variant}/{language}/io/reproloop/autotrace').exists(),
                    'Automatic instrumentation runtime already exists')
    require(not (source / module_path / 'src/debug/java/io/reproloop/sdk/ReproRecorder.kt').exists(),
            'A vendored recorder already exists')
    before = snapshot_source(source)
    main_prefix = (module_path / 'src/main').as_posix() + '/'
    files = {name: (source / name).read_text() for name in before if name.startswith(main_prefix) and name.endswith('.kt')}
    require(bool(files), 'No supported Kotlin product sources found')
    if analyzer is None:
        from .kotlin_instrumenter import instrument_kotlin
        analyzer = instrument_kotlin
    activity = app_profile.component_name.split('/', 1)[1]
    if activity.startswith('.'):activity = config['package'] + activity
    plan = analyzer(files, activity, config['targets']['tap'])
    require(isinstance(plan, dict) and plan.get('schemaVersion') == 1 and isinstance(plan.get('files'), dict)
            and plan.get('activityPath') in files and set(plan['files']) == {plan['activityPath']},
            'Analyzer attempted changes outside the selected activity')
    require(all(isinstance(value, str) and value != files[name] for name, value in plan['files'].items()),
            'Analyzer did not produce a valid activity change')
    config['captureMode'] = 'debug_receiver'
    config['targets']['report'] = None
    config['instrumentation'] = {'kind': 'kotlin_psi_v1', 'sites': plan['sites']}
    config['appLogs'] = 1
    instrumented_profile = validate_app_profile(config)
    require(all(site['path'] == plan['activityPath'] for site in plan['sites']), 'Unbound instrumentation source site')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.' + output.name + '.', dir=output.parent))
    try:
        workspace = staging / 'source'
        require(copy_source(source, workspace) == before, 'Source changed while preparing instrumentation')
        for name, value in plan['files'].items():(workspace / name).write_text(value)
        for variant in ('debug', 'release'):
            for template in (TEMPLATES / variant / 'java').rglob('*.kt'):
                relative = template.relative_to(TEMPLATES / variant / 'java')
                target = workspace / module_path / f'src/{variant}/java' / relative
                require(not target.exists(), 'Instrumentation would overwrite an existing runtime file')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(template, target)
        sdk = workspace / module_path / 'src/debug/java/io/reproloop/sdk/ReproRecorder.kt'
        sdk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'android/sdk/src/main/java/io/reproloop/sdk/ReproRecorder.kt', sdk)
        generated = render_runtime_config(instrumented_profile, plan['sites'])
        (workspace / module_path / 'src/debug/java/io/reproloop/autotrace/ReproConfig.kt').write_text(generated)
        manifest = workspace / module_path / 'src/debug/AndroidManifest.xml'
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(merge_debug_manifest(manifest.read_text() if manifest.exists() else '<manifest/>\n'))
        after = snapshot_source(workspace)
        changed = sorted(name for name in after if after[name] != before.get(name))
        patch = ''.join(''.join(difflib.unified_diff(
            (source / name).read_text().splitlines(True) if name in before else [],
            (workspace / name).read_text().splitlines(True), fromfile='a/' + name, tofile='b/' + name, n=0))
            for name in changed)
        (staging / 'patch.diff').write_text(patch)
        write_json(staging / 'app-profile.json', instrumented_profile.data)
        receipt = {'schemaVersion': 1, 'kind': 'source-instrumentation', 'status': 'instrumented',
            'originalSourceDigest': digest(before), 'instrumentedSourceDigest': digest(after),
            'originalFiles': before, 'instrumentedFiles': after, 'appProfileDigest': instrumented_profile.digest,
            'sites': plan['sites'], 'changedFiles': changed, 'patchSha256': sha_file(staging / 'patch.diff'),
            'debugTask': config['build']['task'], 'releaseTask': config['build']['task'][:-5] + 'Release',
            'behaviorVerified': False, 'parser': 'kotlin-psi'}
        write_json(workspace / MARKER, receipt)
        write_json(staging / 'instrumentation.json', receipt)
        require(snapshot_source(source) == before, 'Original source changed during instrumentation')
        staging.rename(output)
        return {'status': 'instrumented', 'source': output / 'source', 'appProfile': output / 'app-profile.json',
                'patch': output / 'patch.diff', 'report': output / 'instrumentation.json', 'sites': len(plan['sites']),
                'behaviorVerified': False}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_instrumented_source(source, app_profile):
    source = Path(source)
    receipt = read_json(source / MARKER)
    require(isinstance(receipt, dict), 'Invalid instrumentation source receipt')
    require(read_json(source.parent / 'instrumentation.json') == receipt
            and sha_file(source.parent / 'patch.diff') == receipt.get('patchSha256'),
            'Instrumentation source marker differs from its external transformation receipt')
    files = snapshot_source(source, source_inputs=app_profile.data.get('sourceInputs'), isolated=True)
    expected_kind = ('build-instrumentation' if app_profile.data['instrumentation']['kind'] == 'android_asm_v1'
                     else 'source-instrumentation')
    require(receipt.get('schemaVersion') == 1 and receipt.get('kind') == expected_kind
            and receipt.get('appProfileDigest') == app_profile.digest
            and receipt.get('instrumentedFiles') == files and receipt.get('instrumentedSourceDigest') == digest(files)
            and receipt.get('sites') == app_profile.data['instrumentation']['sites'],
            'Instrumented source differs from its source transformation receipt')
    if expected_kind == 'build-instrumentation':
        original = receipt.get('originalFiles', {})
        require(isinstance(original, dict) and bool(original)
                and receipt.get('originalSourceDigest') == digest(original)
                and receipt.get('productSourcesUnchanged') is True
                and all(files.get(path) == checksum for path, checksum in original.items() if '/src/' in '/' + path),
                'Build instrumentation changed an original product source')
    return receipt
