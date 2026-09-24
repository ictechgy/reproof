"""Pinned selected-device queries; these observations grant no execution authority."""
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
import uuid

from . import contracts
from .execution.artifacts import open_regular
from .execution.wire import MAX_FRAME_BYTES, decode_json
from .ios_provisioning_cms import _public_file_digest
from .repair_android_signing import _ProcessOwner, _cleanup_known_work


class IOSDeviceToolError(RuntimeError):
    def __init__(self, code='ios_device_tool_unavailable'):
        self.code = code
        super().__init__(code)


def _require(value, code='ios_device_tool_unavailable'):
    if not value:
        raise IOSDeviceToolError(code)


@dataclass(frozen=True, slots=True)
class IOSDeviceTools:
    devicectl: Path = field(repr=False)
    sha256: str

    def __post_init__(self):
        object.__setattr__(self, 'devicectl', Path(self.devicectl))
        self.verify()

    def verify(self):
        try:
            contracts.validate_digest(self.sha256)
            _require(self.devicectl.is_absolute() and self.devicectl.resolve(strict=True) == self.devicectl
                and os.access(self.devicectl, os.X_OK) and _public_file_digest(self.devicectl) == self.sha256)
            descriptor = open_regular(self.devicectl.parent, self.devicectl.name)
            with os.fdopen(descriptor, 'rb') as stream:
                # Xcode's shell launcher may runFirstLaunch and then dispatch
                # another binary. Require an explicitly pinned Mach-O entry.
                _require(stream.read(4) in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                    b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'))
        except (OSError, RuntimeError, ValueError, TypeError):
            raise IOSDeviceToolError() from None


@dataclass(frozen=True, slots=True)
class DeviceCtlObservation:
    query: str
    definition_digest: str
    _document: str = field(repr=False)
    _native_binding_digest: str | None = field(default=None,repr=False)

    @property
    def data(self):
        return json.loads(self._document)

    def public(self):
        result = {'kind': 'ios-device-query', 'query': self.query,
            'definitionDigest': self.definition_digest,
            'evidenceDigest': hashlib.sha256(self._document.encode()).hexdigest(),
            'executionAuthority': 'none', 'hostClientStopped': True, 'deviceCleanupConfirmed': False}
        if self._native_binding_digest is not None: result['nativeBindingDigest'] = self._native_binding_digest
        return result


def _directory_identity(path):
    _require(path.is_absolute() and path.resolve(strict=True) == path)
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077)
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


