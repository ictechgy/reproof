"""Explicit local VM resources and trusted recipe registration.

Provisioning seals a supplied, already installed VM. It does not download or
install macOS, infer an image, or qualify containment from resource metadata.
"""
from __future__ import annotations

import copy
import ctypes
import hashlib
import os
from pathlib import Path
import platform
import stat

from reproof.core import ContractError
from reproof.contracts.versions import (
    bounded_int, bounded_list, bounded_text, digest, exact, require,
    validate_digest, validate_id,
)
from .artifacts import ArtifactError, open_regular, read_regular
from .protocol import (GUEST_CONTROLS, MAX_RESOURCE_BYTES, validate_environment_descriptor,
                       validate_guest_image_manifest, validate_toolchain_manifest)
from .wire import MAX_TRANSFER_BYTES, ProtocolError, canonical, decode_json, safe_transfer_path

RESOURCE_FILES = {"disk": "disk.img", "auxiliary": "auxiliary.bin", "hardware": "hardware.bin",
                  "machine": "machine.bin", "toolchain": "toolchain.img", "helper": "vm-helper"}
LIMITS = {"disk": MAX_RESOURCE_BYTES, "toolchain": MAX_RESOURCE_BYTES,
          "auxiliary": 128 * 1024 ** 2, "hardware": 1024 ** 2, "machine": 1024 ** 2,
          "helper": 32 * 1024 ** 2}


class ResourceError(RuntimeError):
    pass


def validate_catalog(value):
    try:
        recipes = bounded_list(value, "guest recipes", 64, minimum=1)
        ids = set()
        for recipe in recipes:
            exact(recipe, ("id", "executionClass", "argv", "artifactPolicyId", "cleanupPolicyId",
                           "outputPaths", "maxOutputBytes", "timeoutMs"))
            validate_id(recipe["id"])
            require(recipe["id"] not in ids, "Duplicate recipe")
            ids.add(recipe["id"])
            require(recipe["executionClass"] in ("build-guest", "host-build", "desktop-guest"),
                    "Invalid recipe class")
            validate_id(recipe["artifactPolicyId"])
            validate_id(recipe["cleanupPolicyId"])
            argv = bounded_list(recipe["argv"], "recipe arguments", 64, minimum=1)
            for argument in argv:
                bounded_text(argument, "recipe argument", 4096)
            require(argv[0].startswith("/") and ".." not in argv[0].split("/"), "Absolute guest tool required")
            paths = bounded_list(recipe["outputPaths"], "recipe outputs", 64, minimum=1)
            for path in paths:
                safe_transfer_path(path)
            require(len(set(path.casefold() for path in paths)) == len(paths), "Duplicate outputs")
            bounded_int(recipe["maxOutputBytes"], "output byte limit", 1, MAX_TRANSFER_BYTES)
            bounded_int(recipe["timeoutMs"], "recipe deadline", 1000, 24 * 60 * 60 * 1000)
        return copy.deepcopy(value)
    except (ContractError, TypeError):
        raise ResourceError("Guest recipe catalog rejected") from None


def _metadata(value):
    exact(value, ("schemaVersion", "environment", "guestImage", "toolchain", "agentDigest", "catalog"))
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1, "Invalid bundle version")
    env = validate_environment_descriptor(value["environment"])
    require(env["executionClass"] in ("build-guest", "desktop-guest"), "VM environment required")
    require(env["architecture"] == "arm64" and set(env["controls"]) == GUEST_CONTROLS,
            "Unsupported concrete macOS VM controls")
    image = validate_guest_image_manifest(value["guestImage"])
    toolchain = validate_toolchain_manifest(value["toolchain"])
    require(env["architecture"] == image["architecture"] == toolchain["architecture"]
            and env["guestImageId"] == image["id"] and env["toolchainId"] == toolchain["id"],
            "Resource metadata mismatch")
    validate_digest(value["agentDigest"])
    recipes = validate_catalog(value["catalog"])
    require(any(recipe["executionClass"] == env["executionClass"] for recipe in recipes), "Missing recipe")
    require(all(recipe["timeoutMs"] <= env["resources"]["timeoutMs"] for recipe in recipes),
            "Recipe exceeds environment deadline")
    return copy.deepcopy(value)


def _absolute_fd(path):
    path = Path(path).absolute()
    return open_regular(Path(path.anchor), path.as_posix().lstrip("/"))


