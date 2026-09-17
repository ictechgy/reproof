import tempfile
import unittest
from reproloop.live.model import Lab,LiveError
from tests.test_live_model import Provider


class RejectionProvider(Provider):
    def execute(self,action,payload):
        if action=='text':
            self.calls.append((action,payload));return {'ok':False,'outcome':'rejected','code':'editable_required'}
        return super().execute(action,payload)


class RejectedInputTests(unittest.TestCase):
    def test_known_rejection_keeps_control_and_is_not_reinjected_on_retry(self):
        with tempfile.TemporaryDirectory() as output:
            provider=RejectionProvider();lab=Lab([{'id':'test','kind':'test','capabilities':{'actions':['tap','text']},'factory':lambda:provider}],output)
            s=lab.create_session('test','owner','c');frame=lab.frame(s['id'])
            command={'controllerId':'c','epoch':1,'sequence':1,'commandId':'text-1','frameId':frame['id'],'geometryVersion':1,'action':'text','payload':{'value':'QA'}}
            try:
                for _ in range(2):
                    with self.assertRaises(LiveError) as error:lab.input(s['id'],'owner',command)
                    self.assertEqual(error.exception.code,'input_rejected')
                self.assertEqual(lab.get_session(s['id'])['state'],'active');self.assertEqual(len(provider.calls),1)
                command.update(sequence=2,commandId='tap-2',action='tap',payload={'x':.5,'y':.5})
                self.assertEqual(lab.input(s['id'],'owner',command)['status'],'injected')
            finally:lab.close_all()

if __name__=='__main__':unittest.main()
