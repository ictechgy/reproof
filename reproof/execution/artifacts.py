"""Inert regular-file inputs/outputs. No archive extraction or host execution."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat
import threading

from reproloop.contracts.versions import bounded_int, digest, require, safe_relative_path, validate_digest, validate_id
from reproloop.core import ContractError
from .wire import (CHUNK_BYTES, MAX_TRANSFER_BYTES, ProtocolError,
                   canonical, decode_chunk, safe_transfer_path, validate_manifest)


class ArtifactError(RuntimeError):
    """Static transfer or file-boundary failure."""


def open_directory(root):
    """Pin every component of an absolute directory, including the supplied root."""
    root = Path(root).absolute()
    descriptor = None
    try:
        parts = root.parts[1:]
        if parts:
            safe_transfer_path("/".join(parts))
        descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        result, descriptor = descriptor, None
        return result
    except (OSError, ContractError):
        raise ArtifactError("Directory boundary rejected") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def open_regular(root, relative):
    """Open relative to a pinned directory; reject every symlink component."""
    directory = None
    try:
        safe_transfer_path(relative)
        directory = open_directory(root)
        parts = relative.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ArtifactError("Regular file required")
        return fd
    except (OSError, ContractError):
        raise ArtifactError("File boundary rejected") from None
    finally:
        if directory is not None:
            os.close(directory)


def read_regular(root, relative, *, maximum=MAX_TRANSFER_BYTES):
    fd = open_regular(root, relative)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > maximum:
            raise ArtifactError("File limit exceeded")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if (len(raw) > maximum or len(raw) != before.st_size
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ArtifactError("File changed during capture")
        return raw


@dataclass(frozen=True, slots=True)
class BlobSet:
    entries: tuple[tuple[str, bytes], ...]
    _files: tuple[tuple[str, str, int], ...] = field(init=False, repr=False, compare=False)
    _digest: str = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        try:
            require(type(self.entries) is tuple and all(type(item) is tuple and len(item) == 2
                    and type(item[0]) is str and type(item[1]) is bytes for item in self.entries),
                    "Immutable file bytes required")
            require(sum(len(data) for _, data in self.entries) <= MAX_TRANSFER_BYTES,
                    "Transfer limit exceeded")
            object.__setattr__(self, "entries", tuple(sorted(self.entries)))
            object.__setattr__(self, "_files", tuple(
                (path, hashlib.sha256(data).hexdigest(), len(data)) for path, data in self.entries))
            files = [{"path": path, "digest": file_digest, "size": size}
                     for path, file_digest, size in self._files]
            object.__setattr__(self, "_digest", digest(files))
            manifest = {"digest": self._digest, "files": files}
            validate_manifest(manifest)
            require(len(canonical(manifest)) <= 200 * 1024, "Manifest limit exceeded")
        except (ContractError, ProtocolError, TypeError):
            raise ArtifactError("Invalid sealed files") from None

    @property
    def manifest(self):
        files = [{"path": path, "digest": file_digest, "size": size}
                 for path, file_digest, size in self._files]
        return {"digest": self._digest, "files": files}

    @property
    def digest(self):
        return self._digest

    @classmethod
    def from_directory(cls, root, paths, *, max_bytes=MAX_TRANSFER_BYTES):
        if type(paths) not in (tuple, list) or not 0 < len(paths) <= 1024:
            raise ArtifactError("Invalid sealed file selection")
        entries = []
        remaining = min(MAX_TRANSFER_BYTES, max_bytes)
        for path in paths:
            data = read_regular(root, path, maximum=remaining)
            remaining -= len(data)
            entries.append((path, data))
        return cls(tuple(entries))

    def write_new(self, root):
        """Only a fresh owned directory, before a guest candidate can run."""
        root = Path(root)
        parent = directory = None
        try:
            safe_transfer_path(root.name)
            parent = open_directory(root.parent)
            os.mkdir(root.name, mode=0o700, dir_fd=parent)
            directory = os.open(root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            for relative, data in self.entries:
                current = os.dup(directory)
                try:
                    parts = relative.split("/")
                    for part in parts[:-1]:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=current)
                        except FileExistsError:
                            pass
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                        os.close(current)
                        current = child
                    fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=current)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                finally:
                    os.close(current)
        except (OSError, ContractError):
            # Partial output is retained. The owner must account for and clean it.
            raise ArtifactError("File publication failed") from None
        finally:
            if directory is not None:
                os.close(directory)
            if parent is not None:
                os.close(parent)


@dataclass(frozen=True, slots=True)
class ValidatedArtifactInput:
    blobs: BlobSet
    policy_id: str
    project_digest: str
    execution_class: str
    _issuer: object


class ArtifactValidationAuthority:
    """Host-owned validators. Registration is a local trusted API, never an RPC.

    A fixed checker parses inert bytes for the declared artifact format/identity.
    It must not execute the artifact, extract uncontrolled paths, or delegate its
    result to candidate-produced reports. This capability is not a regression
    verdict and does not authorize signing or mobile installation by itself.
    """
    def __init__(self):
        self._issuer = object()
        self._policies = {}
        self._lock = threading.RLock()

    def register(self, policy_id, *, paths, max_bytes, checker):
        try:
            validate_id(policy_id)
            require(type(paths) in (list, tuple) and 0 < len(paths) <= 64, "Invalid artifact paths")
            for path in paths:
                safe_relative_path(path)
            require(len(set(path.casefold() for path in paths)) == len(paths), "Duplicate artifact paths")
            bounded_int(max_bytes, "artifact policy limit", 1, MAX_TRANSFER_BYTES)
            require(callable(checker), "Fixed artifact checker required")
            policy = (tuple(sorted(paths)), max_bytes, checker)
            with self._lock:
                old = self._policies.get(policy_id)
                require(old is None or (old[:2] == policy[:2] and old[2] is checker), "Artifact policy conflict")
                self._policies[policy_id] = policy
        except (ContractError, TypeError):
            raise ArtifactError("Artifact policy rejected") from None

    def validate(self, blobs, *, policy_id, project_digest, execution_class):
        try:
            validate_digest(project_digest)
            require(execution_class in ("desktop-guest", "mobile-device"), "Invalid artifact destination")
            require(type(blobs) is BlobSet, "Immutable artifact bytes required")
            with self._lock:
                policy = self._policies.get(policy_id)
            require(policy is not None, "Artifact policy unavailable")
            require(tuple(path for path, _ in blobs.entries) == policy[0]
                    and sum(len(data) for _, data in blobs.entries) <= policy[1], "Artifact policy mismatch")
            try:
                accepted = policy[2](blobs)
            except Exception:
                # Format libraries have different exception hierarchies. Peer
                # bytes must never escape as a parser traceback or capability.
                raise ArtifactError("Artifact format rejected") from None
            require(accepted is True, "Artifact validation failed")
            return ValidatedArtifactInput(blobs, policy_id, project_digest, execution_class, self._issuer)
        except (ContractError, TypeError, ValueError, OSError):
            raise ArtifactError("Artifact validation rejected") from None

    def require_input(self, receipt, *, input_digest, project_digest, execution_class):
        if (type(receipt) is not ValidatedArtifactInput or receipt._issuer is not self._issuer
                or receipt.project_digest != project_digest or receipt.execution_class != execution_class
                or receipt.blobs.digest != input_digest):
            raise ArtifactError("Validated artifact capability required")
        return receipt.blobs


def send_blobs(channel, blobs, *, prefix):
    if prefix not in ("input", "artifact") or type(blobs) is not BlobSet:
        raise ArtifactError("Invalid transfer")
    channel.send(prefix + "-start", blobs.manifest)
    for index, (_, data) in enumerate(blobs.entries):
        for offset in range(0, len(data), CHUNK_BYTES):
            channel.send(prefix + "-chunk", {"index": index, "offset": offset,
                         "data": base64.b64encode(data[offset:offset + CHUNK_BYTES]).decode()})
    channel.send(prefix + "-end", {})


def receive_blobs(channel, *, prefix, expected_digest=None, max_bytes=MAX_TRANSFER_BYTES,
                  allowed_paths=None, first_message=None):
    try:
        require(prefix in ("input", "artifact"), "Invalid transfer")
        kind, manifest = first_message if first_message is not None else channel.receive()
        require(kind == prefix + "-start", "Transfer start required")
        validate_manifest(manifest)
        require(manifest["digest"] == digest(manifest["files"]), "Manifest digest mismatch")
        require(expected_digest is None or manifest["digest"] == expected_digest, "Input mismatch")
        require(sum(item["size"] for item in manifest["files"]) <= max_bytes, "Output limit exceeded")
        if allowed_paths is not None:
            require(set(item["path"] for item in manifest["files"]) == set(allowed_paths),
                    "Output policy mismatch")
        entries = []
        for index, item in enumerate(manifest["files"]):
            data = bytearray()
            while len(data) < item["size"]:
                kind, chunk = channel.receive()
                require(kind == prefix + "-chunk" and chunk["index"] == index
                        and chunk["offset"] == len(data), "Transfer order mismatch")
                raw = decode_chunk(chunk["data"])
                require(len(data) + len(raw) <= item["size"], "Transfer size mismatch")
                data.extend(raw)
            require(hashlib.sha256(data).hexdigest() == item["digest"], "File digest mismatch")
            entries.append((item["path"], bytes(data)))
        require(channel.receive() == (prefix + "-end", {}), "Transfer end required")
        blobs = BlobSet(tuple(entries))
        require(blobs.digest == manifest["digest"], "Canonical manifest required")
        return blobs
    except (ContractError, ProtocolError, KeyError, TypeError, StopIteration):
        raise ArtifactError("Transfer rejected") from None
