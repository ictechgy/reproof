#!/usr/bin/env python3
"""Run opt-in sample capture diagnostics on the connected, authorized iPhone."""
import argparse
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reproof.ios_signing import sign_products
from reproof.live.iphone import select_iphone, _devicectl, validate_signed_products
from reproof.ios_runner import prepare_xctestrun, _targets
from reproof.storage import read_json, write_json

p = argparse.ArgumentParser()
p.add_argument('--build', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
assert not a.output.exists()
receipt = read_json(a.build / 'receipt.json')
device = select_iphone()
for part in ['runner', 'sample']:
    sign_products(a.build / part / 'Build/Products', device)
receipt.update(signed=True, signingMethod='existing-local-development-profile',
    applicationIdentity=validate_signed_products(receipt['runnerProducts'], receipt['sampleApp']))
write_json(a.build / 'receipt.json', receipt)
_devicectl('device', 'install', 'app', '--device', device.identifier, receipt['sampleApp'], timeout=90)
a.output.mkdir(parents=True, mode=0o700)
with tempfile.TemporaryDirectory(prefix='repro-capture-probe-') as directory:
    temporary = Path(directory)
    config = prepare_xctestrun(receipt['runnerProducts'], 'ReproLiveTests', temporary / 'probe.xctestrun')
    document = plistlib.loads(config.read_bytes())
    for _, target in _targets(document):
        target.setdefault('EnvironmentVariables', {}).update(REPRO_CAPTURE_DIAGNOSTICS='1',
            REPRO_TARGET_BUNDLE='io.reproof.sample.ios')
    config.write_bytes(plistlib.dumps(document))
    run = subprocess.run(['/usr/bin/xcodebuild', 'test-without-building', '-xctestrun', str(config),
        '-destination', 'id=' + device.udid, '-resultBundlePath', str(temporary / 'result.xcresult'),
        '-parallel-testing-enabled', 'NO', '-only-testing:ReproLiveTests/LiveControlTests/testCaptureFormats'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    exported = subprocess.run(['/usr/bin/xcrun', 'xcresulttool', 'export', 'attachments', '--path',
        str(temporary / 'result.xcresult'), '--output-path', str(temporary / 'attachments')],
        capture_output=True, timeout=30)
    count = 0
    if exported.returncode == 0:
        for test in read_json(temporary / 'attachments/manifest.json'):
            for entry in test.get('attachments', []):
                match = re.match(r'(capture-\d+-\d+-(?:raw-png|converted-jpeg))_', entry.get('suggestedHumanReadableName', ''))
                if match is None:
                    continue
                source = temporary / 'attachments' / entry['exportedFileName']
                assert source.resolve().is_relative_to(temporary.resolve())
                suffix = '.png' if 'raw-png' in match[1] else '.jpg'
                shutil.copyfile(source, a.output / (match[1] + suffix))
                count += 1
    proof = {'passed': run.returncode == 0 and count == 18, 'attachmentCount': count,
             'executionEnvironment': 'physical-iphone'}
    write_json(a.output / 'result.json', proof)
    print(proof)
    raise SystemExit(0 if proof['passed'] else 2)
