"""Independent parent probes for G3 protocol boundaries, not codec acceptance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reproof.live.clock_sync import RecordingStamp
from reproof.live.recording_session import FramePublication
from reproof.live.video import (
    EncoderFrame,
    VideoLimits,
    VideoProtocolError,
    parse_helper_output,
)


def line(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode() + b"\n"


class ParentVideoProtocolBoundaries(unittest.TestCase):
    def setUp(self):
        body = b"parent-protocol-input"
        self.frames = tuple(
            EncoderFrame.from_publication(FramePublication(
                digest=hashlib.sha256(body).hexdigest(), bytes=len(body),
                path="objects/sha256/aa/source", mime_type="image/png",
                width=96, height=160, orientation="portrait",
                acquisition_sequence=sequence,
                stamp=RecordingStamp(
                    display_ms=5_000_000 + offset, offset_ms=offset,
                    earliest_offset_ms=offset, latest_offset_ms=offset,
                    uncertainty_ns=0),
                timing_source="host-acquired"))
            for sequence, offset in ((11, 100), (12, 275))
        )
        self.accepted = [
            {"schemaVersion": 1, "type": "accepted",
             "acquisitionSequence": sequence, "ptsNs": pts}
            for sequence, pts in ((11, 0), (12, 175_000_000))
        ]
        self.finished = {
            "schemaVersion": 1, "type": "finished", "codec": "h264",
            "container": "mp4", "frames": 2, "bytes": 1234,
            "sha256": "a" * 64,
        }

    def parse(self, records):
        return parse_helper_output(b"".join(map(line, records)), self.frames,
                                   max_bytes=4096)

    def test_exact_acknowledgements_parse(self):
        result = self.parse([*self.accepted, self.finished])
        self.assertEqual(result.accepted_sequences, (11, 12))

    def test_duplicate_json_keys_are_rejected(self):
        first = line(self.accepted[0]).replace(
            b'"schemaVersion":1', b'"schemaVersion":2,"schemaVersion":1')
        output = first + line(self.accepted[1]) + line(self.finished)
        with self.assertRaises(VideoProtocolError):
            parse_helper_output(output, self.frames, max_bytes=4096)

    def test_non_integer_wire_numerics_are_rejected(self):
        for index, key in ((0, "schemaVersion"), (0, "acquisitionSequence"),
                           (0, "ptsNs"), (2, "schemaVersion"),
                           (2, "frames"), (2, "bytes")):
            for value in (True, False, 1.0, None, "1"):
                records = [dict(x) for x in (*self.accepted, self.finished)]
                records[index][key] = value
                with self.subTest(index=index, key=key, value=value):
                    with self.assertRaises(VideoProtocolError):
                        self.parse(records)

    def test_acknowledged_pts_must_match_admitted_source_timing(self):
        for index, pts in ((0, 1), (1, 1), (1, 175_000_001)):
            records = [dict(x) for x in (*self.accepted, self.finished)]
            records[index]["ptsNs"] = pts
            with self.subTest(index=index, pts=pts):
                with self.assertRaises(VideoProtocolError):
                    self.parse(records)

    def test_acknowledgement_set_and_order_are_exact(self):
        alternatives = (
            [self.accepted[0], self.finished],
            [self.accepted[1], self.accepted[0], self.finished],
            [self.accepted[0], self.accepted[0], self.finished],
            [self.finished, *self.accepted],
            [*self.accepted, self.finished, self.finished],
            [*self.accepted],
        )
        for records in alternatives:
            with self.subTest(records=records):
                with self.assertRaises(VideoProtocolError):
                    self.parse(records)

    def test_finished_receipt_has_exact_required_fields(self):
        changes = (
            {"codec": "hevc"}, {"container": "mov"}, {"frames": 1},
            {"bytes": 0}, {"bytes": -1}, {"sha256": "A" * 64},
            {"sha256": "a" * 63}, {"outputPath": "/tmp/other.mp4"},
        )
        for changed in changes:
            with self.subTest(changed=changed):
                with self.assertRaises(VideoProtocolError):
                    self.parse([*self.accepted, dict(self.finished, **changed)])

    def test_unknown_acknowledgement_fields_are_rejected(self):
        changed = dict(self.accepted[0], durable=True)
        with self.assertRaises(VideoProtocolError):
            self.parse([changed, self.accepted[1], self.finished])

    def test_policy_numeric_bounds_never_accept_boolean_values(self):
        fields = (
            "segment_duration_ms", "max_frame_bytes", "max_decoded_pixels",
            "max_queue_frames", "max_queue_bytes", "max_frames_per_segment",
            "max_segments", "max_segment_bytes", "max_total_video_bytes",
            "max_helper_output_bytes", "max_helper_error_bytes",
            "max_active_finalizers", "finalization_timeout_seconds",
        )
        for field in fields:
            with self.subTest(field=field):
                with self.assertRaises(VideoProtocolError):
                    VideoLimits(**{field: True})


if __name__ == "__main__":
    unittest.main()
