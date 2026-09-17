"""Fixed material-free code signature inspection of an exact staged iOS app."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import stat
import struct
import sys
import threading
import time
import uuid
import weakref

from . import contracts
from .execution.artifacts import open_directory
from .ios_artifact_staging import stage_ios_artifact
from .ios_artifact_transfer import (MAX_APP_ENTRIES, MAX_EXPANDED_APP_BYTES, _check_relative,
    _open_relative_file, _read_tree_file, _resolve_entry, _scan_tree, parse_ios_artifact, require_ios_artifact)
from .ios_provisioning_cms import _public_file_digest
from .repair_android_signing import _ProcessOwner


class IOSCodeSignatureError(RuntimeError):
    def __init__(self, code='signature_invalid', *, cleanup_confirmed=True):
        self.code = code if code in {'signature_invalid', 'signature_configuration', 'signature_cancelled',
                                    'signature_timeout', 'signature_unavailable', 'signature_quarantined'} else 'signature_invalid'
        self.cleanup_confirmed = cleanup_confirmed
        super().__init__(self.code)


def _require(value, code='signature_invalid'):
    if not value:
        raise IOSCodeSignatureError(code)


def _active(cancellation, deadline):
    _require(callable(getattr(cancellation, 'is_set', None)) and type(deadline) in (int, float)
             and math.isfinite(deadline), 'signature_configuration')
    _require(not cancellation.is_set(), 'signature_cancelled')
    _require(time.monotonic() < deadline, 'signature_timeout')


def _entitlements(value):
    _require(type(value) is dict and len(value) <= 128, 'signature_configuration')
    def scalar(item):
        return (type(item) is bool or type(item) is int and abs(item) < 2 ** 53
                or type(item) is str and len(item.encode()) <= 2048 and '\0' not in item)
    for key, item in value.items():
        _require(type(key) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', key),
                 'signature_configuration')
        _require(scalar(item) or type(item) is list and len(item) <= 256 and all(scalar(row) for row in item),
                 'signature_configuration')
    return value


@dataclass(frozen=True, slots=True)
class IOSCodeSignatureTools:
    codesign: Path
    codesign_sha256: str
    sandbox_sha256: str
    verifier: Path | None = None
    verifier_sha256: str | None = None

    def __post_init__(self):
        _require(Path(self.codesign).is_absolute(), 'signature_configuration')
        object.__setattr__(self, 'codesign', Path(self.codesign).resolve(strict=True))
        _require((self.verifier is None) == (self.verifier_sha256 is None), 'signature_configuration')
        if self.verifier is not None:
            _require(Path(self.verifier).is_absolute(), 'signature_configuration')
            object.__setattr__(self, 'verifier', Path(self.verifier).resolve(strict=True))
        self.verify()

    def verify(self):
        try:
            _require(sys.platform == 'darwin', 'signature_configuration')
            paths = [(self.codesign, self.codesign_sha256),
                     (Path('/usr/bin/sandbox-exec'), self.sandbox_sha256)]
            if self.verifier is not None:
                paths.append((self.verifier, self.verifier_sha256))
            for path, checksum in paths:
                contracts.validate_digest(checksum)
                _require(os.access(path, os.X_OK) and _public_file_digest(path) == checksum,
                         'signature_configuration')
        except Exception:
            raise IOSCodeSignatureError('signature_configuration') from None


@dataclass(frozen=True, slots=True, weakref_slot=True)
class VerifiedIOSCodeSignature:
    context_digest: str
    app_digest: str
    container_digest: str
    definition_digest: str
    _document: str = field(repr=False)
    _issuer: object = field(repr=False, compare=False)

    def public(self):
        return json.loads(self._document)


def _architectures(tree, executable):
    resolved, kind = _resolve_entry(tree, executable)
    _require(kind == 'file')
    entry = tree.file_map[resolved]
    descriptor = _open_relative_file(tree.root, resolved, expected_signature=entry.stat_signature)
    try:
        header = os.pread(descriptor, 8, 0)
        _require(len(header) == 8)
        magic = struct.unpack('>I', header[:4])[0]
        if magic in (0xcafebabe, 0xbebafeca, 0xcafebabf, 0xbfbafeca):
            endian = '>' if magic in (0xcafebabe, 0xcafebabf) else '<'
            count = struct.unpack(endian + 'I', header[4:])[0]
            _require(1 <= count <= 32)
            size = 32 if magic in (0xcafebabf, 0xbfbafeca) else 20
            table = os.pread(descriptor, count * size, 8)
            _require(len(table) == count * size)
            values = [struct.unpack_from(endian + 'ii', table, index * size) for index in range(count)]
        else:
            endian = '<' if magic in (0xcefaedfe, 0xcffaedfe) else '>'
            _require(magic in (0xcefaedfe, 0xcffaedfe, 0xfeedface, 0xfeedfacf))
            fields = os.pread(descriptor, 8, 4)
            _require(len(fields) == 8)
            values = [struct.unpack(endian + 'ii', fields)]
        _require(len(set(values)) == len(values))
        return tuple(values)
    finally:
        os.close(descriptor)


class _Cancellation:
    def __init__(self, parent, stop):
        self.parent, self.stop = parent, stop

    def is_set(self):
        return self.stop.is_set() or self.parent.is_set()


def _remove_owned_contents(descriptor, remaining, depth=0, *, deadline_monotonic=None):
    _require(depth <= 512, 'signature_quarantined')
    _require(deadline_monotonic is None or time.monotonic() < deadline_monotonic, 'signature_quarantined')
    with os.scandir(descriptor) as entries:
        for entry in entries:
            _require(deadline_monotonic is None or time.monotonic() < deadline_monotonic, 'signature_quarantined')
            remaining[0] -= 1
            _require(remaining[0] >= 0, 'signature_quarantined')
            info = entry.stat(follow_symlinks=False)
            _require(info.st_uid == os.getuid(), 'signature_quarantined')
            identity = info.st_dev, info.st_ino
            if stat.S_ISDIR(info.st_mode):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
                try:
                    actual = os.fstat(child)
                    _require((actual.st_dev, actual.st_ino) == identity, 'signature_quarantined')
                    _remove_owned_contents(child, remaining, depth + 1, deadline_monotonic=deadline_monotonic)
                finally:
                    os.close(child)
                current = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                _require((current.st_dev, current.st_ino) == identity, 'signature_quarantined')
                os.rmdir(entry.name, dir_fd=descriptor)
            else:
                _require(stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode), 'signature_quarantined')
                current = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                _require((current.st_dev, current.st_ino) == identity, 'signature_quarantined')
                os.unlink(entry.name, dir_fd=descriptor)


class IOSCodeSignatureInspector:
    """Inspect every code object and architecture with one immutable policy.

    Ad-hoc mode is restricted to Simulator artifacts. Identity mode requires
    an exact embedded leaf certificate and CodeDirectory team. Provisioning,
    installation and mobile qualification remain separate checks.
    """
    def __init__(self, tools, work_root, *, signature_kind, bundle_policies,
                 expected_certificate_sha256=None, expected_team_id=None, _process_owner=None):
        if _process_owner is not None:
            from .ios_native_process import _IOSOwnedProcessFacade
            _require(type(_process_owner) is _IOSOwnedProcessFacade, 'signature_configuration')
            _process_owner.validate_root(Path(work_root))
        try:
            _require(type(tools) is IOSCodeSignatureTools and signature_kind in {'identity', 'adhoc-simulator'},
                     'signature_configuration')
            if signature_kind == 'identity':
                _require(tools.verifier is not None, 'signature_configuration')
                contracts.validate_digest(expected_certificate_sha256)
                _require(type(expected_team_id) is str and re.fullmatch(r'[A-Z0-9]{1,64}', expected_team_id),
                         'signature_configuration')
            else:
                _require(expected_certificate_sha256 is None and expected_team_id is None,
                         'signature_configuration')
            _require(type(bundle_policies) is dict and '.' in bundle_policies
                     and 1 <= len(bundle_policies) <= 512, 'signature_configuration')
            for path, row in bundle_policies.items():
                if path != '.':
                    _check_relative(path)
                _require(type(row) is dict and set(row) == {'bundleId', 'entitlements'}
                    and type(row['bundleId']) is str and re.fullmatch(
                        r'[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+', row['bundleId']),
                    'signature_configuration')
                _entitlements(row['entitlements'])
            policy = {'kind': signature_kind, 'certificateSha256': expected_certificate_sha256,
                'teamId': expected_team_id, 'bundles': bundle_policies,
                'codesignSha256': tools.codesign_sha256, 'sandboxSha256': tools.sandbox_sha256,
                'verifierSha256': tools.verifier_sha256}
            self._policy = json.dumps(policy, sort_keys=True, separators=(',', ':'), allow_nan=False)
            _require(len(self._policy.encode()) <= 1024 * 1024, 'signature_configuration')
            self.definition_digest = contracts.digest(policy)
            self.work_root = Path(work_root)
            _require(self.work_root.is_absolute() and self.work_root.name and '..' not in self.work_root.parts,
                     'signature_configuration')
            parent = open_directory(self.work_root.parent)
            try:
                info = os.fstat(parent)
                _require(info.st_uid == os.getuid() and not info.st_mode & 0o022, 'signature_configuration')
                try:
                    os.mkdir(self.work_root.name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                descriptor = os.open(self.work_root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    info = os.fstat(descriptor)
                    _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                             'signature_configuration')
                    self._root_identity = info.st_dev, info.st_ino
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent)
        except Exception:
            raise IOSCodeSignatureError('signature_configuration') from None
        self.tools = tools
        self._owner = _ProcessOwner() if _process_owner is None else _process_owner
        self._work = {}
        self._proofs = {}
        self._issuer = object()
        self._closed = False
        self._busy = False
        self._stop = threading.Event()
        self._changed = threading.Condition(threading.RLock())

    @property
    def active_processes(self):
        return self._owner.active_processes

    def _root(self):
        descriptor = open_directory(self.work_root)
        info = os.fstat(descriptor)
        if ((info.st_dev, info.st_ino) != self._root_identity or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            os.close(descriptor)
            raise IOSCodeSignatureError('signature_quarantined', cleanup_confirmed=False)
        return descriptor

    def _discard(self, work):
        descriptor = work_fd = None
        try:
            descriptor = self._root()
            work_fd = os.open(work.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            info = os.fstat(work_fd)
            _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                     and (info.st_dev, info.st_ino) == self._work[work])
            _remove_owned_contents(work_fd, [MAX_APP_ENTRIES + 8 * 512 * 32 + 8])
            current = os.stat(work.name, dir_fd=descriptor, follow_symlinks=False)
            _require((current.st_dev, current.st_ino) == self._work[work])
            os.rmdir(work.name, dir_fd=descriptor)
            self._work.pop(work)
            return True
        except Exception:
            return False
        finally:
            for selected in (work_fd, descriptor):
                if selected is not None:
                    os.close(selected)

    def _sandbox(self, work, writable, *, offline_verification=False):
        directories = ('/usr/lib', '/System/Library', '/dev/fd', '/System/Volumes/Preboot/Cryptexes/OS',
                       '/System/Cryptexes/OS', str(work))
        files = ('/', '/System', '/System/Volumes', '/System/Volumes/Preboot',
                 '/System/Volumes/Preboot/Cryptexes', '/System/Cryptexes', '/dev/null',
                 '/dev/random', '/dev/urandom', str(self.tools.codesign))
        if offline_verification:
            files += (str(self.tools.verifier),)
        readable = (' '.join('(subpath ' + json.dumps(value) + ')' for value in directories)
                    + ' ' + ' '.join('(literal ' + json.dumps(value) + ')' for value in files))
        profile = ('(version 1) (allow default) (deny network*) (deny mach-lookup) (deny process-fork) '
            '(deny file-read* file-write*) (allow file-read* ' + readable + ') (allow file-read-metadata) '
            '(deny file-read-data file-read-xattr (subpath "/System/Library/Keychains") '
            '(subpath "/System/Volumes/Preboot/Cryptexes/OS/System/Library/Keychains"))')
        if offline_verification:
            # The fixed verifier calls Security with kSecCSNoNetworkAccess.
            # StaticCode.cpp supplies an empty CMS search list and disables
            # Keychain certificate searches on the resulting SecTrust object.
            profile += ' (allow mach-lookup (global-name "com.apple.trustd.agent"))'
        if writable:
            profile += ' (allow file-write* ' + ' '.join('(literal ' + json.dumps(str(path)) + ')'
                                                       for path in writable) + ')'
        return profile

    def _run(self, arguments, work, cancellation, deadline, *, writable=(), offline_verification=False):
        _active(cancellation, deadline); self.tools.verify()
        binary = self.tools.verifier if offline_verification else self.tools.codesign
        _require(binary is not None, 'signature_configuration')
        profile = self._sandbox(work, writable, offline_verification=offline_verification)
        result = self._owner.run(('/usr/bin/sandbox-exec', '-p', profile,
            str(binary), *map(str, arguments)), work=work, input_bytes=b'', pass_fds=(),
            cancellation=cancellation, deadline_monotonic=deadline, max_output_bytes=1024 * 1024,
            watched_files=tuple((path, 128 * 1024) for path in writable))
        _require(result.terminated and self.active_processes == 0, 'signature_quarantined')
        _active(cancellation, deadline)
        _require(result.returncode == 0 and result.bounded and not result.interrupted)
        if offline_verification and len(arguments) == 1:
            _require(result.stdout == b'{"schemaVersion":1,"ok":true,"nativeStatus":0}\n')
        return result

    def _identity_image(self, path, cpu, subtype, selected, policy, work, stop, deadline):
        result = self._run((path, str(cpu), str(subtype)), work, stop, deadline, offline_verification=True)
        row = plistlib.loads(result.stdout)
        _require(type(row) is dict and set(row) == {'schemaVersion', 'cpuType', 'cpuSubtype',
            'bundleId', 'teamId', 'certificateSha256', 'entitlements'})
        _require(type(row['schemaVersion']) is int and row['schemaVersion'] == 1
            and type(row['cpuType']) is int and row['cpuType'] == cpu
            and type(row['cpuSubtype']) is int and row['cpuSubtype'] == subtype
            and row['bundleId'] == selected['bundleId'] and row['teamId'] == policy['teamId']
            and row['certificateSha256'] == policy['certificateSha256'])
        actual_entitlements = _entitlements(row['entitlements'])
        entitlement_digest = contracts.digest(actual_entitlements)
        _require(entitlement_digest == contracts.digest(selected['entitlements']))
        return {'cpuType': cpu, 'cpuSubtype': subtype, 'signatureKind': 'identity',
            'certificateSha256': row['certificateSha256'], 'entitlementsDigest': entitlement_digest}

    def inspect(self, artifact, *, context_digest, cancellation, deadline_monotonic):
        require_ios_artifact(artifact); contracts.validate_digest(context_digest)
        _active(cancellation, deadline_monotonic)
        with self._changed:
            _require(not self._closed and not self._busy and not self._work and len(self._proofs) < 4096,
                     'signature_unavailable')
            self._busy = True
        work = None; clean = True; failure = None; records = []
        stop = _Cancellation(cancellation, self._stop)
        try:
            root_fd = self._root()
            try:
                work = self.work_root / ('inspection-' + uuid.uuid4().hex)
                os.mkdir(work.name, mode=0o700, dir_fd=root_fd)
                info = os.stat(work.name, dir_fd=root_fd, follow_symlinks=False)
                self._work[work] = info.st_dev, info.st_ino
            finally:
                os.close(root_fd)
            staged = stage_ios_artifact(artifact, work / 'App.app')
            _active(stop, deadline_monotonic)
            policy = json.loads(self._policy)
            objects = staged.manifest['codeObjects']
            _require({row['bundlePath'] for row in objects} == set(policy['bundles']))
            tree = _scan_tree(staged._source, max_bytes=MAX_EXPANDED_APP_BYTES, max_entries=MAX_APP_ENTRIES)
            root_info = plistlib.loads(_read_tree_file(tree, 'Info.plist'))
            platforms = root_info.get('CFBundleSupportedPlatforms')
            _require(platforms in (['iPhoneOS'], ['iPhoneSimulator']))
            if policy['kind'] == 'adhoc-simulator':
                _require(platforms == ['iPhoneSimulator'])
            clean = False
            if policy['kind'] == 'identity':
                self._run((staged._source,), work, stop, deadline_monotonic, offline_verification=True)
            else:
                self._run(('--verify', '--strict', '--deep', '--all-architectures', staged._source),
                          work, stop, deadline_monotonic)
            clean = True
            for number, obj in enumerate(objects):
                selected = policy['bundles'][obj['bundlePath']]
                path = staged._source if obj['bundlePath'] == '.' else staged._source / obj['bundlePath']
                info_path = 'Info.plist' if obj['bundlePath'] == '.' else obj['bundlePath'] + '/Info.plist'
                info = plistlib.loads(_read_tree_file(tree, info_path))
                _require(info.get('CFBundleIdentifier') == selected['bundleId'])
                images = []
                for image, (cpu, subtype) in enumerate(_architectures(tree, obj['executablePath'])):
                    if policy['kind'] == 'identity':
                        clean = False
                        images.append(self._identity_image(path, cpu, subtype, selected, policy,
                            work, stop, deadline_monotonic))
                        clean = True
                        continue
                    prefix = work / f'object-{number:03d}-image-{image:02d}-cert-'
                    certificates = tuple(Path(str(prefix) + str(index)) for index in range(8))
                    clean = False
                    result = self._run(('--display', '--verbose=4', '--architecture', str(cpu) + ',' + str(subtype),
                        '--entitlements', '-', '--xml', '--extract-certificates=' + str(prefix), path),
                        work, stop, deadline_monotonic, writable=certificates)
                    clean = True
                    identifiers = re.findall(rb'^Identifier=([^\r\n]+)$', result.stderr, re.M)
                    _require(identifiers == [selected['bundleId'].encode()])
                    actual_entitlements = plistlib.loads(result.stdout) if result.stdout.strip() else {}
                    _entitlements(actual_entitlements)
                    _require(contracts.digest(actual_entitlements) == contracts.digest(selected['entitlements']))
                    leaf = None
                    present = [item for item in certificates if item.exists()]
                    _require(present == list(certificates[:len(present)]))
                    certificate_identities = {}
                    for certificate in present:
                        descriptor = os.open(certificate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        with os.fdopen(descriptor, 'rb') as stream:
                            info = os.fstat(stream.fileno())
                            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                                and info.st_nlink == 1 and not info.st_mode & 0o022 and 0 < info.st_size <= 128 * 1024)
                            data = stream.read(128 * 1024 + 1)
                            after = os.fstat(stream.fileno())
                            _require(len(data) == info.st_size
                                and (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                                == (after.st_size, after.st_mtime_ns, after.st_ctime_ns))
                            certificate_identities[certificate.name] = info.st_dev, info.st_ino
                        if certificate == certificates[0]:
                            leaf = hashlib.sha256(data).hexdigest()
                    _require(not present and b'Signature=adhoc' in result.stderr)
                    output_directory = open_directory(work)
                    try:
                        directory_info = os.fstat(output_directory)
                        _require((directory_info.st_dev, directory_info.st_ino) == self._work[work])
                        for name, expected in certificate_identities.items():
                            current = os.stat(name, dir_fd=output_directory, follow_symlinks=False)
                            _require((current.st_dev, current.st_ino) == expected and stat.S_ISREG(current.st_mode))
                        for name in certificate_identities:
                            os.unlink(name, dir_fd=output_directory)
                    finally:
                        os.close(output_directory)
                    images.append({'cpuType': cpu, 'cpuSubtype': subtype, 'signatureKind': policy['kind'],
                        'certificateSha256': leaf, 'entitlementsDigest': contracts.digest(actual_entitlements)})
                records.append({'bundlePath': obj['bundlePath'], 'bundleId': selected['bundleId'], 'architectures': images})
            _require(parse_ios_artifact(staged._source).app_digest == artifact.app_digest)
            _active(stop, deadline_monotonic); self.tools.verify()
        except Exception as error:
            failure = error.code if type(error) is IOSCodeSignatureError else 'signature_invalid'
            if self.active_processes == 0:
                clean = True
        except BaseException:
            self._closed = True; self._stop.set()
            clean = self._owner.close(deadline_monotonic=time.monotonic() + 3)
            raise
        finally:
            if work is not None and clean:
                clean = self._discard(work)
            with self._changed:
                self._busy = False; self._changed.notify_all()
        if not clean:
            raise IOSCodeSignatureError('signature_quarantined', cleanup_confirmed=False) from None
        if failure:
            raise IOSCodeSignatureError(failure) from None
        _active(stop, deadline_monotonic)
        with self._changed:
            _require(not self._closed, 'signature_cancelled')
            document = json.dumps({'contextDigest': context_digest, 'appDigest': artifact.app_digest,
                'containerDigest': artifact.container_digest, 'definitionDigest': self.definition_digest,
                'codeObjects': records}, sort_keys=True, separators=(',', ':'))
            _require(len(document.encode()) <= 2 * 1024 * 1024)
            proof = VerifiedIOSCodeSignature(context_digest, artifact.app_digest, artifact.container_digest,
                                            self.definition_digest, document, self._issuer)
            identifier = id(proof)
            owner = weakref.ref(self)
            def forget(reference):
                instance = owner()
                if instance is not None:
                    with instance._changed:
                        if instance._proofs.get(identifier) is reference:
                            instance._proofs.pop(identifier, None)
            self._proofs[identifier] = weakref.ref(proof, forget)
            return proof

    def require_verified(self, proof, artifact, *, context_digest):
        require_ios_artifact(artifact)
        with self._changed:
            reference = self._proofs.get(id(proof))
            _require(not self._closed and type(proof) is VerifiedIOSCodeSignature
                and proof._issuer is self._issuer and reference is not None and reference() is proof
                and proof.context_digest == context_digest and proof.app_digest == artifact.app_digest
                and proof.container_digest == artifact.container_digest and proof.definition_digest == self.definition_digest)
            return proof.public()

    def close(self, *, deadline_monotonic):
        _require(type(deadline_monotonic) in (int, float) and math.isfinite(deadline_monotonic), 'signature_configuration')
        with self._changed:
            self._closed = True; self._stop.set(); self._proofs.clear()
        collected = self._owner.close(deadline_monotonic=deadline_monotonic)
        with self._changed:
            while self._busy and time.monotonic() < deadline_monotonic:
                self._changed.wait(max(0, deadline_monotonic - time.monotonic()))
            if self._busy or not collected or self.active_processes:
                return False
            return all(self._discard(work) for work in tuple(self._work))


__all__ = ['IOSCodeSignatureTools', 'IOSCodeSignatureInspector', 'IOSCodeSignatureError', 'VerifiedIOSCodeSignature']
