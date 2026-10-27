"""Explicit installed-package subset: no native compiler or device tests."""
import json
import sys
import unittest

from tests import test_distribution as distribution


class PortableDistributionTests(unittest.TestCase):
    # Reuse the owned build/install fixture but never its broad test selection.
    # The CI matrix must build using the same interpreter it claims to test.
    builder_executable = sys.executable
    setUpClass = classmethod(distribution.InstalledDistributionTests.setUpClass.__func__)

    def test_installed_preflight_and_locator_module_are_available(self):
        result = distribution._command([str(self.command), 'live-check-config', '--shared-config',
                                        str(self.work / 'missing-public-config.json')], cwd=self.work)
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'rejected')
        self.assertEqual(report['executionAuthority'], 'none')
        self.assertNotIn('Traceback', result.stderr)
        imported = distribution._command([str(self.python), '-I', '-c',
            'from reproof.live.locator_proposals import LocatorProposalMixin; print(LocatorProposalMixin.__name__)'],
            cwd=self.work)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(imported.stdout.strip(), 'LocatorProposalMixin')
        self.assertEqual(list(self.work.iterdir()), [])


# A closed allowlist, not name filtering or discovery: newly added broad tests
# cannot silently introduce compiler, device, signing or AI effects in this lane.
PORTABLE_TESTS = (
    'test_installed_source_snapshot_cli_roundtrip_without_checkout',
    'test_installed_console_serves_every_runtime_asset',
    'test_installed_check_confirms_native_templates_and_avoids_checkout',
    'test_changed_installed_asset_is_reported_without_executing_it',
    'test_distribution_excludes_local_state_and_credentials',
    'test_installed_resources_can_be_exported_for_native_builds',
    'test_source_distribution_rebuilds_without_the_original_checkout',
    'test_stale_unlisted_build_asset_is_never_published',
    'test_stale_unlisted_python_module_is_never_published',
    'test_newer_cached_python_cannot_replace_current_source',
    'test_rebuild_uses_source_bytes_instead_of_a_newer_build_cache',
)
for _name in PORTABLE_TESTS:
    setattr(PortableDistributionTests, _name, getattr(distribution.InstalledDistributionTests, _name))
