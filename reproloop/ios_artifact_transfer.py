"""Bounded, inert iOS ``.app``/``.ipa`` artifact inputs.

This module only parses and fingerprints an application bundle.  It never
executes a Mach-O image, invokes Apple's signing tools, reads a host keychain,
or interprets a provisioning profile.  The returned capability is issued by
this process and intentionally exposes no embedded provisioning-profile data.
"""
from __future__ import annotations

from contextlib import contextmanager

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import posixpath
import re
import stat
import struct
import tempfile
import threading
import unicodedata
import weakref
import zipfile

from .core import ContractError, digest, require


SCHEMA_VERSION = 1
KIND = "ios-bundle-v1"

# These are dedicated iOS input limits.  The generic execution BlobSet and
# wire limits remain 64 MiB; changing them would widen unrelated transfers.
MAX_EXPANDED_APP_BYTES = 512 * 1024 * 1024
MAX_APP_ENTRIES = 100_000
MAX_CODE_OBJECTS = 512
MAX_SYMLINK_DEPTH = 16
MAX_PATH_BYTES = 1024
MAX_PLIST_BYTES = 4 * 1024 * 1024
MAX_LOAD_COMMAND_BYTES = 8 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 1000
MAX_ZIP_CENTRAL_BYTES = 64 * 1024 * 1024

_BUNDLE = re.compile(r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_EXECUTABLE = re.compile(r"[A-Za-z0-9._-]{1,255}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MOBILE_PROFILE = "embedded.mobileprovision"
_CODE_DIRECTORY_SUFFIXES = frozenset({".app", ".appex", ".framework", ".xctest", ".xpc"})
_CODE_FILE_SUFFIXES = frozenset({".dylib", ".so"})
_DSYM_SUFFIX = ".dsym"

# CPU_TYPE_* values from mach/machine.h.  The parser only uses these to bound
# a file-format check; it never loads or executes the image.
_MACHO_CPU_TYPES = frozenset({
    7,                  # x86
    12,                 # arm
    0x01000007,         # x86_64
    0x0100000C,         # arm64 / arm64e subtype
})
_MACHO_FILE_TYPES = frozenset({2, 6, 7, 8})  # execute, dylib, dynamic linker, bundle
_MACHO_BUNDLE_FILE_TYPE = 8
_MACHO_DSYM_FILE_TYPE = 10
_LC_CODE_SIGNATURE = 0x1D
_LC_UUID = 0x1B

_ISSUER = object()
_ISSUED_CAPABILITIES: dict[int, weakref.ReferenceType] = {}
_ISSUED_CAPABILITIES_LOCK = threading.RLock()


def _reject(message="iOS artifact boundary rejected"):
    raise ContractError(message)


def _bounded_limits(max_bytes, max_entries, max_code_objects):
    require(type(max_bytes) is int and 1 <= max_bytes <= MAX_EXPANDED_APP_BYTES,
            "Invalid iOS artifact byte limit")
    require(type(max_entries) is int and 1 <= max_entries <= MAX_APP_ENTRIES,
            "Invalid iOS artifact entry limit")
    require(type(max_code_objects) is int and 1 <= max_code_objects <= MAX_CODE_OBJECTS,
            "Invalid iOS artifact code-object limit")
    return max_bytes, max_entries, max_code_objects


def _check_component(value):
    require(type(value) is str and 0 < len(value) <= MAX_PATH_BYTES,
            "Invalid iOS artifact path")
    require("/" not in value and "\\" not in value and "\x00" not in value
            and value not in {".", ".."}
            and unicodedata.normalize("NFC", value) == value
            and not any(ord(character) < 32 or ord(character) == 127
                        for character in value),
            "Invalid iOS artifact path")


def _check_relative(value, *, allow_empty=False):
    require(type(value) is str and len(value.encode("utf-8")) <= MAX_PATH_BYTES,
            "Invalid iOS artifact path")
    if allow_empty and value == "":
        return value
    require(value and not value.startswith("/") and "\\" not in value
            and "\x00" not in value and unicodedata.normalize("NFC", value) == value,
            "Invalid iOS artifact path")
    parts = value.split("/")
    require(all(part not in {"", ".", ".."} for part in parts),
            "Invalid iOS artifact path")
    for part in parts:
        _check_component(part)
    require(str(PurePosixPath(value)) == value,
            "Invalid iOS artifact path")
    return value


def _check_link_target(value):
    require(type(value) is str and 0 < len(value.encode("utf-8")) <= MAX_PATH_BYTES
            and "\\" not in value and "\x00" not in value
            and not value.startswith("/"), "Invalid iOS symlink target")
    require(unicodedata.normalize("NFC", value) == value
            and not any(ord(character) < 32 or ord(character) == 127
                        for character in value),
            "Invalid iOS symlink target")
    parts = value.split("/")
    require(all(part != "" for part in parts), "Invalid iOS symlink target")
    for part in parts:
        require(part not in {"."}, "Invalid iOS symlink target")
        if part != "..":
            _check_component(part)
    return value


def _path_has_symlink_component(path: Path) -> bool:
    current = path
    while current != Path(current.anchor):
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        current = current.parent
    return False


def _owned_directory(path: Path) -> tuple[int, int, int, int, int, int, int]:
    try:
        info = path.lstat()
    except OSError:
        _reject("iOS artifact directory is unavailable")
    require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),
            "iOS artifact directory is invalid")
    require(info.st_uid == os.getuid()
            and not (info.st_mode & (stat.S_IWGRP | stat.S_IWOTH
                                     | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)),
            "iOS artifact directory ownership is invalid")
    return _file_stat_signature(info)


def _file_stat_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink, info.st_mode & 0o7777)


_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_NONBLOCK
_FILE_FLAGS = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK


def _open_directory_path(path: Path):
    """Open every absolute directory component with O_NOFOLLOW."""
    path = Path(path).absolute()
    descriptor = None
    try:
        descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        require(stat.S_ISDIR(info.st_mode), "iOS artifact directory boundary rejected")
        result, descriptor = descriptor, None
        return result
    except (OSError, ValueError):
        _reject("iOS artifact directory boundary rejected")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_relative_file(root: Path, relative: str, *, expected_signature=None):
    """Open a regular file after pinning every parent directory component."""
    _check_relative(relative)
    directory = None
    descriptor = None
    try:
        directory = _open_directory_path(root)
        parts = relative.split("/")
        for component in parts[:-1]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory)
            info = os.fstat(child)
            require(stat.S_ISDIR(info.st_mode), "iOS artifact directory boundary rejected")
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=directory)
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode), "iOS artifact file boundary rejected")
        if expected_signature is not None:
            require(_file_stat_signature(info) == expected_signature,
                    "iOS artifact file changed while being read")
        result, descriptor = descriptor, None
        return result
    except (OSError, ValueError):
        _reject("iOS artifact file boundary rejected")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if directory is not None:
            try:
                os.close(directory)
            except OSError:
                pass


def _ensure_relative_directory(root: Path, relative: str):
    """Create/open a private relative directory with descriptor-walked parents."""
    if relative:
        _check_relative(relative)
    directory = None
    try:
        directory = os.dup(root) if type(root) is int else _open_directory_path(root)
        if relative:
            for component in relative.split("/"):
                child = None
                try:
                    child = os.open(component, _DIRECTORY_FLAGS,
                                    dir_fd=directory)
                except FileNotFoundError:
                    os.mkdir(component, mode=0o700, dir_fd=directory)
                    child = os.open(component, _DIRECTORY_FLAGS,
                                    dir_fd=directory)
                try:
                    require(stat.S_ISDIR(os.fstat(child).st_mode),
                            "iOS artifact extraction crossed a non-directory")
                except Exception:
                    if child is not None:
                        os.close(child)
                    raise
                os.close(directory)
                directory = child
        result, directory = directory, None
        return result
    except (OSError, ValueError):
        _reject("iOS artifact directory boundary rejected")
    finally:
        if directory is not None:
            try:
                os.close(directory)
            except OSError:
                pass


