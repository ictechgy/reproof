"""Real CLI denial/provisioning with explicit non-bootable resource fixtures."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_execution_resources import resource_inputs
from reproof.execution.journal import RunStore
from reproof.execution.resources import provision

ROOT = Path(__file__).resolve().parents[1]


class ExecutionCLITests(unittest.TestCase):
    def command(self, script, *arguments):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *map(str, arguments)],
                              capture_output=True, timeout=20, cwd=ROOT)

    def test_no_environment_produces_a_blocked_artifact_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve() / "qa"
            result = self.command("qa-execution-backend.py", "--output-new", output)
            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stdout)
            self.assertEqual(report["reason"], "environment-not-supplied")
            self.assertFalse(report["qualified"])
            self.assertFalse(report["actualVM"])
            self.assertEqual(json.loads((output / "result.json").read_bytes()), report)

    def test_json_qualification_and_secret_named_descriptor_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "environment.json"
            path.write_text('{"qualified":true,"actualVM":true}')
            output = root / "rejected-qa"
            result = self.command("qa-execution-backend.py", "--environment", path, "--output-new", output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stdout)["qualified"])
            self.assertEqual(json.loads((output / "result.json").read_bytes()), json.loads(result.stdout))
            # It need not exist: filename policy must reject before opening.
            result = self.command("qa-execution-backend.py", "--environment", root / ".env")
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(str(root).encode(), result.stdout + result.stderr)

    def test_provision_cli_seals_supplied_bytes_without_booting_or_qualifying(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            metadata, paths = resource_inputs(root)
            descriptor = root / "metadata.json"
            descriptor.write_text(json.dumps(metadata))
            arguments = ["--metadata", descriptor, "--output-new", root / "bundle"]
            for key, path in paths.items():
                arguments.extend(["--" + key, path])
            result = self.command("provision-repair-guest.py", *arguments)
            self.assertEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "resources-sealed")
            self.assertFalse(report["actualVM"])
            self.assertFalse(report["qualified"])

    def test_recovery_cli_requires_private_native_proof_and_never_qualifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            metadata, paths = resource_inputs(root)
            bundle = provision(root / "bundle", metadata=metadata, resources=paths)
            store = RunStore(root / "state", environment_digest=bundle.environment_digest, disk_limit=1024)
            with store.machine_lease(bundle.machine_digest), store.admit(
                    "lost-parent", "a" * 64, disk_bytes=bundle.overlay_bytes):
                pass
            environment = root / "environment.json"
            environment.write_text(json.dumps({"schemaVersion": 1, "backendId": "apple-vm",
                "bundlePath": str(bundle.root), "statePath": str(store.root), "diskBudgetBytes": 1024}))
            arguments = ["--environment", environment, "--recover-operation", "lost-parent", "--request-digest", "a" * 64]
            denied = self.command("qa-execution-backend.py", *arguments)
            self.assertEqual(denied.returncode, 2)
            # This is an explicit journal fixture. Native receipt production is
            # independently exercised by test_execution_native without VM boot.
            proof = {"schemaVersion": 1, "operationId": "lost-parent", "requestDigest": "a" * 64, "state": "not-started"}
            (store.root / "runs/lost-parent/termination.json").write_text(json.dumps(proof))
            recovered = self.command("qa-execution-backend.py", *arguments)
            self.assertEqual(recovered.returncode, 0)
            report = json.loads(recovered.stdout)
            self.assertEqual(report["status"], "recovered")
            self.assertEqual(report["recoveryEvidence"], "native-termination-record")
            self.assertFalse(report["actualVM"])
            self.assertFalse(report["qualified"])
            self.assertEqual(store.status("lost-parent")["state"], "failed")
