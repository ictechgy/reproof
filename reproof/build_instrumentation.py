"""Prepare test-build logging without rewriting application source bodies."""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

from .android_profile import validate_app_profile
from .resources import read_resource, resource_root
from .core import ContractError, digest, require
from .instrumentation import MARKER, merge_debug_manifest, render_runtime_config
from .repair import copy_source, snapshot_source
from .storage import read_json, sha_file, write_json

BUILD_TEMPLATES = resource_root() / 'reproloop/build_instrumentation_templates'
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def is_build_instrumented(profile):
    return profile is not None and profile.data.get('instrumentation', {}).get('kind') == 'android_asm_v1'


def build_coordinates(profile):
    parts = profile.data['build']['task'].split(':')
    task = parts[-1]
    require(task.startswith('assemble') and task.endswith('Debug'), 'Build instrumentation requires one Debug variant')
    variant = task[len('assemble'):]
    return Path(*parts[1:-1]), variant[0].lower() + variant[1:]


def activity_class(profile):
    activity = profile.component_name.split('/', 1)[1]
    return profile.data['package'] + activity if activity.startswith('.') else activity


def prepare_build_instrumentation(source, app_profile, output, *, analyzer=None):
    from .android_observation import is_observation_profile, validate_observation_profile
    observations = is_observation_profile(app_profile)
    source, output = Path(source), Path(output)
    require(source.is_dir() and not source.is_symlink(), 'Invalid instrumentation source directory')
    require(not output.exists() and not output.is_symlink(), 'Instrumentation output already exists')
    source, output = source.resolve(), output.resolve()
    require(not output.is_relative_to(source), 'Instrumentation output must be outside the original source')
    require(app_profile.data.get('captureMode', 'report_view') == 'report_view' and not (source / MARKER).exists(),
            'Source is already instrumented; start from the original source')
    require(not observations or 'instrumentation' not in app_profile.data,
            'Select an original Views observation profile')
    inputs = app_profile.data.get('sourceInputs')
    require(inputs is not None or not (source / 'buildSrc').exists() and not (source / 'buildSrc').is_symlink(),
            'Existing buildSrc requires explicit public sourceInputs')
    plugin_root = 'reproloop-build-logic' if inputs is not None else 'buildSrc'
    require(not (source / plugin_root).exists() and not (source / plugin_root).is_symlink(),
            'Reserved instrumentation plugin directory already exists')
    module, variant = build_coordinates(app_profile)
    build_file = module / 'build.gradle.kts'
    require((source / build_file).is_file(), 'Build instrumentation requires the selected module Kotlin Gradle file')
    require(not (source / module / 'reproloop-instrumentation').exists(), 'Build instrumentation runtime already exists')
    if inputs is not None:
        from .android_sources import freeze_source, source_hashes
        original = freeze_source(source, inputs)
        original_bytes = dict(original.entries)
        before = source_hashes(original)
    else:
        before = snapshot_source(source)
    prefix = (module / 'src/main').as_posix() + '/'
    files = {name: (original_bytes[name].decode() if inputs is not None else (source / name).read_text())
             for name in before if name.startswith(prefix) and name.endswith('.kt')}
    require(bool(files), 'No supported Kotlin product sources found')
    if analyzer is None:
        from .kotlin_instrumenter import instrument_kotlin
        analyzer = instrument_kotlin
    taps = app_profile.data['tapTargets'] if observations else app_profile.data['targets']['tap']
    plan = analyzer(files, activity_class(app_profile), taps)
    require(isinstance(plan, dict) and plan.get('schemaVersion') == 1 and plan.get('activityPath') in files
            and isinstance(plan.get('sites'), list), 'Invalid instrumentation site analysis')
    config = app_profile.data
    if not observations:
        config['captureMode'] = 'debug_receiver'
        config['targets']['report'] = None
        config['appLogs'] = 1
    config['instrumentation'] = {'kind': 'android_asm_v1', 'sites': plan['sites']}
    plugin_names = ('settings.gradle.kts', 'build.gradle.kts', *(f'src/main/java/io/reproloop/instrumentation/gradle/{name}.java'
        for name in ('ReproBytecodeTransformer', 'ReproInstrumentationPlugin', 'ReproInstrumentationTask')))
    runtime_names = ('io/reproloop/autotrace/ReproAuto.kt', 'io/reproloop/autotrace/ReproAppLogs.kt') + (
        () if observations else ('io/reproloop/autotrace/AutoExportReceiver.kt',))
    generated = [f'{plugin_root}/{name}' for name in plugin_names] + [
        f'{plugin_root}/src/main/java/io/reproloop/instrumentation/gradle/ReproPlan.java',
        (module / 'reproloop-instrumentation/AndroidManifest.xml').as_posix()] + [
        (module / 'reproloop-instrumentation/runtime' / name).as_posix() for name in (*runtime_names,
            'io/reproloop/autotrace/ReproHooks.kt', 'io/reproloop/autotrace/ReproConfig.kt',
            *(() if observations else ('io/reproloop/sdk/ReproRecorder.kt',)))]
    if observations:
        generated.append((module / 'reproloop-instrumentation/assets/reproloop-observation.json').as_posix())
    if inputs is not None:
        require(not set(inputs) & set(generated), 'Reserved Android instrumentation source input')
        config['sourceInputs'] = sorted(inputs + generated)
        from .kotlin_instrumenter import configure_gradle
        integration = configure_gradle(original_bytes['settings.gradle.kts'].decode(), original_bytes[build_file.as_posix()].decode())
    profile = validate_observation_profile(config) if observations else validate_app_profile(config)
    sites = sorted(plan['sites'], key=lambda site: site['line'])
    require(all(site['path'] == plan['activityPath'] for site in sites)
            and len({site['line'] for site in sites}) == len(sites)
            and len({site['target'] for site in sites}) == len(sites),
            'Ambiguous bytecode line mapping for configured taps')
    plugin = BUILD_TEMPLATES / 'buildSrc'
    hooks = BUILD_TEMPLATES / 'runtime/io/reproloop/autotrace/ReproHooks.kt'
    require((plugin / 'build.gradle.kts').is_file()
            and (plugin / 'src/main/java/io/reproloop/instrumentation/gradle/ReproInstrumentationPlugin.java').is_file()
            and hooks.is_file(), 'Build instrumentation templates are unavailable')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.' + output.name + '.', dir=output.parent))
    try:
        workspace = staging / 'source'
        require(copy_source(source, workspace, source_inputs=inputs) == before, 'Source changed while preparing build instrumentation')
        for name in plugin_names:
            destination = workspace / plugin_root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(read_resource('reproloop/build_instrumentation_templates/buildSrc/' + name))
        if inputs is not None:
            with (workspace / plugin_root / 'build.gradle.kts').open('a') as handle:
                handle.write('\ngradlePlugin {\n    plugins {\n        create("reproInstrumentation") {\n'
                    '            id = "io.reproloop.instrumentation"\n'
                    '            implementationClass = "io.reproloop.instrumentation.gradle.ReproInstrumentationPlugin"\n'
                    '        }\n    }\n}\n')
        # The plan is a protected build input. Product code and the selected
        # activity remain exactly as supplied; PSI's rewritten source is unused.
        plan_java = workspace / plugin_root / 'src/main/java/io/reproloop/instrumentation/gradle/ReproPlan.java'
        plan_java.parent.mkdir(parents=True, exist_ok=True)
        entries = ',\n        '.join(f'Map.entry({site["line"]}, {json.dumps(site["id"])})' for site in sites)
        plan_java.write_text('package io.reproloop.instrumentation.gradle;\n\nimport java.util.Map;\n\n'
            'public final class ReproPlan {\n'
            f'    public static final String MODULE = {json.dumps(":" + ":".join(config["build"]["task"].split(":")[1:-1]))};\n'
            f'    public static final String VARIANT = {json.dumps(variant)};\n'
            f'    public static final String ACTIVITY = {json.dumps(activity_class(profile))};\n'
            f'    public static final String PROFILE_DIGEST = {json.dumps(profile.digest)};\n'
            f'    public static final Map<Integer, String> SITES = Map.ofEntries(\n        {entries}\n    );\n'
            '    private ReproPlan() {}\n}\n')
        runtime = workspace / module / 'reproloop-instrumentation/runtime'
        for name in runtime_names:
            destination = runtime / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            template_root = ('reproloop/observation_templates/android/' if observations and name.endswith('/ReproAuto.kt')
                             else 'reproloop/instrumentation_templates/android/debug/java/')
            destination.write_bytes(read_resource(template_root + name))
        (runtime / 'io/reproloop/autotrace/ReproHooks.kt').write_bytes(read_resource(
            'reproloop/build_instrumentation_templates/runtime/io/reproloop/autotrace/ReproHooks.kt'))
        if observations:
            from .android_observation import render_runtime_config as render_observation_config
            runtime_config = render_observation_config(profile, sites)
            write_json(workspace / module / 'reproloop-instrumentation/assets/reproloop-observation.json', profile.data)
        else:
            runtime_config = render_runtime_config(profile, sites)
            sdk = runtime / 'io/reproloop/sdk/ReproRecorder.kt'
            sdk.parent.mkdir(parents=True, exist_ok=True)
            sdk.write_bytes(read_resource('android/sdk/src/main/java/io/reproloop/sdk/ReproRecorder.kt'))
        (runtime / 'io/reproloop/autotrace/ReproConfig.kt').write_text(runtime_config)
        original_manifest = workspace / module / 'src/debug/AndroidManifest.xml'
        manifest = original_manifest.read_text() if original_manifest.exists() else '<manifest/>\n'
        (workspace / module / 'reproloop-instrumentation/AndroidManifest.xml').write_text(
            manifest if observations else merge_debug_manifest(manifest))
        if inputs is not None:
            (workspace / 'settings.gradle.kts').write_text(integration['settings.gradle.kts'])
            (workspace / build_file).write_text(integration['module.gradle.kts'])
        else:
            with (workspace / build_file).open('a') as handle:
                handle.write('\n// Repro Loop test-build instrumentation; application sources are unchanged.\n'
                             'apply<io.reproloop.instrumentation.gradle.ReproInstrumentationPlugin>()\n')
        after = snapshot_source(workspace, source_inputs=config.get('sourceInputs'), isolated=True)
        require(all(after.get(name) == checksum for name, checksum in before.items()
                    if name not in {build_file.as_posix(), 'settings.gradle.kts'}),
                'Build instrumentation modified an original product source')
        changed = sorted(name for name in after if after[name] != before.get(name))
        patch = ''.join(''.join(difflib.unified_diff(
            ((original_bytes[name].decode() if inputs is not None else (source / name).read_text()).splitlines(True)
             if name in before else []),
            (workspace / name).read_text().splitlines(True), fromfile='a/' + name, tofile='b/' + name, n=0)) for name in changed)
        (staging / 'patch.diff').write_text(patch)
        write_json(staging / 'app-profile.json', profile.data)
        receipt = {'schemaVersion': 1, 'kind': 'build-instrumentation', 'status': 'prepared',
            'originalSourceDigest': digest(before), 'instrumentedSourceDigest': digest(after),
            'originalFiles': before, 'instrumentedFiles': after, 'appProfileDigest': profile.digest,
            'sites': profile.data['instrumentation']['sites'], 'changedFiles': changed,
            'patchSha256': sha_file(staging / 'patch.diff'), 'debugTask': config['build']['task'],
            'releaseTask': config['build']['task'][:-5] + 'Release', 'productSourcesUnchanged': True,
            'behaviorVerified': False, 'parser': 'kotlin-psi', 'transformer': 'android-asm'}
        write_json(workspace / MARKER, receipt)
        write_json(staging / 'instrumentation.json', receipt)
        require(snapshot_source(source, source_inputs=inputs) == before, 'Original source changed during instrumentation')
        staging.rename(output)
        return {'status': 'prepared', 'mode': 'build', 'source': output / 'source',
                'appProfile': output / 'app-profile.json', 'patch': output / 'patch.diff',
                'report': output / 'instrumentation.json', 'sites': len(sites),
                'productSourcesUnchanged': True, 'behaviorVerified': False}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_bytecode_artifacts(source, profile):
    require(is_build_instrumented(profile), 'Select the bytecode instrumentation profile')
    source = Path(source).resolve()
    module, variant = build_coordinates(profile)
    directory = source / module / 'build/reproloop' / variant
    for path in [directory, *directory.parents]:
        require(not path.is_symlink(), 'Linked bytecode instrumentation output')
        if path == source:break
    report_file, jar_file = directory / 'report.json', directory / 'classes.jar'
    require(report_file.is_file() and not report_file.is_symlink() and report_file.stat().st_size <= 1024 * 1024
            and jar_file.is_file() and not jar_file.is_symlink() and 0 < jar_file.stat().st_size <= 256 * 1024 * 1024,
            'Missing or invalid bytecode instrumentation output')
    report = read_json(report_file)
    expected_keys = {'schemaVersion', 'kind', 'appProfileDigest', 'activityClass', 'instrumentedClasses',
                     'sites', 'lifecycle', 'inputClassSha256', 'outputClassSha256', 'outputJarSha256'}
    sites = sorted(({'id': s['id'], 'line': s['line']} for s in profile.data['instrumentation']['sites']), key=lambda s: s['line'])
    require(isinstance(report, dict) and set(report) == expected_keys
            and type(report['schemaVersion']) is int and report['schemaVersion'] == 1
            and report['kind'] == 'android_asm_v1' and report['appProfileDigest'] == profile.digest
            and report['activityClass'] == activity_class(profile)
            and type(report['instrumentedClasses']) is int and report['instrumentedClasses'] == 1
            and report['sites'] == sites and isinstance(report['lifecycle'], dict)
            and set(report['lifecycle']) == {'onCreate', 'onDestroy'}
            and all(value is True for value in report['lifecycle'].values())
            and all(isinstance(report[k], str) and SHA256.fullmatch(report[k])
                    for k in ('inputClassSha256', 'outputClassSha256', 'outputJarSha256'))
            and report['inputClassSha256'] != report['outputClassSha256'],
            'Bytecode instrumentation did not cover the selected contract')
    require(sha_file(jar_file) == report['outputJarSha256'], 'Transformed bytecode archive changed')
    try:
        with zipfile.ZipFile(jar_file) as archive:
            names = archive.namelist()
            require(len(names) <= 100000 and len(set(names)) == len(names), 'Ambiguous transformed class archive')
            entry = archive.getinfo(activity_class(profile).replace('.', '/') + '.class')
            require(0 < entry.file_size <= 16 * 1024 * 1024, 'Transformed activity exceeds the class limit')
            checksum = hashlib.sha256(archive.read(entry)).hexdigest()
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        raise ContractError('Invalid transformed activity archive') from None
    require(checksum == report['outputClassSha256'], 'Transformed activity differs from its build report')
    return {'kind': 'android_asm_v1', 'report': report, 'reportSha256': sha_file(report_file),
            'transformedJarSha256': report['outputJarSha256']}
