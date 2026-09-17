"""Fixed, journaled XCTest helper lifetime; no replay or installed-artifact proof."""
from contextlib import ExitStack
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import plistlib
import re
import secrets
import select
import stat
import subprocess
import sys
import threading
import time
import uuid

from . import contracts
from .ios_device_guardian import IOSDeviceGuardianTools,_IOSProcessOwner
from .ios_device_tools import IOSDeviceQueryDefinition, IOSDeviceTools, IOSDeviceToolError, _require
from .ios_code_signature import _remove_owned_contents
from .ios_mobile_native import IOSMobileNativeOwner, prepared_app, require_native_dispatch
from .ios_mobile_operation import MAX_XCTEST_ITERATIONS, XCTEST_WORK_BYTES
from .ios_xctest_template import IOSXCTestTemplate
from .live.clock_sync import SuspendInclusiveClock
from .repair_android_operation import (_identity_info, _open_child_directory, _open_regular_at,
    _read_fd, _read_json_at, _replace_at, _write_new_at)
from .repair_android_signing import _Collector


@dataclass(frozen=True, slots=True)
class IOSXCTestTools:
    xcodebuild: Path = field(repr=False)
    sha256: str
    developer_root: Path = field(repr=False)
    guardian: IOSDeviceGuardianTools = field(repr=False)
    template: IOSXCTestTemplate = field(repr=False)
    port: int = 8765

    def __post_init__(self):
        object.__setattr__(self,'xcodebuild',Path(self.xcodebuild))
        object.__setattr__(self,'developer_root',Path(self.developer_root))
        self.verify()

    def verify(self):
        try:
            _require(type(self.guardian) is IOSDeviceGuardianTools and type(self.template) is IOSXCTestTemplate
                and type(self.port) is int
                and 1024 <= self.port <= 65535 and self.developer_root.is_absolute()
                and self.developer_root.resolve(strict=True) == self.developer_root
                and self.xcodebuild == self.developer_root/'usr/bin/xcodebuild')
            info = self.developer_root.stat()
            _require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0,os.getuid()) and not info.st_mode & 0o022)
            IOSDeviceTools(self.xcodebuild,self.sha256).verify()
            self.guardian.verify()
            self.template.verify()
        except Exception:
            raise IOSDeviceToolError() from None

    @property
    def definition_digest(self):
        return contracts.digest({'kind':'ios-fixed-xctest-v1','xcodebuild':str(self.xcodebuild),
            'sha256':self.sha256,'developerRoot':str(self.developer_root),
            'guardianDigest':self.guardian.definition_digest,'templateDigest':self.template.definition_digest,'port':self.port})


@dataclass(frozen=True, slots=True, repr=False)
class IOSXCTestLaunch:
    _runner: object
    _work: Path
    _identity: str
    _configuration: bytes
    _document: str
    _endpoint: str
    _token: str

    def __repr__(self): return '<IOSXCTestLaunch>'

    @property
    def payload(self): return json.loads(self._document)


def _bounds(owner, cancellation, deadline, *, role='candidate'):
    owner._check()
    _require(callable(getattr(cancellation,'is_set',None)) and type(deadline) in (int,float)
        and math.isfinite(deadline) and time.monotonic() < deadline and not cancellation.is_set())
    if owner._recovery_context is None and owner.operation.run.cancelled():
        from .ios_mobile_callbacks import require_cleanup_callback
        _require(role=='original')
        require_cleanup_callback(owner)


