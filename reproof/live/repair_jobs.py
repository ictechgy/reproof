"""Opt-in Live → SDK evidence → Claude repair → verified sample app flow."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from .model import check, LiveError
from ..core import digest, ContractError, classify_runs
from ..ios_cases import case_spec
from ..ios_storage import tree_manifest, create_ios_bundle
from ..storage import read_json, write_json, sha_file
from .project_repair_jobs import ProjectRepairConfiguration, ProjectRepairJobs
from ..resources import resource_root

_ID = re.compile(r'[a-f0-9]{32}\Z')
_TERMINAL = {'verified', 'failed', 'cancelled', 'interrupted'}
ROOT = resource_root()


class LiveRepairJobs:
    def __init__(self, lab, source, build, *, platform='ios', app_profile=None, runner_factory=subprocess.Popen):
        self.lab = lab
        self.source = Path(source).resolve()
        self.build = Path(build).resolve()
        self.output = lab.output / 'repairs'
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runner_factory = runner_factory
        self.lock = threading.RLock()
        self.jobs = {}
        self.workers = {}
        self.cancel_events = {}
        self.processes = {}
        self.closed = False
        self.submitting = False
        check(platform in {'ios', 'android'}, 'repair_configuration', 'Unsupported repair platform', 400)
        check(app_profile is None or platform == 'android', 'repair_configuration', 'App profiles currently require Android', 400)
        self.android = None
        if platform == 'android':
            from .android_repair import AndroidRepairProject
            self.android = AndroidRepairProject(self.source, self.build, app_profile)
        self._validate_project()
        self.receipt_digest = sha_file(self.build / 'receipt.json')
        for path in self.output.glob('*/job.json'):
            try:
                job = read_json(path)
                if (not isinstance(job, dict) or path.is_symlink() or path.parent.is_symlink()
                        or not _ID.fullmatch(path.parent.name) or job.get('id') != path.parent.name
                        or not isinstance(job.get('owner'), str) or type(job.get('createdAt')) is not int
                        or not isinstance(job.get('requestId'), str)
                        or job.get('case') not in ({'counter','duplicate-submit','reset'} | ({app_profile.data['id']} if app_profile else set()))):
                    continue
                if job.get('state') not in _TERMINAL:
                    job.update(state='interrupted', phase='interrupted', errorCode='server_restarted')
                    write_json(path, job)
                self.jobs[job['id']] = job
            except (ContractError, ValueError, OSError, KeyError):
                continue

    def _validate_project(self):
        if hasattr(self, 'receipt_digest'):
            check(sha_file(self.build / 'receipt.json') == self.receipt_digest,
                  'repair_configuration', 'Protected build receipt changed after server startup', 400)
        if self.android:
            return self.android.validate()
        receipt = read_json(self.build / 'receipt.json')
        environment = receipt.get('executionEnvironment')
        check(environment in {'physical-iphone', 'simulator'}
              and (environment != 'physical-iphone' or receipt.get('signed') is True),
              'repair_configuration', 'Repair requires a matching iOS replay build', 400)
        products = self.build / 'DerivedData/Build/Products'
        check(receipt.get('sourceDigest') == digest(tree_manifest(self.source, True)),
              'repair_configuration', 'Repair source differs from its protected build receipt', 400)
        check(receipt.get('productsDigest') == digest(tree_manifest(products)),
              'repair_configuration', 'Protected replay build products changed', 400)
        expected_app = 'Debug-' + ('iphoneos' if environment == 'physical-iphone' else 'iphonesimulator') + '/ReproSample.app'
        check(receipt.get('appRelative') == expected_app,
              'repair_configuration', 'Repair is limited to the fixed iPhone sample', 400)
        from ..ios_storage import automatic_profile_for_receipt
        from ..ios_instrumentation import profile_from_source, validate_ios_preparation, MARKER
        self.ios_environment = environment
        self.ios_auto_profile = automatic_profile_for_receipt(products / expected_app, receipt)
        source_profile = profile_from_source(self.source)
        check((self.ios_auto_profile is None) == (source_profile is None)
              and (source_profile is None or source_profile.digest == self.ios_auto_profile.digest),
              'repair_configuration', 'iOS source and app use different recording profiles', 400)
        if source_profile and (self.source / MARKER).exists():
            check(digest(validate_ios_preparation(self.source)) == receipt['automaticInstrumentation'].get('preparationDigest'),
                  'repair_configuration', 'iOS preparation changed after its protected build', 400)
        return receipt, products, products / receipt['appRelative']

    def _save(self, job, **updates):
        with self.lock:
            job.update(updates, updatedAt=int(time.time() * 1000))
            write_json(self.output / job['id'] / 'job.json', job)

    def _public(self, job):
        keys = {'id', 'state', 'phase', 'sourceSessionId', 'resultSessionId', 'recordingId',
                'deviceId', 'case', 'createdAt', 'updatedAt', 'errorCode', 'baselineRuns',
                'verifiedRuns', 'analysis', 'repairStatus'}
        keys.add('appProfileDigest')
        result = {key: copy.deepcopy(value) for key, value in job.items() if key in keys}
        result['reportAvailable'] = (self.output / job['id'] / 'repair/report.html').is_file()
        return result

    def get(self, identifier, owner):
        with self.lock:
            job = self.jobs.get(identifier)
            check(job is not None and job['owner'] == owner, 'not_found', 'Repair job not found', 404)
            return self._public(job)

    def list(self, owner):
        with self.lock:
            return [self._public(job) for job in sorted(self.jobs.values(), key=lambda j: j['createdAt'], reverse=True)
                    if job['owner'] == owner]

    def submit(self, sid, owner, controller, epoch, recording_id, request_id):
        check(isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id),
              'invalid_request', 'A stable repair request ID is required', 400)
        with self.lock:
            check(not self.closed, 'closed', 'Repair service is closing')
            for job in self.jobs.values():
                if job['owner'] == owner and job['requestId'] == request_id:
                    check(job['sourceSessionId'] == sid and job['recordingId'] == recording_id,
                          'request_conflict', 'Repair request ID already has different input')
                    return self._public(job)
            check(not self.submitting and not any(job['state'] not in _TERMINAL for job in self.jobs.values()),
                  'repair_busy', 'A protected repair is already running')
            self.submitting = True
        claim = None
        published = False
        try:
            session = self.lab._session(sid, owner)
            with session['lock']:
                self.lab._control(session, controller, epoch)
                record = self.lab.recording(recording_id, owner)
                check(record['sessionId'] == sid and record['status'] == 'complete' and record['replayable'],
                      'invalid_recording', 'Finish a reset-based recording in this session first')
                provider = session['provider']
                authority_mode=self.lab.devices[session['deviceId']]['capabilities'].get('authorityMode')
                check(authority_mode!='shared-v2','native_protocol_mismatch',
                      'The legacy native repair runner is incompatible with shared authority v2',409)
                check(getattr(provider, 'record_sdk', False), 'unsupported_operation', 'This session has no configured SDK repair capture', 400)
                check(record['startedAt'] >= provider.capture_started_at,
                      'recording_changed', 'The app was reset after this recording')
                last = session['receipts'].get(record['events'][-1]['sourceCommandId'])
                check(last is not None and last[1]['sequence'] == session['lastSequence'],
                      'recording_changed', 'Input changed after recording; record the current reproduction again')
                receipt, products, app = self._validate_project()
                if self.android and self.android.app_profile:
                    check(provider.identity.get('appProfileDigest') == self.android.app_profile.digest,
                          'app_changed', 'Live session uses a different app profile')
                if not self.android and self.ios_auto_profile:
                    current_profile = getattr(provider, 'auto_profile', None)
                    check(current_profile is not None and current_profile.digest == self.ios_auto_profile.digest,
                          'app_changed', 'Live iOS session uses a different automatic profile')
                expected_digest = sha_file(app) if self.android else digest(tree_manifest(app))
                check(provider.identity['artifactDigest'] == expected_digest
                      and record.get('applicationIdentity') == provider.identity,
                      'app_changed', 'Live app differs from the protected repair build')
                identifier = uuid.uuid4().hex
            # claim may cancel active pointers through the provider. Never invoke
            # it while holding the repair service or session lock.
            claim = self.lab.claim(sid, owner, 'repair-' + identifier, epoch, 'automation')
            with self.lock:
                check(not self.closed, 'closed', 'Repair service is closing')
                now = int(time.time() * 1000)
                job = {'id': identifier, 'owner': owner, 'requestId': request_id, 'state': 'running', 'phase': 'capturing',
                    'sourceSessionId': sid, 'recordingId': recording_id, 'deviceId': session['deviceId'],
                    'case': provider.fixture, 'createdAt': now, 'controller': controller,
                    'repairController': claim['controllerId'], 'repairEpoch': claim['epoch']}
                if self.android and self.android.app_profile:
                    job['appProfileDigest'] = self.android.app_profile.digest
                if not self.android:
                    job['executionEnvironment'] = self.ios_environment
                    if self.ios_auto_profile:job['autoProfileDigest'] = self.ios_auto_profile.digest
                self.jobs[identifier] = job
                self.cancel_events[identifier] = threading.Event()
                self._save(job)
                worker = threading.Thread(target=self._run, args=(job, receipt, products, provider), daemon=True)
                self.workers[identifier] = worker
                published = True
                worker.start()
                return self._public(job)
        finally:
            with self.lock:
                self.submitting = False
            if claim is not None and not published:
                try:
                    self.lab.claim(sid, owner, controller, claim['epoch'], 'manual')
                except LiveError:
                    pass

    def _run(self, job, receipt, products, provider):
        root = self.output / job['id']
        reserved = False
        candidate_result = None
        stopped = self.cancel_events[job['id']]
        try:
            session = self.lab._session(job['sourceSessionId'], job['owner'])
            with session['lock']:
                self.lab._control(session, job['repairController'], job['repairEpoch'])
                check(not stopped.is_set(), 'cancelled', 'Repair was cancelled')
            capture = self.lab.collect_sdk_capture(session['id'],job['owner'],job['repairController'],job['repairEpoch'])
            check(not stopped.is_set(), 'cancelled', 'Repair was cancelled')
            frame = self.lab.frame(session['id'])
            with session['lock']:
                self.lab._control(session,job['repairController'],job['repairEpoch'])
            import base64
            images = root / 'analysis'
            images.mkdir(mode=0o700)
            (images / 'frame.jpg').write_bytes(base64.b64decode(frame['imageBase64'], validate=True))
            if self.android:
                bundle, analysis = self.android.capture_bundle(provider, capture, receipt, root)
            else:
                observation = subprocess.run(['/usr/bin/swift', str(ROOT / 'scripts/capture-text-probe.swift'), str(images)],
                    capture_output=True, timeout=45)
                check(observation.returncode == 0, 'analysis_failed', 'Local sample screen analysis failed')
                rows = json.loads(observation.stdout)
                spec = case_spec(job['case'])
                check(len(rows) == 1 and rows[0].get('counter') and rows[0].get('observedCount') == spec.bug_value,
                      'analysis_inconclusive', 'The current sample screenshot does not clearly show the selected bug')
                analysis = {'method': 'local-vision-ocr', 'observedCount': rows[0]['observedCount'],
                            'expectedCount': spec.expected_value}
                diagnostics = (self.lab.collect_sdk_diagnostics(session['id'],job['owner'],job['repairController'],
                               job['repairEpoch'],capture) if receipt.get('automaticInstrumentation') else None)
                if diagnostics is not None:
                    analysis.update(instrumentedActions=len(diagnostics['actions']), autoProfileDigest=diagnostics['profileDigest'])
                bundle = create_ios_bundle(capture, spec.oracle(), products, receipt, root / 'bundle', 'live-sdk', diagnostics=diagnostics)
            check(not stopped.is_set(), 'cancelled', 'Repair was cancelled')
            analysis.update(frameId=frame['id'], recordingDigest=self.lab.recording(job['recordingId'], job['owner'])['digest'])
            write_json(images / 'observation.json', analysis)
            self._save(job, analysis=analysis, phase='handoff')
            closed = self.lab.close_session(session['id'], job['owner'], job['repairController'],
                                            job['repairEpoch'], reserve_for_repair=True)
            check(closed['state'] == 'closed', 'cleanup_failed', 'Live driver did not release the device')
            reserved = True
            check(not stopped.is_set(), 'cancelled', 'Repair was cancelled')
            if self.android:command = self.android.command(provider, bundle, root, receipt)
            else:
                selected_device = (['--simulator', provider.udid] if self.ios_environment == 'simulator'
                                   else ['--iphone', job['deviceId']])
                command = [sys.executable, '-m', 'reproloop', 'ios-repair', str(bundle['path']), *selected_device,
                           '--source', str(self.source), '--output', str(root / 'repair'), '--agent', 'claude', '--max-attempts', '2']
            process = self.runner_factory(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            with self.lock:
                self.processes[job['id']] = process
            deadline = time.monotonic() + 1800
            interrupt_at = None
            while process.poll() is None:
                if stopped.is_set() or time.monotonic() > deadline:
                    if interrupt_at is None:
                        os.killpg(process.pid, signal.SIGINT)
                        interrupt_at = time.monotonic()
                    elif time.monotonic() - interrupt_at > 15:
                        os.killpg(process.pid, signal.SIGKILL)
                        break
                progress = root / 'repair/job.json'
                if progress.is_file():
                    result = read_json(progress)
                    if result['status'] != job['phase']:
                        self._save(job, phase=result['status'])
                time.sleep(.25)
            process.wait(timeout=10)
            check(not stopped.is_set(), 'cancelled', 'Repair was cancelled')
            check(process.returncode == 0, 'repair_failed', 'Protected repair did not verify a fix')
            result = read_json(root / 'repair/job.json')
            if self.android:
                from .android_live import AndroidLiveProvider
                candidate, identity = self.android.candidate(result, root, receipt, provider.device.identity)
                def factory():
                    check(sha_file(candidate) == identity['artifactDigest'],
                          'app_changed', 'Verified candidate APK changed before reopening')
                    return AndroidLiveProvider(provider.device.serial, provider.helper_apk, candidate, record_sdk=False,
                                               app_profile=self.android.app_profile)
            else:
                check(result['status'] == 'verified' and result['executionEnvironment'] == self.ios_environment,
                      'repair_failed', 'Repair did not produce matching iOS execution evidence')
                number = result['attempts'][-1]['attemptId']
                check(type(number) is int and number in {1, 2}, 'repair_failed', 'Invalid iOS repair attempt')
                attempt = root / f'repair/attempt-{number}'
                suffix = 'iphoneos' if self.ios_environment == 'physical-iphone' else 'iphonesimulator'
                candidate_products = attempt / 'build/DerivedData/Build/Products'
                candidate = candidate_products / f'Debug-{suffix}/ReproSample.app'
                candidate_receipt = read_json(attempt / 'build/receipt.json')
                source_files = tree_manifest(attempt / 'source', True)
                product_file = case_spec(job['case']).product_file
                original_files = tree_manifest(self.source, True)
                check(candidate_receipt.get('buildCompleted') is True
                      and candidate_receipt.get('sourceDigest') == digest(source_files)
                      and candidate_receipt.get('productsDigest') == digest(tree_manifest(candidate_products))
                      and {k: v for k, v in source_files.items() if k != product_file}
                          == {k: v for k, v in original_files.items() if k != product_file},
                      'repair_failed', 'iOS candidate differs from its protected build')
                baseline = read_json(root / 'repair/baseline/result.json')
                verification = read_json(attempt / 'verification/result.json')
                check(classify_runs(baseline['runs'], 'original', 3) == 'reproduced'
                      and classify_runs(verification['runs'], 'patched', 3) == 'verified'
                      and all(run.get('appArtifactDigest') == digest(tree_manifest(candidate))
                              for run in verification['runs']),
                      'repair_failed', 'iOS candidate lacks matching repeated verification')
                from ..ios_storage import automatic_profile_for_receipt
                candidate_profile = automatic_profile_for_receipt(candidate, candidate_receipt)
                check((candidate_profile is None) == (self.ios_auto_profile is None)
                      and (candidate_profile is None or candidate_profile.digest == self.ios_auto_profile.digest),
                      'repair_failed', 'iOS candidate recording profile changed')
                if self.ios_environment == 'physical-iphone':
                    from .iphone import validate_signed_products, PhysicalIosProvider
                    identity = validate_signed_products(provider.products, candidate)
                    factory = lambda: PhysicalIosProvider(provider.device, provider.products, candidate, identity,
                                                          fixture=provider.fixture, record_sdk=False)
                else:
                    from .providers import IosProvider
                    identity = {'bundle': provider.bundle, 'artifactDigest': digest(tree_manifest(candidate))}
                    factory = lambda: IosProvider(provider.udid, provider.products, provider.bundle, identity,
                                                 app=candidate, fixture=provider.fixture, record_sdk=False)
            candidate_result = (factory, identity)
            self._save(job, phase='finalizing', repairStatus=result['status'],
                       baselineRuns=3 if self.android else sum(r.get('bugCondition') is True for r in result['runs']),
                       verifiedRuns=3 if self.android else sum(r.get('expectedCondition') is True and r.get('regressionPassed') for r in result['runs']))
        except Exception as error:
            code = getattr(error, 'code', 'repair_failed')
            allowed = {'cancelled', 'analysis_failed', 'analysis_inconclusive', 'capture_invalid', 'cleanup_failed', 'repair_failed'}
            self._save(job, state='cancelled' if stopped.is_set() or code == 'cancelled' else 'failed',
                       phase='finished', errorCode=code if code in allowed else 'repair_failed')
        finally:
            with self.lock:
                process = self.processes.pop(job['id'], None)
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
            if reserved:
                clean = False
                try:
                    if self.android:
                        phone = provider.device
                    elif self.ios_environment == 'physical-iphone':
                        from ..ios_device import IosPhysicalDevice
                        phone = IosPhysicalDevice(job['deviceId'])
                    else:
                        from ..ios_runner import IosSimulator
                        phone = IosSimulator(provider.udid)
                    with phone.lease():
                        phone.stop()
                    clean = True
                except Exception:
                    pass
                # Cancellation and publication share the service lock: a
                # cancellation accepted before this commit cannot expose a
                # candidate, even after its child process has already exited.
                with self.lock, self.lab.lock:
                    self.lab.devices[job['deviceId']].update(state='available' if clean else 'quarantined', sessionId=None)
                    self.lab._persist_devices()
                    if not clean:
                        self._save(job, state='failed', phase='finished', errorCode='cleanup_failed')
                    elif stopped.is_set() and job['state'] == 'running':
                        self._save(job, state='cancelled', phase='finished', errorCode='cancelled')
                    elif candidate_result is not None and job['state'] == 'running':
                        factory, identity = candidate_result
                        device = self.lab.devices[job['deviceId']]
                        device['factory'] = factory
                        device['capabilities'].update(applicationIdentity=identity, sdkCapture=False, repairVerified=True)
                        self._save(job, state='verified', phase='verified', candidateIdentity=identity)
            else:
                try:
                    current = self.lab.get_session(job['sourceSessionId'], job['owner'])
                    if current['state'] == 'active' and current['controllerId'] == job['repairController']:
                        self.lab.claim(current['id'], job['owner'], job['controller'], current['epoch'], 'manual')
                except LiveError:
                    pass

    def cancel(self, identifier, owner):
        self.get(identifier, owner)
        with self.lock:
            if identifier in self.cancel_events and self.jobs[identifier]['state'] not in _TERMINAL:
                self.cancel_events[identifier].set()
            return self._public(self.jobs[identifier])

    def resume(self, identifier, owner, client_id):
        job = self.get(identifier, owner)
        check(job['state'] == 'verified', 'repair_unverified', 'Only a verified repair can open its candidate app')
        with self.lock:
            stored = self.jobs[identifier]
            check(self.lab.devices[job['deviceId']]['capabilities'].get('applicationIdentity') == stored.get('candidateIdentity'),
                  'app_changed', 'The verified candidate is not configured on this server; reload its signed build')
            if stored.get('resultSessionId'):
                previous = self.lab.get_session(stored['resultSessionId'], owner)
                if previous['state'] not in {'closed', 'failed'}:
                    return previous
            session = self.lab.create_session(job['deviceId'], owner, client_id)
            self._save(stored, resultSessionId=session['id'])
            return session

    def report(self, identifier, owner):
        self.get(identifier, owner)
        path = self.output / identifier / 'repair/report.html'
        check(path.is_file(), 'not_found', 'Repair report is not available', 404)
        return path.read_bytes()

    def close(self):
        with self.lock:
            self.closed = True
            for event in self.cancel_events.values():
                event.set()
            workers = list(self.workers.values())
        for worker in workers:
            worker.join(timeout=55)
