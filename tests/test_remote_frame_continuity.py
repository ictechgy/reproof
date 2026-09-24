"""Latest-frame worker polling must preserve gaps in the real source ledger."""
import threading
import unittest

from reproof.live.worker import RemoteProvider, WorkerClient
from tests import test_video_spool_recording as recording_fixture


class RemoteFrameContinuityTests(unittest.TestCase):
    def exercise(self, rows):
        fixture = recording_fixture.VideoSpoolRecordingTests(methodName='runTest')
        fixture.setUp(); self.addCleanup(fixture.tearDown)
        recording = fixture.start()
        received = []; failures = []
        state = {'frameLock': threading.RLock(), 'frame': None}
        class Lab:
            def _session(self, identifier):
                return state

            def publish_frame(self, sid, image, mime, width, height, orientation, captured_at, **kwargs):
                publication = recording.record_frame(image, mime, width, height, orientation,
                    acquisition_sequence=kwargs['acquisition_sequence'], native_incarnation='native_unmapped',
                    timing_source=kwargs['timing_source'], native_sequence_gap=kwargs.get('native_sequence_gap'))
                if publication is None:
                    return False
                received.append(kwargs['acquisition_sequence'])
                state['frame'] = {'id': len(received), 'geometryVersion': 1}
                if len(received) == len(rows):
                    provider.stop.set()
                fixture.clock.advance(100_000_000)
                with fixture.sink._condition:
                    fixture.sink._condition.wait_for(
                        lambda: not fixture.sink._queue and not fixture.sink._finalizer_queue, timeout=2)
                return True

            def fail(self, identifier, message):
                failures.append(message)

        class Client(WorkerClient):
            def __init__(self):
                self.index = 0

            def call(self, path, body=None, **kwargs):
                return {'session': {'controllerId': 'remote-controller', 'epoch': 1, 'state': 'active'}}

            def frame(self, identifier):
                frame_id, sequence = rows[self.index]
                self.index += 1
                metadata = {'id': frame_id, 'mime': 'image/png', 'width': 96, 'height': 160,
                            'orientation': 'portrait', 'geometryVersion': 1, 'capturedAt': 1}
                if sequence is not None:
                    metadata['acquisitionSequence'] = sequence
                return metadata, b'explicit image bytes for the codec double'

        provider = RemoteProvider(Client(), 'remote-device')
        provider.remote_session_id = 'remote-session'
        provider.remote_controller = 'remote-controller'; provider.remote_epoch = 1
        provider.parent_sid = 'parent-session'; provider.lab = Lab()
        provider._poll()
        result = recording.stop()
        return received, failures, result, fixture.sink.manifest()

    def test_skipped_worker_frames_continue_recording_with_exact_transport_loss(self):
        received, failures, result, video = self.exercise([(1, None), (3, None), (7, None)])
        self.assertEqual(received, [1, 3, 7])
        self.assertEqual(failures, [])
        self.assertEqual([row['acquisitionSequence'] for row in video['sourceFrames']], [1, 3, 7])
        gaps = [row['nativeSequenceRange'] for row in video['losses'] if 'nativeSequenceRange' in row]
        self.assertEqual(gaps, [{'first': 2, 'last': 2}, {'first': 4, 'last': 6}])
        self.assertEqual(result['status'], 'frozen-incomplete')

    def test_first_and_explicit_acquisition_sequence_gaps_are_not_host_timestamps(self):
        received, failures, _, video = self.exercise([(4, 10), (8, 14)])
        self.assertEqual(received, [10, 14])
        self.assertEqual(failures, [])
        gaps = [row['nativeSequenceRange'] for row in video['losses'] if 'nativeSequenceRange' in row]
        self.assertEqual(gaps, [{'first': 1, 'last': 9}, {'first': 11, 'last': 13}])
        self.assertTrue(all(row['captureInterval'] is None for row in video['segments']))

    def test_new_worker_frame_cannot_reuse_or_corrupt_the_acquisition_sequence(self):
        for sequence in (1, True, -1):
            with self.subTest(sequence=sequence):
                received, failures, _, video = self.exercise([(1, 1), (2, sequence)])
                self.assertEqual(received, [1])
                self.assertEqual(len(failures), 1)
                self.assertEqual(len(video['sourceFrames']), 1)


if __name__ == '__main__':
    unittest.main()