def _create_relative_file(root: Path, relative: str):
    """Create a regular file below a descriptor-walked private directory."""
    _check_relative(relative)
    parts = relative.split("/")
    parent = "/".join(parts[:-1])
    directory = _ensure_relative_directory(root, parent)
    descriptor = None
    try:
        descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | _O_NOFOLLOW | _O_NONBLOCK,
                             0o700, dir_fd=directory)
        require(stat.S_ISREG(os.fstat(descriptor).st_mode),
                "iOS artifact extraction did not create a regular file")
        result, descriptor = descriptor, None
        return result
    except (OSError, ValueError):
        _reject("iOS artifact file boundary rejected")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.close(directory)
        except OSError:
            pass


def _open_path_file(path: Path, *, expected_signature=None):
    path = Path(path).absolute()
    _check_component(path.name)
    directory = None
    descriptor = None
    try:
        directory = _open_directory_path(path.parent)
        descriptor = os.open(path.name, _FILE_FLAGS, dir_fd=directory)
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode), "iOS artifact file boundary rejected")
        if expected_signature is not None:
            require(_file_stat_signature(info) == expected_signature,
                    "iOS artifact file changed while being read")
        result, descriptor = descriptor, None
        return result
    except (OSError, ValueError):
        _reject("iOS artifact file boundary rejected")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if directory is not None:
            try:
                os.close(directory)
            except OSError:
                pass


def _hash_regular_fd(descriptor, *, max_bytes: int, expected_signature=None):
    """Read one already-pinned regular descriptor and verify its inode."""
    before = os.fstat(descriptor)
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and before.st_uid == os.getuid()
            and not (before.st_mode & (stat.S_IWGRP | stat.S_IWOTH
                                       | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX))
            and 0 <= before.st_size <= max_bytes,
            "iOS artifact file boundary rejected")
    if expected_signature is not None:
        require(_file_stat_signature(before) == expected_signature,
                "iOS artifact file changed while being read")
    hasher = hashlib.sha256()
    total = 0
    while True:
        block = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
        if not block:
            break
        total += len(block)
        require(total <= max_bytes, "iOS artifact byte limit exceeded")
        hasher.update(block)
    after = os.fstat(descriptor)
    require(total == before.st_size
            and _file_stat_signature(before) == _file_stat_signature(after),
            "iOS artifact file changed while being read")
    return total, hasher.hexdigest(), bool(before.st_mode & 0o111), _file_stat_signature(before)


def _regular_path(path: Path, *, max_bytes: int, expected_signature=None):
    """Read and hash one owned regular file after pinning its parent path."""
    descriptor = None
    try:
        descriptor = _open_path_file(path, expected_signature=expected_signature)
        return _hash_regular_fd(descriptor, max_bytes=max_bytes,
                                expected_signature=expected_signature)
    except (OSError, ValueError):
        _reject("iOS artifact file boundary rejected")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class _File:
    path: str
    size: int
    sha256: str
    executable: bool
    stat_signature: tuple[int, int, int, int, int, int, int] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Link:
    path: str
    target: str


@dataclass(frozen=True, slots=True)
class _Tree:
    root: Path = field(repr=False)
    root_signature: tuple[int, int, int, int, int, int, int] = field(repr=False)
    files: tuple[_File, ...]
    directories: tuple[str, ...]
    symlinks: tuple[_Link, ...]
    bytes: int

    @property
    def file_map(self):
        return {item.path: item for item in self.files}

    @property
    def directory_set(self):
        return frozenset(("", *self.directories))

    @property
    def link_map(self):
        return {item.path: item.target for item in self.symlinks}


def _scan_tree(root: Path, *, max_bytes: int, max_entries: int,
               root_signature=None) -> _Tree:
    files: list[_File] = []
    directories: list[str] = []
    symlinks: list[_Link] = []
    seen_casefold: set[str] = set()
    total = 0
    count = 0

    def process(entry, directory_fd: int, relative: str):
        nonlocal total, count
        _check_component(entry.name)
        path = entry.name if not relative else relative + "/" + entry.name
        _check_relative(path)
        folded = path.casefold()
        require(folded not in seen_casefold,
                "Case-colliding iOS artifact paths")
        seen_casefold.add(folded)
        count += 1
        require(count <= max_entries, "iOS artifact entry limit exceeded")
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            _reject("iOS artifact entry is unavailable")
        if stat.S_ISLNK(info.st_mode):
            require(info.st_uid == os.getuid(),
                    "iOS artifact symlink ownership is invalid")
            try:
                target = os.readlink(entry.name, dir_fd=directory_fd)
                after = os.stat(entry.name, dir_fd=directory_fd,
                                follow_symlinks=False)
            except OSError:
                _reject("iOS artifact symlink cannot be read")
            require(_file_stat_signature(after) == _file_stat_signature(info),
                    "iOS artifact symlink changed while being read")
            symlinks.append(_Link(path, _check_link_target(target)))
        elif stat.S_ISDIR(info.st_mode):
            require(info.st_uid == os.getuid()
                    and not (info.st_mode & (stat.S_IWGRP | stat.S_IWOTH
                                             | stat.S_ISUID | stat.S_ISGID
                                             | stat.S_ISVTX)),
                    "iOS artifact directory ownership is invalid")
            child_fd = None
            try:
                child_fd = os.open(entry.name, _DIRECTORY_FLAGS,
                                   dir_fd=directory_fd)
                child_info = os.fstat(child_fd)
                require(_file_stat_signature(child_info) ==
                        _file_stat_signature(info)
                        and stat.S_ISDIR(child_info.st_mode),
                        "iOS artifact directory changed while being read")
                directories.append(path)
                visit(child_fd, path)
            except OSError:
                _reject("iOS artifact directory cannot be opened")
            finally:
                if child_fd is not None:
                    try:
                        os.close(child_fd)
                    except OSError:
                        pass
        elif stat.S_ISREG(info.st_mode):
            descriptor = None
            try:
                descriptor = os.open(entry.name, _FILE_FLAGS,
                                     dir_fd=directory_fd)
                size, checksum, executable, signature = _hash_regular_fd(
                    descriptor, max_bytes=max_bytes - total,
                    expected_signature=_file_stat_signature(info))
            except OSError:
                _reject("iOS artifact file boundary rejected")
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            total += size
            require(total <= max_bytes, "iOS artifact byte limit exceeded")
            files.append(_File(path, size, checksum, executable, signature))
        else:
            _reject("Unexpected iOS artifact file type")

    def visit(directory_fd: int, relative: str):
        try:
            # Do not sort the iterator: sorting would materialize every entry
            # before the global entry budget can reject an oversized directory.
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    process(entry, directory_fd, relative)
        except OSError:
            _reject("iOS artifact directory cannot be read")
    root_fd = None
    try:
        root_fd = _open_directory_path(root)
        root_info = os.fstat(root_fd)
        require(stat.S_ISDIR(root_info.st_mode),
                "iOS artifact directory boundary rejected")
        if root_signature is not None:
            require(_file_stat_signature(root_info) == root_signature,
                    "iOS artifact directory changed while being read")
        actual_root_signature = _file_stat_signature(root_info)
        visit(root_fd, "")
    finally:
        if root_fd is not None:
            try:
                os.close(root_fd)
            except OSError:
                pass
    require(files, "Empty iOS application bundle")
    return _Tree(root, actual_root_signature,
                 tuple(sorted(files, key=lambda item: item.path)),
                 tuple(sorted(directories)), tuple(sorted(symlinks, key=lambda item: item.path)), total)


def _link_path(path: str, target: str) -> str:
    base = posixpath.dirname(path)
    joined = posixpath.normpath(posixpath.join(base, target))
    if joined == ".":
        return ""
    require(joined != ".." and not joined.startswith("../"),
            "iOS symlink escapes its application bundle")
    _check_relative(joined)
    return joined