def _hash_or_copy(path, *, maximum, destination=None):
    with os.fdopen(_absolute_fd(path), "rb") as source:
        before = os.fstat(source.fileno())
        if not 0 < before.st_size <= maximum:
            raise ResourceError("VM resource size rejected")
        target = None
        cloned = False
        if destination is not None and platform.system() == "Darwin":
            # fclonefileat pins the source descriptor, avoiding a source-path race.
            library = ctypes.CDLL(None, use_errno=True)
            clone = getattr(library, "fclonefileat", None)
            if clone is not None:
                directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
                    clone.restype = ctypes.c_int
                    cloned = clone(source.fileno(), directory, os.fsencode(destination.name), 0) == 0
                finally:
                    os.close(directory)
        if destination is not None and not cloned:
            target = destination.open("xb")
        hasher = hashlib.sha256()
        count = 0
        try:
            while raw := source.read(min(1024 ** 2, before.st_size - count + 1)):
                count += len(raw)
                if count > before.st_size:
                    raise ResourceError("VM resource changed")
                hasher.update(raw)
                if target is not None:
                    target.write(raw)
            after = os.fstat(source.fileno())
            if count != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ResourceError("VM resource changed")
            if target is not None:
                target.flush()
                os.fsync(target.fileno())
            if cloned:
                fd = _absolute_fd(destination)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return {"digest": hasher.hexdigest(), "size": count}
        finally:
            if target is not None:
                target.close()


def provision(output, *, metadata, resources):
    """Trusted administrator operation; resource arguments are never candidate input."""
    try:
        metadata = _metadata(metadata)
        require(type(resources) is dict and set(resources) == set(RESOURCE_FILES), "Resource set mismatch")
        # Check all path boundaries before opening any large resource or output.
        for key, path in resources.items():
            fd = _absolute_fd(path)
            os.close(fd)
        output = Path(output).absolute()
        output.mkdir(mode=0o700, exist_ok=False)
        infos = {}
        for key, filename in RESOURCE_FILES.items():
            target = output / filename
            infos[key] = _hash_or_copy(resources[key], maximum=LIMITS[key], destination=target)
            target.chmod(0o500 if key == "helper" else 0o400)
        _check_resource_bindings(metadata, infos)
        definition = {"metadata": metadata, "resources": infos}
        record = {"schemaVersion": 1, "environmentDigest": digest(definition), **definition}
        with (output / "manifest.json").open("xb") as stream:
            stream.write(canonical(record))
            stream.flush()
            os.fsync(stream.fileno())
        (output / "manifest.json").chmod(0o400)
        fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return GuestBundle(output, record)
    except (OSError, ArtifactError, ContractError, ProtocolError):
        raise ResourceError("VM provisioning rejected") from None


def _check_resource_bindings(metadata, infos):
    require(type(infos) is dict and set(infos) == set(RESOURCE_FILES), "Resource set mismatch")
    for key, info in infos.items():
        exact(info, ("digest", "size"))
        validate_digest(info["digest"])
        bounded_int(info["size"], "resource size", 1, LIMITS[key])
    for key, field in (("disk", "guestImage"), ("toolchain", "toolchain")):
        require(infos[key] == {"digest": metadata[field]["artifactDigest"],
                               "size": metadata[field]["sizeBytes"]}, "Resource digest mismatch")
    require(infos["disk"]["size"] + infos["auxiliary"]["size"]
            <= metadata["environment"]["resources"]["diskBytes"], "Overlay disk limit exceeded")


