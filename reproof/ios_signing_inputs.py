"""Exact operator-owned iOS signing inputs; no credential paths in policies."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import threading

from . import contracts
from .execution.protocol import validate_signing_policy
from .ios_artifact_transfer import _check_relative, _open_path_file, _path_has_symlink_component
from .ios_code_signature import _entitlements
from .ios_provisioning_cms import MAX_CMS_BYTES, MAX_RETAINED_PROFILE_BYTES, IOSCmsTools, IOSCmsTrust, _tlv
from .ios_provisioning_cms import _public_file_digest


MAX_PKCS12_BYTES = 8 * 1024 * 1024
MAX_PASSWORD_BYTES = 512


class IOSSigningInputError(RuntimeError):
    def __init__(self, code='ios_signing_configuration'):
        self.code = code
        super().__init__(code)


def _require(condition, code='ios_signing_configuration'):
    if not condition:
        raise IOSSigningInputError(code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class IOSSigningIdentity:
    reference_id: str
    application_id: str
    team_id: str
    certificate_chain: tuple[bytes, ...] = field(repr=False)

    def __post_init__(self):
        try:
            contracts.validate_id(self.reference_id); contracts.validate_id(self.application_id)
            _require(type(self.team_id) is str and re.fullmatch(r'[A-Z0-9]{1,64}', self.team_id))
            _require(type(self.certificate_chain) is tuple and 1 <= len(self.certificate_chain) <= 8
                and all(type(body) is bytes and 0 < len(body) <= 128*1024 for body in self.certificate_chain)
                and len(set(self.certificate_chain)) == len(self.certificate_chain))
            for body in self.certificate_chain:
                tag, _, end = _tlv(body)
                _require(tag == 0x30 and end == len(body))
        except Exception:
            raise IOSSigningInputError() from None

    @property
    def certificate_sha256(self):
        return _sha(self.certificate_chain[0])

    @property
    def scope_digest(self):
        return contracts.digest({'kind': 'ios-app-signing', 'certificateSha256': self.certificate_sha256})

    @property
    def definition_digest(self):
        return contracts.digest({'referenceId': self.reference_id, 'applicationId': self.application_id,
            'teamId': self.team_id, 'certificateChain': [_sha(body) for body in self.certificate_chain]})


@dataclass(frozen=True, slots=True)
class IOSSigningOwnerTools:
    signer: Path
    signer_sha256: str
    verifier: Path
    verifier_sha256: str
    sandbox_sha256: str
    guardian: Path | None = None
    guardian_sha256: str | None = None

    def __post_init__(self):
        try:
            _require((self.guardian is None) == (self.guardian_sha256 is None))
            for name in ('signer', 'verifier', *(('guardian',) if self.guardian is not None else ())):
                path = Path(getattr(self, name))
                _require(path.is_absolute())
                object.__setattr__(self, name, path.resolve(strict=True))
            self.verify()
        except Exception:
            raise IOSSigningInputError('ios_signing_tool') from None

    def verify(self):
        try:
            selected = [(self.signer, self.signer_sha256), (self.verifier, self.verifier_sha256),
                        (Path('/usr/bin/sandbox-exec'), self.sandbox_sha256)]
            if self.guardian is not None: selected.append((self.guardian, self.guardian_sha256))
            for path, checksum in selected:
                contracts.validate_digest(checksum)
                _require(os.access(path, os.X_OK) and _public_file_digest(path) == checksum, 'ios_signing_tool')
        except Exception:
            raise IOSSigningInputError('ios_signing_tool') from None

    @property
    def definition_digest(self):
        value = {'nativeProtocol': 'ios-owner-v1', 'signer': self.signer_sha256,
            'verifier': self.verifier_sha256, 'sandbox': self.sandbox_sha256}
        if self.guardian is not None: value['guardian'] = self.guardian_sha256
        return contracts.digest(value)


@dataclass(frozen=True, slots=True)
class IOSSigningProvisioning:
    tools: IOSCmsTools
    trust: IOSCmsTrust
    application_identifier_prefix: str
    selected_device: str = field(repr=False)

    def __post_init__(self):
        _require(type(self.tools) is IOSCmsTools and type(self.trust) is IOSCmsTrust
            and type(self.application_identifier_prefix) is str
            and re.fullmatch(r'[A-Z0-9]{1,64}', self.application_identifier_prefix)
            and type(self.selected_device) is str and re.fullmatch(r'[A-Za-z0-9-]{1,128}', self.selected_device))

    @property
    def definition_digest(self):
        return contracts.digest({'openssl': self.tools.openssl_sha256, 'sandbox': self.tools.sandbox_sha256,
            'trust': self.trust.definition_digest, 'prefix': self.application_identifier_prefix,
            'deviceDigest': contracts.digest(self.selected_device)})


class IOSSigningDefinition:
    """Freeze all code policies and the explicit profile set before admission.

    Native signing and CMS verification still validate the actual crypto.
    Profile values and entitlements are not exposed by repr or public().
    """
    def __init__(self, identity, provisioning_reference_id, bundle_policies, profiles):
        try:
            _require(type(identity) is IOSSigningIdentity)
            contracts.validate_id(provisioning_reference_id)
            _require(type(bundle_policies) is dict and '.' in bundle_policies and 1 <= len(bundle_policies) <= 512)
            for path, row in bundle_policies.items():
                if path != '.':
                    _check_relative(path)
                    _require(path.endswith(('.framework', '.appex', '.xctest')))
                _require(type(row) is dict and set(row) == {'bundleId', 'entitlements'}
                    and type(row['bundleId']) is str and re.fullmatch(
                        r'[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+', row['bundleId']))
                _entitlements(row['entitlements'])
                _require(not path.endswith('.framework') or row['entitlements'] == {})
            encoded = json.dumps(bundle_policies, sort_keys=True, separators=(',', ':'), allow_nan=False)
            _require(len(encoded.encode()) <= 1024*1024)
            required_profiles = {path for path in bundle_policies if path == '.' or path.endswith('.appex')}
            _require(type(profiles) is dict and set(profiles) == required_profiles)
            captured = {}
            for path, row in profiles.items():
                _require(type(row) is dict and set(row) == {'cms', 'profileDigest'}
                    and type(row['cms']) is bytes and 0 < len(row['cms']) <= MAX_CMS_BYTES)
                contracts.validate_digest(row['profileDigest'])
                captured[path] = (row['cms'], row['profileDigest'])
            _require(sum(len(body) for body, _ in captured.values()) <= MAX_RETAINED_PROFILE_BYTES)
            self._identity = identity
            self._provisioning_reference_id = provisioning_reference_id
            self._policy = encoded
            self._profiles = captured
            # Reserve room for the fixed context fields and maximum work path.
            encoded_native = plistlib.dumps({'certificateChain': list(identity.certificate_chain),
                'codeObjects': self.code_objects}, fmt=plistlib.FMT_BINARY)
            _require(len(encoded_native) <= 2*1024*1024-8192)
        except Exception:
            raise IOSSigningInputError() from None

    def __repr__(self):
        return f'<IOSSigningDefinition codeObjects={len(self.bundle_policies)} profiles={len(self._profiles)}>'

    @property
    def identity(self):
        return self._identity

    @property
    def provisioning_reference_id(self):
        return self._provisioning_reference_id

    @property
    def bundle_policies(self):
        return json.loads(self._policy)

    @property
    def code_objects(self):
        policy = self.bundle_policies
        paths = sorted(policy, key=lambda path: (-1 if path == '.' else path.count('/'), path), reverse=True)
        return [{'bundlePath': path, 'bundleId': policy[path]['bundleId'],
                 'entitlements': plistlib.dumps(policy[path]['entitlements'])} for path in paths]

    @property
    def entitlements_digest(self):
        return contracts.digest(self.bundle_policies)

    @property
    def profile_paths(self):
        return tuple(sorted(self._profiles))

    @property
    def profile_bytes_total(self):
        return sum(len(body) for body, _ in self._profiles.values())

    def profile_bytes(self, path):
        _require(path in self._profiles)
        return self._profiles[path][0]

    @property
    def provisioning_policies(self):
        policy = self.bundle_policies
        return {path: {**policy[path], 'profileDigest': projection,
            'entitlementsDigest': contracts.digest(policy[path]['entitlements'])}
            for path, (_, projection) in self._profiles.items()}

    def public(self):
        return {'identityDigest': self.identity.definition_digest,
            'provisioningReferenceId': self.provisioning_reference_id,
            'entitlementsDigest': self.entitlements_digest,
            'profiles': {path: {'cmsDigest': _sha(body), 'profileDigest': projection}
                         for path, (body, projection) in sorted(self._profiles.items())}}

    @property
    def definition_digest(self):
        return contracts.digest(self.public())

    def validate_policy(self, document):
        try:
            value = validate_signing_policy(document)
            _require(value['platform'] == 'ios' and value['applicationId'] == self.identity.application_id
                and value['identityReferenceId'] == self.identity.reference_id
                and value['provisioningReferenceId'] == self.provisioning_reference_id
                and value['entitlementsDigest'] == self.entitlements_digest)
            return value
        except Exception:
            raise IOSSigningInputError('ios_signing_policy') from None


def _material_identity(info):
    _require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_uid == os.getuid() and info.st_nlink == 1 and 0 < info.st_size <= MAX_PKCS12_BYTES,
        'ios_signing_material')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class _OpenedIOSMaterial:
    def __init__(self, descriptor, password):
        self.descriptor = descriptor
        self.password = bytearray(password)

    def __repr__(self):
        return '<OpenedIOSMaterial>'

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        try:
            if self.descriptor is not None:
                os.close(self.descriptor); self.descriptor = None
        finally:
            self.password[:] = b'\0'*len(self.password)


class IOSSigningMaterialResolver:
    """Use only explicitly registered private input files and password bytes."""
    def __init__(self):
        self._lock = threading.RLock()
        self._materials = {}
        self._closed = False

    def __repr__(self):
        return f'<IOSSigningMaterialResolver registered={len(self._materials)} closed={self._closed}>'

    def register(self, identity, *, pkcs12, password):
        try:
            _require(type(identity) is IOSSigningIdentity and type(password) is bytes
                and 0 < len(password) <= MAX_PASSWORD_BYTES, 'ios_signing_material')
            password.decode('utf-8')
            path = Path(pkcs12)
            _require(path.is_absolute() and not _path_has_symlink_component(path), 'ios_signing_material')
            signature = _material_identity(path.lstat())
            with self._lock:
                _require(not self._closed and identity.reference_id not in self._materials
                    and len(self._materials) < 128, 'ios_signing_material')
                self._materials[identity.reference_id] = (identity, path, signature, bytearray(password))
        except Exception:
            raise IOSSigningInputError('ios_signing_material') from None

    def open(self, identity):
        descriptor = None
        try:
            _require(type(identity) is IOSSigningIdentity, 'ios_signing_material')
            with self._lock:
                row = self._materials.get(identity.reference_id)
                _require(not self._closed and row is not None and row[0] == identity, 'ios_signing_material')
                descriptor = _open_path_file(row[1])
                _require(_material_identity(os.fstat(descriptor)) == row[2], 'ios_signing_material')
                opened = _OpenedIOSMaterial(descriptor, row[3]); descriptor = None
                return opened
        except Exception:
            raise IOSSigningInputError('ios_signing_material') from None
        finally:
            if descriptor is not None: os.close(descriptor)

    def close(self):
        with self._lock:
            self._closed = True
            for _, _, _, password in self._materials.values():
                password[:] = b'\0'*len(password)
            self._materials.clear()


__all__ = ['IOSSigningIdentity', 'IOSSigningDefinition', 'IOSSigningOwnerTools',
           'IOSSigningProvisioning', 'IOSSigningMaterialResolver', 'IOSSigningInputError']
