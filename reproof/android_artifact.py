"""Freeze one selected public APK before inspection or installation."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .core import ContractError, require
from .execution.artifacts import ArtifactError, open_directory, open_regular
from .storage import MAX_APK


def verify_apk(source, *, expected_digest, expected_bytes):
    source = Path(source).absolute()
    require(source.suffix == '.apk' and type(expected_bytes) is int and 0 < expected_bytes <= MAX_APK,
            'Select a bounded public APK artifact')
    try:
        descriptor = open_regular(source.parent, source.name)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(before.st_nlink == 1 and before.st_size == expected_bytes, 'Selected APK changed')
            checksum = hashlib.sha256(); total = 0
            while True:
                chunk = stream.read(min(1024 * 1024, expected_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                require(total <= expected_bytes, 'Selected APK changed during inspection')
                checksum.update(chunk)
            after = os.fstat(stream.fileno())
            require(total == expected_bytes and checksum.hexdigest() == expected_digest
                and (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink),
                'Selected APK changed during inspection')
    except (ArtifactError, OSError):
        raise ContractError('Selected APK file boundary rejected') from None


def stage_apk(source, destination, *, expected_digest, expected_bytes):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    require(source.suffix == destination.suffix == '.apk', 'Select a public APK artifact')
    require(type(expected_bytes) is int and 0 < expected_bytes <= MAX_APK,
            'Selected APK exceeds its size limit')
    parent = None
    created = False
    try:
        parent = open_directory(destination.parent)
        descriptor = open_regular(source.parent, source.name)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(before.st_nlink == 1 and before.st_size == expected_bytes, 'Selected APK changed')
            output = os.open(destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
            created = True
            checksum = hashlib.sha256(); total = 0
            with os.fdopen(output, 'wb') as target:
                while True:
                    chunk = stream.read(min(1024 * 1024, expected_bytes - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    require(total <= expected_bytes, 'Selected APK changed during staging')
                    target.write(chunk); checksum.update(chunk)
            after = os.fstat(stream.fileno())
            require(total == expected_bytes and checksum.hexdigest() == expected_digest
                and (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink),
                'Selected APK changed during staging')
        created = False
        return destination
    except (ArtifactError, OSError):
        raise ContractError('Selected APK file boundary rejected') from None
    finally:
        if parent is not None:
            if created:
                os.unlink(destination.name, dir_fd=parent)
            os.close(parent)
