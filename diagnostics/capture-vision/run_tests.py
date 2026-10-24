#!/usr/bin/env python3
"""Run all applicable archived tests and comparison tests, without child processes.

The archived workflow's three assertions are inapplicable: that workflow is
intentionally NOT copied or executed. Its source test file remains unchanged.
The new workflow's stricter conditions are tested in test_compare.py instead.
"""
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
blocked = []


def audit(event, args):
    if event.startswith('socket.') or event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.posix_spawnp', 'os.fork', 'os.forkpty'}:
        blocked.append(event)
        raise AssertionError('Independent audit hook forbids process execution')


def main():
    sys.addaudithook(audit)
    # Independent hook test needs no process creation.
    try:
        sys.audit('subprocess.Popen', 'never-executed', [], None, None)
    except AssertionError:
        pass
    else:
        raise AssertionError('Process audit guard did not activate')
    try:
        sys.audit('socket.__new__', None, 2, 1, 0)
    except AssertionError:
        pass
    else:
        raise AssertionError('Socket audit guard did not activate')
    blocked.clear()
    spec = importlib.util.spec_from_file_location('archived_offline_tests', HERE.parent/'offline-vision/test_replay.py')
    archived = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = archived
    with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('All Popen calls denied')) as popen:
        spec.loader.exec_module(archived)
        suite = unittest.TestSuite()
        loader = unittest.TestLoader()
        count = 0
        for name, cls in vars(archived).items():
            if isinstance(cls, type) and issubclass(cls, unittest.TestCase) and name != 'WorkflowTests':
                loaded = loader.loadTestsFromTestCase(cls)
                count += loaded.countTestCases()
                suite.addTests(loaded)
        suite.addTests(loader.discover(str(HERE), pattern='test_*.py'))
        print(f'Running {count} unchanged archived tests plus comparison tests; 3 obsolete workflow assertions replaced.')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        popen.assert_not_called()
    if blocked:
        raise AssertionError('A test attempted a forbidden real process')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
