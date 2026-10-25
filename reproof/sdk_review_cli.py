"""Read fixed local SDK siblings as bounded, inert sample-policy annotations."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import stat

from .sdk_review import MAX_BYTES, ReviewInvalid, normalize_export


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, 'Invalid SDK review arguments; use --help\n')


def _need(condition):
    if not condition:
        raise ReviewInvalid('sdk_export_invalid')


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode


def _version(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _directory_path(path, stack):
    """Keep each component anchored until the complete read is checked."""
    path = Path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    parts = (path.anchor, *path.parts[1:]) if path.is_absolute() else ('.', *path.parts)
    directories = []
    parent = None
    for name in parts:
        descriptor = os.open(name, flags, dir_fd=parent)
        stack.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        _need(stat.S_ISDIR(info.st_mode))
        directories.append((descriptor, parent, name, _identity(info)))
        parent = descriptor
    return parent, directories


def _check_directories(directories):
    for descriptor, parent, name, expected in directories:
        _need(_identity(os.fstat(descriptor)) == expected
              and _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == expected)


def _check_files(directory, files):
    for name, descriptor, expected in files:
        _need(_version(os.fstat(descriptor)) == expected
              and _version(os.stat(name, dir_fd=directory, follow_symlinks=False)) == expected)


def _read(descriptor):
    data = bytearray()
    while len(data) <= MAX_BYTES:
        block = os.read(descriptor, MAX_BYTES + 1 - len(data))
        if not block:
            break
        data.extend(block)
        _need(len(data) <= MAX_BYTES)
    return bytes(data)


def review_session(*, platform, session_directory):
    """Return a private envelope, never registration or collector authority.

    No directory listing, recursive reads or root/latest capture fallback. All
    required files are opened and pinned before any bytes are read. A stable
    observed read is not a cross-file transaction or native SDK attestation.
    """
    try:
        _need(platform in ('android', 'ios'))
        names = ('capture.json', 'session.json') if platform == 'android' else (
            'capture.json', 'metadata.json', 'finalized.json')
        with ExitStack() as stack:
            directory, directories = _directory_path(session_directory, stack)
            files = []
            for name in names:
                named = os.stat(name, dir_fd=directory, follow_symlinks=False)
                _need(stat.S_ISREG(named.st_mode) and 0 < named.st_size <= MAX_BYTES)
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=directory)
                stack.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                _need(_version(info) == _version(named))
                files.append((name, descriptor, _version(info)))
            _check_directories(directories)
            _check_files(directory, files)
            values = []
            for _, descriptor, expected in files:
                raw = _read(descriptor)
                _need(len(raw) == expected[3])
                values.append(raw)
                # Recheck even previously consumed siblings through the full read.
                _check_files(directory, files)
            envelope = normalize_export(platform=platform, capture=values[0], metadata=values[1],
                                        marker=values[2] if platform == 'ios' else None)
            _check_files(directory, files)
            _check_directories(directories)
            return envelope
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError):
        raise ReviewInvalid('sdk_export_invalid') from None


def main(argv=None):
    parser = _ArgumentParser(prog='reproof sdk-review', allow_abbrev=False,
        description='Review a finalized local SDK session using repository sample policies only; '
                    'no upload, approval, replay or repair authority.')
    parser.add_argument('--platform', choices=('android', 'ios'), required=True)
    parser.add_argument('--session-directory', required=True,
                        help='Session directory containing the fixed native JSON siblings (no symlinks)')
    arguments = parser.parse_args(argv)
    try:
        result = review_session(platform=arguments.platform,
                                session_directory=arguments.session_directory).summary()
    except ReviewInvalid:
        print(json.dumps({'status': 'sdk_export_invalid'}))
        return 2
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
