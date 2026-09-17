from dataclasses import replace
import json
import threading
import time
import unittest

from reproloop.ios_device_tools import IOSDeviceToolError
from reproloop.ios_mobile_install import IOSInstallObservation
from tests import test_ios_mobile_install as install


class IOSMobileInstalledIdentityTests(unittest.TestCase):
    def setUp(self):
        self.g = install.IOSMobileInstallTests(methodName='runTest')
        self.g.setUp()
        self.addCleanup(self.g.doCleanups)
        self.g.configure(extra="""
if args[:3] == ['device','info','apps']:
    result={'apps':[{'bundleIdentifier':BUNDLE,'version':'1.0','bundleVersion':'27'}]}
""")

    def installed(self):
        manager = self.g.installed_owner()
        owner, installer = manager.__enter__()
        self.addCleanup(lambda: manager.__exit__(None, None, None))
        permit = self.g.permit(installer)
        observation = self.g.run_command(installer, permit)
        return owner, installer, observation

    def test_identity_observation_binds_issued_install_source_and_app_fields(self):
        owner, installer, installed = self.installed()
        identity = installer.observe_installed(installed, cancellation=threading.Event(),
            deadline_monotonic=time.monotonic()+8)
        public = identity.public()
        self.assertEqual(public['kind'], 'ios-installed-identity')
        self.assertEqual(public['command'], 'install-candidate')
        self.assertEqual(public['nativeBindingDigest'], owner.binding_digest)
        self.assertEqual(public['contextDigest'], owner.operation.context.digest)
        self.assertEqual(public['sourceRole'], 'candidate')
        self.assertEqual(public['bundleId'], 'com.example.flat')
        self.assertEqual(public['bundleVersion'], '1.0')
        self.assertEqual(public['bundleBuild'], '27')
        self.assertTrue(public['identityConfirmed'])
        self.assertFalse(public['installedArtifactVerified'])
        self.assertFalse(public['deviceCleanupConfirmed'])
        self.assertEqual(public['executionAuthority'], 'none')
        self.assertEqual(public['sourceAppDigest'], installer.payload('install-candidate')['appDigest'])
        self.assertNotIn(self.g.g.c.selected.udid, json.dumps(public))
        self.assertNotIn(str(self.g.g.c.root), json.dumps(public))

    def test_copied_or_foreign_install_observation_is_rejected(self):
        _owner, installer, installed = self.installed()
        copied = replace(installed)
        with self.assertRaises(IOSDeviceToolError):
            installer.observe_installed(copied, cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+8)
        foreign = IOSInstallObservation(installed.command, installed.native_binding_digest,
            installed.payload_digest, installed.evidence_digest)
        with self.assertRaises(IOSDeviceToolError):
            installer.observe_installed(foreign, cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+8)

    def test_installed_version_build_mismatch_is_rejected(self):
        self.g.configure(extra="""
if args[:3] == ['device','info','apps']:
    result={'apps':[{'bundleIdentifier':BUNDLE,'version':'9.9','bundleVersion':'99'}]}
""")
        _owner, installer, installed = self.installed()
        with self.assertRaises(IOSDeviceToolError):
            installer.observe_installed(installed, cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+8)

    def test_stale_prepared_source_is_rejected(self):
        _owner, installer, installed = self.installed()
        root = self.g.g.operations.operations / self.g.g.c.context.operation_id
        (root / 'candidate' / 'App.app' / 'stale.txt').write_text('changed')
        with self.assertRaises(IOSDeviceToolError):
            installer.observe_installed(installed, cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+8)

    def test_rewritten_install_intent_cannot_issue_identity(self):
        owner, installer, installed = self.installed()
        work = owner.operations.operations / owner.operation.context.operation_id / 'command-install-candidate-work'
        for name in ('intent.json', 'state.json'):
            path = work / name
            value = json.loads(path.read_bytes())
            value['dispatchOperationId'] = 'rewritten-dispatch'
            value['permitFingerprint'] = 'a' * 64
            path.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')))
        with self.assertRaises(IOSDeviceToolError):
            installer.observe_installed(installed, cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+8)

    def test_cancelled_or_closed_installer_cannot_observe(self):
        _owner, installer, installed = self.installed()
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(IOSDeviceToolError):
            installer.observe_installed(installed, cancellation=cancelled,
                deadline_monotonic=time.monotonic()+8)
        installer.close()
        with self.assertRaises(IOSDeviceToolError):
            installer.observe_installed(installed, cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+8)


if __name__ == '__main__':
    unittest.main()
