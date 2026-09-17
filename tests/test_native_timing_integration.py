"""Actual host bridge/recorder integration using explicit native wire doubles."""
import base64
from collections import OrderedDict
import http.client
import json
import struct
import threading
import unittest

from reproloop.live.android_live import AndroidLiveProvider
from reproloop.live.authority import HELPER_VERSION, NATIVE_PROTOCOL_VERSION, NativeHandshake
from reproloop.live.iphone import PhysicalIosProvider
from reproloop.live.model import LiveError
from reproloop.live.native_frame_clock import NativeFrameClock
from reproloop.live.providers import IosProvider
from reproloop.live.server import LiveServer
from tests import test_g2_lab_integration as lab_fixture


class NativeTimingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.env = lab_fixture.ReleaseLabTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.session = self.env.create(self.env.register())
        self.lab = self.env.lab
        self.sync = self.lab._recording_store.clock_sync
        self.anchor = self.lab._session(self.session['id'])['releaseRecorder'].anchor
        # Wire I/O is a double. Clock translation, bridge parsing, collection,
        # durable original, and HTTP authorization are production code.
        self.handshake = NativeHandshake(NATIVE_PROTOCOL_VERSION, HELPER_VERSION,
            'helper-one', 'provider-one', 'native-one', 'ios-mach-continuous',
            900, self.env.clock.nanoseconds, 1000, 1, 'a' * 64, object())
        self.clock = NativeFrameClock(self.sync, self.anchor, lambda: None)
        self.clock.configure(self.handshake, {'nativeFrameTimingVersion': 1})

    def body(self, sequence=2, start=950, end=1100):
        return {'nativeFrameId': sequence, 'imageBase64': base64.b64encode(b'wire-frame-double').decode(),
                'mime': 'image/jpeg', 'width': 400, 'height': 800,
                'orientation': 'portrait', 'capturedAt': 1,
                'nativeTiming': {'version': 1, 'nativeClockId': 'ios-mach-continuous',
                    'nativeIncarnation': 'native-one', 'captureStartMs': start,
                    'captureEndMs': end}}

    def simulator(self):
        provider = IosProvider.__new__(IosProvider)
        provider.lab = self.lab
        provider.sid = self.session['id']
        provider.profile = None
        provider.frame_clock = self.clock
        provider.native_handshake = self.handshake
        provider.handshake_ready = threading.Event()
        provider.handshake_ready.set()
        return provider

    def map_clock(self):
        start = self.clock.begin_exchange({'nativeClockId': 'ios-mach-continuous',
            'nativeIncarnation': 'native-one', 'nativeSendMs': 900})
        self.env.clock.advance(20_000_000)
        self.clock.finish_exchange({'exchangeId': start['exchangeId'], 'nativeReceiveMs': 920})
        self.env.clock.advance(2_000_000_000)

    def test_all_native_bridges_preserve_measured_capture_interval_in_original(self):
        self.map_clock()
        providers = [self.simulator(), AndroidLiveProvider.__new__(AndroidLiveProvider),
                     PhysicalIosProvider.__new__(PhysicalIosProvider)]
        for index, provider in enumerate(providers):
            sequence = index + 2
            body = self.body(sequence, 950 + index * 200, 1100 + index * 200)
            with self.subTest(provider=type(provider).__name__):
                if type(provider) is IosProvider:
                    provider.bridge('frame', body)
                else:
                    provider.lab = self.lab
                    provider.sid = self.session['id']
                    provider.frame_clock = self.clock
                    provider.frame_mutex = threading.RLock()
                    provider.last_native_frame = 0
                    provider.profile = None
                    provider.general_profile = False
                    provider.frame_map = OrderedDict()
                    encoded = body
                    if type(provider) is AndroidLiveProvider:
                        metadata = dict(body, type='frame', id=sequence, geometryVersion=1)
                        image = base64.b64decode(metadata.pop('imageBase64'))
                        header = json.dumps(metadata).encode()
                        encoded = struct.pack('>II', len(header), len(image)) + header + image
                    class Transport:
                        def call(self, *args, **kwargs): return encoded
                    provider.transport = Transport()
                    provider._receive_frame()
                frame = self.lab.frame(self.session['id'])
                self.assertEqual(frame['captureTimingSource'], 'provider-mapped')
                self.assertLess(frame['presentationOffsetMs'], 1000)
                self.assertNotEqual(frame['capturedAt'], 1)
        timeline = self.lab._recording_store.media_timeline(self.session['releaseRecordingId'])
        for index, item in enumerate(timeline[1:]):
            timing = item['timing']
            self.assertEqual(timing['providerCaptureStartNs'], (950 + index * 200) * 1_000_000)
            self.assertGreaterEqual(timing['latestOffsetMs'] - timing['earliestOffsetMs'], 150)
        frozen = self.lab.stop_release_recording(self.session['id'], 'owner',
            self.session['controllerId'], self.session['epoch'])
        self.assertEqual(frozen['status'], 'frozen-complete')
        self.assertNotIn('native_timing_unknown',
                         {item['reason'] for item in frozen['original']['interruptions']})

    def test_invalid_native_timing_never_falls_back_to_host_publication_time(self):
        self.map_clock()
        provider = self.simulator()
        body = self.body()
        body['nativeTiming']['nativeIncarnation'] = 'different-helper'
        before = len(self.lab._recording_store.media_timeline(self.session['releaseRecordingId']))
        with self.assertRaises(LiveError):
            provider.bridge('frame', body)
        self.assertEqual(before, len(self.lab._recording_store.media_timeline(self.session['releaseRecordingId'])))

    def test_clock_exchange_uses_authenticated_native_http_and_durable_frame_path(self):
        provider = self.simulator()
        # Keep the owned double's lifecycle while exercising actual IosProvider.bridge.
        self.env.provider.token = 'synthetic-native-http-token'
        self.env.provider.bridge = provider.bridge
        server = LiveServer(self.lab)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.close_operations()
            server.shutdown()
            thread.join(5)
            server.server_close()
        self.addCleanup(cleanup)
        def request(operation, body, *, authorized=True, origin=None, method='POST'):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            headers = {'Content-Type': 'application/json'}
            if authorized: headers['Authorization'] = 'Bearer synthetic-native-http-token'
            if origin: headers['Origin'] = origin
            connection.request(method, '/bridge/' + self.session['id'] + '/' + operation,
                               json.dumps(body), headers)
            response = connection.getresponse()
            status, value = response.status, json.loads(response.read())
            connection.close()
            return status, value
        send = {'nativeClockId': 'ios-mach-continuous', 'nativeIncarnation': 'native-one', 'nativeSendMs': 900}
        self.assertEqual(request('clock-start', send, authorized=False)[0], 401)
        self.assertEqual(request('clock-start', send, origin=server.origin)[0], 403)
        self.assertEqual(request('clock-start', send, method='GET')[0], 405)
        status, start = request('clock-start', send)
        self.assertEqual(status, 200)
        self.env.clock.advance(20_000_000)
        self.assertEqual(request('clock-end', {'exchangeId': start['exchangeId'], 'nativeReceiveMs': 920})[0], 200)
        self.env.clock.advance(2_000_000_000)
        self.assertEqual(request('frame', self.body())[0], 200)
        self.assertEqual(self.lab.frame(self.session['id'])['captureTimingSource'], 'provider-mapped')
        self.assertEqual(request('clock-end', {'exchangeId': start['exchangeId'], 'nativeReceiveMs': 920})[0], 409)
