"""Repository-owned synthetic capsule for the local causal demonstration.

This is synthetic test data, not SDK capture or mobile dispatch evidence. It
exercises the real package/intake contracts without claiming native execution.
"""
from __future__ import annotations

import hashlib

from . import contracts
from .app_logs import APP_LOG_MIME
from .issue_package import build_archive, canonical
from .ticket_intake import RegisteredCapsule, intake_ticket

SESSION = '00000000-0000-4000-8000-000000000001'
RUN = '00000000-0000-4000-8000-000000000002'
PROFILE = hashlib.sha256(b'repository-synthetic-checkout-profile-v1').hexdigest()


def demo_draft(project, oracle_digest):
    """Correlate a fixed synthetic ticket to this source and frozen oracle.

    The returned review draft is inert. The demo runner's explicit invocation,
    not this document, selects the bounded local software exercise.
    """
    project = contracts.validate_project_revision(project)
    contracts.validate_digest(oracle_digest)
    app = project['applications'][0]
    build = project['builds'][0]
    log = {'schemaVersion': 1, 'platform': app['platform'], 'applicationId': app['bundle'],
        'runId': RUN, 'sessionId': SESSION, 'profileDigest': PROFILE, 'startedAtMs': 1000,
        'endSequence': 1, 'truncated': False, 'lostEvents': False,
        'events': [{'seq': 1, 'elapsedMs': 0, 'type': 'lifecycle', 'name': 'attached',
                    'component': 'application', 'componentId': 'app', 'target': None}]}
    raw_log = canonical(log)
    log_digest = hashlib.sha256(raw_log).hexdigest()
    action = {'action': 'launch', 'parameters': {'applicationId': app['id']}}
    original = {'schemaVersion': 1, 'recordingId': 'synthetic_checkout_recording',
        'projectId': project['id'], 'projectRevision': project['revision'],
        'applicationId': app['id'], 'buildId': build['id'], 'startedAtMs': 1000,
        'endSequence': 1, 'events': [{'id': 'event_one', 'operationId': 'operation_one',
            'generation': 1, 'sequence': 1, 'offsetMs': 0, 'input': action,
            'dispatch': 'injected',
            'provenance': {'kind': 'injected', 'source': 'synthetic-repository-fixture-not-device'},
            'receipt': {'operationId': 'operation_one', 'generation': 1,
                'status': 'injected', 'providerIncarnation': 'synthetic_not_device',
                'observedAtMs': 1000}}],
        'preparation': [], 'observations': [{'id': 'synthetic_log', 'digest': log_digest,
            'path': 'logs/synthetic.json', 'bytes': len(raw_log), 'mimeType': APP_LOG_MIME}],
        'media': [], 'clock': {'source': 'monotonic', 'uncertaintyMs': 0},
        'interruptions': [], 'unknowns': [], 'sealed': True}
    recording_digest = contracts.digest(original)
    assertions = []
    for role, value in (('defect', 5500), ('expected', 5000)):
        assertions.append({'id': role + '_total', 'role': role,
            'predicate': {'kind': 'property', 'observationId': 'checkout_total',
                'property': 'total_cents', 'operator': 'equals', 'value': value},
            'coverage': {'class': 'snapshot', 'windowMs': {'start': 0, 'end': 0},
                'maxUncertaintyMs': 0, 'maxAgeMs': 0, 'scope': 'root',
                'properties': ['total_cents']}, 'windowMs': 0, 'stabilityMs': 0})
    specification = {'schemaVersion': 1, 'id': 'synthetic_checkout_specification',
        'revision': 1, 'originalRecordingDigest': recording_digest,
        'actions': [{'eventId': 'event_one', **action}], 'waits': [], 'bindings': [],
        'fixtures': [], 'assertions': assertions,
        'provenance': {'kind': 'authored', 'source': 'repository-synthetic-oracle-fixture',
            'author': 'fixture_author_not_user_approval', 'revision': '1', 'digest': oracle_digest}}
    contracts.validate_specification(specification)
    recording = {'original': original, 'recordingDigest': recording_digest,
                 'status': 'frozen-complete', 'lifecycleReceipts': []}
    capsule = build_archive(recording, specification, lambda _: raw_log,
                            package_id='synthetic_checkout_capsule')
    entry = RegisteredCapsule('local_demo', project, capsule, log_digest, RUN, SESSION, PROFILE)
    ticket = {'schemaVersion': 1, 'ticketId': 'synthetic_shipping_threshold',
        'tenantId': 'local_demo', 'projectId': project['id'], 'applicationId': app['id'],
        'buildId': build['id'], 'sourceDigest': build['sourceDigest'], 'sessionId': SESSION,
        'recordingDigest': recording_digest,
        'text': 'Synthetic fixture: shipping should be free at the 5000-cent threshold.'}
    draft = intake_ticket(canonical(ticket), tenant_id='local_demo', registry=(entry,))
    # Independent binding checked by the caller before even the local exercise.
    return {**draft, 'demoBinding': {'syntheticEvidence': True,
        'oracleDigest': specification['provenance']['digest'],
        'specificationDigest': contracts.digest(specification)}}