def _resolve_entry(tree: _Tree, path: str):
    files = tree.file_map
    directories = tree.directory_set
    links = tree.link_map

    def resolve(candidate: str, stack: frozenset[str]):
        if candidate in files:
            return candidate, "file"
        if candidate in directories:
            return candidate, "directory"
        if candidate in links:
            require(candidate not in stack and len(stack) < MAX_SYMLINK_DEPTH,
                    "Cyclic or deeply nested iOS symlink")
            target = _link_path(candidate, links[candidate])
            return resolve(target, stack | {candidate})

        parts = candidate.split("/") if candidate else []
        for index in range(len(parts), 0, -1):
            prefix = "/".join(parts[:index])
            if prefix not in links:
                continue
            require(prefix not in stack and len(stack) < MAX_SYMLINK_DEPTH,
                    "Cyclic or deeply nested iOS symlink")
            target = _link_path(prefix, links[prefix])
            suffix = "/".join(parts[index:])
            replacement = target if not suffix else (target + "/" + suffix if target else suffix)
            return resolve(replacement, stack | {prefix})
        _reject("Dangling or external iOS symlink")

    return resolve(path, frozenset())


def _read_tree_file(tree: _Tree, logical_path: str, *, max_bytes: int = MAX_PLIST_BYTES):
    _check_relative(logical_path)
    resolved, kind = _resolve_entry(tree, logical_path)
    require(kind == "file", "iOS artifact property list is not a file")
    record = tree.file_map[resolved]
    require(record.size <= max_bytes, "iOS artifact property list is oversized")
    descriptor = None
    try:
        descriptor = _open_relative_file(
            tree.root, resolved, expected_signature=record.stat_signature)
        before = os.fstat(descriptor)
        require(_file_stat_signature(before) == record.stat_signature,
                "iOS artifact file changed while being read")
        raw = bytearray()
        while True:
            block = os.read(descriptor, min(1024 * 1024, max_bytes - len(raw) + 1))
            if not block:
                break
            raw.extend(block)
            require(len(raw) <= max_bytes, "iOS artifact property list is oversized")
        after = os.fstat(descriptor)
        require(len(raw) == record.size
                and hashlib.sha256(raw).hexdigest() == record.sha256
                and _file_stat_signature(after) == record.stat_signature,
                "iOS artifact file changed while being read")
        return bytes(raw)
    except OSError:
        _reject("iOS artifact file boundary rejected")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _plist(tree: _Tree, logical_path: str):
    try:
        value = plistlib.loads(_read_tree_file(tree, logical_path))
    except (ValueError, plistlib.InvalidFileException, TypeError):
        _reject("Invalid iOS application property list")
    require(type(value) is dict, "Invalid iOS application property list")
    return value


def _bundle_info(tree: _Tree, bundle_path: str):
    info_path = "Info.plist" if bundle_path == "." else bundle_path + "/Info.plist"
    info = _plist(tree, info_path)
    identifier = info.get("CFBundleIdentifier")
    executable = info.get("CFBundleExecutable")
    version = info.get("CFBundleShortVersionString")
    build = info.get("CFBundleVersion")
    require(type(identifier) is str and len(identifier) <= 180
            and _BUNDLE.fullmatch(identifier) is not None,
            "Invalid iOS application bundle identifier")
    require(type(executable) is str and _EXECUTABLE.fullmatch(executable) is not None,
            "Invalid iOS application executable name")
    require(type(version) is str and _VERSION.fullmatch(version) is not None
            and type(build) is str and _VERSION.fullmatch(build) is not None,
            "Invalid iOS application version")
    return identifier, executable, version, build


def _immediate_children(tree: _Tree, parent: str):
    prefix = "" if parent == "" else parent + "/"
    values = {}
    for path in (*tree.directories, *(item.path for item in tree.symlinks),
                 *(item.path for item in tree.files)):
        if not path.startswith(prefix):
            continue
        remainder = path[len(prefix):]
        if not remainder or "/" in remainder:
            continue
        if path in tree.directory_set:
            kind = "directory"
        elif path in tree.link_map:
            kind = "symlink"
        else:
            kind = "file"
        values[path] = kind
    return values


def _recognized_dsym_paths(tree: _Tree, bundles):
    """Return only directly paired root PlugIns XCTest dSYM sidecars."""
    plugin_children = _immediate_children(tree, "PlugIns")
    xctest_paths = {
        path for path, kind in bundles if kind == "xctest"
    }
    sidecars = []
    for path in tree.directories:
        if not path.casefold().endswith(_DSYM_SUFFIX):
            continue
        require(path in plugin_children and plugin_children[path] == "directory",
                "Unsupported nested iOS dSYM sidecar")
        base = path[:-len(_DSYM_SUFFIX)]
        matching = next(
            (candidate for candidate in xctest_paths
             if candidate.casefold() == base.casefold()),
            None,
        )
        require(matching is not None,
                "iOS dSYM sidecar is not paired with an XCTest bundle")
        sidecars.append((path, matching))
    for link in tree.symlinks:
        require(not link.path.casefold().endswith(_DSYM_SUFFIX),
                "Unsupported iOS dSYM sidecar symlink")
    for item in tree.files:
        require(not item.path.casefold().endswith(_DSYM_SUFFIX),
                "Unsupported iOS dSYM sidecar file")
    return tuple(sorted(sidecars))


def _recognized_bundles(tree: _Tree):
    require("Frameworks" not in tree.link_map and "PlugIns" not in tree.link_map,
            "iOS bundle container directory cannot be a symlink")
    bundles: list[tuple[str, str]] = [(".", "app")]

    def frameworks(parent: str):
        directory = "Frameworks" if parent == "." else parent + "/Frameworks"
        require(directory not in tree.link_map,
                "iOS Frameworks container cannot be a symlink")
        require(directory not in tree.file_map,
                "iOS Frameworks container is not a directory")
        if directory not in tree.directory_set:
            return
        children = _immediate_children(tree, directory)
        for path, kind in children.items():
            require(kind == "directory" and path.casefold().endswith(".framework"),
                    "Unsupported iOS Frameworks entry")
            bundles.append((path, "framework"))

    frameworks(".")
    plugins = "PlugIns"
    require(plugins not in tree.file_map,
            "iOS PlugIns container is not a directory")
    if plugins in tree.directory_set:
        for path, kind in _immediate_children(tree, plugins).items():
            suffix = path.casefold()
            if suffix.endswith(".xctest.dsym"):
                require(kind == "directory",
                        "Unsupported iOS PlugIns entry")
                continue
            require(kind == "directory" and suffix.endswith((".appex", ".xctest")),
                    "Unsupported iOS PlugIns entry")
            bundle_kind = "xctest" if suffix.endswith(".xctest") else "appex"
            bundles.append((path, bundle_kind))
            if bundle_kind == "appex":
                frameworks(path)

    recognized = {path for path, _ in bundles}
    dsym_paths = _recognized_dsym_paths(tree, bundles)
    recognized.update(path for path, _ in dsym_paths)
    for special in ("Frameworks", "PlugIns"):
        require(special not in tree.file_map,
                "iOS bundle container is not a directory")
    for path in tree.directories:
        suffix = Path(path).suffix.casefold()
        if suffix == _DSYM_SUFFIX:
            require(path in recognized, "Unsupported nested iOS dSYM sidecar")
        elif suffix in _CODE_DIRECTORY_SUFFIXES:
            require(path in recognized, "Unsupported nested iOS code bundle")
    for link in tree.symlinks:
        suffix = Path(link.path).suffix.casefold()
        require(suffix not in _CODE_DIRECTORY_SUFFIXES,
                "Unsupported nested iOS code bundle")
    for item in tree.files:
        suffix = Path(item.path).suffix.casefold()
        require(suffix not in _CODE_FILE_SUFFIXES | _CODE_DIRECTORY_SUFFIXES,
                "Unsupported nested iOS dynamic library")
    return tuple(sorted(bundles, key=lambda value: (value[0] != ".", value[0])))


