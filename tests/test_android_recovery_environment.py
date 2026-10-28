"""Inert process-environment checks; no ADB server or device is executed."""
import os
import json
import sys
import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof.repair_android_signing import AndroidSigningError, _process_environment


class PrivateEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / 'home'
        self.android = self.root / 'android'
        self.home.mkdir(mode=0o700)
        self.android.mkdir(mode=0o700)

    def test_default_environment_unchanged_and_ambient_values_excluded(self):
        with patch.dict(os.environ, {'HOME': '/foreign', 'ANDROID_USER_HOME': '/foreign',
                'ADB_SERVER_SOCKET': 'tcp:5037', 'ANDROID_SERIAL': 'foreign'}):
            self.assertEqual(_process_environment(self.root), {
                'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'TMPDIR': str(self.root)})
            private = _process_environment(self.root, private_homes=(self.home, self.android))
        self.assertEqual(set(private), {'PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'HOME', 'ANDROID_USER_HOME'})
        self.assertEqual(private['HOME'], str(self.home))
        self.assertEqual(private['ANDROID_USER_HOME'], str(self.android))

    def test_invalid_roots_and_symlink_components_fail_closed(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.home, target_is_directory=True)
        invalid = [(alias, self.android), (self.home, self.home), [self.home, self.android],
                   ('/foreign', self.android), (Path('relative'), self.android)]
        for selected in invalid:
            with self.subTest(selected=selected), self.assertRaises(AndroidSigningError):
                _process_environment(self.root, private_homes=selected)
        self.home.chmod(0o755)
        with self.assertRaises(AndroidSigningError):
            _process_environment(self.root, private_homes=(self.home, self.android))

    def test_foreign_owner_rejected(self):
        with patch('reproof.repair_android_signing.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(AndroidSigningError):
                _process_environment(self.root, private_homes=(self.home, self.android))

    def test_bounded_child_receives_only_explicit_private_homes(self):
        from reproof.repair_android_signing import _ProcessOwner
        owner = _ProcessOwner()
        self.addCleanup(owner.close)
        result = owner.run((sys.executable, '-I', '-c',
            'import os,json;print(json.dumps(dict(os.environ)))'),
            work=self.root, input_bytes=b'', pass_fds=(), cancellation=threading.Event(),
            deadline_monotonic=time.monotonic()+5, max_output_bytes=4096,
            private_homes=(self.home, self.android))
        self.assertEqual(result.returncode, 0)
        actual = json.loads(result.stdout)
        self.assertEqual(actual['HOME'], str(self.home))
        self.assertEqual(actual['ANDROID_USER_HOME'], str(self.android))
        self.assertNotIn('ADB_SERVER_SOCKET', actual)
        self.assertNotIn('ANDROID_SERIAL', actual)

    def test_invalid_homes_rejected_before_child_launch(self):
        from reproof.repair_android_signing import _ProcessOwner
        owner = _ProcessOwner()
        self.addCleanup(owner.close)
        self.home.chmod(0o755)
        with patch('reproof.repair_android_signing.subprocess.Popen') as launch:
            with self.assertRaises(AndroidSigningError):
                owner.run((sys.executable, '-V'), work=self.root, input_bytes=b'', pass_fds=(),
                    cancellation=threading.Event(), deadline_monotonic=time.monotonic()+5,
                    private_homes=(self.home, self.android))
            launch.assert_not_called()
