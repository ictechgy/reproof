"""Fixed Android installation, retained replay and original-app restoration.

Only an enrolled general runtime profile and explicit pinned local tools are
accepted. This adapter does not issue mobile backend qualification.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass,field
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time

from . import contracts
from .android_artifact import stage_apk, verify_apk
from .android_profile import AndroidRuntimeProfile, validate_android_runtime_profile
from .device import AdbDevice, DeviceError
from .execution.artifacts import BlobSet, open_directory, open_regular
from .live.authority import canonical_device_fingerprint
from .live.issue_sessions import FixturePreparation, IssueSessionService
from .live.model import Lab
from .live.recording_session import TrustedProjectRegistration
from .qualification import require_candidate_binding
from .repair_android_signing import AndroidSigningError, _ProcessOwner
from .repair_callbacks import CallbackCancellation
from .repair_mobile import (MobileCleanupObservation, MobileContext,
                            MobileFailureObservation, MobileInstallationObservation,
                            TrustedMobileAdapter)
from .storage import MAX_APK


_MAX_ADB_OUTPUT = 4 * 1024 * 1024
_MAX_TOOL = 64 * 1024 * 1024
NATIVE_TOOL_OWNERSHIP_VERSION = 2
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")


def _require(condition, message="Android mobile boundary rejected"):
    if not condition:
        raise DeviceError(message)


def _active(cancellation, deadline):
    _require(callable(getattr(cancellation, 'is_set', None))
             and type(deadline) in (int, float) and math.isfinite(deadline),
             'Invalid Android operation bounds')
    _require(not cancellation.is_set(), 'Android operation cancelled')
    _require(time.monotonic() < deadline, 'Android operation deadline elapsed')


def _file_sha(path, maximum):
    path = Path(path)
    _require(path.is_absolute(), 'Android file must have an absolute path')
    try:
        descriptor = open_regular(path.parent, path.name)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            _require(before.st_nlink == 1 and 0 < before.st_size <= maximum)
            checksum = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                body = stream.read(min(1024 * 1024, remaining))
                _require(bool(body))
                checksum.update(body)
                remaining -= len(body)
            after = os.fstat(stream.fileno())
            _require((before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink)
                     == (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink))
            return checksum.hexdigest()
    except Exception:
        raise DeviceError('Android file boundary rejected') from None


@dataclass(frozen=True, slots=True)
class AndroidMobileTools:
    adb: Path
    adb_digest: str
    package_inspector: Path
    package_inspector_digest: str
    inspector_support: tuple = field(default=(), init=False, repr=False)

    def __post_init__(self):
        for name in ('adb', 'package_inspector'):
            path = Path(getattr(self, name))
            _require(path.is_absolute(), 'Android tools must have absolute paths')
            # Store the resolved public SDK location once; later changes to
            # that exact location are rejected against the operator's digest.
            object.__setattr__(self, name, path.resolve(strict=True))
            contracts.validate_digest(getattr(self, name + '_digest'))
        support = self.package_inspector.parent/'lib64/libc++.dylib'
        if support.exists() or support.is_symlink():
            _require(support.resolve(strict=True) == support, 'APK inspector support path changed')
            object.__setattr__(self, 'inspector_support', ((support, _file_sha(support, _MAX_TOOL)),))
        self.verify()

    def verify(self):
        for name in ('adb', 'package_inspector'):
            path = getattr(self, name)
            _require(os.access(path, os.X_OK)
                     and _file_sha(path, _MAX_TOOL) == getattr(self, name + '_digest'),
                     'Pinned Android tool changed')
        for path, digest in self.inspector_support:
            _require(_file_sha(path, _MAX_TOOL) == digest, 'Pinned APK inspector support changed')

    @property
    def inspector_support_digest(self):
        return contracts.digest([{'path':str(path),'sha256':digest} for path,digest in self.inspector_support])


class PinnedAdbDevice(AdbDevice):
    """One provider's bounded commands and tracked native helper process."""

    def __init__(self, serial, tools, *, package, work_root, cancellation,
                 deadline_monotonic, timeout=30, endpoint=None, native_dispatcher=None):
        _require(type(tools) is AndroidMobileTools and _PACKAGE.fullmatch(package or ''))
        self.tools = tools
        self.work_root = Path(work_root)
        _require(self.work_root.is_absolute())
        descriptor = open_directory(self.work_root)
        try:
            info = os.fstat(descriptor)
            _require(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
                     'Android tool work directory is not private')
        finally:
            os.close(descriptor)
        self._owner = _ProcessOwner()
        self._persistent = set()
        self._persistent_gateways = {}
        self._client = None
        self._native_dispatcher = native_dispatcher
        self._process_lock = threading.RLock()
        self._bounds_lock = threading.RLock()
        self._closed = False
        self._uncertain = False
        self.set_operation_bounds(cancellation, deadline_monotonic)
        _active(cancellation, deadline_monotonic)
        tools.verify()
        if endpoint is not None:
            from .adb_endpoint import ScopedAdbClient
            self._client = ScopedAdbClient(tools.adb,tools.adb_digest,endpoint,serial=serial,
                work_root=self.work_root,sandbox_sha256=endpoint.sandbox_sha256)
        if native_dispatcher is not None:
            from .android_native_process import _AndroidNativeDispatcher
            _require(type(native_dispatcher) is _AndroidNativeDispatcher and self._client is not None,
                     'Native Android dispatcher requires the scoped SDK client')
        # AdbDevice discovery uses this class's pinned, bounded _command.
        super().__init__(serial=serial, adb=str(tools.adb), timeout=timeout)
        self.package = package

    @property
    def effects_settled(self):
        with self._process_lock:
            return not self._uncertain and not self._persistent and self._owner.active_processes == 0 \
                and (self._client is None or self._client.active_processes == 0) \
                and (self._native_dispatcher is None or self._native_dispatcher.active_processes == 0)

    @property
    def scoped_endpoint_enabled(self):
        return self._client is not None

    def call_live_helper(self,path,body,*,token,timeout,binary=False):
        cancellation,deadline=self._bounds(timeout)
        _require(self._client is not None,'Scoped Android helper transport is unavailable')
        if self._native_dispatcher is not None:
            from .adb_endpoint import AdbEndpointError
            from .repair_android_operation import AndroidOperationError
            try:
                return self._native_dispatcher.call_helper(self._client,path,body,
                    token=token,timeout=timeout,cancellation=cancellation,
                    deadline_monotonic=deadline,binary=binary)
            except AndroidOperationError:
                raise DeviceError('Android helper dispatch failed') from None
            except AdbEndpointError:
                self._uncertain = True
                raise DeviceError('Android helper completion is unconfirmed') from None
        return self._client.call_helper(path,body,token=token,timeout=timeout,cancellation=cancellation,
            deadline_monotonic=deadline,binary=binary)

    def set_operation_bounds(self, cancellation, deadline):
        _require(callable(getattr(cancellation, 'is_set', None))
                 and type(deadline) in (int, float) and math.isfinite(deadline))
        with self._bounds_lock:
            self._operation_cancellation, self._operation_deadline = cancellation, deadline

    @contextmanager
    def cleanup_bounds(self):
        # Cancellation stops forward work. Already-owned native cleanup has
        # its own bounded interval, as it does in the mobile supervisor.
        with self._bounds_lock:
            previous = self._operation_cancellation, self._operation_deadline
            self._operation_cancellation = threading.Event()
            self._operation_deadline = time.monotonic() + 30
        try:
            yield
        finally:
            with self._bounds_lock:
                self._operation_cancellation, self._operation_deadline = previous

    def _bounds(self, timeout=None):
        with self._bounds_lock:
            cancellation, deadline = self._operation_cancellation, self._operation_deadline
        limit = self.timeout if timeout is None else min(self.timeout, timeout)
        _require(type(limit) in (int, float) and math.isfinite(limit) and limit > 0)
        _active(cancellation, deadline)
        _require(not self._closed, 'Android device owner is closed')
        return cancellation, min(deadline, time.monotonic() + limit)

    def _run(self, args, *, timeout=None, input_bytes=b''):
        from .adb_endpoint import AdbEndpointError
        from .repair_android_operation import AndroidOperationError
        cancellation, deadline = self._bounds(timeout)
        _require(args and str(args[0]) in {str(self.tools.adb), str(self.tools.package_inspector)})
        self.tools.verify()
        try:
            if self._client is not None and str(args[0])==str(self.tools.adb):
                command=tuple(map(str,args[1:]))
                if command[:1]==('-s',):
                    _require(len(command)>2 and command[1]==self._client.serial,'Android serial changed')
                    command=command[2:]
                else:_require(command==('devices',),'Unscoped Android request')
                if self._native_dispatcher is not None:
                    result=self._native_dispatcher.run(self._client,command,cancellation=cancellation,
                        deadline_monotonic=deadline,input_bytes=input_bytes)
                else:
                    result=self._client.run(command,cancellation=cancellation,deadline_monotonic=deadline,input_bytes=input_bytes)
            elif self._native_dispatcher is not None:
                _require(len(args)==4 and tuple(args[1:3])==('dump','badging') and input_bytes==b'',
                         'Unpinned APK inspector command')
                result=self._native_dispatcher.inspect_apk(Path(args[3]),cancellation=cancellation,
                                                          deadline_monotonic=deadline)
            else:
                result = self._owner.run(tuple(map(str, args)), work=self.work_root,
                    input_bytes=input_bytes, pass_fds=(), cancellation=cancellation,
                    deadline_monotonic=deadline, max_output_bytes=_MAX_ADB_OUTPUT)
        except (AndroidSigningError,AdbEndpointError,AndroidOperationError):
            if self._owner.active_processes or self._client is not None and self._client.active_processes:
                self._uncertain = True
            raise DeviceError('Android tool dispatch failed') from None
        if not result.terminated or not result.bounded or result.interrupted:
            self._uncertain = True
            raise DeviceError('Android tool completion is unconfirmed')
        _active(cancellation, deadline)
        _require(result.returncode == 0, 'Android tool rejected the operation')
        return result.stdout

    def _command(self, args, timeout=None):
        _require(args and str(args[0]) == str(self.tools.adb), 'Unpinned Android command')
        return self._run(args, timeout=timeout)

    def apk_identity(self, apk):
        data = self._run([self.tools.package_inspector, 'dump', 'badging', str(apk)])
        lines = re.findall(rb"^package: name='([^']+)' versionCode='([0-9]+)'[^\r\n]*$", data, re.M)
        _require(len(lines) == 1, 'APK identity is unavailable')
        package = lines[0][0].decode('ascii')
        _require(_PACKAGE.fullmatch(package) is not None)
        return {'package': package, 'versionCode': int(lines[0][1])}

    def installation_proof(self, apk, package=None):
        package = self.package if package is None else package
        _require(package == self.package, 'Package differs from the selected application')
        identity = self.apk_identity(apk)
        _require(identity['package'] == package)
        lines = self.shell('pm', 'path', package).splitlines()
        paths = [line[8:] for line in lines if line.startswith('package:')]
        _require(len(paths) == len(lines) == 1 and paths[0].startswith('/data/app/')
                 and paths[0].endswith('.apk'), 'Expected one installed APK')
        fields = self.shell('sha256sum', paths[0]).split()
        expected = _file_sha(Path(apk), MAX_APK)
        _require(len(fields) == 2 and fields[0] == expected, 'Installed APK differs')
        return {'package': package, 'versionCode': identity['versionCode'],
                'apkSha256': expected, 'installedVerified': True}

    def write_live_configuration(self, command, body):
        _require(type(body) is bytes and len(body) <= 64 * 1024)
        self._run([self.tools.adb, '-s', self.serial, 'shell', '-T', command],
                  timeout=10, input_bytes=body)

    def start_live_instrumentation(self, helper_package):
        from .live.android_live import HELPER
        _require(helper_package == HELPER)
        cancellation, deadline = self._bounds()
        self.tools.verify()
        with self._process_lock:
            _active(cancellation, deadline)
            _require(not self._closed)
            if self._native_dispatcher is not None:
                with self._bounds_lock:
                    persistent_deadline = self._operation_deadline
                process = self._native_dispatcher.start(self._client,
                    ('shell', 'am instrument -w -r '+HELPER+'/.LiveInstrumentation'),
                    cancellation=cancellation, deadline_monotonic=persistent_deadline)
                self._persistent.add(process)
                return process
            gateway=None
            try:
                command=[str(self.tools.adb), '-s', self.serial, 'shell',
                         'am instrument -w -r ' + HELPER + '/.LiveInstrumentation']
                if self._client is not None:
                    command,gateway=self._client.prepare_command(('shell','am instrument -w -r '+HELPER+'/.LiveInstrumentation'))
                process = subprocess.Popen(
                    command,
                    cwd=self.work_root, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                         'TMPDIR': str(self.work_root)},
                    start_new_session=True, close_fds=True)
            except OSError:
                if gateway is not None:self._client.finish_gateway(gateway)
                raise DeviceError('Android helper could not start') from None
            self._persistent.add(process)
            if gateway is not None:self._persistent_gateways[process]=gateway
            return process

    def collect_live_instrumentation(self, process):
        _require(process in self._persistent, 'Unknown Android helper owner')
        if self._native_dispatcher is not None:
            clean = self._native_dispatcher.collect(process)
            if process.poll() is not None:
                with self._process_lock:
                    self._persistent.discard(process)
            if not clean:
                self._uncertain = True
            return clean
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            # A forced local stop is never a native helper cleanup receipt.
            _ProcessOwner._terminate(process)
            self._uncertain = True
        clean = process.poll() is not None and _ProcessOwner._group_empty(process.pid)
        if clean:
            with self._process_lock:
                self._persistent.discard(process)
                gateway=self._persistent_gateways.pop(process,None)
                if gateway is not None:
                    try:self._client.finish_gateway(gateway)
                    except RuntimeError:self._uncertain=True;clean=False
        else:
            self._uncertain = True
        return clean and process.returncode == 0

    def close(self, *, deadline_monotonic):
        self._closed = True
        if not self._process_lock.acquire(timeout=max(0, deadline_monotonic - time.monotonic())):
            return False
        try:
            for process in tuple(self._persistent):
                if process.poll() is None:
                    self._uncertain = True
                clean = (self._native_dispatcher.stop(process, deadline_monotonic)
                         if self._native_dispatcher is not None else
                         _ProcessOwner._terminate(process, deadline_monotonic=deadline_monotonic))
                if clean:
                    self._persistent.discard(process)
                    gateway=self._persistent_gateways.pop(process,None)
                    if gateway is not None:
                        try:self._client.finish_gateway(gateway)
                        except RuntimeError:self._uncertain=True
        finally:
            self._process_lock.release()
        client_clean=self._client is None or self._client.close(deadline_monotonic=deadline_monotonic)
        return self._owner.close(deadline_monotonic=deadline_monotonic) and not self._persistent and client_clean