def _macho_header(fd, offset, length):
    require(length >= 4, "iOS code object is not a bounded Mach-O")
    raw = os.pread(fd, min(32, length), offset)
    require(len(raw) >= 4, "iOS code object is truncated")
    little = struct.unpack("<I", raw[:4])[0]
    big = struct.unpack(">I", raw[:4])[0]
    if little in {0xFEEDFACE, 0xFEEDFACF}:
        return "thin", "<", little
    if big in {0xFEEDFACE, 0xFEEDFACF}:
        return "thin", ">", big
    if big in {0xCAFEBABE, 0xCAFEBABF}:
        return "fat", ">", big
    if little in {0xCAFEBABE, 0xCAFEBABF}:
        return "fat", "<", little
    _reject("iOS code object is not a Mach-O image")


@dataclass(frozen=True, slots=True)
class _MachoSlice:
    cpu_type: int
    cpu_subtype: int
    file_type: int
    uuid: bytes | None

    @property
    def architecture(self):
        return self.cpu_type, self.cpu_subtype


def _macho_thin(fd, offset, length, endian, magic, *, expected_file_type=None):
    header_size = 32 if magic == 0xFEEDFACF else 28
    require(length >= header_size, "iOS code object header is truncated")
    raw = os.pread(fd, header_size, offset)
    require(len(raw) == header_size, "iOS code object header is truncated")
    _, cpu_type, cpu_subtype, file_type, command_count, command_bytes, _flags = struct.unpack(
        endian + "IiiIIII", raw[:28])
    require(cpu_type in _MACHO_CPU_TYPES
            and (file_type in _MACHO_FILE_TYPES
                 or file_type == expected_file_type)
            and (expected_file_type is None or file_type == expected_file_type)
            and command_count <= 4096
            and command_bytes <= MAX_LOAD_COMMAND_BYTES
            and command_bytes <= length - header_size,
            "iOS code object header is outside its bounds")
    commands = os.pread(fd, command_bytes, offset + header_size)
    require(len(commands) == command_bytes, "iOS code object load commands are truncated")
    cursor = 0
    commands_seen = 0
    image_uuid = None
    while cursor < command_bytes:
        require(cursor + 8 <= command_bytes, "iOS code object load command is truncated")
        command, command_size = struct.unpack_from(endian + "II", commands, cursor)
        require(command_size >= 8 and cursor + command_size <= command_bytes,
                "iOS code object load command is invalid")
        if command == _LC_CODE_SIGNATURE and command_size >= 16:
            data_offset, data_size = struct.unpack_from(endian + "II", commands, cursor + 8)
            require(data_size > 0 and data_offset <= length
                    and data_size <= length - data_offset,
                    "iOS code signature range is invalid")
        if command == _LC_UUID:
            require(command_size == 24 and image_uuid is None,
                    "iOS code object UUID command is invalid")
            image_uuid = bytes(commands[cursor + 8:cursor + 24])
        cursor += command_size
        commands_seen += 1
    require(cursor == command_bytes and commands_seen == command_count,
            "iOS code object load commands are invalid")
    return _MachoSlice(cpu_type, cpu_subtype, file_type, image_uuid)


def _macho_slices(fd, length, *, expected_file_type=None):
    kind, endian, magic = _macho_header(fd, 0, length)
    if kind == "thin":
        return (_macho_thin(fd, 0, length, endian, magic,
                            expected_file_type=expected_file_type),)
    header = os.pread(fd, 8, 0)
    require(len(header) == 8, "iOS fat code object header is truncated")
    count = struct.unpack_from(endian + "I", header, 4)[0]
    is_fat64 = magic in {0xCAFEBABF}
    arch_size = 32 if is_fat64 else 20
    require(0 < count <= 64 and 8 + count * arch_size <= length,
            "iOS fat code object table is invalid")
    table = os.pread(fd, count * arch_size, 8)
    require(len(table) == count * arch_size, "iOS fat code object table is truncated")
    table_end = 8 + count * arch_size
    slices = []
    architectures = set()
    ranges = []
    for index in range(count):
        item = table[index * arch_size:(index + 1) * arch_size]
        cpu_type, cpu_subtype = struct.unpack_from(endian + "Ii", item, 0)
        if is_fat64:
            slice_offset, slice_size = struct.unpack_from(endian + "QQ", item, 8)
        else:
            slice_offset, slice_size = struct.unpack_from(endian + "II", item, 8)
        architecture = (cpu_type, cpu_subtype)
        require(cpu_type in _MACHO_CPU_TYPES and architecture not in architectures
                and slice_offset >= table_end and slice_size > 0
                and slice_offset <= length
                and slice_size <= length - slice_offset,
                "iOS fat code object slice is invalid")
        require(all(slice_offset >= end or slice_offset + slice_size <= start
                    for start, end in ranges),
                "iOS fat code object slices overlap")
        slice_kind, slice_endian, slice_magic = _macho_header(
            fd, slice_offset, slice_size)
        require(slice_kind == "thin", "Nested fat iOS code object is unsupported")
        parsed = _macho_thin(
            fd, slice_offset, slice_size, slice_endian, slice_magic,
            expected_file_type=expected_file_type)
        require(parsed.architecture == architecture,
                "iOS fat code object architecture is inconsistent")
        architectures.add(architecture)
        ranges.append((slice_offset, slice_offset + slice_size))
        slices.append(parsed)
    return tuple(slices)


def _validate_macho(path: Path, record: _File, *, expected_file_type=None):
    descriptor = None
    try:
        descriptor = _open_path_file(
            path, expected_signature=record.stat_signature)
        info = os.fstat(descriptor)
        require(_file_stat_signature(info) == record.stat_signature,
                "iOS code object changed while being inspected")
        return _macho_slices(descriptor, record.size,
                             expected_file_type=expected_file_type)
    except OSError:
        _reject("iOS code object could not be inspected")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validate_dsym(tree: _Tree, sidecar_path: str, executable_name: str,
                   xctest_slices):
    """Validate one paired Xcode dSYM and return its data file paths."""
    prefix = sidecar_path + "/"
    directories = {
        path for path in tree.directories
        if path == sidecar_path or path.startswith(prefix)
    }
    files = {
        item.path for item in tree.files
        if item.path.startswith(prefix)
    }
    require(not any(link.path.startswith(prefix) for link in tree.symlinks),
            "iOS dSYM sidecar cannot contain symlinks")
    contents = sidecar_path + "/Contents"
    info_path = contents + "/Info.plist"
    resources = contents + "/Resources"
    dwarf = resources + "/DWARF"
    dwarf_path = dwarf + "/" + executable_name
    relocations = resources + "/Relocations"
    expected_directories = {sidecar_path, contents, resources, dwarf}
    expected_files = {info_path, dwarf_path}

    if relocations in directories:
        relocation_architectures = {
            path for path in directories
            if path.startswith(relocations + "/")
            and "/" not in path[len(relocations) + 1:]
        }
        require(relocation_architectures,
                "iOS dSYM relocation directory is empty")
        expected_directories.add(relocations)
        for architecture in relocation_architectures:
            name = architecture[len(relocations) + 1:]
            require(_EXECUTABLE.fullmatch(name) is not None,
                    "Invalid iOS dSYM relocation architecture")
            expected_directories.add(architecture)
            expected_files.add(architecture + "/" + executable_name + ".yml")

    require(directories == expected_directories
            and files == expected_files,
            "Unsupported iOS dSYM sidecar layout")
    info = _plist(tree, info_path)
    require(info.get("CFBundlePackageType") == "dSYM"
            and type(info.get("CFBundleIdentifier")) is str
            and _BUNDLE.fullmatch(info["CFBundleIdentifier"]) is not None
            and type(info.get("CFBundleInfoDictionaryVersion")) is str
            and _VERSION.fullmatch(info["CFBundleInfoDictionaryVersion"]) is not None
            and type(info.get("CFBundleShortVersionString")) is str
            and _VERSION.fullmatch(info["CFBundleShortVersionString"]) is not None
            and type(info.get("CFBundleVersion")) is str
            and _VERSION.fullmatch(info["CFBundleVersion"]) is not None,
            "Invalid iOS dSYM property list")

    for path in expected_files:
        record = tree.file_map[path]
        require(not record.executable,
                "iOS dSYM sidecar file is executable")
    dwarf_record = tree.file_map[dwarf_path]
    dsym_slices = _validate_macho(
        tree.root / Path(dwarf_path), dwarf_record,
        expected_file_type=_MACHO_DSYM_FILE_TYPE,
    )
    expected_architectures = {
        item.architecture for item in xctest_slices
    }
    debug_architectures = {
        item.architecture for item in dsym_slices
    }
    require(debug_architectures == expected_architectures,
            "iOS dSYM architectures do not match XCTest binary")
    xctest_by_architecture = {
        item.architecture: item for item in xctest_slices
    }
    for item in dsym_slices:
        matching = xctest_by_architecture[item.architecture]
        require(matching.uuid is not None and any(matching.uuid)
                and item.uuid == matching.uuid,
                "iOS dSYM requires a matching nonzero XCTest UUID")
    return frozenset(expected_files)


