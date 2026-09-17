#!/usr/bin/env python3
"""Prepare a renamed synthetic app to verify explicit Android app profiles."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reproloop.android_profile import sample_app_profile, validate_app_profile
from reproloop.core import require
from reproloop.repair import copy_source
from reproloop.storage import write_json


def prepare(output):
    output = Path(output).resolve()
    require(not output.exists(), 'Use a new fixture output directory')
    output.mkdir(parents=True, mode=0o700)
    source = output / 'source'
    copy_source(ROOT / 'android', source)
    for path in (source / 'sample').rglob('*'):
        if not path.is_file():
            continue
        text = path.read_text()
        text = text.replace('io.reproloop.sample', 'io.reproloop.inventory')
        for old, new in [('name', 'label'), ('count', 'quantity'), ('add', 'commit'), ('report', 'export_capture')]:
            text = text.replace('"' + old + '"', '"' + new + '"')
            text = text.replace('R.id.' + old, 'R.id.' + new)
        text = text.replace('CounterLogic.increment()', 'CounterLogic.unitsPerItem()')
        text = text.replace('fun increment()', 'fun unitsPerItem()')
        if path.name == 'MainActivity.kt':
            anchor = '                context = this,\n'
            require(text.count(anchor) == 1, 'Fixture SDK constructor changed; review the preparation script')
            text = text.replace(anchor, anchor +
                '                safeTextTargets = setOf("label"),\n' +
                '                fixtureVersion = intent.getIntExtra("fixture_version", 1),\n' +
                '                reportTarget = "export_capture",\n' +
                '                tapTargets = setOf("commit", "next", "back", "bottom"),\n')
        if path.name == 'strings.xml':
            text = text.replace('Repro Loop Sample', 'Inventory Profile Fixture')
        path.write_text(text)
    document = sample_app_profile().data
    document.update(id='inventory', package='io.reproloop.inventory')
    document['fixture']['id'] = 'inventory_empty'
    document['startState']['nodes'] = {'quantity': '0', 'label': ''}
    document['targets'].update(tap=['commit', 'next', 'back', 'bottom'], text=['label'], numeric=['quantity'],
                               report='export_capture')
    for condition in document['oracle'].values():
        condition['target'] = 'quantity'
    document['edit']['function'] = 'unitsPerItem'
    profile = validate_app_profile(document)
    write_json(output / 'app-profile.json', profile.data)
    write_json(output / 'fixture.json', {'synthetic': True, 'base': 'repository Android sample',
        'appProfileDigest': profile.digest, 'changes': ['package', 'fixture ID', 'text/numeric/tap/report IDs', 'product function']})
    return {'source': str(source), 'appProfile': str(output / 'app-profile.json'), 'synthetic': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.output))
