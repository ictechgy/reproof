#!/usr/bin/env python3
"""Run real Live input/replay checks against a sample-only Android provider."""
import argparse
import base64
from pathlib import Path
import statistics
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reproof.live.client import Client
from reproof.storage import read_json,write_json

p=argparse.ArgumentParser();p.add_argument('--server',default='http://127.0.0.1:8765');p.add_argument('--device',required=True)
p.add_argument('--recording',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();assert not a.output.exists();a.output.mkdir(parents=True,mode=0o700)
c=Client(a.server);s=c.call('/api/sessions',{'deviceId':a.device,'clientId':'android-qa-'+uuid.uuid4().hex})['session'];sid=s['id'];path='/api/sessions/'+sid
proof={'provider':a.device,'passed':False,'replays':[],'receipts':[]}
try:
 deadline=time.monotonic()+40
 while s['state']=='connecting' and time.monotonic()<deadline:
  time.sleep(.1);s=c.call(path)['session']
 assert s['state']=='active',s['state']
 record=read_json(a.recording)
 if 'recording' in record:record=record['recording']
 for index in range(3):
  s=c.call(path)['session'];c.call(path+'/replay',{'controllerId':s['controllerId'],'epoch':s['epoch'],'recordingId':record['id'],'variables':{}})
  deadline=time.monotonic()+30
  while time.monotonic()<deadline:
   s=c.call(path)['session']
   if s['replay']['state'] not in {'running','cancelling'}:break
   time.sleep(.05)
  assert s['replay']['state']=='actions_replayed',s['replay'];proof['replays'].append(s['replay'])
 f=c.call(path+'/frame');(a.output/'replayed.jpg').write_bytes(base64.b64decode(f['imageBase64']))
 def send(phase,pointer=0,x=.7,y=.65):
  current=c.call(path)['session'];frame=c.call(path+'/frame')
  response=c.call(path+'/input',{'controllerId':current['controllerId'],'epoch':current['epoch'],'sequence':current['lastSequence']+1,
    'commandId':uuid.uuid4().hex,'frameId':frame['id'],'geometryVersion':frame['geometryVersion'],'action':'pointer',
    'payload':{'phase':phase,'pointerId':pointer,'x':x,'y':y}})
  proof['receipts'].append(response['receipt']);return response['session']
 s=c.call(path)['session'];c.call(path+'/recordings/start',{'controllerId':s['controllerId'],'epoch':s['epoch'],'reset':True})
 send('down')
 for i in range(1,9):send('move',y=.65-i*.02);time.sleep(.025)
 send('up',y=.49)
 s=c.call(path)['session'];drag=c.call(path+'/recordings/stop',{'controllerId':s['controllerId'],'epoch':s['epoch']})['recording']
 assert drag['replayable'] and len(drag['events'])==10
 proof['dragRecordingId']=drag['id']
 f=c.call(path+'/frame');(a.output/'dragged.jpg').write_bytes(base64.b64decode(f['imageBase64']))
 send('down',0,.35,.60);active=send('down',1,.65,.60)
 assert active['activePointerIds']==[0,1]
 send('move',0,.30,.55);send('move',1,.70,.55);send('up',1,.70,.55);active=send('up',0,.30,.55)
 assert active['activePointerIds']==[];proof['multitouch']=True
 s=send('down',0,.75,.65);old_epoch=s['epoch'];deadline=time.monotonic()+14
 while time.monotonic()<deadline:
  s=c.call(path)['session']
  if s['epoch']>old_epoch:break
  time.sleep(.25)
 assert s['state']=='active' and not s['activePointerIds'] and s['epoch']>old_epoch
 proof['pointerTimeoutRecovery']=True;proof['metrics']=s['metrics']
 durations=[r['durationMs'] for r in proof['receipts']]
 proof['acknowledgementMs']={'median':statistics.median(durations),'maximum':max(durations),'samples':len(durations)}
 proof['passed']=True
finally:
 s=c.call(path)['session'];closed=c.call(path+'/close',{'controllerId':s['controllerId'],'epoch':s['epoch']})['session']
 proof['cleanup']=closed['state'];proof['passed']=proof['passed'] and closed['state']=='closed';write_json(a.output/'result.json',proof)
print({'passed':proof['passed'],'replays':len(proof['replays']),'multitouch':proof.get('multitouch'),'timeoutRecovery':proof.get('pointerTimeoutRecovery'),'metrics':proof.get('metrics'),'ack':proof.get('acknowledgementMs'),'cleanup':proof['cleanup']})
raise SystemExit(0 if proof['passed'] else 2)
