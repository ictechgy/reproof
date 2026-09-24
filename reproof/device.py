"""ADB transport restricted to the sample and an ID-only instrumentation driver."""
from __future__ import annotations
from contextlib import nullcontext
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from .core import ContractError, identifier, require
from .storage import PACKAGE, Lease, sha_file

DRIVER = "io.reproof.driver"
SAFE_NODES = {"name", "count", "add", "list", "bottom", "next", "back", "report", "title"}


class DeviceError(RuntimeError):
    pass


class _TrackedLease:
    def __init__(self, owner, lease):
        self.owner = owner
        self.lease = lease

    def __enter__(self):
        value = self.lease.__enter__()
        local = self.owner._lease_local
        local.depth = getattr(local, 'depth', 0) + 1
        return value

    def __exit__(self, *args):
        local = self.owner._lease_local
        try:
            return self.lease.__exit__(*args)
        finally:
            local.depth = max(0, getattr(local, 'depth', 1) - 1)


def _leased_effect(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._mutation_lease():
            return method(self, *args, **kwargs)
    return guarded


def find_adb():
    path = shutil.which("adb")
    if path: return path
    path = Path.home()/"Library/Android/sdk/platform-tools/adb"
    if path.is_file(): return str(path)
    raise DeviceError("ADB is unavailable; add Android platform-tools to PATH")


class AdbDevice:
    def __init__(self, serial=None, adb=None, timeout=30, app_profile=None):
        if app_profile is not None:
            from .android_profile import AndroidAppProfile
            require(isinstance(app_profile, AndroidAppProfile), "Invalid Android app profile")
        self.adb = adb or find_adb(); self.timeout = timeout
        self.app_profile = app_profile
        self.package = app_profile.data["package"] if app_profile is not None else PACKAGE
        self.activity = app_profile.data["activity"] if app_profile is not None else ".MainActivity"
        self.last_profile_receipt = None
        self.sdk_session_id = None
        self._lease_local = threading.local()
        self.authority_lease = None
        result = self._command([self.adb, "devices"], timeout=10).decode()
        available = [line.split()[0] for line in result.splitlines()[1:]
                     if len(line.split()) == 2 and line.split()[1] == "device"]
        require(bool(available), "No authorized Android device is connected")
        if serial is None:
            require(len(available) == 1, "Select one authorized device with --serial")
            serial = available[0]
        require(serial in available, "Selected device is not authorized or connected")
        self.serial = serial
        self.identity = hashlib.sha256(serial.encode()).hexdigest()[:16]

    def _profile_targets(self):
        if self.app_profile is None:
            return None
        return self.app_profile.data["targets"]

    def _profile_proof(self, proof):
        if self.app_profile is not None:
            require(isinstance(proof, dict)
                    and proof.get("appProfileDigest") == self.app_profile.digest
                    and proof.get("nativeDigest") == self.app_profile.native_digest,
                    "Device proof does not match the trusted app profile")
        return proof

    def _command(self, args, timeout=None):
        try:
            result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=timeout or self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise DeviceError("Device operation timed out") from exc
        except OSError as exc:
            raise DeviceError("Device tool could not start") from exc
        if result.returncode:
            # Do not leak device or account information from stderr.
            raise DeviceError("Device operation failed (check device authorization and sample installation)")
        return result.stdout

    def adb_call(self, *args, timeout=None):
        return self._command([self.adb, "-s", self.serial, *map(str,args)], timeout)

    def shell(self, *args, timeout=None):
        return self.adb_call("shell", shlex.join([str(a) for a in args]), timeout=timeout).decode("utf-8", "replace")

    def lease(self):
        if not hasattr(self,'_lease_local'):self._lease_local=threading.local()
        if getattr(self._lease_local, 'depth', 0) > 0:
            return nullcontext()
        return _TrackedLease(self, Lease(self.serial))

    def _mutation_lease(self):
        authority_lease=getattr(self,'authority_lease',None)
        return authority_lease if authority_lease is not None else self.lease()

    def apk_package(self, apk):
        return self.apk_identity(apk)["package"]

    def apk_identity(self, apk):
        sdk = Path(self.adb).resolve().parent.parent
        candidates = sorted((sdk / "build-tools").glob("*/aapt"), reverse=True)
        require(bool(candidates), "Android aapt is required to inspect APK identity")
        data = self._command([str(candidates[0]), "dump", "badging", str(Path(apk).resolve())]).decode()
        package = re.search(r"^package: name='([^']+)'",data,re.M)
        version = re.search(r"^package: .*versionCode='([0-9]+)'",data,re.M)
        require(package is not None and version is not None,"Cannot determine APK identity")
        return {"package":package.group(1),"versionCode":int(version.group(1))}

    @_leased_effect
    def install(self, apk, expected_package=None):
        expected_package = self.package if expected_package is None else expected_package
        require(expected_package in {self.package, DRIVER}, "Package outside trusted app scope")
        require(self.apk_package(apk) == expected_package, "APK package does not match sample scope")
        output=self.adb_call("install", "-r", "-t", str(Path(apk).resolve()), timeout=90).decode()
        if 'Success' not in output: raise DeviceError("APK installation did not succeed")
        return self.installation_proof(apk,expected_package)

    def installation_proof(self, apk, package=None):
        package = self.package if package is None else package
        require(package in {self.package,DRIVER},"Package outside trusted app scope")
        entries=self.shell("pm","path",package).splitlines()
        paths=[line.removeprefix('package:').strip() for line in entries if line.startswith('package:')]
        require(len(paths)==1 and paths[0].startswith('/data/app/') and paths[0].endswith('.apk'),
                "MVP requires a single installed APK")
        checksum=self.shell("sha256sum",paths[0]).split()[0]
        require(re.fullmatch(r'[0-9a-f]{64}',checksum) is not None and checksum==sha_file(apk),
                "Installed APK does not match requested artifact")
        proof = {"package":package,"apkSha256":checksum,"deviceId":self.identity,"installedVerified":True}
        if self.app_profile is not None:
            proof.update(appProfileDigest=self.app_profile.digest, nativeDigest=self.app_profile.native_digest)
        return self._profile_proof(proof)

    @_leased_effect
    def prepare(self, apk, fixture, mode='replay'):
        expected_fixture = (self.app_profile.data["fixture"] if self.app_profile is not None
                            else {"id":"default","version":1,"inputs":{}})
        require(fixture == expected_fixture,"Unsupported fixture")
        require(mode in {'record','replay'},"Invalid sample mode")
        proof=self.install(apk,self.package)
        self.shell('am','force-stop',self.package)
        if self.app_profile is None:
            require(self.shell('pm','clear',self.package).strip()=='Success','Sample data reset failed')
        elif mode == 'record':
            # The SDK's latest pointers are the only state recording may
            # remove.  Fixture/product data remains untouched.
            self.shell('run-as', self.package, 'sh', '-c',
                       'rm -f files/repro/capture.json files/repro/current-session')
        component = self.app_profile.component_name if self.app_profile is not None else self.package+'/'+self.activity
        start_args = ['am','start','-W','-n',component,
                      '--es','repro_mode',mode,
                      '--es','fixture_id',expected_fixture['id']]
        if self.app_profile is not None:
            start_args += ['--ei','fixture_version',str(expected_fixture['version'])]
        self.app_log_run_id = (str(uuid.uuid4()) if mode=='record' and self.app_profile is not None
                               and self.app_profile.data.get('appLogs')==1 else None)
        if self.app_log_run_id:start_args += ['--es','repro_log_run_id',self.app_log_run_id]
        self.shell(*start_args)
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            try:
                nodes=self.observe()
                if self.app_profile is not None:
                    valid_start = nodes == self.app_profile.data['startState']['nodes']
                else:
                    valid_start = nodes.get('count') == '0' and nodes.get('name', '') == ''
                if valid_start:
                    if mode == 'record' and self._receiver_capture():
                        self.sdk_session_id = self.pin_sdk_session()
                    proof['fixtureVerified']=True;return proof
            except DeviceError:pass
            time.sleep(.25)
        raise DeviceError('Sample start state does not match fixture')

    @_leased_effect
    def driver(self,op,**parameters):
        if self.app_profile is not None:
            # A failed or rejected operation must never inherit a previous
            # operation's native profile proof.
            self.last_profile_receipt = None
        require(op in {'observe','tap','replace','scroll_to','back'},'Unsupported driver operation')
        allowed_keys = {'target','value','container','direction'}
        require(set(parameters) <= allowed_keys, 'Unsupported driver parameter')
        targets = self._profile_targets()
        if op == 'observe':
            require(set(parameters) <= {'target'} and
                    (not parameters or isinstance(parameters['target'], str)),
                    'Observe accepts only an optional target')
            if parameters:
                target = parameters['target']; identifier(target)
                observed = ((set(targets['text']) | set(targets['numeric']) |
                             set(targets['tap']) | set(targets['scroll']) |
                             {value for values in targets['scroll'].values() for value in values} |
                             {targets['back'], targets['report']})
                            if targets is not None else SAFE_NODES)
                require(target in observed, 'Unsupported observation target')
        elif op == 'tap':
            require(set(parameters) == {'target'} and isinstance(parameters['target'], str),
                    'Tap requires one target')
            target = parameters['target']; identifier(target)
            allowed = (set(targets['tap']) | {targets['report']}) if targets is not None else SAFE_NODES
            require(target in allowed, 'Unsupported tap target')
        elif op == 'replace':
            require(set(parameters) == {'target','value'} and isinstance(parameters['target'], str)
                    and isinstance(parameters['value'], str), 'Replace requires target and value')
            target = parameters['target']; identifier(target)
            allowed = set(targets['text']) if targets is not None else {'name'}
            require(target in allowed and parameters['value'] in {'','QA','Test'},
                    'Unsupported text target or value')
        elif op == 'back':
            require(set(parameters) == {'target'} and isinstance(parameters['target'], str),
                    'Back requires one target')
            target = parameters['target']; identifier(target)
            require(target == (targets['back'] if targets is not None else 'back'),
                    'Unsupported back target')
        elif op == 'scroll_to':
            require(set(parameters) == {'target','container','direction'}
                    and all(isinstance(parameters[key], str) for key in ('target','container','direction')),
                    'Scroll requires target, container and direction')
            target = parameters['target']; container = parameters['container']
            identifier(target); identifier(container)
            scroll_targets = targets['scroll'] if targets is not None else {'list':['bottom']}
            require(container in scroll_targets and target in scroll_targets[container]
                    and parameters['direction'] in {'forward','backward'},
                    'Unsupported scroll target or direction')
        args=['am','instrument','-w','-e','op',op,'-e','package',self.package]
        if self.app_profile is not None:
            args += ['-e', 'app_profile', json.dumps(self.app_profile.native(), sort_keys=True, separators=(',', ':')),
                     '-e', 'profile_digest', self.app_profile.digest]
        for key,value in parameters.items():
            args += ['-e',key,str(value)]
        args.append(DRIVER+'/.DriverInstrumentation')
        text=self.shell(*args,timeout=25)
        matches=re.findall(r'^INSTRUMENTATION_(?:RESULT|STATUS): result=(.*)$',text,re.M)
        if not matches:raise DeviceError('Driver returned no structured result')
        try:result=json.loads(matches[-1])
        except json.JSONDecodeError as exc:raise DeviceError('Malformed driver result') from exc
        if self.app_profile is not None:
            require(result.get('profileDigest') == self.app_profile.digest
                    and result.get('nativeDigest') == self.app_profile.native_digest,
                    'Driver profile digest mismatch')
        if result.get('ok') is not True:raise DeviceError('Driver failed: target missing, ambiguous or action unsupported')
        if self.app_profile is not None:
            self.last_profile_receipt = {
                'profileDigest': result['profileDigest'],
                'nativeDigest': result['nativeDigest'],
                'operation': op,
            }
        return result

    def observe(self):
        response=self.driver('observe');nodes={}
        profile_targets = self._profile_targets()
        observed = (set(profile_targets['text']) | set(profile_targets['numeric'])) if profile_targets is not None else SAFE_NODES
        for node in response.get('nodes',[]):
            target=node.get('id','').split('/')[-1]
            if target not in observed:continue
            require(node.get('redacted') is not True,'Observation contains a suppressed input')
            if target in nodes:raise DeviceError('Ambiguous UI target')
            text=node.get('text') or ''
            # Only selected, synthetic-value fields become host observations.
            if profile_targets is not None:
                if target in profile_targets['text']:
                    require(text in {'','QA','Test'},'Observation outside app profile allowlist')
                elif target in profile_targets['numeric']:
                    require(re.fullmatch(r'[0-9]{1,9}', text) is not None,
                            'Observation outside app profile allowlist')
                nodes[target]=text
            elif target in {'name','count'}:
                require(text in {'','QA','Test'} or (text.isdigit() and len(text)<10),'Observation outside sample allowlist')
                nodes[target]=text
            else:nodes[target]=''
        return nodes

    @_leased_effect
    def execute(self,step):
        action=step['action'];params=dict(step['parameters'])
        self.driver(action,target=step['target'],**params)

    def _receiver_capture(self):
        return self.app_profile is not None and self.app_profile.data.get('captureMode') == 'debug_receiver'

    @_leased_effect
    def request_sdk_export(self):
        if self._receiver_capture():
            result = self.shell('am', 'broadcast', '--receiver-foreground', '-a', 'io.reproof.EXPORT_CAPTURE',
                                '-n', self.package + '/io.reproof.autotrace.AutoExportReceiver', timeout=15)
            require(re.search(r'Broadcast completed: result=0(?:\D|$)', result) is not None,
                    'Debug instrumentation export was not accepted')
            return
        report_target = self.app_profile.data['targets']['report'] if self.app_profile is not None else 'report'
        self.driver('tap',target=report_target)

    def _sdk_json(self, path, limit=20*1024*1024):
        from .storage import _unique_object
        data = self.adb_call('exec-out', 'run-as', self.package, 'cat', path)
        require(len(data) <= limit, 'SDK artifact exceeds the size limit')
        try:
            value = json.loads(data, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError):
            raise ContractError('Invalid SDK JSON artifact') from None
        require(isinstance(value, dict), 'Invalid SDK artifact object')
        return value

    def _sdk_session(self):
        data = self.adb_call('exec-out', 'run-as', self.package, 'cat', 'files/repro/current-session')
        require(len(data) <= 128, 'Invalid SDK session pointer')
        try:value = data.decode('ascii').strip()
        except UnicodeError:raise ContractError('Invalid SDK session pointer') from None
        require(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None, 'Invalid SDK session pointer')
        return value

    def pin_sdk_session(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:return self._sdk_session()
            except DeviceError:time.sleep(.1)
        raise DeviceError('SDK session pointer was not published')

    def collect_instrumentation_diagnostics(self, capture):
        from .instrumentation_diagnostics import validate_diagnostics, MAX_DIAGNOSTICS
        require(self._receiver_capture(), 'Instrumentation diagnostics are not configured')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            require(self._sdk_session() == capture['sessionId'], 'SDK session changed during diagnostics collection')
            try:value = self._sdk_json('files/repro/diagnostics.json', MAX_DIAGNOSTICS)
            except DeviceError:
                time.sleep(.1);continue
            if value.get('sessionId') != capture['sessionId']:
                time.sleep(.1);continue
            validated = validate_diagnostics(value, capture, self.app_profile)
            require(self._sdk_session() == capture['sessionId'], 'SDK session changed during diagnostics collection')
            return validated
        raise DeviceError('Instrumentation diagnostics were not published for this capture')

    def collect_app_logs(self,run_id=None,*,expected_marker=None):
        from .app_logs import collect_app_logs,MAX_APP_LOG_BYTES
        require(self.app_profile is not None and self.app_profile.data.get('appLogs')==1,
                'Automatic app logs are not configured for this Android build')
        profile=self.app_profile.data
        return collect_app_logs(lambda relative:self._sdk_json('files/repro/'+relative,MAX_APP_LOG_BYTES),
            platform='android',application_id=self.package,profile_digest=self.app_profile.digest,
            run_id=run_id or getattr(self,'app_log_run_id',None),click_targets=set(profile['targets']['tap']),
            screen_targets=set(profile.get('screenTargets',{}).values()),expected_marker=expected_marker)

    @_leased_effect
    def freeze_capture(self):
        self.request_sdk_export()
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            try:
                capture = self._sdk_json('files/repro/capture.json')
                if self._receiver_capture():
                    require(self.sdk_session_id is not None and self._sdk_session() == self.sdk_session_id,
                            'SDK recording session is not pinned')
                    if capture.get('sessionId') != self.sdk_session_id:
                        time.sleep(.1);continue
                    metadata = self._sdk_json('files/repro/' + self.sdk_session_id + '/session.json')
                    if metadata.get('finalized') is not True:
                        time.sleep(.1);continue
                    require(metadata.get('sessionId') == self.sdk_session_id and metadata.get('incomplete') is False
                            and metadata.get('lostEvents') is False and metadata.get('unsupported') is False,
                            'SDK capture was not finalized cleanly')
                return capture
            except (DeviceError,json.JSONDecodeError):time.sleep(.2)
        raise DeviceError('Capture did not freeze successfully')

    @_leased_effect
    def stop(self):
        self.shell('am','force-stop',self.package)
