#!/usr/bin/env python3
"""Verify rotation cancellation and loss of the owned ADB forward on the sample."""
import argparse
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reproof.device import AdbDevice
from reproof.live.android_live import android_live_device
from reproof.live.model import Lab
from reproof.storage import write_json

p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();assert not a.output.exists()
d=AdbDevice();rotation=d.shell('wm','user-rotation').strip();lab=Lab([android_live_device(d.serial,'android/live/build/outputs/apk/debug/live-debug.apk','android/sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk')],a.output)
proof={'passed':False}
try:
 d.shell('wm','user-rotation','lock','0');s=lab.create_session('android-'+d.identity,'qa','recovery-qa');sid=s['id'];deadline=time.monotonic()+40
 while s['state']=='connecting' and time.monotonic()<deadline:time.sleep(.1);s=lab.get_session(sid,'qa')
 assert s['state']=='active',s['error'];r=lab.start_recording(sid,'qa',s['controllerId'],s['epoch'],True);frame=lab.frame(sid)
 def command(sequence,phase,original):
  return {'controllerId':s['controllerId'],'epoch':s['epoch'],'sequence':sequence,'commandId':uuid.uuid4().hex,
   'frameId':original['id'],'geometryVersion':original['geometryVersion'],'action':'pointer','payload':{'phase':phase,'pointerId':0,'x':.7,'y':.6}}
 lab.input(sid,'qa',command(1,'down',frame));d.shell('wm','user-rotation','lock','1');deadline=time.monotonic()+6
 while time.monotonic()<deadline and lab.frame(sid)['geometryVersion']==frame['geometryVersion']:time.sleep(.1)
 assert lab.frame(sid)['geometryVersion']>frame['geometryVersion']
 lab.input(sid,'qa',command(2,'cancel',frame));current=lab.recording(r['id'],'qa')
 assert current['status']=='invalid' and not current['replayable'];assert not lab.get_session(sid)['activePointerIds']
 proof['rotationCancel']=True;proof['recordingInvalidated']=True
 d.shell('wm','user-rotation','lock','0')
 provider=lab._session(sid)['provider'];d.adb_call('forward','--remove','tcp:'+str(provider.port));deadline=time.monotonic()+5
 while time.monotonic()<deadline and lab.get_session(sid)['state']!='failed':time.sleep(.1)
 assert lab.get_session(sid)['state']=='failed'
 closed=lab.close_session(sid,'qa');assert closed['state']=='closed',closed['error']
 assert getattr(provider,'cleanup_reconnected',False)
 proof.update(passed=True,forwardLossDetected=True,cleanupReconnected=True,deviceReleased=lab.list_devices()[0]['state']=='available')
finally:
 lab.close_all()
 if rotation=='free':d.shell('wm','user-rotation','free')
 elif rotation.startswith('lock '):d.shell('wm','user-rotation','lock',rotation.split()[-1])
 write_json(a.output/'result.json',proof)
print(proof)
raise SystemExit(0 if proof['passed'] else 2)