@dataclass(frozen=True, slots=True)
class IOSDeviceQueryDefinition:
    tools: IOSDeviceTools = field(repr=False)
    identifier: str = field(repr=False)
    udid: str = field(repr=False)
    bundle: str
    work_root: Path = field(repr=False)
    native_guardian: object = field(default=None,repr=False)

    def __post_init__(self):
        object.__setattr__(self,'work_root',Path(self.work_root))
        self.verify()

    def verify(self):
        try:
            _require(type(self.tools) is IOSDeviceTools and type(self.identifier) is str
                and str(uuid.UUID(self.identifier)) == self.identifier.lower())
            _require(type(self.udid) is str and re.fullmatch(r'[A-Za-z0-9-]{1,128}',self.udid))
            _require(type(self.bundle) is str and len(self.bundle) <= 180
                and re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+',self.bundle))
            _require(self.work_root.is_absolute() and '..' not in self.work_root.parts
                and self.work_root.resolve(strict=False) == self.work_root)
            if self.work_root.exists():_directory_identity(self.work_root)
            self.tools.verify()
            if self.native_guardian is not None:
                from .ios_device_guardian import IOSDeviceGuardianTools
                _require(type(self.native_guardian) is IOSDeviceGuardianTools)
                self.native_guardian.verify()
        except (OSError,RuntimeError,TypeError,ValueError):
            raise IOSDeviceToolError() from None

    @property
    def definition_digest(self):
        value = {'kind':'pinned-ios-device-query-v1','tool':str(self.tools.devicectl),
            'toolSha256':self.tools.sha256,'identifier':self.identifier,'udid':self.udid,
            'bundle':self.bundle,'workRoot':str(self.work_root)}
        if self.native_guardian is not None:value['nativeGuardianDigest']=self.native_guardian.definition_digest
        return contracts.digest(value)

    def open_client(self, *, native_owner=None, guardian=None):
        self.verify()
        if guardian is not None:
            from .ios_device_guardian import IOSDeviceGuardianTools
            _require(type(guardian) is IOSDeviceGuardianTools and self.native_guardian is not None
                and guardian.definition_digest == self.native_guardian.definition_digest)
        return PinnedDeviceCtlClient(self.tools,identifier=self.identifier,udid=self.udid,
            bundle=self.bundle,work_root=self.work_root,native_owner=native_owner,guardian=self.native_guardian)

    def open_installer(self, *, native_owner):
        from .ios_mobile_install import IOSMobileInstaller
        return IOSMobileInstaller(self, native_owner)

    def open_runtime_reader(self, *, native_owner):
        from .ios_mobile_runtime_identity import IOSRuntimeIdentityReader
        return IOSRuntimeIdentityReader(self,native_owner)


class PinnedDeviceCtlClient:
    """Query one explicit CoreDevice identity with a pinned tool and clean environment.

    Device pairing, service isolation, native lease inheritance and mobile
    qualification are separate prerequisites for protected mutation/replay.
    This client exposes only selected-device information queries.
    """
    def __init__(self, tools, *, identifier, udid, bundle, work_root,native_owner=None,guardian=None):
        try:
            definition=IOSDeviceQueryDefinition(tools,identifier,udid,bundle,Path(work_root),guardian)
            self._root = definition.work_root
            self._root_identity = _directory_identity(self._root)
            self._tools, self._identifier, self._udid, self._bundle = tools, identifier, udid, bundle
            self._definition_digest = definition.definition_digest
            if native_owner is None and guardian is None:
                self._owner = _ProcessOwner(); self._native_binding_digest = None
            else:
                from .ios_device_guardian import _IOSDeviceQueryGuardian
                self._owner = _IOSDeviceQueryGuardian(definition,native_owner,guardian)
                self._native_binding_digest = self._owner.binding_digest
            self._lock = threading.Lock()
            self._pending = {}
            self._closed = False
        except (OSError, RuntimeError, TypeError, ValueError):
            raise IOSDeviceToolError() from None

    def __repr__(self):
        return f'<PinnedDeviceCtlClient closed={self._closed}>'

    @property
    def definition_digest(self):
        return self._definition_digest

    @property
    def active_processes(self):
        return self._owner.active_processes

    def _bounds(self, cancellation, deadline):
        _require(callable(getattr(cancellation, 'is_set', None))
            and type(deadline) in (int, float) and math.isfinite(deadline)
            and time.monotonic() < deadline and not cancellation.is_set() and not self._closed)
        _require(_directory_identity(self._root) == self._root_identity)
        self._tools.verify()
        if self._native_binding_digest is not None:self._owner.verify()

    def _discard(self, work):
        try:
            _require(_directory_identity(self._root) == self._root_identity
                and _directory_identity(work) == self._pending[work])
            if _cleanup_known_work(self._root, work, {'result.json'}):
                del self._pending[work]
                return True
        except (OSError, RuntimeError):
            pass
        return False

    def _read_result(self, work):
        descriptor = open_regular(work, 'result.json')
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                and before.st_nlink == 1 and 0 < before.st_size <= MAX_FRAME_BYTES)
            body = stream.read(MAX_FRAME_BYTES+1)
            after = os.fstat(stream.fileno())
            _require(len(body) == before.st_size and
                (before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns))
        value = decode_json(body)
        _require(type(value) is dict and type(value.get('info')) is dict
            and value['info'].get('outcome') == 'success' and type(value.get('result')) is dict)
        return value['result']

    def _query(self, kind, cancellation, deadline):
        self._bounds(cancellation, deadline)
        work = self._root/('query-'+secrets.token_hex(16))
        work.mkdir(mode=0o700)
        self._pending[work] = _directory_identity(work)
        arguments = (str(self._tools.devicectl), 'device', 'info', kind, '--device', self._identifier)
        if kind == 'apps':
            arguments += ('--bundle-id', self._bundle)
        arguments += ('--json-output', str(work/'result.json'))
        try:
            result = self._owner.run(arguments, work=work, input_bytes=b'', pass_fds=(),
                cancellation=cancellation, deadline_monotonic=deadline,
                watched_files=((work/'result.json', MAX_FRAME_BYTES),), max_output_bytes=65536)
            _require(result.terminated and result.bounded and not result.interrupted and result.returncode == 0)
            self._bounds(cancellation, deadline)
            _require(_directory_identity(work) == self._pending[work])
            return self._read_result(work)
        finally:
            if self._owner.active_processes == 0:
                _require(self._discard(work), 'ios_device_cleanup_unknown')

    def query(self, kind, *, cancellation, deadline_monotonic):
        acquired = False
        try:
            _require(type(kind) is str and kind in ('details', 'apps', 'processes'))
            acquired = self._lock.acquire(blocking=False)
            _require(acquired, 'ios_device_query_busy')
            _require(not self._pending, 'ios_device_cleanup_unknown')
            details = self._query('details', cancellation, deadline_monotonic)
            hardware = details.get('hardwareProperties')
            _require(type(hardware) is dict and hardware.get('udid') == self._udid
                and hardware.get('deviceType') == 'iPhone' and hardware.get('platform') == 'iOS'
                and details.get('identifier', self._identifier) == self._identifier)
            details = dict(details, identifier=self._identifier)
            value = details if kind == 'details' else self._query(kind, cancellation, deadline_monotonic)
            if kind == 'apps':
                applications = value.get('apps')
                _require(type(applications) is list and len(applications) <= 1
                    and all(type(row) is dict and row.get('bundleIdentifier') == self._bundle
                            for row in applications))
            elif kind == 'processes':
                _require(type(value.get('runningProcesses')) is list
                    and all(type(row) is dict for row in value['runningProcesses']))
            return DeviceCtlObservation(kind, self.definition_digest,
                json.dumps(value, sort_keys=True, separators=(',', ':')),self._native_binding_digest)
        except BaseException as error:
            if not isinstance(error, Exception):
                self._closed = True
                self._owner.close(deadline_monotonic=time.monotonic()+3)
                raise
            if isinstance(error, IOSDeviceToolError):
                raise
            raise IOSDeviceToolError() from None
        finally:
            if acquired:
                self._lock.release()

    def close(self, *, deadline_monotonic=None):
        deadline = time.monotonic()+3 if deadline_monotonic is None else deadline_monotonic
        _require(type(deadline) in (int, float) and math.isfinite(deadline))
        self._closed = True
        stopped = self._owner.close(deadline_monotonic=deadline)
        acquired = self._lock.acquire(timeout=max(0, deadline-time.monotonic()))
        if not acquired:
            return False
        try:
            if stopped:
                for work in tuple(self._pending):
                    self._discard(work)
            return stopped and not self._pending
        finally:
            self._lock.release()
