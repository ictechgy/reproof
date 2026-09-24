"""Resource registration proves hashes and policy, never macOS bootability."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
import uuid

from reproof.execution.resources import GuestBundle, ResourceError, provision, validate_catalog
from tests.test_execution_protocol import build_environment


def catalog():
    return [{"id": "build", "executionClass": "build-guest", "argv": ["/usr/bin/true"],
             "artifactPolicyId": "bounded-artifacts", "cleanupPolicyId": "dispose-overlay",
             "outputPaths": ["product.bin"], "maxOutputBytes": 4096, "timeoutMs": 1000}]


def resource_inputs(root):
    paths = {}
    for name in ("disk", "auxiliary", "hardware", "machine", "toolchain", "helper"):
        path = root / name
        path.write_bytes((name + " fixture" + (uuid.uuid4().hex if name == "machine" else "")).encode())
        paths[name] = path
    image = {"schemaVersion": 1, "id": "macos-base", "kind": "macos-vm-image",
             "architecture": "arm64", "artifactDigest": hashlib.sha256(paths["disk"].read_bytes()).hexdigest(),
             "sizeBytes": paths["disk"].stat().st_size}
    toolchain = {"schemaVersion": 1, "id": "offline-tools", "kind": "offline-toolchain",
                 "architecture": "arm64", "artifactDigest": hashlib.sha256(paths["toolchain"].read_bytes()).hexdigest(),
                 "sizeBytes": paths["toolchain"].stat().st_size,
                 "tools": [{"id": "python", "version": "3.14", "artifactDigest": "a" * 64}]}
    descriptor = build_environment()
    descriptor.update(guestImageId=image["id"], toolchainId=toolchain["id"])
    metadata = {"schemaVersion": 1, "environment": descriptor, "guestImage": image,
                "toolchain": toolchain, "agentDigest": "d" * 64, "catalog": catalog()}
    return metadata, paths


class ExecutionResourcesTests(unittest.TestCase):
    def test_new_bundle_is_hash_bound_and_modified_resource_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            metadata, paths = resource_inputs(root)
            bundle = provision(root / "bundle", metadata=metadata, resources=paths)
            self.assertEqual(bundle.environment_digest, GuestBundle.load(root / "bundle").environment_digest)
            self.assertEqual(bundle.recipe("build")["argv"], ["/usr/bin/true"])
            with self.assertRaises(ResourceError):
                provision(root / "bundle", metadata=metadata, resources=paths)
            target = root / "bundle/disk.img"
            target.chmod(0o600)
            target.write_bytes(b"tamper")
            with self.assertRaises(ResourceError):
                GuestBundle.load(root / "bundle")

    def test_resource_symlink_secret_and_declared_digest_mismatch_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            metadata, paths = resource_inputs(root)
            for index, replacement in enumerate((root / "link", root / ".env")):
                if index == 0:
                    replacement.symlink_to(paths["disk"])
                with self.assertRaises(ResourceError):
                    provision(root / f"denied-{index}", metadata=metadata,
                              resources={**paths, "disk": replacement})
            wrong = copy.deepcopy(metadata)
            wrong["guestImage"]["artifactDigest"] = "f" * 64
            with self.assertRaises(ResourceError):
                provision(root / "mismatch", metadata=wrong, resources=paths)

    def test_registered_recipe_and_composite_environment_identity_are_closed(self):
        for change in ({"argv": ["relative"]}, {"outputPaths": ["../out"]},
                       {"maxOutputBytes": True}, {"command": "candidate-hook"}):
            value = catalog()
            value[0].update(change)
            with self.assertRaises(ResourceError):
                validate_catalog(value)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            metadata, paths = resource_inputs(root)
            first = provision(root / "first", metadata=metadata, resources=paths)
            metadata["catalog"][0]["argv"] = ["/usr/bin/false"]
            second = provision(root / "second", metadata=metadata, resources=paths)
            self.assertNotEqual(first.environment_digest, second.environment_digest)
