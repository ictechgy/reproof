"""Portable Android locator boundary probes, not native/device acceptance."""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from reproof.live.android_live import AndroidLiveProvider
from reproof.live.model import LiveError


class AndroidLocatorGeometryTests(unittest.TestCase):
    def setUp(self):
        # Exercise the real resolver and metadata validator without creating an
        # ADB client, helper process, installed app, or device authority.
        self.provider = provider = object.__new__(AndroidLiveProvider)
        provider.general_profile = True
        provider.explicit_profile = True
        provider.target_package = 'com.example.checkout'
        provider.app_profile = SimpleNamespace(
            digest='1' * 64, native_digest='2' * 64,
            data={'capabilities': {'locator': {'targets': ['checkout_button']},
                                   'actions': ['tap']}})
        provider.device_authority = None
        provider.automatic_app_logs = False
        provider.provider_incarnation = 'provider_one'
        provider.sid = 'synthetic_session'
        provider.frame_mutex = threading.RLock()
        provider.frame_map = {12: 7}
        provider.last_native_frame = 20
        provider.lab = Mock()
        # Host geometry and native geometry are independent counters. JPEG
        # dimensions are scaled and must never stand in for display pixels.
        provider.lab.frame.return_value = {
            'id': 12, 'geometryVersion': 2, 'width': 540, 'height': 960,
            'orientation': 'portrait'}
        self.snapshot = {
            'profileDigest': '1' * 64, 'nativeDigest': '2' * 64,
            'targetPackage': provider.target_package, 'generalProfile': True,
            'capabilities': {'actions': ['tap']}, 'ready': True,
            'screenBounds': {'left': 100, 'top': 200, 'right': 900, 'bottom': 1500},
            'locatorEvidence': {
                'version': 1, 'coordinateSpace': 'display',
                'displayWidth': 1080, 'displayHeight': 1920,
                'geometryVersion': 7, 'nativeFrameId': 20, 'complete': True},
            'nodes': [{'id': 'checkout_button', 'visible': True, 'enabled': True,
                       'bounds': {'left': 180, 'top': 280, 'right': 220, 'bottom': 320}}]}
        provider.transport = Mock()
        provider.transport.call.return_value = self.snapshot
        self.target = {'kind': 'resource-id', 'value': 'checkout_button'}

    def resolve(self):
        return self.provider.resolve_locator(self.target)

    def assert_rejected(self):
        with self.assertRaises(LiveError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, 'observation_failed')

    def test_inset_window_uses_display_coordinates_not_accessibility_root(self):
        value = self.resolve()
        self.assertAlmostEqual(value['x'] * 1079, 200)
        self.assertAlmostEqual(value['y'] * 1919, 300)
        self.assertEqual((value['frameId'], value['geometryVersion']), (12, 2))
        self.provider.transport.call.assert_called_once_with('/inspect')

    def test_zero_origin_partial_window_and_scaled_jpeg_do_not_change_coordinates(self):
        self.snapshot['screenBounds'] = {'left': 0, 'top': 0, 'right': 700, 'bottom': 900}
        value = self.resolve()
        self.assertAlmostEqual(value['x'] * 1079, 200)
        self.assertAlmostEqual(value['y'] * 1919, 300)

    def test_full_display_window_keeps_exact_pixel_center(self):
        self.snapshot['screenBounds'] = {'left': 0, 'top': 0, 'right': 1080, 'bottom': 1920}
        value = self.resolve()
        self.assertAlmostEqual(value['x'] * 1079, 200)
        self.assertAlmostEqual(value['y'] * 1919, 300)

    def test_landscape_uses_native_display_dimensions(self):
        self.snapshot['locatorEvidence'].update(displayWidth=1920, displayHeight=1080)
        self.provider.lab.frame.return_value.update(width=960, height=540, orientation='landscape')
        value = self.resolve()
        self.assertAlmostEqual(value['x'] * 1919, 200)
        self.assertAlmostEqual(value['y'] * 1079, 300)

    def test_fresh_snapshot_may_be_ahead_of_buffered_frame_in_same_geometry(self):
        self.snapshot['locatorEvidence']['nativeFrameId'] = 22
        self.resolve()

    def test_old_helper_missing_evidence_fails_only_locator(self):
        self.snapshot.pop('locatorEvidence')
        self.assertEqual(self.provider.observe(), self.snapshot)
        self.assert_rejected()

    def test_evidence_is_exact_versioned_and_display_bound(self):
        original = copy.deepcopy(self.snapshot['locatorEvidence'])
        for change in ({'version': 2}, {'version': True},
                       {'coordinateSpace': 'window'}, {'extra': 'unknown'},
                       {'complete': False}, {'complete': 1},
                       {'nativeFrameId': True}, {'nativeFrameId': 0},
                       {'geometryVersion': True}, {'geometryVersion': 0}):
            with self.subTest(change=change):
                self.snapshot['locatorEvidence'] = dict(original, **change)
                self.assert_rejected()
        for key in original:
            with self.subTest(missing=key):
                self.snapshot['locatorEvidence'] = dict(original)
                self.snapshot['locatorEvidence'].pop(key)
                self.assert_rejected()
        for bad in (None, [], 'display'):
            with self.subTest(evidence=bad):
                self.snapshot['locatorEvidence'] = bad
                self.assert_rejected()

    def test_invalid_display_dimensions_fail_closed(self):
        for key in ('displayWidth', 'displayHeight'):
            for bad in (None, True, 0, -1, 1080.0, float('nan'), 2 ** 31):
                with self.subTest(key=key, bad=bad):
                    original = self.snapshot['locatorEvidence'][key]
                    self.snapshot['locatorEvidence'][key] = bad
                    self.assert_rejected()
                    self.snapshot['locatorEvidence'][key] = original

    def test_not_ready_or_incomplete_tree_cannot_establish_unique_target(self):
        for ready in (False, None, 1):
            with self.subTest(ready=ready):
                self.snapshot['ready'] = ready
                self.assert_rejected()
        self.snapshot['ready'] = True
        self.snapshot['locatorEvidence']['complete'] = False
        self.assert_rejected()

    def test_missing_or_changed_native_geometry_rejects_current_host_frame(self):
        self.provider.frame_map = {}
        self.assert_rejected()
        self.provider.frame_map = {12: 8}
        self.assert_rejected()

    def test_snapshot_older_than_latest_acquired_frame_is_rejected(self):
        self.snapshot['locatorEvidence']['nativeFrameId'] = 19
        self.assert_rejected()

    def test_frame_advance_during_inspection_rejects_older_snapshot(self):
        def inspect(_path):
            self.provider.last_native_frame = 21
            return self.snapshot
        self.provider.transport.call.side_effect = inspect
        self.assert_rejected()

    def test_display_and_host_orientation_must_agree(self):
        self.provider.lab.frame.return_value['orientation'] = 'landscape'
        self.assert_rejected()

    def test_duplicate_hidden_disabled_or_off_display_targets_fail_closed(self):
        original = copy.deepcopy(self.snapshot['nodes'])
        for nodes in (
                original * 2,
                [dict(original[0], visible=False)],
                [dict(original[0], enabled=False)],
                [dict(original[0], bounds={'left': -300, 'top': 280, 'right': -100, 'bottom': 320})],
                [dict(original[0], bounds={'left': 1080, 'top': 280, 'right': 1120, 'bottom': 320})],
                [dict(original[0], bounds={'left': True, 'top': 280, 'right': 220, 'bottom': 320})],
                [dict(original[0], bounds=['left', 'top', 'right', 'bottom'])]):
            with self.subTest(nodes=nodes):
                self.snapshot['nodes'] = nodes
                self.assert_rejected()

    def test_missing_target_invalid_or_oversized_node_list_fails_closed(self):
        for nodes in (None, {}, 'nodes', [], [{'id': 'other'}], [None] * 2001):
            with self.subTest(nodes_type=type(nodes).__name__,
                              count=len(nodes) if nodes is not None else None):
                self.snapshot['nodes'] = nodes
                self.assert_rejected()

    def test_unsupported_target_is_rejected_before_inspection(self):
        for target in ({'kind': 'resource-id', 'value': 'other'},
                       dict(self.target, role='button')):
            with self.subTest(target=target):
                with self.assertRaises(LiveError) as caught:
                    self.provider.resolve_locator(target)
                self.assertEqual(caught.exception.code, 'unsupported_operation')
        self.provider.transport.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
