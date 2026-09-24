"""Failure atomicity and descriptor ownership of the iOS completion boundary."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reproof.ios_device_guardian import _IOSProcessOwner


class IOSGuardianProcessOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.owner = _IOSProcessOwner()
        self.completion_read, self.completion_write = os.pipe()
        self.live_read, self.live_write = os.pipe()
        self.descriptors = [self.completion_read, self.completion_write, self.live_read, self.live_write]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.owner.close(deadline_monotonic=time.monotonic()+2)
        # Unknown completion remains quarantined in production. These tests
        # own and reap their synthetic children before disposing test handles.
        for item in tuple(self.owner._processes):
            self.assertIsNotNone(item.process.poll())
            item.process.wait()
            item.close_streams(); item.close_live(); item.close_completion()
            self.owner._processes.discard(item)
        for descriptor in self.descriptors:
            os.close(descriptor)

    def run_owner(self, *, acknowledge=True):
        code = ('import os; os.write(%d,b"D")' % self.completion_write) if acknowledge else 'pass'
        return self.owner.run((sys.executable, '-I', '-c', code), work=self.root,
            input_bytes=b'', pass_fds=(self.completion_write,), cancellation=threading.Event(),
            deadline_monotonic=time.monotonic()+3, completion_read=self.completion_read,
            live_write=self.live_write)

    def test_descriptor_allocation_failure_happens_before_popen(self):
        original = os.dup
        calls = 0
        def fail_second(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2: raise OSError('owned allocation failure')
            return original(descriptor)
        with patch('reproof.ios_device_guardian.os.dup', side_effect=fail_second), \
                patch('reproof.ios_device_guardian.subprocess.Popen') as popen:
            with self.assertRaises(Exception): self.run_owner()
        popen.assert_not_called()
        self.assertEqual(self.owner.active_processes, 0)
        for descriptor in self.descriptors: os.fstat(descriptor)

    def test_unacknowledged_exit_keeps_ownership_without_closing_reused_caller_fd(self):
        result = self.run_owner(acknowledge=False)
        self.assertFalse(result.terminated)
        self.assertEqual(self.owner.active_processes, 1)
        old = self.completion_read
        os.close(old); self.descriptors.remove(old)
        replacement = os.open(os.devnull, os.O_RDONLY)
        self.descriptors.append(replacement)
        if replacement != old:
            os.dup2(replacement, old); self.descriptors.append(old)
        self.assertFalse(self.owner.close(deadline_monotonic=time.monotonic()+.02))
        os.fstat(old)

    def test_close_waits_until_created_child_is_tracked(self):
        created, release, closed = threading.Event(), threading.Event(), threading.Event()
        original = subprocess.Popen
        outcomes, errors = [], []
        def popen(*args, **kwargs):
            process = original(*args, **kwargs)
            created.set(); release.wait(2)
            return process
        def run():
            try: outcomes.append(self.run_owner())
            except Exception as error: errors.append(error)
        def close():
            self.owner.close(deadline_monotonic=time.monotonic()+3)
            closed.set()
        with patch('reproof.ios_device_guardian.subprocess.Popen', side_effect=popen):
            worker = threading.Thread(target=run); worker.start()
            self.assertTrue(created.wait(1))
            closing = threading.Thread(target=close); closing.start()
            self.assertFalse(closed.wait(.05))
            release.set(); worker.join(4); closing.join(4)
        self.assertFalse(worker.is_alive() or closing.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].terminated)

    def test_collector_construction_failure_retains_or_proves_child_completion(self):
        with patch('reproof.ios_device_guardian._Collector', side_effect=RuntimeError('owned collector failure')):
            with self.assertRaises(RuntimeError): self.run_owner()
        for item in self.owner._processes:
            self.assertIsNotNone(item.process.poll())
            self.assertFalse(item.terminal)
        for descriptor in self.descriptors: os.fstat(descriptor)

    def test_cancellation_allows_native_initialization_to_acknowledge_revocation(self):
        cancelled = threading.Event()
        timer = threading.Timer(.02, cancelled.set); timer.start()
        try:
            code = ('import os,time; time.sleep(.1); os.read(%d,1); os.write(%d,b"D")'
                    % (self.live_read, self.completion_write))
            result = self.owner.run((sys.executable, '-I', '-c', code), work=self.root,
                input_bytes=b'', pass_fds=(self.live_read, self.completion_write), cancellation=cancelled,
                deadline_monotonic=time.monotonic()+3, completion_read=self.completion_read,
                live_write=self.live_write)
            self.assertTrue(result.interrupted)
            self.assertTrue(result.terminated)
            self.assertEqual(self.owner.active_processes, 0)
        finally: timer.join()


if __name__ == '__main__': unittest.main()
