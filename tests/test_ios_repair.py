from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
from reproloop.core import digest
from reproloop.ios_repair import repair_ios_job
from reproloop.ios_storage import create_ios_bundle,tree_manifest
from reproloop.ios_build import PRODUCT_FILE
from tests.test_ios_core import ios_capture,ios_oracle


class Agent:
    def propose(self,*args):return [{'path':PRODUCT_FILE,'old':'return 2','new':'return 1'}]


class FakeSimulator:
    udid='00000000-0000-0000-0000-000000000000'
    def __init__(self,outcomes):self.outcomes=iter(outcomes);self.kits=[]
    def run_scenario(self,products,app,scenario,output,mode='replay'):
        self.kits.append(products);output.mkdir(parents=True)
        outcome=next(self.outcomes)
        return {'runId':str(len(self.kits)),'runValid':True,'bugCondition':outcome=='bug',
                'expectedCondition':outcome=='normal','evidenceValid':True,'protectedPathsValid':True,'regressionPassed':False}


class IosRepairTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name);self.source=self.root/'ios'
        file=self.source/PRODUCT_FILE;file.parent.mkdir(parents=True);file.write_text('enum CounterLogic { static func increment() -> Int { return 2 } }\n')
        (self.source/'Protected.swift').write_text('let protected = true\n')
        self.products=self.root/'products';self.app=self.products/'ReproSample.app';self.app.mkdir(parents=True)
        with (self.app/'Info.plist').open('wb') as f:plistlib.dump({'CFBundleIdentifier':'io.reproloop.sample.ios','ReproBuildID':'original-build'},f)
        receipt={'productsDigest':digest(tree_manifest(self.products)),'sourceDigest':digest(tree_manifest(self.source,True)),
                 'buildCompleted':True,'buildId':'original-build','appRelative':'ReproSample.app'}
        self.bundle=create_ios_bundle(ios_capture(),ios_oracle(),self.products,receipt,self.root/'bundle')
    def tearDown(self):self.t.cleanup()
    def build(self,source,output,simulator_id,**kwargs):
        app=output/'products/ReproSample.app';app.mkdir(parents=True)
        with (app/'Info.plist').open('wb') as f:plistlib.dump({'CFBundleIdentifier':'io.reproloop.sample.ios','ReproBuildID':'patched-build'},f)
        return {'app':app,'products':app.parent,'receipt':{'sourceDigest':digest(tree_manifest(source,True)),'buildId':'patched-build'}}
    def test_patch_uses_original_runner_and_keeps_root_source(self):
        sim=FakeSimulator(['bug']*3+['normal']*3)
        with patch('reproloop.ios_repair.build_ios',self.build),patch('reproloop.ios_repair.run_logic_test',return_value={'passedTests':1}):
            result=repair_ios_job(sim,self.bundle,self.source,self.root/'repair',Agent())
        self.assertEqual(result['status'],'verified');self.assertEqual(result['executionEnvironment'],'simulator')
        self.assertTrue(all(kit==self.bundle['products'] for kit in sim.kits))
        self.assertIn('return 2',(self.source/PRODUCT_FILE).read_text())
    def test_mixed_baseline_stops_before_patch(self):
        with patch('reproloop.ios_repair.build_ios') as build:
            result=repair_ios_job(FakeSimulator(['bug','normal','bug']),self.bundle,self.source,self.root/'repair',Agent())
            build.assert_not_called()
        self.assertEqual(result['status'],'inconclusive')
    def test_protected_source_edit_is_blocked(self):
        class BadAgent:
            def propose(self,*args):return [{'path':'Protected.swift','old':'true','new':'false'}]
        with patch('reproloop.ios_repair.build_ios') as build:
            result=repair_ios_job(FakeSimulator(['bug']*3),self.bundle,self.source,self.root/'repair',BadAgent())
            build.assert_not_called()
        self.assertEqual(result['status'],'verification_failed')

if __name__=='__main__':unittest.main()
