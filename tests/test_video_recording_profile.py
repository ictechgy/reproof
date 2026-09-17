import json
import struct
import unittest

from reproloop.live.video import VideoLimits, encode_configuration


class VideoRecordingProfileTests(unittest.TestCase):
    def test_profile_covers_ten_minute_capture_without_raw_image_archives(self):
        limits = VideoLimits.recording_profile()
        self.assertGreaterEqual(limits.max_segments * limits.max_frames_per_segment, 10_000)
        self.assertGreaterEqual(limits.max_total_video_bytes, 600 * limits.target_bitrate // 8)
        self.assertLessEqual(3 * limits.max_compressed_segment_bytes + limits.max_queue_bytes,
                             64 * 1024 * 1024)
        self.assertEqual(limits.segment_duration_ms, 10_000)

    def test_recording_profile_uses_explicit_v2_compression_configuration(self):
        limits = VideoLimits.recording_profile()
        wire = encode_configuration(width=960, height=2134, limits=limits)
        size = struct.unpack('>I', wire[8:12])[0]
        value = json.loads(wire[12:12 + size])
        self.assertEqual(value['schemaVersion'], 2)
        self.assertEqual(value['targetBitrate'], 1_200_000)
        self.assertEqual(value['maxKeyFrameInterval'], 30)
        self.assertEqual(value['maxCompressedBytes'], limits.max_compressed_segment_bytes)

    def test_legacy_configuration_retains_its_original_shape(self):
        wire = encode_configuration(width=96, height=160, limits=VideoLimits())
        value = json.loads(wire[12:])
        self.assertEqual(value['schemaVersion'], 1)
        self.assertNotIn('targetBitrate', value)
        self.assertNotIn('maxKeyFrameInterval', value)


if __name__ == '__main__':
    unittest.main()
