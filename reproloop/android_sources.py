"""Explicit public Android build inputs, frozen before a build can execute."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from .core import ContractError, require
from .execution.artifacts import ArtifactError, BlobSet, open_regular
from .execution.wire import safe_transfer_path

_SUFFIXES = {'.kt', '.kts', '.java', '.gradle', '.xml', '.json', '.toml', '.properties',
             '.pro', '.txt', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.ttf', '.otf',
             '.aidl', '.c', '.cpp', '.h', '.hpp', '.cmake', '.strings'}
_OUTPUT_DIRS = {'build', '.gradle', '.kotlin'}
_METADATA = {'instrumentation-receipt.json'}


def validate_source_inputs(names):
    require(type(names) is list and 1 <= len(names) <= 900, 'Declare the public Android build inputs')
    for name in names:
        safe_transfer_path(name)
        require(re.fullmatch(r'[A-Za-z0-9_./-]{1,512}', name) is not None
                and Path(name).suffix.lower() in _SUFFIXES
                and not any(part.startswith('.') or part.casefold() in {
                    'build', 'artifacts', 'runs', 'node_modules', 'local.properties',
                    'instrumentation-receipt.json'} or Path(part).stem.casefold() in {
                    'auth', 'credentials', 'secrets', 'keys', 'keystore'} for part in name.split('/')),
                'Unsupported public Android input path')
    lowered = {name.casefold() for name in names}
    require(len(lowered) == len(names) and not any('/'.join(name.split('/')[:i]).casefold() in lowered
        for name in names for i in range(1, len(name.split('/')))), 'Conflicting Android input paths')
    return names


def freeze_source(root, names):
    validate_source_inputs(names)
    entries = []; total = 0
    try:
        for name in sorted(names):
            with os.fdopen(open_regular(root, name), 'rb') as stream:
                before = os.fstat(stream.fileno())
                require(before.st_nlink == 1 and before.st_size <= 8 * 1024 * 1024,
                        'Unsupported linked or oversized Android input')
                raw = stream.read(8 * 1024 * 1024 + 1)
                after = os.fstat(stream.fileno())
                require(len(raw) == before.st_size and
                    (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink),
                    'Android input changed while being captured')
            total += len(raw)
            require(total <= 60 * 1024 * 1024, 'Public Android input size limit exceeded')
            entries.append((name, raw))
        return BlobSet(tuple(entries))
    except (ArtifactError, OSError):
        raise ContractError('Public Android input boundary rejected') from None


def source_hashes(snapshot):
    return {name: hashlib.sha256(raw).hexdigest() for name, raw in snapshot.entries}


def require_declared_tree(root, names):
    """Copied workspaces may contain build outputs, never extra executable inputs."""
    root = Path(root)
    selected = set(names)
    prefixes = {'/'.join(name.split('/')[:i]) for name in names for i in range(1, len(name.split('/')))}
    for directory, dirs, files in os.walk(root, onerror=lambda _: _unreadable()):
        parent = Path(directory)
        for name in dirs:
            path = parent / name
            require(not path.is_symlink(), 'Linked Android build directory')
            require(name in _OUTPUT_DIRS or path.relative_to(root).as_posix() in prefixes,
                    'Unselected Android build directory')
        dirs[:] = sorted(name for name in dirs if name not in _OUTPUT_DIRS)
        for name in files:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            require(not path.is_symlink() and path.is_file()
                    and (relative in selected or relative in _METADATA), 'Unselected Android build input')


def _unreadable():
    raise ContractError('Android build input tree is unreadable')
