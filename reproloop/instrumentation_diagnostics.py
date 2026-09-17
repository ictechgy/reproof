"""Bounded diagnostic observations; never executable replay instructions."""
import copy
import json
import re

from .core import require

MAX_DIAGNOSTICS = 1024 * 1024


def validate_diagnostics(value, capture, app_profile):
    require(app_profile is not None and app_profile.data.get('captureMode') == 'debug_receiver',
            'Diagnostics require an explicit source instrumentation profile')
    require(isinstance(value, dict) and set(value) == {'schemaVersion', 'sessionId', 'appProfileDigest', 'endSequence', 'actions'}
            and type(value['schemaVersion']) is int and value['schemaVersion'] == 1,
            'Invalid instrumentation diagnostics')
    require(value['sessionId'] == capture['sessionId'] and value['appProfileDigest'] == app_profile.digest
            and type(value['endSequence']) is int and value['endSequence'] == capture['endSequence'],
            'Diagnostics do not match the captured SDK session and profile')
    taps = [event for event in capture['events'] if event['action'] == 'tap']
    actions = value['actions']
    require(isinstance(actions, list) and 0 < len(actions) <= 500 and len(actions) == len(taps),
            'Diagnostics must cover every recorded tap exactly once')
    sites = {site['id']: site for site in app_profile.data['instrumentation']['sites']}
    numeric = set(app_profile.data['targets']['numeric'])
    for action, event in zip(actions, taps):
        require(isinstance(action, dict) and set(action) == {'eventId', 'target', 'siteId', 'before', 'after', 'outcome'}
                and action['eventId'] == event['id'] and action['target'] == event['target']
                and isinstance(action['outcome'], str) and action['outcome'] in {'returned', 'threw'}
                and isinstance(action['siteId'], str),
                'Diagnostic event does not match the recorded tap')
        site = sites.get(action['siteId'])
        require(site is not None and site['target'] == event['target'], 'Diagnostic source site is not configured')
        for phase in ('before', 'after'):
            nodes = action[phase]
            require(isinstance(nodes, dict) and set(nodes) == numeric
                    and all(isinstance(text, str) and re.fullmatch(r'[0-9]{1,9}', text) for text in nodes.values()),
                    'Diagnostic values are outside the numeric observation policy')
    require(len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= MAX_DIAGNOSTICS,
            'Instrumentation diagnostics exceed the size limit')
    return copy.deepcopy(value)
