"""One pinned, local public CA bundle; never a source/transfer exception."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import ssl
import stat
import time

from reproof.core import ContractError
from .android_recovery_authority import _directory


@dataclass(frozen=True)
class _PublicEmulatorCA:
    relative: str
    sha256: str
    size: int
    emulator_sha256: str
    emulator_size: int
    properties_sha256: str
    properties_size: int


# Operator-recorded selection of SDK emulator 36.1.9/build 13823996.
# These pins identify bytes; they are not a vendor signature/provenance proof.
_APPROVED_CA = _PublicEmulatorCA(
    'lib/ca-bundle.pem',
    '0f119da204025da7808273fab42ed8e030cafb5c7ea4e1deda4e75f066f528fb', 258424,
    '7e8559aac63105502a6a4fec03dcc0cc7aaaf6f8e1e046bcad29095d46bd26eb', 1694800,
    '1b29166b5f4c5e690859fed6b03c6e37fbfe77e7f103d52ae21796844a91d492', 102)
_MAX_CA_BYTES = 1024 * 1024


def _require(value):
    if not value:
        raise ContractError('Pinned local emulator public CA boundary rejected')


def selected_public_ca(root, files, executables):
    """Pure metadata selection; content alone can never enable this path."""
    pin = _APPROVED_CA
    entries = {item.path: item for item in files}
    path = str(Path(root)/pin.relative)
    if path not in entries:
        return None
    emulator = str(Path(root)/'emulator')
    properties = str(Path(root)/'source.properties')
    _require(pin.relative == 'lib/ca-bundle.pem' and 0 < pin.size <= _MAX_CA_BYTES
             and emulator in executables and path not in executables
             and emulator in entries and properties in entries)
    for name, digest, size in ((path, pin.sha256, pin.size),
                              (emulator, pin.emulator_sha256, pin.emulator_size),
                              (properties, pin.properties_sha256, pin.properties_size)):
        _require(entries[name].sha256 == digest and entries[name].size == size)
    return path


def _active(cancellation, deadline):
    _require(cancellation is None or not cancellation.is_set())
    _require(deadline is None or time.monotonic() < deadline)


def _open_ca(root):
    directory = _directory(root)
    child = fd = None
    try:
        child = os.open('lib', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        fd = os.open('ca-bundle.pem', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=child)
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                 and info.st_size <= _MAX_CA_BYTES)
        result, fd = fd, None
        return result
    finally:
        try:
            if fd is not None: os.close(fd)
        finally:
            try:
                if child is not None: os.close(child)
            finally:
                os.close(directory)


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _certificates_only(raw, *, cancellation=None, deadline=None):
    _require(type(raw) is bytes and 0 < len(raw) <= _MAX_CA_BYTES)
    # Public curl bundles can contain UTF-8 certificate titles/comments.
    # PEM labels and base64 bodies remain strictly ASCII in the matcher below.
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        _require(False)
    _require(all(char in '\t\r\n' or char.isprintable() for char in text))
    lines = raw.splitlines()
    _require(len(lines) <= 16384 and all(len(line) <= 4096 for line in lines)
             and b'PRIVATE KEY' not in raw.upper())
    blocks = list(re.finditer(br'-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----', raw))
    _require(1 <= len(blocks) <= 512 and raw.count(b'-----BEGIN') == len(blocks)
             and raw.count(b'-----END') == len(blocks))
    # A fresh in-memory X509 parser only. No default/system trust store loading,
    # network connection, verification handshake, or global trust mutation.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    for block in blocks:
        _active(cancellation, deadline)
        _require(len(block.group()) <= 65536)
        try:
            context.load_verify_locations(cadata=block.group().decode('ascii'))
        except (ssl.SSLError, ValueError):
            _require(False)
    _require(context.cert_store_stats()['x509'] >= 1)


def verify_public_ca(root, item, files, executables, *, cancellation=None, deadline=None):
    """Bounded local read of the exact declaration; never expose its contents."""
    _active(cancellation, deadline)
    _require(selected_public_ca(root, files, executables) == item.path
             and item.sha256 == _APPROVED_CA.sha256 and item.size == _APPROVED_CA.size)
    with os.fdopen(_open_ca(root), 'rb') as source:
        before = os.fstat(source.fileno())
        _require(before.st_size == item.size)
        raw = source.read(item.size + 1)
        _active(cancellation, deadline)
        after = os.fstat(source.fileno())
        _require(len(raw) == item.size and _stamp(before) == _stamp(after)
                 and hashlib.sha256(raw).hexdigest() == item.sha256)
        _certificates_only(raw, cancellation=cancellation, deadline=deadline)
        _active(cancellation, deadline)
        rebound = _open_ca(root)
        try:
            _require(_stamp(os.fstat(rebound)) == _stamp(after)
                     and _stamp(os.fstat(source.fileno())) == _stamp(after))
        finally:
            os.close(rebound)
    _active(cancellation, deadline)
