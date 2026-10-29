"""Public certificates and temporary files only; no keys, network or emulator."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof.execution.artifacts import ArtifactError, BlobSet, open_regular
from reproof.execution.wire import safe_transfer_path
from reproof.live.android_recovery_birth import PinnedFile, PinnedTree
from reproof.live import android_recovery_tool_ca as ca

# Public certificate only, CPython 3.13 Lib/test/certdata/leaf-missing-aki.ca.pem
# Git blob 36b202ae02ee54de7197c54ddc3887a35e968f21. No private key was fetched/generated.
CERT = "# Taken from x509-limbo's `rfc5280::aki::leaf-missing-aki` testcase.\n# See: https://x509-limbo.com/testcases/rfc5280/#rfc5280akileaf-missing-aki\n-----BEGIN CERTIFICATE-----\nMIIBkDCCATWgAwIBAgIUGjIb/aYm9u9fBh2o4GAYRJwk5XIwCgYIKoZIzj0EAwIw\nGjEYMBYGA1UEAwwPeDUwOS1saW1iby1yb290MCAXDTcwMDEwMTAwMDAwMVoYDzI5\nNjkwNTAzMDAwMDAxWjAaMRgwFgYDVQQDDA94NTA5LWxpbWJvLXJvb3QwWTATBgcq\nhkjOPQIBBggqhkjOPQMBBwNCAARUzBhjMOkO911U65Fvs4YmL1YPNj63P9Fa+g9U\nKrUqiIy8WjaDXdIe8g8Zj0TalpbU1gYCs3atteMxgIp6qxwHo1cwVTAPBgNVHRMB\nAf8EBTADAQH/MAsGA1UdDwQEAwICBDAWBgNVHREEDzANggtleGFtcGxlLmNvbTAd\nBgNVHQ4EFgQUcv1fyqgezMGzmo+lhmUkdUuAbIowCgYIKoZIzj0EAwIDSQAwRgIh\nAIOErPSRlWpnyMub9UgtPF/lSzdvnD4Q8KjLQppHx6oPAiEA373p4L/HvUbs0xg8\n6/pLyn0RT02toKKJcMV3ChohLtM=\n-----END CERTIFICATE-----\n".encode('ascii')


class PublicEmulatorCATests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='public-ca-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.original_pin = ca._APPROVED_CA
        self.exe = self.file('emulator', b'public inert executable')
        Path(self.exe.path).chmod(0o700)
        self.props = self.file('source.properties', b'public synthetic properties')
        self.bundle = self.file('lib/ca-bundle.pem', CERT)
        self.pin = replace(self.original_pin, sha256=self.bundle.sha256, size=self.bundle.size,
            emulator_sha256=self.exe.sha256, emulator_size=self.exe.size,
            properties_sha256=self.props.sha256, properties_size=self.props.size)
        selected = patch.object(ca, '_APPROVED_CA', self.pin)
        selected.start(); self.addCleanup(selected.stop)
        self.tree = PinnedTree(str(self.root), (self.exe, self.props, self.bundle), (self.exe.path,))

    def file(self, relative, raw):
        path = self.root/relative;path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(raw)
        return PinnedFile(str(path), hashlib.sha256(raw).hexdigest(), len(raw))

    def test_exact_context_reads_public_ca_locally_without_network_or_global_trust(self):
        with patch.object(socket, 'socket') as network, patch.object(ssl, 'create_default_context') as defaults:
            self.tree.verify()
        network.assert_not_called(); defaults.assert_not_called()

    def test_recorded_production_pin_is_exact_and_not_generic_pem_permission(self):
        pin = self.original_pin
        self.assertEqual((pin.relative, pin.size), ('lib/ca-bundle.pem', 258424))
        self.assertEqual(pin.sha256, '0f119da204025da7808273fab42ed8e030cafb5c7ea4e1deda4e75f066f528fb')
        self.assertEqual(pin.emulator_sha256, '7e8559aac63105502a6a4fec03dcc0cc7aaaf6f8e1e046bcad29095d46bd26eb')
        self.assertEqual((pin.emulator_size, pin.properties_size), (1694800, 102))
        self.assertEqual(pin.properties_sha256, '1b29166b5f4c5e690859fed6b03c6e37fbfe77e7f103d52ae21796844a91d492')

    def test_general_file_and_transfer_boundaries_still_reject_same_public_bytes(self):
        with self.assertRaises(ArtifactError): self.bundle.verify()
        with self.assertRaises(ArtifactError): open_regular(self.root, 'lib/ca-bundle.pem')
        with self.assertRaises(ArtifactError): BlobSet((('lib/ca-bundle.pem', CERT),))
        with self.assertRaises(ContractError): safe_transfer_path('lib/ca-bundle.pem')

    def test_unapproved_hash_size_companion_or_executable_membership_fails_before_read(self):
        variants = [replace(self.tree, executables=()),
                    replace(self.tree, executables=(self.exe.path, self.bundle.path))]
        for index in range(3):
            for field, value in (('sha256', '0'*64), ('size', self.tree.files[index].size+1)):
                files = list(self.tree.files);files[index] = replace(files[index], **{field:value})
                variants.append(replace(self.tree, files=tuple(files)))
        for tree in variants:
            with self.subTest(tree=tree.executables), patch.object(ca, '_open_ca') as opened, \
                    patch.object(PinnedFile, 'verify') as ordinary:
                with self.assertRaises(ContractError): tree.verify()
                opened.assert_not_called(); ordinary.assert_not_called()

    def test_every_other_secret_path_is_rejected_before_any_selected_content(self):
        for relative in ('other.pem', 'lib/CA-BUNDLE.PEM', 'lib/nested/ca-bundle.pem',
                         'secret.key', 'x.p12', 'x.pfx', 'x.jks', 'x.keystore',
                         'auth.json', 'credentials.json', '.env', 'id_rsa',
                         'x.mobileprovision', 'x.keychain-db'):
            selected = self.file(relative, b'public harmless bytes')
            tree = replace(self.tree, files=self.tree.files+(selected,))
            with self.subTest(relative=relative), patch.object(ca, '_open_ca') as opened, \
                    patch.object(PinnedFile, 'verify') as ordinary:
                with self.assertRaises(ContractError): tree.verify()
                opened.assert_not_called(); ordinary.assert_not_called()

    def test_private_key_markers_unknown_blocks_and_invalid_certificates_rejected(self):
        for raw in (CERT+b'PRIVATE KEY', CERT+b'-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----',
                    CERT+b'-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----',
                    b'-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----',
                    CERT+b'\x00', b'x'*4097+b'\n'+CERT, CERT*1500):
            with self.subTest(size=len(raw)), self.assertRaises(ContractError): ca._certificates_only(raw)
        ca._certificates_only(b'# public curl-style comments\nPublic root\n===========\n'+CERT+CERT)

    def test_utf8_titles_only_outside_strict_ascii_pem_blocks(self):
        ca._certificates_only('# Öffentliches Zertifikat\nRacine émise\n'.encode('utf-8')+CERT)
        for raw in (b'\xff title\n'+CERT, '\u202e'.encode('utf-8')+CERT,
                    CERT.replace(b'MIIB', 'MIIé'.encode('utf-8'), 1),
                    CERT.replace(b'CERTIFICATE', 'CERTIFICATÉ'.encode('utf-8'), 1)):
            with self.subTest(size=len(raw)), self.assertRaises(ContractError):
                ca._certificates_only(raw)

    def test_even_redeclared_private_key_bytes_cannot_use_special_opener(self):
        raw = CERT+b'\nPRIVATE KEY\n'
        changed = self.file('lib/ca-bundle.pem', raw)
        with patch.object(ca, '_APPROVED_CA', replace(self.pin, sha256=changed.sha256, size=changed.size)):
            tree = replace(self.tree, files=(self.exe, self.props, changed))
            with self.assertRaises(ContractError): tree.verify()

    def test_actual_hash_mismatch_and_forged_separate_item_rejected(self):
        Path(self.bundle.path).write_bytes(b'x'*self.bundle.size)
        with self.assertRaises(ContractError): self.tree.verify()
        with patch.object(ca, '_open_ca') as opened:
            with self.assertRaises(ContractError):
                ca.verify_public_ca(self.root, replace(self.bundle, size=8*1024**3), self.tree.files, self.tree.executables)
            opened.assert_not_called()

    def test_symlink_hardlink_and_changed_path_after_parse_rejected(self):
        selected = Path(self.bundle.path); selected.unlink()
        target = self.root/'public-cert';target.write_bytes(CERT);selected.symlink_to(target)
        with self.assertRaises((OSError, ContractError)):
            ca.verify_public_ca(self.root, self.bundle, self.tree.files, self.tree.executables)
        selected.unlink();os.link(target, selected)
        with self.assertRaises(ContractError):
            ca.verify_public_ca(self.root, self.bundle, self.tree.files, self.tree.executables)
        selected.unlink();selected.write_bytes(CERT)
        original = ca._certificates_only
        def replace_after_parse(raw, **kwargs):
            original(raw, **kwargs)
            selected.unlink();selected.write_bytes(CERT)
        with patch.object(ca, '_certificates_only', replace_after_parse), self.assertRaises(ContractError):
            self.tree.verify()

    def test_cancel_deadline_and_data_only_declaration_remain_fail_closed(self):
        cancel = threading.Event();cancel.set()
        with patch.object(ca, '_open_ca') as opened:
            with self.assertRaises(ContractError):
                ca.verify_public_ca(self.root, self.bundle, self.tree.files, self.tree.executables, cancellation=cancel)
            with self.assertRaises(ContractError):
                ca.verify_public_ca(self.root, self.bundle, self.tree.files, self.tree.executables, deadline=time.monotonic()-1)
            opened.assert_not_called()
