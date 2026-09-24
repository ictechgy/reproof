"""Actual owned CMS signatures and explicit trust; no Apple/user profiles."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reproof import contracts
from reproof.ios_provisioning_policy import decoded_profile_digest
from tests import test_ios_provisioning_policy as policy


def sha(body):
    return hashlib.sha256(body).hexdigest()


class IOSCmsVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.material = tempfile.TemporaryDirectory(prefix='owned-cms-test-')
        cls.addClassCleanup(cls.material.cleanup)
        cls.material_root = Path(cls.material.name).resolve()
        cls.openssl = Path('/usr/bin/openssl')
        root = cls.material_root
        config = root / 'test.cnf'
        config.write_text('[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=Owned Test\n'
            '[root]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n'
            '[leaf]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n'
            'extendedKeyUsage=codeSigning\n')
        cls.environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'OPENSSL_CONF': str(config)}
        for name in ('a', 'b'):
            cls.native('req', '-newkey', 'rsa:2048', '-x509', '-nodes', '-days', '1', '-sha256',
                '-config', str(config), '-extensions', 'root', '-subj', '/CN=Owned Root ' + name,
                '-keyout', str(root / f'root-{name}.key'), '-out', str(root / f'root-{name}.pem'))
            (root / f'root-{name}.key').chmod(0o600)
        for name in ('signer', 'other'):
            cls.native('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-config', str(config),
                '-subj', '/CN=Owned ' + name, '-keyout', str(root / f'{name}.key'),
                '-out', str(root / f'{name}.csr'))
            (root / f'{name}.key').chmod(0o600)
            cls.native('x509', '-req', '-in', str(root / f'{name}.csr'), '-CA', str(root / 'root-a.pem'),
                '-CAkey', str(root / 'root-a.key'), '-CAcreateserial', '-days', '1', '-sha256',
                '-extfile', str(config), '-extensions', 'leaf', '-out', str(root / f'{name}.pem'))
        cls.evaluated = datetime.now(timezone.utc).replace(microsecond=0)
        cls.profile = policy.decoded_profile()
        cls.profile.update(CreationDate=(cls.evaluated - timedelta(hours=1)).replace(tzinfo=None),
                           ExpirationDate=(cls.evaluated + timedelta(hours=6)).replace(tzinfo=None))
        cls.content = plistlib.dumps(cls.profile)
        (root / 'profile.plist').write_bytes(cls.content)
        cls.native('cms', '-encrypt', '-binary', '-in', str(root / 'profile.plist'),
                   '-outform', 'DER', '-out', str(root / 'encrypted.cms'), str(root / 'signer.pem'))
        cls.encrypted = (root / 'encrypted.cms').read_bytes()
        for digest in ('sha256', 'md5'):
            cls.native('cms', '-sign', '-binary', '-nodetach', '-md', digest,
                '-in', str(root / 'profile.plist'), '-signer', str(root / 'signer.pem'),
                '-inkey', str(root / 'signer.key'), '-certfile', str(root / 'root-a.pem'),
                '-outform', 'DER', '-out', str(root / (digest + '.cms')))
        cls.cms = (root / 'sha256.cms').read_bytes()
        cls.weak = (root / 'md5.cms').read_bytes()
        cls.certs = {name: ssl.PEM_cert_to_DER_cert((root / (name + '.pem')).read_text())
                     for name in ('root-a', 'root-b', 'signer', 'other')}

    @classmethod
    def native(cls, *arguments):
        result = subprocess.run([str(cls.openssl), *arguments], env=cls.environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if result.returncode:
            raise RuntimeError('owned test certificate setup failed')

    def setUp(self):
        from reproof.ios_provisioning_cms import IOSCmsTools, IOSCmsTrust, IOSCmsVerifier
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tools = IOSCmsTools(self.openssl, sha(self.openssl.read_bytes()),
                                 sha(Path('/usr/bin/sandbox-exec').read_bytes()))
        self.trust = IOSCmsTrust('owned-profile-issuer', self.certs['signer'], (self.certs['root-a'],))
        self.verifier = IOSCmsVerifier(self.tools, self.root / 'verify')
        self.addCleanup(lambda: self.verifier.close(deadline_monotonic=time.monotonic() + 5))

    def verify(self, body=None, **overrides):
        body = self.cms if body is None else body
        values = dict(expected_cms_digest=sha(body), trust=self.trust, evaluated_at=self.evaluated,
                      cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 10)
        return self.verifier.verify(body, **{**values, **overrides})

    def test_actual_signature_and_explicit_chain_issue_only_an_exact_private_capability(self):
        from reproof.ios_provisioning_cms import IOSCmsError
        result = self.verify()
        profile = self.verifier.require_profile(result, trust=self.trust, expected_cms_digest=sha(self.cms))
        self.assertEqual(profile, self.profile)
        public = json.dumps(result.public(), sort_keys=True)
        self.assertNotIn(self.profile['Name'], public)
        self.assertNotIn(policy.DEVICE, public)
        self.assertEqual(result.public()['cmsDigest'], sha(self.cms))
        self.assertEqual(result.public()['contentDigest'], sha(self.content))
        self.assertEqual(list(self.verifier.work_root.iterdir()), [])
        for forged in (result.public(), replace(result)):
            with self.assertRaises(IOSCmsError):
                self.verifier.require_profile(forged, trust=self.trust, expected_cms_digest=sha(self.cms))
        profile['Entitlements']['get-task-allow'] = False
        self.assertEqual(self.verifier.require_profile(result, trust=self.trust,
            expected_cms_digest=sha(self.cms)), self.profile)

    def test_native_signature_anchor_signer_and_expiry_failures_never_issue_content(self):
        from reproof.ios_provisioning_cms import IOSCmsError
        tampered = self.cms.replace(b'private dummy profile name', b'changed dummy profile name')
        self.assertNotEqual(tampered, self.cms)
        for body, overrides in (
            (tampered, {}),
            (self.cms, {'trust': replace(self.trust, anchors_der=(self.certs['root-b'],))}),
            (self.cms, {'trust': replace(self.trust, signer_der=self.certs['other'])}),
            (self.cms, {'evaluated_at': self.evaluated + timedelta(days=2)}),
            (self.encrypted, {}),
            (self.weak, {}),
        ):
            with self.subTest(sha=sha(body), overrides=tuple(overrides)):
                with self.assertRaises(IOSCmsError) as caught:
                    self.verify(body, **overrides)
                self.assertTrue(caught.exception.cleanup_confirmed)
                self.assertNotIn(self.profile['Name'], str(caught.exception))
                self.assertEqual(list(self.verifier.work_root.iterdir()), [])

    def test_wrong_input_digest_invalid_bounds_and_cancellation_have_no_native_work(self):
        from reproof.ios_provisioning_cms import IOSCmsError
        cancelled = threading.Event(); cancelled.set()
        for values in ({'expected_cms_digest': '0' * 64}, {'cancellation': cancelled},
                       {'deadline_monotonic': float('inf')},
                       {'deadline_monotonic': time.monotonic() - 1}):
            with self.subTest(keys=tuple(values)):
                with self.assertRaises(IOSCmsError):
                    self.verify(**values)
                self.assertEqual(list(self.verifier.work_root.iterdir()), [])
                self.assertEqual(self.verifier.active_processes, 0)

    def test_verified_cms_binds_pure_policy_to_its_actual_evaluation_time(self):
        result = self.verify()
        expected = policy.expected_entitlements()
        assessment = self.verifier.assess_profile(result, trust=self.trust,
            expected_cms_digest=sha(self.cms), expected_profile_digest=decoded_profile_digest(self.profile),
            expected_certificate_sha256=sha(policy.CERTIFICATE), bundle_id=policy.BUNDLE,
            team_id=policy.TEAM, application_identifier_prefix=policy.TEAM, selected_device=policy.DEVICE,
            expected_entitlements=expected, expected_entitlements_digest=contracts.digest(expected))
        self.assertTrue(assessment.valid)
        self.assertTrue(self.verifier.close(deadline_monotonic=time.monotonic() + 5))
        with self.assertRaises(Exception):
            self.verifier.require_profile(result, trust=self.trust, expected_cms_digest=sha(self.cms))

    def stopped_native(self, started):
        actual = subprocess.Popen
        def popen(*args, **kwargs):
            process = actual(*args, **kwargs)
            if process.poll() is None:
                os.kill(process.pid, signal.SIGSTOP)
                started.set()
            return process
        return patch('reproof.repair_android_signing.subprocess.Popen', side_effect=popen)

    def test_actual_stopped_native_process_times_out_and_is_collected(self):
        from reproof.ios_provisioning_cms import IOSCmsError
        started = threading.Event()
        with self.stopped_native(started):
            with self.assertRaises(IOSCmsError) as caught:
                self.verify(deadline_monotonic=time.monotonic() + .25)
        self.assertTrue(started.is_set())
        self.assertEqual(caught.exception.code, 'cms_timeout')
        self.assertTrue(caught.exception.cleanup_confirmed)
        self.assertEqual(self.verifier.active_processes, 0)
        self.assertEqual(list(self.verifier.work_root.iterdir()), [])

    def test_keyboard_interrupt_collects_the_owned_native_process_before_return(self):
        started = threading.Event()
        class Interrupt:
            def is_set(self):
                if started.is_set():
                    raise KeyboardInterrupt()
                return False
        with self.stopped_native(started):
            with self.assertRaises(KeyboardInterrupt):
                self.verify(cancellation=Interrupt())
        self.assertTrue(started.is_set())
        self.assertEqual(self.verifier.active_processes, 0)
        self.assertEqual(list(self.verifier.work_root.iterdir()), [])

    def test_close_waits_for_admitted_dispatch_and_rejects_late_capability(self):
        from reproof.ios_provisioning_cms import IOSCmsError
        entered = threading.Event(); release = threading.Event()
        failures = []
        actual = self.verifier._owner.run
        def held(*args, **kwargs):
            entered.set(); release.wait(5)
            return actual(*args, **kwargs)
        def verify():
            try:
                self.verify()
            except IOSCmsError as error:
                failures.append(error.code)
        with patch.object(self.verifier._owner, 'run', side_effect=held):
            worker = threading.Thread(target=verify); worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.verifier.close(deadline_monotonic=time.monotonic() + .03))
            finally:
                release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, ['cms_quarantined'])
        self.assertTrue(self.verifier.close(deadline_monotonic=time.monotonic() + 1))
        self.assertEqual(list(self.verifier.work_root.iterdir()), [])

    def test_trust_and_tool_definitions_reject_extra_bytes_empty_anchors_and_changed_digest(self):
        from reproof.ios_provisioning_cms import IOSCmsError
        for fields in ({'anchors_der': ()}, {'anchors_der': (self.certs['root-a'],) * 2},
                       {'signer_der': self.certs['signer'] + b'ignored'}):
            with self.assertRaises(IOSCmsError):
                replace(self.trust, **fields)
        with self.assertRaises(IOSCmsError):
            replace(self.tools, openssl_sha256='0' * 64)

    def test_work_parent_link_is_rejected_before_creating_another_directory(self):
        from reproof.ios_provisioning_cms import IOSCmsVerifier
        actual = self.root / 'actual-parent'; actual.mkdir()
        alias = self.root / 'alias-parent'; alias.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(Exception):
            IOSCmsVerifier(self.tools, alias / 'must-not-be-created')
        self.assertFalse((actual / 'must-not-be-created').exists())

    def test_private_content_retention_is_bounded_and_release_revokes_the_capability(self):
        from reproof import ios_provisioning_cms as module
        budget = len(self.cms) + len(self.content) - 1
        with patch.object(module, 'MAX_RETAINED_PROFILE_BYTES', budget, create=True):
            first = self.verify()
            with self.assertRaises(module.IOSCmsError):
                self.verify()
            self.verifier.release_profile(first)
            with self.assertRaises(module.IOSCmsError):
                self.verifier.require_profile(first, trust=self.trust, expected_cms_digest=sha(self.cms))
            second = self.verify()
            self.assertEqual(second.content_digest, first.content_digest)

    def test_native_sandbox_cannot_read_material_outside_its_staged_inputs(self):
        canary = self.material_root / 'signer.pem'
        control = subprocess.run([str(self.openssl), 'x509', '-in', str(canary), '-noout'],
            env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5)
        self.assertEqual(control.returncode, 0)
        actual = self.verifier._owner.run
        denied = []
        def inspected(*args, **kwargs):
            profile = args[0][2]
            probe = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, str(self.openssl),
                'x509', '-in', str(canary), '-noout'],
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'OPENSSL_CONF': '/dev/null'},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5)
            denied.append(probe.returncode != 0 and b'Operation not permitted' in probe.stderr)
            return actual(*args, **kwargs)
        with patch.object(self.verifier._owner, 'run', side_effect=inspected):
            self.verify()
        self.assertEqual(denied, [True])


if __name__ == '__main__':
    unittest.main()
