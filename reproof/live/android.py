"""ADB batch-input baseline; continuous touch and Unicode IME are not advertised."""
from __future__ import annotations
import struct
import threading
from .model import check
from ..device import AdbDevice

ANDROID_ACTIONS=['tap','long_press','swipe','home']


class AndroidProvider:
    def __init__(self,serial):
        self.device=AdbDevice(serial);self.stop=threading.Event();self.lock=threading.Lock();self.lease=None;self.thread=None;self.capture_sequence=0
    def start(self,session,lab):
        self.sid=session['id'];self.lab=lab;self.lease=self.device.lease();self.lease.__enter__()
        self.capture();self.thread=threading.Thread(target=self._frames,daemon=True);self.thread.start()
    def capture(self):
        with self.lock:
            data=self.device.adb_call('exec-out','screencap','-p',timeout=8)
            check(data[:8]==b'\x89PNG\r\n\x1a\n' and len(data)>=24,'capture_failed','ADB did not return a PNG')
            width,height=struct.unpack('>II',data[16:24]);self.size=(width,height)
            self.capture_sequence+=1
            self.lab.publish_frame(self.sid,data,'image/png',width,height,'landscape' if width>=height else 'portrait',
                                   acquisition_sequence=self.capture_sequence,
                                   timing_source='native-unmapped')
    def _frames(self):
        while not self.stop.wait(.5):
            try:self.capture()
            except Exception:
                self.lab.fail(self.sid,'Android capture stopped; reconnect the device and close the session');return
    def execute(self,action,payload):
        with self.lock:
            check(not self.stop.is_set(),'session_inactive','Android provider is closing')
            width,height=self.size
            def point(x,y):return str(round(x*(width-1))),str(round(y*(height-1)))
            if action=='home':args=['keyevent','KEYCODE_HOME']
            elif action=='tap':args=['tap',*point(payload['x'],payload['y'])]
            elif action=='long_press':
                position=point(payload['x'],payload['y']);args=['swipe',*position,*position,str(payload['durationMs'])]
            elif action=='swipe':args=['swipe',*point(payload['fromX'],payload['fromY']),*point(payload['toX'],payload['toY']),str(payload['durationMs'])]
            else:check(False,'unsupported_operation','Android baseline does not support this operation')
            self.device.shell('input',*args,timeout=8)
        self.capture();return {'ok':True,'timing':'best-effort'}
    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=10);check(not self.thread.is_alive(),'cleanup_failed','Android capture worker is still active')
        if self.lease:self.lease.__exit__(None,None,None);self.lease=None


def android_device(serial,*,authority_mode='shared-v2'):
    check(authority_mode in {'shared-v2','legacy-offline-v1'},'invalid_argument','Invalid authority compatibility mode',400)
    device=AdbDevice(serial)
    descriptor={'id':'android-'+device.identity,'name':'Android · ADB baseline','platform':'android','kind':'android-adb',
            'capabilities':{'actions':ANDROID_ACTIONS,'inputMode':'gesture-batch','media':'sampled-png','multitouch':False,
                            'resetContract':'none','timing':'best-effort','verification':'device-validation-pending',
                            'authorityMode':authority_mode},
            'factory':lambda:AndroidProvider(serial)}
    # The v1 ADB driver has no native-local grant checker. Shared mode therefore
    # reaches Lab's provider-version rejection before start() can mutate.
    if authority_mode=='shared-v2':descriptor['_authority']={'deviceKind':'android','physicalId':serial}
    return descriptor