class GuestBundle:
    isolation = "guest-vm"

    def __init__(self, root, record):
        self.root = Path(root)
        self._record = copy.deepcopy(record)
        self.environment_digest = record["environmentDigest"]

    @property
    def metadata(self):
        return copy.deepcopy(self._record["metadata"])

    @property
    def machine_digest(self):
        return digest({key: self._record["resources"][key] for key in ("hardware", "machine")})

    @property
    def overlay_bytes(self):
        return sum(self._record["resources"][key]["size"] for key in ("disk", "auxiliary"))

    def path(self, key):
        return self.root / RESOURCE_FILES[key]

    def recipe(self, recipe_id):
        for recipe in self.metadata["catalog"]:
            if recipe["id"] == recipe_id:
                return recipe
        raise ResourceError("Unregistered guest recipe")

    @classmethod
    def load(cls, root):
        try:
            root = Path(root).absolute()
            info = root.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077, "Private bundle required")
            record = decode_json(read_regular(root, "manifest.json", maximum=256 * 1024))
            exact(record, ("schemaVersion", "environmentDigest", "metadata", "resources"))
            require(type(record["schemaVersion"]) is int and record["schemaVersion"] == 1,
                    "Invalid bundle version")
            metadata = _metadata(record["metadata"])
            _check_resource_bindings(metadata, record["resources"])
            require(record["environmentDigest"] == digest({"metadata": metadata, "resources": record["resources"]}),
                    "Bundle digest mismatch")
            bundle = cls(root, record)
            bundle.verify()
            return bundle
        except (OSError, ArtifactError, ContractError, ProtocolError):
            raise ResourceError("VM resource bundle rejected") from None

    def verify(self):
        try:
            for key in RESOURCE_FILES:
                if _hash_or_copy(self.path(key), maximum=LIMITS[key]) != self._record["resources"][key]:
                    raise ResourceError("VM resource digest changed")
        except (OSError, ArtifactError):
            raise ResourceError("VM resource verification failed") from None

    def create_overlays(self, directory):
        try:
            for key in ("disk", "auxiliary"):
                target = Path(directory) / RESOURCE_FILES[key]
                info = _hash_or_copy(self.path(key), maximum=LIMITS[key], destination=target)
                if info != self._record["resources"][key]:
                    raise ResourceError("VM base changed during clone")
                target.chmod(0o600)
        except (OSError, ArtifactError):
            raise ResourceError("VM overlay creation failed") from None


HOST_PROBE_RECIPE = "host-qualification-probe"


def _host_tool_path(value):
    bounded_text(value, "host tool path", 1024)
    require(value.startswith("/") and ".." not in value.split("/"), "Absolute host tool required")
    return value


def _host_metadata(value):
    exact(value, ("schemaVersion", "environment", "tools", "catalog"))
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1, "Invalid bundle version")
    env = validate_environment_descriptor(value["environment"])
    require(env["executionClass"] == "host-build", "Host build environment required")
    tools = bounded_list(value["tools"], "host tools", 64, minimum=1)
    ids = set()
    for tool in tools:
        exact(tool, ("id", "version", "path", "artifactDigest", "sizeBytes"))
        validate_id(tool["id"], "tool id")
        require(tool["id"] not in ids, "Duplicate tool")
        ids.add(tool["id"])
        _version_label = tool["version"]
        bounded_text(_version_label, "tool version", 80)
        _host_tool_path(tool["path"])
        validate_digest(tool["artifactDigest"], "tool digest")
        bounded_int(tool["sizeBytes"], "tool size", 1, MAX_RESOURCE_BYTES)
    recipes = validate_catalog(value["catalog"])
    tool_paths = {tool["path"] for tool in tools}
    for recipe in recipes:
        require(recipe["executionClass"] == "host-build"
                and recipe["argv"][0] in tool_paths, "Host recipe must run a pinned host tool")
        require(recipe["timeoutMs"] <= env["resources"]["timeoutMs"],
                "Recipe exceeds environment deadline")
    require(any(recipe["id"] == HOST_PROBE_RECIPE for recipe in recipes),
            "Qualification probe recipe missing")
    return copy.deepcopy(value)


def _check_host_bindings(metadata, infos):
    require(type(infos) is dict, "Resource set mismatch")
    tools = {tool["id"]: tool for tool in metadata["tools"]}
    require(set(infos) == set(tools), "Resource set mismatch")
    for tool_id, info in infos.items():
        exact(info, ("digest", "size"))
        validate_digest(info["digest"])
        bounded_int(info["size"], "resource size", 1, MAX_RESOURCE_BYTES)
        tool = tools[tool_id]
        require(info == {"digest": tool["artifactDigest"], "size": tool["sizeBytes"]},
                "Resource digest mismatch")


