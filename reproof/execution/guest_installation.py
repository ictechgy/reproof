"""Hash-bound guest agent package, independent of an installed macOS image."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from reproloop.contracts.versions import bounded_int, digest, exact, require
from reproloop.core import ContractError
from .artifacts import ArtifactError, BlobSet, read_regular
from .resources import ResourceError, validate_catalog
from .wire import ProtocolError, canonical, decode_json

AGENT_FILES = (
    "reproloop/__init__.py", "reproloop/core.py", "reproloop/contracts/__init__.py",
    "reproloop/contracts/versions.py", "reproloop/contracts/project.py", "reproloop/contracts/evidence.py",
    "reproloop/contracts/scenario.py", "reproloop/contracts/observation.py", "reproloop/contracts/execution.py",
    "reproloop/execution/__init__.py", "reproloop/execution/backend.py", "reproloop/execution/protocol.py",
    "reproloop/execution/wire.py", "reproloop/execution/artifacts.py", "reproloop/execution/resources.py",
    "reproloop/execution/guest.py", "reproloop/execution/guest_probe.py",
    "reproloop/execution/guest_installation.py",
)


class InstallationError(RuntimeError):
    pass


def _policy(value):
    exact(value, ("schemaVersion", "uid", "gid", "catalog", "files", "agentDigest"))
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1, "Invalid guest version")
    bounded_int(value["uid"], "guest UID", 501, 60000)
    bounded_int(value["gid"], "guest GID", 1, 60000)
    validate_catalog(value["catalog"])
    require(type(value["files"]) is list, "Guest files required")
    expected = {*AGENT_FILES, "main.py", "probe.py", "guest-connect", "guest-run", "io.reproloop.guest.plist"}
    require({item["path"] for item in value["files"]} == expected
            and len(value["files"]) == len(expected), "Guest file set mismatch")
    require(value["agentDigest"] == digest({key: item for key, item in value.items() if key != "agentDigest"}),
            "Guest policy digest mismatch")
    return value


def package_agent(output, *, source_root, native_root, catalog, uid, gid):
    try:
        catalog = validate_catalog(catalog)
        entries = [(name, read_regular(source_root, name, maximum=2 * 1024 ** 2)) for name in AGENT_FILES]
        for name in ("main.py", "probe.py", "io.reproloop.guest.plist"):
            entries.append((name, read_regular(source_root, "guest/reproloop_agent/" + name, maximum=2 * 1024 ** 2)))
        for name in ("guest-connect", "guest-run"):
            entries.append((name, read_regular(native_root, name, maximum=8 * 1024 ** 2)))
        blobs = BlobSet(tuple(entries))
        definition = {"schemaVersion": 1, "uid": uid, "gid": gid, "catalog": catalog,
                      "files": blobs.manifest["files"]}
        policy = {**definition, "agentDigest": digest(definition)}
        _policy(policy)
        output = Path(output)
        blobs.write_new(output)
        with (output / "policy.json").open("xb") as stream:
            stream.write(canonical(policy))
            stream.flush()
            os.fsync(stream.fileno())
        for name in ("guest-connect", "guest-run"):
            (output / name).chmod(0o500)
        (output / "policy.json").chmod(0o400)
        return policy
    except (ArtifactError, ResourceError, ProtocolError, ContractError, OSError, TypeError, KeyError):
        raise InstallationError("Guest package rejected") from None


def verify_package(root, *, root_owned=False):
    try:
        root = Path(root)
        policy = _policy(decode_json(read_regular(root, "policy.json", maximum=256 * 1024)))
        if root_owned:
            paths = {root, root / "policy.json"}
            for item in policy["files"]:
                path = root / item["path"]
                paths.add(path)
                while path.parent != root:
                    path = path.parent
                    paths.add(path)
            for path in paths:
                info = path.lstat()
                require(info.st_uid == 0 and not info.st_mode & 0o022
                        and (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)),
                        "Guest package ownership mismatch")
        actual = BlobSet.from_directory(root, [item["path"] for item in policy["files"]], max_bytes=16 * 1024 ** 2)
        require(actual.manifest["files"] == policy["files"], "Guest file digest mismatch")
        return policy
    except (ArtifactError, ResourceError, ProtocolError, ContractError, OSError, TypeError, KeyError):
        raise InstallationError("Guest installation rejected") from None
