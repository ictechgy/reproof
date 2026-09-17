"""Immutable, bounded transfer tests without launching candidate code."""
import base64
import io
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import zipfile

from reproloop.execution import artifacts, wire


class ExecutionArtifactsTests(unittest.TestCase):
    def test_format_parser_failure_is_a_static_artifact_rejection(self):
        authority = artifacts.ArtifactValidationAuthority()
        def fixed_zip_checker(blobs):
            with zipfile.ZipFile(io.BytesIO(dict(blobs.entries)["app.zip"])):
                return True
        authority.register("zip-format", paths=["app.zip"], max_bytes=1024, checker=fixed_zip_checker)
        with self.assertRaises(artifacts.ArtifactError):
            authority.validate(artifacts.BlobSet((("app.zip", b"not a zip"),)), policy_id="zip-format",
                               project_digest="a" * 64, execution_class="desktop-guest")

    def test_validation_capability_is_project_class_and_local_validator_bound(self):
        authority = artifacts.ArtifactValidationAuthority()
        authority.register("desktop-app", paths=["app.zip"], max_bytes=1024,
                           checker=lambda blobs: dict(blobs.entries)["app.zip"].startswith(b"PK"))
        blobs = artifacts.BlobSet((("app.zip", b"PK bounded fixture"),))
        receipt = authority.validate(blobs, policy_id="desktop-app", project_digest="a" * 64,
                                     execution_class="desktop-guest")
        self.assertIs(authority.require_input(receipt, input_digest=blobs.digest,
                      project_digest="a" * 64, execution_class="desktop-guest"), blobs)
        for candidate, project, execution_class in (({}, "a" * 64, "desktop-guest"),
                (receipt, "b" * 64, "desktop-guest"), (receipt, "a" * 64, "mobile-device")):
            with self.assertRaises(artifacts.ArtifactError):
                authority.require_input(candidate, input_digest=blobs.digest,
                                        project_digest=project, execution_class=execution_class)
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.ArtifactValidationAuthority().require_input(receipt, input_digest=blobs.digest,
                project_digest="a" * 64, execution_class="desktop-guest")
        with self.assertRaises(artifacts.ArtifactError):
            authority.validate(artifacts.BlobSet((("app.zip", b"bad"),)), policy_id="desktop-app",
                               project_digest="a" * 64, execution_class="desktop-guest")

    def test_freeze_protects_original_and_roundtrips_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "file.py").write_bytes(b"old\n")
            frozen = artifacts.BlobSet.from_directory(root, ["file.py"])
            (root / "file.py").write_bytes(b"new\n")
            frozen.write_new(root / "candidate")
            self.assertEqual((root / "candidate/file.py").read_bytes(), b"old\n")
            self.assertEqual((root / "file.py").read_bytes(), b"new\n")
            with self.assertRaises(artifacts.ArtifactError):
                frozen.write_new(root / "candidate")

    def test_symlink_ancestor_secret_and_file_collisions_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "real").mkdir()
            (root / "real/file").write_bytes(b"content")
            (root / "link").symlink_to(root / "real", target_is_directory=True)
            for path in ("link/file", "../escape", ".env", "auth.json", "keys/key.pem"):
                with self.subTest(path=path), self.assertRaises(artifacts.ArtifactError):
                    artifacts.BlobSet.from_directory(root, [path])
            (root / "real/subdir").mkdir()
            (root / "real/subdir/file").write_bytes(b"public fixture")
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.BlobSet.from_directory(root / "link/subdir", ["file"])
            for entries in ((("a", b"x"), ("a/b", b"y")), (("A", b"x"), ("a", b"y"))):
                with self.assertRaises(artifacts.ArtifactError):
                    artifacts.BlobSet(entries)

    def test_socket_transfer_is_digest_checked_and_ordered(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        host = wire.Channel(left, key=b"k" * 32, run_id="blob", role="host", deadline=time.monotonic() + 2)
        guest = wire.Channel(right, key=b"k" * 32, run_id="blob", role="guest", deadline=time.monotonic() + 2)
        blobs = artifacts.BlobSet((("src/main.py", b"x" * 80000), ("empty", b"")))
        sender = threading.Thread(target=artifacts.send_blobs, args=(host, blobs), kwargs={"prefix": "input"})
        sender.start()
        received = artifacts.receive_blobs(guest, prefix="input", expected_digest=blobs.digest)
        sender.join(2)
        self.assertFalse(sender.is_alive())
        self.assertEqual(received.entries, blobs.entries)

    def test_forged_size_digest_reordered_or_extra_data_cannot_escape(self):
        blobs = artifacts.BlobSet((("file", b"good"),))
        valid_chunk = {"index": 0, "offset": 0, "data": base64.b64encode(b"good").decode()}
        cases = [
            [("input-chunk", {**valid_chunk, "data": "YmFkIQ=="}), ("input-end", {})],
            [("input-chunk", {**valid_chunk, "offset": 1}), ("input-end", {})],
            [("input-end", {})],
            [("input-chunk", valid_chunk), ("input-chunk", valid_chunk), ("input-end", {})],
        ]
        for frames in cases:
            with self.subTest(frames=tuple(kind for kind, _ in frames)):
                iterator = iter([("input-start", blobs.manifest), *frames])
                class Peer:
                    def receive(self):
                        return next(iterator)
                with self.assertRaises(artifacts.ArtifactError):
                    artifacts.receive_blobs(Peer(), prefix="input", expected_digest=blobs.digest)

    def test_peer_manifest_cannot_expand_local_output_policy(self):
        blobs = artifacts.BlobSet((("result.bin", b"12345"),))
        class Peer:
            def receive(self):
                return "artifact-start", blobs.manifest
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.receive_blobs(Peer(), prefix="artifact", max_bytes=4,
                                    allowed_paths=("result.bin",))
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.receive_blobs(Peer(), prefix="artifact", allowed_paths=("another.bin",))
