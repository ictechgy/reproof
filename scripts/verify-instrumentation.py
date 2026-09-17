#!/usr/bin/env python3
"""Check functional equivalence and debug/release recording on an owned AVD."""
import argparse
import hashlib
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reproloop.android_profile import load_app_profile
from reproloop.core import ContractError
from reproloop.device import AdbDevice, DeviceError, DRIVER
from reproloop.storage import read_json, write_json, sha_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--plain-build', type=Path, required=True)
    parser.add_argument('--instrumented-build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    args.output.mkdir(parents=True, mode=0o700)
    runtime = read_json(args.runtime)
    assert runtime['owned'] and runtime['ready']
    plain_profile = load_app_profile(args.plain_build / 'app-profile.json')
    auto_profile = load_app_profile(args.instrumented_build / 'app-profile.json')
    plain = AdbDevice('emulator-' + str(runtime['port']), app_profile=plain_profile)
    auto = AdbDevice(plain.serial, app_profile=auto_profile)
    assert 'android-' + plain.identity == runtime['deviceId']
    assert plain.adb_call('emu', 'avd', 'name').decode().splitlines()[0] == runtime['name']
    driver = args.instrumented_build / 'driver.apk'
    plain_apk, auto_apk = args.plain_build / 'original.apk', args.instrumented_build / 'original.apk'
    release_rel = 'app-source/sample/build/outputs/apk/buggy/release/sample-buggy-release.apk'
    release_apk = args.instrumented_build / release_rel
    result = {'passed': False, 'executionEnvironment': 'owned-android-emulator', 'syntheticFixture': True,
              'cases': [], 'captures': [], 'originalApkSha256': sha_file(plain_apk),
              'instrumentedApkSha256': sha_file(auto_apk), 'releaseApkSha256': sha_file(release_apk)}

    def run_case(device, apk, value, expected):
        device.prepare(apk, device.app_profile.data['fixture'])
        try:
            device.driver('replace', target='name', value=value)
            device.driver('tap', target='add')
            final = device.observe()
            assert final == {'name': value, 'count': expected}
            return final
        finally:
            device.stop()

    def crash_case(device, apk):
        device.prepare(apk, device.app_profile.data['fixture'])
        try:
            tap_acknowledged = True
            try:device.driver('tap', target='crash')
            except DeviceError:
                # A synchronous throwing callback can remove its window before
                # ACTION_CLICK acknowledges. Require the subsequent failure
                # state rather than counting this transport error as success.
                tap_acknowledged = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    device.observe()
                except (DeviceError, ContractError):
                    return {'appUnavailableAfterThrow': True, 'tapAcknowledged': tap_acknowledged}
                time.sleep(.2)
            raise AssertionError('Expected throwing handler remained observable')
        finally:
            device.stop()

    def snapshot_sdk(device, session_id):
        paths = ['capture.json', 'diagnostics.json', 'current-session',
                 session_id + '/events.jsonl', session_id + '/session.json', session_id + '/capture.json']
        return {name: hashlib.sha256(device.adb_call('exec-out', 'run-as', device.package,
                        'cat', 'files/repro/' + name)).hexdigest() for name in paths}

    with plain.lease():
        try:
            plain.install(driver, DRIVER)
            for value, expected in [('QA', '2'), ('Test', '0')]:
                before = run_case(plain, plain_apk, value, expected)
                after = run_case(auto, auto_apk, value, expected)
                result['cases'].append({'input': value, 'before': before, 'after': after, 'equal': before == after})
                print({'functionalCase': value, 'equal': True}, flush=True)
            result['throwBefore'] = crash_case(plain, plain_apk)
            result['throwAfter'] = crash_case(auto, auto_apk)
            result['throwPreserved'] = (result['throwBefore']['appUnavailableAfterThrow']
                                        and result['throwAfter']['appUnavailableAfterThrow'])
            print({'throwPreserved': result['throwPreserved']}, flush=True)
            for value, expected in [('Test', '0'), ('QA', '2')]:
                auto.prepare(auto_apk, auto_profile.data['fixture'], 'record')
                auto.driver('replace', target='name', value=value)
                auto.driver('tap', target='add')
                capture = auto.freeze_capture()
                diagnostic = auto.collect_instrumentation_diagnostics(capture)
                assert diagnostic['actions'][0]['before'] == {'count': '0'}
                assert diagnostic['actions'][0]['after'] == {'count': expected}
                assert diagnostic['actions'][0]['outcome'] == 'returned'
                write_json(args.output / (value.lower() + '-capture.json'), capture)
                write_json(args.output / (value.lower() + '-diagnostics.json'), diagnostic)
                result['captures'].append({'input': value, 'events': len(capture['events']),
                    'stateBefore': diagnostic['actions'][0]['before'], 'stateAfter': diagnostic['actions'][0]['after']})
                auto.stop()
            saved = snapshot_sdk(auto, capture['sessionId'])
            # Request record mode explicitly in the release variant. The host
            # does not delete prior SDK data; it must remain byte-identical.
            auto.install(release_apk)
            auto.shell('am', 'start', '-W', '-n', auto_profile.component_name,
                '--es', 'repro_mode', 'record', '--es', 'fixture_id', auto_profile.data['fixture']['id'],
                '--ei', 'fixture_version', str(auto_profile.data['fixture']['version']))
            auto.driver('replace', target='name', value='QA')
            auto.driver('tap', target='add')
            assert auto.observe() == {'name': 'QA', 'count': '2'}
            auto.stop()
            auto.install(auto_apk)
            assert snapshot_sdk(auto, capture['sessionId']) == saved
            result['releaseWritesNoRecording'] = True
            result['releaseIsolation'] = read_json(args.instrumented_build / 'release-isolation.json')
            assert result['releaseIsolation']['apkSha256'] == sha_file(release_apk)
            result['sourceReceiptDigest'] = read_json(args.instrumented_build / 'receipt.json')['instrumentationReceiptDigest']
            result['originalApkSha256'] = sha_file(plain_apk)
            result['instrumentedApkSha256'] = sha_file(auto_apk)
            result['releaseApkSha256'] = sha_file(release_apk)
            result['passed'] = (len(result['cases']) == 2 and all(case['equal'] for case in result['cases'])
                and result['throwPreserved'] and len(result['captures']) == 2
                and result['releaseWritesNoRecording'] is True and result['releaseIsolation']['passed'] is True)
        finally:
            auto.stop()
            result['fixtureStopped'] = True
            write_json(args.output / 'result.json', result)
    print({'passed': result['passed'], 'releaseWritesNoRecording': result.get('releaseWritesNoRecording')}, flush=True)
    return 0 if result['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
