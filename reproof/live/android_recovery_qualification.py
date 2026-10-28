"""Private, UNQUALIFIED same-process Android recovery experiment.

Not imported by CLI/server/device inventory. Never enables normal Lab recovery.
Native calls in this module are intended only for a separately authorized,
explicit disposable-emulator qualification run; portable tests replace transport.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from reproof.core import ContractError
from reproof.android_profile import validate_android_runtime_profile, AndroidRuntimeProfile
from .authority import HostAuthority, ParentGrant, canonical_device_fingerprint
from .android_recovery_authority import QualificationJournal, RecoveryIssuer, _directory
from .model import Lab

_RETIRE = frozenset(('normalAdmissionClosed', 'commandQueueDrained', 'inflightCompleted',
    'pointersCancelled', 'captureWorkersStopped', 'inspectionMediaDrained', 'publicationClosed'))
_FRAME = frozenset(('nativeFrameId', 'frameSha256', 'frameBytes', 'width', 'height',
    'geometryVersion', 'targetForeground', 'captureStartMs', 'captureEndMs'))
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')


def _require(value, message='Android recovery qualification unavailable'):
    if not value:
        raise ContractError(message)


def _bytes(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ContractError('Invalid qualification evidence') from None
    _require(len(raw) <= 16 * 1024, 'Qualification evidence exceeds bound')
    return raw


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _config(binding, phase):
    value = {key: item for key, item in binding.items()
        if key not in {'hostIncarnation', 'helperIncarnation', 'providerIncarnation'}}
    value['phase'] = phase
    raw = _bytes(value)
    return value, {'configurationBase64': base64.b64encode(raw).decode('ascii'),
        'configurationDigest': hashlib.sha256(raw).hexdigest()}


class _RecoveryChannel:
    """Integration-owned authenticated observations, never imported evidence."""
    def __init__(self, owner, role, producer, configuration):
        self._owner, self.role, self._producer = owner, role, producer
        self._configuration = _bytes(configuration)
        self.configuration_digest = hashlib.sha256(self._configuration).hexdigest()
        self.host_incarnation = producer.host_incarnation
        self.helper_incarnation = producer.helper_incarnation
        self.provider_incarnation = producer.provider_incarnation
        self._startup = copy.deepcopy(producer.startup_status)
        self._status = None
        self._terminal = False
        self._receipts = {}
        self._pid = os.getpid()
        owner._channels[role] = self
        self.require_owned()

    @property
    def configuration(self):
        return json.loads(self._configuration)

    def require_owned(self, *, issuer=None):
        self._owner._check()
        if issuer is not None:
            _require(self._owner._active_issuer is issuer and issuer._journal is self._owner._journal
                and issuer._device is self._owner._provider.device_authority, 'Private recovery issuer differs')
        _require(self._pid == os.getpid() and self._owner._channels.get(self.role) is self
            and self.role in {'old', 'probe'}, 'Live qualification channel required')
        _require(self._producer is (self._owner._provider if self.role == 'old' else self._owner._probe),
            'Qualification producer changed')
        _require(self._producer.configuration_digest == self.configuration_digest,
            'Qualification configuration changed')
        for key, value in (('hostIncarnation', self.host_incarnation),
                           ('helperIncarnation', self.helper_incarnation),
                           ('providerIncarnation', self.provider_incarnation)):
            _require(self._startup.get(key) == value, 'Qualification startup identity differs')

    def read_status(self):
        self.require_owned()
        issuer = self._owner._active_issuer
        _require(issuer is not None, 'Live recovery issuer required')
        value = (copy.deepcopy(self._status) if self._terminal else
                 self._producer.read_status(guard=issuer.check))
        _require(type(value) is dict, 'Qualification status unavailable')
        for key in ('nativeIncarnation', 'nativeProcessIdentity', 'helperIncarnation',
                    'providerIncarnation', 'hostIncarnation', 'nativeClockId'):
            _require(value.get(key) == self._startup.get(key), 'Qualification native producer changed')
        _require(type(value.get('recoveryQualificationVersion')) is int
            and value['recoveryQualificationVersion'] == 1
            and value.get('recoveryQualificationConfigurationDigest') == self.configuration_digest,
            'Qualification protocol differs')
        self._status = copy.deepcopy(value)
        return value

    def operation(self, issuer, purpose):
        self.require_owned()
        grant = issuer.issue(purpose, self)
        wire = grant.wire()
        self._producer.recovery_call(grant)
        while True:
            issuer.require_grant(grant)
            self.require_owned()
            result = self._producer.recovery_result(grant)
            issuer.require_grant(grant)
            _require(type(result) is dict, 'Invalid qualification response')
            if result.get('pending') is True:
                _require(set(result) == {'pending', 'operationId'}
                    and result['operationId'] == wire['operationId'], 'Invalid pending qualification response')
                time.sleep(.01)
                continue
            _require(set(result) == {'pending', 'receipt'} and result['pending'] is False,
                'Qualification native effect unconfirmed')
            receipt = result['receipt']
            self._validate_receipt(receipt, wire, purpose)
            self._receipts[purpose] = _bytes(receipt)
            if purpose in {'old-retire', 'probe-retire'}:
                self._terminal = True
                self._status['nativeTimeMs'] = receipt['completedNativeTimeMs']
            if purpose == 'fresh-probe':
                frame = self._producer.recovery_frame(grant)
                self._validate_frame(frame, receipt['evidence'])
            if purpose in {'old-exit', 'probe-exit'}:
                retired = 'old-retire' if purpose == 'old-exit' else 'probe-retire'
                _require(retired in self._receipts, 'Terminal retirement evidence required')
                exit_evidence = self._producer.collect_exit(issuer, grant)
                issuer.require_grant(grant)
                _require(type(exit_evidence) is dict and set(exit_evidence) == {
                    'nativeProcessIdentity', 'remoteHelperAbsent', 'clientCollected', 'pollerJoined'}
                    and exit_evidence['nativeProcessIdentity'] == self._startup['nativeProcessIdentity']
                    and all(exit_evidence[key] is True for key in
                        ('remoteHelperAbsent', 'clientCollected', 'pollerJoined')),
                    'Qualification producer exit unconfirmed')
                return _digest({'terminalReceipt': receipt, 'exitEvidence': exit_evidence})
            return _digest(receipt)

    def _validate_receipt(self, receipt, wire, purpose):
        _require(type(receipt) is dict and set(receipt) == {'schemaVersion', 'status', 'grant',
            'nativeProcessIdentity', 'completedNativeTimeMs', 'kind', 'evidence'}
            and type(receipt['schemaVersion']) is int and receipt['schemaVersion'] == 1
            and receipt['status'] == 'complete' and _bytes(receipt['grant']) == _bytes(wire)
            and _bytes(receipt['nativeProcessIdentity']) == _bytes(self._startup['nativeProcessIdentity']),
            'Qualification receipt binding differs')
        completed = receipt['completedNativeTimeMs']
        _require(type(completed) is int and self._status is not None
            and self._status['nativeTimeMs'] <= completed < wire['nativeDeadlineMs'],
            'Qualification receipt deadline differs')
        evidence = receipt['evidence']
        _require(type(evidence) is dict, 'Invalid qualification evidence')
        if purpose in {'old-retire', 'probe-retire'}:
            _require(receipt['kind'] == 'retirement' and set(evidence) == _RETIRE | {
                'admissionWatermark', 'detachedCommandId'}
                and all(evidence[key] is True for key in _RETIRE)
                and type(evidence['admissionWatermark']) is int
                and 0 <= evidence['admissionWatermark'] <= 2**31-1
                and (evidence['detachedCommandId'] is None or
                    (type(evidence['detachedCommandId']) is str and
                     re.fullmatch(r'[A-Za-z0-9_-]{1,256}', evidence['detachedCommandId']))),
                'Qualification producer quiescence unconfirmed')
        elif purpose == 'fresh-probe':
            _require(receipt['kind'] == 'probe-frame' and set(evidence) == _FRAME
                and evidence['targetForeground'] is True
                and type(evidence['frameSha256']) is str
                and _DIGEST.fullmatch(evidence['frameSha256']) is not None,
                'Qualification probe frame unconfirmed')
            for key in ('nativeFrameId', 'frameBytes', 'width', 'height', 'geometryVersion'):
                _require(type(evidence[key]) is int and evidence[key] > 0, 'Invalid probe frame geometry')
            _require(evidence['width'] <= 8192 and evidence['height'] <= 8192
                and evidence['width'] * evidence['height'] <= 8_294_400
                and evidence['frameBytes'] <= 3*1024*1024 + 4104,
                'Probe frame exceeds bound')
            _require(type(evidence['captureStartMs']) is int and type(evidence['captureEndMs']) is int
                and self._status['nativeTimeMs'] <= evidence['captureStartMs']
                <= evidence['captureEndMs'] <= completed, 'Probe frame freshness differs')
        else:
            _require(purpose in {'old-exit', 'probe-exit'}
                and receipt['kind'] == 'exit-accepted' and evidence == {},
                'Qualification exit receipt differs')

    @staticmethod
    def _validate_frame(raw, evidence):
        from .android_live import decode_native_frame
        _require(type(raw) is bytes and len(raw) == evidence['frameBytes']
            and hashlib.sha256(raw).hexdigest() == evidence['frameSha256'], 'Probe frame bytes differ')
        metadata, image = decode_native_frame(raw)
        _require(bool(image) and all(type(metadata.get(key)) is int and metadata.get(key) == evidence[key]
            for key in ('nativeFrameId', 'width', 'height', 'geometryVersion')),
            'Probe frame metadata differs')


class QualificationHarness:
    """Private source-level experiment, not production recovery authority.

    Creation never starts a device. `start`/`recover` are effectful and require a
    separately authorized disposable-AVD run. The supplied grant provider must
    use the normal trusted coordinator composition; no local grant is fabricated.
    """
    @classmethod
    def create(cls, parent, *, serial, helper_apk, app_apk, runtime_profile, grant_provider, context):
        from .android_recovery_transport import _digest_file
        from .android_recovery_context import IsolatedAdbContext
        _require(type(context) is IsolatedAdbContext, 'Owned isolated ADB context required')
        _require(type(serial) is str and re.fullmatch(r'emulator-[0-9]{4,5}', serial)
            and callable(grant_provider), 'Explicit disposable emulator required')
        profile = runtime_profile if type(runtime_profile) is AndroidRuntimeProfile else validate_android_runtime_profile(runtime_profile)
        data = profile.data
        _require(data['package'] != 'io.reproof.live' and data['capabilities']['logAdapter'] is None
            and 'launch' in data['capabilities']['actions'], 'Unsupported qualification profile')
        helper_digest = _digest_file(helper_apk, 150*1024*1024)
        _require(_digest_file(app_apk, 150*1024*1024) == data['artifact']['sha256'],
            'Qualification app artifact differs')
        context.require_bound(serial, data['package'], helper_digest, data['artifact']['sha256'])
        parent = Path(parent).absolute()
        parent_fd = _directory(parent)
        root_fd = None
        obj = None
        try:
            name = 'qualification-' + uuid.uuid4().hex
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            root_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            obj = cls.__new__(cls)
            obj._parent, obj._parent_fd, obj._root_fd = parent, parent_fd, root_fd
            obj.root = parent / name
            obj._pid, obj._thread = os.getpid(), threading.get_ident()
            obj._closed = False
            obj._attempt = 'attempt_' + uuid.uuid4().hex
            obj.session_id = 'session_' + uuid.uuid4().hex
            obj.serial, obj.profile = serial, profile
            obj.helper_apk, obj.app_apk = Path(helper_apk).absolute(), Path(app_apk).absolute()
            obj.helper_digest = helper_digest
            obj._context = context
            obj._grant_provider = grant_provider
            obj._channels = {}
            obj._provider = obj._probe = obj._journal = obj._active_issuer = obj._session = None
            obj._recovery_attempted = False
            obj.cancel = threading.Event()
            # Canonical shared lease directory is intentionally NOT relocated
            # into this private root: another host must still conflict.
            obj.authority = HostAuthority(obj.root / 'authority' / 'state.sqlite3')
            grant = grant_provider(obj.authority, 'initial')
            _require(type(grant) is ParentGrant, 'Trusted qualification parent grant required')
            obj.authority._require_parent_grant(grant)
            obj.device_id = 'qualification-' + uuid.uuid4().hex
            capabilities = dict(actions=list(data['capabilities']['actions']), resetContract=None,
                authorityMode='shared-v2', applicationIdentity=profile.application_identity,
                applicationProfile=data, applicationProfileDigest=profile.digest,
                automaticAppLogs=False, sdkCapture=False, verification='unqualified',
                inputMode='continuous-pointer', maxPointers=5, multitouch=True,
                timing='best-effort', media='jpeg-stream', pointerTimeoutSeconds=10)
            descriptor = dict(id=obj.device_id, name='Private Android qualification', platform='android',
                kind='android-live', capabilities=capabilities,
                _authority={'deviceKind':'android', 'physicalId':serial}, factory=obj._factory)
            obj._lab = Lab([descriptor], obj.root / 'live', authority=obj.authority, parent_grant=grant)
            obj._check()
            os.fsync(root_fd)
            os.fsync(parent_fd)
            return obj
        except BaseException:
            if obj is not None and hasattr(obj, 'authority'):
                obj.authority.close()
            if root_fd is not None: os.close(root_fd)
            os.close(parent_fd)
            # Preserve any created state; never recursively remove a possibly
            # changed output or manufacture proof of native cleanup.
            raise

    def _check(self):
        _require(not self._closed and self._pid == os.getpid() and self._thread == threading.get_ident(),
            'Live private qualification owner required')
        from .android_recovery_context import IsolatedAdbContext
        _require(type(self._context) is IsolatedAdbContext, 'Owned isolated ADB context required')
        self._context.require_bound(self.serial, self.profile.data['package'],
                                    self.helper_digest, self.profile.data['artifact']['sha256'])
        parent = _directory(self._parent)
        try:
            _require((os.fstat(parent).st_dev, os.fstat(parent).st_ino) ==
                (os.fstat(self._parent_fd).st_dev, os.fstat(self._parent_fd).st_ino),
                'Qualification output parent changed')
        finally:
            os.close(parent)
        current = os.stat(self.root.name, dir_fd=self._parent_fd, follow_symlinks=False)
        pinned = os.fstat(self._root_fd)
        _require((current.st_dev, current.st_ino) == (pinned.st_dev, pinned.st_ino),
            'Qualification output changed')

    def _factory(self):
        from .android_recovery_transport import QualificationAndroidProvider
        self._check()
        _require(self._provider is None and self._journal is None, 'Qualification session is one-use')
        provider = QualificationAndroidProvider(self, self.serial, self.helper_apk, self.app_apk,
            runtime_profile=self.profile, helper_digest=self.helper_digest, work=self.root,
            context=self._context)
        self._provider = provider
        return provider

    def start(self):
        self._check()
        _require(self._session is None and self._provider is None, 'Qualification session is one-use')
        result = self._lab.create_session(self.device_id, 'qualification', 'qualification-controller',
            _session_id=self.session_id)
        self._session = self._lab.sessions[self.session_id]
        _require(self._journal is not None and self._journal.state == 'producer-running',
            'Qualification startup unconfirmed')
        return {'sessionId':self.session_id, 'qualification':'unqualified',
            'normalRecoveryEnabled':False, 'state':result['state']}

    def bind_initial(self, provider, session, permit):
        self._check()
        _require(provider is self._provider and self._journal is None
            and session is self._lab.sessions.get(self.session_id)
            and session['id'] == self.session_id and provider.lab is self._lab,
            'Private qualification session required')
        handle = provider.device_authority
        handle.check_dispatch_permit(permit)
        _require(handle._authority is self.authority
            and handle._device_fingerprint == canonical_device_fingerprint('android', self.serial)
            and self._lab.devices[self.device_id]['capabilities'].get('recovery') is not True,
            'Private qualification ownership differs')
        binding = dict(schemaVersion=1, kind='reproof-android-recovery-qualification',
            attemptId=self._attempt, sessionId=self.session_id,
            deviceScopeDigest=handle._device_fingerprint, applicationId=self.profile.data['applicationId'],
            artifactDigest=self.profile.data['artifact']['sha256'], profileDigest=self.profile.digest,
            helperArtifactDigest=self.helper_digest, priorOwnershipGeneration=handle.generation,
            hostIncarnation=permit.host_incarnation, helperIncarnation=permit.helper_incarnation,
            providerIncarnation=permit.provider_incarnation)
        self._journal = QualificationJournal.create(self.root / 'recovery-journal', binding)
        self._session = session
        return _config(binding, 'session')[0]

    def require_initial(self, provider):
        self._check()
        _require(provider is self._provider and self._journal is not None
            and self._journal.state in {'session-bound', 'producer-start-pending'},
            'Private qualification startup unavailable')

    def initial_dispatch(self, provider):
        self.require_initial(provider)
        self._journal.advance('session-bound', 'producer-start-pending')

    def initial_started(self, provider):
        self.require_initial(provider)
        _require(provider.startup_status is not None and provider.target_launched,
            'Qualification startup evidence missing')
        self._journal.advance('producer-start-pending', 'producer-running',
            _digest(provider.startup_status))

    def failure(self, provider=None):
        self._check()
        _require(provider is None or provider is self._provider, 'Qualification producer differs')
        if self._journal is not None and self._journal.state not in {'blocked', 'released'}:
            self._journal.block('recovery-unconfirmed')

    def recover(self):
        """Run the private protocol only after this session is quarantined.

        This does not make either the private Lab or any normal Lab available.
        No success is returned until private durable authority reconciliation and
        producer collection finish. Original unknown receipts remain untouched.
        """
        from .android_recovery_transport import _ProbeProducer
        self._check()
        _require(not self._recovery_attempted and self._session is not None
            and self._journal is not None and self._journal.state == 'producer-running',
            'Qualification recovery is one-use')
        session = self._session
        with session['lock']:
            _require(session['state'] == 'failed' and session.get('recordingId') is None
                and not session.get('inflightCommands') and not session.get('inflightFrames')
                and not session.get('inflightCollections') and session.get('releaseRecorder') is None
                and self._lab.devices[self.device_id]['state'] == 'quarantined',
                'Quarantined idle private session required')
            session['stopAdmission'] = True
            handle = session.get('deviceAuthority')
            _require(handle is self._provider.device_authority and handle._authority is self.authority,
                'Qualification authority differs')
        original_receipts = _digest(session['receipts'])
        original_known_start = session['knownStart']
        self._recovery_attempted = True
        self._journal.advance('producer-running', 'quarantined')
        issuer = None
        try:
            grant = self._grant_provider(self.authority, 'recovery')
            _require(type(grant) is ParentGrant, 'Trusted recovery parent grant required')
            snapshot = handle.recovery_snapshot()
            with handle.borrow_native_recovery_lease(snapshot, parent_grant=grant) as lease:
                issuer = RecoveryIssuer(handle, lease, grant, self._journal, self.cancel.is_set)
                self._active_issuer = issuer
                old = _RecoveryChannel(self, 'old', self._provider,
                    _config(self._journal.binding, 'session')[0])
                self._journal.advance('quarantined', 'old-retire-pending')
                old_terminal = old.operation(issuer, 'old-retire')
                self._journal.advance('old-retire-pending', 'old-terminal', old_terminal)
                self._journal.advance('old-terminal', 'old-exit-pending')
                old_exit = old.operation(issuer, 'old-exit')
                self._journal.advance('old-exit-pending', 'old-exited', old_exit)
                self._journal.advance('old-exited', 'probe-start-pending')
                configuration = _config(self._journal.binding, 'probe')[0]
                self._probe = _ProbeProducer(self._provider, configuration,
                    'helper_' + uuid.uuid4().hex, 'provider_' + uuid.uuid4().hex, issuer)
                self._probe.start()
                issuer.check()
                probe = _RecoveryChannel(self, 'probe', self._probe, configuration)
                self._journal.advance('probe-start-pending', 'probe-running', _digest(probe.read_status()))
                self._journal.advance('probe-running', 'fresh-probe-pending')
                probe_frame = probe.operation(issuer, 'fresh-probe')
                self._journal.advance('fresh-probe-pending', 'probe-frame', probe_frame)
                self._journal.advance('probe-frame', 'probe-retire-pending')
                probe_terminal = probe.operation(issuer, 'probe-retire')
                self._journal.advance('probe-retire-pending', 'probe-terminal', probe_terminal)
                self._journal.advance('probe-terminal', 'probe-exit-pending')
                probe_exit = probe.operation(issuer, 'probe-exit')
                self._journal.advance('probe-exit-pending', 'probe-exited', probe_exit)
                issuer.check()
                with session['lock']:
                    _require(_digest(session['receipts']) == original_receipts
                        and session['knownStart'] is original_known_start
                        and not session.get('inflightCommands'), 'Original session changed during recovery')
                self._journal.advance('probe-exited', 'reconcile-pending', probe_exit)
                current = handle.recovery_snapshot()
                dispositions = [self.authority.record_operation_disposition(current,
                    operation_id=operation_id, terminal_status='not-dispatched' if status == 'queued' else 'recovered',
                    result_digest=probe_frame, evidence_digest=probe_exit)
                    for operation_id, status in current.operations]
                reconciliation = self.authority.record_reconciliation(current, dispositions=dispositions,
                    prior_helper_exit_digest=old_exit, pointer_cleanup_digest=old_terminal,
                    fresh_helper_incarnation=probe.helper_incarnation, fresh_handshake_digest=probe_frame)
                issuer.check()
            issuer.close()
            self._active_issuer = None
            self._check()
            _require(not self.cancel.is_set(), 'Qualification cancelled')
            self.authority._require_parent_grant(grant)
            handle.reconcile(reconciliation, parent_grant=grant)
            self._journal.advance('reconcile-pending', 'reconciled', reconciliation.reconciliation_fingerprint)
            self._journal.advance('reconciled', 'release-pending')
            _require(not self.cancel.is_set(), 'Qualification cancelled')
            self.authority._require_parent_grant(grant)
            _require(handle.close(), 'Private qualification release unconfirmed')
            self._journal.advance('release-pending', 'released', reconciliation.reconciliation_fingerprint)
            return {'qualification':'unqualified', 'normalRecoveryEnabled':False,
                'privateAuthorityReleased':True, 'originalInputOutcome':'unchanged',
                'startingStatePromoted':False, 'reconciliationDigest':reconciliation.reconciliation_fingerprint}
        except BaseException:
            if not handle._closed:
                try: handle.revoke_dispatches()
                except ContractError: pass
            if self._journal.state not in {'blocked', 'released'}:
                self._journal.block('recovery-unconfirmed')
            raise
        finally:
            if issuer is not None: issuer.close()
            self._active_issuer = None

    def close(self):
        """Close diagnostic FDs only; never pretend native producers were stopped."""
        self._check()
        _require(self._journal is None or self._journal.state == 'released',
            'Uncollected qualification producer; quarantine and output retained')
        self.authority.close()
        if self._journal is not None: self._journal.close()
        self._closed = True
        os.close(self._root_fd)
        os.close(self._parent_fd)
