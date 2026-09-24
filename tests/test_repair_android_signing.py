import hashlib
import io
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from reproof import contracts
from reproof.execution.artifacts import BlobSet
from reproof.repair_android_signing import (
    AndroidApkInspector,
    AndroidApkSigner,
    AndroidSigningError,
    AndroidSigningIdentity,
    AndroidSigningMaterialResolver,
    AndroidSigningTools,
)
from reproof.repair_signing import SigningContext


CERTIFICATE = "a" * 64
PACKAGE = "com.example.product"
APPLICATION = "android_app"
SCHEMES = ("v2", "v3")
PERMISSIONS = ()
CONFIGURATION = contracts.digest({
    "schemaVersion": 1,
    "signatureSchemes": list(SCHEMES),
    "permissions": list(PERMISSIONS),
})


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_apk():
    value = io.BytesIO()
    with zipfile.ZipFile(value, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"bounded manifest")
        archive.writestr("classes.dex", b"bounded dex")
    return value.getvalue()


def _write_executable(path, body):
    path.write_text(body)
    path.chmod(0o700)
    return path


def _apksigner_script(certificate=CERTIFICATE, *, sign_body=None,
                      verify_exit=0, additional_certificate=None,
                      source_stamp=None):
    sign_body = sign_body or 'cp "$input" "$output"\nprintf signed >> "$output"'
    additional = ("" if additional_certificate is None else
                  f"printf '%s\\n' 'Signer #2 certificate SHA-256 digest: {additional_certificate}'")
    stamp = ("" if source_stamp is None else
             f"printf '%s\\n' 'Source Stamp Signer certificate SHA-256 digest: {source_stamp}'")
    return f'''#!/bin/sh
set -eu
 [ "$1" = -Xmx256m ]
 [ "$2" = -jar ]
 shift 3
mode="$1"
shift
if [ "$mode" = sign ]; then
  case "$*" in *store-secret*|*key-secret*) exit 91;; esac
  IFS= read -r store_password
  IFS= read -r key_password
  [ "$store_password" = store-secret ]
  [ "$key_password" = key-secret ]
  output=
  previous=
  input=
  for argument in "$@"; do
    if [ "$previous" = out ]; then output="$argument"; previous=; continue; fi
    if [ "$argument" = --out ]; then previous=out; continue; fi
    input="$argument"
  done
  {sign_body}
  exit 0
fi
if [ "$mode" = verify ]; then
  printf '%s\\n' 'Signer #1 certificate SHA-256 digest: {certificate}'
  printf '%s\\n' 'Verified using v1 scheme (JAR signing): false'
  printf '%s\\n' 'Verified using v2 scheme (APK Signature Scheme v2): true'
  printf '%s\\n' 'Verified using v3 scheme (APK Signature Scheme v3): true'
  printf '%s\\n' 'Verified using v3.1 scheme (APK Signature Scheme v3.1): false'
  printf '%s\\n' 'Verified using v4 scheme (APK Signature Scheme v4): false'
  {additional}
  {stamp}
  exit {verify_exit}
fi
exit 92
'''


def _aapt_script(package=PACKAGE, *, permissions=PERMISSIONS, exit_code=0):
    permission_lines = "\\n".join(
        f"uses-permission: name='{item}'" for item in permissions)
    return f'''#!/bin/sh
set -eu
printf '%s\\n' 'package: {package}'
printf '%s\\n' '{permission_lines}'
exit {exit_code}
'''


class ProcessOwnerPermissionTests(unittest.TestCase):
    def test_signal_denial_after_child_exit_is_collected_without_another_signal(self):
        from reproof.repair_android_signing import _ProcessOwner
        process=Mock(pid=12345)
        process.poll.side_effect=[None,0]
        with patch('os.killpg',side_effect=PermissionError) as signal_group, \
                patch.object(_ProcessOwner,'_group_empty',return_value=True):
            self.assertTrue(_ProcessOwner._terminate(process))
        self.assertEqual(signal_group.call_count,1)

    def test_signal_denial_for_live_child_does_not_claim_collection(self):
        from reproof.repair_android_signing import _ProcessOwner
        process=Mock(pid=12345);process.poll.return_value=None
        with patch('os.killpg',side_effect=PermissionError), \
                patch.object(_ProcessOwner,'_group_empty',return_value=False):
            self.assertFalse(_ProcessOwner._terminate(process))


class AndroidSigningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.java = _write_executable(
            self.root / "java", _apksigner_script())
        self.apksigner_jar = self.root / "apksigner.jar"
        self.apksigner_jar.write_bytes(b"fixed fake apksigner jar")
        self.aapt = _write_executable(
            self.root / "aapt2", _aapt_script())
        self.tools = AndroidSigningTools(
            self.java, _digest(self.java),
            self.apksigner_jar, _digest(self.apksigner_jar),
            self.aapt, _digest(self.aapt))
        self.identity = AndroidSigningIdentity(
            "owned-signing", APPLICATION, PACKAGE, CERTIFICATE,
            SCHEMES, PERMISSIONS)
        self.policy = {
            "schemaVersion": 1,
            "id": "android-signing",
            "platform": "android",
            "applicationId": APPLICATION,
            "identityReferenceId": self.identity.reference_id,
            "entitlementsDigest": CONFIGURATION,
            "tool": "host-apksigner-fixed",
            "candidateHooks": "forbidden",
            "artifactRelation": "pre-post-digests",
        }
        self.keystore = self.root / "owned-test.p12"
        self.keystore.write_bytes(b"owned ephemeral test key")
        self.keystore.chmod(0o600)
        self.resolver = AndroidSigningMaterialResolver()
        self.resolver.register(
            self.identity, keystore=self.keystore, key_alias="owned-test",
            store_password=b"store-secret", key_password=b"key-secret")
        self.apk = _fake_apk()

    def tearDown(self):
        self.resolver.close()
        self.temporary.cleanup()

    def context(self, body=None, *, signed_digest=None):
        body = self.apk if body is None else body
        return SigningContext(
            "sign-operation", "b" * 64, "c" * 64, APPLICATION,
            "d" * 64, hashlib.sha256(body).hexdigest(),
            contracts.digest(self.policy), "private-nonce",
            signed_artifact_digest=signed_digest)

    def signer(self, **kwargs):
        return AndroidApkSigner(
            self.tools, self.resolver, self.identity, self.policy,
            self.root / "sign-work", **kwargs)

    def inspector(self, tools=None, identity=None, **kwargs):
        return AndroidApkInspector(
            tools or self.tools, identity or self.identity, self.policy,
            self.root / "inspect-work", **kwargs)

    def test_fixed_signer_and_distinct_inspector_bind_artifact_package_and_certificate(self):
        signer = self.signer()
        inspector = self.inspector()
        self.assertIsNot(signer, inspector)

        signed = signer(
            self.context(), BlobSet((("candidate.apk", self.apk),)),
            cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 2)
        signed_body = signed.artifacts.entries[0][1]
        self.assertEqual(signed_body, self.apk + b"signed")
        self.assertTrue(signed.termination_confirmed)
        self.assertTrue(signed.cleanup_confirmed)
        self.assertNotEqual(hashlib.sha256(signed_body).hexdigest(),
                            hashlib.sha256(self.apk).hexdigest())
        self.assertEqual(list((self.root / "sign-work").iterdir()), [])

        inspected = inspector(
            self.context(signed_digest=hashlib.sha256(signed_body).hexdigest()),
            signed.artifacts, cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 2)
        self.assertTrue(inspected.valid)
        self.assertTrue(inspected.termination_confirmed)
        self.assertTrue(inspected.cleanup_confirmed)
        self.assertNotEqual(inspected.evidence_digest, signed.evidence_digest)
        self.assertTrue(inspector.accepts(signed.artifacts))

    def test_policy_and_material_are_exact_and_secrets_never_enter_arguments_or_repr(self):
        self.assertNotIn("store-secret", repr(self.resolver))
        self.assertNotIn("key-secret", repr(self.resolver))
        changed = dict(self.policy, command="apksigner anything")
        with self.assertRaises(AndroidSigningError):
            AndroidApkSigner(
                self.tools, self.resolver, self.identity, changed,
                self.root / "bad-policy")
        wrong = AndroidSigningIdentity(
            "other-signing", APPLICATION, PACKAGE, CERTIFICATE,
            SCHEMES, PERMISSIONS)
        with self.assertRaises(AndroidSigningError):
            AndroidApkSigner(
                self.tools, self.resolver, wrong, self.policy,
                self.root / "bad-material")
        with self.assertRaises(AndroidSigningError):
            AndroidSigningTools(
                Path("java"), "0" * 64, self.apksigner_jar,
                _digest(self.apksigner_jar), self.aapt, _digest(self.aapt))
        with self.assertRaises(AndroidSigningError):
            self.resolver.register(
                wrong, keystore=Path("relative-key.p12"), key_alias="key",
                store_password=b"one", key_password=b"two")
        with self.assertRaises(AndroidSigningError):
            AndroidApkInspector(
                self.tools, self.identity, self.policy,
                Path("relative-work"))

    def test_signer_rejects_wrong_artifact_shape_context_and_tool_tamper(self):
        signer = self.signer()
        for artifacts in (
            BlobSet((("other.apk", self.apk),)),
            BlobSet((("candidate.apk", self.apk), ("extra", b"x"))),
        ):
            with self.subTest(paths=[item[0] for item in artifacts.entries]), \
                    self.assertRaises(AndroidSigningError):
                signer(self.context(), artifacts,
                       cancellation=threading.Event(),
                       deadline_monotonic=time.monotonic() + 2)
        with self.assertRaises(AndroidSigningError):
            signer(self.context(b"different"),
                   BlobSet((("candidate.apk", self.apk),)),
                   cancellation=threading.Event(),
                   deadline_monotonic=time.monotonic() + 2)
        self.apksigner_jar.write_bytes(
            self.apksigner_jar.read_bytes() + b"tampered")
        failed = signer(
            self.context(), BlobSet((("candidate.apk", self.apk),)),
            cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 2)
        self.assertEqual(failed.code, "signing_failed")
        self.assertTrue(failed.termination_confirmed)
        self.assertTrue(failed.cleanup_confirmed)
        self.assertEqual(signer.active_processes, 0)

    def test_manifest_preflight_rejects_before_material_resolution_and_checker_is_pure(self):
        wrong_aapt = _write_executable(
            self.root / "wrong-aapt2", _aapt_script("com.example.other"))
        tools = AndroidSigningTools(
            self.java, _digest(self.java), self.apksigner_jar,
            _digest(self.apksigner_jar), wrong_aapt, _digest(wrong_aapt))
        signer = AndroidApkSigner(
            tools, self.resolver, self.identity, self.policy,
            self.root / "preflight-work")
        self.resolver.close()
        result = signer(
            self.context(), BlobSet((("candidate.apk", self.apk),)),
            cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 2)
        self.assertEqual(result.code, "artifact_invalid")
        self.assertEqual(signer.active_processes, 0)
        self.assertEqual(list((self.root / "preflight-work").iterdir()), [])

        inspector = self.inspector()
        before = tuple((self.root / "inspect-work").iterdir())
        self.assertTrue(inspector.accepts(
            BlobSet((("candidate.apk", self.apk),))))
        self.assertFalse(inspector.accepts(
            BlobSet((("candidate.apk", b"not a zip"),))))
        self.assertEqual(tuple((self.root / "inspect-work").iterdir()), before)
        self.assertEqual(inspector.active_processes, 0)

    def test_inspector_reports_false_for_wrong_package_certificate_or_signature(self):
        signed_body = self.apk + b"signed"
        artifacts = BlobSet((("candidate.apk", signed_body),))
        context = self.context(
            signed_digest=hashlib.sha256(signed_body).hexdigest())
        cases = (
            (_apksigner_script("f" * 64), _aapt_script(), "certificate"),
            (_apksigner_script(additional_certificate="e" * 64),
             _aapt_script(), "multiple-signers"),
            (_apksigner_script(source_stamp="e" * 64),
             _aapt_script(), "source-stamp"),
            (_apksigner_script(), _aapt_script("com.example.other"), "package"),
            (_apksigner_script(),
             _aapt_script(permissions=("android.permission.INTERNET",)),
             "permissions"),
            (_apksigner_script(verify_exit=7), _aapt_script(), "signature"),
        )
        for index, (signer_body, aapt_body, label) in enumerate(cases):
            with self.subTest(label=label):
                java = _write_executable(
                    self.root / f"java-{index}", signer_body)
                apksigner_jar = self.root / f"apksigner-{index}.jar"
                apksigner_jar.write_bytes(b"fake jar")
                aapt = _write_executable(
                    self.root / f"aapt2-{index}", aapt_body)
                tools = AndroidSigningTools(
                    java, _digest(java), apksigner_jar,
                    _digest(apksigner_jar), aapt, _digest(aapt))
                observation = self.inspector(tools=tools)(
                    context, artifacts, cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 2)
                self.assertFalse(observation.valid)
                self.assertTrue(observation.termination_confirmed)
                self.assertTrue(observation.cleanup_confirmed)

    def test_timeout_and_cancellation_terminate_and_collect_the_process_group(self):
        slow = _write_executable(
            self.root / "slow-java",
            _apksigner_script(sign_body="trap '' TERM\nsleep 10"))
        tools = AndroidSigningTools(
            slow, _digest(slow), self.apksigner_jar,
            _digest(self.apksigner_jar), self.aapt, _digest(self.aapt))
        signer = AndroidApkSigner(
            tools, self.resolver, self.identity, self.policy,
            self.root / "slow-work")
        started = time.monotonic()
        timed_out = signer(
            self.context(), BlobSet((("candidate.apk", self.apk),)),
            cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + .05)
        self.assertEqual(timed_out.code, "signing_timeout")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(signer.active_processes, 0)
        cancellation = threading.Event()
        cancellation.set()
        cancelled = signer(
            self.context(), BlobSet((("candidate.apk", self.apk),)),
            cancellation=cancellation,
            deadline_monotonic=time.monotonic() + 2)
        self.assertEqual(cancelled.code, "cancelled")
        signer.close()
        self.assertEqual(signer.active_processes, 0)

        slow_verify_body = _apksigner_script().replace(
            'if [ "$mode" = verify ]; then',
            'if [ "$mode" = verify ]; then\n  trap \'\' TERM\n  sleep 10')
        slow_verify = _write_executable(
            self.root / "slow-verify-java", slow_verify_body)
        inspect_tools = AndroidSigningTools(
            slow_verify, _digest(slow_verify), self.apksigner_jar,
            _digest(self.apksigner_jar), self.aapt, _digest(self.aapt))
        inspector = self.inspector(tools=inspect_tools)
        signed_body = self.apk + b"signed"
        failure = inspector(
            self.context(signed_digest=hashlib.sha256(signed_body).hexdigest()),
            BlobSet((("candidate.apk", signed_body),)),
            cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + .05)
        self.assertEqual(failure.code, "signing_timeout")
        self.assertTrue(failure.termination_confirmed)
        self.assertTrue(failure.cleanup_confirmed)
        self.assertEqual(inspector.active_processes, 0)

    def test_oversize_and_cleanup_uncertainty_preserve_private_failure_artifacts(self):
        large = _write_executable(
            self.root / "large-java",
            _apksigner_script(sign_body=
                'cp "$input" "$output"\nwhile :; do dd if=/dev/zero bs=2048 count=1 >> "$output" 2>/dev/null; sleep 1; done'))
        tools = AndroidSigningTools(
            large, _digest(large), self.apksigner_jar,
            _digest(self.apksigner_jar), self.aapt, _digest(self.aapt))
        signer = AndroidApkSigner(
            tools, self.resolver, self.identity, self.policy,
            self.root / "large-work", max_apk_bytes=1024)
        started = time.monotonic()
        failed = signer(
            self.context(), BlobSet((("candidate.apk", self.apk),)),
            cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 2)
        self.assertEqual(failed.code, "signing_failed")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(list((self.root / "large-work").iterdir()), [])

        extra = _write_executable(
            self.root / "extra-java",
            _apksigner_script(sign_body=
                'cp "$input" "$output"\nprintf signed >> "$output"\nprintf preserve > "$(dirname "$output")/unexpected"'))
        tools = AndroidSigningTools(
            extra, _digest(extra), self.apksigner_jar,
            _digest(self.apksigner_jar), self.aapt, _digest(self.aapt))
        signer = AndroidApkSigner(
            tools, self.resolver, self.identity, self.policy,
            self.root / "extra-work")
        observation = signer(
            self.context(), BlobSet((("candidate.apk", self.apk),)),
            cancellation=threading.Event(),
            deadline_monotonic=time.monotonic() + 2)
        self.assertFalse(observation.cleanup_confirmed)
        self.assertTrue(any(path.name == "unexpected"
                            for work in (self.root / "extra-work").iterdir()
                            for path in work.iterdir()))

    def test_actual_cached_apksigner_signs_owned_public_apk_and_inspector_rejects_tamper(self):
        repository = Path(__file__).parents[1]
        documented_java = Path(
            "/opt/homebrew/opt/openjdk@17/libexec/"
            "openjdk.jdk/Contents/Home/bin/java")
        try:
            java = documented_java.resolve(strict=True)
        except OSError:
            self.skipTest("authorized cached JDK unavailable")
        keytool = java.with_name("keytool")
        build_tools = Path.home() / "Library/Android/sdk/build-tools/36.0.0"
        jar = build_tools / "lib/apksigner.jar"
        aapt = build_tools / "aapt2"
        apk = (repository / "artifacts/product-delivery/d1-android-views-r1/"
               "original-debug/source/app/build/outputs/apk/release/"
               "app-release-unsigned.apk")
        if not all(path.is_file() for path in (java, keytool, jar, aapt, apk)):
            self.skipTest("authorized cached Android signing inputs unavailable")
        password = secrets.token_hex(18).encode("ascii")
        password_variable = "REPROOF_D4_TEST_KEY_PASSWORD"
        key_environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            password_variable: password.decode("ascii"),
        }
        alias = "d4-owned-test"
        actual_key = self.root / "actual-owned-test.p12"
        generate_arguments = [
            str(keytool), "-genkeypair", "-storetype", "PKCS12",
            "-keystore", str(actual_key), "-storepass:env", password_variable,
            "-keypass:env", password_variable, "-alias", alias,
            "-keyalg", "RSA", "-keysize", "2048", "-validity", "1",
            "-dname", "CN=Reproof D4 Owned Test",
        ]
        self.assertNotIn(password.decode("ascii"), generate_arguments)
        generated = subprocess.run(generate_arguments,
            env=key_environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30, check=False)
        self.assertEqual(generated.returncode, 0)
        actual_key.chmod(0o600)
        export_arguments = [
            str(keytool), "-exportcert", "-storetype", "PKCS12",
            "-keystore", str(actual_key), "-storepass:env", password_variable,
            "-alias", alias,
        ]
        self.assertNotIn(password.decode("ascii"), export_arguments)
        exported = subprocess.run(export_arguments,
            env=key_environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30, check=False)
        self.assertEqual(exported.returncode, 0)
        certificate = hashlib.sha256(exported.stdout).hexdigest()
        tools = AndroidSigningTools(
            java, _digest(java), jar, _digest(jar), aapt, _digest(aapt))
        identity = AndroidSigningIdentity(
            "actual-owned-signing", "inventory_app",
            "com.example.reproinventory", certificate, SCHEMES, PERMISSIONS)
        configuration = identity.signing_configuration_digest
        policy = {
            "schemaVersion": 1, "id": "actual-android-signing",
            "platform": "android", "applicationId": "inventory_app",
            "identityReferenceId": "actual-owned-signing",
            "entitlementsDigest": configuration,
            "tool": "host-apksigner-fixed", "candidateHooks": "forbidden",
            "artifactRelation": "pre-post-digests",
        }
        resolver = AndroidSigningMaterialResolver()
        resolver.register(identity, keystore=actual_key, key_alias=alias,
                          store_password=password, key_password=password)
        signer = AndroidApkSigner(
            tools, resolver, identity, policy, self.root / "actual-sign-work")
        inspector = AndroidApkInspector(
            tools, identity, policy, self.root / "actual-inspect-work")
        body = apk.read_bytes()
        context = SigningContext(
            "actual-sign", "b" * 64, "c" * 64, "inventory_app",
            "d" * 64, hashlib.sha256(body).hexdigest(),
            contracts.digest(policy), "actual-private-nonce")
        try:
            signed = signer(
                context, BlobSet((("candidate.apk", body),)),
                cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 30)
            signed_body = signed.artifacts.entries[0][1]
            inspection_context = SigningContext(
                "actual-sign", "b" * 64, "c" * 64, "inventory_app",
                "d" * 64, hashlib.sha256(body).hexdigest(),
                contracts.digest(policy), "actual-inspection-nonce",
                hashlib.sha256(signed_body).hexdigest())
            self.assertTrue(inspector(
                inspection_context, signed.artifacts,
                cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 30).valid)
            self.assertTrue(inspector.accepts(signed.artifacts))

            wrong_certificate = AndroidSigningIdentity(
                identity.reference_id, identity.application_id,
                identity.package_name, "f" * 64, SCHEMES, PERMISSIONS)
            wrong_package = AndroidSigningIdentity(
                identity.reference_id, identity.application_id,
                "com.example.other", certificate, SCHEMES, PERMISSIONS)
            for index, altered in enumerate((wrong_certificate, wrong_package)):
                rejected = AndroidApkInspector(
                    tools, altered, policy,
                    self.root / f"actual-reject-{index}")
                self.assertFalse(rejected(
                    inspection_context, signed.artifacts,
                    cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 30).valid)
            tampered = signed_body + b"tampered"
            tampered_context = SigningContext(
                "actual-sign", "b" * 64, "c" * 64, "inventory_app",
                "d" * 64, hashlib.sha256(body).hexdigest(),
                contracts.digest(policy), "actual-tamper-nonce",
                hashlib.sha256(tampered).hexdigest())
            self.assertFalse(inspector(
                tampered_context, BlobSet((("candidate.apk", tampered),)),
                cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 30).valid)
            public = repr(resolver) + signed.evidence_digest
            self.assertNotIn(password.decode(), public)
        finally:
            signer.close()
            inspector.close()
            resolver.close()


if __name__ == "__main__":
    unittest.main()
