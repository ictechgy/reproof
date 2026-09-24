import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from reproof.core import ContractError,digest
from reproof.ios_storage import create_ios_bundle,load_ios_bundle,tree_manifest
from reproof.ios_runner import prepare_xctestrun
from tests.test_ios_core import ios_capture,ios_oracle


class IosStorageTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name);self.products=self.root/'products';self.products.mkdir()
        self.app=self.products/'Debug-iphonesimulator/ReproSample.app';self.app.mkdir(parents=True)
        with (self.app/'Info.plist').open('wb') as f:plistlib.dump({'CFBundleIdentifier':'io.reproof.sample.ios','ReproBuildID':'12345678'},f)
        (self.app/'ReproSample').write_bytes(b'synthetic-app')
        self.receipt={'productsDigest':digest(tree_manifest(self.products)),'buildCompleted':True,'buildId':'12345678',
                      'appRelative':'Debug-iphonesimulator/ReproSample.app'}
    def tearDown(self):self.t.cleanup()
    def test_v2_roundtrip_and_tampering(self):
        b=create_ios_bundle(ios_capture(),ios_oracle(),self.products,self.receipt,self.root/'bundle')
        self.assertEqual(load_ios_bundle(b['path'])['scenario']['platform'],'ios')
        (b['app']/'ReproSample').write_bytes(b'changed')
        with self.assertRaises(ContractError):load_ios_bundle(b['path'])
    def test_wrong_evidence_strength_is_rejected(self):
        b=create_ios_bundle(ios_capture(),ios_oracle(),self.products,self.receipt,self.root/'bundle')
        m=b['manifest'];m['policy']['requiredEvidence']='installed-binary-hash'
        (b['path']/'manifest.json').write_text(json.dumps(m))
        with self.assertRaises(ContractError):load_ios_bundle(b['path'])
    def test_symlinked_artifacts_rejected(self):
        (self.products/'linked').symlink_to(self.app/'ReproSample')
        with self.assertRaises(ContractError):tree_manifest(self.products)
    def test_physical_bundle_cannot_relabel_simulator_or_unsigned_products(self):
        self.receipt['executionEnvironment']='physical-iphone'
        with self.assertRaises(ContractError):
            create_ios_bundle(ios_capture(),ios_oracle(),self.products,self.receipt,self.root/'unsigned')
        target=self.products/'Debug-iphoneos'
        self.app.parent.rename(target);self.app=target/'ReproSample.app'
        self.receipt.update(appRelative='Debug-iphoneos/ReproSample.app',signed=True,
                            productsDigest=digest(tree_manifest(self.products)))
        bundle=create_ios_bundle(ios_capture(),ios_oracle(),self.products,self.receipt,self.root/'physical')
        self.assertEqual(bundle['manifest']['policy']['requiredEvidence'],'ios-physical-install-receipt-runtime-id')
        manifest=bundle['manifest'];manifest['executionEnvironment']='simulator'
        (bundle['path']/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(ContractError):load_ios_bundle(bundle['path'])
    def test_test_configuration_preserves_fixed_runner_and_injects_bounded_payload(self):
        runner=self.products/'Runner.app';runner.mkdir();test=runner/'UITests.xctest';test.mkdir()
        config={'__xctestrun_metadata__':{'FormatVersion':2},'TestConfigurations':[{'Name':'Default','TestTargets':[
            {'BlueprintName':'ReproReplayTests','TestBundlePath':'__TESTROOT__/Runner.app/UITests.xctest',
             'TestHostPath':'__TESTROOT__/Runner.app','UITargetAppPath':'__TESTROOT__/Debug-iphonesimulator/ReproSample.app'}]}]}
        with (self.products/'Replay.xctestrun').open('wb') as f:plistlib.dump(config,f)
        out=prepare_xctestrun(self.products,'ReproReplayTests',self.root/'run.xctestrun',payload={'runId':'id'},app=self.app)
        with out.open('rb') as f:data=plistlib.load(f)
        target=data['TestConfigurations'][0]['TestTargets'][0]
        self.assertEqual(Path(target['TestHostPath']).resolve(),runner.resolve())
        self.assertIn('REPRO_SCENARIO_B64',target['EnvironmentVariables'])
        with self.assertRaises(ContractError):
            prepare_xctestrun(self.products,'ReproReplayTests',self.root/'large.xctestrun',payload={'x':'a'*20000},app=self.app)
        config['TestConfigurations'][0]['TestTargets'][0]['TestHostPath']='/bin/sh'
        with (self.products/'Replay.xctestrun').open('wb') as f:plistlib.dump(config,f)
        with self.assertRaises(ContractError):prepare_xctestrun(self.products,'ReproReplayTests',self.root/'unsafe.xctestrun')

if __name__=='__main__':unittest.main()
