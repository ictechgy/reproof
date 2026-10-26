"""Bounded inert source paths; never authorizes execution or expands byte budgets."""
from pathlib import PurePosixPath
import unicodedata

from .core import require
from .execution.wire import safe_transfer_path

EXTRA_EXTENSIONS = frozenset({'.metal', '.glsl', '.tiff', '.nib', '.a',
    '.js', '.css', '.woff', '.woff2', '.aar', '.jar', '.so', '.html', '.map'})
_DEVICES = {'con', 'prn', 'aux', 'nul', 'clock$',
    *(f'{prefix}{number}' for prefix in ('com', 'lpt') for number in '123456789¹²³')}


def input_extensions(value=None):
    if value is None:
        return frozenset()
    require(type(value) is list and 1 <= len(value) <= len(EXTRA_EXTENSIONS)
            and all(type(item) is str and item in EXTRA_EXTENSIONS for item in value)
            and len(set(value)) == len(value), 'Unsupported input extensions')
    return frozenset(value)


def inert_input_path(value):
    safe_transfer_path(value)
    require(len(value.encode('utf-8')) <= 512 and unicodedata.normalize('NFC', value) == value,
            'Unsupported source input path')
    # Reject format controls (including bidi), aliases and shell syntax. Paths
    # remain opaque data, but conservative syntax also protects generated inputs.
    require(all(char in '/._ -' or unicodedata.category(char)[0] in 'LMN'
                for char in value), 'Unsupported source input path')
    for part in value.split('/'):
        require(not part.startswith('.') and part == part.strip() and not part.endswith('.')
                and len(part.encode('utf-8')) <= 255
                and part.split('.')[0].casefold() not in _DEVICES,
                'Unsupported source input path')
    return value


def input_paths(names):
    require(type(names) is list and 1 <= len(names) <= 900, 'Declare bounded source inputs')
    for name in names:
        inert_input_path(name)
    folded = {name.casefold() for name in names}
    require(len(folded) == len(names) and not any(
        '/'.join(name.split('/')[:i]).casefold() in folded
        for name in names for i in range(1, len(name.split('/')))), 'Conflicting source input paths')
    return names


def allowed_suffix(value, defaults, extensions=None):
    inert_input_path(value)
    require(PurePosixPath(value).suffix.lower() in defaults | input_extensions(extensions),
            'Unsupported source input suffix')
    return value
