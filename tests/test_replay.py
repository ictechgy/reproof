from pathlib import Path
import tempfile
import unittest
from reproloop.storage import create_bundle
from reproloop.replay import replay_suite, write_report
from tests.test_core import capture, oracle


class FakeDevice:
    def __init__(self, outcomes):self.outcomes=iter(outcomes);self.value='0';self.steps=[];self.resets=0
    def prepare(self,apk,fixture):
        self.resets+=1;self.value='0';self.name='';self.outcome=next(self.outcomes)
        return {'installedVerified':True,'fixtureVerified':True,'apkSha256':'test'}
    def observe(self):return {'name':self.name,'count':self.value}
    def execute(self,step):
        self.steps.append(step['action'])
        if step['action']=='replace':self.name=step['parameters']['value']
        if step['action']=='tap':self.value=self.outcome
    def stop(self):pass


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name);self.apk=self.root/'test.apk';self.apk.write_bytes(b'test')
        self.bundle=create_bundle(capture(),oracle(),self.apk,self.root/'bundle')
    def tearDown(self):self.t.cleanup()
    def test_original_resets_every_run_and_persists_all_steps(self):
        d=FakeDevice(['2']*3);r=replay_suite(d,self.bundle,self.apk,self.root/'run')
        self.assertEqual(r['status'],'reproduced');self.assertEqual(d.resets,3)
        self.assertEqual(d.steps,['replace','tap']*3)
        self.assertEqual(len(list((self.root/'run').glob('run-*.json'))),3)
    def test_patched_compare_without_build_evidence_does_not_verify(self):
        r=replay_suite(FakeDevice(['1']*3),self.bundle,self.apk,self.root/'run','patched')
        self.assertNotEqual(r['status'],'verified')
    def test_mixed_results_not_cherry_picked(self):
        r=replay_suite(FakeDevice(['2','1','2']),self.bundle,self.apk,self.root/'run')
        self.assertEqual(r['status'],'inconclusive');self.assertEqual(len(r['runs']),3)
    def test_mutation_after_compilation_blocks_device_work(self):
        (self.bundle['path']/'capture.json').write_text('{}')
        d=FakeDevice(['2']*3);r=replay_suite(d,self.bundle,self.apk,self.root/'run')
        self.assertEqual(r['status'],'environment_blocked');self.assertEqual(d.resets,0)
    def test_expired_budget_never_touches_device(self):
        d=FakeDevice(['2']*3)
        r=replay_suite(d,self.bundle,self.apk,self.root/'run',deadline=0)
        self.assertEqual(r['status'],'budget_exhausted');self.assertEqual(d.resets,0)
    def test_cancelled_run_is_persisted(self):
        d=FakeDevice(['2']*3)
        def cancel(step):raise KeyboardInterrupt()
        d.execute=cancel
        r=replay_suite(d,self.bundle,self.apk,self.root/'run')
        self.assertEqual(r['status'],'cancelled');self.assertTrue(r['runs'][0]['cancelled'])

    def test_html_escapes_untrusted_content(self):
        path=self.root/'report.html';write_report(path,{'status':'<script>bad</script>','runs':[]})
        self.assertNotIn('<script>',path.read_text());self.assertIn('&lt;script&gt;',path.read_text())

if __name__=='__main__':unittest.main()
