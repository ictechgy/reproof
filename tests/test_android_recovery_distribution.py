"""Private recovery source/distribution closure, no device or compiler execution."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL='android/live/src/main/java/io/reproof/live/AndroidRecoveryProtocol.kt'
HELPER='android/live/src/main/java/io/reproof/live/LiveInstrumentation.kt'

class RecoveryDistributionTests(unittest.TestCase):
    def test_protocol_and_helper_have_real_pin_and_distribution_closure(self):
        from reproof.android_logic_repair_demo import PIN_FILE,PIN_SHA256,RECIPE,_load_source,NativeLogicError
        raw=(ROOT/PIN_FILE).read_bytes();pins=json.loads(raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),PIN_SHA256)
        resources=json.loads((ROOT/'distribution-resources.json').read_text())['files']
        for name in (PROTOCOL,HELPER):
            self.assertIn(name,pins);self.assertIn(name,resources)
        with tempfile.TemporaryDirectory() as temporary:
            copied=Path(temporary).resolve()
            for name in (*pins,PIN_FILE,RECIPE):
                target=copied/name;target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes((ROOT/name).read_bytes())
            _load_source(copied)
            target=copied/PROTOCOL;target.write_bytes(target.read_bytes()+b'\n// synthetic tamper\n')
            with self.assertRaises(NativeLogicError) as caught:_load_source(copied)
            self.assertEqual(caught.exception.code,'source_pin')

    def test_ci_is_compile_only_and_no_normal_recovery_exposure(self):
        goals=json.loads((ROOT/'tests/release_inventory.json').read_text())['goals']
        goal=goals['ANDROID_RECOVERY_QUALIFICATION_NATIVE']['checks'][0]
        self.assertIn('native-compile',goal['effects']);self.assertNotIn('owned-device',goal['effects'])
        workflow=(ROOT/'.github/workflows/android-compile.yml').read_text()
        self.assertIn('--goal ANDROID_RECOVERY_QUALIFICATION_NATIVE',workflow)
        self.assertIn('recovery-contracts.json',workflow)
        self.assertNotIn('connectedAndroidTest',workflow)
        self.assertIn(':live:compileDebugKotlin',workflow)
        for name in ('cli.py','server.py','worker_cli.py','model.py','android_live.py'):
            self.assertNotIn('android_recovery_qualification',(ROOT/'reproof/live'/name).read_text())
        self.assertIn('--goal ANDROID_RECOVERY_QUALIFICATION_CONTRACTS',
            (ROOT/'.github/workflows/portable-tests.yml').read_text())
