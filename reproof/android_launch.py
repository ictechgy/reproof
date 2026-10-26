"""Explicit local Android launch selection. Never a fixture/readiness proof."""
from __future__ import annotations
from dataclasses import dataclass, field
import base64
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from .core import ContractError, require

MAX_BYTES = 8192
_SECRET = re.compile(r'PASSWORD|PASSWD|TOKEN|SECRET|AUTH|CREDENTIAL|COOKIE|SESSION|PRIVATE|API_?KEY|ACCESS_?KEY')
_KEY = re.compile(r'QA_[A-Z][A-Z0-9_]{0,47}\Z')
_VALUE = re.compile(r'[A-Za-z0-9 ._:/?&=%+@-]{0,512}\Z')

@dataclass(frozen=True, slots=True)
class ApprovedAndroidLaunch:
    _canonical: bytes = field(repr=False)

    @property
    def digest(self):
        return hashlib.sha256(self._canonical).hexdigest()

    @property
    def data(self):
        return json.loads(self._canonical)

    def native(self):
        return {'configurationBase64': base64.b64encode(self._canonical).decode('ascii'),
                'configurationDigest': self.digest}


def validate_android_launch(value, profile):
    from .android_profile import AndroidRuntimeProfile
    require(type(profile) is AndroidRuntimeProfile, 'Android launch requires a runtime profile')
    require(type(value) is dict and set(value) == {'schemaVersion', 'kind', 'referenceId', 'profileDigest', 'extras'},
            'Invalid approved Android launch configuration')
    require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1
            and value['kind'] == 'reproof-approved-android-launch'
            and value['referenceId'] == profile.data['approvedReferences']['launch']
            and value['profileDigest'] == profile.digest,
            'Approved Android launch binding differs')
    extras = value['extras']
    require(type(extras) is list and 1 <= len(extras) <= 16, 'Invalid approved Android launch extras')
    seen = set()
    for item in extras:
        require(type(item) is dict and set(item) == {'key', 'type', 'value'}, 'Invalid approved Android launch extra')
        key, kind, extra = item['key'], item['type'], item['value']
        require(type(key) is str and _KEY.fullmatch(key) and not _SECRET.search(key)
                and key not in seen, 'Invalid approved Android launch key')
        seen.add(key)
        require(type(kind) is str and kind in {'string', 'boolean', 'integer'}, 'Invalid approved Android launch type')
        if kind == 'string':
            require(type(extra) is str and _VALUE.fullmatch(extra), 'Invalid approved Android launch string')
            if '://' in extra:
                try:
                    url = urlsplit(extra)
                    valid = (url.scheme == 'https' and bool(url.hostname) and url.username is None
                             and url.password is None and not url.query and not url.fragment
                             and url.port in (None, 443) and '%' not in extra and ' ' not in extra
                             and '?' not in extra)
                except ValueError:
                    valid = False
                require(valid, 'Invalid approved Android launch URL')
        elif kind == 'boolean':
            require(type(extra) is bool, 'Invalid approved Android launch boolean')
        else:
            require(type(extra) is int and -(2**31) <= extra <= 2**31-1, 'Invalid approved Android launch integer')
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')
    require(len(encoded) <= MAX_BYTES, 'Approved Android launch configuration exceeds limit')
    return ApprovedAndroidLaunch(encoded)


def load_android_launch(path, profile):
    from .execution.artifacts import read_regular, ArtifactError
    from .execution.wire import ProtocolError
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate approved Android launch field')
            result[key] = value
        return result
    try:
        path = Path(path).absolute()
        raw = read_regular(path.parent, path.name, maximum=MAX_BYTES)
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return validate_android_launch(value, profile)
    except (ArtifactError, ProtocolError, OSError, ValueError, UnicodeError, TypeError, RecursionError):
        raise ContractError('Approved Android launch configuration is unavailable') from None


def require_android_launch(selection, profile):
    require(type(selection) is ApprovedAndroidLaunch, 'Trusted Android launch selection required')
    validated = validate_android_launch(selection.data, profile)
    require(validated.digest == selection.digest, 'Approved Android launch configuration changed')
    return validated
