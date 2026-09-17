import base64
from collections import OrderedDict
import json
import struct
import threading
import unittest
from unittest.mock import Mock, patch

from reproloop.live.android_live import AndroidLiveProvider, UsbBridgeClient
from reproloop.live.authority import HELPER_VERSION, NATIVE_PROTOCOL_VERSION, NativeHandshake
from reproloop.live.iphone import PhysicalIosProvider, TunnelClient
from reproloop.live.model import LiveError
from reproloop.live.native_frame_clock import NativeFrameClock


def handshake():
    return NativeHandshake(NATIVE_PROTOCOL_VERSION, HELPER_VERSION, 'helper-one',
                           'provider-one', 'native-one', 'clock-one', 900, 1000, 1000, 1,
                           'a' * 64, object())


def android_frame(frame_id):
    header = json.dumps({
        'type': 'frame', 'id': frame_id, 'nativeFrameId': frame_id,
        'geometryVersion': 1, 'width': 400, 'height': 800,
        'orientation': 'portrait', 'capturedAt': 1, 'mime': 'image/jpeg',
    }, separators=(',', ':')).encode()
    body = b'jpeg-frame'
    return struct.pack('>II', len(header), len(body)) + header + body


def ios_frame(frame_id):
    return json.dumps({
        'id': f'native-{frame_id}', 'nativeFrameId': frame_id,
        'imageBase64': base64.b64encode(b'jpeg-frame').decode(),
        'mime': 'image/jpeg', 'width': 400, 'height': 800,
        'orientation': 'portrait', 'capturedAt': 1,
    }).encode()


class FrameLab:
    def __init__(self):
        self.calls = []
        self.session = {'frameLock': threading.RLock(), 'frameSequence': 0}

    def _session(self, _sid):
        return self.session

    def publish_frame(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.session['frameSequence'] += 1
        return True


class NativeBufferTransportTests(unittest.TestCase):
    def test_ack_routes_accept_shared_operation_ids_and_legacy_uuids(self):
        for client in (UsbBridgeClient(8766, 'synthetic-test-token'),
                       TunnelClient('fd00::1', 8766, 'synthetic-test-token')):
            for identifier in ('operation_' + 'a' * 40, '0' * 32, 'f' * 32):
                with self.subTest(client=type(client).__name__, identifier=identifier):
                    response = Mock(status=200)
                    response.read.return_value = b'{"pending":true}'
                    with patch('http.client.HTTPConnection') as connection:
                        connection.return_value.getresponse.return_value = response
                        self.assertEqual(client.call('/ack/' + identifier), {'pending': True})
                        self.assertEqual(connection.return_value.request.call_args.args[1], '/ack/' + identifier)
            for path in ('/ack/../status', '/ack/a/b', '/ack/' + 'a' * 65):
                with self.subTest(path=path), patch('http.client.HTTPConnection') as connection:
                    with self.assertRaises(LiveError):client.call(path)
                    connection.assert_not_called()

    def test_clock_binds_optional_buffer_version_and_capacity(self):
        clock = NativeFrameClock(None, None, lambda: None)
        clock.configure(handshake(), {})
        self.assertFalse(clock.buffered_frames)
        with self.assertRaises(LiveError):
            clock.configure(handshake(), {'nativeFrameBufferVersion': 1})

        buffered = NativeFrameClock(None, None, lambda: None)
        buffered.configure(handshake(), {'nativeFrameBufferVersion': 1, 'nativeFrameBufferCapacity': 16})
        self.assertTrue(buffered.buffered_frames)
        with self.assertRaises(LiveError):
            buffered.configure(handshake(), {'nativeFrameBufferVersion': 1, 'nativeFrameBufferCapacity': 15})
        for bad in (True, 0, 2, '1'):
            with self.subTest(bad=bad), self.assertRaises(LiveError):
                NativeFrameClock(None, None, lambda: None).configure(
                    handshake(), {'nativeFrameBufferVersion': bad})

    def test_android_uses_after_cursor_dedupes_latest_duplicate_and_reports_gap(self):
        lab = FrameLab()
        provider = object.__new__(AndroidLiveProvider)
        provider.transport = Mock()
        provider.transport.call.return_value = android_frame(3)
        provider.frame_mutex = threading.RLock()
        provider.frame_clock = None
        provider.native_frame_buffered = True
        provider.native_frame_timing_declared = False
        provider.general_profile = False
        provider.last_native_frame = 1
        provider.lab = lab
        provider.sid = 'session'
        provider.frame_map = OrderedDict()
        provider._receive_frame()
        self.assertEqual(provider.transport.call.call_args.args[0], '/frames/after/1')
        self.assertEqual(lab.calls[0][1]['native_sequence_gap'], (2, 2))
        provider._receive_frame()
        self.assertEqual(len(lab.calls), 1)
        provider.last_native_frame = 0
        provider._receive_frame()
        self.assertEqual(lab.calls[-1][1].get('native_sequence_gap'), (1, 2))

    def test_android_legacy_fallback_and_timed_identity_mismatch_reject(self):
        lab = FrameLab()
        provider = object.__new__(AndroidLiveProvider)
        provider.transport = Mock()
        provider.transport.call.return_value = android_frame(1)
        provider.frame_mutex = threading.RLock()
        provider.frame_clock = None
        provider.native_frame_buffered = False
        provider.native_frame_timing_declared = False
        provider.general_profile = False
        provider.last_native_frame = 0
        provider.lab = lab
        provider.sid = 'session'
        provider.frame_map = OrderedDict()
        provider._receive_frame()
        self.assertEqual(provider.transport.call.call_args.args[0], '/frame')

        bad = android_frame(2).replace(b'\"nativeFrameId\":2', b'\"nativeFrameId\":9')
        provider.transport.call.return_value = bad
        provider.native_frame_buffered = True
        provider.native_frame_timing_declared = True
        with self.assertRaises(LiveError):
            provider._receive_frame()

    def test_ios_uses_buffered_json_route_and_legacy_route(self):
        for buffered, expected_path in ((True, '/frames/after/0'), (False, '/frame')):
            with self.subTest(buffered=buffered):
                lab = FrameLab()
                provider = object.__new__(PhysicalIosProvider)
                provider.transport = Mock()
                provider.transport.call.return_value = ios_frame(1) if buffered else {
                    'id': 'native-1', 'nativeFrameId': 1,
                    'imageBase64': base64.b64encode(b'jpeg-frame').decode(),
                    'mime': 'image/jpeg', 'width': 400, 'height': 800,
                    'orientation': 'portrait', 'capturedAt': 1,
                }
                provider.profile = None
                provider.frame_mutex = threading.RLock()
                provider.frame_clock = None
                provider.native_frame_buffered = buffered
                provider.last_native_frame = 0
                provider.lab = lab
                provider.sid = 'session'
                provider._receive_frame()
                self.assertEqual(provider.transport.call.call_args.args[0], expected_path)
                if buffered:
                    provider.last_native_frame = 0
                    provider.transport.call.return_value = ios_frame(3)
                    provider._receive_frame()
                    self.assertEqual(lab.calls[-1][1].get('native_sequence_gap'), (1, 2))


if __name__ == '__main__':
    unittest.main()
