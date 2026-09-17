"""Real authenticated coordinator requests, package uploads and Range reads."""
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from reproloop.live.issue_http import single_range
from reproloop.live.server import LiveServer
from reproloop.issue_package import NativeMediaValidator
from tests.g4_support import ScenarioProvider, specification
from tests.test_issue_media import png
from tests import test_issue_workflow as workflow_support


ROOT=Path(__file__).resolve().parents[1]


class RangeContractTests(unittest.TestCase):
    def test_complete_prefix_suffix_and_open_ranges(self):
        self.assertEqual(single_range(None,10),(0,9,200))
        self.assertEqual(single_range('bytes=0-9',10),(0,9,206))
        self.assertEqual(single_range('bytes=2-',10),(2,9,206))
        self.assertEqual(single_range('bytes=-4',10),(6,9,206))
        self.assertEqual(single_range('bytes=-100',10),(0,9,206))
        self.assertEqual(single_range('bytes=8-100',10),(8,9,206))

    def test_malformed_and_out_of_bounds_ranges_are_416(self):
        from reproloop.live.model import LiveError
        for value in ('bytes=10-','bytes=2-1','bytes=-0','bytes=-','bytes=0-1,3-4',
                      'Bytes=0-1','bytes= 0-1','bytes=1e3-','bytes='+('9'*100)+'-'):
            with self.subTest(value=value),self.assertRaises(LiveError) as caught:
                single_range(value,10)
            self.assertEqual(caught.exception.status,416)


class IssueHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools=tempfile.TemporaryDirectory()
        cls.helper=Path(cls.tools.name)/'media-validator'
        result=subprocess.run(['swiftc',str(ROOT/'native/macos-media-validator/main.swift'),
                               '-o',str(cls.helper)],capture_output=True,timeout=30)
        if result.returncode:
            cls.tools.cleanup()
            raise RuntimeError('Local media helper compilation failed')

    @classmethod
    def tearDownClass(cls):cls.tools.cleanup()

    def setUp(self):
        self.fixture=workflow_support.IssueWorkflowTests('runTest')
        self.fixture.setUp()
        self.workflow=self.fixture.workflow
        self.store=self.fixture.access_store
        self.tokens={identity:self.store.issue_principal_credential('admin',identity,lifetime_seconds=600)['token']
                     for identity in ('owner','viewer','maintainer')}
        self.store.create_identity('admin','outsider')
        self.tokens['outsider']=self.store.issue_principal_credential('admin','outsider',lifetime_seconds=600)['token']
        control=self.fixture.env.control
        class Provider(ScenarioProvider):
            def render(self):
                self.lab.publish_frame(self.session['id'],png(8,12),'image/png',8,12,'portrait')
        def factory():
            provider=Provider(control);control['provider']=provider
            return provider
        self.fixture.env.lab.devices['device']['factory']=factory
        self.workflow.packages.media_validator=NativeMediaValidator(
            self.workflow.packages.archive_evidence,self.helper,self.workflow.packages.storage_owner)
        self.server=LiveServer(self.fixture.env.lab,access=self.fixture.access,issue_workflow=self.workflow)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.thread.join(2);self.server.server_close()
        self.fixture.tearDown()

    def call(self,path,body=None,*,role='owner',method=None,headers=None):
        data=json.dumps(body).encode() if isinstance(body,dict) else body
        request_headers={'Authorization':'Bearer '+self.tokens[role],
                         'Content-Type':'application/json','Origin':self.server.origin}
        request_headers.update(headers or {})
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        try:
            connection.request(method or ('POST' if body is not None else 'GET'),path,body=data,headers=request_headers)
            response=connection.getresponse();payload=response.read(4*1024*1024)
            mime=response.getheader('Content-Type','')
            value=json.loads(payload) if payload and 'json' in mime else payload
            return response.status,dict(response.getheaders()),value
        finally:connection.close()

    def record(self):
        status,_,result=self.call('/api/release/issues',{'projectId':'checkout','applicationId':'ios_app',
            'buildId':'original','deviceId':'device','clientId':'browser','preparationIds':['seed_account'],
            'unprepared':False})
        self.assertEqual(status,202)
        issue_id=result['issue']['id']
        view=self.fixture.wait(issue_id,states={'recording'})
        session=self.fixture.env.lab.peek_session(view['issue']['sessionId'],'owner')
        for sequence,action in enumerate((
            {'action':'tap','parameters':{},'target':{'kind':'accessibility-id','value':'checkout'}},
            {'action':'text','parameters':{'variableId':'secret_text'},
             'target':{'kind':'accessibility-id','value':'account'}},
        ),1):
            status,_,_=self.call('/api/release/issues/'+issue_id+'/input',{
                'input':action,'operationId':f'http_manual_{sequence}','sequence':sequence,
                'controllerId':session['controllerId'],'epoch':session['epoch']})
            self.assertEqual(status,200)
        status,_,_=self.call('/api/release/issues/'+issue_id+'/stop',{})
        self.assertEqual(status,202)
        view=self.fixture.wait(issue_id,states={'complete','failed','quarantined'})
        self.assertEqual(view['issue']['state'],'complete')
        spec=specification(view['recording'])
        status,_,saved=self.call('/api/release/issues/'+issue_id+'/specifications',{'baseRevision':0,
            **{key:spec[key] for key in ('actions','waits','bindings','fixtures','assertions')}})
        self.assertEqual(status,200)
        return issue_id,view,saved

    def test_package_round_trip_explicit_local_approval_and_replay_through_http(self):
        issue_id,view,saved=self.record()
        status,_,exported=self.call('/api/release/issues/'+issue_id+'/export',{})
        self.assertEqual(status,200,exported)
        package=exported['package']
        path='/api/release/projects/checkout/packages/'+package['id']+'/export'
        status,headers,body=self.call(path)
        self.assertEqual(status,200)
        self.assertEqual(hashlib.sha256(body).hexdigest(),package['archiveDigest'])
        status,_,imported=self.call('/api/release/projects/checkout/import',body,
            headers={'Content-Type':'application/zip','X-Repro-Content-SHA256':package['archiveDigest']})
        self.assertEqual(status,201,imported)
        imported_id=imported['issue']['id']
        status,_,received=self.call('/api/release/issues/'+imported_id)
        self.assertEqual(received['recording']['original'],view['recording']['original'])
        self.assertEqual(received['specification'],saved['specification'])
        self.assertIsNone(received['approval'])
        approval={'revision':1,'specificationDigest':saved['specificationDigest'],'bindImported':False}
        self.assertEqual(self.call('/api/release/issues/'+imported_id+'/approve',approval)[0],409)
        approval['bindImported']=True
        self.assertEqual(self.call('/api/release/issues/'+imported_id+'/approve',approval)[0],200)
        request={'deviceId':'device','clientId':'replay_browser','specificationDigest':saved['specificationDigest']}
        self.assertEqual(self.call('/api/release/issues/'+imported_id+'/replay',request)[0],202)
        final=self.fixture.wait(imported_id,states={'reproduced','failed','quarantined','unknown'})
        self.assertEqual(final['issue']['state'],'reproduced',final['campaign'])
        self.assertEqual(len(final['campaign']['attempts']),3)

    def test_media_range_framing_access_and_revocation(self):
        issue_id,view,_=self.record()
        media=view['recording']['original']['media'][0]
        path='/api/release/issues/'+issue_id+'/media/'+media['digest']
        status,headers,full=self.call(path,role='viewer')
        self.assertEqual(status,200)
        self.assertEqual(len(full),media['bytes'])
        self.assertEqual(headers['ETag'],'"sha256-'+media['digest']+'"')
        for value,start,end in (('bytes=0-9',0,9),('bytes=-4',len(full)-4,len(full)-1),
                                ('bytes=2-',2,len(full)-1)):
            status,headers,part=self.call(path,role='viewer',headers={'Range':value})
            self.assertEqual(status,206)
            self.assertEqual(part,full[start:end+1])
            self.assertEqual(headers['Content-Range'],f'bytes {start}-{end}/{len(full)}')
            self.assertEqual(int(headers['Content-Length']),len(part))
        status,headers,body=self.call(path,headers={'Range':'bytes=999999-'})
        self.assertEqual(status,416)
        self.assertEqual(headers['Content-Range'],f'bytes */{len(full)}')
        self.assertEqual(body,b'')
        self.assertEqual(self.call(path,role='outsider')[0],404)
        self.store.revoke_membership('admin','checkout','viewer','viewer')
        self.assertEqual(self.call(path,role='viewer')[0],404)

    def test_binary_import_requires_membership_and_validated_content(self):
        bad=b'invalid archive'
        headers={'Content-Type':'application/zip','X-Repro-Content-SHA256':hashlib.sha256(bad).hexdigest()}
        self.assertEqual(self.call('/api/release/projects/checkout/import',bad,role='viewer',headers=headers)[0],403)
        self.assertEqual(self.call('/api/release/projects/checkout/import',bad,headers=headers)[0],400)
        self.assertEqual(self.call('/api/release/issues')[2]['issues'],[])

    def test_duplicate_digest_header_and_interrupted_upload_remain_unpublished(self):
        body=b'incomplete'
        path='/api/release/projects/checkout/import'
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.putrequest('POST',path)
            connection.putheader('Authorization','Bearer '+self.tokens['owner'])
            connection.putheader('Content-Type','application/zip')
            connection.putheader('Content-Length',str(len(body)))
            for _ in range(2):connection.putheader('X-Repro-Content-SHA256',hashlib.sha256(body).hexdigest())
            connection.endheaders(body)
            response=connection.getresponse()
            self.assertEqual(response.status,400)
            response.read()
        finally:connection.close()
        import socket
        before=self.fixture.env.lab._recording_budget.snapshot()['chargedBytes']
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.putrequest('POST',path)
            connection.putheader('Authorization','Bearer '+self.tokens['owner'])
            connection.putheader('Content-Type','application/zip')
            connection.putheader('Content-Length','1024')
            connection.putheader('X-Repro-Content-SHA256','a'*64)
            connection.endheaders(body)
            connection.sock.shutdown(socket.SHUT_WR)
            response=connection.getresponse()
            self.assertEqual(response.status,400)
            response.read()
        finally:connection.close()
        self.assertEqual(self.call('/api/release/issues')[2]['issues'],[])
        self.assertEqual(self.fixture.env.lab._recording_budget.snapshot()['chargedBytes'],before)


if __name__=='__main__':unittest.main()
