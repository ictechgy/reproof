#!/usr/bin/env python3
"""Check exact synthetic Swift-test exports, never promote annotations to evidence."""
import argparse
import json
from itertools import islice
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reproof.sdk_review import MAX_BYTES, ReviewInvalid, normalize_export


def check(directory):
    directory = Path(directory)
    names = {'capture.json', 'metadata.json', 'finalized.json'}
    if directory.is_symlink() or not directory.is_dir() or {p.name for p in islice(directory.iterdir(), 4)} != names:
        raise ReviewInvalid('sdk_export_invalid')
    values = {}
    for name in names:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ReviewInvalid('sdk_export_invalid')
        with path.open('rb') as source:
            values[name] = source.read(MAX_BYTES + 1)
    summary = normalize_export(platform='ios', capture=values['capture.json'],
        metadata=values['metadata.json'], marker=values['finalized.json']).summary()
    if summary['actions'] != [{'action': 'replace', 'target': 'counter.name'},
                              {'action': 'tap', 'target': 'counter.add'}]:
        raise ReviewInvalid('sdk_export_invalid')
    # No raw bytes, session IDs, timestamps, parameters or digests reach logs.
    return {'status': 'synthetic-writer-export-compatible', 'reviewStatus': summary['status'],
            'repairReady': summary['repairReady'], 'executionAuthority': summary['executionAuthority']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = check(arguments.directory)
    except (OSError, ReviewInvalid):
        print(json.dumps({'status': 'sdk_export_invalid'}))
        raise SystemExit(1)
    print(json.dumps(result, sort_keys=True))