def _profile_paths(tree: _Tree, bundle_paths: set[str]):
    allowed = {_MOBILE_PROFILE}
    allowed.update(path + "/" + _MOBILE_PROFILE for path in bundle_paths
                   if path != "." and path.casefold().endswith(".appex"))
    profiles = []
    all_paths = [item.path for item in tree.files]
    all_paths += list(tree.directories)
    all_paths += [item.path for item in tree.symlinks]
    for path in all_paths:
        if path.casefold().endswith(".mobileprovision"):
            require(path in allowed and path in tree.file_map
                    and path.split("/")[-1] == _MOBILE_PROFILE,
                    "Provisioning profile is outside an app or appex bundle")
    for item in tree.files:
        if item.path.casefold().endswith(".mobileprovision"):
            require(item.path in allowed and item.path.split("/")[-1] == _MOBILE_PROFILE,
                    "Provisioning profile is outside an app or appex bundle")
            profiles.append((item.path, item.size, item.sha256))
    return tuple(profiles)


def _has_macho_magic(path: Path, record: _File):
    descriptor = None
    try:
        descriptor = _open_path_file(
            path, expected_signature=record.stat_signature)
        info = os.fstat(descriptor)
        require(_file_stat_signature(info) == record.stat_signature,
                "iOS artifact file changed while being inspected")
        raw = os.pread(descriptor, min(4, record.size), 0)
        if len(raw) < 4:
            return False
        little = struct.unpack("<I", raw)[0]
        big = struct.unpack(">I", raw)[0]
        return (little in {0xFEEDFACE, 0xFEEDFACF, 0xCAFEBABE, 0xCAFEBABF}
                or big in {0xFEEDFACE, 0xFEEDFACF, 0xCAFEBABE, 0xCAFEBABF})
    except OSError:
        _reject("iOS artifact file boundary rejected")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _app_model(tree: _Tree, *, max_code_objects: int, _profile_sink=None):
    bundles = _recognized_bundles(tree)
    dsym_pairs = _recognized_dsym_paths(tree, bundles)
    require(len(bundles) <= max_code_objects,
            "iOS code-object limit exceeded")
    bundle_paths = {path for path, _ in bundles}
    framework_paths = {path for path, kind in bundles if kind == "framework"}
    for link in tree.symlinks:
        require(any(link.path.startswith(path + "/") for path in framework_paths),
                "Only framework symlinks are supported for iOS app input")
    profiles = _profile_paths(tree, bundle_paths)
    if _profile_sink is not None:
        require(type(_profile_sink) is list and not _profile_sink,
                'Invalid private iOS provisioning capture')
        require(all(size <= 4 * 1024 * 1024 for _, size, _ in profiles)
                and sum(size for _, size, _ in profiles) <= 64 * 1024 * 1024,
                'iOS provisioning capture exceeds its byte limit')
    profiles_by_path = {path: (size, checksum) for path, size, checksum in profiles}
    code_objects = []
    resolved_code_paths = set()
    xctest_slices_by_path = {}
    identity = version = build = None
    for bundle_path, bundle_kind in bundles:
        selected_identity, executable, selected_version, selected_build = _bundle_info(
            tree, bundle_path)
        if bundle_path == ".":
            identity, version, build = selected_identity, selected_version, selected_build
        if _profile_sink is not None and bundle_kind in {'app', 'appex'}:
            profile_path = (_MOBILE_PROFILE if bundle_path == '.'
                            else bundle_path + '/' + _MOBILE_PROFILE)
            body = (_read_tree_file(tree, profile_path, max_bytes=4 * 1024 * 1024)
                    if profile_path in profiles_by_path else None)
            _profile_sink.append((bundle_path, selected_identity, body))
        executable_path = executable if bundle_path == "." else bundle_path + "/" + executable
        resolved, kind = _resolve_entry(tree, executable_path)
        require(kind == "file", "iOS application executable is missing")
        executable_record = tree.file_map[resolved]
        require(executable_record.executable,
                "iOS application executable bit is missing")
        macho_slices = _validate_macho(tree.root / Path(resolved), executable_record,
            expected_file_type=_MACHO_BUNDLE_FILE_TYPE if bundle_kind == "xctest" else None)
        resolved_code_paths.add(resolved)
        if bundle_kind == "xctest":
            xctest_slices_by_path[bundle_path] = macho_slices
        code_objects.append({"bundlePath": bundle_path,
                             "executablePath": executable_path,
                             "kind": bundle_kind})
    require(identity is not None and version is not None and build is not None,
            "Root iOS application bundle is missing")
    resolved_data_paths = set()
    for sidecar_path, xctest_path in dsym_pairs:
        require(xctest_path in xctest_slices_by_path,
                "iOS dSYM sidecar is not paired with an XCTest bundle")
        executable = next(
            row["executablePath"].rsplit("/", 1)[-1]
            for row in code_objects
            if row["bundlePath"] == xctest_path
        )
        resolved_data_paths.update(
            _validate_dsym(tree, sidecar_path, executable,
                           xctest_slices_by_path[xctest_path])
        )
    profile_paths = {path for path, _, _ in profiles}
    for item in tree.files:
        if (item.path not in resolved_code_paths
                and item.path not in resolved_data_paths
                and item.path not in profile_paths):
            require(not _has_macho_magic(tree.root / Path(item.path), item),
                    "Unsupported unmodeled iOS code object")
    complete_files = [{"path": item.path, "bytes": item.size,
                       "sha256": item.sha256, "executable": item.executable}
                      for item in tree.files]
    complete_symlinks = [{"path": item.path, "target": item.target}
                         for item in tree.symlinks]
    complete_directories = list(tree.directories)
    complete_code_objects = sorted(code_objects,
                                   key=lambda item: (item["bundlePath"] != ".",
                                                     item["bundlePath"]))
    # Profile metadata is intentionally retained only in this private digest
    # input.  It is not returned by ``manifest`` or any public capability
    # field, but changing embedded profile bytes still changes appDigest.
    private_model = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "ios-app-content-v1",
        "applicationId": identity,
        "bundleVersion": version,
        "bundleBuild": build,
        "directories": complete_directories,
        "files": complete_files,
        "symlinks": complete_symlinks,
        "codeObjects": complete_code_objects,
        "embeddedProfiles": [{"path": path, "bytes": size, "sha256": checksum}
                             for path, size, checksum in profiles],
    }
    app_digest = digest(private_model)
    public_files = [item for item in complete_files
                    if item["path"].casefold() not in {
                        path.casefold() for path, _, _ in profiles}]
    public_manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "format": "app",
        "applicationId": identity,
        "bundleVersion": version,
        "bundleBuild": build,
        "appDigest": app_digest,
        "containerDigest": None,
        "containerBytes": tree.bytes,
        "directories": complete_directories,
        "files": public_files,
        "symlinks": complete_symlinks,
        "codeObjects": complete_code_objects,
    }
    return public_manifest, app_digest


