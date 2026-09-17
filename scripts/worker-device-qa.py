#!/usr/bin/env python3
"""A separate worker process controls the real sample through a parent Lab."""
import argparse
import base64
from pathlib import Path
import secrets
import selectors
import signal
import subprocess
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reproloop.live.worker import WorkerClient,remote_devices
from reproloop.live.model import Lab
from reproloop.device import AdbDevice
from reproloop.storage import read_json,write_json

p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
assert not a.output.exists();a.output.mkdir(parents=True,mode=0o700)
device=AdbDevice();rotation=device.shell('wm','user-rotation').strip();device.shell('wm','user-rotation','lock','0')
token=secrets.token_urlsafe(32)
process=subprocess.Popen([sys.executable,'-m','reproloop','live-worker','--port','0','--output',str(a.output/'worker'),
 '--token-stdin','--android','auto','--android-helper','android/live/build/outputs/apk/debug/live-debug.apk',
 '--android-app','android/sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
process.stdin.write(__import__('json').dumps({'token':token}).encode());process.stdin.close();process.stdin=None
parent=None;proof={'passed':False,'workerProcessSeparate':True}
try:
 selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
 assert selector.select(timeout=15),'worker did not start'
 started=__import__('json').loads(process.stdout.readline());selector.close()
 client=WorkerClient(started['worker'],token);parent=Lab(remote_devices(client,'usb-worker'),a.output/'coordinator')
 device=parent.list_devices()[0];s=parent.create_session(device['id'],'qa','remote-qa');sid=s['id'];deadline=time.monotonic()+45
 while s['state']=='connecting' and time.monotonic()<deadline:
  time.sleep(.1);s=parent.get_session(sid,'qa')
 assert s['state']=='active',s['error']
 parent.start_recording(sid,'qa',s['controllerId'],s['epoch'],True)
 target=read_json('artifacts/android-live-targets.json');b=target['nodes']['add'];x=(b['left']+b['right'])/2/target['width'];y=(b['top']+b['bottom'])/2/target['height']
 for sequence,phase in enumerate(('down','up'),1):
  frame=parent.frame(sid,'qa');parent.input(sid,'qa',{'controllerId':s['controllerId'],'epoch':s['epoch'],'sequence':sequence,
   'commandId':uuid.uuid4().hex,'frameId':frame['id'],'geometryVersion':frame['geometryVersion'],'action':'pointer',
   'payload':{'phase':phase,'pointerId':0,'x':x,'y':y}});time.sleep(.07)
 recording=parent.stop_recording(sid,'qa',s['controllerId'],s['epoch']);assert recording['replayable']
 parent.start_replay(sid,'qa',s['controllerId'],s['epoch'],recording['id'])
 deadline=time.monotonic()+30
 while time.monotonic()<deadline:
  s=parent.get_session(sid,'qa')
  if s['replay']['state'] not in {'running','cancelling'}:break
  time.sleep(.05)
 assert s['replay']['state']=='actions_replayed',s['replay']
 frame=parent.frame(sid,'qa');after_ack_frame=frame['id'];deadline=time.monotonic()+3
 while frame['id']<after_ack_frame+3 and time.monotonic()<deadline:
  time.sleep(.08);frame=parent.frame(sid,'qa')
 assert frame['id']>=after_ack_frame+3
 (a.output/'replayed.jpg').write_bytes(base64.b64decode(frame['imageBase64']))
 closed=parent.close_session(sid,'qa');assert closed['state']=='closed'
 worker_devices=client.call('/v1/devices')['devices'];assert all(d['state']=='available' for d in worker_devices)
 device=AdbDevice()
 with device.lease():
  count=device.driver('observe',target='count')['nodes'][0]['text']
 assert count=='2',count
 proof.update(passed=True,recordingId=recording['id'],replay=s['replay'],metrics=s['metrics'],sampleCount=count,workerDeviceReleased=True)
finally:
 if parent is not None:parent.close_all()
 process.send_signal(signal.SIGINT)
 try:process.wait(timeout=20)
 except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
 if rotation=='free':device.shell('wm','user-rotation','free')
 elif rotation.startswith('lock '):device.shell('wm','user-rotation','lock',rotation.split()[-1])
 token='';proof['workerExitCode']=process.returncode;write_json(a.output/'result.json',proof)
print({'passed':proof['passed'],'separateWorker':True,'sampleCount':proof.get('sampleCount'),'workerDeviceReleased':proof.get('workerDeviceReleased'),'workerExitCode':process.returncode})
raise SystemExit(0 if proof['passed'] and process.returncode==0 else 2)
