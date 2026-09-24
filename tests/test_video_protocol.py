import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from reproof.live.clock_sync import RecordingStamp
from reproof.live.recording_session import FramePublication
from reproof.live.video import (
    PROTOCOL_MAGIC,
    EncoderFrame,
    VideoLimits,
    VideoProtocolError,
    encode_configuration,
    encode_finish,
    encode_frame,
    parse_helper_output,
    validate_video_manifest,
)


class VideoProtocolTests(unittest.TestCase):
    def setUp(self):
        self.body = b"\x89PNG\r\n\x1a\nsynthetic"
        stamp = RecordingStamp(
            display_ms=5_000_010,
            offset_ms=10,
            earliest_offset_ms=9,
            latest_offset_ms=11,
            uncertainty_ns=2_000_000,
        )
        self.publication = FramePublication(
            digest=hashlib.sha256(self.body).hexdigest(),
            bytes=len(self.body),
            path="objects/sha256/aa/source",
            mime_type="image/png",
            width=96,
            height=160,
            orientation="portrait",
            acquisition_sequence=7,
            stamp=stamp,
            timing_source="host-acquired",
        )
        self.frame = EncoderFrame.from_publication(
            self.publication,
            provider_incarnation="provider_one",
            native_incarnation="native_one",
        )

    def test_limits_are_strict_and_bounded(self):
        limits = VideoLimits(max_frame_bytes=1024, max_queue_bytes=4096)
        self.assertEqual(limits.max_frame_bytes, 1024)
        for kwargs in (
            {"max_frame_bytes": True},
            {"max_queue_frames": 0},
            {"max_decoded_pixels": 100_000_000},
            {"max_segment_bytes": 65 * 1024 * 1024},
            {"finalization_timeout_seconds": 31},
            {"max_active_finalizers": 3},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(VideoProtocolError):
                VideoLimits(**kwargs)

    def test_configuration_is_versioned_and_has_exact_bounded_shape(self):
        wire = encode_configuration(
            width=96,
            height=160,
            limits=VideoLimits(max_frame_bytes=1024, max_queue_bytes=4096),
        )
        self.assertEqual(wire[:8], PROTOCOL_MAGIC)
        size = struct.unpack(">I", wire[8:12])[0]
        body = json.loads(wire[12:12 + size])
        self.assertEqual(body["schemaVersion"], 1)
        self.assertEqual(body["codec"], "h264")
        self.assertEqual(body["container"], "mp4")
        self.assertEqual(set(body), {
            "schemaVersion", "codec", "container", "width", "height",
            "maxFrames", "maxCompressedBytes", "maxDecodedPixels",
            "maxOutputBytes", "faultMode",
        })

    def test_frame_record_binds_digest_dimensions_lineage_and_interval(self):
        wire = encode_frame(
            self.frame,
            self.body,
            segment_first_offset_ms=10,
            limits=VideoLimits(max_frame_bytes=1024, max_queue_bytes=4096),
        )
        self.assertEqual(wire[0:1], b"F")
        metadata_size, body_size = struct.unpack(">II", wire[1:9])
        metadata = json.loads(wire[9:9 + metadata_size])
        self.assertEqual(body_size, len(self.body))
        self.assertEqual(metadata["digest"], self.publication.digest)
        self.assertEqual(metadata["ptsNs"], 0)
        self.assertEqual(metadata["earliestRecordingOffsetMs"], 9)
        self.assertEqual(metadata["latestRecordingOffsetMs"], 11)
        self.assertEqual(metadata["timingSource"], "host-acquired")
        self.assertEqual(metadata["providerIncarnation"], "provider_one")
        self.assertEqual(metadata["nativeIncarnation"], "native_one")

    def test_native_unmapped_is_never_serialized_as_capture_interval(self):
        publication = FramePublication(
            **{name: getattr(self.publication, name) for name in (
                "digest", "bytes", "path", "mime_type", "width", "height",
                "orientation", "acquisition_sequence", "stamp")},
            timing_source="native-unmapped",
        )
        frame = EncoderFrame.from_publication(
            publication,
            provider_incarnation="provider_one",
            native_incarnation="native_one",
        )
        wire = encode_frame(
            frame,
            self.body,
            segment_first_offset_ms=10,
            limits=VideoLimits(max_frame_bytes=1024, max_queue_bytes=4096),
        )
        metadata_size = struct.unpack(">I", wire[1:5])[0]
        metadata = json.loads(wire[9:9 + metadata_size])
        self.assertEqual(metadata["timingSource"], "native-unmapped")
        self.assertNotIn("earliestRecordingOffsetMs", metadata)
        self.assertNotIn("latestRecordingOffsetMs", metadata)
        self.assertEqual(metadata["ptsRelation"], "display-publication-order-only")

    def test_frame_rejects_mismatch_backward_pts_and_excess_bytes(self):
        limits = VideoLimits(max_frame_bytes=len(self.body), max_queue_bytes=4096)
        with self.assertRaises(VideoProtocolError):
            encode_frame(self.frame, self.body + b"x", 10, limits)
        with self.assertRaises(VideoProtocolError):
            encode_frame(self.frame, self.body, 11, limits)
        altered = EncoderFrame.from_publication(
            self.publication, width=95,
            provider_incarnation="provider_one",
            native_incarnation="native_one",
        )
        with self.assertRaises(VideoProtocolError):
            encode_frame(altered, self.body, 10, limits)

    def test_finish_record_is_fixed(self):
        self.assertEqual(encode_finish(), b"E\x00\x00\x00\x00\x00\x00\x00\x00")

    def test_helper_output_is_bounded_exact_and_monotonic(self):
        output = (
            b'{"schemaVersion":1,"type":"accepted","acquisitionSequence":7,"ptsNs":0}\n'
            b'{"schemaVersion":1,"type":"finished","codec":"h264","container":"mp4",'
            b'"frames":1,"bytes":123,"sha256":"' + b"a" * 64 + b'"}\n'
        )
        parsed = parse_helper_output(output, (self.frame,), max_bytes=4096)
        self.assertEqual(parsed.accepted_sequences, (7,))
        self.assertEqual(parsed.bytes, 123)
        with self.assertRaises(VideoProtocolError):
            parse_helper_output(output + b"{}\n", (self.frame,), max_bytes=4096)
        with self.assertRaises(VideoProtocolError):
            parse_helper_output(output, (self.frame,), max_bytes=32)

    def test_manifest_validator_rejects_unknown_fields_and_false_completeness(self):
        base = {
            "schemaVersion": 1,
            "kind": "reproof-avfoundation-video",
            "recordingId": "recording_one",
            "status": "incomplete",
            "failureReason": "no-video-frames",
            "codec": "h264",
            "container": "mp4",
            "samplingMode": "no-acquired-frames",
            "segmentDurationBoundMs": 1000,
            "segments": [],
            "losses": [{
                "lossClass": "not-acquired",
                "stage": "native",
                "reason": "zero-frames",
                "recordingInterval": {"startOffsetMs": 0, "endOffsetMs": 10},
            }],
            "eventMappings": [],
            "limits": {
                "maxFrameBytes": 1024,
                "maxDecodedPixels": 1_000_000,
                "maxQueueFrames": 4,
                "maxQueueBytes": 4096,
                "maxFramesPerSegment": 16,
                "maxSegments": 8,
                "maxSegmentBytes": 1024 * 1024,
                "maxTotalVideoBytes": 4 * 1024 * 1024,
                "maxActiveFinalizers": 1,
                "maxProcessFinalizers": 2,
                "finalizationTimeoutSeconds": 1,
            },
        }
        self.assertEqual(validate_video_manifest(base), base)
        altered = json.loads(json.dumps(base))
        altered["unknown"] = True
        with self.assertRaises(VideoProtocolError):
            validate_video_manifest(altered)
        altered = json.loads(json.dumps(base))
        altered["status"] = "complete"
        altered["failureReason"] = None
        with self.assertRaises(VideoProtocolError):
            validate_video_manifest(altered)
        altered = json.loads(json.dumps(base))
        altered["limits"]["maxQueueFrames"] = True
        with self.assertRaises(VideoProtocolError):
            validate_video_manifest(altered)


if __name__ == "__main__":
    unittest.main()