def _app_capability(source: Path, *, format_name: str, container_digest: str,
                    container_bytes: int, max_bytes: int, max_entries: int,
                    max_code_objects: int, capability_source: Path | None = None,
                    _profile_sink=None, _root_signature=None):
    tree = _scan_tree(source, max_bytes=max_bytes, max_entries=max_entries, root_signature=_root_signature)
    # Every symlink must be resolvable before any capability is issued.  This
    # also detects links hidden behind a framework's Versions/Current alias.
    for link in tree.symlinks:
        _resolve_entry(tree, link.path)
    manifest, app_digest = _app_model(tree, max_code_objects=max_code_objects, _profile_sink=_profile_sink)
    manifest["format"] = format_name
    manifest["containerDigest"] = container_digest
    manifest["containerBytes"] = container_bytes
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    require(len(encoded.encode("utf-8")) <= 2 * 1024 * 1024,
            "iOS artifact manifest is oversized")
    return _issue_capability(
        capability_source or source, format_name, encoded, app_digest,
        container_digest, container_bytes)


def _regular_container_hash(path: Path, *, max_bytes: int, _descriptor=None):
    descriptor = os.dup(_descriptor) if _descriptor is not None else _open_path_file(path)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                and not (info.st_mode & (stat.S_IWGRP | stat.S_IWOTH
                                         | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX))
                and 0 < info.st_size <= max_bytes, "iOS IPA file boundary rejected")
        signature = _file_stat_signature(info)
        checksum = hashlib.sha256(); offset = 0
        while offset < info.st_size:
            body = os.pread(descriptor, min(1024*1024, info.st_size-offset), offset)
            require(bool(body), "iOS IPA changed while being hashed")
            checksum.update(body); offset += len(body)
        require(_file_stat_signature(os.fstat(descriptor)) == signature, "iOS IPA changed while being hashed")
        return offset, checksum.hexdigest(), signature
    finally:
        os.close(descriptor)


def _zip_name(value):
    require(type(value) is str and value and "\x00" not in value
            and not value.startswith("/") and "\\" not in value,
            "Invalid iOS IPA path")
    directory = value.endswith("/")
    clean = value[:-1] if directory else value
    _check_relative(clean)
    return clean, directory


def _zip_symlink(info: zipfile.ZipInfo):
    if info.create_system != 3:
        return False
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _zip_preflight(path: Path, size: int, *, max_entries: int,
                   expected_signature, _descriptor=None):
    """Bound ZIP metadata before ``ZipFile`` allocates its central directory."""
    descriptor = None
    try:
        descriptor = os.dup(_descriptor) if _descriptor is not None else _open_path_file(path, expected_signature=expected_signature)
        require(_file_stat_signature(os.fstat(descriptor)) == expected_signature,
                "iOS IPA snapshot changed")
        eocd_length = 22 + 0xFFFF + 20 + 56
        tail_length = min(size, eocd_length)
        tail_offset = size - tail_length
        tail = os.pread(descriptor, tail_length, tail_offset)
        require(len(tail) == tail_length, "iOS IPA end record is truncated")
        marker = tail.rfind(b"PK\x05\x06")
        eocd_offset = None
        while marker >= 0:
            require(marker + 22 <= len(tail), "iOS IPA end record is truncated")
            comment_length = struct.unpack_from("<H", tail, marker + 20)[0]
            if marker + 22 + comment_length == len(tail):
                eocd_offset = tail_offset + marker
                break
            marker = tail.rfind(b"PK\x05\x06", 0, marker)
        require(eocd_offset is not None, "iOS IPA end record is unavailable")
        eocd = os.pread(descriptor, 22, eocd_offset)
        require(len(eocd) == 22 and eocd[:4] == b"PK\x05\x06",
                "iOS IPA end record is invalid")
        (_signature, disk_number, central_disk, entries_on_disk,
         entries_total, central_bytes, central_offset,
         _comment_length) = struct.unpack("<4s4H2IH", eocd)
        zip64 = (disk_number == 0xFFFF or central_disk == 0xFFFF
                 or entries_on_disk == 0xFFFF or entries_total == 0xFFFF
                 or central_bytes == 0xFFFFFFFF
                 or central_offset == 0xFFFFFFFF)
        central_end = eocd_offset
        if zip64:
            locator_offset = eocd_offset - 20
            require(locator_offset >= 0, "iOS ZIP64 locator is unavailable")
            locator = os.pread(descriptor, 20, locator_offset)
            require(len(locator) == 20 and locator[:4] == b"PK\x06\x07",
                    "iOS ZIP64 locator is invalid")
            (_locator_signature, locator_disk, zip64_offset, disk_count) = struct.unpack(
                "<4sIQI", locator)
            require(locator_disk == 0 and disk_count == 1
                    and zip64_offset < eocd_offset
                    and zip64_offset <= size - 56,
                    "Multi-disk iOS IPA is unsupported")
            zip64_head = os.pread(descriptor, 56, zip64_offset)
            require(len(zip64_head) == 56 and zip64_head[:4] == b"PK\x06\x06",
                    "iOS ZIP64 end record is invalid")
            record_size = struct.unpack_from("<Q", zip64_head, 4)[0]
            # ZipFile supports the fixed ZIP64 record and finds it immediately
            # before the locator.  Reject layouts it would interpret differently.
            require(record_size == 44
                    and zip64_offset + 56 == locator_offset,
                    "iOS ZIP64 end record is outside the archive")
            (_zip64_signature, _record_size, _version_made, _version_needed,
             zip64_disk, zip64_central_disk, zip64_entries_on_disk,
             zip64_entries_total, zip64_central_bytes, zip64_central_offset) = struct.unpack(
                "<4sQ2H2I4Q", zip64_head)
            require(zip64_disk == 0 and zip64_central_disk == 0
                    and zip64_entries_on_disk == zip64_entries_total,
                    "Multi-disk iOS IPA is unsupported")
            entries_total = zip64_entries_total
            central_bytes = zip64_central_bytes
            central_offset = zip64_central_offset
            central_end = zip64_offset
        else:
            require(disk_number == 0 and central_disk == 0
                    and entries_on_disk == entries_total,
                    "Multi-disk iOS IPA is unsupported")
        require(type(entries_total) is int and 0 < entries_total <= max_entries,
                "iOS IPA entry limit exceeded")
        require(0 < central_bytes <= MAX_ZIP_CENTRAL_BYTES
                and central_bytes <= size
                and central_offset <= size
                and central_bytes <= size - central_offset
                and central_offset + central_bytes == central_end,
                "iOS IPA central directory is outside its bounds")
        # EOCD counts are declarations, whereas ZipFile iterates the actual
        # directory bytes.  Count each bounded record before it allocates any
        # ZipInfo objects; do not trust a forged low declaration.
        cursor, actual_entries = central_offset, 0
        while cursor < central_end:
            actual_entries += 1
            require(actual_entries <= entries_total
                    and actual_entries <= max_entries,
                    "iOS IPA entry limit exceeded")
            require(central_end - cursor >= 46,
                    "iOS IPA central directory is truncated")
            header = os.pread(descriptor, 46, cursor)
            require(len(header) == 46 and header[:4] == b"PK\x01\x02",
                    "iOS IPA central directory is invalid")
            name_bytes, extra_bytes, comment_bytes = struct.unpack_from(
                "<3H", header, 28)
            record_bytes = 46 + name_bytes + extra_bytes + comment_bytes
            require(name_bytes > 0 and record_bytes <= central_end - cursor,
                    "iOS IPA central directory is truncated")
            cursor += record_bytes
        require(actual_entries == entries_total,
                "iOS IPA entry count is inconsistent")
        return entries_total, central_offset, central_bytes
    except (OSError, ValueError, struct.error):
        _reject("iOS IPA metadata preflight failed")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _snapshot_ipa(path: Path, destination: Path, *, max_bytes: int, _source_fd=None, _destination_fd=None):
    """Freeze bounded producer bytes before ZIP metadata is interpreted.

    A pinned descriptor alone does not prevent an input producer from changing
    the same inode between preflight and ZipFile initialization.  Only the
    private snapshot is parsed; the original is checked again before issuance.
    """
    descriptor = None
    try:
        descriptor = os.dup(_source_fd) if _source_fd is not None else _open_path_file(path)
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and before.st_uid == os.getuid()
                and not (before.st_mode & (stat.S_IWGRP | stat.S_IWOTH
                                           | stat.S_ISUID | stat.S_ISGID
                                           | stat.S_ISVTX))
                and 0 < before.st_size <= max_bytes,
                "iOS IPA file boundary rejected")
        signature = _file_stat_signature(before)
        checksum, total = hashlib.sha256(), 0
        outgoing = os.fdopen(os.dup(_destination_fd), 'wb') if _destination_fd is not None else destination.open('xb')
        with outgoing:
            os.fchmod(outgoing.fileno(), 0o600)
            while True:
                block = os.pread(descriptor, min(1024 * 1024, max_bytes - total + 1), total)
                if not block:
                    break
                total += len(block)
                require(total <= max_bytes, "iOS IPA file boundary rejected")
                checksum.update(block)
                outgoing.write(block)
            outgoing.flush(); os.fsync(outgoing.fileno())
        require(total == before.st_size
                and _file_stat_signature(os.fstat(descriptor)) == signature,
                "iOS IPA changed while being copied")
        return total, checksum.hexdigest(), signature
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validated_ipa_members(infos, *, max_bytes, max_entries):
    require(0 < len(infos) <= max_entries,
            "iOS IPA entry limit exceeded")
    seen: set[str] = set()
    normalized: list[tuple[zipfile.ZipInfo, str, bool]] = []
    total_uncompressed = 0
    candidates: set[str] = set()
    for info in infos:
        name, directory = _zip_name(info.filename)
        folded = name.casefold()
        require(folded not in seen, "Case-colliding iOS IPA paths")
        seen.add(folded)
        require(not (info.flag_bits & 0x1),
                "Encrypted iOS IPA entries are unsupported")
        require(info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED},
                "Unsupported iOS IPA compression")
        require(info.file_size >= 0 and info.file_size <= max_bytes,
                "iOS IPA entry is oversized")
        if not directory:
            if info.compress_size == 0:
                require(info.file_size == 0, "Invalid iOS IPA compression size")
            else:
                require(info.file_size <= info.compress_size * MAX_ZIP_COMPRESSION_RATIO,
                        "iOS IPA compression ratio is unsafe")
            total_uncompressed += info.file_size
            require(total_uncompressed <= max_bytes,
                    "Expanded iOS IPA exceeds its byte limit")
        require(not _zip_symlink(info),
                "ZIP symlinks are unsupported for iOS IPA input")
        mode = (info.external_attr >> 16) & 0xFFFF
        if info.create_system == 3 and mode:
            require(directory and stat.S_ISDIR(mode)
                    or not directory and stat.S_ISREG(mode),
                    "Unexpected iOS IPA entry type")
            require(not (mode & (stat.S_IWGRP | stat.S_IWOTH
                                 | stat.S_ISUID | stat.S_ISGID
                                 | stat.S_ISVTX)),
                    "Unsafe iOS IPA Unix mode")
        parts = name.split("/")
        if name == "Payload":
            require(directory, "IPA Payload entry is not a directory")
        else:
            require(parts[0] == "Payload" and len(parts) >= 2,
                    "Unsupported iOS IPA top-level entry")
            if parts[1].casefold().endswith(".app"):
                candidates.add(parts[1])
        normalized.append((info, name, directory))
    require(len(candidates) == 1, "IPA must contain exactly one Payload app")
    app_name = next(iter(candidates))
    app_prefix = "Payload/" + app_name + "/"
    tree_nodes = {}
    for _info, name, directory in normalized:
        require(name == "Payload" or name == "Payload/" + app_name
                or name.startswith(app_prefix),
                "Unsupported iOS IPA payload")
        if name == "Payload/" + app_name:
            require(directory, "IPA application entry is not a directory")
        if not name.startswith(app_prefix):
            continue
        parts = name[len(app_prefix):].split("/")
        for count in range(1, len(parts)+1):
            path = "/".join(parts[:count])
            is_directory = count < len(parts) or directory
            folded = path.casefold()
            previous = tree_nodes.get(folded)
            require(previous is None or previous == (path, is_directory),
                    "Conflicting implicit iOS IPA directory")
            tree_nodes[folded] = (path, is_directory)
            require(len(tree_nodes) <= max_entries, "Expanded iOS IPA entry limit exceeded")
    return normalized, app_name


