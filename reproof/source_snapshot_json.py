"""Allocation preflight for bounded local snapshot metadata, not general JSON."""
from __future__ import annotations

import json


def bounded_json(raw, *, max_items, max_depth):
    """Reject oversized token/depth/cardinality shapes before JSON builds objects.

    Callers also cap input byte length and validate their exact semantic schema.
    Escaped strings are capped in encoded bytes; selected paths remain subject
    to the stricter 512-byte inert path contract after decoding.
    """
    if (type(raw) is not bytes or type(max_items) is not int or not 1 <= max_items <= 32768
            or type(max_depth) is not int or not 1 <= max_depth <= 3):
        raise ValueError('Invalid metadata envelope')
    depth = punctuation = scalar = string_bytes = containers = tokens = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            string_bytes += 1
            if string_bytes > 4096:
                raise ValueError('Metadata string limit exceeded')
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            continue
        if byte == 34:
            quoted = True;string_bytes = 0;scalar = 0;tokens += 1
        elif byte in (123, 91):
            depth += 1;punctuation += 1;scalar = 0;containers += 1
            if depth > max_depth:
                raise ValueError('Metadata nesting limit exceeded')
        elif byte in (125, 93):
            depth -= 1;punctuation += 1;scalar = 0
            if depth < 0:
                raise ValueError('Invalid metadata nesting')
        elif byte in (44, 58):
            punctuation += 1;scalar = 0
        elif byte in (9, 10, 13, 32):
            scalar = 0
        else:
            if scalar == 0: tokens += 1
            scalar += 1
            if scalar > 64 or byte > 127:
                raise ValueError('Metadata scalar limit exceeded')
        if (punctuation > 10 * max_items + 32 or containers > max_items + 2
                or tokens > 6 * max_items + 16):
            raise ValueError('Metadata member limit exceeded')
    if quoted or depth:
        raise ValueError('Incomplete metadata')

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate metadata field')
            value[key] = item
        return value

    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid metadata scalar')))
