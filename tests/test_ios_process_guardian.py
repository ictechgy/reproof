import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from reproloop.resources import read_resource


class IOSProcessGuardianTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix='owned-ios-guardian-tools-')
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        for path in ('ios-process-guardian/main.c', 'ios-signing-owner/ownership.h'):
            target = root/path; target.parent.mkdir(exist_ok=True)
            target.write_bytes(read_resource('native/'+path))
        cls.binary = root/'guardian'
        sdk = '/Applications/Xcode-27.0.0-beta.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX27.0.sdk'
        result = subprocess.run(['/usr/bin/clang', '-std=c11', '-Wall', '-Wextra', '-Werror', '-isysroot', sdk,
            str(root/'ios-process-guardian/main.c'), '-framework', 'CoreFoundation', '-o', str(cls.binary)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode: raise RuntimeError(result.stderr.decode())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='owned-ios-guardian-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.descriptors = []; self.writer = None; self.process = None
        self.addCleanup(self.close)

    def close(self):
        if self.writer is not None: os.close(self.writer); self.writer = None
        for fd in self.descriptors: os.close(fd)
        self.descriptors.clear()
        if self.process is not None:
            if self.process.poll() is None:
                try: self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL); self.process.wait(timeout=3)
            self.process.stdout.close(); self.process.stderr.close()

    def start(self, command, maximum=4096, *, ignored_sigchld=False):
        profile = '(version 1) (allow default) (deny network*) (deny process-fork)'
        arguments = ['/usr/bin/sandbox-exec', '-p', profile, *map(str, command)]
        encoded = plistlib.dumps(arguments, fmt=plistlib.FMT_BINARY)
        config = {'schemaVersion': '1', 'operationId': 'owned-verify', 'requestDigest': 'a'*64,
            'contextDigest': 'b'*64, 'scopeDigest': 'c'*64, 'definitionDigest': 'd'*64,
            'workPath': str(self.root), 'childWorkPath': str(self.root),
            'commandDigest': hashlib.sha256(encoded).hexdigest(), 'maxOutputBytes': str(maximum)}
        for name, body in (('request.plist', plistlib.dumps(config, fmt=plistlib.FMT_BINARY)),
                           ('arguments.plist', encoded)):
            path = self.root/name; path.write_bytes(body); path.chmod(0o600)
        def opened(name, flags):
            fd = os.open(self.root/name, flags | os.O_NOFOLLOW); self.descriptors.append(fd); return fd
        for name in ('producer.lock', 'owner.lock', 'start.json', 'termination.json', 'native-result.plist'):
            (self.root/name).touch(mode=0o600)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW); self.descriptors.append(directory)
        producer = opened('producer.lock', os.O_RDWR); owner = opened('owner.lock', os.O_RDWR)
        fcntl.flock(producer, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reader, self.writer = os.pipe(); self.descriptors.append(reader)
        fds = [opened('request.plist', os.O_RDONLY), directory, producer, owner, reader,
            opened('arguments.plist', os.O_RDONLY), opened('native-result.plist', os.O_RDWR),
            opened('start.json', os.O_RDWR), opened('termination.json', os.O_RDWR)]
        launch = [str(self.binary), *map(str, fds)]
        if ignored_sigchld:
            launch = [sys.executable, '-c', 'import os,signal,sys; signal.signal(signal.SIGCHLD,signal.SIG_IGN); os.execv(sys.argv[1],sys.argv[1:])', *launch]
        self.process = subprocess.Popen(launch, pass_fds=tuple(fds),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, cwd=self.root)
        for fd in self.descriptors: os.close(fd)
        self.descriptors.clear()

    def lock_available(self):
        fd = os.open(self.root/'owner.lock', os.O_RDWR | os.O_NOFOLLOW)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); return True
            except BlockingIOError: return False
        finally: os.close(fd)

    def completed(self):
        self.assertTrue(select.select([self.process.stdout], [], [], 10)[0])
        control = self.process.stdout.readline(4096)
        if not control: self.fail(self.process.stderr.read(4096))
        result = plistlib.loads((self.root/'native-result.plist').read_bytes())
        self.assertFalse(self.lock_available())
        os.write(self.writer, b'\x01'); os.close(self.writer); self.writer = None
        self.process.wait(timeout=3)
        self.assertTrue(self.lock_available())
        return result

    def test_child_output_is_bounded_and_locks_last_through_acknowledged_exit(self):
        self.start(['/usr/bin/printf', 'owned verification output'])
        result = self.completed()
        self.assertEqual(result['returnCode'], 0)
        self.assertEqual(result['stdout'], b'owned verification output')
        self.assertTrue(result['bounded'])

    def test_excessive_child_output_fails_and_collects_the_child(self):
        self.start(['/usr/bin/yes', 'owned'], maximum=128)
        result = self.completed()
        self.assertFalse(result['bounded'])
        self.assertLessEqual(len(result['stdout']), 128)
        self.assertNotEqual(self.process.returncode, 0)

    def test_inherited_ignored_sigchld_does_not_lose_child_ownership(self):
        self.start(['/usr/bin/printf', 'owned'], ignored_sigchld=True)
        if not select.select([self.process.stdout], [], [], 1)[0]:
            # Stop this still-owned guardian directly. A broken wait policy
            # must never get a chance to signal its already-reaped child PID.
            self.process.kill(); self.process.wait(timeout=3)
            self.fail('guardian did not retain wait ownership of its child')
        result = self.completed()
        self.assertEqual(result['returnCode'], 0)
        self.assertEqual(result['stdout'], b'owned')

    def test_parent_eof_stops_a_live_child_before_releasing_its_locks(self):
        self.start(['/bin/sleep', '30'])
        deadline = time.monotonic()+5
        while time.monotonic() < deadline and not (self.root/'start.json').stat().st_size:
            self.assertIsNone(self.process.poll()); time.sleep(.01)
        # The group stays live until its actual child has been collected.
        self.assertFalse(self.lock_available())
        os.close(self.writer); self.writer = None
        self.assertEqual(self.process.wait(timeout=3), 75)
        self.assertTrue(self.lock_available())
        with self.assertRaises(ProcessLookupError): os.killpg(self.process.pid, 0)

    def test_openssl_retains_locks_if_the_guardian_is_abruptly_killed(self):
        configuration = self.root/'openssl.cnf'; configuration.write_bytes(b'')
        fifo = self.root/'owned-input'; os.mkfifo(fifo, 0o600)
        self.start(['/usr/bin/env', 'OPENSSL_CONF='+str(configuration), '/usr/bin/openssl',
                    'enc', '-a', '-in', str(fifo)])
        writer = None
        try:
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                try: writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK); break
                except OSError: time.sleep(.01)
            self.assertIsNotNone(writer)
            self.process.kill(); self.process.wait(timeout=3)
            self.assertFalse(self.lock_available())
            os.close(writer); writer = None
            deadline = time.monotonic()+5
            while time.monotonic() < deadline and not self.lock_available(): time.sleep(.01)
            self.assertTrue(self.lock_available())
        finally:
            if writer is not None: os.close(writer)
