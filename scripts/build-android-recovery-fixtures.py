#!/usr/bin/env python3
"""Build two public, unsigned qualification APKs; never install or execute them."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reproof.android_snapshot_observation import _unsigned_apk

SELECTIONS = (
    ('sample', ':sample:assembleBuggyDebug', 'sample/build/outputs/apk/buggy/debug/sample-buggy-debug-unsigned.apk', 'io.reproof.sample'),
    ('helper', ':live:assembleDebug', 'live/build/outputs/apk/debug/live-debug-unsigned.apk', 'io.reproof.live'),
)
METADATA_TASKS = {':sample:writeBuggyDebugSigningConfigVersions', ':live:writeDebugSigningConfigVersions'}
UNSIGNED = '''
// Qualification artifact copy only: never sign or generate a debug key.
android {
    defaultConfig { signingConfig = null }
    productFlavors.configureEach { signingConfig = null }
    buildTypes { getByName("debug") { signingConfig = null } }
}
'''


def require_plan(text):
    tasks = {line.split()[0] for line in text.splitlines() if line.startswith(':')}
    expected = {entry[1] for entry in SELECTIONS} | METADATA_TASKS
    if not expected <= tasks or any(
            task not in METADATA_TASKS and any(word in task.lower() for word in ('sign', 'key'))
            for task in tasks):
        raise RuntimeError('Unexpected unsigned qualification task plan')


def no_keys(root):
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            count += 1
            path = Path(directory) / name
            if count > 100000 or path.is_symlink() or name.lower().endswith(('.keystore', '.jks')):
                raise RuntimeError('Unexpected key, link or excessive fixture output')


def copy_source(destination):
    raw = subprocess.check_output(['git', 'ls-files', '-z', '--', 'android'], cwd=ROOT, timeout=15)
    names = raw.decode('utf-8').split('\0')[:-1]
    if not names or len(names) > 4096:
        raise RuntimeError('Unexpected public Android source selection')
    total = 0
    for name in names:
        source = ROOT / name
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024:
            raise RuntimeError('Unsupported public fixture source')
        total += info.st_size
        if total > 64 * 1024 * 1024:
            raise RuntimeError('Public fixture source exceeds bound')
        target = destination / Path(name).relative_to('android')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for module in ('sample', 'live'):
        path = destination / module / 'build.gradle.kts'
        path.write_text(path.read_text() + UNSIGNED)


def run(command, cwd, environment, log):
    with log.open('w') as stream:
        result = subprocess.run(command, cwd=cwd, env=environment, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=600)
    if result.returncode:
        raise RuntimeError('Public fixture build failed; inspect retained log')


def publish_apks(source, output, sdk, environment, commit):
    artifacts = []
    aapt = sdk / 'build-tools/35.0.0/aapt'
    for label, task, relative, package in SELECTIONS:
        path = source / relative
        identity = _unsigned_apk(path)
        result = subprocess.run([str(aapt), 'dump', 'badging', str(path)], env=environment,
                                capture_output=True, timeout=30, check=True)
        if len(result.stdout) > 1024 * 1024:
            raise RuntimeError('APK package metadata exceeds bound')
        matches = re.findall(rb"^package: name='([^']+)'", result.stdout, re.MULTILINE)
        if matches != [package.encode()]:
            raise RuntimeError('Unexpected public qualification package')
        target = output / (label + '-unsigned.apk')
        with path.open('rb') as original, target.open('xb') as copied:
            shutil.copyfileobj(original, copied, 1024 * 1024)
        if _unsigned_apk(target) != identity or _unsigned_apk(path) != identity:
            raise RuntimeError('APK identity changed during publication')
        artifacts.append({'name': target.name, 'package': package, 'sha256': identity[0],
                          'bytes': identity[1], 'task': task, 'signed': False})
    manifest = {'schemaVersion': 1, 'kind': 'reproof-public-recovery-unsigned-fixtures',
                'sourceCommit': commit, 'artifacts': artifacts, 'deviceExecution': False,
                'runtimeQualified': False, 'keystoreCreated': False}
    return manifest


def gradle_command(gradle, home):
    # Gradle/AGP can consult the JVM property instead of the process HOME.
    return [str(gradle.resolve()), '-Duser.home=' + str(home.resolve()),
            '--no-daemon', '--no-build-cache', '--console=plain', '--offline',
            *(entry[1] for entry in SELECTIONS)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gradle', type=Path, required=True)
    parser.add_argument('--gradle-home', type=Path, required=True)
    parser.add_argument('--java-home', type=Path, required=True)
    parser.add_argument('--sdk-home', type=Path, required=True)
    args = parser.parse_args()
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, timeout=15).decode().strip()
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise RuntimeError('Invalid source commit')
    root = args.output.absolute()
    root.mkdir(mode=0o700)
    source = root / 'source'; source.mkdir()
    home = root / 'home'; home.mkdir(mode=0o700)
    android_home = root / 'android-home'; android_home.mkdir(mode=0o700)
    artifacts = root / 'artifacts'; artifacts.mkdir()
    copy_source(source)
    environment = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL'}}
    environment.update(HOME=str(home), ANDROID_USER_HOME=str(android_home),
        GRADLE_USER_HOME=str(args.gradle_home.resolve()), JAVA_HOME=str(args.java_home.resolve()),
        ANDROID_HOME=str(args.sdk_home.resolve()), ANDROID_SDK_ROOT=str(args.sdk_home.resolve()))
    command = gradle_command(args.gradle, home)
    try:
        run(command + ['--dry-run'], source, environment, root / 'task-plan.log')
        require_plan((root / 'task-plan.log').read_text())
        no_keys(root)
        run(command, source, environment, root / 'build.log')
        manifest = publish_apks(source, artifacts, args.sdk_home.resolve(), environment, commit)
        no_keys(root)
        (artifacts / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        print('ANDROID_RECOVERY_UNSIGNED_FIXTURES ' + json.dumps(manifest, sort_keys=True))
    finally:
        # Retain all build output on failure; never recursively delete executed trees.
        no_keys(root)


if __name__ == '__main__':
    main()
