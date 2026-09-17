"""Profile bytes must belong to the exact parsed app/IPA and bundle IDs."""
from dataclasses import replace
import gc
import hashlib
import json
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile
import weakref

from reproloop.core import ContractError
from tests import test_ios_artifact_transfer as support
from tests import test_ios_provisioning_cms as cms_support
from tests import test_ios_provisioning_policy as policy_support


class IOSArtifactProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.IOSArtifactTransferTests(methodName='runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.app = self.fixture.make_app(links=False)
        version = self.app / 'Frameworks/Foo.framework/Versions/A'
        (version / 'Foo').unlink(); version.rmdir(); version.parent.rmdir()

    def parse(self, source=None):
        from reproloop.ios_artifact_provisioning import parse_provisioned_ios_artifact
        return parse_provisioned_ios_artifact(self.app if source is None else source)

    def test_profiles_are_private_and_bound_to_exact_bundle_identifiers(self):
        from reproloop.ios_artifact_provisioning import require_provisioned_ios_artifact
        from reproloop.ios_artifact_transfer import parse_ios_artifact
        selected = self.parse()
        self.assertIs(require_provisioned_ios_artifact(selected), selected)
        self.assertEqual(selected.artifact.app_digest, parse_ios_artifact(self.app).app_digest)
        rows = selected.bundles
        self.assertEqual([(row.bundle_path, row.bundle_id) for row in rows], [
            ('.', 'com.example.inventory'), ('PlugIns/Widget.appex', 'com.example.inventory.widget')])
        self.assertEqual(rows[0].cms_bytes, b'owned dummy root profile')
        self.assertEqual(rows[0].cms_digest, hashlib.sha256(rows[0].cms_bytes).hexdigest())
        self.assertEqual(rows[1].cms_bytes, b'owned dummy extension profile')
        public = json.dumps(selected.public(), sort_keys=True)
        self.assertNotIn('owned dummy', public)
        self.assertNotIn('embedded.mobileprovision', public)
        self.assertNotIn(str(self.app), public)
        for forged in (selected.public(), replace(selected)):
            with self.assertRaises(ContractError):
                require_provisioned_ios_artifact(forged)

    def test_ipa_and_app_have_matching_profile_bindings_after_private_extraction_ends(self):
        archive = self.fixture.root / 'owned.ipa'
        with zipfile.ZipFile(archive, 'w') as output:
            for file in sorted(self.app.rglob('*')):
                name = 'Payload/Inventory.app/' + file.relative_to(self.app).as_posix()
                if file.is_dir():
                    support._zip_directory(output, name)
                else:
                    support._zip_file(output, name, file.read_bytes(), executable=bool(file.stat().st_mode & 0o111))
        app = self.parse(); ipa = self.parse(archive)
        self.assertEqual(ipa.artifact.app_digest, app.artifact.app_digest)
        self.assertNotEqual(ipa.artifact.container_digest, app.artifact.container_digest)
        self.assertEqual([(row.bundle_id, row.cms_digest) for row in ipa.bundles],
                         [(row.bundle_id, row.cms_digest) for row in app.bundles])
        self.assertEqual([row.cms_bytes for row in ipa.bundles], [row.cms_bytes for row in app.bundles])

    def test_missing_profile_remains_explicit_in_the_required_bundle_inventory(self):
        (self.app / 'PlugIns/Widget.appex/embedded.mobileprovision').unlink()
        selected = self.parse()
        self.assertEqual(len(selected.bundles), 2)
        self.assertIsNone(selected.bundles[1].cms_bytes)
        self.assertIsNone(selected.bundles[1].cms_digest)
        first = selected.artifact.app_digest
        (self.app / 'embedded.mobileprovision').write_bytes(b'different-owned-profile')
        second = self.parse()
        self.assertNotEqual(first, second.artifact.app_digest)
        self.assertNotEqual(selected.binding_digest, second.binding_digest)

    def test_profile_changes_during_capture_do_not_issue_an_artifact_binding(self):
        from reproloop import ios_artifact_transfer as transfer
        actual = transfer._read_tree_file
        def changed(tree, logical_path, **kwargs):
            if logical_path == 'embedded.mobileprovision':
                (self.app / logical_path).write_bytes(b'changed-during-capture')
            return actual(tree, logical_path, **kwargs)
        with patch.object(transfer, '_read_tree_file', side_effect=changed):
            with self.assertRaises(ContractError):
                self.parse()

    def test_oversized_profile_is_rejected_before_capturing_its_bytes(self):
        from reproloop import ios_artifact_transfer as transfer
        (self.app / 'embedded.mobileprovision').write_bytes(b'x' * (4 * 1024 * 1024 + 1))
        actual = transfer._read_tree_file
        profiles = []
        def read(tree, logical_path, **kwargs):
            if logical_path.endswith('.mobileprovision'):
                profiles.append(logical_path)
            return actual(tree, logical_path, **kwargs)
        with patch.object(transfer, '_read_tree_file', side_effect=read):
            with self.assertRaises(ContractError):
                self.parse()
        self.assertEqual(profiles, [])

    def test_combined_profile_capture_is_bounded_before_any_profile_is_retained(self):
        from reproloop import ios_artifact_transfer as transfer
        body = b'x' * (4 * 1024 * 1024)
        (self.app / 'embedded.mobileprovision').write_bytes(body)
        (self.app / 'PlugIns/Widget.appex/embedded.mobileprovision').write_bytes(body)
        for number in range(15):
            bundle = self.app / 'PlugIns' / f'Extra{number}.appex'
            support._write(bundle / 'Info.plist', support._app_info(
                f'com.example.inventory.extra{number}', 'Extra'))
            support._write(bundle / 'Extra', support.MACHO64_ARM64, executable=True)
            support._write(bundle / 'embedded.mobileprovision', body)
        actual = transfer._read_tree_file
        profiles = []
        def read(tree, logical_path, **kwargs):
            if logical_path.endswith('.mobileprovision'):
                profiles.append(logical_path)
            return actual(tree, logical_path, **kwargs)
        with patch.object(transfer, '_read_tree_file', side_effect=read):
            with self.assertRaisesRegex(ContractError, 'provisioning capture exceeds'):
                self.parse()
        self.assertEqual(profiles, [])


class IOSArtifactProvisioningVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cms_support.IOSCmsVerificationTests.setUpClass()
        cls.material = cms_support.IOSCmsVerificationTests
        cls.addClassCleanup(cls.material.doClassCleanups)

    def setUp(self):
        self.fixture = IOSArtifactProvisioningTests(methodName='runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.app = self.fixture.app
        self.root = self.fixture.fixture.root
        for name in ('embedded.mobileprovision', 'PlugIns/Widget.appex/embedded.mobileprovision'):
            (self.app / name).write_bytes(self.material.cms)

    def verifier(self, policies=None):
        from reproloop import contracts
        from reproloop.ios_artifact_provisioning import IOSArtifactProvisioningVerifier
        from reproloop.ios_provisioning_cms import IOSCmsTools, IOSCmsTrust
        from reproloop.ios_provisioning_policy import decoded_profile_digest
        tools = IOSCmsTools(self.material.openssl,
            hashlib.sha256(self.material.openssl.read_bytes()).hexdigest(),
            hashlib.sha256(Path('/usr/bin/sandbox-exec').read_bytes()).hexdigest())
        trust = IOSCmsTrust('owned-bundle-profile-issuer', self.material.certs['signer'],
                           (self.material.certs['root-a'],))
        required = {}
        for path, bundle in (('.', policy_support.BUNDLE),
                             ('PlugIns/Widget.appex', policy_support.BUNDLE + '.widget')):
            entitlements = policy_support.expected_entitlements(bundle=bundle)
            required[path] = {'bundleId': bundle,
                'profileDigest': decoded_profile_digest(self.material.profile),
                'entitlements': entitlements, 'entitlementsDigest': contracts.digest(entitlements)}
        required = required if policies is None else policies(required)
        owner = IOSArtifactProvisioningVerifier(tools, self.root / 'native-bundle-profiles', trust=trust,
            expected_certificate_sha256=hashlib.sha256(policy_support.CERTIFICATE).hexdigest(),
            team_id=policy_support.TEAM, application_identifier_prefix=policy_support.TEAM,
            selected_device=policy_support.DEVICE, bundle_policies=required)
        self.addCleanup(lambda: owner.close(deadline_monotonic=time.monotonic() + 5))
        return owner, required

    def verify(self, owner, artifact):
        return owner.verify(artifact, context_digest='c' * 64, evaluated_at=self.material.evaluated,
                            cancellation=threading.Event(), deadline_monotonic=time.monotonic() + 10)

    def test_actual_cms_and_policy_cover_both_bundles_in_one_artifact_context(self):
        from reproloop.ios_artifact_provisioning import parse_provisioned_ios_artifact
        owner, policy = self.verifier()
        # Operator input is snapshotted at construction.
        policy['.']['entitlements']['get-task-allow'] = False
        artifact = parse_provisioned_ios_artifact(self.app)
        result = self.verify(owner, artifact)
        document = owner.require_verified(result, artifact, context_digest='c' * 64)
        self.assertEqual(document['appDigest'], artifact.artifact.app_digest)
        self.assertEqual(len(document['bundles']), 2)
        self.assertTrue(all(row['valid'] for row in document['bundles']))
        self.assertNotIn(policy_support.DEVICE, json.dumps(document))
        for forged in (result.public(), replace(result)):
            with self.assertRaises(Exception):
                owner.require_verified(forged, artifact, context_digest='c' * 64)
        with self.assertRaises(Exception):
            owner.require_verified(result, artifact, context_digest='d' * 64)
        (self.app / 'new-resource.txt').write_bytes(b'changed artifact')
        changed = parse_provisioned_ios_artifact(self.app)
        with self.assertRaises(Exception):
            owner.require_verified(result, changed, context_digest='c' * 64)
        owner.release(result)
        with self.assertRaises(Exception):
            owner.require_verified(result, artifact, context_digest='c' * 64)

    def test_missing_or_mismatched_bundle_policies_never_issue_partial_success(self):
        from reproloop.ios_artifact_provisioning import parse_provisioned_ios_artifact
        def changed(required):
            required['PlugIns/Widget.appex']['profileDigest'] = '0' * 64
            return required
        owner, _ = self.verifier(changed)
        artifact = parse_provisioned_ios_artifact(self.app)
        with self.assertRaises(Exception):
            self.verify(owner, artifact)
        self.assertEqual(owner.retained_profile_bytes, 0)
        self.assertEqual(owner.active_processes, 0)
        (self.app / 'PlugIns/Widget.appex/embedded.mobileprovision').unlink()
        absent = parse_provisioned_ios_artifact(self.app)
        with self.assertRaises(Exception):
            self.verify(owner, absent)
        self.assertEqual(owner.retained_profile_bytes, 0)

    def test_result_collection_close_and_initial_cancellation_preserve_owner_bounds(self):
        from reproloop.ios_artifact_provisioning import parse_provisioned_ios_artifact
        from reproloop.ios_provisioning_cms import IOSCmsError
        owner, _ = self.verifier()
        artifact = parse_provisioned_ios_artifact(self.app)
        result = self.verify(owner, artifact)
        self.assertGreater(owner.retained_profile_bytes, 0)
        reference = weakref.ref(result)
        del result; gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(owner.retained_profile_bytes, 0)
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(IOSCmsError) as caught:
            owner.verify(artifact, context_digest='c' * 64, evaluated_at=self.material.evaluated,
                         cancellation=cancelled, deadline_monotonic=time.monotonic() + 10)
        self.assertEqual(caught.exception.code, 'cms_cancelled')
        result = self.verify(owner, artifact)
        self.assertTrue(owner.close(deadline_monotonic=time.monotonic() + 5))
        self.assertEqual(owner.retained_profile_bytes, 0)
        with self.assertRaises(IOSCmsError):
            owner.require_verified(result, artifact, context_digest='c' * 64)
        self.assertEqual(owner.active_processes, 0)


if __name__ == '__main__':
    unittest.main()
