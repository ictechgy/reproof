"""Behavioral checks for the read-only repair backend prerequisite doctor."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).parents[1]


class RepairBackendDoctorTests(unittest.TestCase):
    def invoke(self, *arguments):
        process = subprocess.run(
            [sys.executable, "scripts/repair-backend-doctor.py", "--read-only", *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
        )
        return process, json.loads(process.stdout)

    def test_current_mac_support_is_measured_but_missing_inputs_do_not_qualify_it(self):
        process, report = self.invoke()
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertEqual(report["hostSupport"]["architecture"], "arm64")
        self.assertEqual(report["hostSupport"]["avFoundation"], "available")
        self.assertEqual(report["hostSupport"]["virtualizationFramework"], "available")
        expected_status = {
            "supported": "prerequisites-missing",
            "unsupported": "backend-unqualified",
        }[report["hostSupport"]["virtualizationRuntime"]]
        self.assertEqual(report["status"], expected_status)
        self.assertEqual(report["prerequisites"]["guestImage"], "missing")
        self.assertEqual(report["prerequisites"]["offlineToolchain"], "missing")
        self.assertEqual(report["prerequisites"]["buildEnvironment"], "missing")
        self.assertEqual(report["prerequisites"]["iosSigning"], "optional-missing")
        self.assertEqual(report["authority"], "none")
        self.assertEqual(report["environmentGate"], "blocked-unqualified")

    def test_explicit_metadata_and_resources_remain_unqualified_inspection_inputs(self):
        with tempfile.TemporaryDirectory(prefix="g8a-private-marker-") as temporary:
            directory = Path(temporary)
            image = directory / "guest-image.bin"
            toolchain = directory / "offline-tools.bin"
            image.write_bytes(b"not-a-bootable-image")
            toolchain.write_bytes(b"not-a-toolchain")
            image_manifest = directory / "guest-image.json"
            toolchain_manifest = directory / "offline-tools.json"
            environment = directory / "build-environment.json"
            image_manifest.write_text(json.dumps({
                "schemaVersion": 1,
                "id": "macos-image",
                "kind": "macos-vm-image",
                "architecture": "arm64",
                "artifactDigest": hashlib.sha256(image.read_bytes()).hexdigest(),
                "sizeBytes": image.stat().st_size,
            }))
            toolchain_manifest.write_text(json.dumps({
                "schemaVersion": 1,
                "id": "offline-tools",
                "kind": "offline-toolchain",
                "architecture": "arm64",
                "artifactDigest": hashlib.sha256(toolchain.read_bytes()).hexdigest(),
                "sizeBytes": toolchain.stat().st_size,
                "tools": [{
                    "id": "swift", "version": "6.2", "artifactDigest": "3" * 64
                }],
            }))
            environment.write_text(json.dumps({
                "schemaVersion": 1,
                "id": "build-environment",
                "executionClass": "build-guest",
                "architecture": "arm64",
                "network": "none",
                "transport": "virtio-vsock",
                "guestImageId": "macos-image",
                "toolchainId": "offline-tools",
                "controls": [
                    "network-deny", "immutable-input", "bounded-output",
                    "process-termination", "overlay-cleanup",
                ],
                "resources": {
                    "cpuCount": 2,
                    "memoryMiB": 4096,
                    "diskBytes": 20_000_000_000,
                    "timeoutMs": 900_000,
                },
            }))
            process, report = self.invoke(
                "--guest-image-manifest", str(image_manifest),
                "--guest-image", str(image),
                "--offline-toolchain-manifest", str(toolchain_manifest),
                "--offline-toolchain", str(toolchain),
                "--environment-descriptor", str(environment),
            )
            rendered = process.stdout + process.stderr
        expected = {
            "supported": (0, "prerequisite-inputs-present"),
            "unsupported": (2, "backend-unqualified"),
        }[report["hostSupport"]["virtualizationRuntime"]]
        self.assertEqual((process.returncode, report["status"]), expected, process.stderr)
        self.assertEqual(report["prerequisites"]["guestImage"], "resource-present-unverified")
        self.assertEqual(report["prerequisites"]["offlineToolchain"], "resource-present-unverified")
        self.assertEqual(report["prerequisites"]["buildEnvironment"], "descriptor-valid")
        self.assertEqual(report["environmentGate"], "blocked-unqualified")
        self.assertNotIn("g8a-private-marker", rendered)

    def test_sensitive_descriptor_name_is_refused_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = "should-never-be-read-or-printed"
            descriptor = Path(temporary) / "credentials.json"
            descriptor.write_text(marker)
            process, report = self.invoke("--guest-image-manifest", str(descriptor))
        self.assertEqual(process.returncode, 2)
        self.assertEqual(report["status"], "backend-unqualified")
        self.assertEqual(report["prerequisites"]["guestImage"], "invalid")
        self.assertNotIn(marker, process.stdout + process.stderr)
        self.assertNotIn("credentials.json", process.stdout + process.stderr)


if __name__ == "__main__":
    unittest.main()