@dataclass(frozen=True, slots=True)
class AndroidMobileAdapterConfig:
    lab: Lab
    service: IssueSessionService
    registration: TrustedProjectRegistration
    device_id: str
    owner: str
    original_profile: AndroidRuntimeProfile
    original_apk: Path
    helper_apk: Path
    helper_digest: str
    preparations: tuple[FixturePreparation, ...]
    serial: str
    tools: AndroidMobileTools
    runtime_policy_digest: str
    adb_endpoint: object = field(default=None,repr=False)
    native_guardian: object = field(default=None,repr=False)

    @property
    def application_id(self):
        return self.original_profile.data['applicationId']

    @property
    def original_build_id(self):
        return self.original_profile.data['buildId']

    @property
    def package(self):
        return self.original_profile.package

    @property
    def scope_digest(self):
        return canonical_device_fingerprint('android', self.serial)

    def __post_init__(self):
        _require(type(self.lab) is Lab and type(self.service) is IssueSessionService
                 and type(self.registration) is TrustedProjectRegistration)
        _require(self.service.lab is self.lab and self.service.runner is not None
                 and self.service.runner.lab is self.lab
                 and self.service.registry is self.service.runner.registry)
        _require(type(self.original_profile) is AndroidRuntimeProfile
                 and type(self.tools) is AndroidMobileTools,
                 'A general Android profile and pinned tools are required')
        contracts.validate_id(self.device_id)
        contracts.validate_id(self.owner)
        contracts.validate_digest(self.runtime_policy_digest)
        contracts.validate_digest(self.helper_digest)
        _require(type(self.serial) is str and 0 < len(self.serial) <= 256
                 and not any(char.isspace() or ord(char) < 32 for char in self.serial))
        for name in ('original_apk', 'helper_apk'):
            path = Path(getattr(self, name))
            _require(path.is_absolute() and path.suffix == '.apk')
            object.__setattr__(self, name, path)
        _require(type(self.preparations) is tuple and len(self.preparations) <= 128
                 and all(type(item) is FixturePreparation for item in self.preparations))
        self.validate()

    def validate(self):
        if self.adb_endpoint is not None:
            from .adb_endpoint import AdbEndpoint
            _require(type(self.adb_endpoint) is AdbEndpoint and self.adb_endpoint.sandbox_sha256 is not None,
                     'An explicitly pinned ADB endpoint is required')
            self.adb_endpoint.verify()
        if self.native_guardian is not None:
            from .android_native_process import AndroidGuardianTools
            _require(type(self.native_guardian) is AndroidGuardianTools and self.adb_endpoint is not None,
                     'Native Android execution requires pinned guardian tools and an explicit endpoint')
            self.native_guardian.verify()
        profile = self.original_profile.data
        _require(profile['projectDigest'] == self.registration.project_digest
                 and profile['projectId'] == self.registration.project['id'])
        device = self.lab.devices.get(self.device_id)
        _require(device is not None and device.get('kind') == 'android-live'
                 and device.get('_authority', {}).get('deviceKind') == 'android'
                 and device.get('_authority', {}).get('physicalId') == self.serial)
        capabilities = device.get('capabilities', {})
        _require(capabilities.get('applicationProfile') == profile
                 and capabilities.get('applicationProfileDigest') == self.original_profile.digest
                 and capabilities.get('applicationIdentity') == self.original_profile.application_identity,
                 'Original Android profile differs from the enrolled application')
        self.lab._validate_release_selection(self.device_id, self.registration,
            self.application_id, self.original_build_id)
        self.service._checked_preparations(self.preparations, self.registration, self.application_id)
        artifact = profile['artifact']
        verify_apk(self.original_apk, expected_digest=artifact['sha256'], expected_bytes=artifact['bytes'])
        _require(_file_sha(self.helper_apk, MAX_APK) == self.helper_digest, 'Android helper changed')
        self.tools.verify()


