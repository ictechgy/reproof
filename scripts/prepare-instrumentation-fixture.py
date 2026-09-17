#!/usr/bin/env python3
"""Create a plain synthetic Android app with no recorder or Report UI."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reproloop.android_profile import sample_app_profile, validate_app_profile
from reproloop.core import require
from reproloop.repair import copy_source
from reproloop.storage import write_json


def prepare(output):
    output = Path(output).resolve()
    require(not output.exists(), 'Use a new fixture directory')
    output.mkdir(parents=True, mode=0o700)
    source = output / 'source'
    copy_source(ROOT / 'android', source)
    for path in (source / 'sample').rglob('*'):
        if path.is_file():
            path.write_text(path.read_text().replace('io.reproloop.sample', 'io.reproloop.plain'))
    activity = source / 'sample/src/main/java/io/reproloop/sample/MainActivity.kt'
    activity.write_text((ROOT / 'tests/fixtures/plain_activity.kt').read_text())
    build = source / 'sample/build.gradle.kts'
    text = build.read_text()
    require(text.count('    implementation(project(":sdk"))') == 1, 'Fixture SDK dependency changed')
    text = text.replace('    implementation(project(":sdk"))\n', '')
    # A development signature is used solely to install the release variant in
    # local equivalence QA; no production key or publishing task is involved.
    text = text.replace('    buildFeatures.buildConfig = true',
        '    buildTypes.getByName("release").signingConfig = signingConfigs.getByName("debug")\n\n'
        '    buildFeatures.buildConfig = true')
    build.write_text(text)
    (source / 'sample/src/main/res/values/ids.xml').write_text(
        '<resources>\n' + ''.join(f'    <item type="id" name="{name}" />\n' for name in ('name','count','add','crash')) + '</resources>\n')
    strings = source / 'sample/src/main/res/values/strings.xml'
    strings.write_text(strings.read_text().replace('Repro Loop Sample', 'Plain Instrumentation Fixture'))
    config = sample_app_profile().data
    config.update(id='plain', package='io.reproloop.plain')
    config['targets']['tap'] = ['add', 'crash']
    config['targets']['scroll'] = {}
    profile = validate_app_profile(config)
    write_json(output / 'app-profile.json', profile.data)
    write_json(output / 'fixture.json', {'synthetic': True, 'instrumented': False,
        'manualSdkCalls': 0, 'reportView': False, 'releaseSigning': 'development-only',
        'cases': ['normal add', 'labeled early return', 'uncaught handler exception']})
    return {'source': str(source), 'appProfile': str(output / 'app-profile.json'), 'manualSdkCalls': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.output))
