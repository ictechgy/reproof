"""Explicit inputs for native qualification; never manufacture signature evidence.

These selectors do not build, download, inspect or authorize use of an app.
Portable tests use generated parser fixtures instead. A missing selection is a
native prerequisite skip, which the release harness rejects as qualification.
"""
import os
from pathlib import Path
import stat
import unittest


_INPUTS = {
    'REPROOF_TEST_ANDROID_APK': ('file', '.apk'),
    'REPROOF_TEST_ANDROID_UNSIGNED_APK': ('file', '.apk'),
    'REPROOF_TEST_IOS_APP': ('directory', '.app'),
    'REPROOF_TEST_IOS_DEVICE_RUNNER': ('directory', '.app'),
    'REPROOF_TEST_IOS_DEVICE_RUNNER_SECOND': ('directory', '.app'),
}


def native_fixture(name, *, environment=None):
    """Return an explicitly selected fixture or a precise, redacted prerequisite.

    Existing native inspectors still validate contents/identity. Selection is
    not evidence that a native app or signature is valid. A configured missing
    or malformed path is an error, not a silently skipped broken assertion.
    """
    if name not in _INPUTS:
        raise ValueError('Unknown native fixture selector')
    environment = os.environ if environment is None else environment
    if name not in environment:
        raise unittest.SkipTest('Native fixture not selected: set ' + name +
                                ' to an owned test artifact; native qualification not run')
    value = environment[name]
    try:
        if (type(value) is not str or not value or len(value) > 4096 or
                any(ord(char) < 32 for char in value)):
            raise ValueError
        path = Path(value)
        kind, suffix = _INPUTS[name]
        if not path.is_absolute() or '..' in path.parts or path.suffix != suffix:
            raise ValueError
        # Do not silently follow a selected app/parent symlink into other data.
        for part in (path, *path.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError
        info = path.lstat()
        if kind == 'file':
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > 64 * 1024 * 1024:
                raise ValueError
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError
        return path
    except (OSError, ValueError):
        raise ValueError('Invalid native fixture selection: ' + name) from None
