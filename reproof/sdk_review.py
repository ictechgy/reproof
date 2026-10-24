"""Bounded SDK annotations for human review, never dispatch or repair authority.

Only the repository sample policies are supported. CollectorBinding is supplied
by an authenticated operator/collector, never deserialized from the SDK export.
Raw-byte digests and session identifiers remain private to the in-memory envelope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import uuid

from . import contracts
from .android_profile import sample_app_profile
from .core import compile_capture
from .ios_cases import case_spec
from .ios_core import APPLICATION_ID, compile_ios_capture, validate_finalization
from .storage import _unique_object
from .issue_package import canonical
from .ticket_intake import IDENTITY, RegisteredCapsule, _ticket, intake_ticket

MAX_BYTES = 256 * 1024
MAX_EVENTS = 256
UNRESOLVED = ('tenantId', 'projectId', 'buildId', 'sourceDigest', 'recordingDigest',
              'installedApplicationIdentity', 'injectedDispatch', 'authoredOracle',
              'typedAppLogs', 'approval')
BASE = {'schemaVersion': 1, 'kind': 'sdk-review-envelope', 'executionAuthority': 'none',
        'approved': False, 'aiTransfer': False, 'actualAI': False,
        'nativeExecution': False, 'companyAcceptance': False, 'repairReady': False}


class ReviewInvalid(ValueError):
    """Fixed code only: never expose parser or payload text."""


def _need(ok):
    if not ok:
        raise ReviewInvalid('sdk_export_invalid')


def export_digest(platform, capture, metadata, marker=None):
    """Collector-only exact-byte identity, with unambiguous length framing."""
    _need(platform in ('android', 'ios'))
    pieces = (platform.encode(), capture, metadata, marker if marker is not None else b'')
    _need(all(type(p) is bytes and len(p) <= MAX_BYTES for p in pieces))
    return hashlib.sha256(b''.join(len(p).to_bytes(8, 'big') + p for p in pieces)).hexdigest()


def _parse(raw):
    _need(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES)
    value = json.loads(raw, object_pairs_hook=_unique_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    _need(type(value) is dict)
    # Bound depth, arrays, scalar lengths, integer sizes before policy validation.
    def bounded(v, depth=0):
        _need(depth <= 8)
        if type(v) is dict:
            _need(len(v) <= 32)
            for k, item in v.items():
                _need(len(k) <= 64)
                bounded(item, depth + 1)
        elif type(v) is list:
            _need(len(v) <= MAX_EVENTS)
            for item in v:
                bounded(item, depth + 1)
        elif type(v) is str:
            _need(len(v) <= 256)
        else:
            _need(type(v) in (bool, int) and (type(v) is bool or 0 <= v <= 2**53 - 1))
    bounded(value)
    return value


@dataclass(frozen=True)
class ReviewEnvelope:
    _digest: str = field(repr=False)
    _session: str = field(repr=False)
    _platform: str
    _application: str
    _actions: tuple

    def summary(self):
        # Copy on every read; no mutable payload or raw identifiers escape.
        return {**BASE, 'status': 'review-only', 'platform': self._platform,
                'policy': 'repository-sample-semantic-allowlist-v1',
                'actions': [{'action': a, 'target': t} for a, t in self._actions],
                'applicationIdentityEvidence': 'policy-declared-not-installed-attestation',
                'dispatchEvidence': 'not-established-sdk-annotations',
                'unresolved': list(UNRESOLVED)}


def normalize_export(*, platform, capture: bytes, metadata: bytes, marker: bytes | None = None):
    """Validate finalized SDK files or raise a non-echoing ReviewInvalid.

    Existing compilers are used solely as validators with repository policy
    oracles. Their compiled scenarios/oracles are discarded, not exported.
    """
    try:
        identity = export_digest(platform, capture, metadata, marker)
        c, m = _parse(capture), _parse(metadata)
        _need(type(c.get('startedAtMs')) is int and c['startedAtMs'] >= 0)
        _need(type(c.get('events')) is list and 0 < len(c['events']) <= MAX_EVENTS)
        _need(type(c.get('sessionId')) is str
              and str(uuid.UUID(c['sessionId'])) == c['sessionId'])
        previous = 0
        for event in c['events']:
            _need(type(event) is dict and type(event.get('elapsedMs')) is int
                  and previous <= event['elapsedMs'] <= 600000)
            previous = event['elapsedMs']
        if platform == 'android':
            _need(marker is None)
            profile = sample_app_profile()
            compile_capture(c, profile.oracle(), app_profile=profile)
            _need(set(m) == {'schemaVersion', 'sessionId', 'fixture', 'startState',
                            'finalized', 'incomplete', 'lostEvents', 'unsupported'})
            _need(type(m['schemaVersion']) is int and m['schemaVersion'] == 1)
            _need(m['sessionId'] == c['sessionId'] and m['startState'] == c['startState'])
            _need(m['fixture'] == {'id': 'default', 'version': 1}
                  and type(m['fixture'].get('version')) is int)
            _need(m['finalized'] is True and all(m[k] is False for k in
                                               ('incomplete', 'lostEvents', 'unsupported')))
            application = 'io.reproof.sample'
        else:
            _need(set(m) == {'schemaVersion', 'platform', 'applicationId', 'sessionId',
                            'fixture', 'startState', 'startedAtMs', 'finalized', 'endSequence'})
            _need(type(m['schemaVersion']) is int and m['schemaVersion'] == 2
                  and m['platform'] == 'ios' and m['applicationId'] == APPLICATION_ID)
            _need(type(m['startedAtMs']) is int and type(m['endSequence']) is int)
            final = _parse(marker)
            _need(set(final) == {'finalized', 'endSequence'} and type(final['endSequence']) is int)
            # Narrow initial bridge: counter only, not arbitrary app policy registration.
            _need(canonical(c.get('fixture')) == canonical(case_spec('counter').fixture))
            compile_ios_capture(c, case_spec('counter').oracle())
            validate_finalization(c, m, final)
            _need(canonical(m['fixture']) == canonical(c['fixture'])
                  and canonical(m['startState']) == canonical(c['startState']))
            application = APPLICATION_ID
        return ReviewEnvelope(identity, c['sessionId'], platform, application,
                              tuple((e['action'], e['target']) for e in c['events']))
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ReviewInvalid('sdk_export_invalid') from None


@dataclass(frozen=True)
class CollectorBinding:
    """Trusted registration, not authentication or SDK-provided authority.

    Explicitly links exact export bytes to separate evidence; it does not assert
    that SDK annotations are injected receipts. Caller must authorize this link.
    """
    export_digest: str
    sdk_session_id: str
    sdk_platform: str
    sdk_application_id: str
    capsule_digest: str
    ticket_identity: tuple[str, ...]


def linked_review(*, envelope: ReviewEnvelope, binding: CollectorBinding | None,
                  ticket: bytes, tenant_id: str, entry: RegisteredCapsule):
    """Invoke unchanged strict intake only after exact trusted collector binding."""
    base = {**BASE, 'kind': 'sdk-linked-ticket-review'}
    try:
        if binding is None:
            return {**base, 'status': 'abstained', 'reason': 'collector_binding_missing'}
        _need(type(envelope) is ReviewEnvelope and type(binding) is CollectorBinding
              and type(entry) is RegisteredCapsule)
        parsed = _ticket(ticket)
        _need(binding.export_digest == envelope._digest
              and binding.sdk_session_id == envelope._session
              and binding.sdk_platform == envelope._platform
              and binding.sdk_application_id == envelope._application
              and binding.capsule_digest == hashlib.sha256(entry.capsule).hexdigest()
              and type(binding.ticket_identity) is tuple
              and binding.ticket_identity == tuple(parsed[k] for k in IDENTITY))
        apps = entry.project['applications']
        _need(any(a['id'] == parsed['applicationId']
                  and a['platform'] == envelope._platform
                  and a['bundle'] == envelope._application for a in apps))
        result = intake_ticket(ticket, tenant_id=tenant_id, registry=(entry,))
        if result['status'] != 'review-required':
            return {**base, 'status': 'abstained', 'reason': 'separate_evidence_ineligible'}
        return {**base, 'status': 'review-required', 'draft': result,
                'sdkEvidenceRole': 'supplemental-annotations-only',
                'bindingAuthority': 'caller-trusted-collector-not-sdk'}
    except (ValueError, TypeError, KeyError, RecursionError, contracts.ContractError):
        return {**base, 'status': 'abstained', 'reason': 'collector_binding_mismatch'}
