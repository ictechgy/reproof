#!/usr/bin/env python3
"""Require real, non-skipped SDK fixture JVM results; never run a device task."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

SUITES = {'io.reproof.sdk.FixtureExecutionTest': 5,
          'io.reproof.sdk.ReproofFixtureContractTest': 5}


def verify(root):
    root = Path(root)
    total = 0
    for name, expected in SUITES.items():
        path = root / ('TEST-' + name + '.xml')
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError('Required SDK fixture JUnit report is unavailable')
        raw = path.read_bytes()
        if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
            raise ValueError('Unsupported SDK fixture JUnit XML')
        suite = ET.fromstring(raw)
        cases = suite.findall('testcase')
        if (suite.tag != 'testsuite' or suite.get('name') != name
                or suite.get('tests') != str(expected) or len(cases) != expected
                or any(suite.get(field, '0') != '0' for field in ('failures', 'errors', 'skipped'))
                or len({case.get('name') for case in cases}) != expected
                or any(not case.get('name') or case.get('classname') != name
                       or any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped'))
                       for case in cases)):
            raise ValueError('SDK fixture JVM tests did not all execute and pass')
        total += expected
    return total


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:raise ValueError('Expected the SDK JUnit result directory')
        print(f'Android fixture JVM tests: {verify(sys.argv[1])} passed; Binder/device runtime not qualified')
    except (ValueError, OSError, ET.ParseError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