class _OwnerCancellation(CallbackCancellation):
    def __init__(self, parent, owner_stopped):
        super().__init__(parent)
        self.owner_stopped = owner_stopped

    def is_set(self):
        return self.owner_stopped.is_set() or super().is_set()


class AndroidTrustedMobileAdapter:
    def __init__(self, config, *, operations=None):
        _require(type(config) is AndroidMobileAdapterConfig)
        _require(config.native_guardian is None or operations is not None,
                 'Native Android execution requires the durable operation store')
        if operations is not None:
            from .repair_android_operation import AndroidOperationStore
            _require(type(operations) is AndroidOperationStore and operations.config is config)
        self.config = config
        self.operations = operations
        self._native_binding = None
        self._native_dispatcher = None
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._stopping = threading.Event()
        self._phase = None
        self._context = None
        self._scope = None
        self._temporary = None
        self._candidate_path = None
        self._original_path = None
        self._helper_path = None
        self._candidate_profile = None
        self._installed = False
        self._devices = []
        self._issues = {}
        self._completed_contexts = set()

    def trusted_adapter(self, *, adapter_id):
        return TrustedMobileAdapter(adapter_id, self.config.scope_digest,
            self.config.device_id, self.config.registration.project_digest,
            self.config.runtime_policy_digest, self.install, self.replay, self.cleanup)

    def _check_context(self, context):
        _require(type(context) is MobileContext)
        contracts.validate_id(context.operation_id)
        for name in ('request_digest', 'repair_plan_digest', 'project_digest',
                     'source_digest', 'artifact_digest', 'scope_digest', 'runtime_policy_digest'):
            contracts.validate_digest(getattr(context, name))
        _require(context.project_digest == self.config.registration.project_digest
                 and context.application_id == self.config.application_id
                 and context.scope_digest == self.config.scope_digest
                 and context.runtime_policy_digest == self.config.runtime_policy_digest)
        if self.operations is not None:
            self.operations.require_operation(context._operation_binding, context)

    @contextmanager
    def _callback(self, context, phase):
        self._check_context(context)
        with self._lock:
            _require(self._phase is None, 'An Android callback is still active')
            _require(phase == 'cleanup' or not self._stopping.is_set(), 'Android adapter is closed')
            if phase == 'install' or (phase == 'cleanup' and self.operations is not None
                                      and self._context is None):
                _require(self._context is None and context.digest not in self._completed_contexts
                         and len(self._completed_contexts) < 65536,
                         'Android context cannot be admitted again')
                self._context = context
            else:
                _require(self._context is not None and self._context.digest == context.digest,
                         'Android context differs from the active owner')
            self._phase = phase
        try:
            yield
        finally:
            with self._lock:
                self._phase = None
                self._changed.notify_all()

    def _stage(self, context, artifacts):
        _require(type(artifacts) is BlobSet and len(artifacts.entries) == 1)
        name, body = artifacts.entries[0]
        _require(name == 'candidate.apk' and 0 < len(body) <= MAX_APK
                 and hashlib.sha256(body).hexdigest() == context.artifact_digest)
        if self.operations is not None:
            operation = self.operations.require_operation(context._operation_binding, context)
            self._candidate_path, self._original_path, self._helper_path = (
                operation.candidate_path, operation.original_path, operation.helper_path)
            return
        self._temporary = tempfile.TemporaryDirectory(prefix='repro-android-candidate-')
        root = Path(self._temporary.name).resolve()
        self._candidate_path = root / 'candidate.apk'
        with os.fdopen(os.open(self._candidate_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                               0o600), 'wb') as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        artifact = self.config.original_profile.data['artifact']
        self._original_path = stage_apk(self.config.original_apk, root / 'original.apk',
            expected_digest=artifact['sha256'], expected_bytes=artifact['bytes'])
        self._helper_path = stage_apk(self.config.helper_apk, root / 'helper.apk',
            expected_digest=self.config.helper_digest, expected_bytes=self.config.helper_apk.stat().st_size)

    def _new_device(self, cancellation, deadline):
        # Register before discovery so failed construction cannot lose a tool owner.
        device = PinnedAdbDevice.__new__(PinnedAdbDevice)
        self._devices.append(device)
        device.__init__(self.config.serial, self.config.tools, package=self.config.package,
                        work_root=self._candidate_path.parent, cancellation=cancellation,
                        deadline_monotonic=deadline,endpoint=self.config.adb_endpoint,
                        native_dispatcher=self._native_dispatcher)
        return device

    def _provider(self, apk, profile, device):
        from .live.android_live import AndroidLiveProvider
        return AndroidLiveProvider(self.config.serial, self._helper_path, apk,
                                   runtime_profile=profile, _device=device)

    def _candidate_build_id(self, context):
        return 'candidate_' + contracts.digest({'operation': context.operation_id,
                                               'request': context.request_digest})[:32]

    def _derive_candidate_profile(self, context, device):
        identity = device.apk_identity(self._candidate_path)
        _require(identity['package'] == self.config.package, 'Candidate APK package differs')
        data = self.config.original_profile.data
        data['buildId'] = self._candidate_build_id(context)
        data['artifact'].update(sha256=context.artifact_digest,
            bytes=self._candidate_path.stat().st_size, versionCode=identity['versionCode'],
            provenanceDigest=contracts.digest({'kind': 'protected-mobile-candidate',
                                               'contextDigest': context.digest}))
        self._candidate_profile = validate_android_runtime_profile(data)

    def _scope_effect(self, kind, payload):
        return self.config.lab.prepare_retained_scope_effect(self._scope,
            owner=self.config.owner, kind=kind, payload=payload,
            provider_incarnation='provider_android_scope')

    def _confirm(self, permit, evidence):
        handle = self._scope._reservation._authority_handle
        handle.check_dispatch_permit(permit)
        self.config.lab.confirm_retained_scope_effect(self._scope, permit,
            owner=self.config.owner, status='succeeded', result_digest=contracts.digest(evidence))

    def _scope_install(self, provider, apk, kind):
        artifact = provider.app_profile.data['artifact']
        verify_apk(apk, expected_digest=artifact['sha256'], expected_bytes=artifact['bytes'])
        handle = self._scope._reservation._authority_handle
        provider.bind_authority(handle, 'provider_android_scope')
        permit = self._scope_effect(kind, {'package': self.config.package,
                                          'artifactDigest': artifact['sha256']})
        provider._install(apk, self.config.package, permit)
        proof = provider.device.installation_proof(apk, self.config.package)
        _require(proof['apkSha256'] == artifact['sha256'] and proof['installedVerified'] is True)
        self._confirm(permit, {'kind': kind, 'proof': proof})
        return proof

    def _settled(self):
        try:
            return all(device.effects_settled for device in self._devices)
        except (AttributeError, RuntimeError):
            return False

    def _failure(self, context, code):
        settled = self._settled()
        return MobileFailureObservation(context.digest, code,
            contracts.digest({'contextDigest': context.digest, 'code': code,
                              'effectsSettled': settled}), settled)

    @contextmanager
    def _journal_phase(self, context, phase, replay_number=None):
        if self.operations is None:
            yield None
        else:
            _require(self._scope is None or self._native_binding is not None,
                     'Android native owner was not durably bound')
            with self.operations.phase(context._operation_binding, context,
                    self._native_binding, phase, replay_number) as capability:
                if self.config.native_guardian is not None and self._scope is not None:
                    from .android_native_process import native_dispatcher
                    handle = self._scope._reservation._authority_handle
                    with self.operations.borrow_native_descriptors(context._operation_binding, context,
                            self._native_binding, capability, handle) as descriptors:
                        with native_dispatcher(self.operations, descriptors, self.config.native_guardian) as dispatcher:
                            self._native_dispatcher = dispatcher
                            try:
                                yield capability
                            finally:
                                self._native_dispatcher = None
                else:
                    yield capability

    def _phase_result(self, capability, result):
        if capability is not None:
            digest = (result.evidence_digest if hasattr(result, 'evidence_digest')
                      else contracts.digest(result.public()))
            self.operations.complete_phase(capability, digest)
        return result

    def install(self, context, artifacts, *, cancellation, deadline_monotonic):
        with self._callback(context, 'install'):
            cancellation = _OwnerCancellation(cancellation, self._stopping)
            try:
                _active(cancellation, deadline_monotonic)
                self.config.validate()
                self._stage(context, artifacts)
                _active(cancellation, deadline_monotonic)
                self._scope = self.config.lab.begin_retained_device_scope(
                    self.config.device_id, self.config.owner,
                    'reservation_' + contracts.digest(context.operation_id)[:32],
                    self.config.registration, application_id=self.config.application_id,
                    build_id=self.config.original_build_id)
                if self.operations is not None:
                    handle = self._scope._reservation._authority_handle
                    self._native_binding = self.operations.bind_native(context._operation_binding, context,
                        ownership_generation=handle.generation,
                        host_incarnation=handle._authority.host_incarnation,
                        helper_incarnation=handle.helper_incarnation,
                        provider_incarnation='provider_android_scope')
                with self._journal_phase(context, 'install') as capability:
                    try:
                        device = self._new_device(cancellation, deadline_monotonic)
                        self._derive_candidate_profile(context, device)
                        provider = self._provider(self._candidate_path, self._candidate_profile, device)
                        proof = self._scope_install(provider, self._candidate_path, 'install_candidate')
                        _active(cancellation, deadline_monotonic)
                        self._installed = True
                        result = MobileInstallationObservation(context.digest, context.artifact_digest,
                            context.application_id, context.scope_digest, contracts.digest(proof), True)
                    except Exception:
                        result = self._failure(context, 'mobile_install_failed')
                    return self._phase_result(capability, result)
            except Exception:
                return self._failure(context, 'mobile_install_failed')

    def replay(self, context, execution, number, *, cancellation, deadline_monotonic):
        with self._callback(context, 'replay'), self._journal_phase(context, 'replay', number) as capability:
            cancellation = _OwnerCancellation(cancellation, self._stopping)
            _active(cancellation, deadline_monotonic)
            _require(self._installed and self._scope is not None)
            _require(type(number) is int and number == len(self._issues) + 1 and number <= 128,
                     'Candidate replay order changed')
            execution = self.config.service.registry.require_execution(execution)
            build = require_candidate_binding(execution.candidate_binding,
                project_digest=context.project_digest, application_id=context.application_id,
                build_id=self._candidate_build_id(context))
            _require(build['artifactDigest'] == context.artifact_digest
                     and build['sourceDigest'] == context.source_digest
                     and execution.approved.runtime_policy_digest == context.runtime_policy_digest)
            profile_data = self._candidate_profile.data
            identity = self._candidate_profile.application_identity
            self.config.lab.validate_retained_device_scope(self._scope,
                owner=self.config.owner, device_id=self.config.device_id,
                registration=self.config.registration, application_id=context.application_id,
                build_id=build['id'], candidate_binding=execution.candidate_binding,
                _candidate_identity=identity, _candidate_profile=profile_data)
            device = self._new_device(cancellation, deadline_monotonic)
            provider = self._provider(self._candidate_path, self._candidate_profile, device)
            issue_id = 'mobile_' + contracts.digest({'context': context.digest, 'attempt': number})[:40]
            self._issues[number] = issue_id
            try:
                result = self.config.service.replay(execution, registration=self.config.registration,
                    device_id=self.config.device_id, owner=self.config.owner,
                    controller_id='candidate_' + str(number), preparations=self.config.preparations,
                    cancellation=cancellation, timeout_seconds=max(.001, deadline_monotonic - time.monotonic()),
                    issue_id=issue_id, device_scope=self._scope, _candidate_identity=identity,
                    _candidate_profile=profile_data, _provider_factory=lambda: provider)
                _active(cancellation, deadline_monotonic)
                return self._phase_result(capability, result)
            except Exception:
                return self._phase_result(capability, self._failure(context, 'mobile_replay_failed'))

    def _issues_clean(self):
        try:
            for issue_id in self._issues.values():
                record = self.config.service.get(issue_id)
                if (record.get('deviceCleanup') != 'complete'
                        or any(item.get('status') != 'complete' for item in record.get('cleanup', []))):
                    return False
            return True
        except Exception:
            return False

    def _sanitize(self, provider):
        permit = self._scope_effect('sanitize_original', {'package': self.config.package, 'clear': True})
        handle = self._scope._reservation._authority_handle
        device = provider.device
        for args in (('am', 'force-stop', self.config.package), ('pm', 'clear', self.config.package)):
            handle.check_dispatch_permit(permit)
            result = device.shell(*args)
            if args[0] == 'pm':
                _require(result.strip() == 'Success', 'Original application data clear was not confirmed')
        handle.check_dispatch_permit(permit)
        names = device.shell('ps', '-A', '-o', 'NAME').splitlines()
        _require(names and names[0].strip() == 'NAME', 'Android process snapshot is unavailable')
        _require(not any(name.strip() == self.config.package
                         or name.strip().startswith(self.config.package + ':') for name in names[1:]),
                 'An application process is still running')
        self._confirm(permit, {'kind': 'sanitize_original', 'package': self.config.package,
                               'dataCleared': True, 'targetProcessAbsent': True})

    def cleanup(self, context, *, cancellation, deadline_monotonic):
        _require(type(context) is MobileContext)
        evidence = {'restored': False, 'fixtures': False, 'processes': False, 'scopeReleased': False}
        completed = False
        try:
            self._check_context(context)
            with self._callback(context, 'cleanup'), self._journal_phase(context, 'cleanup') as capability:
                _active(cancellation, deadline_monotonic)
                evidence['fixtures'] = self._issues_clean()
                local_closed = True
                for device in self._devices:
                    local_closed = device.close(deadline_monotonic=deadline_monotonic) and local_closed
                _require(local_closed, 'An Android tool owner remains active')
                _require(evidence['fixtures'] and self._settled(), 'Android producers or fixtures remain unsettled')
                if self._scope is not None:
                    self.config.lab.validate_retained_device_scope(self._scope,
                        owner=self.config.owner, device_id=self.config.device_id)
                    device = self._new_device(cancellation, deadline_monotonic)
                    provider = self._provider(self._original_path, self.config.original_profile, device)
                    self._scope_install(provider, self._original_path, 'restore_original')
                    self._sanitize(provider)
                evidence['restored'] = True
                for device in self._devices:
                    _require(device.close(deadline_monotonic=deadline_monotonic),
                             'Android tool owner did not close')
                evidence['processes'] = self._settled()
                _require(evidence['processes'])
                if self._temporary is not None:
                    self._temporary.cleanup()
                    self._temporary = None
                if self.operations is not None:
                    evidence['stagedDiscardDigest'] = self.operations.discard_staged(
                        context._operation_binding, capability)
                if self._scope is not None:
                    self.config.service.release_retained_device_scope(self._scope, owner=self.config.owner)
                evidence['scopeReleased'] = True
                self._phase_result(capability, MobileCleanupObservation(context.digest,
                    contracts.digest(evidence), True, True, True, True))
                with self._lock:
                    self._completed_contexts.add(context.digest)
                    self._context = self._scope = None
                    self._native_binding = None
                    self._candidate_path = self._original_path = self._helper_path = None
                    self._candidate_profile = None
                    self._installed = False
                    self._devices.clear(); self._issues.clear()
            completed = True
        except Exception:
            pass
        return MobileCleanupObservation(context.digest, contracts.digest(evidence),
            evidence['processes'], evidence['fixtures'], evidence['restored'],
            evidence['scopeReleased'] and completed)

    def revoke(self):
        self._stopping.set()

    def close(self, *, deadline_monotonic=None):
        deadline = time.monotonic() + 10 if deadline_monotonic is None else deadline_monotonic
        _require(type(deadline) in (int, float) and math.isfinite(deadline))
        self.revoke()
        with self._changed:
            while self._phase is not None and time.monotonic() < deadline:
                self._changed.wait(max(0, deadline - time.monotonic()))
            if self._phase is not None:
                return False
            context = self._context
        if context is None:
            return (self.operations is None
                    or self.operations.close(deadline_monotonic=deadline))
        result = self.cleanup(context, cancellation=threading.Event(), deadline_monotonic=deadline)
        clean = all((result.termination_confirmed, result.fixture_cleanup_confirmed,
                     result.sanitation_confirmed, result.ownership_released))
        return clean and (self.operations is None
                          or self.operations.close(deadline_monotonic=deadline))


__all__ = ['AndroidMobileTools', 'AndroidMobileAdapterConfig', 'AndroidTrustedMobileAdapter', 'PinnedAdbDevice']
