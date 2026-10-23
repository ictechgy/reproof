"""Portable runtime-asset integrity checks, without building/installing a wheel."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof import resources


class RuntimeResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.package = self.root / 'reproof'
        self.package.mkdir()
        package_patch = patch.object(resources, 'PACKAGE', self.package)
        package_patch.start()
        self.addCleanup(package_patch.stop)
        self.name = 'live-web/app.js'
        self.raw = b'public synthetic resource\n'
        self.digest = hashlib.sha256(self.raw).hexdigest()

    def prepare(self, *, installed):
        root = self.package / '_assets' if installed else self.root
        asset = root / self.name
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(self.raw)
        if not installed:
            (self.root / 'pyproject.toml').write_text('[project]\nname = "fixture"\n')
        self.manifest = root / ('manifest.json' if installed else 'distribution-resources.json')
        value = {'schemaVersion': 1, 'files': [self.name]}
        if installed:
            value['sha256'] = {self.name: self.digest}
        self.manifest.write_text(json.dumps(value))
        return root

    def assert_rejected(self):
        self.assertEqual(resources.installation_check()['status'], 'resource-integrity-failed')
        with self.assertRaises(resources.ResourceError):
            resources.read_resource(self.name)
        output = self.root / 'exported'
        with self.assertRaises(resources.ResourceError):
            resources.export_resources(output)
        self.assertFalse(output.exists())

    def test_valid_installed_resources_read_check_and_export(self):
        self.prepare(installed=True)
        self.assertEqual(resources.read_resource(self.name), self.raw)
        self.assertEqual(resources.installation_check()['status'], 'ready')
        output = self.root / 'exported'
        self.assertEqual(resources.export_resources(output)['status'], 'exported')
        self.assertEqual((output / self.name).read_bytes(), self.raw)

    def test_valid_checkout_resources_freeze_without_packaging_tools(self):
        root = self.prepare(installed=False)
        self.assertEqual(resources.freeze_resource_files(root), {self.name: self.raw})
        self.assertEqual(resources.installation_check()['distribution'], 'checkout')
        self.assertEqual(resources.read_resource(self.name), self.raw)

    def test_duplicate_installed_members_fail_closed(self):
        self.prepare(installed=True)
        original = self.manifest.read_text()
        variants = {
            'version': original.replace('"schemaVersion": 1', '"schemaVersion": 999, "schemaVersion": 1'),
            'files': original.replace('"files": [', '"files": ["live-web/missing.js"], "files": ['),
            'hash-map': original.replace('"sha256": {', '"sha256": {}, "sha256": {'),
            'digest': original.replace('"sha256": {', '"sha256": {"live-web/app.js": "' + '0' * 64 + '", '),
            'same-value': original.replace('"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'),
            'escaped-key': original.replace('"sha256": {', '"sha256": {"live-web/app\\u002ejs": "' + self.digest + '", '),
        }
        for label, raw in variants.items():
            with self.subTest(label=label):
                self.manifest.write_text(raw)
                self.assert_rejected()

    def test_duplicate_checkout_files_fail_before_asset_read(self):
        root = self.prepare(installed=False)
        raw = self.manifest.read_text().replace('"files": [', '"files": ["live-web/missing.js"], "files": [')
        self.manifest.write_text(raw)
        with patch.object(resources, '_read', wraps=resources._read) as read:
            with self.assertRaises(resources.ResourceError):
                resources.freeze_resource_files(root)
            read.assert_called_once_with(root, 'distribution-resources.json', maximum=512 * 1024)
        self.assert_rejected()

    def test_changed_installed_bytes_remain_rejected(self):
        root = self.prepare(installed=True)
        (root / self.name).write_bytes(b'changed')
        self.assert_rejected()


if __name__ == '__main__':
    unittest.main()
