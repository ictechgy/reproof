"""Freeze a selected Simulator app before invoking the native installer."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..core import ContractError, digest, require
from ..execution.artifacts import ArtifactError, open_directory, open_regular
from ..ios_storage import MAX_TREE_BYTES, tree_manifest


def stage_simulator_app(source, destination, *, expected_digest, expected_bytes):
    source, destination = Path(source), Path(destination)
    files = tree_manifest(source)
    require(digest(files) == expected_digest and len(files) <= 65536
        and type(expected_bytes) is int and 0 < expected_bytes <= MAX_TREE_BYTES,
        'Selected Simulator app changed before installation')
    parent = output = None
    total = 0
    try:
        parent = open_directory(destination.parent)
        os.mkdir(destination.name, mode=0o700, dir_fd=parent)
        output = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        for name, expected in sorted(files.items()):
            source_fd = open_regular(source, name)
            with os.fdopen(source_fd, 'rb') as incoming:
                before = os.fstat(incoming.fileno())
                require(before.st_nlink == 1 and before.st_size <= expected_bytes - total,
                        'Simulator app input is linked or oversized')
                current = os.dup(output)
                try:
                    parts = name.split('/')
                    for part in parts[:-1]:
                        try: os.mkdir(part, mode=0o700, dir_fd=current)
                        except FileExistsError: pass
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                        os.close(current); current = child
                    mode = 0o700 if before.st_mode & 0o111 else 0o600
                    fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=current)
                    hasher = hashlib.sha256(); copied = 0
                    with os.fdopen(fd, 'wb') as outgoing:
                        while block := incoming.read(min(1024 * 1024, before.st_size - copied + 1)):
                            copied += len(block)
                            require(copied <= before.st_size, 'Simulator app input changed during capture')
                            hasher.update(block); outgoing.write(block)
                    after = os.fstat(incoming.fileno())
                    require(copied == before.st_size and hasher.hexdigest() == expected
                        and (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) ==
                            (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink),
                        'Simulator app input changed during capture')
                    total += copied
                finally: os.close(current)
        require(total == expected_bytes and tree_manifest(destination) == files,
                'Simulator app snapshot does not match the selected artifact')
        # All files and directories have fresh creation/write times. Do not copy
        # source timestamps: simctl can retain an older same-sized executable.
        return destination
    except (ArtifactError, OSError):
        raise ContractError('Simulator app snapshot boundary rejected') from None
    finally:
        if output is not None: os.close(output)
        if parent is not None: os.close(parent)