def provision_host(output, *, metadata):
    """Seal a host-build bundle; tools stay at their declared host paths.

    The manifest pins every tool by digest and size. Provisioning measures the
    live host tools; it does not copy them into the bundle or qualify anything.
    """
    try:
        metadata = _host_metadata(metadata)
        infos = {}
        for tool in metadata["tools"]:
            infos[tool["id"]] = _hash_or_copy(tool["path"], maximum=MAX_RESOURCE_BYTES)
        _check_host_bindings(metadata, infos)
        output = Path(output).absolute()
        output.mkdir(mode=0o700, exist_ok=False)
        definition = {"metadata": metadata, "resources": infos}
        record = {"schemaVersion": 1, "environmentDigest": digest(definition), **definition}
        with (output / "manifest.json").open("xb") as stream:
            stream.write(canonical(record))
            stream.flush()
            os.fsync(stream.fileno())
        (output / "manifest.json").chmod(0o400)
        fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        bundle = HostBuildBundle(output, record)
        bundle.verify()  # shebang 인터프리터 포함, 봉인 시점에도 도구를 검증한다.
        return bundle
    except (OSError, ArtifactError, ContractError, ProtocolError):
        raise ResourceError("Host build provisioning rejected") from None


class HostBuildBundle:
    """A digest-pinned host toolchain plus fixed recipes; no isolation is implied."""

    isolation = "host"

    def __init__(self, root, record):
        self.root = Path(root)
        self._record = copy.deepcopy(record)
        self.environment_digest = record["environmentDigest"]

    @property
    def metadata(self):
        return copy.deepcopy(self._record["metadata"])

    @property
    def machine_digest(self):
        # 실행 scope는 환경 ID에 묶는다: 도구 digest로 묶으면 toolchain 교체 시
        # 다른 lease 키가 되어 진행 중인 scope의 격리 표시를 우회할 수 있다.
        return digest({"kind": "host-build-scope",
                       "environmentId": self._record["metadata"]["environment"]["id"]})

    @property
    def overlay_bytes(self):
        return self._record["metadata"]["environment"]["resources"]["diskBytes"]

    @property
    def tool_paths(self):
        return {tool["path"] for tool in self._record["metadata"]["tools"]}

    def recipe(self, recipe_id):
        for recipe in self.metadata["catalog"]:
            if recipe["id"] == recipe_id:
                return recipe
        raise ResourceError("Unregistered host recipe")

    @classmethod
    def load(cls, root):
        try:
            root = Path(root).absolute()
            info = root.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077, "Private bundle required")
            record = decode_json(read_regular(root, "manifest.json", maximum=256 * 1024))
            exact(record, ("schemaVersion", "environmentDigest", "metadata", "resources"))
            require(type(record["schemaVersion"]) is int and record["schemaVersion"] == 1,
                    "Invalid bundle version")
            metadata = _host_metadata(record["metadata"])
            _check_host_bindings(metadata, record["resources"])
            require(record["environmentDigest"] == digest({"metadata": metadata, "resources": record["resources"]}),
                    "Bundle digest mismatch")
            bundle = cls(root, record)
            bundle.verify()
            return bundle
        except (OSError, ArtifactError, ContractError, ProtocolError):
            raise ResourceError("Host build bundle rejected") from None

    def verify(self):
        try:
            tools = {tool["id"]: tool for tool in self._record["metadata"]["tools"]}
            pinned_paths = {tool["path"] for tool in tools.values()}
            for tool_id, info in self._record["resources"].items():
                tool = tools[tool_id]
                path = Path(tool["path"])
                detail = path.lstat()
                require(stat.S_ISREG(detail.st_mode) and detail.st_uid in (os.geteuid(), 0)
                        and detail.st_mode & 0o111 and not detail.st_mode & 0o022,
                        "Host tool permissions rejected")
                if _hash_or_copy(path, maximum=MAX_RESOURCE_BYTES) != info:
                    raise ResourceError("Host tool digest changed")
                # 스크립트 도구는 커널이 shebang 인터프리터를 실행한다 —
                # 그 인터프리터도 선언된 고정 도구여야 진짜로 "toolchain-pinned"다.
                with path.open("rb") as stream:
                    head = stream.readline(512)
                if head.startswith(b"#!"):
                    interpreter = head[2:].decode("utf-8", "strict").strip().split()[0]
                    require(interpreter.startswith("/") and interpreter in pinned_paths,
                            "Script interpreter must be a pinned host tool")
        except (OSError, UnicodeDecodeError, IndexError, ArtifactError, ContractError):
            raise ResourceError("Host tool verification failed") from None
