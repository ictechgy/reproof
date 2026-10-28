"""Public, fixed Darwin ADB release pin. Reading this module executes no tool.

Hashes identify bytes fetched from Google's versioned distribution URL, not a
reproducible-build attestation. Runtime discovery isolation still needs its own
negative qualification tests before any ordinary recovery activation.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from reproof.core import ContractError
from reproof.execution.artifacts import open_regular

ADB_RELEASE = '35.0.2'
ADB_BUILD = '12147458'
ADB_ARCHIVE_URL = 'https://dl.google.com/android/repository/platform-tools_r35.0.2-darwin.zip'
ADB_ARCHIVE_BYTES = 13_335_867
ADB_ARCHIVE_SHA256 = '1820078db90bf21628d257ff052528af1c61bb48f754b3555648f5652fa35d78'
ADB_DARWIN_BYTES = 14_046_464
ADB_DARWIN_SHA256 = '9e78ca23f20ce357d156704d0eb706f90056d15310524881a1aec532be3acec3'
ADB_SOURCE_COMMIT = 'ce9ea51f30f69f7560db692f7cb4d5d5502c3653'
ADB_SOURCE_TAG = 'https://android.googlesource.com/platform/packages/modules/adb/+/refs/tags/platform-tools-35.0.2'


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_nlink,
            info.st_mtime_ns, info.st_ctime_ns)


def require_pinned_adb(path):
    """Check the already selected binary without extracting, running or fetching it."""
    descriptor = rebound = None
    try:
        path = Path(path)
        if not path.is_absolute():
            raise ValueError
        descriptor = open_regular(path.parent, path.name)
        before = os.fstat(descriptor)
        if before.st_size != ADB_DARWIN_BYTES or before.st_nlink != 1:
            raise ValueError
        digest = hashlib.sha256()
        remaining = ADB_DARWIN_BYTES
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1) or _stamp(os.fstat(descriptor)) != _stamp(before):
            raise ValueError
        rebound = open_regular(path.parent, path.name)
        if _stamp(os.fstat(rebound)) != _stamp(before) or digest.hexdigest() != ADB_DARWIN_SHA256:
            raise ValueError
        return ADB_DARWIN_SHA256
    except (OSError, RuntimeError, ValueError, TypeError):
        raise ContractError('Pinned public Darwin ADB release is required') from None
    finally:
        for fd in (descriptor, rebound):
            if fd is not None:
                os.close(fd)
