#!/usr/bin/env python3
"""Inert tests: independent audit guard denies every process/socket operation."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

blocked=[]
def audit(event,args):
    if event.startswith('socket.') or event in {'subprocess.Popen','os.system','os.posix_spawn','os.posix_spawnp','os.fork','os.forkpty'}:
        blocked.append(event)
        raise AssertionError('Process and socket operations are denied in inert tests')

def main():
    sys.addaudithook(audit)
    for event in ('subprocess.Popen','socket.__new__'):
        try: sys.audit(event,None)
        except AssertionError: pass
        else: raise AssertionError('Audit guard did not activate')
    blocked.clear()
    with mock.patch.object(subprocess,'Popen',side_effect=AssertionError('Popen denied')) as popen:
        suite=unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent),pattern='test_*.py')
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        popen.assert_not_called()
    if blocked: raise AssertionError('A real process or socket operation was attempted')
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__': raise SystemExit(main())
