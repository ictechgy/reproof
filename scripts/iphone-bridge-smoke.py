#!/usr/bin/env python3
"""Validate the physical-device HTTP bridge on an isolated Simulator (not a phone claim)."""
import base64
import http.client
import json
from pathlib import Path
import plistlib
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reproof.ios_runner import prepare_xctestrun,_targets
from reproof.storage import write_json

simulator=sys.argv[1];output=Path(sys.argv[2]);assert not output.exists();output.mkdir(parents=True,mode=0o700)
app=Path('artifacts/ios-cases-build/DerivedData/Build/Products/Debug-iphonesimulator/ReproSample.app').resolve()
subprocess.run(['/usr/bin/xcrun','simctl','install',simulator,str(app)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
port_socket=socket.socket(socket.AF_INET6);port_socket.bind(('::1',0));port=port_socket.getsockname()[1];port_socket.close()
token=secrets.token_urlsafe(32);process=None;proof={'executionEnvironment':'simulator','physicalDeviceValidated':False,'passed':False}
def call(path,body=None,auth=True):
 connection=http.client.HTTPConnection('::1',port,timeout=5)
 try:
  headers={'Authorization':'Bearer '+token} if auth else {}
  if body is not None:headers['Content-Type']='application/json'
  connection.request('POST' if body is not None else 'GET',path,None if body is None else json.dumps(body).encode(),headers)
  response=connection.getresponse();value=json.loads(response.read(5*1024*1024));return response.status,value
 finally:connection.close()
with tempfile.TemporaryDirectory(prefix='repro-bridge-smoke-') as directory:
 config=prepare_xctestrun('live-ios/build/Build/Products','ReproLiveTests',Path(directory)/'bridge.xctestrun')
 with config.open('rb') as stream:document=plistlib.load(stream)
 for _,target in _targets(document):target.setdefault('EnvironmentVariables',{}).update(REPRO_LIVE_LISTEN_HOST='::1',REPRO_LIVE_LISTEN_PORT=str(port),REPRO_LIVE_TOKEN=token,REPRO_TARGET_BUNDLE='io.reproof.sample.ios')
 with config.open('wb') as stream:plistlib.dump(document,stream)
 process=subprocess.Popen(['/usr/bin/xcodebuild','test-without-building','-xctestrun',str(config),'-destination','id='+simulator,
  '-resultBundlePath',str(Path(directory)/'result.xcresult'),'-parallel-testing-enabled','NO','-only-testing:ReproLiveTests/LiveControlTests/testControlSession'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 try:
  deadline=time.monotonic()+95
  while time.monotonic()<deadline:
   try:
    if call('/status')[1].get('ready'):break
   except Exception:pass
   time.sleep(.2)
  assert call('/status')[1]['ready'];assert call('/status',auth=False)[0]==401
  before=call('/frame')[1]['nativeFrameId'];time.sleep(1.5);assert call('/frame')[1]['nativeFrameId']>before
  receipts=[]
  for action,payload in [('tap',{'x':.5,'y':.1788}),('text',{'value':'QA'}),('tap',{'x':.5,'y':.2955}),('tap',{'x':.5,'y':.3924})]:
   command_id=uuid.uuid4().hex;assert call('/command',{'id':command_id,'action':action,'payload':payload})[0]==202
   deadline=time.monotonic()+25
   while time.monotonic()<deadline:
    result=call('/ack/'+command_id)[1]
    if not result.get('pending',False):break
    time.sleep(.05)
   assert result['id']==command_id and result['ok'];receipts.append({'action':action,**result})
  image=call('/frame')[1];(output/'sample.jpg').write_bytes(base64.b64decode(image['imageBase64']))
  proof.update(passed=True,unauthorizedRejected=True,idleFramesAdvance=True,receipts=receipts)
 finally:
  try:call('/stop',{})
  except Exception:pass
  try:process.wait(timeout=20)
  except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
  proof['runnerExitCode']=process.returncode;proof['passed']=proof['passed'] and process.returncode==0
write_json(output/'result.json',proof);print({'passed':proof['passed'],'environment':'simulator','commands':len(proof.get('receipts',[])),'runnerExitCode':proof['runnerExitCode']})
raise SystemExit(0 if proof['passed'] else 2)