@contextmanager
def _opened_ipa_contents(path: Path, *, max_bytes: int, max_entries: int, _workspace=None,
                         _workspace_fd=None, _source_fd=None):
    """Use a caller-owned empty namespace when a persistent journal owns cleanup.

    The private workspace form intentionally retains both complete and partial
    files. Its caller reserves capacity and records directory ownership first.
    Ordinary parser callers retain the original automatic temporary cleanup.
    """
    temp = None
    if _workspace is None:
        temp = tempfile.TemporaryDirectory(prefix="repro-ios-ipa-")
        temp_root = Path(temp.name).resolve()
    else:
        temp_root = Path(_workspace)
        require(temp_root.is_absolute(), "iOS IPA workspace must be absolute")
        descriptor = _open_directory_path(temp_root)
        try:
            info = os.fstat(descriptor)
            require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
                    and not os.listdir(descriptor), "iOS IPA workspace is not empty and private")
        finally:
            os.close(descriptor)
    workspace_fd = snapshot_fd = app_fd = None
    try:
        workspace_fd = _open_directory_path(temp_root)
        workspace_stat = os.fstat(workspace_fd)
        workspace_identity = workspace_stat.st_dev, workspace_stat.st_ino
        if _workspace_fd is not None:
            expected = os.fstat(_workspace_fd)
            require((expected.st_dev, expected.st_ino) == workspace_identity, 'iOS IPA workspace changed')
        snapshot = temp_root / "source.ipa"
        snapshot_fd = os.open('source.ipa', os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                              0o600, dir_fd=workspace_fd)
        size, container_digest, source_signature = _snapshot_ipa(
            path, snapshot, max_bytes=max_bytes, _source_fd=_source_fd, _destination_fd=snapshot_fd)
        snapshot_signature = _file_stat_signature(os.fstat(snapshot_fd))
        _zip_preflight(snapshot, size, max_entries=max_entries,
                       expected_signature=snapshot_signature, _descriptor=snapshot_fd)
        try:
            os.lseek(snapshot_fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(snapshot_fd), "rb") as pinned, zipfile.ZipFile(pinned, "r", allowZip64=True) as archive:
                normalized, app_name = _validated_ipa_members(archive.infolist(),
                    max_bytes=max_bytes, max_entries=max_entries)
                app_prefix = "Payload/" + app_name + "/"
                extracted = temp_root / "app"
                os.mkdir('app', mode=0o700, dir_fd=workspace_fd)
                app_fd = os.open('app', _DIRECTORY_FLAGS, dir_fd=workspace_fd)
                copied = 0
                for _info, name, directory in normalized:
                    if not directory or name in {"Payload", "Payload/" + app_name}:
                        continue
                    require(name.startswith(app_prefix),
                            "Unsupported iOS IPA directory")
                    relative = name[len(app_prefix):]
                    _check_relative(relative)
                    directory_fd = _ensure_relative_directory(app_fd, relative)
                    try:
                        os.fchmod(directory_fd, 0o700)
                    finally:
                        os.close(directory_fd)
                for info, name, directory in normalized:
                    if directory or not name.startswith(app_prefix):
                        continue
                    relative = name[len(app_prefix):]
                    _check_relative(relative)
                    descriptor = _create_relative_file(app_fd, relative)
                    try:
                        mode = (info.external_attr >> 16) & 0xFFFF
                        os.fchmod(descriptor, 0o700 if mode & 0o111 else 0o600)
                        written = 0
                        with archive.open(info, "r") as incoming:
                            with os.fdopen(descriptor, "wb") as outgoing:
                                descriptor = None
                                while True:
                                    block = incoming.read(min(1024 * 1024,
                                                              info.file_size - written + 1))
                                    if not block:
                                        break
                                    written += len(block)
                                    copied += len(block)
                                    require(written <= info.file_size and copied <= max_bytes,
                                            "Expanded iOS IPA exceeds its byte limit")
                                    outgoing.write(block)
                        require(written == info.file_size,
                                "iOS IPA entry changed while extracting")
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)
                app_tree = extracted
        except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile,
                zipfile.LargeZipFile, EOFError, struct.error):
            _reject("iOS IPA archive boundary rejected")
        try:
            current_size, current_digest, current_signature = _regular_container_hash(
                path, max_bytes=max_bytes, _descriptor=_source_fd)
        except ContractError:
            raise
        require((current_size, current_digest, current_signature)
                == (size, container_digest, source_signature),
                "iOS IPA changed while being parsed")
        descriptor = _open_directory_path(temp_root)
        try:
            info = os.fstat(descriptor)
            require((info.st_dev, info.st_ino) == workspace_identity,
                    "iOS IPA workspace changed")
        finally:
            os.close(descriptor)
        yield app_tree, size, container_digest
    finally:
        for descriptor in (app_fd, snapshot_fd, workspace_fd):
            if descriptor is not None: os.close(descriptor)
        if temp is not None:
            temp.cleanup()


