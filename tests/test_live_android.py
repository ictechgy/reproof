import struct
import tempfile
from unittest import TestCase
from unittest.mock import patch
from reproloop.live.android import AndroidProvider,ANDROID_ACTIONS
from reproloop.live.model import Lab,LiveError


class Device:
    def __init__(self,*args):self.calls=[]
    def lease(self):return self
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def adb_call(self,*args,**kwargs):return b'\x89PNG\r\n\x1a\n'+b'\0'*8+struct.pack('>II',400,800)
    def shell(self,*args,**kwargs):self.calls.append(args)


class AndroidBatchTests(TestCase):
    def test_batch_coordinates_and_unsupported_text(self):
        with tempfile.TemporaryDirectory() as output,patch('reproloop.live.android.AdbDevice',Device):
            provider=AndroidProvider('synthetic')
            lab=Lab([{'id':'android','kind':'android-adb','capabilities':{
                'actions':ANDROID_ACTIONS,'authorityMode':'legacy-offline-v1'},
                'factory':lambda:provider}],output)
            s=lab.create_session('android','owner','c')
            try:
                frame=lab.frame(s['id']);command={'controllerId':'c','epoch':1,'sequence':1,'commandId':'tap','frameId':frame['id'],
                   'geometryVersion':frame['geometryVersion'],'action':'tap','payload':{'x':1,'y':0}}
                lab.input(s['id'],'owner',command);self.assertEqual(provider.device.calls,[('input','tap','399','0')])
                command.update(sequence=2,commandId='text',action='text',payload={'value':'QA'})
                with self.assertRaises(LiveError):lab.input(s['id'],'owner',command)
                self.assertEqual(len(provider.device.calls),1)
            finally:lab.close_all()
