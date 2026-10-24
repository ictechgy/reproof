"""Reference-only QA ticket intake. This module never saves or approves an issue.

The service must authorize and pin its registry entries for the requesting tenant
before calling this API. Registry construction is not an authentication API.
Imported capsules and tickets cannot register themselves. All text and evidence
stay out of the returned draft; reviewers resolve digests through existing,
authorized IssueWorkflow/package APIs. No AI transfer or execution occurs here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import uuid
import zipfile

from . import contracts
from .app_logs import APP_LOG_MIME, IDENTITY_KEYS, validate_app_log
from .issue_package import PackageLimits, canonical, inspect_archive
from .storage import _unique_object

MAX_TICKET_BYTES = 16_384
MAX_REGISTRY_ENTRIES = 128
LIMITS = PackageLimits(max_archive_bytes=2 * 1024 * 1024,
    max_expanded_bytes=2 * 1024 * 1024, max_object_bytes=1024 * 1024,
    max_json_bytes=1024 * 1024, max_objects=128)
IDENTITY = ('tenantId', 'projectId', 'applicationId', 'buildId',
            'sourceDigest', 'sessionId', 'recordingDigest')


@dataclass(frozen=True)
class RegisteredCapsule:
    """Locally authorized snapshot, not a transferable authority handle.

    ``app_log_digest`` must name a sealed observation in the capsule. ``run_id``
    and ``session_id`` come from the trusted collector, not ticket text.
    """
    tenant_id: str
    project: dict
    capsule: bytes
    app_log_digest: str
    run_id: str
    session_id: str
    profile_digest: str
    click_targets: tuple[str, ...] = ()
    screen_targets: tuple[str, ...] = ()


class _Abstain(ValueError):
    pass


def _need(value, code):
    if not value:
        raise _Abstain(code)


def _ticket(raw):
    _need(type(raw) is bytes and 0 < len(raw) <= MAX_TICKET_BYTES, 'ticket_invalid')
    value = json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    _need(type(value) is dict and set(value) in (
        {'schemaVersion', 'ticketId', *IDENTITY},
        {'schemaVersion', 'ticketId', 'text', *IDENTITY}), 'ticket_invalid')
    _need(type(value['schemaVersion']) is int and value['schemaVersion'] == 1, 'ticket_invalid')
    for key in ('ticketId', 'tenantId', 'projectId', 'applicationId', 'buildId'):
        contracts.validate_id(value[key])
    for key in ('sourceDigest', 'recordingDigest'):
        contracts.validate_digest(value[key])
    _need(type(value['sessionId']) is str and str(uuid.UUID(value['sessionId'])) == value['sessionId'],
          'ticket_invalid')
    if 'text' in value:
        _need(type(value['text']) is str and len(value['text'].encode('utf-8')) <= 8192,
              'ticket_invalid')
    # Free text is deliberately discarded, never parsed as commands or retained
    # even in a hash: secrets in prose must not leak through diagnostics or IDs.
    return {key: value[key] for key in ('ticketId', *IDENTITY)}


def _selected(entry, ticket):
    _need(type(entry) is RegisteredCapsule, 'registry_invalid')
    project = contracts.validate_project_revision(entry.project)
    return entry.tenant_id == ticket['tenantId'] and project['id'] == ticket['projectId']


def _validate(entry, ticket):
    project = contracts.validate_project_revision(entry.project)
    _need(project['evidencePolicy']['logs'] is True, 'logs_denied')
    _need(type(entry.capsule) is bytes and len(entry.capsule) <= LIMITS.max_archive_bytes,
          'capsule_invalid')
    # Full closed-graph package checks are reused; imported qualification is ignored.
    contents = inspect_archive(io.BytesIO(entry.capsule), limits=LIMITS)
    original = contents.recording['original']
    spec = contents.specification
    actual = {'tenantId': entry.tenant_id, 'projectId': project['id'],
        'applicationId': original['applicationId'], 'buildId': original['buildId'],
        'recordingDigest': contracts.digest(original), 'sessionId': entry.session_id}
    build = next((b for b in project['builds'] if b['id'] == original['buildId']), None)
    _need(build is not None, 'identity_mismatch')
    actual['sourceDigest'] = build.get('sourceDigest')
    _need(all(ticket[key] == actual[key] for key in IDENTITY)
          and original['projectId'] == project['id']
          and original['projectRevision'] == project['revision']
          and build['applicationId'] == original['applicationId'], 'identity_mismatch')
    _need(contents.recording['status'] == 'frozen-complete'
          and not original['unknowns'] and not original['interruptions']
          and original['events'] and all(e['dispatch'] == 'injected' for e in original['events'])
          and all(p['status'] == 'complete' for p in original['preparation']), 'evidence_incomplete')
    _need(any(a['role'] == 'expected' for a in spec['assertions']), 'oracle_missing')
    _need(spec['originalRecordingDigest'] == actual['recordingDigest'], 'identity_mismatch')
    app = next(a for a in project['applications'] if a['id'] == actual['applicationId'])
    logs = [r for r in original['observations'] if r['mimeType'] == APP_LOG_MIME]
    # Multiple snapshots/sessions require explicit consolidation by the existing
    # collector, never a fuzzy "latest" choice by ticket intake.
    _need(len(logs) == 1 and logs[0]['digest'] == entry.app_log_digest, 'log_ambiguous')
    with zipfile.ZipFile(io.BytesIO(entry.capsule)) as archive:
        raw = archive.read('objects/' + entry.app_log_digest)
    _need(hashlib.sha256(raw).hexdigest() == entry.app_log_digest, 'identity_mismatch')
    log = json.loads(raw, object_pairs_hook=_unique_object)
    _need(type(log) is dict, 'log_invalid')
    marker = {key: log.get(key) for key in IDENTITY_KEYS}
    _need(marker.get('platform') == app['platform']
          and marker.get('applicationId') == app['bundle']
          and marker.get('runId') == entry.run_id
          and marker.get('sessionId') == entry.session_id
          and marker.get('profileDigest') == entry.profile_digest, 'identity_mismatch')
    log = validate_app_log(log, marker, click_targets=entry.click_targets,
                           screen_targets=entry.screen_targets)
    _need(not log['lostEvents'] and not log['truncated'] and log['events'], 'evidence_incomplete')
    return {'projectDigest': contracts.digest(project),
            'recordingDigest': actual['recordingDigest'],
            'specificationDigest': contracts.digest(spec),
            'sourceDigest': actual['sourceDigest'],
            'capsuleDigest': hashlib.sha256(entry.capsule).hexdigest(),
            'appLogDigest': entry.app_log_digest}


def intake_ticket(raw: bytes, *, tenant_id: str, registry: tuple[RegisteredCapsule, ...]):
    """Return an inert digest-only review draft or a fixed non-echoing reason.

    No persistent state is changed. Repeated identical references produce the
    same draft ID. The caller must still use IssueWorkflow's separate explicit
    save/review/approve path; this result is not an authored specification.
    """
    base = {'schemaVersion': 1, 'kind': 'ticket-review-draft',
            'executionAuthority': 'none', 'approved': False, 'aiTransfer': False}
    try:
        ticket = _ticket(raw)
        _need(ticket['tenantId'] == tenant_id, 'tenant_mismatch')
        _need(type(registry) is tuple and len(registry) <= MAX_REGISTRY_ENTRIES, 'registry_invalid')
        candidates = [entry for entry in registry if _selected(entry, ticket)]
        matches = []
        for entry in candidates:
            # Do not select a near match or use external content to resolve one.
            try:
                matches.append(_validate(entry, ticket))
            except _Abstain as exc:
                if str(exc) != 'identity_mismatch':
                    raise
        _need(len(matches) == 1, 'correlation_unresolved' if not matches else 'correlation_ambiguous')
        references = matches[0]
        identifier = contracts.digest({'ticket': ticket, 'references': references})
        return {**base, 'status': 'review-required', 'draftId': identifier,
                'references': references, 'textPolicy': 'discarded-default-deny'}
    except _Abstain as exc:
        return {**base, 'status': 'abstained', 'reason': str(exc)}
    except (ValueError, TypeError, KeyError, StopIteration, UnicodeError,
            RecursionError, contracts.ContractError, OSError):
        return {**base, 'status': 'abstained', 'reason': 'input_invalid'}
