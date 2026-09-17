#!/usr/bin/env python3
"""Exercise the local Live API against the installed synthetic iOS counter sample."""
import argparse
import base64
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reproloop.live.client import Client
from reproloop.live.model import check
from reproloop.storage import write_json

parser=argparse.ArgumentParser()
parser.add_argument('--server',default='http://127.0.0.1:8765');parser.add_argument('--device',required=True)
parser.add_argument('--output',type=Path,required=True)
a=parser.parse_args();check(not a.output.exists(),'output_exists','Use a fresh evidence directory')
a.output.mkdir(parents=True,mode=0o700);client=Client(a.server);controller='qa-'+uuid.uuid4().hex
started=time.monotonic();s=client.call('/api/sessions',{'deviceId':a.device,'clientId':controller})['session'];path='/api/sessions/'+s['id']
proof={'kind':'live-api-smoke','deviceId':a.device,'sessionId':s['id'],'captureMethod':'scripted-console-input','browserManual':False,'passed':False}
try:
    deadline=time.monotonic()+95
    while s['state']=='connecting' and time.monotonic()<deadline:
        time.sleep(.4);s=client.call(path)['session']
    check(s['state']=='active','startup_failed','Native provider did not become active')
    proof['firstFrameMs']=round((time.monotonic()-started)*1000)
    control={'controllerId':controller,'epoch':s['epoch']}
    client.call(path+'/recordings/start',dict(control,reset=True))
    actions=[('tap',{'x':.5,'y':.1788}),('text',{'value':'QA'}),('tap',{'x':.5,'y':.2955}),('tap',{'x':.5,'y':.3924})]
    receipts=[]
    for index,(action,payload) in enumerate(actions,1):
        f=client.call(path+'/frame');command=dict(control,sequence=index,commandId=uuid.uuid4().hex,
            frameId=f['id'],geometryVersion=f['geometryVersion'],action=action,payload=payload)
        receipts.append(client.call(path+'/input',command)['receipt']);time.sleep(.25)
    record=client.call(path+'/recordings/stop',control)['recording']
    check(len(record['events'])==4 and record['variables']==['text_1'],'recording_failed','Recorded event coverage differs')
    for name in ('recorded','replayed'):
        if name=='replayed':
            response=client.call(path+'/replay',dict(control,recordingId=record['id'],variables={'text_1':'QA'}))
            s=response['session'];deadline=time.monotonic()+90
            while s['replay']['state'] in {'running','cancelling'} and time.monotonic()<deadline:
                time.sleep(.4);s=client.call(path)['session']
            check(s['replay']['state']=='actions_replayed','replay_failed','Native replay did not finish')
            proof['replay']=s['replay']
        f=client.call(path+'/frame');(a.output/f'{name}.jpg').write_bytes(base64.b64decode(f['imageBase64']))
    proof.update(passed=True,recordingId=record['id'],recordingDigest=record['digest'],receipts=receipts,metrics=s['metrics'])
    write_json(a.output/'recording.json',record)
finally:
    s=client.call(path)['session'];closed=client.call(path+'/close',{'controllerId':s['controllerId'],'epoch':s['epoch']})['session']
    proof['cleanupState']=closed['state'];proof['passed']=proof['passed'] and closed['state']=='closed'
    write_json(a.output/'result.json',proof)
print({'passed':proof['passed'],'events':len(proof.get('receipts',[])),'cleanupState':proof['cleanupState'],'output':str(a.output)})
raise SystemExit(0 if proof['passed'] else 2)
