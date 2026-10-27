"""Operator-selected iOS QA launch settings, not preparation or readiness proof."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re

from .core import ContractError, require

MAX_BYTES = 8192
_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
_SERVER = re.compile(r'https://(' + _LABEL + r'(?:\.' + _LABEL + r')+)(/[^?#]*)?\Z')
_SEGMENT = re.compile(r'[A-Za-z0-9._~-]+\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')


@dataclass(frozen=True, slots=True)
class ApprovedIosLaunch:
    _canonical: bytes = field(repr=False)

    @property
    def digest(self):
        return hashlib.sha256(self._canonical).hexdigest()

    @property
    def data(self):
        return json.loads(self._canonical)

    def native(self):
        value = self.data
        return {
            'REPRO_LIVE_APPROVED_LAUNCH': self._canonical.decode('ascii'),
            'REPRO_LIVE_APPROVED_LAUNCH_DIGEST': self.digest,
            'REPRO_LIVE_APPROVED_LAUNCH_REFERENCE_ID': value['referenceId'],
            'REPRO_LIVE_APPROVED_LAUNCH_ARTIFACT_DIGEST': value['artifactDigest'],
        }


def _server(value):
    if type(value) is not str or len(value) > 512:
        return False
    match = _SERVER.fullmatch(value)
    if match is None:
        return False
    hostname, path = match.groups()
    if len(hostname) > 253 or all(label.isdigit() for label in hostname.split('.')):
        return False
    if path in (None, '/'):
        return True
    segments = path[1:].split('/')
    if segments[-1] == '':
        segments.pop()
    return all(_SEGMENT.fullmatch(segment) and segment not in {'.', '..'} for segment in segments)


def validate_ios_launch(value, profile):
    from .ios_profile import IosAppProfile
    require(type(profile) is IosAppProfile, 'iOS launch requires a runtime profile')
    require(type(value) is dict and set(value) == {
        'schemaVersion', 'kind', 'referenceId', 'profileDigest', 'artifactDigest', 'environment'
    }, 'Invalid approved iOS launch configuration')
    require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1
            and value['kind'] == 'reproof-approved-ios-launch'
            and value['referenceId'] == profile.data['approvedReferences']['launch']
            and value['profileDigest'] == profile.digest
            and value['artifactDigest'] == profile.data['artifact']['sha256'],
            'Approved iOS launch binding differs')
    environment = value['environment']
    require(type(environment) is list and 1 <= len(environment) <= 4,
            'Invalid approved iOS launch environment')
    seen = set()
    for item in environment:
        require(type(item) is dict and set(item) == {'key', 'type', 'value'},
                'Invalid approved iOS launch setting')
        key, kind, setting = item['key'], item['type'], item['value']
        require(type(key) is str and key in {'QA_SERVER', 'QA_MODE', 'QA_FIXTURE_ID', 'QA_FIXTURE_VERSION'}
                and key not in seen, 'Invalid approved iOS launch key')
        seen.add(key)
        if key == 'QA_FIXTURE_VERSION':
            valid = kind == 'integer' and type(setting) is int and 1 <= setting <= 2147483647
        else:
            valid = kind == 'string' and type(setting) is str
            if valid:
                valid = (_server(setting) if key == 'QA_SERVER' else
                         setting in {'qa', 'mock'} if key == 'QA_MODE' else bool(_ID.fullmatch(setting)))
        require(valid, 'Invalid approved iOS launch value')
    require(('QA_FIXTURE_ID' in seen) == ('QA_FIXTURE_VERSION' in seen),
            'Approved iOS fixture launch parameters must be paired')
    # Sort the bounded list as well as object keys: one canonical cross-language representation.
    canonical = dict(value, environment=sorted(environment, key=lambda item: item['key']))
    encoded = json.dumps(canonical, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True, allow_nan=False).encode('ascii')
    require(len(encoded) <= MAX_BYTES, 'Approved iOS launch configuration exceeds limit')
    return ApprovedIosLaunch(encoded)


def load_ios_launch(path, profile):
    from .execution.artifacts import read_regular, ArtifactError
    from .execution.wire import ProtocolError

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate approved iOS launch field')
            result[key] = value
        return result

    try:
        path = Path(path).absolute()
        raw = read_regular(path.parent, path.name, maximum=MAX_BYTES)
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return validate_ios_launch(value, profile)
    except (ArtifactError, ProtocolError, OSError, ValueError, UnicodeError, TypeError, RecursionError):
        raise ContractError('Approved iOS launch configuration is unavailable') from None


def require_ios_launch(selection, profile):
    require(type(selection) is ApprovedIosLaunch, 'Trusted iOS launch selection required')
    validated = validate_ios_launch(selection.data, profile)
    require(validated.digest == selection.digest, 'Approved iOS launch configuration changed')
    return validated
