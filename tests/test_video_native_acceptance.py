import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).parents[1]


class NativeVideoAcceptanceTests(unittest.TestCase):
    def test_real_verify_video_route_encodes_and_independently_decodes(self):
        completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/verify-video.py"),
                    "--backend", "avfoundation",
                    "--cases", "all",
                    "--output-root", str(ROOT / "artifacts/qa-delivery/g3-native-gate"),
                    "--output-new",
                ],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
        stdout = completed.stdout.decode("utf-8", "replace")
        stderr = completed.stderr.decode("utf-8", "replace")
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            result = None
        if completed.returncode != 0:
            if isinstance(result, dict):
                diagnostic = (
                    f"status={result.get('status')!r} "
                    f"reason={result.get('reason')!r} "
                    f"outputDirectory={result.get('outputDirectory')!r}"
                )
            else:
                diagnostic = (stdout + "\n" + stderr)[:4096]
            self.fail(diagnostic)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["actualEncoding"], "avfoundation-h264-mp4")
        self.assertEqual(result["independentDecode"]["decodedFrameCount"], 24)
        self.assertEqual(result["segments"], 2)
        self.assertEqual(len(result["protocolRejections"]), 21)
        self.assertTrue(all(item["rejected"] for item in result["protocolRejections"]))
        self.assertEqual(len(result["failureCases"]), 7)
        self.assertTrue(all(item["segments"] == 0 for item in result["failureCases"]))
        print("Native video evidence: " + result["outputDirectory"])


if __name__ == "__main__":
    unittest.main()
