"""One private deliberate-ACK-omission trial; no ordinary recovery activation.

Calling run_one_lost_ack is effectful and requires separate runtime permission.
The fault observes an authentic transport response, then intentionally withholds
its delivery to the input caller. It is not evidence of naturally lost traffic.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import uuid

from reproof.core import ContractError
from reproof.android_profile import AndroidRuntimeProfile, validate_android_runtime_profile
from .android_recovery_birth import BirthPlan, BirthIncomplete, launch_qualification_birth
from .android_recovery_diagnostics import birth_summary
from .android_recovery_qualification import QualificationHarness
from .android_recovery_preflight import run_sandbox_preflight
from .configuration import issue_bounded_project_grant
from .model import LiveError


def _require(value, message='Private qualification trial unavailable'):
    if not value:
        raise ContractError(message)


def _encoded(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    _require(len(raw) <= 16384, 'Private trial evidence exceeds its bound')
    return raw


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


class _TapAckLoss:
    """Private, one-operation wrapper around the real qualification bridge."""
    def __init__(self, bridge, operation_id):
        from .android_recovery_transport import _QualificationBridge
        _require(type(bridge) is _QualificationBridge and not bridge._probe)
        _require(type(operation_id) is str and operation_id.startswith('operation_')
                 and len(operation_id) == 50 and all(c in '0123456789abcdef' for c in operation_id[10:]))
        self._bridge = bridge
        self._operation = operation_id
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._command = None
        self._accepted = False
        self._dropped = False
        self._ack_digest = None
        self._lock = threading.Lock()

    def call(self, path, body=None, timeout=5, binary=False, *, guard=None):
        # Recovery has its own authenticated channel. The deliberate fault never
        # observes or mutates those receipts, nor status/frame polling results.
        selected = path == '/command' or path == '/ack/' + self._operation
        if not selected:
            return self._bridge.call(path, body, timeout, binary, guard=guard)
        with self._lock:
            _require(self._pid == os.getpid() and self._thread == threading.get_ident()
                     and guard is None and not binary and not self._dropped)
            if path == '/command':
                _require(self._command is None and type(body) is dict
                         and set(body) == {'id', 'action', 'payload', 'geometryVersion', 'authority'}
                         and body['id'] == self._operation and body['action'] == 'tap'
                         and _encoded(body['payload']) == b'{"x":0.5,"y":0.5}'
                         and type(body['geometryVersion']) is int and body['geometryVersion'] > 0
                         and type(body['authority']) is dict)
                self._command = copy.deepcopy(body)
            else:
                _require(self._accepted and self._command is not None)
        response = self._bridge.call(path, body, timeout, binary, guard=guard)
        with self._lock:
            if path == '/command':
                _require(type(response) is dict and response.get('accepted') is True,
                         'Selected trial input was not accepted')
                self._accepted = True
                return response
            if type(response) is dict and response.get('pending') is True:
                _require(set(response) == {'pending'})
                return response
            matches = (type(response) is dict
                       and set(response) == {'pending', 'id', 'ok', 'timing', 'authority'}
                       and response['pending'] is False and response['ok'] is True
                       and response['id'] == self._operation and response['timing'] == 'best-effort'
                       and _encoded(response['authority']) == _encoded(self._command['authority']))
            if not matches:
                # Preserve the ordinary provider's validation/rejection semantics;
                # a different or rejected response is not this deliberate fault.
                return response
            self._ack_digest = _digest(response)
            self._dropped = True
        # AndroidLiveProvider._execute does not retry this exception. Lab.input
        # records its original unknown receipt and quarantines through its normal
        # failure path. Never replace that receipt with this observer's knowledge.
        raise LiveError('injection_unknown', 'Private trial deliberately withheld one acknowledgement')

    def evidence(self):
        with self._lock:
            return {'deliberatelyOmittedResponses': int(self._dropped),
                    'observedAckDigest': self._ack_digest,
                    'fault': 'deliberate-matching-ack-delivery-omission',
                    'naturalLossProven': False}


class TrialIncomplete(ContractError):
    def __init__(self, report, owner=None, harness=None):
        super().__init__('Private trial incomplete; inspect retained evidence and owned resources')
        self.report = copy.deepcopy(report)
        self.owner, self.harness = owner, harness


def _grant(authority, phase):
    _require(phase in {'initial', 'recovery'})
    return issue_bounded_project_grant(authority, 'android-recovery-qualification', lifetime_seconds=300)


def _live(owner, cancellation):
    _require(not cancellation.is_set(), 'Private trial cancelled')
    owner.require_live()


def _frame(harness, owner, cancellation):
    until = min(owner.deadline, time.monotonic() + 30)
    while True:
        _live(owner, cancellation)
        _require(time.monotonic() < until, 'Private trial frame deadline expired')
        session = harness._session
        with session['lock']:
            _require(session['state'] in {'connecting', 'active'}, 'Private trial startup failed')
            if session['state'] == 'active':
                try:
                    return harness._lab._frame_meta(session)
                except LiveError as error:
                    if error.code != 'frame_pending':
                        raise
        cancellation.wait(.02)


def _stop_pollers(harness):
    """Local thread collection only; no provider.close or native stop command."""
    if harness is None:
        return True
    selected = [item for item in (harness._provider, harness._probe) if item is not None]
    clean = True
    for producer in selected:
        stop = getattr(producer, 'stop', None)
        if stop is not None:
            try:
                stop.set()
            except (RuntimeError, OSError):
                clean = False
    until = time.monotonic() + 5
    for producer in selected:
        thread = getattr(producer, 'thread', None)
        if thread is not None:
            try:
                thread.join(max(0, until - time.monotonic()))
                clean = not thread.is_alive() and clean
            except (RuntimeError, OSError):
                clean = False
    return clean


def run_one_lost_ack(plan, runtime_profile, *, sandbox_probe, cancellation,
                     allow_vulkan_virtual_queue_advisory=False):
    """Birth, one real tap with deliberately omitted ACK, recovery, collection.

    Source/synthetic validation does not authorize invoking this function. It
    can create temporary authentication data, boot a new AVD, and first-install
    two selected public APKs. No existing AVD or production Lab is accepted.
    """
    _require(type(allow_vulkan_virtual_queue_advisory) is bool,
             'Private advisory option must be boolean')
    _require(type(plan) is BirthPlan and plan.lifetime_seconds <= 900
             and type(cancellation) is threading.Event and not cancellation.is_set())
    profile = runtime_profile if type(runtime_profile) is AndroidRuntimeProfile else validate_android_runtime_profile(runtime_profile)
    data = profile.data
    _require(data['package'] == 'io.reproof.sample'
             and data['artifact']['sha256'] == plan.sample_apk.sha256
             and data['artifact']['bytes'] == plan.sample_apk.size
             and 'tap' in data['capabilities']['actions'] and 'launch' in data['capabilities']['actions']
             and data['capabilities']['logAdapter'] is None,
             'Only the selected public sample profile is supported')
    # This is a direct effectful check, not an imported success flag. Failure
    # retains its own preflight owner and occurs before any ADB/AVD birth.
    preflight = run_sandbox_preflight(plan, sandbox_probe, cancellation=cancellation)
    owner = harness = fault = None
    report = {'schemaVersion': 1, 'kind': 'reproof-private-ack-omission-trial',
              'trialPassed': False, 'normalRecoveryEnabled': False,
              'vulkanAdvisoryRequested': allow_vulkan_virtual_queue_advisory,
              'runtimeQualificationGeneralized': False, 'startingStatePromoted': False,
              'privateDataRetained': True, 'diagnosticStateRetained': True,
              'cleanupScope': 'local-processes-and-pollers-only',
              'authorityDisposition': 'no-returned-harness', 'phase': 'birth',
              'sandboxPreflight': preflight, 'profileDigest': profile.digest, 'appArtifactDigest': plan.sample_apk.sha256,
              'helperArtifactDigest': plan.helper_apk.sha256, 'adbDigest': plan.adb.sha256}
    try:
        owner = launch_qualification_birth(plan, cancellation=cancellation,
            allow_vulkan_virtual_queue_advisory=allow_vulkan_virtual_queue_advisory)
        _live(owner, cancellation)
        report['phase'] = 'session'
        harness = QualificationHarness.create(owner.root,
            serial='emulator-' + str(plan.console_port), helper_apk=plan.helper_apk.path,
            app_apk=plan.sample_apk.path, runtime_profile=profile,
            grant_provider=_grant, context=owner.context)
        report['authorityDisposition'] = 'release-unconfirmed'
        harness.cancel = cancellation
        harness.start()
        frame = _frame(harness, owner, cancellation)
        from .android_recovery_transport import QualificationAndroidProvider
        _require(type(harness._provider) is QualificationAndroidProvider)
        operation = 'operation_' + uuid.uuid4().hex + uuid.uuid4().hex[:8]
        fault = _TapAckLoss(harness._provider.transport, operation)
        original_transport = harness._provider.transport
        harness._provider.transport = fault
        report['phase'] = 'deliberate-ack-omission'
        session = harness._session
        command = {'controllerId': session['controllerId'], 'epoch': session['epoch'],
                   'sequence': session['lastSequence'] + 1, 'commandId': operation,
                   'frameId': frame['id'], 'geometryVersion': frame['geometryVersion'],
                   'action': 'tap', 'payload': {'x': .5, 'y': .5}}
        try:
            _live(owner, cancellation)
            try:
                harness._lab.input(harness.session_id, 'qualification', command)
            except LiveError as error:
                _require(error.code == 'injection_unknown')
            else:
                raise ContractError('The trial did not produce an uncertain input')
        finally:
            harness._provider.transport = original_transport
        report.update(fault.evidence())
        _require(report['deliberatelyOmittedResponses'] == 1)
        with session['lock']:
            receipt = session['receipts'][operation][1]
            _require(receipt['status'] == 'unknown' and session['knownStart'] is False
                     and session['state'] == 'failed'
                     and harness._lab.devices[harness.device_id]['state'] == 'quarantined')
            original_digest = _digest(session['receipts'])
        report.update(originalInputOutcome='unknown', originalReceiptsDigest=original_digest)
        _live(owner, cancellation)
        report['phase'] = 'recovery'
        result = harness.recover()
        _require(result['privateAuthorityReleased'] is True and result['normalRecoveryEnabled'] is False
                 and result['startingStatePromoted'] is False)
        report.update(privateAuthorityReleased=True, authorityDisposition='released-by-recovery')
        with session['lock']:
            _require(_digest(session['receipts']) == original_digest
                     and session['receipts'][operation][1]['status'] == 'unknown'
                     and session['knownStart'] is False
                     and harness._lab.devices[harness.device_id]['state'] == 'quarantined')
        report.update(originalInputOutcome='unknown', privateAuthorityReleased=True,
                      authorityDisposition='released-by-recovery',
                      originalReceiptsDigest=original_digest,
                      reconciliationDigest=result['reconciliationDigest'])
        report['phase'] = 'collection'
        _require(_stop_pollers(harness), 'Private trial poller collection incomplete')
        harness.close()
        collected = owner.collect()
        _require(collected['processesCollected'] is True)
        report['birthDiagnostics'] = birth_summary(owner)
        report.update(trialPassed=True, phase='complete', processesCollected=True, pollersJoined=True,
                      liveDiagnosticOwnerRetained=False)
        return report
    except BaseException as error:
        if isinstance(error, BirthIncomplete):
            owner = error.owner
        pollers = _stop_pollers(harness)
        report['pollersJoined'] = pollers
        report['liveDiagnosticOwnerRetained'] = harness is not None and not harness._closed
        if fault is not None:
            report.update(fault.evidence())
        if owner is not None:
            try:
                report['abort'] = owner.abort()
            except BaseException:
                report['abort'] = {'processesCollected': False, 'cleanupIncomplete': True,
                                   'recoverySucceeded': False}
        if owner is not None:
            report['birthDiagnostics'] = birth_summary(owner)
        report['cleanupIncomplete'] = not pollers or not report.get('abort', {}).get('processesCollected', owner is None)
        raise TrialIncomplete(report, owner, harness) from None