def _extract_ipa(path: Path, *, max_bytes: int, max_entries: int,
                 max_code_objects: int, _profile_sink=None):
    with _opened_ipa_contents(path, max_bytes=max_bytes, max_entries=max_entries) as (app_tree, size, checksum):
        return _app_capability(app_tree, format_name='ipa', container_digest=checksum,
            container_bytes=size, max_bytes=max_bytes, max_entries=max_entries,
            max_code_objects=max_code_objects, capability_source=path, _profile_sink=_profile_sink)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class IosBundleCapability:
    """Parser-issued inert capability for one bounded iOS application."""

    _source: Path = field(repr=False, compare=False)
    _format: str
    _manifest_json: str = field(repr=False, compare=False)
    _app_digest: str
    _container_digest: str
    _container_bytes: int
    _issuer: object = field(repr=False, compare=False)

    @property
    def manifest(self):
        # JSON round-tripping prevents mutation of the sealed capability's
        # private canonical representation.
        return deepcopy(json.loads(self._manifest_json))

    @property
    def app_digest(self):
        return self._app_digest

    @property
    def container_digest(self):
        return self._container_digest

    @property
    def format(self):
        return self._format

    @property
    def container_bytes(self):
        return self._container_bytes


def _issue_capability(source: Path, format_name: str, manifest_json: str,
                      app_digest: str, container_digest: str,
                      container_bytes: int):
    capability = IosBundleCapability(
        source, format_name, manifest_json, app_digest, container_digest,
        container_bytes, _ISSUER)
    identifier = id(capability)

    def forget(reference, *, identifier=identifier):
        with _ISSUED_CAPABILITIES_LOCK:
            if _ISSUED_CAPABILITIES.get(identifier) is reference:
                _ISSUED_CAPABILITIES.pop(identifier, None)

    with _ISSUED_CAPABILITIES_LOCK:
        _ISSUED_CAPABILITIES[identifier] = weakref.ref(capability, forget)
    return capability


def require_ios_artifact(value):
    require(type(value) is IosBundleCapability and value._issuer is _ISSUER,
            "Parser-issued iOS artifact capability required")
    with _ISSUED_CAPABILITIES_LOCK:
        reference = _ISSUED_CAPABILITIES.get(id(value))
        require(reference is not None and reference() is value,
                "Parser-issued iOS artifact capability required")
    try:
        manifest = json.loads(value._manifest_json)
        require(type(manifest) is dict and set(manifest) == {
            "schemaVersion", "kind", "format", "applicationId", "bundleVersion",
            "bundleBuild", "appDigest", "containerDigest", "containerBytes",
            "directories", "files", "symlinks", "codeObjects",
        }, "Invalid iOS artifact capability")
        require(manifest["schemaVersion"] == SCHEMA_VERSION
                and manifest["kind"] == KIND
                and manifest["format"] in {"app", "ipa"}
                and _DIGEST.fullmatch(manifest["appDigest"]) is not None
                and _DIGEST.fullmatch(manifest["containerDigest"]) is not None
                and type(manifest["containerBytes"]) is int
                and manifest["containerBytes"] > 0
                and value._format == manifest["format"]
                and value._app_digest == manifest["appDigest"]
                and value._container_digest == manifest["containerDigest"]
                and value._container_bytes == manifest["containerBytes"],
                "Invalid iOS artifact capability")
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        _reject("Invalid iOS artifact capability")
    return value


def _parse_app(path: Path, *, max_bytes: int, max_entries: int,
               max_code_objects: int, _profile_sink=None):
    require(path.name.casefold().endswith(".app"),
            "Select an iOS .app or .ipa artifact")
    require(not _path_has_symlink_component(path),
            "iOS application path contains a symlink component")
    root_signature = _owned_directory(path)
    # The directory itself is an app container, so its separate container
    # digest is deliberately distinct from the content digest.
    tree = _scan_tree(path, max_bytes=max_bytes, max_entries=max_entries,
                      root_signature=root_signature)
    for link in tree.symlinks:
        _resolve_entry(tree, link.path)
    manifest, app_digest = _app_model(tree, max_code_objects=max_code_objects, _profile_sink=_profile_sink)
    container_digest = digest({"schemaVersion": SCHEMA_VERSION,
                               "kind": "ios-app-container-v1",
                               "format": "app", "appDigest": app_digest})
    manifest["containerDigest"] = container_digest
    manifest["containerBytes"] = tree.bytes
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    require(len(encoded.encode("utf-8")) <= 2 * 1024 * 1024,
            "iOS artifact manifest is oversized")
    return _issue_capability(path, "app", encoded, app_digest,
                             container_digest, tree.bytes)


def _parse_ios_artifact(source, *, max_bytes, max_entries, max_code_objects, _profile_sink=None):
    _bounded_limits(max_bytes, max_entries, max_code_objects)
    try:
        path = Path(source).absolute()
    except (TypeError, ValueError):
        _reject("Invalid iOS artifact path")
    require(not _path_has_symlink_component(path),
            "iOS artifact path contains a symlink component")
    try:
        info = path.lstat()
    except OSError:
        _reject("iOS artifact is unavailable")
    try:
        if stat.S_ISDIR(info.st_mode):
            return _parse_app(path, max_bytes=max_bytes,
                              max_entries=max_entries,
                              max_code_objects=max_code_objects, _profile_sink=_profile_sink)
        require(stat.S_ISREG(info.st_mode) and path.suffix == ".ipa",
                "Select an iOS .app or .ipa artifact")
        return _extract_ipa(path, max_bytes=max_bytes,
                            max_entries=max_entries,
                            max_code_objects=max_code_objects, _profile_sink=_profile_sink)
    except ContractError:
        raise
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException,
            zipfile.BadZipFile, zipfile.LargeZipFile, struct.error):
        _reject()


def parse_ios_artifact(source, *, max_bytes=MAX_EXPANDED_APP_BYTES,
                       max_entries=MAX_APP_ENTRIES,
                       max_code_objects=MAX_CODE_OBJECTS):
    """Parse one owned ``.app`` directory or one bounded ``.ipa`` archive.

    IPA input is extracted only into a private temporary directory after all
    archive names and entry types have passed validation.  The temporary copy
    is not exposed; the capability retains only the source identity and the
    recomputed public manifest/digests.
    """
    return _parse_ios_artifact(source, max_bytes=max_bytes, max_entries=max_entries,
                               max_code_objects=max_code_objects)


parse_ios_bundle = parse_ios_artifact


__all__ = [
    "IosBundleCapability",
    "KIND",
    "MAX_APP_ENTRIES",
    "MAX_CODE_OBJECTS",
    "MAX_EXPANDED_APP_BYTES",
    "MAX_SYMLINK_DEPTH",
    "parse_ios_artifact",
    "parse_ios_bundle",
    "require_ios_artifact",
]
