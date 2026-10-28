"""Capture policy package/pin/CI closure without any native execution."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POLICY = 'android/live/src/main/java/io/reproof/live/CapturePolicy.kt'
HELPER = 'android/live/src/main/java/io/reproof/live/LiveInstrumentation.kt'


class AndroidCaptureDistributionTests(unittest.TestCase):
    def test_policy_helper_and_all_existing_sources_remain_pinned_and_distributed(self):
        from reproof.android_logic_repair_demo import PIN_SHA256, PIN_FILE, RECIPE, _load_source, NativeLogicError
        raw = (ROOT / 'examples/android-logic-repair/source-pins.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), PIN_SHA256)
        pins = json.loads(raw)
        resources = json.loads((ROOT / 'distribution-resources.json').read_text())['files']
        for name in (POLICY, HELPER):
            self.assertIn(name, pins); self.assertIn(name, resources)
        for name, digest in pins.items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest, name)
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary).resolve()
            for name in (*pins, PIN_FILE, RECIPE):
                target = copied / name; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            _load_source(copied)
            target = copied / POLICY
            target.write_bytes(target.read_bytes() + b'\n// synthetic tamper\n')
            self.assertNotEqual(hashlib.sha256(target.read_bytes()).hexdigest(), pins[POLICY])
            with self.assertRaises(NativeLogicError) as caught:
                _load_source(copied)
            self.assertEqual(getattr(caught.exception, 'code', None), 'source_pin')

    def test_native_gate_is_explicit_and_does_not_add_device_execution(self):
        goals = json.loads((ROOT / 'tests/release_inventory.json').read_text())['goals']
        check = goals['ANDROID_CAPTURE_NATIVE']['checks'][0]
        self.assertIn('native-compile', check['effects'])
        self.assertNotIn('owned-device', check['effects'])
        self.assertIn('tests.test_android_native_frame_geometry', check['command'])
        workflow = (ROOT / '.github/workflows/android-compile.yml').read_text()
        self.assertIn('--goal ANDROID_CAPTURE_NATIVE', workflow)
        self.assertIn('capture-contracts.json', workflow)
        self.assertNotIn('connectedAndroidTest', workflow)
        self.assertNotIn('testDebugAndroidTest', workflow)
        self.assertIn(':live:compileDebugKotlin', workflow)
        portable = (ROOT / '.github/workflows/portable-tests.yml').read_text()
        self.assertIn('--goal ANDROID_CAPTURE_CONTRACTS', portable)
        self.assertNotIn('--goal ANDROID_CAPTURE_NATIVE', portable)


if __name__ == '__main__': unittest.main()
