"""Policy-selected app observations derived from a sealed recording.

These events describe app callbacks and lifecycle changes. Their elapsed clock
is not mapped to recording time and they never supply actions or verdicts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from . import contracts
from .app_logs import (APP_LOG_MIME, IDENTITY_KEYS, MAX_APP_LOG_BYTES,
                       MAX_APP_LOG_EVENTS, MAX_APP_LOG_DURATION_MS,
                       LIFECYCLE_NAMES, validate_app_log, validate_app_log_marker)
from .execution.wire import ProtocolError, canonical
from .project_repair import RepairError, _require
from .storage import _unique_object


EVENT_FIELDS = frozenset({'seq', 'elapsedMs', 'type', 'name', 'component', 'target'})
REQUIRED_EVENT_FIELDS = frozenset({'seq', 'type', 'name'})
MAX_DIAGNOSTIC_BYTES = 384 * 1024
MAX_SOURCE_OBJECTS = 64
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_RUNS = 16
_TARGET = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,79}\Z')


def diagnostic_json(raw, *, maximum=MAX_DIAGNOSTIC_BYTES):
    def invalid(_):
        raise ValueError()
    try:
        _require(type(raw) is bytes and 0 < len(raw) <= maximum, 'diagnostics_limit')
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=invalid)
    except (contracts.ContractError, ValueError, UnicodeError, RecursionError):
        raise RepairError('diagnostics_invalid') from None


def diagnostic_fields(value, *, code='diagnostics_policy'):
    _require(type(value) is list and len(value) <= len(EVENT_FIELDS)
        and all(type(item) is str and item in EVENT_FIELDS for item in value)
        and len(set(value)) == len(value) and REQUIRED_EVENT_FIELDS <= set(value), code)
    return list(value)


def validate_diagnostic_policy(value, *, project=None, project_digest=None):
    try:
        _require(type(value) is dict and set(value) == {'schemaVersion', 'projectDigest',
            'applicationId', 'profileDigest', 'clickTargets', 'screenTargets', 'eventFields', 'maxEvents'},
            'diagnostics_policy')
        _require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1, 'diagnostics_policy')
        contracts.validate_digest(value['projectDigest']); contracts.validate_digest(value['profileDigest'])
        contracts.validate_id(value['applicationId'])
        for key in ('clickTargets', 'screenTargets'):
            items = value[key]
            _require(type(items) is list and len(items) <= 128
                and all(type(item) is str and _TARGET.fullmatch(item) for item in items)
                and len(items) == len(set(items)), 'diagnostics_policy')
        diagnostic_fields(value['eventFields'])
        _require(type(value['maxEvents']) is int and 1 <= value['maxEvents'] <= MAX_APP_LOG_EVENTS,
                 'diagnostics_policy')
        if project is not None:
            project = contracts.validate_project_revision(project)
            project_digest = contracts.digest(project)
            _require(project['evidencePolicy']['logs'] is True and any(
                app['id'] == value['applicationId'] for app in project['applications']), 'diagnostics_policy')
        if project_digest is not None:
            _require(value['projectDigest'] == project_digest, 'diagnostics_policy')
        return copy.deepcopy(value)
    except contracts.ContractError:
        raise RepairError('diagnostics_policy') from None


def diagnostic_references(original):
    """Select explicit typed references without opening unrelated observations."""
    references = [item for item in original['observations'] if item['mimeType'] == APP_LOG_MIME]
    _require(1 <= len(references) <= MAX_SOURCE_OBJECTS, 'diagnostics_unavailable')
    _require(all(item['bytes'] <= MAX_APP_LOG_BYTES for item in references)
        and sum(item['bytes'] for item in references) <= MAX_SOURCE_BYTES, 'diagnostics_limit')
    return copy.deepcopy(references)


def derive_app_log_diagnostics(project, original, source_digest, policy, *, read_object):
    """Read only pinned/authorized source objects supplied by the local service.

    The caller owns retention and authorization across the entire operation.
    This function checks bytes, app/profile identities, snapshot prefixes and
    project/build/source bindings before selecting any diagnostic field.
    """
    try:
        project = contracts.validate_project_revision(project)
        original = contracts.validate_original_evidence(original)
        policy = validate_diagnostic_policy(policy, project=project)
        build = next((item for item in project['builds'] if item['id'] == original['buildId']), None)
        _require(original['projectId'] == project['id'] and original['projectRevision'] == project['revision']
            and original['applicationId'] == policy['applicationId'] and build is not None
            and build['applicationId'] == original['applicationId']
            and build.get('sourceDigest') == source_digest, 'diagnostics_binding')
        app = next(item for item in project['applications'] if item['id'] == original['applicationId'])
        _require(callable(read_object), 'diagnostics_unavailable')
        snapshots, sources = {}, []
        for reference in diagnostic_references(original):
            raw = read_object(copy.deepcopy(reference))
            _require(type(raw) is bytes and len(raw) == reference['bytes']
                and hashlib.sha256(raw).hexdigest() == reference['digest'], 'diagnostics_binding')
            value = diagnostic_json(raw, maximum=MAX_APP_LOG_BYTES)
            _require(type(value) is dict and canonical(value) == raw, 'diagnostics_invalid')
            marker = validate_app_log_marker({key: value.get(key) for key in IDENTITY_KEYS},
                platform=app['platform'], application_id=app['bundle'],
                profile_digest=policy['profileDigest'], run_id=value.get('runId'))
            value = validate_app_log(value, marker, click_targets=set(policy['clickTargets']),
                                     screen_targets=set(policy['screenTargets']))
            key = value['sessionId']
            if key in snapshots:
                previous, digests = snapshots[key]
                _require(all(previous[field] == value[field] for field in IDENTITY_KEYS)
                    and value['events'][:len(previous['events'])] == previous['events']
                    and (not previous['truncated'] or value['truncated'])
                    and (not previous['lostEvents'] or value['lostEvents']), 'diagnostics_binding')
            else:
                _require(len(snapshots) < MAX_RUNS, 'diagnostics_limit')
                digests = []
            if reference['digest'] not in digests: digests.append(reference['digest'])
            snapshots[key] = (value, digests)
            if reference['digest'] not in sources: sources.append(reference['digest'])
        runs, remaining = [], policy['maxEvents']
        for value, digests in snapshots.values():
            selected = value['events'][:remaining]
            remaining -= len(selected)
            events = [{field: event[field] for field in policy['eventFields']} for event in selected]
            for event in events:
                if 'target' in event and event['target'] not in policy['clickTargets'] + policy['screenTargets']:
                    event['target'] = None
            runs.append({'runDigest': contracts.digest({field: value[field] for field in IDENTITY_KEYS}),
                'sourceObjects': digests, 'clock': 'app-elapsed-unmapped',
                'truncated': value['truncated'], 'lostEvents': value['lostEvents'],
                'totalEvents': len(value['events']), 'omittedEvents': len(value['events']) - len(events),
                'events': events})
        result = {'schemaVersion': 1, 'purpose': 'diagnostic-only',
            'projectDigest': contracts.digest(project), 'recordingDigest': contracts.digest(original),
            'buildId': build['id'], 'sourceDigest': source_digest, 'artifactDigest': build['artifactDigest'],
            'policyDigest': contracts.digest(policy), 'sourceObjects': sources,
            'eventFields': list(policy['eventFields']), 'runs': runs}
        _require(len(canonical(result)) <= MAX_DIAGNOSTIC_BYTES, 'diagnostics_limit')
        return result
    except (contracts.ContractError, ProtocolError, KeyError, ValueError, TypeError, StopIteration, RecursionError):
        raise RepairError('diagnostics_invalid') from None


def project_diagnostics(value, project, source_digest, specification, *, fields=None):
    """Validate a derived document again, then select fields for this recipient."""
    try:
        _require(type(value) is dict and set(value) == {'schemaVersion', 'purpose', 'projectDigest',
            'recordingDigest', 'buildId', 'sourceDigest', 'artifactDigest', 'policyDigest',
            'sourceObjects', 'eventFields', 'runs'}, 'diagnostics_invalid')
        _require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1
            and value['purpose'] == 'diagnostic-only', 'diagnostics_invalid')
        for key in ('projectDigest', 'recordingDigest', 'sourceDigest', 'artifactDigest', 'policyDigest'):
            contracts.validate_digest(value[key])
        _require(value['projectDigest'] == contracts.digest(project) and value['sourceDigest'] == source_digest
            and value['recordingDigest'] == specification['originalRecordingDigest']
            and any(item['id'] == value['buildId'] and item.get('sourceDigest') == source_digest
                and item['artifactDigest'] == value['artifactDigest'] for item in project['builds']),
            'diagnostics_binding')
        available = diagnostic_fields(value['eventFields'])
        selected = available if fields is None else diagnostic_fields(fields, code='ai_transfer_denied')
        _require(set(selected) <= set(available), 'ai_transfer_denied')
        sources = value['sourceObjects']
        _require(type(sources) is list and 1 <= len(sources) <= MAX_SOURCE_OBJECTS, 'diagnostics_invalid')
        for digest in sources: contracts.validate_digest(digest)
        _require(len(set(sources)) == len(sources), 'diagnostics_invalid')
        _require(type(value['runs']) is list and 1 <= len(value['runs']) <= MAX_RUNS, 'diagnostics_invalid')
        used, run_ids, count = set(), set(), 0
        for run in value['runs']:
            _require(type(run) is dict and set(run) == {'runDigest', 'sourceObjects', 'clock', 'truncated',
                'lostEvents', 'totalEvents', 'omittedEvents', 'events'}, 'diagnostics_invalid')
            contracts.validate_digest(run['runDigest'])
            _require(run['runDigest'] not in run_ids and run['clock'] == 'app-elapsed-unmapped'
                and type(run['truncated']) is bool and type(run['lostEvents']) is bool, 'diagnostics_invalid')
            run_ids.add(run['runDigest'])
            _require(type(run['sourceObjects']) is list and run['sourceObjects']
                and all(type(item) is str and item in sources for item in run['sourceObjects'])
                and len(run['sourceObjects']) == len(set(run['sourceObjects']))
                and not used.intersection(run['sourceObjects']), 'diagnostics_invalid')
            used.update(run['sourceObjects'])
            _require(type(run['events']) is list and type(run['totalEvents']) is int
                and 0 <= run['totalEvents'] <= MAX_APP_LOG_EVENTS and type(run['omittedEvents']) is int
                and run['omittedEvents'] == run['totalEvents'] - len(run['events']) >= 0, 'diagnostics_invalid')
            previous = 0
            for sequence, event in enumerate(run['events'], 1):
                _require(type(event) is dict and set(event) == set(available)
                    and type(event['seq']) is int and event['seq'] == sequence
                    and type(event['type']) is str and type(event['name']) is str, 'diagnostics_invalid')
                names = {'lifecycle': LIFECYCLE_NAMES, 'screen': {'appeared', 'disappeared'},
                         'click': {'began', 'returned', 'threw'}}
                _require(event['type'] in names and event['name'] in names[event['type']], 'diagnostics_invalid')
                if 'elapsedMs' in event:
                    _require(type(event['elapsedMs']) is int and previous <= event['elapsedMs'] <= MAX_APP_LOG_DURATION_MS,
                             'diagnostics_invalid')
                    previous = event['elapsedMs']
                if 'component' in event:
                    _require(type(event['component']) is str and event['component'] in {
                        'application', 'activity', 'scene', 'view_controller', 'view'}, 'diagnostics_invalid')
                if 'target' in event:
                    _require(event['target'] is None or type(event['target']) is str and _TARGET.fullmatch(event['target']),
                             'diagnostics_invalid')
            count += len(run['events'])
        _require(used == set(sources) and count <= MAX_APP_LOG_EVENTS
            and len(canonical(value)) <= MAX_DIAGNOSTIC_BYTES, 'diagnostics_limit')
        result = copy.deepcopy(value)
        result['eventFields'] = list(selected)
        for run in result['runs']:
            run['events'] = [{key: event[key] for key in selected} for event in run['events']]
        return result
    except (contracts.ContractError, ProtocolError, KeyError, ValueError, TypeError, RecursionError):
        raise RepairError('diagnostics_invalid') from None
