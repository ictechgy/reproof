"""Bounded automatic app observations, independent of executable replay events."""
from __future__ import annotations
import copy
import json
import re
import uuid
from .core import require

MAX_APP_LOG_BYTES = 1024 * 1024
APP_LOG_MIME = 'application/vnd.reproloop.app-log+json'
MAX_APP_LOG_EVENTS = 2000
MAX_APP_LOG_DURATION_MS = 1_800_000
IDENTITY_KEYS = {'schemaVersion', 'platform', 'applicationId', 'runId', 'sessionId', 'profileDigest', 'startedAtMs'}
EVENT_KEYS = {'seq', 'elapsedMs', 'type', 'name', 'component', 'componentId', 'target'}
COMPONENT_ID = re.compile(r'c[0-9a-f]{16}\Z')
LIFECYCLE_NAMES = {'attached', 'created', 'started', 'resumed', 'paused', 'stopped', 'destroyed', 'save_state',
                   'foreground', 'background', 'active', 'inactive', 'connected', 'disconnected', 'termination_requested'}


def _uuid(value):
    if not isinstance(value, str):return False
    try:return str(uuid.UUID(value)) == value
    except ValueError:return False


def validate_app_log_marker(value, *, platform, application_id, profile_digest, run_id):
    require(isinstance(value, dict) and set(value) == IDENTITY_KEYS
            and type(value.get('schemaVersion')) is int and value['schemaVersion'] == 1
            and platform in {'android', 'ios'} and value['platform'] == platform
            and isinstance(application_id, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{1,199}', application_id)
            and value['applicationId'] == application_id
            and isinstance(profile_digest, str) and re.fullmatch(r'[0-9a-f]{64}', profile_digest)
            and value['profileDigest'] == profile_digest
            and _uuid(run_id) and value['runId'] == run_id and _uuid(value['sessionId'])
            and type(value['startedAtMs']) is int and value['startedAtMs'] >= 0,
            'Automatic app log identity differs from the selected run')
    return copy.deepcopy(value)


def validate_app_log(value, marker, *, click_targets, screen_targets):
    validate_app_log_marker(marker, platform=marker.get('platform'), application_id=marker.get('applicationId'),
                            profile_digest=marker.get('profileDigest'), run_id=marker.get('runId'))
    require(isinstance(value, dict) and set(value) == IDENTITY_KEYS | {'endSequence', 'truncated', 'lostEvents', 'events'}
            and {key:value.get(key) for key in IDENTITY_KEYS} == marker
            and type(value.get('schemaVersion')) is int and type(value.get('startedAtMs')) is int
            and type(value.get('truncated')) is bool and type(value.get('lostEvents')) is bool
            and isinstance(value.get('events'), list) and len(value['events']) <= MAX_APP_LOG_EVENTS
            and type(value.get('endSequence')) is int and value['endSequence'] == len(value['events']),
            'Invalid automatic app log snapshot')
    previous_time = 0
    for sequence, event in enumerate(value['events'], 1):
        require(isinstance(event, dict) and set(event) == EVENT_KEYS
                and type(event.get('seq')) is int and event['seq'] == sequence
                and type(event.get('elapsedMs')) is int and previous_time <= event['elapsedMs'] <= MAX_APP_LOG_DURATION_MS
                and isinstance(event.get('component'), str)
                and event['component'] in {'application', 'activity', 'scene', 'view_controller', 'view'}
                and isinstance(event.get('componentId'), str)
                and (event['componentId'] == 'app' or COMPONENT_ID.fullmatch(event['componentId']))
                and isinstance(event.get('type'), str) and isinstance(event.get('name'), str),
                'Invalid automatic app log event')
        previous_time = event['elapsedMs']
        kind, name, target = event['type'], event['name'], event['target']
        if kind == 'lifecycle':
            require(name in LIFECYCLE_NAMES and target is None, 'Invalid lifecycle observation')
        elif kind == 'click':
            require(name in {'began', 'returned', 'threw'} and isinstance(target, str) and target in click_targets,
                    'Click observation is outside the selected target policy')
        elif kind == 'screen':
            require(name in {'appeared', 'disappeared'} and isinstance(target, str)
                    and (target in screen_targets or COMPONENT_ID.fullmatch(target)),
                    'Screen observation is outside the selected target policy')
        else:require(False, 'Unsupported app observation type')
    require(len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()) <= MAX_APP_LOG_BYTES,
            'Automatic app log exceeds its size limit')
    return copy.deepcopy(value)


def collect_app_logs(read, *, platform, application_id, profile_digest, run_id, click_targets, screen_targets,
                     expected_marker=None):
    marker = validate_app_log_marker(read('app-log-session.json'), platform=platform, application_id=application_id,
                                    profile_digest=profile_digest, run_id=run_id)
    if expected_marker is not None:require(marker == expected_marker, 'Automatic app log session changed')
    value = read('app-logs/' + marker['sessionId'] + '/app-log.json')
    result = validate_app_log(value, marker, click_targets=click_targets, screen_targets=screen_targets)
    require(read('app-log-session.json') == marker, 'Automatic app log marker changed during collection')
    return result
