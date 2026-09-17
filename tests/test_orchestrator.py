from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from reproloop.core import digest
from reproloop.agents import AgentUnavailable
from reproloop.storage import create_bundle,sha_file
from reproloop.repair import snapshot_source
from reproloop.orchestrator import repair_job,PRODUCT_FILE,APK_RELATIVE
from tests.test_core import capture,oracle
from tests.test_replay import FakeDevice


class Agent:
    last_receipt={'provider':'claude','status':'completed','promptDigest':'synthetic-prompt-digest'}
    def propose(self,*args):return [{'path':PRODUCT_FILE,'old':'increment() = 2','new':'increment() = 1'}]


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name);self.source=self.root/'android'
        f=self.source/PRODUCT_FILE;f.parent.mkdir(parents=True);f.write_text('fun increment() = 2\n')
        (self.source/'build.gradle.kts').write_text('// protected build fixture\n')
        self.apk=self.root/'original.apk';self.apk.write_bytes(b'original')
        self.proof={'sourceDigest':digest(snapshot_source(self.source)),'apkSha256':sha_file(self.apk),'buildCompleted':True}
        self.bundle=create_bundle(capture(),oracle(),self.apk,self.root/'bundle',self.proof)
    def tearDown(self):self.t.cleanup()
    def build(self,workspace,**kwargs):
        f=workspace/APK_RELATIVE;f.parent.mkdir(parents=True);f.write_bytes(b'patched')
        return f,{'sourceDigest':digest(snapshot_source(workspace)),'apkSha256':sha_file(f),'buildCompleted':True}
    def _tests_pass(self,command,cwd,**kwargs):
        p=Path(cwd)/'sample/build/test-results/testBuggyDebugUnitTest';p.mkdir(parents=True)
        (p/'TEST-Counter.xml').write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>')
    def invoke(self,device):
        return repair_job(device,self.bundle,self.source,self.root/'repair',Agent(),
                          {'gradle':'fake-gradle','java_home':'fake-jdk','sdk_home':'fake-sdk'})
    def test_full_boundary_retains_original_and_verifies_patch(self):
        with patch('reproloop.orchestrator.build_android',self.build),patch('reproloop.orchestrator.run_command',self._tests_pass):
            r=self.invoke(FakeDevice(['2']*3+['1']*3))
        self.assertEqual(r['status'],'verified');self.assertEqual(len(r['runs']),6)
        self.assertIn('increment() = 2',(self.source/PRODUCT_FILE).read_text())
        self.assertTrue((self.root/'repair/attempt-1/patch.diff').is_file())
        self.assertEqual(r['attempts'][0]['agentReceipt'],Agent.last_receipt)
    def test_no_source_regression_task_cannot_verify(self):
        with patch('reproloop.orchestrator.build_android',self.build),patch('reproloop.orchestrator.run_command',return_value='NO-SOURCE'):
            r=self.invoke(FakeDevice(['2']*3))
        self.assertEqual(r['status'],'verification_failed')
    def test_provider_failure_is_terminal_without_repeated_calls(self):
        class UnavailableAgent:
            calls=0
            def propose(self,*args):
                self.calls+=1
                raise AgentUnavailable('provider unavailable')
        agent=UnavailableAgent()
        result=repair_job(FakeDevice(['2']*3),self.bundle,self.source,self.root/'repair',agent,
                          {'gradle':'fake','java_home':'fake','sdk_home':'fake'})
        self.assertEqual(result['status'],'agent_unavailable')
        self.assertEqual(agent.calls,1)
        self.assertEqual(len(result['attempts']),1)

    def test_baseline_mixed_stops_before_agent_or_build(self):
        with patch('reproloop.orchestrator.build_android') as build:
            r=self.invoke(FakeDevice(['2','1','2']));build.assert_not_called()
        self.assertEqual(r['status'],'inconclusive');self.assertEqual(r['attempts'],[])

    def test_preexisting_regression_report_cannot_verify_a_patch(self):
        def build(workspace,**kwargs):
            result=self.build(workspace,**kwargs)
            self._tests_pass([],workspace)
            return result
        with patch('reproloop.orchestrator.build_android',build),patch('reproloop.orchestrator.run_command',return_value='UP-TO-DATE'):
            result=self.invoke(FakeDevice(['2']*3+['1']*3))
        self.assertEqual(result['status'],'verification_failed')

    def test_linked_regression_directory_cannot_verify_a_patch(self):
        def tests(command,cwd,**kwargs):
            external=self.root/'external-reports'
            self._tests_pass([],external)
            (Path(cwd)/'sample/build/test-results').symlink_to(external/'sample/build/test-results',target_is_directory=True)
        with patch('reproloop.orchestrator.build_android',self.build),patch('reproloop.orchestrator.run_command',tests):
            result=self.invoke(FakeDevice(['2']*3+['1']*3))
        self.assertEqual(result['status'],'verification_failed')

if __name__=='__main__':unittest.main()