class IOSXCTestRunner:
    def __init__(self, tools, query, native_owner):
        self.tools, self.query, self.native_owner = tools, query, native_owner
        self._closed = False
        self._launches = {}
        self._started = set()
        self._dispatches = {}
        self._sessions = set()
        self._lock = threading.RLock()
        self._verify()
        with native_owner.operations._changed:
            native_owner._check()
            native_owner.operations._native_clients.add(self)

    def __repr__(self): return '<IOSXCTestRunner>'

    def _verify(self):
        try:
            _require(not self._closed and type(self.tools) is IOSXCTestTools
                and type(self.query) is IOSDeviceQueryDefinition and type(self.native_owner) is IOSMobileNativeOwner)
            owner = self.native_owner
            owner._check(); self.tools.verify(); self.query.verify()
            _require(owner.operations.definition.xctest_definition_digest == self.tools.definition_digest
                and owner.operations.definition.query_definition_digest == self.query.definition_digest
                and type(self.query.native_guardian) is IOSDeviceGuardianTools
                and self.query.native_guardian.definition_digest == self.tools.guardian.definition_digest
                and self.query.udid == owner.operations.definition.udid
                and self.query.bundle == owner.operations.definition.bundle_id
                and set(owner.operations._roles) == {'original','candidate','helper-host','helper-runner'}
                and len({owner.operations._roles[name] for name in ('candidate','helper-host','helper-runner')}) == 3
                and sys.platform == 'darwin'
                and type(owner.device._authority.clock_sync._clock) is SuspendInclusiveClock)
        except Exception:
            raise IOSDeviceToolError() from None

    @property
    def active_processes(self):
        with self._lock: return sum(session.active_processes for session in self._sessions)

    def _checked_apps(self, role):
        apps = {selected:prepared_app(self.native_owner,selected) for selected in (role,'helper-host','helper-runner')}
        runner = apps['helper-runner']
        _require(runner.manifest['applicationId'] == self.tools.template.runner_bundle_identifier)
        path = 'PlugIns/ReproLiveTests.xctest'
        _require(sum(row['bundlePath'] == path and row['kind'] == 'xctest'
            for row in runner.manifest['codeObjects']) == 1)
        directory = os.open(runner._source/path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        descriptor = None
        try:
            descriptor = _open_regular_at(directory,'Info.plist')
            info = plistlib.loads(_read_fd(descriptor,128*1024))
        finally:
            if descriptor is not None:os.close(descriptor)
            os.close(directory)
        _require(type(info.get('ReproLiveProtocolVersion')) is int and info['ReproLiveProtocolVersion'] == 2
            and type(info.get('ReproLiveHelperVersion')) is int and info['ReproLiveHelperVersion'] == 2)
        return apps

    def prepare(self, *, role, iteration, application_id, profile_digest, actions, cancellation, deadline_monotonic):
        acquired = False
        try:
            self._verify(); owner = self.native_owner
            _bounds(owner,cancellation,deadline_monotonic,role=role)
            _require(type(role) is str and role in ('candidate','original') and type(iteration) is int
                and 1 <= iteration <= MAX_XCTEST_ITERATIONS and application_id == owner.operations.definition.application_id
                and type(actions) is tuple and 1 <= len(actions) <= 7 and len(set(actions)) == len(actions)
                and set(actions) <= {'tap','long_press','swipe','text','home','launch','terminate'})
            contracts.validate_digest(profile_digest)
            acquired = owner._command_lock.acquire(blocking=False); _require(acquired)
            apps = self._checked_apps(role)
            client = self.query.open_client(native_owner=owner)
            try: details = client.query('details',cancellation=cancellation,deadline_monotonic=deadline_monotonic).data
            finally: _require(client.close())
            address = details.get('connectionProperties',{}).get('tunnelIPAddress')
            parsed = ipaddress.IPv6Address(address)
            connection = details.get('connectionProperties',{})
            _require(type(address) is str and str(parsed) == address and parsed.is_private
                and not (parsed.is_unspecified or parsed.is_loopback or parsed.is_multicast)
                and connection.get('pairingState') == 'paired' and connection.get('transportType') == 'wired'
                and connection.get('tunnelState') == 'connected')
            name = f'command-xctest-{role}-{iteration:03}-work'
            _require(name not in owner._command_attempts and name not in os.listdir(owner.command_directory))
            owner._command_attempts.add(name)
            os.mkdir(name,mode=0o700,dir_fd=owner.command_directory);os.fsync(owner.command_directory)
            work = owner.command_root/name
            directory = _open_child_directory(owner.command_directory,name)
            token = secrets.token_urlsafe(32)
            try:
                identity = _identity_info(os.fstat(directory))
                os.mkdir('home',mode=0o700,dir_fd=directory)
                environment = {'REPRO_LIVE_LISTEN_HOST':address,'REPRO_LIVE_LISTEN_PORT':str(self.tools.port),
                        'REPRO_LIVE_TOKEN':token,'REPRO_TARGET_BUNDLE':self.query.bundle,
                        'REPRO_LIVE_GENERAL_PROFILE_DIGEST':profile_digest,'REPRO_LIVE_APPLICATION_ID':application_id,
                        'REPRO_LIVE_GENERAL_ACTIONS':','.join(actions),'REPRO_LIVE_PROTOCOL_VERSION':'2',
                        'REPRO_LIVE_HELPER_VERSION':'2','REPRO_LIVE_HELPER_INCARNATION':owner.helper_incarnation,
                        'REPRO_LIVE_HOST_INCARNATION':owner.device._authority.host_incarnation}
                # Derive the provider incarnation before the caller requests its permit.
                payload = {'kind':'ios-fixed-xctest-launch-v1','role':role,'iteration':iteration,
                    'contextDigest':owner.operation.context.digest,'nativeBindingDigest':owner.binding_digest,
                    'projectDigest':owner.operations.definition.project_digest,'scopeDigest':owner.operations.definition.scope_digest,
                    'definitionDigest':self.tools.definition_digest,'queryDefinitionDigest':self.query.definition_digest,
                    'applicationId':application_id,'profileDigest':profile_digest,'actions':list(actions),
                    'appDigests':{name:app.app_digest for name,app in apps.items()},
                    'endpointDigest':contracts.digest({'address':address,'port':self.tools.port,'token':token})}
                from .ios_instrumentation import profile_from_app
                automatic = profile_from_app(apps[role]._source)
                info = plistlib.loads((apps[role]._source/'Info.plist').read_bytes())
                if automatic is not None and type(info.get('ReproRuntimeIdentitySchemaVersion')) is int \
                        and info['ReproRuntimeIdentitySchemaVersion'] in (1,2):
                    build_id = info.get('ReproBuildID')
                    _require(type(build_id) is str and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',build_id))
                    runtime = {'bundleId':self.query.bundle,'buildId':build_id,
                        'profileDigest':automatic.digest,'runId':str(uuid.uuid4())}
                    payload['runtimeIdentity'] = runtime
                    environment['REPRO_LIVE_AUTO_RUN_ID'] = runtime['runId']
                    environment['REPRO_LIVE_AUTO_PROFILE_DIGEST'] = runtime['profileDigest']
                from .ios_sanitation import policy_from_app
                sanitation=policy_from_app(apps[role]._source)
                expected_sanitation=owner.operations.definition.sanitation_policy_digest
                _require((sanitation.digest if sanitation is not None else None) == expected_sanitation)
                if sanitation is not None:
                    _require('runtimeIdentity' in payload and info['ReproRuntimeIdentitySchemaVersion']==2)
                    payload['runtimeIdentity']['sanitationPolicyDigest']=sanitation.digest
                    environment['REPRO_LIVE_SANITATION_POLICY_DIGEST']=sanitation.digest
                environment['REPRO_LIVE_PROVIDER_INCARNATION'] = 'ios-xctest-'+contracts.digest(payload)[:24]
                payload['providerIncarnation'] = environment['REPRO_LIVE_PROVIDER_INCARNATION']
                body = self.tools.template.render({role:apps[role]._source for role in ('helper-host','helper-runner')},environment)
                payload['configurationDigest'] = hashlib.sha256(body).hexdigest()
                _require(len(body) <= 128*1024)
                descriptor = os.open('session.xctestrun',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
                with os.fdopen(descriptor,'wb') as stream:
                    stream.write(body);stream.flush();os.fsync(stream.fileno())
                record = {'schemaVersion':1,'payload':payload,'state':'prepared'}
                _write_new_at(directory,'intent.json',record);_write_new_at(directory,'state.json',record)
                _bounds(owner,cancellation,deadline_monotonic,role=role)
                launch = IOSXCTestLaunch(self,work,json.dumps(identity,sort_keys=True),body,json.dumps(payload,sort_keys=True),address,token)
                self._launches[id(launch)] = launch
                return launch
            finally: os.close(directory)
        except Exception:
            raise IOSDeviceToolError() from None
        finally:
            if acquired:self.native_owner._command_lock.release()

    def _check_launch(self, launch):
        self._verify()
        _require(type(launch) is IOSXCTestLaunch and launch._runner is self and self._launches.get(id(launch)) is launch)
        directory = _open_child_directory(self.native_owner.command_directory,launch._work.name,expected=json.loads(launch._identity))
        descriptor = None
        try:
            descriptor = _open_regular_at(directory,'session.xctestrun')
            _require(_read_fd(descriptor,128*1024) == launch._configuration)
            record = _read_json_at(directory,'intent.json')
            _require(record == {'schemaVersion':1,'payload':launch.payload,'state':'prepared'})
        finally:
            if descriptor is not None:os.close(descriptor)
            os.close(directory)
        apps = self._checked_apps(launch.payload['role'])
        _require({name:app.app_digest for name,app in apps.items()} == launch.payload['appDigests'])

    def _state(self, launch, before, after):
        directory = _open_child_directory(self.native_owner.command_directory,launch._work.name,expected=json.loads(launch._identity))
        try:
            dispatch = self._dispatches[id(launch)]
            _require(_read_json_at(directory,'native.json') == dispatch)
            expected = {'schemaVersion':1,'payload':launch.payload,'state':before}
            if before != 'prepared':expected['dispatch'] = dispatch
            _require(_read_json_at(directory,'state.json') == expected)
            _replace_at(directory,'state.json',{'schemaVersion':1,'payload':launch.payload,'state':after,'dispatch':dispatch})
        finally: os.close(directory)

    def start(self, launch, *, permit, cancellation, deadline_monotonic):
        acquired = False; session = None
        try:
            _bounds(self.native_owner,cancellation,deadline_monotonic,role=launch.payload['role']); self._check_launch(launch)
            _require(id(launch) not in self._started)
            now = require_native_dispatch(self.native_owner,permit,contracts.digest(launch.payload))
            _require(permit.provider_incarnation == launch.payload['providerIncarnation'])
            acquired = self.native_owner._command_lock.acquire(blocking=False); _require(acquired)
            self._started.add(id(launch))
            dispatch = {'dispatchOperationId':permit.operation_id,'permitFingerprint':permit.operation_fingerprint,
                'payloadDigest':permit.payload_digest,'nativeBindingDigest':self.native_owner.binding_digest}
            directory = _open_child_directory(self.native_owner.command_directory,launch._work.name,
                expected=json.loads(launch._identity))
            try:_write_new_at(directory,'native.json',dispatch)
            finally:os.close(directory)
            self._dispatches[id(launch)] = dispatch
            self._state(launch,'prepared','starting')
            deadline = min(permit.deadline_ns,self.native_owner.native_deadline_ns,
                now+int(max(0,deadline_monotonic-time.monotonic())*1_000_000_000))
            session = IOSXCTestSession(self,launch,cancellation,deadline_monotonic)
            session._owns_command_lock = True; acquired = False
            with self._lock:self._sessions.add(session)
            session._start(permit,deadline)
            return session
        except Exception:
            if session is not None:session.close()
            raise IOSDeviceToolError() from None
        except BaseException:
            if session is not None:session.close()
            raise
        finally:
            if acquired:self.native_owner._command_lock.release()

    def close(self, *, deadline_monotonic=None):
        deadline = time.monotonic()+3 if deadline_monotonic is None else deadline_monotonic
        _require(type(deadline) in (int,float) and math.isfinite(deadline))
        self._closed = True
        with self._lock:sessions = tuple(self._sessions)
        stopped = all([session.close(deadline_monotonic=deadline) for session in sessions])
        if stopped:
            with self.native_owner.operations._changed:
                self.native_owner.operations._native_clients.discard(self)
                self.native_owner.operations._changed.notify_all()
        return stopped


class IOSXCTestSession:
    def __init__(self, runner, launch, cancellation, deadline):
        self.runner,self.launch,self.cancellation,self.deadline = runner,launch,cancellation,deadline
        self._stack = ExitStack();self._processes = _IOSProcessOwner();self._process = None
        self._collectors = [];self._live = None;self._closed = False;self._owns_command_lock = False
        self._over_budget = False
        self._finished = False
        self._operation_directory = None
        self._completion_read = None
        self._completion_write = None
        self._process_state = None
        self._process_state = None
        self._lock = threading.RLock()

    def __repr__(self):return '<IOSXCTestSession>'

    @property
    def active_processes(self):return self._processes.active_processes

    @property
    def host_process_running(self):
        item=self._process_state
        if item is None:return False
        with item._lock:return item.process.poll() is None

    def public(self):
        return {'kind':'ios-xctest-session','nativeBindingDigest':self.launch.payload['nativeBindingDigest'],
            'payloadDigest':contracts.digest(self.launch.payload),'hostClientStopped':self.active_processes == 0,
            'deviceCleanupConfirmed':False,'installedArtifactVerified':False,'executionAuthority':'none'}

    def _start(self, permit, native_deadline):
        owner = self.runner.native_owner
        borrowed = self._stack.enter_context(owner.borrow_descriptors())
        self._operation_directory = os.dup(owner.command_directory)
        self._stack.callback(os.close,self._operation_directory)
        live_read,self._live=os.pipe()
        self._stack.callback(self._close_liveness)
        self._stack.callback(os.close,live_read)
        completion_read,completion_write=os.pipe();self._completion_read=completion_read
        self._completion_write=completion_write
        def pipe():
            pair=os.pipe()
            for fd in pair:self._stack.callback(os.close,fd)
            return pair
        ready_read,ready_write=pipe();grant_read,grant_write=pipe()
        descriptors=(borrowed.producer_fd,borrowed.operation_directory_fd,borrowed.device_fd,
            borrowed.device_directory_fd,live_read,ready_write,grant_read,completion_write)
        payload=self.launch.payload;tools=self.runner.tools;query=self.runner.query
        extra=(str(self._operation_directory),) if owner._recovery_context is not None else ()
        if extra:descriptors+=(self._operation_directory,)
        command=(str(tools.guardian.path),*map(str,descriptors[:5]),owner.operations.definition.scope_digest,
            str(tools.xcodebuild),tools.sha256,'xctest-'+payload['role'],query.identifier,query.bundle,str(self.launch._work),
            str(ready_write),str(grant_read),str(native_deadline),str(tools.developer_root),query.udid,
            str(payload['iteration']),payload['configurationDigest'],*extra,str(completion_write))
        with self._lock:
            _require(not self._closed and not self.runner._closed)
            self._collectors=[]
            self._process_state=self._processes.spawn(command,completion_read=completion_read,
                live_write=None,collectors=self._collectors,cwd=self.launch._work,stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,pass_fds=descriptors,close_fds=True,
                start_new_session=True,env={'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C','TMPDIR':str(self.launch._work)})
            self._process=self._process_state.process
            os.close(completion_read);self._completion_read=None
            self._collectors.extend((_Collector(self._process.stdout,65536),_Collector(self._process.stderr,65536)))
            os.close(completion_write);completion_write=None;self._completion_write=None
            for collector in self._collectors:collector.thread.start()
        while True:
            _bounds(owner,self.cancellation,self.deadline,role=self.launch.payload['role'])
            require_native_dispatch(owner,permit,contracts.digest(payload))
            _require(not self._closed and not self.runner._closed and self.host_process_running)
            if select.select([ready_read],[],[],.01)[0]:
                _require(os.read(ready_read,2) == b'R');break
        self.runner._check_launch(self.launch)
        self.runner._state(self.launch,'starting','dispatching')
        _bounds(owner,self.cancellation,self.deadline,role=self.launch.payload['role'])
        require_native_dispatch(owner,permit,contracts.digest(payload))
        _require(os.write(grant_write,b'G') == 1)

    def _bounded_work(self):
        total=entries=0;seen=set()
        live=self.host_process_running
        def scan(directory,depth):
            nonlocal total,entries
            _require(depth <= 128)
            with os.scandir(directory) as children:
                for child in children:
                    entries+=1
                    if entries > 8192:self._over_budget=True;raise IOSDeviceToolError()
                    try:
                        info=child.stat(follow_symlinks=False)
                        _require(info.st_uid == os.getuid() and not stat.S_ISLNK(info.st_mode))
                        identity=(info.st_dev,info.st_ino)
                        if stat.S_ISDIR(info.st_mode):
                            selected=os.open(child.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
                            try:
                                actual=os.fstat(selected)
                                if live and (actual.st_dev,actual.st_ino) != identity:continue
                                _require((actual.st_dev,actual.st_ino) == identity)
                                scan(selected,depth+1)
                            finally:os.close(selected)
                        else:
                            if live and stat.S_ISREG(info.st_mode) and info.st_nlink == 0:continue
                            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
                            if identity not in seen:total+=info.st_size;seen.add(identity)
                            if total > XCTEST_WORK_BYTES:self._over_budget=True;raise IOSDeviceToolError()
                    except (FileNotFoundError,NotADirectoryError):
                        if not live:raise
        parent=self._operation_directory if self._operation_directory is not None else self.runner.native_owner.command_directory
        directory=_open_child_directory(parent,self.launch._work.name,
            expected=json.loads(self.launch._identity))
        try:scan(directory,0)
        finally:os.close(directory)

    def _discard_excess_output(self, deadline):
        """Discard only this launch's generated output after every owned child stopped."""
        retained={'intent.json','state.json','native.json','session.xctestrun','stage.json','helper-control'}
        parent=self._operation_directory if self._operation_directory is not None else self.runner.native_owner.command_directory
        directory=_open_child_directory(parent,self.launch._work.name,
            expected=json.loads(self.launch._identity))
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name in retained:continue
                    _require(time.monotonic() < deadline)
                    info=entry.stat(follow_symlinks=False)
                    _require(info.st_uid == os.getuid())
                    identity=(info.st_dev,info.st_ino)
                    if stat.S_ISDIR(info.st_mode):
                        child=os.open(entry.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
                        try:
                            actual=os.fstat(child);_require((actual.st_dev,actual.st_ino) == identity)
                            _remove_owned_contents(child,[16384],deadline_monotonic=deadline)
                        finally:os.close(child)
                        current=os.stat(entry.name,dir_fd=directory,follow_symlinks=False)
                        _require((current.st_dev,current.st_ino) == identity)
                        os.rmdir(entry.name,dir_fd=directory)
                    else:
                        _require(stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode))
                        current=os.stat(entry.name,dir_fd=directory,follow_symlinks=False)
                        _require((current.st_dev,current.st_ino) == identity)
                        os.unlink(entry.name,dir_fd=directory)
            if 'stage.json' not in os.listdir(directory):
                _write_new_at(directory,'stage.json',{'kind':'xctest-output-limit',
                    'payloadDigest':contracts.digest(self.launch.payload),'generatedOutputDiscarded':True,
                    'streamDigests':[hashlib.sha256(bytes(item.data)).hexdigest() for item in self._collectors],
                    'deviceCleanupConfirmed':False})
            os.fsync(directory)
            self._bounded_work();self._over_budget=False
        finally:os.close(directory)

    def poll(self):
        try:
            if self._finished:return False
            _require(not self._closed and not self.runner._closed)
            _bounds(self.runner.native_owner,self.cancellation,self.deadline,role=self.launch.payload['role'])
            _require(not any(collector.oversized for collector in self._collectors))
            self._bounded_work()
            if self.host_process_running:return True
            self._bounded_work()
            _require(self._process.returncode == 0)
            return False
        except Exception:
            self.close();raise IOSDeviceToolError() from None

    def wait(self, *, deadline_monotonic):
        try:
            _require(type(deadline_monotonic) in (int,float) and math.isfinite(deadline_monotonic))
            while self.poll():
                _require(time.monotonic() < deadline_monotonic);time.sleep(.01)
            stopped=self.close(deadline_monotonic=deadline_monotonic)
            _require(stopped)
            return True
        except Exception:
            self.close();raise IOSDeviceToolError() from None

    def close(self, *, deadline_monotonic=None):
        deadline=time.monotonic()+3 if deadline_monotonic is None else deadline_monotonic
        _require(type(deadline) in (int,float) and math.isfinite(deadline))
        with self._lock:
            if self._finished:return True
            self._closed=True
            self._close_liveness()
            if self._completion_read is not None:
                try:os.close(self._completion_read)
                except OSError:pass
                self._completion_read=None
            if self._completion_write is not None:
                try:os.close(self._completion_write)
                except OSError:pass
                self._completion_write=None
            stopped=self._processes.close(deadline_monotonic=deadline)
            for collector in self._collectors:
                if collector.thread.ident is not None:
                    collector.thread.join(max(0,deadline-time.monotonic()))
                    if collector.thread.is_alive():stopped=False
                elif stopped:
                    collector.stream.close()
            # The first check can precede the output threads consuming EOF.
            # After joining them, recheck the same native completion authority;
            # a missing guardian D or a still-live collector remains unresolved.
            stopped=self._processes.close(deadline_monotonic=deadline)
            if stopped:
                try:self._bounded_work()
                except Exception:
                    if not self._over_budget:stopped=False
                if self._over_budget:
                    try:self._discard_excess_output(deadline)
                    except Exception:stopped=False
            if stopped:
                try:self._record_host_stop()
                except Exception:stopped=False
            if stopped:
                self._stack.close()
                if self._owns_command_lock:
                    self.runner.native_owner._command_lock.release();self._owns_command_lock=False
                with self.runner._lock:self.runner._sessions.discard(self)
                self._finished=True
            return stopped

    def _record_host_stop(self):
        if self._process is None:return
        _require(self.active_processes==0 and not self.host_process_running)
        directory=_open_child_directory(self._operation_directory,self.launch._work.name,
            expected=json.loads(self.launch._identity))
        try:
            dispatch=self.runner._dispatches[id(self.launch)]
            _require(_read_json_at(directory,'native.json')==dispatch)
            state=_read_json_at(directory,'state.json')
            _require(state.get('state') in ('starting','dispatching','started','host-stopped')
                and state=={'schemaVersion':1,'payload':self.launch.payload,
                    'state':state['state'],'dispatch':dispatch})
            _replace_at(directory,'state.json',dict(state,state='host-stopped'))
        finally:os.close(directory)

    def _close_liveness(self):
        if self._live is not None:
            os.close(self._live);self._live=None
