"""Real codesign controls; no private signing identity or device is used."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reproloop.ios_artifact_transfer import parse_ios_artifact


SOURCE = Path(__file__).resolve().parents[1] / (
    'artifacts/product-delivery/d1-uikit-r1/original-release/DerivedData/'
    'Build/Products/Release-iphonesimulator/Inventory.app')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class IOSCodeSignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from reproloop.resources import read_resource
        temporary = tempfile.TemporaryDirectory(prefix='owned-ios-code-verifier-')
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        source = root / 'main.c'
        source.write_bytes(read_resource('native/ios-code-verifier/main.c'))
        cls.verifier = root / 'code-verifier'
        sdk = '/Applications/Xcode-27.0.0-beta.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX27.0.sdk'
        result = subprocess.run(['/usr/bin/clang', '-Wall', '-Wextra', '-Werror', '-isysroot', sdk,
            str(source), '-framework', 'Security', '-framework', 'CoreFoundation', '-o', str(cls.verifier)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
        if result.returncode:
            raise RuntimeError('owned_code_verifier_build_failed')

    def setUp(self):
        from reproloop.ios_code_signature import IOSCodeSignatureTools, IOSCodeSignatureInspector
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / 'Inventory.app'
        shutil.copytree(SOURCE, self.app, symlinks=True)
        self.info = plistlib.loads((self.app / 'Info.plist').read_bytes())
        self.entitlements = {'get-task-allow': True}
        entitlement_file = self.root / 'owned-entitlements.plist'
        entitlement_file.write_bytes(plistlib.dumps(self.entitlements))
        signed = subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none',
            '--entitlements', str(entitlement_file), str(self.app)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        self.assertEqual(signed.returncode, 0)
        self.selected = parse_ios_artifact(self.app)
        self.tools = IOSCodeSignatureTools(Path('/usr/bin/codesign'), sha('/usr/bin/codesign'),
                                          sha('/usr/bin/sandbox-exec'))
        self.policy = {'.': {'bundleId': self.info['CFBundleIdentifier'], 'entitlements': self.entitlements}}
        self.inspector = IOSCodeSignatureInspector(self.tools, self.root / 'inspect',
            signature_kind='adhoc-simulator', bundle_policies=self.policy)
        self.addCleanup(lambda: self.inspector.close(deadline_monotonic=time.monotonic() + 5))

    def inspect(self, artifact=None, inspector=None, **changes):
        args = dict(context_digest='a' * 64, cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 15)
        return (inspector or self.inspector).inspect(self.selected if artifact is None else artifact,
                                                   **{**args, **changes})

    def test_each_universal_architecture_has_expected_identity_and_entitlements(self):
        proof = self.inspect()
        report = self.inspector.require_verified(proof, self.selected, context_digest='a' * 64)
        self.assertEqual(report['appDigest'], self.selected.app_digest)
        self.assertEqual(len(report['codeObjects']), 1)
        images = report['codeObjects'][0]['architectures']
        self.assertEqual({row['cpuType'] for row in images}, {16777223, 16777228})
        self.assertTrue(all(row['signatureKind'] == 'adhoc-simulator' for row in images))
        self.assertEqual(list(self.inspector.work_root.iterdir()), [])
        self.assertNotIn(str(self.root), json.dumps(report))
        for forged in (report, replace(proof)):
            with self.assertRaises(Exception):
                self.inspector.require_verified(forged, self.selected, context_digest='a' * 64)
        with self.assertRaises(Exception):
            self.inspector.require_verified(proof, self.selected, context_digest='b' * 64)

    def test_modified_covered_slice_and_resource_are_rejected(self):
        executable = self.app / self.info['CFBundleExecutable']
        original = executable.read_bytes()
        mutated = bytearray(original)
        _, _, offset, _, _ = struct.unpack_from('>iiIII', mutated, 8)
        mutated[offset + 28] ^= 1
        executable.write_bytes(mutated)
        with self.assertRaises(Exception):
            self.inspect(parse_ios_artifact(self.app))
        executable.write_bytes(original)
        info = dict(self.info); info['CFBundleVersion'] = 'changed'
        (self.app / 'Info.plist').write_bytes(plistlib.dumps(info))
        with self.assertRaises(Exception):
            self.inspect(parse_ios_artifact(self.app))
        self.assertEqual(self.inspector.active_processes, 0)

    def test_certificate_mode_cannot_accept_an_adhoc_signature(self):
        from reproloop.ios_code_signature import IOSCodeSignatureInspector
        tools = replace(self.tools, verifier=self.verifier, verifier_sha256=sha(self.verifier))
        owner = IOSCodeSignatureInspector(tools, self.root / 'identity-inspect',
            signature_kind='identity', expected_certificate_sha256='0' * 64,
            expected_team_id='OWNTEAM001', bundle_policies=self.policy)
        try:
            with self.assertRaises(Exception):
                self.inspect(inspector=owner)
        finally:
            self.assertTrue(owner.close(deadline_monotonic=time.monotonic() + 5))

    def test_identity_configuration_requires_a_pinned_offline_verifier(self):
        from reproloop.ios_code_signature import IOSCodeSignatureError, IOSCodeSignatureInspector
        with self.assertRaises(IOSCodeSignatureError) as caught:
            IOSCodeSignatureInspector(self.tools, self.root / 'missing-verifier',
                signature_kind='identity', expected_certificate_sha256='0' * 64,
                expected_team_id='OWNTEAM001', bundle_policies=self.policy)
        self.assertEqual(caught.exception.code, 'signature_configuration')
        self.assertFalse((self.root / 'missing-verifier').exists())

    def test_fixed_offline_verifier_checks_covered_code_and_resource_changes(self):
        tools = replace(self.tools, verifier=self.verifier, verifier_sha256=sha(self.verifier))
        from reproloop.ios_code_signature import IOSCodeSignatureInspector
        owner = IOSCodeSignatureInspector(tools, self.root / 'offline-inspect',
            signature_kind='identity', expected_certificate_sha256='0' * 64,
            expected_team_id='OWNTEAM001', bundle_policies=self.policy)
        self.addCleanup(lambda: owner.close(deadline_monotonic=time.monotonic()+5))
        def verify():
            return owner._run((self.app,), self.root, threading.Event(), time.monotonic()+10,
                              offline_verification=True)
        self.assertEqual(verify().returncode, 0)
        executable = self.app / self.info['CFBundleExecutable']
        original = executable.read_bytes()
        changed = bytearray(original)
        _, _, offset, _, _ = struct.unpack_from('>iiIII', changed, 8)
        changed[offset + 28] ^= 1
        executable.write_bytes(changed)
        with self.assertRaises(Exception):
            verify()
        executable.write_bytes(original)
        info = dict(self.info, CFBundleVersion='changed')
        (self.app / 'Info.plist').write_bytes(plistlib.dumps(info))
        with self.assertRaises(Exception):
            verify()
        self.assertEqual(owner.active_processes, 0)

    def test_changed_offline_verifier_and_missing_protocol_output_are_rejected(self):
        from reproloop.ios_code_signature import IOSCodeSignatureError, IOSCodeSignatureInspector
        binary = self.root / 'copied-verifier'
        shutil.copyfile(self.verifier, binary); binary.chmod(0o700)
        tools = replace(self.tools, verifier=binary, verifier_sha256=sha(binary))
        owner = IOSCodeSignatureInspector(tools, self.root / 'changed-verifier-inspect',
            signature_kind='identity', expected_certificate_sha256='0' * 64,
            expected_team_id='OWNTEAM001', bundle_policies=self.policy)
        self.addCleanup(lambda: owner.close(deadline_monotonic=time.monotonic()+5))
        binary.write_bytes(b'changed owned native tool')
        with patch.object(owner._owner, 'run') as dispatch:
            with self.assertRaises(IOSCodeSignatureError):
                self.inspect(inspector=owner)
            dispatch.assert_not_called()
        tools = replace(self.tools, verifier=Path('/usr/bin/true'), verifier_sha256=sha('/usr/bin/true'))
        owner2 = IOSCodeSignatureInspector(tools, self.root / 'missing-protocol-inspect',
            signature_kind='identity', expected_certificate_sha256='0' * 64,
            expected_team_id='OWNTEAM001', bundle_policies=self.policy)
        self.addCleanup(lambda: owner2.close(deadline_monotonic=time.monotonic()+5))
        with self.assertRaises(IOSCodeSignatureError):
            self.inspect(inspector=owner2)

    def test_wrong_entitlements_and_incomplete_bundle_inventory_are_rejected(self):
        from reproloop.ios_code_signature import IOSCodeSignatureInspector
        wrong = {'.': {'bundleId': self.info['CFBundleIdentifier'], 'entitlements': {}}}
        owner = IOSCodeSignatureInspector(self.tools, self.root / 'wrong-policy',
            signature_kind='adhoc-simulator', bundle_policies=wrong)
        try:
            with self.assertRaises(Exception):
                self.inspect(inspector=owner)
        finally:
            owner.close(deadline_monotonic=time.monotonic() + 5)
        extra = {**self.policy, 'Frameworks/Missing.framework': {
            'bundleId': 'com.example.missing', 'entitlements': {}}}
        owner = IOSCodeSignatureInspector(self.tools, self.root / 'extra-policy',
            signature_kind='adhoc-simulator', bundle_policies=extra)
        try:
            with self.assertRaises(Exception):
                self.inspect(inspector=owner)
        finally:
            owner.close(deadline_monotonic=time.monotonic() + 5)
        self.assertEqual(self.inspector.active_processes, 0)

    def test_cancelled_and_closed_inspection_never_produces_a_proof(self):
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(Exception):
            self.inspect(cancellation=cancelled)
        self.assertEqual(list(self.inspector.work_root.iterdir()), [])
        proof = self.inspect()
        self.assertTrue(self.inspector.close(deadline_monotonic=time.monotonic() + 5))
        with self.assertRaises(Exception):
            self.inspector.require_verified(proof, self.selected, context_digest='a' * 64)
        with self.assertRaises(Exception):
            self.inspect()

    def test_close_waits_for_staging_and_blocks_late_native_dispatch(self):
        from reproloop import ios_code_signature as module
        entered, release = threading.Event(), threading.Event()
        actual = module.stage_ios_artifact
        failures = []
        def staged(*args):
            entered.set(); release.wait(5)
            return actual(*args)
        def worker():
            try:
                self.inspect()
            except module.IOSCodeSignatureError as error:
                failures.append(error.code)
        with patch.object(module, 'stage_ios_artifact', side_effect=staged):
            thread = threading.Thread(target=worker); thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.inspector.close(deadline_monotonic=time.monotonic() + .03))
            finally:
                release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, ['signature_cancelled'])
        self.assertTrue(self.inspector.close(deadline_monotonic=time.monotonic() + 5))
        self.assertEqual(list(self.inspector.work_root.iterdir()), [])

    def test_stopped_native_process_is_collected_on_timeout(self):
        from reproloop.ios_code_signature import IOSCodeSignatureError
        actual = subprocess.Popen
        started = threading.Event()
        def popen(*args, **kwargs):
            process = actual(*args, **kwargs)
            if process.poll() is None:
                os.kill(process.pid, signal.SIGSTOP); started.set()
            return process
        with patch('reproloop.repair_android_signing.subprocess.Popen', side_effect=popen):
            with self.assertRaises(IOSCodeSignatureError) as caught:
                self.inspect(deadline_monotonic=time.monotonic() + .25)
        self.assertTrue(started.is_set())
        self.assertEqual(caught.exception.code, 'signature_timeout')
        self.assertTrue(caught.exception.cleanup_confirmed)
        self.assertEqual(self.inspector.active_processes, 0)
        self.assertEqual(list(self.inspector.work_root.iterdir()), [])

    def test_native_inspector_cannot_read_an_app_outside_its_staging_root(self):
        control = subprocess.run(['/usr/bin/codesign', '--display', str(self.app)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        self.assertEqual(control.returncode, 0)
        actual = self.inspector._owner.run
        denied = []
        def checked(*args, **kwargs):
            if not denied:
                result = subprocess.run(['/usr/bin/sandbox-exec', '-p', args[0][2], '/usr/bin/codesign',
                    '--display', str(self.app)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, timeout=5)
                denied.append(result.returncode != 0)
            return actual(*args, **kwargs)
        with patch.object(self.inspector._owner, 'run', side_effect=checked):
            self.inspect()
        self.assertEqual(denied, [True])

    def test_cleanup_does_not_delete_a_replacement_directory_after_its_identity_check(self):
        from reproloop import ios_code_signature as module
        borrowed = self.root / 'unrelated-owned-content'; borrowed.mkdir()
        (borrowed / 'marker').write_bytes(b'preserve this unrelated test content')
        active = {'work': None, 'swapped': False}
        original_discard = self.inspector._discard
        original_stat = os.stat
        def discard(work):
            active['work'] = work
            return original_discard(work)
        def inspect_stat(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            work = active['work']
            if (work is not None and not active['swapped'] and path == work.name
                    and kwargs.get('dir_fd') is not None and kwargs.get('follow_symlinks') is False):
                active['swapped'] = True
                work.rename(self.root / 'retained-original-work')
                borrowed.rename(work)
            return info
        with patch.object(self.inspector, '_discard', side_effect=discard), \
                patch.object(module.os, 'stat', side_effect=inspect_stat):
            with self.assertRaises(module.IOSCodeSignatureError) as caught:
                self.inspect()
        self.assertTrue(active['swapped'])
        self.assertTrue((active['work'] / 'marker').is_file())
        self.assertFalse(caught.exception.cleanup_confirmed)


if __name__ == '__main__':
    unittest.main()
