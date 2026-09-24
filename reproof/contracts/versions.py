"""Shared strict scalar, collection, path, digest, and version rules."""
from __future__ import annotations
import hashlib, json, math, re
from pathlib import PurePosixPath
from reproof.core import ContractError
CONTRACT_VERSION = 1
ID_RE = re.compile('[a-z][a-z0-9_-]{0,63}\\Z')
HEX64_RE = re.compile('[0-9a-f]{64}\\Z')
MAX_EPOCH_MS = 32503680000000

def fail(message):
    raise ContractError(message)

def require(condition, message):
    if not condition:
        fail(message)

def validate_id(value, field='id'):
    require(type(value) is str and ID_RE.fullmatch(value) is not None, f'Invalid {field}')
    return value

def validate_version(value, field='schemaVersion'):
    require(type(value) is int and value == CONTRACT_VERSION, f'Unsupported {field}')
    return value

def bounded_int(value, field, low=0, high=2 ** 31 - 1):
    require(type(value) is int and low <= value <= high, f'Invalid {field}')
    return value

def bounded_number(value, field, low=0.0, high=2 ** 31 - 1):
    require(type(value) in (int, float), f'Invalid {field}')
    require(low <= value <= high, f'Invalid {field}')
    require(type(value) is int or math.isfinite(value), f'Invalid {field}')
    return value

def bounded_text(value, field, limit=240, *, empty=False):
    require(type(value) is str and (empty or len(value) > 0) and (len(value) <= limit), f'Invalid {field}')
    require(not any((ord(c) < 32 or ord(c) == 127 for c in value)), f'Invalid {field}')
    try:
        encoded = value.encode('utf-8', errors='strict')
    except UnicodeError:
        fail(f'Invalid {field}')
    require(len(encoded) <= limit, f'Invalid {field}')
    return value

def epoch_ms(value, field='time'):
    return bounded_int(value, field, 0, MAX_EPOCH_MS)

def safe_relative_path(value, field='path'):
    bounded_text(value, field, 1024)
    require('\\' not in value and (not value.startswith('/')), f'Invalid {field}')
    parts = value.split('/')
    require(all((p not in ('', '.', '..') for p in parts)), f'Invalid {field}')
    require(str(PurePosixPath(value)) == value, f'Invalid {field}')
    lowered = [p.lower() for p in parts]
    secrets = {'auth.json', 'credentials', 'credentials.json', 'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa', '.netrc', '.npmrc', '.pypirc'}
    secret_suffixes = ('.keystore', '.jks', '.p12', '.pfx', '.pem', '.key')
    require(not any((p in secrets or p.startswith('.env') or p.endswith(secret_suffixes) for p in lowered)), f'Invalid {field}')
    return value

def digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        fail('Contract contains non-JSON data')
    return hashlib.sha256(raw).hexdigest()

def validate_digest(value, field='digest'):
    require(type(value) is str and HEX64_RE.fullmatch(value) is not None, f'Invalid {field}')
    return value

def exact(document, required, optional=()):
    require(type(document) is dict, 'Contract must be an object')
    keys = set(document)
    require(set(required) <= keys <= set(required) | set(optional), 'Unknown or missing contract field')

def unique_ids(items, field):
    ids = [x['id'] for x in items]
    require(len(ids) == len(set(ids)), f'Duplicate {field}')

def bounded_list(value, field, maximum, *, minimum=0):
    require(type(value) is list and minimum <= len(value) <= maximum, f'Invalid {field}')
    return value

def json_scalar(value, field, *, nullable=False):
    if value is None:
        require(nullable, f'Invalid {field}')
    elif type(value) is bool:
        pass
    elif type(value) is int:
        bounded_int(value, field, -(2 ** 53 - 1), 2 ** 53 - 1)
    elif type(value) is float:
        bounded_number(value, field, -(2 ** 53 - 1), 2 ** 53 - 1)
    elif type(value) is str:
        bounded_text(value, field, 4096, empty=True)
    else:
        fail(f'Invalid {field}')
    return value
