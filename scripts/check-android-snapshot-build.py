#!/usr/bin/env python3
"""Compile two public synthetic snapshot consumers; never run an Android device.

The explicit online warm builds populate an isolated cache. Only the subsequent
consumer builds are offline. Both phases disable Debug signing before tasks.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ACTIVITY = 'app/src/main/java/com/example/snapshotsynthetic/MainActivity.kt'
PACKAGE = 'com.example.snapshotsynthetic'
def build_selection(dsl):
    if dsl == 'kotlin':
        return ':app:assembleDebug', 'app/build/outputs/apk/debug/app-debug-unsigned.apk'
    if dsl == 'groovy':
        return ':app:assembleDemoDebug', 'app/build/outputs/apk/demo/debug/app-demo-debug-unsigned.apk'
    raise ValueError('Unsupported synthetic DSL')

PSI_DEPENDENCIES = (
    'org.jetbrains.kotlin:kotlin-compiler-embeddable:2.2.10',
    'org.jetbrains.kotlin:kotlin-stdlib:2.2.10',
    'org.jetbrains.kotlin:kotlin-script-runtime:2.2.10',
    'org.jetbrains.intellij.deps:trove4j:1.0.20200330',
    'org.jetbrains:annotations:23.0.0',
    'org.jetbrains.kotlinx:kotlinx-coroutines-core-jvm:1.8.0',
    'org.jetbrains.kotlin:kotlin-reflect:1.6.10',
)


def fixture(root, dsl):
    if dsl not in {'kotlin', 'groovy'}:
        raise ValueError('Unsupported synthetic DSL')
    settings = '''pluginManagement {
    repositories { google(); mavenCentral(); gradlePluginPortal() }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories { google(); mavenCentral() }
}
'''
    if dsl == 'kotlin':
        settings += 'rootProject.name = "snapshot-synthetic"\ninclude(":app")\n'
        module = '''plugins {
    id("com.android.application") version "8.13.2"
    id("org.jetbrains.kotlin.android") version "2.2.10"
}
android {
    namespace = "com.example.snapshotsynthetic"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.example.snapshotsynthetic"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
        signingConfig = signingConfigs.getByName("debug")
    }
    buildTypes { getByName("debug") { signingConfig = null } }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
kotlin { jvmToolchain(17) }
'''
        suffix = '.gradle.kts'
    else:
        settings += "rootProject.name = 'snapshot-synthetic'\ninclude ':app'\n"
        module = '''plugins {
    id 'com.android.application' version '8.13.2'
    id 'org.jetbrains.kotlin.android' version '2.2.10'
}
android {
    namespace 'com.example.snapshotsynthetic'
    compileSdk 35
    defaultConfig {
        applicationId 'com.example.snapshotsynthetic'
        minSdk 26
        targetSdk 35
        versionCode 1
        versionName '1.0'
        signingConfig = signingConfigs.debug
    }
    flavorDimensions 'qa'
    productFlavors {
        demo {
            dimension 'qa'
            signingConfig = signingConfigs.debug
        }
    }
    buildTypes { debug { signingConfig = null } }
    compileOptions {
        sourceCompatibility JavaVersion.VERSION_17
        targetCompatibility JavaVersion.VERSION_17
    }
}
kotlin { jvmToolchain(17) }
'''
        suffix = '.gradle'
    files = {'settings' + suffix: settings, 'app/build' + suffix: module,
        'app/src/main/AndroidManifest.xml': '''<manifest xmlns:android="http://schemas.android.com/apk/res/android">
<application android:label="Snapshot Synthetic" android:theme="@android:style/Theme.Material.Light.NoActionBar">
<activity android:name=".MainActivity" android:exported="false"/>
</application></manifest>
''',
        'app/src/main/res/values/ids.xml': '<resources><item name="save" type="id"/><item name="inventory_root" type="id"/></resources>\n',
        ACTIVITY: '''package com.example.snapshotsynthetic
import android.app.Activity
import android.os.Bundle
import android.widget.Button
import android.widget.LinearLayout
class MainActivity : Activity() {
    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        val root = LinearLayout(this).apply { id = R.id.inventory_root }
        val save = Button(this).apply {
            id = R.id.save
            setOnClickListener { text = "Saved" }
        }
        root.addView(save)
        setContentView(root)
    }
}
'''}
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    return sorted(files)


def bootstrap(root, gradle, environment):
    project = root / 'psi-bootstrap'; project.mkdir()
    (project / 'settings.gradle.kts').write_text('rootProject.name = "reproof-public-psi-bootstrap"\n')
    dependencies = '\n'.join(f'    psi("{item}")' for item in PSI_DEPENDENCIES)
    (project / 'build.gradle.kts').write_text('''repositories { mavenCentral() }
val psi by configurations.creating
dependencies {
''' + dependencies + '''
}
tasks.register("resolvePinnedPsi") { doLast { psi.resolve() } }
''')
    run([str(gradle), '--no-daemon', '--no-build-cache', '--console=plain', 'resolvePinnedPsi'],
        project, environment, root / 'psi-bootstrap.log')
    run(['/bin/sh', str(ROOT / 'tools/kotlin-instrumenter/build.sh')], ROOT,
        environment, root / 'psi-compile.log')


def run(command, cwd, environment, log):
    with log.open('w') as stream:
        result = subprocess.run(command, cwd=cwd, env=environment, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=600)
    if result.returncode:
        raise RuntimeError('Synthetic compile command failed; retained log available')


def no_keys(root):
    # Only inspect the dedicated public-fixture workspace, never user key stores.
    for path in root.rglob('*'):
        if path.name == 'debug.keystore' or path.suffix.lower() in {'.keystore', '.jks'}:
            raise RuntimeError('Unexpected signing credential in synthetic workspace')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gradle', type=Path, required=True)
    parser.add_argument('--java-home', type=Path, required=True)
    parser.add_argument('--sdk-home', type=Path, required=True)
    args = parser.parse_args()
    root = args.output.absolute(); root.mkdir(mode=0o700)
    environment = {key: value for key, value in os.environ.items()
                   if key in {'PATH', 'USER', 'LANG', 'LC_ALL'}}
    # A dedicated HOME also matches the supported PSI builder's exact cache path.
    home = root / 'home'; home.mkdir(mode=0o700)
    android_home = root / 'android-user-home'; android_home.mkdir(mode=0o700)
    environment.update(HOME=str(home), GRADLE_USER_HOME=str(home / '.gradle'),
        ANDROID_USER_HOME=str(android_home), JAVA_HOME=str(args.java_home),
        ANDROID_HOME=str(args.sdk_home), ANDROID_SDK_ROOT=str(args.sdk_home))
    os.environ.update({key: environment[key] for key in ('HOME', 'GRADLE_USER_HOME',
        'ANDROID_USER_HOME', 'JAVA_HOME', 'ANDROID_HOME', 'ANDROID_SDK_ROOT')})
    bootstrap(root, args.gradle, environment)
    from reproof.source_snapshot import capture, materialize
    from reproof.android_snapshot_observation import (validate_snapshot_observation_profile,
        prepare_snapshot_observation, build_snapshot_observation_app, _unsigned_apk, _apk_identity)
    from reproof.android_observation import profile_from_apk
    proofs = []
    for dsl in ('kotlin', 'groovy'):
        case = root / dsl; case.mkdir()
        task, apk_relative = build_selection(dsl)
        names = fixture(case / 'original', dsl)
        summary = capture(case / 'original', names, case / 'original-snapshot')
        document = {'schemaVersion': 3, 'kind': 'views-observation-v3',
            'package': PACKAGE, 'activity': '.MainActivity', 'activitySource': ACTIVITY,
            'build': {'task': task, 'apk': apk_relative, 'dsl': dsl},
            'tapTargets': ['save'], 'screenTargets': {'inventory_root': 'inventory'},
            'sourceSnapshot': {'schemaVersion': 1, 'kind': 'reproof-local-source-snapshot',
                **{key: summary[key] for key in ('digest', 'files', 'bytes')}}}
        profile = validate_snapshot_observation_profile(document)
        prepared = prepare_snapshot_observation(case / 'original-snapshot', profile, case / 'prepared')
        # API result keys are checked below, not inferred from application output.
        prepared_profile = validate_snapshot_observation_profile(json.loads(Path(prepared['appProfile']).read_text()))
        snapshot = Path(prepared['source'])
        warm = case / 'warm'; materialize(snapshot, warm)
        run([str(args.gradle), '--no-daemon', '--no-build-cache', '--console=plain',
             task, '--dry-run'], warm, environment, case / 'online-task-plan.log')
        planned = [line.split()[0] for line in (case / 'online-task-plan.log').read_text().splitlines()
                   if line.startswith(':')]
        assert planned and not any('sign' in task.lower() for task in planned)
        run([str(args.gradle), '--no-daemon', '--no-build-cache', '--console=plain', task],
            warm, environment, case / 'online-warm.log')
        assert _unsigned_apk(warm / apk_relative) == _apk_identity(warm / apk_relative)
        no_keys(root)
        result = build_snapshot_observation_app(snapshot, case / 'build', profile=prepared_profile,
            gradle=args.gradle, java_home=args.java_home, sdk_home=args.sdk_home, timeout=600)
        receipt = result['receipt']
        assert receipt['buildTask'] == task
        assert receipt['buildCompleted'] is True and receipt['automaticObservations'] is True
        assert receipt['behaviorVerified'] is False and receipt['signed'] is False
        embedded = profile_from_apk(result['app'])
        assert embedded is not None and embedded.digest == prepared_profile.digest
        bytecode = receipt['bytecodeInstrumentation']
        assert bytecode['kind'] == 'android_asm_v1'
        assert bytecode['report']['appProfileDigest'] == prepared_profile.digest
        assert bytecode['report']['instrumentedClasses'] == 1
        assert bytecode['report']['lifecycle'] == {'onCreate': True, 'onDestroy': True}
        assert len(bytecode['report']['sites']) == 1
        no_keys(root)
        proofs.append({'dsl': dsl, 'buildTask': task, 'compiledApps': 1, 'schemaVersion': 3,
            'signed': False, 'deviceExecution': False, 'behaviorVerified': False,
            'onlineWarmBuild': True, 'offlineConsumerBuild': True, 'asmVerified': True,
            'embeddedProfileVerified': True, 'keystoreCreated': False})
    report = {'status': 'pass', 'cases': proofs}
    (root / 'compile-proof.json').write_text(json.dumps(report, sort_keys=True) + '\n')
    print('ANDROID_SNAPSHOT_BUILD ' + json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
