#!/usr/bin/env python3
"""Verify sample-only input, recording and replay through a real iPhone session."""
import argparse
import base64
import http.client
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reproof.live.client import Client
from reproof.live.iphone import select_iphone
from reproof.storage import read_json, write_json

p = argparse.ArgumentParser()
p.add_argument('--server', default='http://127.0.0.1:8765')
p.add_argument('--session-file', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
assert not a.output.exists()
a.output.mkdir(parents=True, mode=0o700)
c = Client(a.server)
sid = read_json(a.session_file)['id']
path = '/api/sessions/' + sid
proof = {'passed': False, 'executionEnvironment': 'physical-iphone', 'receipts': [], 'replays': []}

def session():
    return c.call(path)['session']

def control():
    s = session()
    return {'controllerId': s['controllerId'], 'epoch': s['epoch']}

def send(action, payload):
    s = session()
    f = c.call(path + '/frame')
    result = c.call(path + '/input', {**control(), 'sequence': s['lastSequence'] + 1,
        'commandId': uuid.uuid4().hex, 'frameId': f['id'], 'geometryVersion': f['geometryVersion'],
        'action': action, 'payload': payload})
    assert result['receipt']['status'] == 'injected'
    proof['receipts'].append(result['receipt'])

def save_frame(name):
    initial = c.call(path + '/frame')['id']
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        f = c.call(path + '/frame')
        if f['id'] > initial:
            (a.output / name).write_bytes(base64.b64decode(f['imageBase64']))
            return
        time.sleep(.2)
    raise AssertionError('No fresh frame')

try:
    assert session()['state'] == 'active'
    phone = select_iphone()
    connection = http.client.HTTPConnection(phone.tunnel_address, 8766, timeout=5)
    try:
        connection.request('GET', '/status')
        response = connection.getresponse()
        assert response.status == 401
        response.read()
        proof['unauthorizedRejected'] = True
    finally:
        connection.close()
    save_frame('initial.jpg')
    proof['idleFramesAdvance'] = True
    c.call(path + '/recordings/start', {**control(), 'reset': True})
    for action, payload in [('tap', {'x': .5, 'y': .1788}), ('text', {'value': 'QA'}),
            ('tap', {'x': .5, 'y': .2955}), ('tap', {'x': .5, 'y': .3924})]:
        send(action, payload)
    record = c.call(path + '/recordings/stop', control())['recording']
    assert record['replayable'] and len(record['events']) == 4
    assert all('value' not in event['payload'] for event in record['events'])
    proof['recordingId'] = record['id']
    proof['textStoredAsVariable'] = True
    save_frame('recorded.jpg')
    for index in range(3):
        c.call(path + '/replay', {**control(), 'recordingId': record['id'],
            'variables': {name: 'QA' for name in record['variables']}})
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            s = session()
            if s['replay']['state'] not in {'running', 'cancelling'}:
                break
            time.sleep(.2)
        assert s['replay']['state'] == 'actions_replayed'
        proof['replays'].append(s['replay'])
        save_frame('replayed-' + str(index + 1) + '.jpg')
    send('swipe', {'fromX': .5, 'fromY': .85, 'toX': .5, 'toY': .62, 'durationMs': 500})
    save_frame('swiped.jpg')
    send('tap', {'x': .5, 'y': .451})
    save_frame('details.jpg')
    proof['metrics'] = session()['metrics']
    proof['passed'] = True
finally:
    closed = c.call(path + '/close', control())['session']
    proof['cleanup'] = closed['state']
    proof['passed'] = proof['passed'] and closed['state'] == 'closed'
    write_json(a.output / 'result.json', proof)
print({'passed': proof['passed'], 'replays': len(proof['replays']), 'cleanup': proof['cleanup']})
raise SystemExit(0 if proof['passed'] else 2)
