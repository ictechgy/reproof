#!/usr/bin/env python3
"""Verify the native sample observation contract on an attached Android device."""
import argparse
import json
import re
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reproloop.device import AdbDevice
from reproloop.live.android_live import android_live_device
from reproloop.live.model import Lab
from reproloop.storage import write_json

p = argparse.ArgumentParser()
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
assert not a.output.exists()
d = AdbDevice()
rotation = d.shell('wm', 'user-rotation').strip()
lab = Lab([android_live_device(d.serial,
    'android/live/build/outputs/apk/debug/live-debug.apk',
    'android/sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk')], a.output)
proof = {'passed': False}
try:
    d.shell('wm', 'user-rotation', 'lock', '0')
    sizes = re.findall(r'(\d+)x(\d+)', d.shell('wm', 'size'))
    width, height = map(int, sizes[-1])
    s = lab.create_session('android-' + d.identity, 'qa', 'inspect-qa')
    sid = s['id']
    deadline = time.monotonic() + 40
    while s['state'] == 'connecting' and time.monotonic() < deadline:
        time.sleep(.1)
        s = lab.get_session(sid, 'qa')
    assert s['state'] == 'active', s['error']
    def nodes():
        observation = lab.observe(sid, 'qa')
        assert observation['ready']
        assert 'inspect-contract-value' not in json.dumps(observation)
        return {node['id']: node for node in observation['nodes']}
    def send(action, payload):
        current = lab.get_session(sid, 'qa')
        frame = lab.frame(sid)
        lab.input(sid, 'qa', {'controllerId': current['controllerId'],
            'epoch': current['epoch'], 'sequence': current['lastSequence'] + 1,
            'commandId': uuid.uuid4().hex, 'frameId': frame['id'],
            'geometryVersion': frame['geometryVersion'], 'action': action, 'payload': payload})
    def tap(node):
        bounds = node['bounds']
        send('tap', {'x': (bounds['left'] + bounds['right']) / (2 * width),
                     'y': (bounds['top'] + bounds['bottom']) / (2 * height)})
    before = nodes()
    assert before['count']['text'] == '0' and not before['name']['hasText']
    tap(before['name'])
    send('text', {'value': 'inspect-contract-value'})
    assert nodes()['name']['hasText']
    d.shell('input', 'keyevent', '4')
    time.sleep(.2)
    tap(nodes()['add'])
    deadline = time.monotonic() + 3
    while nodes()['count'].get('text') != '2' and time.monotonic() < deadline:
        time.sleep(.1)
    assert nodes()['count']['text'] == '2'
    proof.update(passed=True, emptyHintIsNotText=True, textValueOmitted=True,
                 textPresenceUpdated=True, sampleCount='2', nodes=nodes())
finally:
    lab.close_all()
    if rotation == 'free':
        d.shell('wm', 'user-rotation', 'free')
    elif rotation.startswith('lock '):
        d.shell('wm', 'user-rotation', 'lock', rotation.split()[-1])
    proof['deviceReleased'] = lab.list_devices()[0]['state'] == 'available'
    write_json(a.output / 'result.json', proof)
print({'passed': proof['passed'], 'deviceReleased': proof['deviceReleased']})
