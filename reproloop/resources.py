"""Declared public runtime resources for checkouts and installed distributions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat


PACKAGE = Path(__file__).resolve().parent
_PART = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}\Z')
_ROOTS = frozenset({'reproloop', 'live-web', 'android', 'ios', 'live-ios', 'native',
                    'guest', 'tools', 'scripts'})
_EXCLUDED = frozenset({'auth.json', 'local.properties', 'credentials.json', 'build',
                       'build-device', 'artifacts', 'node_modules', '__pycache__'})
_SECRET_SUFFIXES = ('.pem', '.key', '.p12', '.pfx', '.keystore', '.jks', '.mobileprovision')


class ResourceError(RuntimeError):
    def __init__(self, code='resource_integrity'):
        self.code = code
        message = ('Use a new output directory with an existing writable parent and no symlink components'
                   if code == 'resource_output' else
                   'Runtime resources are missing, changed or unsafe; reinstall the distribution')
        super().__init__(message)


def distribution_kind():
    return 'checkout' if (PACKAGE.parent / 'pyproject.toml').is_file() else 'installed'


def resource_root():
    return PACKAGE / '_assets' if distribution_kind() == 'installed' else PACKAGE.parent


def _name(value):
    if type(value) is not str or len(value) > 1024:
        raise ResourceError()
    parts = value.split('/')
    if (len(parts) < 2 or parts[0] not in _ROOTS
            or any(not _PART.fullmatch(part) or part.lower() in _EXCLUDED for part in parts)
            or value.lower().endswith(_SECRET_SUFFIXES)):
        raise ResourceError()
    return value


def _read(root, name, *, maximum=2 * 1024 * 1024):
    """Never follow a file or directory link while reading a declared asset."""
    current = Path(root)
    for part in (current, *current.parents):
        if part.is_symlink(): raise ResourceError()
    directory = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        parts = name.split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory); directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ResourceError()
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(maximum + 1)
        if len(raw) > maximum: raise ResourceError()
        return raw
    finally:
        if descriptor is not None: os.close(descriptor)
        os.close(directory)


def _manifest(root, *, installed):
    try:
        name = 'manifest.json' if installed else 'distribution-resources.json'
        document = json.loads(_read(root, name, maximum=512 * 1024))
        expected = {'schemaVersion', 'files'} | ({'sha256'} if installed else set())
        if (type(document) is not dict or set(document) != expected
                or type(document['schemaVersion']) is not int or document['schemaVersion'] != 1
                or type(document['files']) is not list or not 1 <= len(document['files']) <= 1024):
            raise ResourceError()
        names = [_name(name) for name in document['files']]
        if names != sorted(set(names)) or len({name.casefold() for name in names}) != len(names):
            raise ResourceError()
        if installed and (type(document['sha256']) is not dict or set(document['sha256']) != set(names)
                or any(type(value) is not str or re.fullmatch(r'[a-f0-9]{64}', value) is None
                       for value in document['sha256'].values())):
            raise ResourceError()
        return document
    except (OSError, ValueError, TypeError, RecursionError):
        raise ResourceError() from None


def freeze_resource_files(root):
    """Freeze declared bytes before a build can publish or reuse an asset cache."""
    root = Path(root)
    document = _manifest(root, installed=False)
    total = 0; frozen = {}
    try:
        for name in document['files']:
            raw = _read(root, name); total += len(raw)
            if total > 32 * 1024 * 1024: raise ResourceError()
            frozen[name] = raw
    except OSError:
        raise ResourceError() from None
    return frozen


def checked_resource_files(root):
    return list(freeze_resource_files(root))


def read_resource(name):
    """Read one declared fixed tool source; installed bytes must match its manifest."""
    name = _name(name)
    kind = distribution_kind(); root = resource_root()
    document = _manifest(root, installed=kind == 'installed')
    if name not in document['files']: raise ResourceError()
    try: raw = _read(root, name)
    except OSError: raise ResourceError() from None
    if kind == 'installed' and hashlib.sha256(raw).hexdigest() != document['sha256'][name]:
        raise ResourceError()
    return raw


def _current_resources():
    kind = distribution_kind()
    root = resource_root(); document = _manifest(root, installed=kind == 'installed')
    frozen = {}; total = 0
    for name in document['files']:
        raw = _read(root, name); total += len(raw)
        if total > 32 * 1024 * 1024: raise ResourceError()
        if kind == 'installed' and hashlib.sha256(raw).hexdigest() != document['sha256'][name]:
            raise ResourceError()
        frozen[name] = raw
    return kind, frozen


def export_resources(output):
    from .execution.artifacts import ArtifactError, BlobSet
    from .execution.wire import canonical
    try:
        _, frozen = _current_resources()
        files = BlobSet(tuple(frozen.items()))
        metadata = {'schemaVersion': 1, 'resourceDigest': files.digest,
                    'files': files.manifest['files'], 'actualVM': False, 'actualMobile': False}
    except (ResourceError, ArtifactError, OSError):
        raise ResourceError() from None
    try:
        BlobSet((*files.entries, ('resource-manifest.json', canonical(metadata)))).write_new(Path(output).absolute())
    except (ArtifactError, OSError):
        raise ResourceError('resource_output') from None
    return {'schemaVersion': 1, 'status': 'exported', 'resourceCount': len(frozen),
            'resourceDigest': files.digest, 'actualVM': False, 'actualMobile': False}


def installation_check():
    kind = distribution_kind()
    try:
        kind, frozen = _current_resources()
        return {'schemaVersion': 1, 'status': 'ready', 'distribution': kind,
                'resources': list(frozen), 'resourceBytes': sum(map(len, frozen.values())),
                'actualVM': False, 'actualMobile': False}
    except (ResourceError, OSError):
        return {'schemaVersion': 1, 'status': 'resource-integrity-failed', 'distribution': kind,
                'resources': [], 'actualVM': False, 'actualMobile': False}
