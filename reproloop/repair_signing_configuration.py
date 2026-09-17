"""Explicit public configuration for the fixed Android signing owner.

This file format selects pinned tools and an existing operation journal.  It
contains no signing material, callbacks, commands or qualification receipts.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from . import contracts
from .contracts.versions import exact, require
from .android_signing_tools import load_android_signing_owner
from .execution.artifacts import open_regular
from .execution.journal import RunDenied, RunStore
from .execution.wire import decode_json, safe_transfer_path
from .repair_android_signing import AndroidSigningIdentity
from .repair_signing_recovery import (
    MIN_OPERATION_BYTES, SigningOperationStore, SigningOwnerTools,
)


def _path(value):
    require(type(value) is str and value.startswith("/"),
                      "Explicit absolute signing configuration path required")
    safe_transfer_path(value[1:])
    selected = Path(value)
    require(str(selected) == value, "Signing configuration path rejected")
    return selected


def _read_configuration(path):
    selected = _path(str(path))
    require(selected.suffix == ".json", "Public JSON configuration required")
    descriptor = open_regular(selected.parent, selected.name)
    with os.fdopen(descriptor, "rb") as incoming:
        before = os.fstat(incoming.fileno())
        require(before.st_uid == os.getuid() and before.st_nlink == 1
            and not (before.st_mode & (stat.S_IWGRP | stat.S_IWOTH
                                       | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX))
            and 0 < before.st_size <= 64 * 1024, "Signing configuration file rejected")
        raw = incoming.read(64 * 1024 + 1)
        after = os.fstat(incoming.fileno())
        require(len(raw) == before.st_size
            and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "Signing configuration changed while reading")
    return decode_json(raw)


@dataclass(frozen=True, slots=True)
class AndroidSigningConfiguration:
    run_store_path: Path
    environment_digest: str
    disk_budget_bytes: int
    owner_root: Path
    tools: SigningOwnerTools
    identity: AndroidSigningIdentity
    definition_digest: str

    @property
    def scope_digest(self):
        return contracts.digest({"kind": "android-apk-signing",
                                  "certificateSha256": self.identity.certificate_sha256})

    def open_existing(self):
        """Open a previously admitted scope without creating replacement state."""
        store = RunStore(self.run_store_path, environment_digest=self.environment_digest,
                         disk_limit=self.disk_budget_bytes, create=False)
        require(store._load().get("scope") == {
            "kind": "signing", "scopeDigest": self.scope_digest},
            "Existing signing journal scope required")
        operations = SigningOperationStore(store, self.scope_digest, self.tools,
                                           self.identity, self.owner_root, create=False)
        if operations.definition_digest != self.definition_digest:
            operations.close()
            raise RunDenied("Signing owner configuration does not match the journal")
        return operations


def load_android_signing_configuration(path):
    """Read one administrator-selected public JSON document; run no tool."""
    value = _read_configuration(path)
    exact(value, ("schemaVersion", "kind", "runStorePath", "environmentDigest",
        "diskBudgetBytes", "ownerRoot", "toolsPath", "toolsManifestSha256",
        "definitionDigest", "identity"))
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1
                      and value["kind"] == "android-signing-owner-v1",
                      "Signing configuration version rejected")
    paths = {name: _path(value[name]) for name in ("runStorePath", "ownerRoot", "toolsPath")}
    for name in ("environmentDigest", "toolsManifestSha256", "definitionDigest"):
        contracts.validate_digest(value[name])
    contracts.bounded_int(value["diskBudgetBytes"], "signing disk budget",
                          MIN_OPERATION_BYTES, 512 * 1024 ** 3)
    identity = value["identity"]
    exact(identity, ("referenceId", "applicationId", "packageName",
                               "certificateSha256", "signatureSchemes", "permissions"))
    require(type(identity["signatureSchemes"]) is list
                      and type(identity["permissions"]) is list,
                      "Signing identity lists required")
    selected = AndroidSigningIdentity(identity["referenceId"], identity["applicationId"],
        identity["packageName"], identity["certificateSha256"],
        tuple(identity["signatureSchemes"]), tuple(identity["permissions"]))
    tools = load_android_signing_owner(paths["toolsPath"], value["toolsManifestSha256"])
    return AndroidSigningConfiguration(paths["runStorePath"], value["environmentDigest"],
        value["diskBudgetBytes"], paths["ownerRoot"], tools, selected, value["definitionDigest"])
