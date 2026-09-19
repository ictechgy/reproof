"""Fixed offline CMS verification against an explicit issuer and trust anchors.

This verifies the selected profile's cryptography. It does not select Apple
trust material, sign an app, access a Keychain, or qualify a mobile backend.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import stat
import sys
import threading
import time
import uuid

from . import contracts
from .execution.artifacts import open_directory, open_regular
from .ios_provisioning_policy import assess_decoded_profile, decoded_profile_digest
from .repair_android_signing import _ProcessOwner


MAX_CMS_BYTES = 4 * 1024 * 1024
MAX_PROFILE_BYTES = 4 * 1024 * 1024
MAX_RETAINED_PROFILE_BYTES = 64 * 1024 * 1024
_SIGNED_DATA = bytes.fromhex('2a864886f70d010702')
_DATA = bytes.fromhex('2a864886f70d010701')
# SHA-256/384/512 plus SHA-1 ('2b0e03021a'): Apple's provisioning-profile CMS
# is still issued with sha1WithRSAEncryption by the Apple iPhone CA, so real
# profiles cannot pass a SHA-2-only gate. Signature validity is still decided
# by the pinned signer certificate and anchors in the sandboxed openssl step.
_DIGESTS = frozenset(
    {bytes.fromhex('2b0e03021a')}
    | {bytes.fromhex('6086480165030402' + suffix) for suffix in ('01', '02', '03')})


class IOSCmsError(RuntimeError):
    def __init__(self, code='cms_unavailable', *, cleanup_confirmed=True):
        self.code = code if code in {'cms_unavailable', 'cms_invalid', 'cms_configuration',
                                    'cms_cancelled', 'cms_timeout', 'cms_quarantined'} else 'cms_unavailable'
        self.cleanup_confirmed = cleanup_confirmed
        super().__init__(self.code)


def _require(condition, code='cms_invalid'):
    if not condition:
        raise IOSCmsError(code)


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _active(cancellation, deadline):
    _require(callable(getattr(cancellation, 'is_set', None))
             and type(deadline) in (int, float) and math.isfinite(deadline), 'cms_configuration')
    _require(not cancellation.is_set(), 'cms_cancelled')
    _require(time.monotonic() < deadline, 'cms_timeout')


def _tlv(body, offset=0):
    _require(offset + 2 <= len(body))
    tag, length = body[offset:offset + 2]
    _require(tag & 31 != 31)
    start = offset + 2
    if length & 128:
        count = length & 127
        _require(1 <= count <= 4 and start + count <= len(body) and body[start] != 0)
        length = int.from_bytes(body[start:start + count], 'big')
        _require(length >= 128)
        start += count
    end = start + length
    _require(end <= len(body))
    return tag, body[start:end], end


def _children(body, maximum):
    values = []; offset = 0
    while offset < len(body):
        _require(len(values) < maximum)
        tag, payload, offset = _tlv(body, offset)
        values.append((tag, payload))
    return values


def _algorithm(value):
    _require(value[0] == 0x30)
    parts = _children(value[1], 2)
    _require(1 <= len(parts) <= 2 and parts[0][0] == 6
             and parts[0][1] in _DIGESTS
             and (len(parts) == 1 or parts[1] == (5, b'')))
    return parts[0][1]


def _preflight(body):
    """Bound DER SignedData and require allowlisted digests before native parsing."""
    tag, value, end = _tlv(body)
    _require(tag == 0x30 and end == len(body))
    outer = _children(value, 2)
    _require(len(outer) == 2 and outer[0] == (6, _SIGNED_DATA) and outer[1][0] == 0xa0)
    tag, value, end = _tlv(outer[1][1])
    _require(tag == 0x30 and end == len(outer[1][1]))
    signed = _children(value, 6)
    _require(4 <= len(signed) <= 6 and signed[0][0] == 2
             and signed[0][1] in (b'\x01', b'\x03') and signed[1][0] == 0x31)
    algorithms = {_algorithm(item) for item in _children(signed[1][1], 4)}
    _require(bool(algorithms) and signed[2][0] == 0x30)
    content = _children(signed[2][1], 2)
    _require(len(content) == 2 and content[0] == (6, _DATA) and content[1][0] == 0xa0)
    tag, payload, end = _tlv(content[1][1])
    _require(tag == 4 and end == len(content[1][1]) and 0 < len(payload) <= MAX_PROFILE_BYTES)
    _require([item[0] for item in signed[3:-1]] in ([], [0xa0], [0xa1], [0xa0, 0xa1])
             and signed[-1][0] == 0x31)
    signers = _children(signed[-1][1], 8)
    _require(bool(signers))
    for signer in signers:
        _require(signer[0] == 0x30)
        fields = _children(signer[1], 7)
        _require(5 <= len(fields) <= 7 and fields[0][0] == 2
                 and fields[1][0] in (0x30, 0x80) and _algorithm(fields[2]) in algorithms)
    return _sha(payload)


def _public_file_digest(path):
    descriptor = open_regular(path.parent, path.name)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_uid in {0, os.getuid()} and not before.st_mode & 0o022
                 and 0 < before.st_size <= 64 * 1024 * 1024, 'cms_configuration')
        checksum = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            body = stream.read(min(1024 * 1024, remaining))
            _require(bool(body), 'cms_configuration')
            checksum.update(body)
            remaining -= len(body)
        _require(not stream.read(1), 'cms_configuration')
        after = os.fstat(stream.fileno())
        _require((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                 == (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'cms_configuration')
        return checksum.hexdigest()


@dataclass(frozen=True, slots=True)
class IOSCmsTools:
    openssl: Path
    openssl_sha256: str
    sandbox_sha256: str

    def __post_init__(self):
        path = Path(self.openssl)
        _require(path.is_absolute(), 'cms_configuration')
        object.__setattr__(self, 'openssl', path.resolve(strict=True))
        self.verify()

    def verify(self):
        try:
            _require(sys.platform == 'darwin', 'cms_configuration')
            for path, digest in ((self.openssl, self.openssl_sha256),
                                 (Path('/usr/bin/sandbox-exec'), self.sandbox_sha256)):
                contracts.validate_digest(digest)
                _require(os.access(path, os.X_OK) and _public_file_digest(path) == digest,
                         'cms_configuration')
        except Exception:
            raise IOSCmsError('cms_configuration') from None


@dataclass(frozen=True, slots=True)
class IOSCmsTrust:
    reference_id: str
    signer_der: bytes = field(repr=False)
    anchors_der: tuple[bytes, ...] = field(repr=False)

    def __post_init__(self):
        try:
            contracts.validate_id(self.reference_id)
            _require(type(self.anchors_der) is tuple and 1 <= len(self.anchors_der) <= 8,
                     'cms_configuration')
            certificates = (self.signer_der, *self.anchors_der)
            _require(all(type(value) is bytes and 0 < len(value) <= 128 * 1024 for value in certificates)
                     and len(set(self.anchors_der)) == len(self.anchors_der), 'cms_configuration')
            for certificate in certificates:
                tag, _, end = _tlv(certificate)
                _require(tag == 0x30 and end == len(certificate), 'cms_configuration')
        except Exception:
            raise IOSCmsError('cms_configuration') from None

    @property
    def definition_digest(self):
        return contracts.digest({'referenceId': self.reference_id, 'signerSha256': _sha(self.signer_der),
                                  'anchorSha256': sorted(_sha(value) for value in self.anchors_der)})


@dataclass(frozen=True, slots=True)
class VerifiedCmsProfile:
    cms_digest: str
    content_digest: str
    trust_digest: str
    evaluated_at: datetime
    signer_digest: str
    _content: bytes = field(repr=False)
    _issuer: object = field(repr=False, compare=False)

    def public(self):
        return {'cmsDigest': self.cms_digest, 'contentDigest': self.content_digest,
                'trustDigest': self.trust_digest, 'evaluatedAt': self.evaluated_at.isoformat(),
                'signerCertificateSha256': self.signer_digest}


class _Cancellation:
    def __init__(self, parent, owner):
        self.parent, self.owner = parent, owner

    def is_set(self):
        return self.parent.is_set() or self.owner.is_set()


def _pem(der):
    encoded = base64.b64encode(der)
    return (b'-----BEGIN CERTIFICATE-----\n'
            + b'\n'.join(encoded[index:index + 64] for index in range(0, len(encoded), 64))
            + b'\n-----END CERTIFICATE-----\n')


def _identity(path):
    info = path.lstat()
    return _file_identity(info)


def _file_identity(info):
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o600, 'cms_quarantined')
    return info.st_dev, info.st_ino


class IOSCmsVerifier:
    def __init__(self, tools, work_root, *, _process_owner=None):
        if _process_owner is not None:
            from .ios_native_process import _IOSOwnedProcessFacade
            _require(type(_process_owner) is _IOSOwnedProcessFacade, 'cms_configuration')
            _process_owner.validate_root(Path(work_root))
        _require(type(tools) is IOSCmsTools, 'cms_configuration')
        self.tools = tools
        self.work_root = Path(work_root)
        _require(self.work_root.is_absolute() and self.work_root.name
                 and '..' not in self.work_root.parts, 'cms_configuration')
        parent = descriptor = None
        try:
            parent = open_directory(self.work_root.parent)
            parent_info = os.fstat(parent)
            _require(parent_info.st_uid == os.getuid() and not parent_info.st_mode & 0o022,
                     'cms_configuration')
            try:
                os.mkdir(self.work_root.name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            descriptor = os.open(self.work_root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=parent)
            info = os.fstat(descriptor)
            _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                     'cms_configuration')
            self._root_identity = (info.st_dev, info.st_ino)
        except Exception:
            raise IOSCmsError('cms_configuration') from None
        finally:
            for selected in (descriptor, parent):
                if selected is not None:
                    os.close(selected)
        self._owner = _ProcessOwner() if _process_owner is None else _process_owner
        self._issuer = object()
        self._proofs = {}
        self._proof_bytes = 0
        self._work = {}
        self._work_directory_ids = {}
        self._closed = False
        self._busy = False
        self._stop = threading.Event()
        self._changed = threading.Condition(threading.RLock())

    @property
    def active_processes(self):
        return self._owner.active_processes

    @property
    def retained_profile_bytes(self):
        with self._changed:
            return self._proof_bytes

    def _root_directory(self):
        descriptor = open_directory(self.work_root)
        info = os.fstat(descriptor)
        if ((info.st_dev, info.st_ino) != self._root_identity or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            os.close(descriptor)
            raise IOSCmsError('cms_quarantined')
        return descriptor

    def _read_owned(self, work, name, maximum):
        parent = open_directory(work)
        try:
            info = os.fstat(parent)
            _require((info.st_dev, info.st_ino) == self._work_directory_ids[work]
                     and name in self._work[work])
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            _require(_file_identity(before) == self._work[work][name]
                     and 0 <= before.st_size <= maximum)
            content = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            _require(len(content) == before.st_size <= maximum
                     and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                     == (after.st_size, after.st_mtime_ns, after.st_ctime_ns))
            return content

    def _discard(self, work):
        parent = descriptor = empty = None
        try:
            parent = self._root_directory()
            descriptor = os.open(work.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            info = os.fstat(descriptor)
            _require((info.st_dev, info.st_ino) == self._work_directory_ids[work]
                     and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                     'cms_quarantined')
            identities = self._work[work]
            _require(set(os.listdir(descriptor)) == set(identities) | {'empty-ca'},
                     'cms_quarantined')
            for name, expected in identities.items():
                _require(_file_identity(os.stat(name, dir_fd=descriptor, follow_symlinks=False)) == expected,
                         'cms_quarantined')
            empty = os.open('empty-ca', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            _require(not os.listdir(empty), 'cms_quarantined')
            for name in identities:
                os.unlink(name, dir_fd=descriptor)
            os.rmdir('empty-ca', dir_fd=descriptor)
            current = os.stat(work.name, dir_fd=parent, follow_symlinks=False)
            _require((current.st_dev, current.st_ino) == self._work_directory_ids[work], 'cms_quarantined')
            os.rmdir(work.name, dir_fd=parent)
            self._work.pop(work)
            self._work_directory_ids.pop(work)
            return True
        except Exception:
            return False
        finally:
            for selected in (empty, descriptor, parent):
                if selected is not None:
                    os.close(selected)

    def verify(self, cms, *, expected_cms_digest, trust, evaluated_at, cancellation, deadline_monotonic):
        _active(cancellation, deadline_monotonic)
        _require(type(cms) is bytes and 0 < len(cms) <= MAX_CMS_BYTES
                 and type(expected_cms_digest) is str and _sha(cms) == expected_cms_digest)
        _require(type(trust) is IOSCmsTrust and type(evaluated_at) is datetime
                 and evaluated_at.tzinfo is not None, 'cms_configuration')
        evaluated_at = evaluated_at.astimezone(timezone.utc)
        expected_content = _preflight(cms)
        with self._changed:
            _require(not self._closed and not self._busy and not self._work and len(self._proofs) < 4096,
                     'cms_unavailable')
            _require(self._proof_bytes + len(cms) <= MAX_RETAINED_PROFILE_BYTES, 'cms_unavailable')
            self._busy = True
        work = None; clean = True; result_content = None; failure = None
        root_fd = work_fd = None; native_fds = []
        stop = _Cancellation(cancellation, self._stop)
        try:
            self.tools.verify(); _active(stop, deadline_monotonic)
            root_fd = self._root_directory()
            work = self.work_root / ('cms-' + uuid.uuid4().hex)
            os.mkdir(work.name, mode=0o700, dir_fd=root_fd)
            work_fd = os.open(work.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            info = os.fstat(work_fd)
            self._work_directory_ids[work] = (info.st_dev, info.st_ino)
            self._work[work] = {}
            os.mkdir('empty-ca', mode=0o700, dir_fd=work_fd)
            files = {'profile.cms': cms, 'signer.pem': _pem(trust.signer_der),
                     'anchors.pem': b''.join(_pem(value) for value in trust.anchors_der),
                     'openssl.cnf': b'', 'decoded.plist': b''}
            for name, body in files.items():
                with os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                      0o600, dir_fd=work_fd), 'wb') as stream:
                    stream.write(body); stream.flush(); os.fsync(stream.fileno())
                    self._work[work][name] = _file_identity(os.fstat(stream.fileno()))
            references = {}
            for name in files:
                descriptor = os.open(name, (os.O_WRONLY if name == 'decoded.plist' else os.O_RDONLY)
                                     | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=work_fd)
                native_fds.append(descriptor)
                _require(_file_identity(os.fstat(descriptor)) == self._work[work][name])
                references[name] = '/dev/fd/' + str(descriptor)
            system_directories = ('/usr/lib', '/System/Library', '/dev/fd',
                '/System/Volumes/Preboot/Cryptexes/OS', '/System/Cryptexes/OS',
                '/private/preboot/Cryptexes/OS')
            public_files = ('/usr/bin/env', '/usr/bin/sandbox-exec', '/dev/null', '/dev/random',
                '/dev/urandom', '/', '/System', '/System/Volumes', '/System/Volumes/Preboot',
                '/System/Volumes/Preboot/Cryptexes', '/System/Cryptexes', str(self.tools.openssl))
            readable = (' '.join('(subpath ' + json.dumps(path) + ')' for path in system_directories)
                        + ' ' + ' '.join('(literal ' + json.dumps(path) + ')' for path in public_files))
            profile = ('(version 1) (allow default) (deny network*) (deny mach-lookup) (deny process-fork) '
                '(deny file-read* file-write*) (allow file-read* ' + readable
                + ' (subpath ' + json.dumps(str(work)) + ')) (allow file-read-metadata) '
                '(deny file-read-data file-read-xattr (subpath "/System/Library/Keychains") '
                '(subpath "/System/Volumes/Preboot/Cryptexes/OS/System/Library/Keychains")) '
                '(allow file-write* (literal ' + json.dumps(str(work / 'decoded.plist'))
                + ') (literal ' + json.dumps(references['decoded.plist'])
                + ')) (allow file-read* (literal ' + json.dumps(str(self.tools.openssl)) + '))')
            arguments = ('/usr/bin/sandbox-exec', '-p', profile, '/usr/bin/env',
                'OPENSSL_CONF=' + references['openssl.cnf'], 'SSL_CERT_FILE=' + references['anchors.pem'],
                'SSL_CERT_DIR=' + str(work / 'empty-ca'), str(self.tools.openssl),
                'cms', '-verify', '-binary', '-inform', 'DER', '-nointern', '-purpose', 'any',
                '-attime', str(int(evaluated_at.timestamp())), '-in', references['profile.cms'],
                '-certfile', references['signer.pem'], '-CAfile', references['anchors.pem'],
                '-CApath', str(work / 'empty-ca'), '-out', references['decoded.plist'])
            clean = False
            result = self._owner.run(arguments, work=work, input_bytes=b'', pass_fds=tuple(native_fds),
                cancellation=stop, deadline_monotonic=deadline_monotonic,
                watched_files=((work / 'decoded.plist', MAX_PROFILE_BYTES),), max_output_bytes=16 * 1024)
            clean = result.terminated and self._owner.active_processes == 0
            _require(clean, 'cms_quarantined')
            _active(stop, deadline_monotonic)
            _require(result.bounded and not result.interrupted and result.returncode == 0)
            self.tools.verify()
            _require(all(_identity(work / name) == identity for name, identity in self._work[work].items()))
            for name, body in files.items():
                if name != 'decoded.plist':
                    _require(self._read_owned(work, name, len(body)) == body)
            result_content = self._read_owned(work, 'decoded.plist', MAX_PROFILE_BYTES)
            _require(_sha(result_content) == expected_content and len(result_content) <= MAX_PROFILE_BYTES)
            decoded_profile_digest(plistlib.loads(result_content))
        except Exception as error:
            failure = error.code if type(error) is IOSCmsError else 'cms_invalid'
        except BaseException:
            with self._changed:
                self._closed = True
                self._stop.set()
                self._proofs.clear()
                self._proof_bytes = 0
            clean = self._owner.close(deadline_monotonic=time.monotonic() + 3)
            raise
        finally:
            for descriptor in (*native_fds, work_fd, root_fd):
                if descriptor is not None:
                    os.close(descriptor)
            if work is not None and clean:
                clean = self._discard(work)
            with self._changed:
                self._busy = False
                self._changed.notify_all()
        if not clean:
            raise IOSCmsError('cms_quarantined', cleanup_confirmed=False) from None
        if failure is not None:
            raise IOSCmsError(failure) from None
        _active(stop, deadline_monotonic)
        with self._changed:
            _require(not self._closed, 'cms_cancelled')
            _require(self._proof_bytes + len(result_content) <= MAX_RETAINED_PROFILE_BYTES,
                     'cms_unavailable')
            proof = VerifiedCmsProfile(expected_cms_digest, expected_content, trust.definition_digest,
                evaluated_at, _sha(trust.signer_der), result_content, self._issuer)
            self._proofs[id(proof)] = proof
            self._proof_bytes += len(result_content)
            return proof

    def require_profile(self, proof, *, trust, expected_cms_digest):
        with self._changed:
            _require(not self._closed and type(proof) is VerifiedCmsProfile
                and proof._issuer is self._issuer and self._proofs.get(id(proof)) is proof
                and type(trust) is IOSCmsTrust and proof.trust_digest == trust.definition_digest
                and proof.cms_digest == expected_cms_digest, 'cms_invalid')
            return plistlib.loads(proof._content)

    def assess_profile(self, proof, *, trust, expected_cms_digest, **policy):
        profile = self.require_profile(proof, trust=trust, expected_cms_digest=expected_cms_digest)
        _require('evaluated_at' not in policy, 'cms_configuration')
        return assess_decoded_profile(profile, evaluated_at=proof.evaluated_at, **policy)

    def release_profile(self, proof):
        with self._changed:
            _require(not self._closed and type(proof) is VerifiedCmsProfile
                     and self._proofs.get(id(proof)) is proof and proof._issuer is self._issuer)
            self._proofs.pop(id(proof))
            self._proof_bytes -= len(proof._content)

    def close(self, *, deadline_monotonic):
        _require(type(deadline_monotonic) in (int, float) and math.isfinite(deadline_monotonic),
                 'cms_configuration')
        with self._changed:
            self._closed = True; self._stop.set(); self._proofs.clear()
            self._proof_bytes = 0
        collected = self._owner.close(deadline_monotonic=deadline_monotonic)
        with self._changed:
            while self._busy and time.monotonic() < deadline_monotonic:
                self._changed.wait(max(0, deadline_monotonic - time.monotonic()))
            if self._busy or not collected or self._owner.active_processes:
                return False
            return all(self._discard(work) for work in tuple(self._work))


__all__ = ['IOSCmsTools', 'IOSCmsTrust', 'IOSCmsVerifier', 'IOSCmsError', 'VerifiedCmsProfile']
