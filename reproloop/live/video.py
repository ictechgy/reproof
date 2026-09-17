"""Bounded AVFoundation video encoding over G2-authorized frame objects.

The wire protocol carries bytes, dimensions and timing facts, never commands,
URLs, or source paths.  A segment is evidence only after the helper output is
validated and atomically published through :class:`EvidenceStore`.
"""
from __future__ import annotations

from collections import deque
import copy
from dataclasses import dataclass, field
from functools import wraps
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import struct
import subprocess
import threading
import time
from typing import Iterable

from ..core import ContractError
from ..storage import _unique_object
from .disk_budget import DiskBudgetError, DiskReservation
from .evidence_store import (
    EvidencePin,
    EvidenceStore,
    EvidenceStoreError,
    OBJECT_METADATA_BYTES,
)
from .recording_session import FramePublication, ORIGINAL_FRAME_MODE, VIDEO_SOURCE_MODE
from .video_sources import validate_source_coverage, MAX_SOURCE_FRAMES


PROTOCOL_MAGIC = b"RLVID001"
PROTOCOL_VERSION = 1
MAX_CONFIG_BYTES = 4096
MAX_FRAME_METADATA_BYTES = 4096
MAX_COMPRESSED_SEGMENT_BYTES = 64 * 1024 * 1024
MAX_VIDEO_PIXELS = 4_194_304
MAX_HELPER_STREAM_BYTES = 64 * 1024
VIDEO_JOURNAL_RESERVATION_BYTES = 8 * 1024 * 1024
VIDEO_CATALOG_RESERVATION_BYTES = 256 * 1024
MAX_PROCESS_FINALIZERS = 2
MAX_VIDEO_LOSSES = 512
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_MIME_TYPES = frozenset({"image/png", "image/jpeg"})
VIDEO_MANIFEST_MIME = "application/vnd.reproloop.video-manifest+json"
MAX_VIDEO_MANIFEST_BYTES = 8 * 1024 * 1024
_TIMING_SOURCES = frozenset(
    {"host-acquired", "provider-mapped", "native-unmapped"}
)
_FAULT_MODES = frozenset(
    {"none", "stall-finalization", "stall-after-first-accept", "fail-write"}
)
_LOSS_STAGES = {
    "native": "not-acquired",
    "transport": "captured-dropped",
    "queue": "captured-dropped",
    "encoder": "encoder-accepted-not-durable",
}


class VideoProtocolError(ContractError):
    """Video input or helper evidence violates a bounded contract."""


class VideoEncodingError(VideoProtocolError):
    def __init__(self, reason: str, *, accepted_sequences=()):
        super().__init__(reason)
        self.reason = reason
        self.accepted_sequences = tuple(accepted_sequences)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VideoProtocolError(message)


def _integer(value: object, name: str, low: int, high: int) -> int:
    _require(type(value) is int and low <= value <= high, f"Invalid {name}")
    return value


def _identifier(value: object, name: str) -> str:
    _require(type(value) is str and _ID.fullmatch(value) is not None,
             f"Invalid {name}")
    return value


def _state_locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._condition:
            return method(self, *args, **kwargs)
    return call


def _json_bytes(value: object, maximum: int, message: str) -> bytes:
    try:
        body = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise VideoProtocolError(message) from None
    _require(len(body) <= maximum, message)
    return body


def _owned_directory(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    stat = path.lstat()
    _require(path.is_dir() and not path.is_symlink() and stat.st_uid == os.getuid(),
             "Invalid video directory")
    return path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prebinding_source_root(video_root: Path, recording_id: str) -> Path:
    return Path(video_root) / "sources" / hashlib.sha256(
        recording_id.encode("ascii")
    ).hexdigest()


def _prebinding_work_root(video_root: Path, recording_id: str) -> Path:
    return Path(video_root) / "work" / hashlib.sha256(
        recording_id.encode("ascii")
    ).hexdigest()


def _source_spool_bounds(limits: VideoLimits) -> tuple[int, int]:
    maximum_input = min(
        limits.max_compressed_segment_bytes,
        limits.max_frames_per_segment * limits.max_frame_bytes)
    return (3 * limits.max_frames_per_segment + limits.max_queue_frames,
            3 * maximum_input + limits.max_queue_bytes)


def _validate_prebinding_source_config(video_root: Path, row) -> dict:
    _require(row["source_mode"] == VIDEO_SOURCE_MODE,
             "Prebinding video source mode changed")
    try:
        limits = json.loads(row["limits_json"])
        source_config = json.loads(row["source_config_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        raise VideoProtocolError("Prebinding source configuration is invalid") from None
    expected = {
        "limits": limits,
        "retainUntilMs": row["retain_until_ms"],
        "sourceRoot": str(_prebinding_source_root(
            video_root, row["recording_id"])),
    }
    _require(type(source_config) is dict and source_config == expected
             and row["source_config_json"] == _json_bytes(
                 expected, MAX_FRAME_METADATA_BYTES,
                 "Video source configuration is too large").decode("utf-8"),
             "Prebinding source configuration changed")
    return source_config


def _retire_empty_prebinding_source(video_root: Path, evidence: EvidenceStore,
                                    recording_id: str, spool=None, *,
                                    max_frames=None, max_bytes=None) -> None:
    """Retire only a proven-empty source rooted at the derived recording path."""
    from .frame_spool import FrameSpool

    source_root = _prebinding_source_root(video_root, recording_id)
    supplied = spool is not None
    if supplied:
        _require(type(spool) is FrameSpool and spool.root == source_root
                 and spool.recording_id == recording_id
                 and spool.budget is evidence.budget,
                 "Prebinding source spool identity changed")
    elif os.path.lexists(source_root / "spool.sqlite3"):
        database = source_root / "spool.sqlite3"
        _require(database.is_file() and not database.is_symlink(),
                 "Prebinding source database is invalid")
        spool = FrameSpool.open_existing(
            source_root, evidence.budget, recording_id)
    elif os.path.lexists(source_root):
        _require(type(max_frames) is int and type(max_bytes) is int,
                 "Historical prebinding source bounds are unavailable")
        FrameSpool.retire_uninitialized(
            source_root, evidence.budget, recording_id,
            max_frames=max_frames, max_bytes=max_bytes)
        return
    else:
        _require(type(max_frames) is int and type(max_bytes) is int,
                 "Historical prebinding source bounds are unavailable")
        FrameSpool.retire_uninitialized(
            source_root, evidence.budget, recording_id,
            max_frames=max_frames, max_bytes=max_bytes)
        parent = source_root.parent
        _fsync_directory(parent if parent.exists() else Path(video_root))
        return

    try:
        snapshot = spool.snapshot()
        _require(snapshot["activeCount"] == 0
                 and snapshot["stagingCount"] == 0
                 and snapshot["releasableCount"] == 0
                 and snapshot["acceptedCount"] == 0,
                 "Prebinding source spool contains admitted data")
        spool.finalcleanup()
    finally:
        if not supplied and not spool._closed:
            spool.close()


def _remove_empty_prebinding_work(video_root: Path, recording_id: str) -> None:
    work = _prebinding_work_root(video_root, recording_id)
    work_parent = Path(video_root) / "work"
    if not os.path.lexists(work):
        _fsync_directory(work_parent if work_parent.exists() else Path(video_root))
        return
    info = work.lstat()
    _require(stat.S_ISDIR(info.st_mode) and not work.is_symlink()
             and info.st_uid == os.getuid() and next(work.iterdir(), None) is None,
             "Prebinding work directory is not empty")
    work.rmdir()
    _fsync_directory(work_parent)
    _require(not os.path.lexists(work),
             "Prebinding work cleanup was not confirmed")


@dataclass(frozen=True, slots=True)
class VideoLimits:
    segment_duration_ms: int = 10_000
    max_frame_bytes: int = 3 * 1024 * 1024
    max_decoded_pixels: int = MAX_VIDEO_PIXELS
    max_queue_frames: int = 4
    max_queue_bytes: int = 12 * 1024 * 1024
    max_frames_per_segment: int = 16
    max_segments: int = 32
    max_segment_bytes: int = 32 * 1024 * 1024
    max_total_video_bytes: int = 256 * 1024 * 1024
    max_helper_output_bytes: int = MAX_HELPER_STREAM_BYTES
    max_helper_error_bytes: int = MAX_HELPER_STREAM_BYTES
    max_active_finalizers: int = 1
    finalization_timeout_seconds: int = 10
    max_compressed_segment_bytes: int = MAX_COMPRESSED_SEGMENT_BYTES
    target_bitrate: int | None = None
    max_key_frame_interval: int | None = None

    @classmethod
    def recording_profile(cls):
        return cls(max_frames_per_segment=256, max_segments=128,
            max_compressed_segment_bytes=16 * 1024 * 1024,
            max_total_video_bytes=128 * 1024 * 1024,
            target_bitrate=1_200_000, max_key_frame_interval=30)

    def __post_init__(self):
        _integer(self.segment_duration_ms, "segment duration", 100, 60_000)
        _integer(self.max_frame_bytes, "frame byte limit", 1,
                 3 * 1024 * 1024)
        _integer(self.max_decoded_pixels, "decoded pixel limit", 1,
                 MAX_VIDEO_PIXELS)
        _integer(self.max_queue_frames, "queue frame limit", 1, 16)
        _integer(self.max_queue_bytes, "queue byte limit", 1,
                 MAX_COMPRESSED_SEGMENT_BYTES)
        _require(self.max_queue_bytes >= self.max_frame_bytes,
                 "Queue cannot hold one maximum frame")
        _integer(self.max_frames_per_segment, "segment frame limit", 1, 256)
        _integer(self.max_compressed_segment_bytes, "compressed segment limit",
                 self.max_frame_bytes, MAX_COMPRESSED_SEGMENT_BYTES)
        _integer(self.max_segments, "segment count limit", 1, 128)
        _integer(self.max_segment_bytes, "segment byte limit", 4096,
                 64 * 1024 * 1024)
        _integer(self.max_total_video_bytes, "total video byte limit",
                 self.max_segment_bytes, 1024 * 1024 * 1024)
        _integer(self.max_helper_output_bytes, "helper output limit", 256,
                 MAX_HELPER_STREAM_BYTES)
        _integer(self.max_helper_error_bytes, "helper error limit", 256,
                 MAX_HELPER_STREAM_BYTES)
        _integer(self.max_active_finalizers, "active finalizer limit", 1, 2)
        _integer(self.finalization_timeout_seconds, "finalization timeout", 1, 30)
        _require((self.target_bitrate is None) == (self.max_key_frame_interval is None),
                 "Incomplete video compression profile")
        if self.target_bitrate is not None:
            _integer(self.target_bitrate, "target bitrate", 128_000, 4_000_000)
            _integer(self.max_key_frame_interval, "key frame interval", 1, 60)


@dataclass(frozen=True, slots=True)
class EncoderFrame:
    digest: str
    bytes: int
    path: str
    mime_type: str
    width: int
    height: int
    orientation: str
    acquisition_sequence: int
    offset_ms: int
    earliest_offset_ms: int
    latest_offset_ms: int
    uncertainty_ns: int
    timing_source: str
    provider_clock_id: str | None
    provider_boot_digest: str | None
    provider_incarnation: str | None
    native_incarnation: str | None
    published_width: int = field(repr=False)
    published_height: int = field(repr=False)
    recording_frame_sequence: int | None = None
    source_token: object = field(default=None, repr=False, compare=False)

    @classmethod
    def from_publication(cls, publication: FramePublication, *, width=None,
                         height=None, provider_incarnation=None,
                         native_incarnation=None):
        _require(type(publication) is FramePublication,
                 "Invalid authorized frame publication")
        stamp = publication.stamp
        return cls(
            publication.digest,
            publication.bytes,
            publication.path,
            publication.mime_type,
            publication.width if width is None else width,
            publication.height if height is None else height,
            publication.orientation,
            publication.acquisition_sequence,
            stamp.offset_ms,
            stamp.earliest_offset_ms,
            stamp.latest_offset_ms,
            stamp.uncertainty_ns,
            publication.timing_source,
            stamp.provider_clock_id,
            stamp.provider_boot_digest,
            provider_incarnation,
            (stamp.native_incarnation if native_incarnation is None
             else native_incarnation),
            publication.width,
            publication.height,
            publication.recording_frame_sequence,
            publication.source_token,
        )

    @property
    def geometry(self):
        return self.width, self.height, self.orientation

    @property
    def clock_segment(self):
        if self.timing_source == "provider-mapped":
            return (
                self.timing_source,
                self.provider_clock_id,
                self.provider_boot_digest,
                self.native_incarnation,
            )
        return (self.timing_source,)

    def manifest_value(self, first_offset_ms: int):
        item = {
            "acquisitionSequence": self.acquisition_sequence,
            "sourceDigest": self.digest,
            "presentationTimeMs": self.offset_ms - first_offset_ms,
            "timingSource": self.timing_source,
            "uncertaintyNs": self.uncertainty_ns,
            "providerIncarnation": self.provider_incarnation,
            "nativeIncarnation": self.native_incarnation,
        }
        if self.recording_frame_sequence is not None:
            item["recordingFrameSequence"] = self.recording_frame_sequence
        if self.timing_source == "native-unmapped":
            item.update({
                "displayOffsetMs": self.offset_ms,
                "ptsRelation": "display-publication-order-only",
            })
        else:
            item.update({
                "earliestRecordingOffsetMs": self.earliest_offset_ms,
                "latestRecordingOffsetMs": self.latest_offset_ms,
                "ptsRelation": "recording-elapsed",
            })
        return item


@dataclass(frozen=True, slots=True)
class HelperResult:
    accepted_sequences: tuple[int, ...]
    bytes: int
    digest: str
    codec: str
    container: str


@dataclass(frozen=True, slots=True)
class EncodedSegment:
    path: Path
    accepted_sequences: tuple[int, ...]
    bytes: int
    digest: str
    codec: str
    container: str


@dataclass(frozen=True, slots=True)
class _SealedSegment:
    frames: tuple[EncoderFrame, ...]
    rotation_reason: str
    index: int
    reservation: DiskReservation
    work: Path


def _frame_from_json(value):
    common = {
        "digest", "bytes", "path", "mimeType", "width", "height",
        "orientation", "acquisitionSequence", "offsetMs",
        "earliestOffsetMs", "latestOffsetMs", "uncertaintyNs",
        "timingSource", "providerClockId", "providerBootDigest",
        "providerIncarnation", "nativeIncarnation",
    }
    _require(type(value) is dict and set(value) in (common, common | {"recordingFrameSequence"}),
             "Stored frame lineage is invalid")
    return EncoderFrame(
        value["digest"], value["bytes"], value["path"], value["mimeType"],
        value["width"], value["height"], value["orientation"],
        value["acquisitionSequence"], value["offsetMs"],
        value["earliestOffsetMs"], value["latestOffsetMs"],
        value["uncertaintyNs"], value["timingSource"],
        value["providerClockId"], value["providerBootDigest"],
        value["providerIncarnation"], value["nativeIncarnation"],
        value["width"], value["height"],
        value.get("recordingFrameSequence"),
    )


def _segment_manifest_value(frames, reference, rotation_reason, index):
    frames = tuple(frames)
    precise = all(frame.timing_source != "native-unmapped" for frame in frames)
    capture_interval = None
    if precise:
        capture_interval = {
            "firstCapturedEarliestOffsetMs": frames[0].earliest_offset_ms,
            "firstCapturedLatestOffsetMs": frames[0].latest_offset_ms,
            "lastCapturedEarliestOffsetMs": frames[-1].earliest_offset_ms,
            "lastCapturedLatestOffsetMs": frames[-1].latest_offset_ms,
        }
    return {
        "segmentId": f"video_segment_{index}",
        "segmentIndex": index,
        "state": "durable",
        "digest": reference.digest,
        "bytes": reference.bytes,
        "path": reference.path,
        "mimeType": "video/mp4",
        "codec": "h264",
        "container": "mp4",
        "width": frames[0].width,
        "height": frames[0].height,
        "orientation": frames[0].orientation,
        "frameCount": len(frames),
        "firstCapturedOffsetMs": (frames[0].offset_ms if precise else None),
        "lastCapturedOffsetMs": (frames[-1].offset_ms if precise else None),
        "captureInterval": capture_interval,
        "displayInterval": {
            "startOffsetMs": frames[0].offset_ms,
            "endOffsetMs": frames[-1].offset_ms,
        },
        "timingRelation": (
            "recording-elapsed-segment-local-pts" if precise
            else "display-publication-order-only"
        ),
        "sourceSamplingMode": (
            "irregular-capture-intervals" if precise
            else "native-acquisition-unknown"
        ),
        "rotationReason": rotation_reason,
        "frames": [frame.manifest_value(frames[0].offset_ms) for frame in frames],
    }


def _limits_manifest(limits, *, source_mode=ORIGINAL_FRAME_MODE):
    value = {
        "maxFrameBytes": limits.max_frame_bytes,
        "maxDecodedPixels": limits.max_decoded_pixels,
        "maxQueueFrames": limits.max_queue_frames,
        "maxQueueBytes": limits.max_queue_bytes,
        "maxFramesPerSegment": limits.max_frames_per_segment,
        "maxSegments": limits.max_segments,
        "maxSegmentBytes": limits.max_segment_bytes,
        "maxTotalVideoBytes": limits.max_total_video_bytes,
        "maxActiveFinalizers": limits.max_active_finalizers,
        "maxProcessFinalizers": MAX_PROCESS_FINALIZERS,
        "finalizationTimeoutSeconds": limits.finalization_timeout_seconds,
    }
    if source_mode == VIDEO_SOURCE_MODE:
        value.update(maxCompressedSegmentBytes=limits.max_compressed_segment_bytes,
                     targetBitrate=limits.target_bitrate,
                     maxKeyFrameInterval=limits.max_key_frame_interval)
    return value


def validate_video_manifest(value):
    """Validate the strict, versioned durable G3 manifest."""
    _require(type(value) is dict, "Invalid video manifest")
    transient = type(value.get("schemaVersion")) is int and value["schemaVersion"] == 2
    fields = {
        "schemaVersion", "kind", "recordingId", "status", "failureReason",
        "codec", "container", "samplingMode", "segmentDurationBoundMs",
        "segments", "losses", "eventMappings", "limits",
    }
    _require(set(value) == fields | ({"sourceDisposition", "sourceFrames"} if transient else set()),
             "Invalid video manifest")
    _require(type(value["schemaVersion"]) is int
             and value["schemaVersion"] in {1, 2},
             "Unsupported video manifest")
    _require(value["kind"] == "reproloop-avfoundation-video"
             and value["codec"] == "h264" and value["container"] == "mp4",
             "Invalid video manifest codec")
    _identifier(value["recordingId"], "video recording identity")
    _require(value["status"] in {"complete", "incomplete"},
             "Invalid video manifest status")
    failure = value["failureReason"]
    _require(failure is None or (type(failure) is str
                                 and _ID.fullmatch(failure) is not None),
             "Invalid video failure reason")
    _require(value["samplingMode"] in {
        "no-acquired-frames", "irregular-source-capture"
    }, "Invalid video sampling mode")
    bound = _integer(value["segmentDurationBoundMs"],
                     "segment duration bound", 100, 60_000)
    limits_value = value["limits"]
    limit_fields = {
        "maxFrameBytes", "maxDecodedPixels", "maxQueueFrames",
        "maxQueueBytes", "maxFramesPerSegment", "maxSegments",
        "maxSegmentBytes", "maxTotalVideoBytes", "maxActiveFinalizers",
        "maxProcessFinalizers", "finalizationTimeoutSeconds",
    }
    _require(type(limits_value) is dict and set(limits_value) == limit_fields | (
        {"maxCompressedSegmentBytes", "targetBitrate", "maxKeyFrameInterval"} if transient else set()),
        "Invalid video manifest limits")
    if not transient:
        _require(type(limits_value["maxSegments"]) is int and limits_value["maxSegments"] <= 32
                 and type(limits_value["maxFramesPerSegment"]) is int
                 and type(limits_value["maxFrameBytes"]) is int
                 and limits_value["maxFramesPerSegment"] * limits_value["maxFrameBytes"]
                 <= MAX_COMPRESSED_SEGMENT_BYTES, "Legacy video limits changed")
    _require(limits_value["maxProcessFinalizers"] == MAX_PROCESS_FINALIZERS,
             "Invalid process finalizer limit")
    limits = VideoLimits(
        segment_duration_ms=bound,
        max_frame_bytes=limits_value["maxFrameBytes"],
        max_decoded_pixels=limits_value["maxDecodedPixels"],
        max_queue_frames=limits_value["maxQueueFrames"],
        max_queue_bytes=limits_value["maxQueueBytes"],
        max_frames_per_segment=limits_value["maxFramesPerSegment"],
        max_segments=limits_value["maxSegments"],
        max_segment_bytes=limits_value["maxSegmentBytes"],
        max_total_video_bytes=limits_value["maxTotalVideoBytes"],
        max_active_finalizers=limits_value["maxActiveFinalizers"],
        finalization_timeout_seconds=limits_value[
            "finalizationTimeoutSeconds"
        ],
        max_compressed_segment_bytes=limits_value.get("maxCompressedSegmentBytes", MAX_COMPRESSED_SEGMENT_BYTES),
        target_bitrate=limits_value.get("targetBitrate"),
        max_key_frame_interval=limits_value.get("maxKeyFrameInterval"),
    )
    segments = value["segments"]
    _require(type(segments) is list and len(segments) <= limits.max_segments,
             "Invalid video segments")
    total_bytes = 0
    segment_ids = set()
    segment_intervals = {}
    all_durable_sequences = set()
    for expected_index, segment in enumerate(segments, 1):
        _require(type(segment) is dict and set(segment) == {
            "segmentId", "segmentIndex", "state", "digest", "bytes", "path",
            "mimeType", "codec", "container", "width", "height",
            "orientation", "frameCount", "firstCapturedOffsetMs",
            "lastCapturedOffsetMs", "captureInterval", "displayInterval",
            "timingRelation", "sourceSamplingMode", "rotationReason", "frames",
        }, "Invalid video segment")
        _require(segment["segmentId"] == f"video_segment_{expected_index}"
                 and segment["segmentIndex"] == expected_index
                 and segment["state"] == "durable",
                 "Invalid video segment identity")
        segment_ids.add(segment["segmentId"])
        _require(type(segment["digest"]) is str
                 and _DIGEST.fullmatch(segment["digest"]) is not None,
                 "Invalid video segment digest")
        _require(segment["path"]
                 == f"objects/sha256/{segment['digest'][:2]}/{segment['digest']}",
                 "Invalid video segment path")
        size = _integer(segment["bytes"], "video segment bytes", 1,
                        limits.max_segment_bytes)
        total_bytes += size
        _require(total_bytes <= limits.max_total_video_bytes,
                 "Total video size limit exceeded")
        width = _integer(segment["width"], "video width", 2, 4096)
        height = _integer(segment["height"], "video height", 2, 4096)
        _require(width * height <= limits.max_decoded_pixels,
                 "Video pixel limit exceeded")
        _require(width % 2 == 0 and height % 2 == 0,
                 "Video geometry is not H.264 compatible")
        _require(segment["mimeType"] == "video/mp4"
                 and segment["codec"] == "h264"
                 and segment["container"] == "mp4"
                 and segment["orientation"] in {"portrait", "landscape"},
                 "Invalid video segment media")
        frames = segment["frames"]
        count = _integer(segment["frameCount"], "video frame count", 1,
                         limits.max_frames_per_segment)
        _require(type(frames) is list and len(frames) == count,
                 "Video frame count differs")
        previous_pts = -1
        precise = segment["captureInterval"] is not None
        for frame in frames:
            _require(type(frame) is dict, "Invalid video frame lineage")
            common = {
                "acquisitionSequence", "sourceDigest", "presentationTimeMs",
                "timingSource", "uncertaintyNs", "providerIncarnation",
                "nativeIncarnation", "ptsRelation",
            }
            if transient:
                common.add("recordingFrameSequence")
            expected = (common | {"displayOffsetMs"}
                        if frame.get("timingSource") == "native-unmapped"
                        else common | {"earliestRecordingOffsetMs",
                                       "latestRecordingOffsetMs"})
            _require(set(frame) == expected,
                     "Invalid video frame lineage")
            sequence = _integer(frame["acquisitionSequence"],
                                "video acquisition sequence", 1, 2 ** 63 - 1)
            _require(sequence not in all_durable_sequences,
                     "Duplicate durable video frame")
            all_durable_sequences.add(sequence)
            _require(type(frame["sourceDigest"]) is str
                     and _DIGEST.fullmatch(frame["sourceDigest"]) is not None,
                     "Invalid source frame digest")
            pts = _integer(frame["presentationTimeMs"],
                           "video presentation time", 0, 600_000)
            _require(pts > previous_pts, "Video presentation time is not increasing")
            previous_pts = pts
            _integer(frame["uncertaintyNs"], "video uncertainty", 0,
                     60_000_000_000)
            _identifier(frame["providerIncarnation"], "provider incarnation")
            _identifier(frame["nativeIncarnation"], "native incarnation")
            if frame["timingSource"] == "native-unmapped":
                _require(not precise
                         and frame["ptsRelation"]
                         == "display-publication-order-only",
                         "Native-unmapped frame claims capture timing")
                _integer(frame["displayOffsetMs"], "display offset", 0, 600_000)
            else:
                _require(precise and frame["timingSource"] in {
                    "host-acquired", "provider-mapped"
                } and frame["ptsRelation"] == "recording-elapsed",
                         "Invalid precise video timing")
                earliest = _integer(frame["earliestRecordingOffsetMs"],
                                    "capture interval start", 0, 600_000)
                _integer(frame["latestRecordingOffsetMs"],
                         "capture interval end", earliest, 600_000)
        _require(frames[0]["presentationTimeMs"] == 0
                 and frames[-1]["presentationTimeMs"] < bound,
                 "Video segment duration bound exceeded")
        display = segment["displayInterval"]
        _require(type(display) is dict and set(display) == {
            "startOffsetMs", "endOffsetMs"
        }, "Invalid video display interval")
        display_start = _integer(display["startOffsetMs"],
                                 "display interval start", 0, 600_000)
        _integer(display["endOffsetMs"], "display interval end",
                 display_start, 600_000)
        _require(display["endOffsetMs"]
                 == display_start + frames[-1]["presentationTimeMs"],
                 "Video display interval differs from PTS")
        segment_intervals[segment["segmentId"]] = (
            display_start, display["endOffsetMs"]
        )
        _require(segment["rotationReason"] in {
            "duration-bound", "geometry-change", "clock-discontinuity",
            "frame-count-bound", "timestamp-collision", "stop-barrier", "compressed-byte-bound",
        }, "Invalid video rotation reason")
        if precise:
            interval = segment["captureInterval"]
            _require(type(interval) is dict and set(interval) == {
                "firstCapturedEarliestOffsetMs", "firstCapturedLatestOffsetMs",
                "lastCapturedEarliestOffsetMs", "lastCapturedLatestOffsetMs",
            }, "Invalid video capture interval")
            first_earliest = _integer(
                interval["firstCapturedEarliestOffsetMs"],
                "first capture interval start", 0, 600_000,
            )
            first_latest = _integer(
                interval["firstCapturedLatestOffsetMs"],
                "first capture interval end", first_earliest, 600_000,
            )
            last_earliest = _integer(
                interval["lastCapturedEarliestOffsetMs"],
                "last capture interval start", 0, 600_000,
            )
            last_latest = _integer(
                interval["lastCapturedLatestOffsetMs"],
                "last capture interval end", last_earliest, 600_000,
            )
            _require(segment["firstCapturedOffsetMs"] == display_start
                     and segment["lastCapturedOffsetMs"] == display["endOffsetMs"]
                     and first_earliest
                     == frames[0]["earliestRecordingOffsetMs"]
                     and first_latest
                     == frames[0]["latestRecordingOffsetMs"]
                     and last_earliest
                     == frames[-1]["earliestRecordingOffsetMs"]
                     and last_latest
                     == frames[-1]["latestRecordingOffsetMs"]
                     and segment["timingRelation"]
                     == "recording-elapsed-segment-local-pts"
                     and segment["sourceSamplingMode"]
                     == "irregular-capture-intervals",
                     "Invalid video capture relation")
        else:
            _require(segment["firstCapturedOffsetMs"] is None
                     and segment["lastCapturedOffsetMs"] is None
                     and segment["timingRelation"]
                     == "display-publication-order-only"
                     and segment["sourceSamplingMode"]
                     == "native-acquisition-unknown",
                     "Native-unmapped segment claims capture interval")
    losses = value["losses"]
    _require(type(losses) is list and len(losses) <= MAX_VIDEO_LOSSES,
             "Invalid video losses")
    for loss in losses:
        _require(type(loss) is dict, "Invalid video loss")
        loss_fields = set(loss) - ({"recordingFrameRange", "nativeSequenceRange"} if transient else set())
        allowed_loss_fields = [{
            "lossClass", "stage", "reason", "recordingInterval"
        }, {
            "lossClass", "stage", "reason", "recordingInterval",
            "acquisitionSequence"
        }, {
            "lossClass", "stage", "reason", "recordingInterval",
            "acquisitionSequence", "segmentIndex"
        }]
        if transient:
            allowed_loss_fields.append({"lossClass", "stage", "reason", "recordingInterval", "segmentIndex"})
        _require(loss_fields in allowed_loss_fields, "Invalid video loss")
        _require(loss["lossClass"] in {
            "not-acquired", "captured-dropped",
            "encoder-accepted-not-durable", "unknown-acquisition-interval",
        } and loss["stage"] in {
            "native", "transport", "queue", "encoder", "timing"
        }, "Invalid video loss class")
        _identifier(loss["reason"], "video loss reason")
        interval = loss["recordingInterval"]
        _require(type(interval) is dict and set(interval) == {
            "startOffsetMs", "endOffsetMs"
        }, "Invalid video loss interval")
        start = _integer(interval["startOffsetMs"], "loss start", 0, 600_000)
        _integer(interval["endOffsetMs"], "loss end", start, 600_000)
        if "acquisitionSequence" in loss:
            _integer(loss["acquisitionSequence"], "loss acquisition sequence", 1,
                     2 ** 63 - 1)
        if "segmentIndex" in loss:
            _integer(loss["segmentIndex"], "loss segment index", 1,
                     limits.max_segments)
    mappings = value["eventMappings"]
    _require(type(mappings) is list and len(mappings) <= 512,
             "Invalid video event mappings")
    for expected_sequence, event in enumerate(mappings, 1):
        _require(type(event) is dict and set(event) == {
            "eventId", "eventSequence", "recordingOffsetMs", "mapping"
        } and event["eventSequence"] == expected_sequence,
                 "Invalid video event mapping")
        _identifier(event["eventId"], "video event identity")
        _integer(event["recordingOffsetMs"], "event recording offset", 0, 600_000)
        mapping = event["mapping"]
        _require(type(mapping) is dict, "Invalid video event mapping")
        if mapping.get("kind") == "gap":
            _require(set(mapping) == {"kind"}, "Invalid video gap mapping")
        else:
            _require(type(mapping) is dict and set(mapping) == {
                "kind", "segmentId", "presentationTimeMs"
            } and mapping["kind"] == "segment"
                     and mapping["segmentId"] in segment_ids,
                     "Invalid video segment mapping")
            presentation = _integer(
                mapping["presentationTimeMs"], "event presentation time",
                0, 600_000,
            )
            start, end = segment_intervals[mapping["segmentId"]]
            _require(start <= event["recordingOffsetMs"] <= end
                     and presentation == event["recordingOffsetMs"] - start,
                     "Event mapping differs from segment timing")
    _require((value["status"] == "complete") == (not losses and failure is None),
             "Video completeness contradicts loss evidence")
    _require((value["samplingMode"] == "no-acquired-frames")
             == (not segments and not any(
                 "acquisitionSequence" in loss for loss in losses
             )), "Video sampling mode contradicts evidence")
    if transient:
        _require(value["sourceDisposition"] == "transient-after-durable-outcome",
                 "Invalid video source disposition")
        validate_source_coverage(value["sourceFrames"], segments, losses)
        _require(value["status"] != "complete" or bool(value["sourceFrames"]),
                 "Complete video has no admitted source frames")
    return copy.deepcopy(value)


def _validate_encoder_frame(frame: EncoderFrame, limits: VideoLimits) -> None:
    _require(type(frame) is EncoderFrame, "Invalid encoder frame")
    _require(_DIGEST.fullmatch(frame.digest) is not None, "Invalid frame digest")
    _integer(frame.bytes, "frame bytes", 1, limits.max_frame_bytes)
    _require(frame.mime_type in _MIME_TYPES, "Unsupported encoder frame MIME type")
    _integer(frame.width, "frame width", 2, 4096)
    _integer(frame.height, "frame height", 2, 4096)
    _require(frame.width % 2 == 0 and frame.height % 2 == 0,
             "Frame geometry is not H.264 compatible")
    _require(frame.width == frame.published_width
             and frame.height == frame.published_height,
             "Frame geometry differs from publication")
    _require(frame.width * frame.height <= limits.max_decoded_pixels,
             "Decoded frame pixel limit exceeded")
    _require(frame.orientation in {"portrait", "landscape"},
             "Invalid frame orientation")
    _integer(frame.acquisition_sequence, "acquisition sequence", 1, 2 ** 63 - 1)
    _integer(frame.offset_ms, "recording offset", 0, 600_000)
    _integer(frame.earliest_offset_ms, "capture interval start", 0, 600_000)
    _integer(frame.latest_offset_ms, "capture interval end", 0, 600_000)
    _require(frame.earliest_offset_ms <= frame.latest_offset_ms,
             "Invalid frame capture interval")
    _integer(frame.uncertainty_ns, "frame uncertainty", 0, 60_000_000_000)
    _require(frame.timing_source in _TIMING_SOURCES, "Invalid frame timing source")
    _identifier(frame.provider_incarnation, "provider incarnation")
    _identifier(frame.native_incarnation, "native incarnation")


def encode_configuration(*, width: int, height: int, limits: VideoLimits,
                         fault_mode: str = "none") -> bytes:
    _require(type(limits) is VideoLimits, "Invalid video limits")
    _integer(width, "video width", 2, 4096)
    _integer(height, "video height", 2, 4096)
    _require(width * height <= limits.max_decoded_pixels,
             "Decoded video pixel limit exceeded")
    _require(fault_mode in _FAULT_MODES, "Invalid video fault mode")
    value = {
        "schemaVersion": 2 if limits.target_bitrate is not None else PROTOCOL_VERSION,
        "codec": "h264",
        "container": "mp4",
        "width": width,
        "height": height,
        "maxFrames": limits.max_frames_per_segment,
        "maxCompressedBytes": min(
            limits.max_compressed_segment_bytes,
            limits.max_frames_per_segment * limits.max_frame_bytes,
        ),
        "maxDecodedPixels": limits.max_decoded_pixels,
        "maxOutputBytes": limits.max_segment_bytes,
        "faultMode": fault_mode,
    }
    if limits.target_bitrate is not None:
        value.update(targetBitrate=limits.target_bitrate,
                     maxKeyFrameInterval=limits.max_key_frame_interval)
    body = _json_bytes(value, MAX_CONFIG_BYTES, "Encoder configuration is too large")
    return PROTOCOL_MAGIC + struct.pack(">I", len(body)) + body


def encode_frame(frame: EncoderFrame, body: bytes, segment_first_offset_ms: int,
                 limits: VideoLimits) -> bytes:
    _require(type(limits) is VideoLimits, "Invalid video limits")
    _validate_encoder_frame(frame, limits)
    _integer(segment_first_offset_ms, "segment start offset", 0, 600_000)
    _require(type(body) is bytes and len(body) == frame.bytes,
             "Frame bytes differ from publication")
    _require(hashlib.sha256(body).hexdigest() == frame.digest,
             "Frame digest differs from publication")
    _require(frame.offset_ms >= segment_first_offset_ms,
             "Frame presentation time moved backward")
    metadata = {
        "schemaVersion": PROTOCOL_VERSION,
        "mimeType": frame.mime_type,
        "width": frame.width,
        "height": frame.height,
        "acquisitionSequence": frame.acquisition_sequence,
        "digest": frame.digest,
        "ptsNs": (frame.offset_ms - segment_first_offset_ms) * 1_000_000,
        "timingSource": frame.timing_source,
        "uncertaintyNs": frame.uncertainty_ns,
        "providerIncarnation": frame.provider_incarnation,
        "nativeIncarnation": frame.native_incarnation,
    }
    if frame.timing_source == "native-unmapped":
        metadata.update({
            "displayRecordingOffsetMs": frame.offset_ms,
            "ptsRelation": "display-publication-order-only",
        })
    else:
        metadata.update({
            "earliestRecordingOffsetMs": frame.earliest_offset_ms,
            "latestRecordingOffsetMs": frame.latest_offset_ms,
            "ptsRelation": "recording-elapsed",
        })
    meta = _json_bytes(metadata, MAX_FRAME_METADATA_BYTES,
                       "Frame metadata is too large")
    return b"F" + struct.pack(">II", len(meta), len(body)) + meta + body


def encode_finish() -> bytes:
    return b"E" + struct.pack(">II", 0, 0)


def _parse_output(output: bytes, frames: tuple[EncoderFrame, ...], *,
                  max_bytes: int, require_finished: bool):
    _require(type(output) is bytes and len(output) <= max_bytes,
             "Helper output limit exceeded")
    _require(output.endswith(b"\n") or not output,
             "Helper output is truncated")
    expected = {frame.acquisition_sequence: frame for frame in frames}
    accepted = []
    previous_pts = -1
    finished = None
    for raw in output.splitlines():
        _require(finished is None, "Helper emitted data after completion")
        _require(0 < len(raw) <= MAX_FRAME_METADATA_BYTES,
                 "Malformed helper output")
        try:
            item = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, ContractError):
            raise VideoProtocolError("Malformed helper output") from None
        _require(type(item) is dict and type(item.get("schemaVersion")) is int
                 and item["schemaVersion"] == 1,
                 "Malformed helper output")
        if item.get("type") == "accepted":
            _require(set(item) == {
                "schemaVersion", "type", "acquisitionSequence", "ptsNs"
            }, "Malformed helper acknowledgement")
            sequence = item["acquisitionSequence"]
            _require(type(sequence) is int and sequence in expected
                     and sequence not in accepted,
                     "Unexpected helper acknowledgement")
            pts = item["ptsNs"]
            _require(type(pts) is int and pts >= 0 and pts > previous_pts,
                     "Helper acknowledgement time is invalid")
            expected_pts = (
                expected[sequence].offset_ms - frames[0].offset_ms
            ) * 1_000_000
            _require(pts == expected_pts, "Helper acknowledgement time changed")
            accepted.append(sequence)
            previous_pts = pts
        elif item.get("type") == "finished":
            _require(set(item) == {
                "schemaVersion", "type", "codec", "container", "frames",
                "bytes", "sha256"
            } and finished is None, "Malformed helper completion")
            _require(item["codec"] == "h264" and item["container"] == "mp4",
                     "Unexpected helper codec")
            _require(type(item["frames"]) is int
                     and item["frames"] == len(frames) == len(accepted),
                     "Helper frame count differs")
            _require(type(item["bytes"]) is int and item["bytes"] > 0,
                     "Invalid helper output size")
            _require(type(item["sha256"]) is str
                     and _DIGEST.fullmatch(item["sha256"]) is not None,
                     "Invalid helper output digest")
            finished = item
        else:
            raise VideoProtocolError("Malformed helper output")
    if require_finished:
        _require(finished is not None and tuple(accepted) == tuple(expected),
                 "Helper did not durably finish the segment")
    return accepted, finished


def parse_helper_output(output: bytes, frames: Iterable[EncoderFrame], *,
                        max_bytes: int) -> HelperResult:
    checked = tuple(frames)
    _require(bool(checked), "Empty encoded segment")
    accepted, finished = _parse_output(
        output, checked, max_bytes=max_bytes, require_finished=True,
    )
    return HelperResult(
        tuple(accepted), finished["bytes"], finished["sha256"],
        finished["codec"], finished["container"],
    )


class _BoundedReader(threading.Thread):
    def __init__(self, stream, maximum):
        super().__init__(daemon=True)
        self.stream = stream
        self.maximum = maximum
        self.body = bytearray()
        self.exceeded = threading.Event()

    def run(self):
        try:
            while True:
                chunk = self.stream.read(4096)
                if not chunk:
                    return
                remaining = self.maximum + 1 - len(self.body)
                if remaining > 0:
                    self.body.extend(chunk[:remaining])
                if len(self.body) > self.maximum:
                    self.exceeded.set()
                    return
        except (OSError, ValueError):
            return


class AVFoundationSegmentEncoder:
    """One trusted helper process per segment with bounded stdio and lifetime."""

    def __init__(self, helper: Path, evidence: EvidenceStore, limits: VideoLimits):
        _require(type(evidence) is EvidenceStore, "Evidence store is required")
        _require(type(limits) is VideoLimits, "Invalid video limits")
        helper = Path(helper)
        try:
            stat = helper.lstat()
        except OSError:
            raise VideoProtocolError("Video helper is unavailable") from None
        _require(helper.is_file() and not helper.is_symlink()
                 and stat.st_uid == os.getuid() and os.access(helper, os.X_OK),
                 "Video helper is unavailable")
        self.helper = helper.resolve()
        self.evidence = evidence
        self.limits = limits
        self.source_reader = None

    @staticmethod
    def _stop_process(process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def encode(self, frames, output_directory, *, timeout_seconds,
               fault_mode="none", ownership_fd=None):
        frames = tuple(frames)
        _require(0 < len(frames) <= self.limits.max_frames_per_segment,
                 "Invalid segment frame count")
        for frame in frames:
            _validate_encoder_frame(frame, self.limits)
        _require(sum(frame.bytes for frame in frames) <= self.limits.max_compressed_segment_bytes,
                 "Segment compressed input limit exceeded")
        _require(len({frame.geometry for frame in frames}) == 1,
                 "Segment geometry changed")
        _require(len({frame.clock_segment for frame in frames}) == 1,
                 "Segment clock mapping changed")
        _require(all(later.offset_ms > earlier.offset_ms
                     for earlier, later in zip(frames, frames[1:])),
                 "Segment presentation time is not monotonic")
        directory = _owned_directory(Path(output_directory))
        _require(not any(directory.iterdir()), "Segment work directory is not empty")
        if ownership_fd is not None:
            _integer(ownership_fd, "video ownership descriptor", 0, 2 ** 31 - 1)
            _require(os.fstat(ownership_fd).st_uid == os.getuid(),
                     "Video ownership descriptor is unavailable")
        process = subprocess.Popen(
            [str(self.helper), "--protocol-stdio"],
            cwd=directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            pass_fds=(() if ownership_fd is None else (ownership_fd,)),
        )
        stdout = _BoundedReader(process.stdout, self.limits.max_helper_output_bytes)
        stderr = _BoundedReader(process.stderr, self.limits.max_helper_error_bytes)
        stdout.start()
        stderr.start()
        writer_error = []

        def write_input():
            try:
                process.stdin.write(encode_configuration(
                    width=frames[0].width,
                    height=frames[0].height,
                    limits=self.limits,
                    fault_mode=fault_mode,
                ))
                for frame in frames:
                    # Re-read the approved, pinned object at the last moment.
                    body = (self.source_reader(frame) if self.source_reader is not None
                            else self.evidence.read(frame.digest))
                    process.stdin.write(encode_frame(
                        frame, body, frames[0].offset_ms, self.limits,
                    ))
                    process.stdin.flush()
                process.stdin.write(encode_finish())
                process.stdin.flush()
            except Exception as error:
                writer_error.append(error)
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
        deadline = time.monotonic() + timeout_seconds
        reason = None
        while process.poll() is None:
            if stdout.exceeded.is_set() or stderr.exceeded.is_set():
                reason = "helper-stream-limit"
                break
            if time.monotonic() >= deadline:
                reason = "finalization-timeout"
                break
            time.sleep(0.005)
        if reason is not None:
            self._stop_process(process)
        else:
            process.wait()
        writer.join(0.2)
        stdout.join(0.2)
        stderr.join(0.2)
        stdout_body = bytes(stdout.body[:self.limits.max_helper_output_bytes])
        stderr_body = bytes(stderr.body[:self.limits.max_helper_error_bytes])
        process.stdout.close()
        process.stderr.close()
        accepted, finished = _parse_output(
            stdout_body,
            frames,
            max_bytes=self.limits.max_helper_output_bytes,
            require_finished=False,
        )
        if reason is not None:
            raise VideoEncodingError(reason, accepted_sequences=accepted)
        if stdout.exceeded.is_set() or stderr.exceeded.is_set():
            raise VideoEncodingError("helper-stream-limit",
                                     accepted_sequences=accepted)
        if process.returncode != 0 or writer_error or finished is None:
            static_error = stderr_body.decode("ascii", "ignore").strip()
            reason = (
                "codec-unavailable"
                if static_error == "video_helper_failed:writer_start_failed_-11834"
                else "encoder-process-failed"
            )
            raise VideoEncodingError(reason,
                                     accepted_sequences=accepted)
        if stderr_body:
            raise VideoEncodingError("helper-error-output",
                                     accepted_sequences=accepted)
        result = parse_helper_output(
            stdout_body, frames,
            max_bytes=self.limits.max_helper_output_bytes,
        )
        path = directory / "segment.mp4"
        try:
            stat = path.lstat()
        except OSError:
            raise VideoEncodingError("encoder-output-missing",
                                     accepted_sequences=accepted) from None
        _require(path.is_file() and not path.is_symlink()
                 and stat.st_uid == os.getuid()
                 and stat.st_size == result.bytes
                 and 0 < stat.st_size <= self.limits.max_segment_bytes,
                 "Encoder output violates file bounds")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        _require(digest == result.digest, "Encoder output digest differs")
        return EncodedSegment(
            path, result.accepted_sequences, result.bytes, result.digest,
            result.codec, result.container,
        )


class _VideoJournal:
    def __init__(self, root: Path, evidence: EvidenceStore):
        catalog_id = "video_catalog_" + hashlib.sha256(
            str(Path(root).resolve()).encode("utf-8")
        ).hexdigest()[:40]
        reservation = evidence.budget.reserve(
            catalog_id, "journal", VIDEO_CATALOG_RESERVATION_BYTES,
            idempotency_key=catalog_id,
        )
        reservation.commit(VIDEO_CATALOG_RESERVATION_BYTES)
        self.root = _owned_directory(root)
        self._lock = threading.RLock()
        database = self.root / "video.sqlite3"
        _require(not database.is_symlink(), "Invalid video journal")
        self.connection = sqlite3.connect(
            database, timeout=10, isolation_level=None, check_same_thread=False,
        )
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA busy_timeout=10000")
            self.connection.execute("PRAGMA journal_size_limit=262144")
            self.connection.execute("PRAGMA wal_autocheckpoint=32")
            pages = self.connection.execute("PRAGMA max_page_count=32768").fetchone()[0]
            _require(pages <= 32768, "Video journal is oversized")
            self._initialize()
        except Exception:
            self.connection.close()
            self.connection = None
            raise

    def _initialize(self):
        with self._lock:
            connection = self.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS sessions (
                           recording_id TEXT PRIMARY KEY,
                           state TEXT NOT NULL,
                           limits_json TEXT NOT NULL,
                           journal_reservation_id TEXT NOT NULL,
                           spool_reservation_id TEXT NOT NULL,
                           encoding_reservation_id TEXT NOT NULL,
                           retain_until_ms INTEGER NOT NULL,
                           manifest_json TEXT,
                           manifest_digest TEXT,
                           manifest_reference_json TEXT,
                           failure_reason TEXT,
                           binding_state TEXT NOT NULL DEFAULT 'bound',
                           source_config_json TEXT NOT NULL DEFAULT ''
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS frames (
                           recording_id TEXT NOT NULL,
                           acquisition_sequence INTEGER NOT NULL,
                           frame_json TEXT NOT NULL,
                           pin_id TEXT NOT NULL,
                           state TEXT NOT NULL,
                           segment_index INTEGER,
                           PRIMARY KEY(recording_id, acquisition_sequence)
                       )"""
                )
                columns = {item[1] for item in connection.execute("PRAGMA table_info(sessions)")}
                if "source_mode" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN source_mode TEXT NOT NULL DEFAULT 'original-cas-v1'")
                if "binding_state" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN binding_state TEXT NOT NULL DEFAULT 'bound'")
                if "source_config_json" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN source_config_json TEXT NOT NULL DEFAULT ''")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS segments (
                           recording_id TEXT NOT NULL,
                           segment_index INTEGER NOT NULL,
                           state TEXT NOT NULL,
                           rotation_reason TEXT NOT NULL,
                           lineage_json TEXT NOT NULL,
                           reservation_id TEXT,
                           expected_digest TEXT,
                           expected_bytes INTEGER,
                           reference_json TEXT,
                           manifest_json TEXT,
                           PRIMARY KEY(recording_id, segment_index)
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS losses (
                           recording_id TEXT NOT NULL,
                           loss_sequence INTEGER NOT NULL,
                           loss_json TEXT NOT NULL,
                           PRIMARY KEY(recording_id, loss_sequence)
                       )"""
                )
                existing = dict(connection.execute("SELECT key, value FROM metadata"))
                expected = {"format_version": 2}
                if existing == {"format_version": 1}:
                    connection.execute("UPDATE metadata SET value=2 WHERE key='format_version'")
                    existing = expected
                _require(not existing or existing == expected,
                         "Video journal version mismatch")
                if not existing:
                    connection.executemany(
                        "INSERT INTO metadata(key, value) VALUES (?, ?)",
                        expected.items(),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def transaction(self, callback):
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                result = callback(self.connection)
                self.connection.commit()
                return result
            except Exception:
                self.connection.rollback()
                raise

    def close(self):
        with self._lock:
            if self.connection is not None:
                self.connection.close()
                self.connection = None


class VideoFrameSink:
    """G2 frame sink with bounded capture and asynchronous sealing workers."""

    _process_finalizer_lock = threading.Lock()
    _process_finalizers = 0

    def __init__(self, evidence: EvidenceStore, root: Path, *, encoder=None,
                 helper: Path | None = None, limits: VideoLimits | None = None,
                 backpressure_policy: str = "gap", fault_mode: str = "none",
                 source_mode: str = ORIGINAL_FRAME_MODE):
        _require(type(evidence) is EvidenceStore, "Evidence store is required")
        self.evidence = evidence
        self.root = _owned_directory(Path(root))
        self._work_root = _owned_directory(self.root / "work")
        self.work = None
        self.limits = limits or VideoLimits()
        _require(type(self.limits) is VideoLimits, "Invalid video limits")
        _require(backpressure_policy in {"gap", "pause"},
                 "Invalid video backpressure policy")
        _require(fault_mode in _FAULT_MODES, "Invalid video fault mode")
        if encoder is None:
            _require(helper is not None, "Video helper is required")
            encoder = AVFoundationSegmentEncoder(helper, evidence, self.limits)
        _require(callable(getattr(encoder, "encode", None)),
                 "Invalid segment encoder")
        self.encoder = encoder
        self.backpressure_policy = backpressure_policy
        self.fault_mode = fault_mode
        _require(source_mode in {ORIGINAL_FRAME_MODE, VIDEO_SOURCE_MODE}, "Invalid video source mode")
        self.source_mode = source_mode
        self.source_spool = None
        self._source_manifest = None
        self._source_manifest_attached = False
        self.journal = _VideoJournal(self.root, evidence)
        self._locks = _owned_directory(self.root / "locks")
        self._writer_fd = None
        self._condition = threading.Condition(threading.RLock())
        self._queue = deque()
        self._queued_bytes = 0
        self._session = None
        self._recording_id = None
        self._state = "unbound"
        self._current = []
        self._segment_index = 0
        self._segments = []
        self._losses = []
        self._pins: dict[str, EvidencePin] = {}
        self._segment_pins: dict[str, EvidencePin] = {}
        self._worker = None
        self._worker_done = threading.Event()
        self._finalizer_queue = deque()
        self._finalizer_active = False
        self._finalizer_stop = False
        self._finalizer = None
        self._finalizer_done = threading.Event()
        self._close_requested = False
        self._close_complete = False
        self._work_cleanup_unconfirmed = False
        self._cancelled = threading.Event()
        self._manifest_sealed = False
        self._admitted_frame_count = 0
        self._finalized = None
        self._spool_reservation = None
        self._encoding_reservation = None
        self._journal_reservation = None

    @classmethod
    def _claim_process_finalizer(cls):
        with cls._process_finalizer_lock:
            if cls._process_finalizers >= MAX_PROCESS_FINALIZERS:
                return False
            cls._process_finalizers += 1
            return True

    @classmethod
    def _release_process_finalizer(cls):
        with cls._process_finalizer_lock:
            _require(cls._process_finalizers > 0,
                     "Video finalizer accounting underflow")
            cls._process_finalizers -= 1

    def bind_recording(self, session):
        from .recording_session import RecordingSession

        _require(type(session) is RecordingSession
                 and session.store.evidence is self.evidence,
                 "Video sink is not bound to the recording evidence store")
        with self._condition:
            _require(self._state == "unbound", "Video sink is already bound")
            recording_id = session.recording_id
            self.work = _owned_directory(
                self._work_root / hashlib.sha256(
                    recording_id.encode("ascii")
                ).hexdigest()
            )
            _require(not any(self.work.iterdir()),
                     "Video recording work directory is not empty")
            lock_path = self._locks / (
                hashlib.sha256(recording_id.encode("ascii")).hexdigest() + ".lock"
            )
            self._writer_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
            )
            try:
                fcntl.flock(self._writer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(self._writer_fd)
                self._writer_fd = None
                raise VideoProtocolError(
                    "Video recording already has a live writer"
                ) from None
            try:
                budget_key = "video_" + hashlib.sha256(
                    f"{self.root.resolve()}:{recording_id}".encode("utf-8")
                ).hexdigest()[:40]
                self._journal_reservation = self.evidence.budget.reserve(
                    recording_id, "journal", (32 * 1024 * 1024
                        if self.source_mode == VIDEO_SOURCE_MODE else VIDEO_JOURNAL_RESERVATION_BYTES),
                    idempotency_key=budget_key + "_journal",
                )
                self._spool_reservation = self.evidence.budget.reserve(
                    recording_id, "spool", self.limits.max_queue_bytes,
                    idempotency_key=budget_key + "_spool",
                )
                self._encoding_reservation = self.evidence.budget.reserve(
                    recording_id, "encoding", self.limits.max_segment_bytes,
                    idempotency_key=budget_key + "_encoding",
                )
            except Exception:
                if self._spool_reservation is not None:
                    self._spool_reservation.close()
                if self._journal_reservation is not None:
                    self._journal_reservation.close()
                self._spool_reservation = None
                self._journal_reservation = None
                self._unlock_writer()
                raise
            limits_json = _json_bytes(
                {name: getattr(self.limits, name)
                 for name in self.limits.__dataclass_fields__},
                MAX_CONFIG_BYTES,
                "Video limits are too large",
            ).decode("utf-8")
            retain_until_ms = (
                int(time.time() * 1000)
                + session.registration.collection_policy[
                    "retentionSeconds"
                ]["original"] * 1000
            )

            def insert(connection):
                _require(connection.execute(
                    "SELECT 1 FROM sessions WHERE recording_id = ?",
                    (recording_id,),
                ).fetchone() is None, "Video recording identity already exists")
                source_root = str(self.root / "sources" / hashlib.sha256(
                    recording_id.encode("ascii")).hexdigest())
                source_config = _json_bytes({
                    "limits": json.loads(limits_json),
                    "retainUntilMs": retain_until_ms,
                    "sourceRoot": source_root,
                }, MAX_FRAME_METADATA_BYTES, "Video source configuration is too large").decode("utf-8")
                connection.execute(
                    """INSERT INTO sessions
                       (recording_id, state, limits_json, spool_reservation_id,
                        encoding_reservation_id, retain_until_ms,
                        journal_reservation_id, source_mode, binding_state,
                        source_config_json)
                       VALUES (?, 'recording', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (recording_id, limits_json,
                     self._spool_reservation.reservation_id,
                     self._encoding_reservation.reservation_id,
                     retain_until_ms,
                     self._journal_reservation.reservation_id, self.source_mode,
                     "preparing" if self.source_mode == VIDEO_SOURCE_MODE else "bound",
                     source_config),
                )
            try:
                self.journal.transaction(insert)
            except Exception:
                self._spool_reservation.close()
                self._encoding_reservation.close()
                self._journal_reservation.close()
                self._spool_reservation = self._encoding_reservation = None
                self._journal_reservation = None
                self._unlock_writer()
                raise
            self._session = session
            self._recording_id = recording_id
            try:
                if self.source_mode == VIDEO_SOURCE_MODE:
                    from .frame_spool import FrameSpool
                    (self._source_spool_max_frames,
                     self._source_spool_max_bytes) = _source_spool_bounds(
                         self.limits)
                    self.source_spool = FrameSpool(
                        self.root / "sources" / hashlib.sha256(recording_id.encode("ascii")).hexdigest(),
                        self.evidence.budget, recording_id,
                        max_frames=self._source_spool_max_frames,
                        max_bytes=self._source_spool_max_bytes)
                    session.use_frame_spool(self.source_spool, limits=self.limits,
                                            retain_until_ms=retain_until_ms)
                    if isinstance(self.encoder, AVFoundationSegmentEncoder):
                        self.encoder.source_reader = self.read_frame_source
                self.journal.transaction(lambda connection: connection.execute(
                    "UPDATE sessions SET binding_state=? WHERE recording_id=? AND binding_state='preparing'",
                    ("bound", recording_id)))
                self._state = "recording"
                self._finalizer = threading.Thread(
                    target=self._finalizer_main,
                    name=f"video-finalizer-{recording_id}",
                    daemon=True,
                )
                self._finalizer.start()
                self._worker = threading.Thread(
                    target=self._worker_main,
                    name=f"video-{recording_id}",
                    daemon=True,
                )
                self._worker.start()
            except Exception:
                if self.source_mode == VIDEO_SOURCE_MODE:
                    self._abort_prebinding()
                raise

    @staticmethod
    def _frame_json(frame: EncoderFrame):
        value = {
            "digest": frame.digest,
            "bytes": frame.bytes,
            "path": frame.path,
            "mimeType": frame.mime_type,
            "width": frame.width,
            "height": frame.height,
            "orientation": frame.orientation,
            "acquisitionSequence": frame.acquisition_sequence,
            "offsetMs": frame.offset_ms,
            "earliestOffsetMs": frame.earliest_offset_ms,
            "latestOffsetMs": frame.latest_offset_ms,
            "uncertaintyNs": frame.uncertainty_ns,
            "timingSource": frame.timing_source,
            "providerClockId": frame.provider_clock_id,
            "providerBootDigest": frame.provider_boot_digest,
            "providerIncarnation": frame.provider_incarnation,
            "nativeIncarnation": frame.native_incarnation,
        }
        if frame.recording_frame_sequence is not None:
            value["recordingFrameSequence"] = frame.recording_frame_sequence
        return _json_bytes(value, MAX_FRAME_METADATA_BYTES, "Frame lineage is too large").decode("utf-8")

    def read_frame_source(self, frame):
        if self.source_mode == VIDEO_SOURCE_MODE:
            _require(self.source_spool is not None and frame.source_token is not None,
                     "Video source spool is unavailable")
            return self.source_spool.read(frame.source_token)
        return self.evidence.read(frame.digest)

    def _prebinding_g2_empty(self):
        g2_row = self._session._row()
        _require(g2_row["source_mode"] == ORIGINAL_FRAME_MODE,
                 "Prebinding G2 source mode is no longer original")
        with self.journal._lock:
            row = self.journal.connection.execute(
                "SELECT recording_id,source_mode,binding_state,manifest_json,manifest_digest,"
                "limits_json,retain_until_ms,source_config_json "
                "FROM sessions WHERE recording_id=?", (self._recording_id,)).fetchone()
            _require(row is not None
                     and row["binding_state"] in {"preparing", "aborting"}
                     and row["manifest_json"] is None and row["manifest_digest"] is None,
                     "Prebinding G2 session is not empty")
            _validate_prebinding_source_config(self.root, row)
            for table in ("frames", "segments", "losses"):
                _require(self.journal.connection.execute(
                    f"SELECT 1 FROM {table} WHERE recording_id=? LIMIT 1",
                    (self._recording_id,)).fetchone() is None,
                    "Prebinding video journal contains admitted data")

    def _retire_prebinding_source(self):
        if self.source_mode != VIDEO_SOURCE_MODE:
            return
        _retire_empty_prebinding_source(
            self.root, self.evidence, self._recording_id, self.source_spool,
            max_frames=self._source_spool_max_frames,
            max_bytes=self._source_spool_max_bytes)
        self.source_spool = None

    def _abort_prebinding(self):
        """Idempotently abort a v2 binding while retaining a tiny tombstone."""
        if self.source_mode != VIDEO_SOURCE_MODE or self._recording_id is None:
            return
        with self._condition:
            self._state = "aborting"
            _require(not self._queue and not self._finalizer_queue,
                     "Prebinding queue is not empty")
        try:
            self.journal.transaction(lambda connection: connection.execute(
                "UPDATE sessions SET binding_state='aborting' WHERE recording_id=? "
                "AND binding_state='preparing'", (self._recording_id,)))
            self._prebinding_g2_empty()
            self._retire_prebinding_source()
            _remove_empty_prebinding_work(self.root, self._recording_id)
            _require(self._spool_reservation is not None
                     and self._encoding_reservation is not None
                     and self._journal_reservation is not None,
                     "Prebinding reservations are unavailable")
            self._spool_reservation.close()
            self._encoding_reservation.close()
            self._spool_reservation = self._encoding_reservation = None
            self._journal_reservation.commit(256 * 1024)
            self.journal.transaction(lambda connection: connection.execute(
                "UPDATE sessions SET state='aborted', binding_state='aborted', "
                "failure_reason='prebinding_failed' WHERE recording_id=?",
                (self._recording_id,)))
            self._state = "aborted"
            self._unlock_writer()
        except Exception:
            # The aborting row and all unproven reservations remain durable for
            # restart reconciliation; never release a charge on uncertain cleanup.
            raise

    @_state_locked
    def _record_loss(self, loss_class, stage, reason, start_offset_ms,
                     end_offset_ms, acquisition_sequence=None,
                     segment_index=None, native_sequence_range=None):
        if self._manifest_sealed:
            return
        _require(loss_class in {
            "not-acquired", "captured-dropped",
            "encoder-accepted-not-durable", "unknown-acquisition-interval",
        }, "Invalid video loss class")
        _require(stage in {"native", "transport", "queue", "encoder", "timing"},
                 "Invalid video loss stage")
        _identifier(reason, "video loss reason")
        _integer(start_offset_ms, "loss interval start", 0, 600_000)
        _integer(end_offset_ms, "loss interval end", start_offset_ms, 600_000)
        item = {
            "lossClass": loss_class,
            "stage": stage,
            "reason": reason,
            "recordingInterval": {
                "startOffsetMs": start_offset_ms,
                "endOffsetMs": end_offset_ms,
            },
        }
        if acquisition_sequence is not None:
            item["acquisitionSequence"] = _integer(
                acquisition_sequence, "loss acquisition sequence", 1, 2 ** 63 - 1,
            )
            if self.source_mode == VIDEO_SOURCE_MODE:
                source = self._session.video_source(acquisition_sequence)
                _require(source is not None, "Video loss does not identify an admitted source")
                sequence = source["recordingFrameSequence"]
                item["recordingFrameRange"] = {"first": sequence, "last": sequence}
        if segment_index is not None:
            item["segmentIndex"] = _integer(
                segment_index, "loss segment index", 1, self.limits.max_segments,
            )
        if native_sequence_range is not None:
            _require(self.source_mode == VIDEO_SOURCE_MODE and acquisition_sequence is None,
                     "Native gap cannot cover an admitted source")
            item["nativeSequenceRange"] = dict(native_sequence_range)

        def insert(connection):
            sequence = int(connection.execute(
                "SELECT COALESCE(MAX(loss_sequence), 0) + 1 FROM losses WHERE recording_id = ?",
                (self._recording_id,),
            ).fetchone()[0])
            _require(sequence <= MAX_VIDEO_LOSSES, "Video loss limit reached")
            connection.execute(
                "INSERT INTO losses(recording_id, loss_sequence, loss_json) VALUES (?, ?, ?)",
                (self._recording_id, sequence,
                 _json_bytes(item, MAX_FRAME_METADATA_BYTES,
                             "Video loss is too large").decode("utf-8")),
            )
        self.journal.transaction(insert)
        self._losses.append(item)

    @_state_locked
    def declare_loss(self, stage, *, reason, start_offset_ms, end_offset_ms,
                     acquisition_sequence=None):
        _require(stage in _LOSS_STAGES, "Invalid video loss stage")
        with self._condition:
            _require(self._state == "recording", "Video admission is closed")
        self._record_loss(
            _LOSS_STAGES[stage], stage, reason, start_offset_ms, end_offset_ms,
            acquisition_sequence=acquisition_sequence,
        )

    @_state_locked
    def accept_frame(self, publication, body):
        with self._condition:
            _require(self._state == "recording", "Video admission is closed")
        lineage = self._session.video_frame_lineage(publication)
        if self.source_mode == VIDEO_SOURCE_MODE:
            source = self._session.video_source(publication.acquisition_sequence)
            if "nativeSequenceGap" in source:
                interval = source["nativeGapInterval"]
                self._record_loss("captured-dropped", "transport", "native-frame-gap",
                    interval["startOffsetMs"], interval["endOffsetMs"],
                    native_sequence_range=source["nativeSequenceGap"])
        frame = EncoderFrame.from_publication(
            publication,
            provider_incarnation=lineage["providerIncarnation"],
            native_incarnation=lineage["nativeIncarnation"],
        )
        try:
            _validate_encoder_frame(frame, self.limits)
            _require(type(body) is bytes and len(body) == frame.bytes
                     and hashlib.sha256(body).hexdigest() == frame.digest,
                     "Frame bytes differ from durable publication")
            if self.source_mode == VIDEO_SOURCE_MODE:
                _require(self.read_frame_source(frame) == body, "Video source publication is unavailable")
            else:
                reference = self.evidence.lookup(frame.digest)
                _require(reference is not None and reference.path == frame.path
                         and reference.bytes == frame.bytes
                         and self.evidence.read(frame.digest) == body,
                         "Frame publication is unavailable")
        except (VideoProtocolError, EvidenceStoreError):
            self._record_loss(
                "captured-dropped", "transport", "decode-incompatible",
                frame.offset_ms, frame.offset_ms,
                acquisition_sequence=frame.acquisition_sequence,
            )
            return False
        pin_id = "video_" + hashlib.sha256(
            f"{self._recording_id}:source:{frame.digest}".encode("ascii")
        ).hexdigest()[:48]
        pin = self._pins.get(frame.digest)
        new_pin = pin is None and self.source_mode == ORIGINAL_FRAME_MODE
        try:
            if new_pin:
                pin = self.evidence.pin(frame.digest, pin_id, "finalizer")
        except EvidenceStoreError:
            self._record_loss(
                "captured-dropped", "transport", "source-pin-unavailable",
                frame.offset_ms, frame.offset_ms,
                acquisition_sequence=frame.acquisition_sequence,
            )
            return False
        with self._condition:
            if (self._state != "recording"
                    or len(self._queue) >= self.limits.max_queue_frames
                    or self._queued_bytes + frame.bytes > self.limits.max_queue_bytes):
                if new_pin:
                    pin.close()
                self._record_loss(
                    "captured-dropped", "queue", "queue-over-limit",
                    frame.offset_ms, frame.offset_ms,
                    acquisition_sequence=frame.acquisition_sequence,
                )
                if self.backpressure_policy == "pause":
                    raise VideoProtocolError("Video queue backpressure requires pause")
                return False

            def insert(connection):
                connection.execute(
                    """INSERT INTO frames
                       (recording_id, acquisition_sequence, frame_json, pin_id, state)
                       VALUES (?, ?, ?, ?, 'queued')""",
                    (self._recording_id, frame.acquisition_sequence,
                     self._frame_json(frame), pin_id),
                )
            try:
                self.journal.transaction(insert)
            except Exception:
                if new_pin:
                    pin.close()
                raise
            if pin is not None:
                self._pins[frame.digest] = pin
            self._queue.append(frame)
            self._queued_bytes += frame.bytes
            self._admitted_frame_count += 1
            if frame.timing_source == "native-unmapped":
                self._record_loss(
                    "unknown-acquisition-interval", "timing",
                    "native-acquisition-unknown", frame.offset_ms, frame.offset_ms,
                    acquisition_sequence=frame.acquisition_sequence,
                )
            self._condition.notify_all()
            return True

    def invalidate_clock_mapping(self):
        with self._condition:
            _require(self._state == "recording", "Video admission is closed")
            if self._queue and self._queue[-1] == "clock-discontinuity":
                return
            _require(len(self._queue) <= self.limits.max_queue_frames,
                     "Video control queue limit reached")
            self._queue.append("clock-discontinuity")
            self._condition.notify_all()

    def _worker_main(self):
        try:
            while True:
                with self._condition:
                    while not self._queue:
                        self._condition.wait()
                    item = self._queue.popleft()
                    if type(item) is EncoderFrame:
                        self._queued_bytes -= item.bytes
                        self._condition.notify_all()
                if item == "stop":
                    if self._current:
                        self._seal_current("stop-barrier")
                    return
                if item == "clock-discontinuity":
                    if self._current:
                        self._seal_current("clock-discontinuity")
                    continue
                frame = item
                if self._cancelled.is_set():
                    self._record_loss(
                        "captured-dropped", "encoder", "finalizer-cancelled",
                        frame.offset_ms, frame.offset_ms,
                        acquisition_sequence=frame.acquisition_sequence,
                    )
                    continue
                reason = None
                if self._current:
                    first = self._current[0]
                    if frame.geometry != first.geometry:
                        reason = "geometry-change"
                    elif frame.clock_segment != first.clock_segment:
                        reason = "clock-discontinuity"
                    elif frame.offset_ms <= self._current[-1].offset_ms:
                        reason = "timestamp-collision"
                    elif frame.offset_ms - first.offset_ms >= self.limits.segment_duration_ms:
                        reason = "duration-bound"
                    elif len(self._current) >= self.limits.max_frames_per_segment:
                        reason = "frame-count-bound"
                    elif sum(item.bytes for item in self._current) + frame.bytes > self.limits.max_compressed_segment_bytes:
                        reason = "compressed-byte-bound"
                if reason is not None:
                    self._seal_current(reason)
                    if self._cancelled.is_set():
                        self._record_loss(
                            "captured-dropped", "encoder",
                            "finalizer-cancelled", frame.offset_ms,
                            frame.offset_ms,
                            acquisition_sequence=frame.acquisition_sequence,
                        )
                        continue
                self._current.append(frame)
        except Exception:
            with self._condition:
                self._cancelled.set()
                self._condition.notify_all()
            try:
                frames = list(self._current)
                self._current.clear()
                with self._condition:
                    frames.extend(
                        item for item in self._queue if type(item) is EncoderFrame
                    )
                for frame in frames:
                    self._record_loss(
                        "captured-dropped", "encoder", "video-worker-failed",
                        frame.offset_ms, frame.offset_ms,
                        acquisition_sequence=frame.acquisition_sequence,
                    )
            except Exception:
                pass

        finally:
            self._worker_done.set()
            if self._finalized is not None or self._close_requested:
                self._release_runtime_resources()
            self._maybe_close_after_workers()

    def _segment_manifest(self, frames, reference, rotation_reason, index):
        return _segment_manifest_value(
            frames, reference, rotation_reason, index,
        )

    def _publish_file(self, path, digest, size, reservation):
        writer = self.evidence.begin_blob(
            digest, size, owner=self._recording_id,
            retention_class="original",
            retain_until_ms=(
                int(time.time() * 1000)
                + self._session.registration.collection_policy[
                    "retentionSeconds"
                ]["original"] * 1000
            ),
            reservation=reservation,
        )
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    writer.write(chunk)
            return writer.publish()
        except Exception:
            if not writer._closed:
                writer.abort()
            raise

    @staticmethod
    def _remove_work(directory):
        try:
            if not directory.exists() and not directory.is_symlink():
                _fsync_directory(directory.parent)
                return True
            if directory.is_symlink() or not directory.is_dir():
                return False
            for child in directory.iterdir():
                if child.is_file() and not child.is_symlink():
                    child.unlink()
                else:
                    return False
            directory.rmdir()
            _fsync_directory(directory.parent)
            return True
        except OSError:
            return False

    def _seal_current(self, rotation_reason):
        frames = tuple(self._current)
        self._current.clear()
        if not frames:
            return
        if self._cancelled.is_set():
            for frame in frames:
                self._record_loss(
                    "captured-dropped", "encoder", "finalizer-cancelled",
                    frame.offset_ms, frame.offset_ms,
                    acquisition_sequence=frame.acquisition_sequence,
                )
            return
        with self._condition:
            wait_deadline = time.monotonic() + 0.1
            while (len(self._finalizer_queue) >= 1
                   and not self._finalizer_stop
                   and not self._cancelled.is_set()):
                remaining = wait_deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(0.05, remaining))
            if (self._finalizer_stop or self._cancelled.is_set()
                    or self._finalizer is None
                    or self._finalizer_done.is_set()):
                for frame in frames:
                    self._record_loss(
                        "captured-dropped", "encoder", "finalizer-cancelled",
                        frame.offset_ms, frame.offset_ms,
                        acquisition_sequence=frame.acquisition_sequence,
                    )
                return
            if len(self._finalizer_queue) >= 1:
                for frame in frames:
                    self._record_loss(
                        "captured-dropped", "queue", "queue-over-limit",
                        frame.offset_ms, frame.offset_ms,
                        acquisition_sequence=frame.acquisition_sequence,
                    )
                self._cancelled.set()
                self._condition.notify_all()
                return
        self._segment_index += 1
        index = self._segment_index
        if index > self.limits.max_segments:
            for frame in frames:
                self._record_loss(
                    "captured-dropped", "queue", "segment-limit",
                    frame.offset_ms, frame.offset_ms,
                    acquisition_sequence=frame.acquisition_sequence,
                )
            self._cancelled.set()
            return
        reservation = None
        work = self.work / f"segment_{index:03d}"
        task = None
        try:
            reservation = self.evidence.budget.reserve(
                self._recording_id,
                "finalization",
                self.limits.max_segment_bytes + OBJECT_METADATA_BYTES,
            )
            _owned_directory(work)
            lineage = [frame.manifest_value(frames[0].offset_ms) for frame in frames]

            def sealing(connection):
                connection.execute(
                    """INSERT INTO segments
                       (recording_id, segment_index, state, rotation_reason,
                        lineage_json, reservation_id)
                       VALUES (?, ?, 'sealing', ?, ?, ?)""",
                    (self._recording_id, index, rotation_reason,
                     _json_bytes(lineage, 512 * 1024,
                                 "Segment lineage is too large").decode("utf-8"),
                     reservation.reservation_id),
                )
                connection.executemany(
                    """UPDATE frames SET state = 'assigned', segment_index = ?
                         WHERE recording_id = ? AND acquisition_sequence = ?""",
                    [(index, self._recording_id, frame.acquisition_sequence)
                     for frame in frames],
                )
            self.journal.transaction(sealing)
            task = _SealedSegment(frames, rotation_reason, index, reservation, work)
            with self._condition:
                if (self._finalizer_stop or self._finalizer_done.is_set()
                        or len(self._finalizer_queue) >= 1):
                    self._discard_sealed_segment(task, "finalizer-cancelled")
                    self._cancelled.set()
                    return
                self._finalizer_queue.append(task)
                self._condition.notify_all()
        except DiskBudgetError:
            for frame in frames:
                self._record_loss(
                    "captured-dropped", "encoder", "video-storage-exhausted",
                    frame.offset_ms, frame.offset_ms,
                    acquisition_sequence=frame.acquisition_sequence,
                )
            self._cancelled.set()
            return
        except Exception:
            if task is not None:
                self._discard_sealed_segment(task, "video-worker-failed")
                self._cancelled.set()
                return
            if reservation is not None:
                try:
                    self.evidence.abandon_reservation(
                        reservation.reservation_id, release=True,
                    )
                except EvidenceStoreError:
                    pass
                self.evidence.release_unused_reservation(reservation.reservation_id)
            for frame in frames:
                self._record_loss(
                    "captured-dropped", "encoder", "video-worker-failed",
                    frame.offset_ms, frame.offset_ms,
                    acquisition_sequence=frame.acquisition_sequence,
                )
            self._cancelled.set()
            return

    def _discard_sealed_segment(self, task, reason="finalizer-cancelled"):
        for frame in task.frames:
            self._record_loss(
                "captured-dropped", "encoder", reason,
                frame.offset_ms, frame.offset_ms,
                acquisition_sequence=frame.acquisition_sequence,
                segment_index=task.index,
            )
        try:
            self.journal.transaction(lambda connection: (
                connection.execute(
                    """UPDATE segments SET state = 'failed'
                         WHERE recording_id = ? AND segment_index = ?""",
                    (self._recording_id, task.index),
                ),
                connection.executemany(
                    """UPDATE frames SET state = 'dropped'
                         WHERE recording_id = ? AND acquisition_sequence = ?""",
                    [(self._recording_id, frame.acquisition_sequence)
                     for frame in task.frames],
                ),
            ))
        except Exception:
            pass
        try:
            self.evidence.abandon_reservation(
                task.reservation.reservation_id, release=True,
            )
        except EvidenceStoreError:
            pass
        self.evidence.release_unused_reservation(task.reservation.reservation_id)
        if not self._remove_work(task.work):
            self._work_cleanup_unconfirmed = True

    def _finalizer_main(self):
        active_task = None
        try:
            while True:
                with self._condition:
                    while not self._finalizer_queue and not self._finalizer_stop:
                        self._condition.wait()
                    if not self._finalizer_queue and self._finalizer_stop:
                        return
                    task = self._finalizer_queue.popleft()
                    active_task = task
                    self._finalizer_active = True
                    self._condition.notify_all()
                try:
                    self._finalize_segment(task)
                except Exception:
                    raise
                else:
                    active_task = None
                finally:
                    with self._condition:
                        self._finalizer_active = False
                        self._condition.notify_all()
        except Exception:
            with self._condition:
                self._cancelled.set()
                pending = list(self._finalizer_queue)
                self._finalizer_queue.clear()
                self._condition.notify_all()
            if active_task is not None:
                self._discard_sealed_segment(active_task, "video-finalizer-failed")
            for task in pending:
                self._discard_sealed_segment(task, "video-finalizer-failed")
        finally:
            self._finalizer_done.set()
            self._maybe_close_after_workers()

    def _maybe_close_after_workers(self):
        with self._condition:
            if (not self._close_requested or self._close_complete
                    or not self._worker_done.is_set()
                    or not self._finalizer_done.is_set()):
                return
            self._close_complete = True
        self._release_runtime_resources()
        self.journal.close()
        self._unlock_writer()

    def _finalize_segment(self, task):
        frames = task.frames
        rotation_reason = task.rotation_reason
        index = task.index
        reservation = task.reservation
        work = task.work
        accepted = ()
        finalizer_claimed = False
        if self._cancelled.is_set():
            self._discard_sealed_segment(task)
            return
        try:
            finalizer_claimed = self._claim_process_finalizer()
            if not finalizer_claimed:
                raise VideoEncodingError("finalizer-over-limit")
            ownership = ({"ownership_fd": self._writer_fd}
                         if isinstance(self.encoder, AVFoundationSegmentEncoder) else {})
            result = self.encoder.encode(
                frames, work,
                timeout_seconds=self.limits.finalization_timeout_seconds,
                fault_mode=self.fault_mode,
                **ownership,
            )
            accepted = tuple(result.accepted_sequences)
            _require(accepted == tuple(frame.acquisition_sequence for frame in frames),
                     "Encoder did not accept every segment frame")
            expected_path = work / "segment.mp4"
            _require(Path(result.path) == expected_path,
                     "Encoder returned an unexpected output path")
            stat = expected_path.lstat()
            _require(expected_path.is_file() and not expected_path.is_symlink()
                     and stat.st_uid == os.getuid()
                     and stat.st_size == result.bytes
                     and 0 < result.bytes <= self.limits.max_segment_bytes
                     and result.codec == "h264" and result.container == "mp4",
                     "Encoder output is invalid")
            digest = hashlib.sha256(expected_path.read_bytes()).hexdigest()
            _require(digest == result.digest, "Encoder output digest differs")
            current_total = sum(segment["bytes"] for segment in self._segments)
            _require(current_total + result.bytes <= self.limits.max_total_video_bytes,
                     "Total video byte limit exceeded")
            _require(not self._cancelled.is_set(), "Video finalization was cancelled")
            self.journal.transaction(lambda connection: connection.execute(
                """UPDATE segments SET expected_digest = ?, expected_bytes = ?
                     WHERE recording_id = ? AND segment_index = ?
                       AND state = 'sealing'""",
                (result.digest, result.bytes, self._recording_id, index),
            ))
            reference = self._publish_file(
                expected_path, result.digest, result.bytes, reservation,
            )
            segment = self._segment_manifest(
                frames, reference, rotation_reason, index,
            )

            def durable(connection):
                connection.execute(
                    """UPDATE segments SET state = 'durable', reference_json = ?,
                              manifest_json = ?
                         WHERE recording_id = ? AND segment_index = ?
                           AND state = 'sealing'""",
                    (_json_bytes({
                        "digest": reference.digest,
                        "bytes": reference.bytes,
                        "path": reference.path,
                    }, MAX_FRAME_METADATA_BYTES,
                        "Segment reference is too large").decode("utf-8"),
                     _json_bytes(segment, 512 * 1024,
                                 "Segment manifest is too large").decode("utf-8"),
                     self._recording_id, index),
                )
                connection.executemany(
                    """UPDATE frames SET state = 'durable'
                         WHERE recording_id = ? AND acquisition_sequence = ?""",
                    [(self._recording_id, frame.acquisition_sequence)
                     for frame in frames],
                )
            with self._condition:
                _require(not self._cancelled.is_set() and not self._manifest_sealed,
                         "Video finalization was cancelled")
                if reference.digest not in self._segment_pins:
                    pin_id = "video_media_" + hashlib.sha256(
                        self._recording_id.encode("ascii")
                    ).hexdigest()[:40]
                    self._segment_pins[reference.digest] = self.evidence.pin(
                        reference.digest, pin_id, "finalizer",
                    )
                self.journal.transaction(durable)
                self._segments.append(segment)
            if self.source_spool is not None:
                try:
                    self.source_spool.release(tuple(frame.source_token for frame in frames),
                                              self._source_release_proof)
                except (ContractError, OSError):
                    # The MP4 and its lineage are already durable. Preserve
                    # that outcome while retaining the unconfirmed spool charge.
                    self._work_cleanup_unconfirmed = True
                    self._cancelled.set()
        except Exception as error:
            accepted = tuple(getattr(error, "accepted_sequences", accepted))
            reason = getattr(error, "reason", None)
            if reason not in {
                "finalization-timeout", "helper-stream-limit",
                "encoder-process-failed", "encoder-output-missing",
                "codec-unavailable", "helper-error-output",
                "finalizer-over-limit",
            }:
                reason = "encoder-failed"
            for frame in frames:
                loss_class = (
                    "encoder-accepted-not-durable"
                    if frame.acquisition_sequence in accepted
                    else "captured-dropped"
                )
                self._record_loss(
                    loss_class, "encoder", reason,
                    frames[0].offset_ms, frames[-1].offset_ms,
                    acquisition_sequence=frame.acquisition_sequence,
                    segment_index=index,
                )

            def failed(connection):
                connection.execute(
                    """UPDATE segments SET state = 'failed'
                         WHERE recording_id = ? AND segment_index = ?""",
                    (self._recording_id, index),
                )
                connection.executemany(
                    """UPDATE frames SET state = ?
                         WHERE recording_id = ? AND acquisition_sequence = ?""",
                    [("accepted" if frame.acquisition_sequence in accepted else "dropped",
                      self._recording_id, frame.acquisition_sequence)
                     for frame in frames],
                )
            self.journal.transaction(failed)
            try:
                self.evidence.abandon_reservation(
                    reservation.reservation_id, release=True,
                )
            except EvidenceStoreError:
                pass
            self.evidence.release_unused_reservation(reservation.reservation_id)
            self._cancelled.set()
        finally:
            if finalizer_claimed:
                self._release_process_finalizer()
            if not self._remove_work(work):
                self._work_cleanup_unconfirmed = True
                self._cancelled.set()

    def _source_release_proof(self, tokens):
        identities = [(token.frame_sequence, token.acquisition_sequence, token.digest) for token in tokens]
        for segment in self._segments:
            expected = [(frame["recordingFrameSequence"], frame["acquisitionSequence"], frame["sourceDigest"])
                        for frame in segment["frames"]]
            if identities != expected:
                continue
            with self.journal._lock:
                row = self.journal.connection.execute(
                    "SELECT state,manifest_json FROM segments WHERE recording_id=? AND segment_index=?",
                    (self._recording_id, segment["segmentIndex"])).fetchone()
            _require(row is not None and row["state"] == "durable"
                     and json.loads(row["manifest_json"]) == segment,
                     "Source segment journal is not durable")
            body = self.evidence.read(segment["digest"])
            _require(len(body) == segment["bytes"], "Source segment is unavailable")
            return {"kind": "video-segment", "recordingId": self._recording_id,
                    "digest": segment["digest"], "frames": [item[0] for item in identities]}
        _require(self._source_manifest is not None and self._source_manifest_attached,
                 "Source outcome manifest has not been attached")
        manifest, reference = self._source_manifest
        _require(self.evidence.read(reference.digest) == _json_bytes(manifest, MAX_VIDEO_MANIFEST_BYTES,
                     "Video manifest is too large"), "Source outcome manifest is unavailable")
        ledger = {item["sequence"]: item for item in manifest["sourceFrames"]}
        for token in tokens:
            item = ledger.get(token.frame_sequence)
            if item is not None:
                _require(item["acquisitionSequence"] == token.acquisition_sequence
                         and item["digest"] == token.digest, "Source outcome identity differs")
            else:
                _require(token.frame_sequence > len(ledger)
                         and self._session.video_source(token.acquisition_sequence) is None,
                         "Source was admitted outside the frozen outcome ledger")
        return {"kind": "video-manifest", "recordingId": self._recording_id,
                "digest": reference.digest, "frames": [item[0] for item in identities]}

    def _retire_source_spool(self):
        if self.source_spool is None:
            return
        self.source_spool.recover(self._source_release_proof)
        tokens = self.source_spool.get_active_tokens()
        for offset in range(0, len(tokens), 256):
            self.source_spool.release(tokens[offset:offset + 256], self._source_release_proof)
        self.source_spool.finalcleanup()
        self._session.complete_source_cleanup()

    def _event_mappings(self, segments):
        return _event_mappings(self._session.video_event_snapshot(), segments, self._losses)

    def _manifest_body(self, failure_reason):
        value = {
            "schemaVersion": 2 if self.source_mode == VIDEO_SOURCE_MODE else 1,
            "kind": "reproloop-avfoundation-video",
            "recordingId": self._recording_id,
            "status": "incomplete" if self._losses or failure_reason else "complete",
            "failureReason": failure_reason,
            "codec": "h264",
            "container": "mp4",
            "samplingMode": (
                "no-acquired-frames" if (
                    self._admitted_frame_count == 0
                    and not any("acquisitionSequence" in loss
                                for loss in self._losses)
                )
                else "irregular-source-capture"
            ),
            "segmentDurationBoundMs": self.limits.segment_duration_ms,
            "segments": list(self._segments),
            "losses": list(self._losses),
            "eventMappings": self._event_mappings(self._segments),
            "limits": _limits_manifest(self.limits, source_mode=self.source_mode),
        }
        if self.source_mode == VIDEO_SOURCE_MODE:
            value["sourceDisposition"] = "transient-after-durable-outcome"
            value["sourceFrames"] = [{"sequence": source["recordingFrameSequence"],
                "acquisitionSequence": source["acquisitionSequence"], "digest": source["digest"]}
                for source in self._session.video_sources()]
        return value

    def _record_unfinished_frames(self, reason):
        known = {
            item.get("acquisitionSequence") for item in self._losses
            if item.get("acquisitionSequence") is not None
        }
        with self.journal._lock:
            rows = self.journal.connection.execute(
                """SELECT acquisition_sequence, frame_json, state, segment_index
                     FROM frames WHERE recording_id = ? AND state != 'durable'
                     ORDER BY acquisition_sequence""",
                (self._recording_id,),
            ).fetchall()
        for row in rows:
            if row["acquisition_sequence"] in known:
                continue
            frame = json.loads(row["frame_json"])
            self._record_loss(
                ("encoder-accepted-not-durable"
                 if row["state"] == "accepted" else "captured-dropped"),
                "encoder", reason, frame["offsetMs"], frame["offsetMs"],
                acquisition_sequence=row["acquisition_sequence"],
                segment_index=row["segment_index"],
            )

    def _publish_manifest(self, body):
        encoded = _json_bytes(body, MAX_VIDEO_MANIFEST_BYTES if body.get("schemaVersion") == 2 else 4 * 1024 * 1024,
                              "Video manifest is too large")
        digest = hashlib.sha256(encoded).hexdigest()
        reservation = self.evidence.budget.reserve(
            self._recording_id, "finalization",
            len(encoded) + OBJECT_METADATA_BYTES,
        )
        reference = self.evidence.put_bytes(
            encoded, owner=self._recording_id, retention_class="original",
            retain_until_ms=(
                int(time.time() * 1000)
                + self._session.registration.collection_policy[
                    "retentionSeconds"
                ]["original"] * 1000
            ),
            reservation=reservation,
        )
        _require(reference.digest == digest, "Video manifest digest differs")
        return reference

    def finalize(self):
        with self._condition:
            if self._finalized is not None:
                return self._finalized
            _require(self._state == "recording", "Video cannot be finalized")
            self._state = "finalizing"
            self.journal.transaction(lambda connection: connection.execute(
                "UPDATE sessions SET state = 'finalizing' WHERE recording_id = ?",
                (self._recording_id,),
            ))
            self._queue.append("stop")
            self._condition.notify_all()
        deadline = time.monotonic() + self.limits.finalization_timeout_seconds + 0.5
        self._worker.join(max(0.0, deadline - time.monotonic()))
        with self._condition:
            self._finalizer_stop = True
            self._condition.notify_all()
        self._finalizer.join(max(0.0, deadline - time.monotonic()))
        failure_reason = None
        if self._worker.is_alive() or self._finalizer.is_alive():
            with self._condition:
                self._cancelled.set()
                self._condition.notify_all()
            failure_reason = "finalization-timeout"
            self._record_unfinished_frames("finalization-timeout")
        elif self._cancelled.is_set():
            reasons = [loss["reason"] for loss in self._losses]
            failure_reason = (
                "finalization-timeout" if "finalization-timeout" in reasons
                else reasons[0] if reasons else "video-finalization-failed"
            )
        if self._work_cleanup_unconfirmed:
            failure_reason = "work-cleanup-unconfirmed"
        if self._admitted_frame_count == 0 and not self._losses:
            barrier = self._session.video_barrier_snapshot()
            self._record_loss(
                "not-acquired", "native", "zero-frames",
                0, barrier["offsetMs"],
            )
            failure_reason = failure_reason or "no-video-frames"
        with self._condition:
            self._manifest_sealed = True
            body = validate_video_manifest(self._manifest_body(failure_reason))
        reference = self._publish_manifest(body)
        if self.source_mode == VIDEO_SOURCE_MODE:
            self._source_manifest = (body, reference)
        self.evidence.budget.commit(
            self._journal_reservation,
            actual_bytes=self._journal_reservation.bytes,
        )
        artifacts, timings = _video_artifacts(body, reference,
            self._session.video_barrier_snapshot()["offsetMs"])
        incomplete_reason = (
            "video_incomplete" if body["status"] == "incomplete" else None
        )
        self._session.attach_finalized_video(
            artifacts, timings, incomplete_reason=incomplete_reason,
            source_manifest=body if self.source_mode == VIDEO_SOURCE_MODE else None,
        )
        self._source_manifest_attached = self.source_mode == VIDEO_SOURCE_MODE
        enriched = dict(body)
        enriched["manifestDigest"] = reference.digest
        enriched["manifestPath"] = reference.path
        self.journal.transaction(lambda connection: connection.execute(
            """UPDATE sessions SET state = ?, manifest_json = ?,
                      manifest_digest = ?, manifest_reference_json = ?,
                      failure_reason = ?
                 WHERE recording_id = ?""",
            ("frozen-incomplete" if body["status"] == "incomplete"
             else "frozen-complete",
             _json_bytes(body, MAX_VIDEO_MANIFEST_BYTES if self.source_mode == VIDEO_SOURCE_MODE else 4 * 1024 * 1024,
                         "Video manifest is too large").decode("utf-8"),
             reference.digest,
             _json_bytes({
                 "digest": reference.digest,
                 "bytes": reference.bytes,
                 "path": reference.path,
             }, MAX_FRAME_METADATA_BYTES,
                 "Video manifest reference is too large").decode("utf-8"),
             failure_reason, self._recording_id),
        ))
        self._state = "frozen"
        self._finalized = enriched
        if self.source_spool is not None and self._worker_done.is_set() and self._finalizer_done.is_set():
            self._retire_source_spool()
        self._release_runtime_resources()
        self._remove_empty_recording_work()
        return enriched

    def manifest(self):
        _require(self._finalized is not None, "Video manifest is not finalized")
        return json.loads(json.dumps(self._finalized))

    def close(self):
        if self._close_complete:
            return
        self._close_requested = True
        if self._state == "aborting":
            try:
                self._abort_prebinding()
            except Exception:
                try:
                    if (self.source_spool is not None
                            and not self.source_spool._closed):
                        self.source_spool.close()
                finally:
                    try:
                        self.journal.close()
                    finally:
                        self._unlock_writer()
                        self._close_complete = True
                return
        if self._state == "aborted":
            self.journal.close()
            self._unlock_writer()
            self._close_complete = True
            return
        if self._state in {"recording", "finalizing"}:
            with self._condition:
                self._cancelled.set()
                self._condition.notify_all()
        deadline = time.monotonic() + self.limits.finalization_timeout_seconds + 0.5
        if self._worker is not None and self._worker.is_alive():
            with self._condition:
                self._queue.append("stop")
                self._condition.notify_all()
            self._worker.join(max(0.0, deadline - time.monotonic()))
        with self._condition:
            self._finalizer_stop = True
            self._condition.notify_all()
        if self._finalizer is not None and self._finalizer.is_alive():
            self._finalizer.join(max(0.0, deadline - time.monotonic()))
        if ((self._worker is not None and not self._worker_done.is_set())
                or (self._finalizer is not None and not self._finalizer_done.is_set())):
            return
        self._release_runtime_resources()
        self._remove_empty_recording_work()
        self.journal.close()
        self._unlock_writer()
        self._close_complete = True

    @_state_locked
    def _release_runtime_resources(self):
        if self._state == "aborting":
            return
        if ((self._worker is not None and not self._worker_done.is_set())
                or (self._finalizer is not None and not self._finalizer_done.is_set())):
            return
        if self.source_spool is not None and not self.source_spool._closed:
            try:
                if self._source_manifest_attached:
                    self._retire_source_spool()
                elif self.source_spool.snapshot()["activeCount"] == 0:
                    self.source_spool.finalcleanup()
            except (ContractError, OSError):
                # Failed source retirement keeps its own durable reservation
                # and proof for catalog recovery after the writer is closed.
                self._work_cleanup_unconfirmed = True
            finally:
                self.source_spool.close()
        for pins in (self._pins, self._segment_pins):
            for digest, pin in list(pins.items()):
                try:
                    pin.close()
                except EvidenceStoreError:
                    continue
                del pins[digest]
        cleaned = True
        if self.work is not None and self.work.exists():
            for directory in self.work.iterdir():
                cleaned = self._remove_work(directory) and cleaned
            if cleaned:
                try:
                    _fsync_directory(self.work)
                except OSError:
                    cleaned = False
        self._work_cleanup_unconfirmed = not cleaned
        if not cleaned:
            return
        for reservation in (self._spool_reservation, self._encoding_reservation):
            try:
                reservation.close()
            except (AttributeError, DiskBudgetError):
                pass

    def _remove_empty_recording_work(self):
        if self.work is None or self._state == "aborting":
            return
        try:
            if (self.work.is_dir() and not self.work.is_symlink()
                    and not any(self.work.iterdir())):
                self.work.rmdir()
                _fsync_directory(self._work_root)
        except OSError:
            pass

    def _unlock_writer(self):
        if self._writer_fd is not None:
            fcntl.flock(self._writer_fd, fcntl.LOCK_UN)
            os.close(self._writer_fd)
            self._writer_fd = None


def _event_mappings(events, segments, losses):
    mappings = []
    for event in events:
        covered = None
        in_gap = any(loss["recordingInterval"]["startOffsetMs"] <= event["offsetMs"]
                     <= loss["recordingInterval"]["endOffsetMs"] for loss in losses)
        for segment in (() if in_gap else segments):
            interval = segment["displayInterval"]
            if interval["startOffsetMs"] <= event["offsetMs"] <= interval["endOffsetMs"]:
                covered = {"kind": "segment", "segmentId": segment["segmentId"],
                           "presentationTimeMs": event["offsetMs"] - interval["startOffsetMs"]}
                break
        mappings.append({"eventId": event["eventId"], "eventSequence": event["sequence"],
                         "recordingOffsetMs": event["offsetMs"], "mapping": covered or {"kind": "gap"}})
    return mappings


def _video_artifacts(manifest, reference, barrier_offset_ms):
    artifacts, timings = [], []
    for segment in manifest["segments"]:
        artifacts.append({"id": segment["segmentId"], "digest": segment["digest"],
            "path": segment["path"], "bytes": segment["bytes"], "mimeType": "video/mp4"})
        capture, display = segment["captureInterval"], segment["displayInterval"]
        timings.append({
            "offsetMs": display["startOffsetMs"],
            "earliestOffsetMs": (capture["firstCapturedEarliestOffsetMs"] if capture is not None
                                 else display["startOffsetMs"]),
            "latestOffsetMs": (capture["lastCapturedLatestOffsetMs"] if capture is not None
                               else display["endOffsetMs"]),
            "uncertaintyNs": max((frame["uncertaintyNs"] for frame in segment["frames"]), default=0),
            "timingSource": "native-unmapped" if capture is None else segment["frames"][0]["timingSource"],
        })
    artifacts.append({"id": "video_manifest", "digest": reference.digest,
        "path": reference.path, "bytes": reference.bytes, "mimeType": VIDEO_MANIFEST_MIME})
    timings.append({"offsetMs": 0, "earliestOffsetMs": 0, "latestOffsetMs": barrier_offset_ms,
                    "uncertaintyNs": 0, "timingSource": "host-acquired"})
    return artifacts, timings


class VideoCatalog:
    """Read and validate completed G3 manifests without trusting filenames."""

    def __init__(self, root: Path, evidence: EvidenceStore):
        _require(type(evidence) is EvidenceStore, "Evidence store is required")
        self.evidence = evidence
        self.journal = _VideoJournal(Path(root), evidence)

    def load(self, recording_id):
        recording_id = _identifier(recording_id, "recording identity")
        with self.journal._lock:
            row = self.journal.connection.execute(
                "SELECT * FROM sessions WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
            _require(row is not None and row["manifest_json"] is not None
                     and row["manifest_digest"] is not None,
                     "Video manifest is unavailable")
            try:
                manifest = validate_video_manifest(
                    json.loads(row["manifest_json"])
                )
            except (TypeError, ValueError, UnicodeError):
                raise VideoProtocolError("Video manifest is corrupt") from None
        body = _json_bytes(manifest, MAX_VIDEO_MANIFEST_BYTES if manifest["schemaVersion"] == 2 else 4 * 1024 * 1024,
                           "Video manifest is too large")
        _require(hashlib.sha256(body).hexdigest() == row["manifest_digest"]
                 and self.evidence.read(row["manifest_digest"]) == body,
                 "Video manifest evidence differs")
        for segment in manifest["segments"]:
            value = self.evidence.read(segment["digest"])
            _require(len(value) == segment["bytes"]
                     and hashlib.sha256(value).hexdigest() == segment["digest"],
                     "Durable video segment differs")
        manifest["manifestDigest"] = row["manifest_digest"]
        manifest["manifestPath"] = json.loads(
            row["manifest_reference_json"]
        )["path"]
        return manifest

    def reconcile_prebindings(self, store, project_digest):
        """Reconcile only durable prebinding rows with no live writer."""
        _require(getattr(store, "evidence", None) is self.evidence,
                 "Recording store does not own this video evidence")
        metadata_reader = getattr(store, "prebinding_recording_metadata", None)
        _require(callable(metadata_reader),
                 "Public recording prebinding metadata is unavailable")
        recording_ids_reader = getattr(store, "prebinding_recording_ids", None)
        _require(callable(recording_ids_reader),
                 "Public recording prebinding identities are unavailable")
        _require(isinstance(project_digest, str) and
                 re.fullmatch(r"[0-9a-f]{64}", project_digest) is not None,
                 "Invalid recording project digest")
        with self.journal._lock:
            rows = self.journal.connection.execute(
                "SELECT * FROM sessions WHERE binding_state IN ('preparing','aborting') "
                "ORDER BY recording_id").fetchall()
        recording_ids = recording_ids_reader(project_digest)
        _require(type(recording_ids) is tuple
                 and len(recording_ids) == len(set(recording_ids))
                 and all(type(item) is str and _ID.fullmatch(item) is not None
                         for item in recording_ids),
                 "Recording prebinding identities differ")
        selected = set(recording_ids)
        reconciled = {"aborted": 0, "bound": 0,
                      "skipped": sum(row["recording_id"] not in selected
                                     for row in rows)}
        locks = _owned_directory(self.journal.root / "locks")
        for row in rows:
            recording_id = row["recording_id"]
            if recording_id not in selected:
                continue
            g2 = metadata_reader(recording_id, project_digest)
            _require(type(g2) is dict and set(g2) == {
                "recordingId", "projectDigest", "state", "sourceMode",
                "sourceConfig", "active",
            } and g2["recordingId"] == recording_id
                and g2["projectDigest"] == project_digest
                and type(g2["state"]) is str
                and g2["sourceMode"] in {ORIGINAL_FRAME_MODE, VIDEO_SOURCE_MODE}
                and type(g2["active"]) is bool,
                "Recording prebinding metadata differs")
            if g2["sourceMode"] == ORIGINAL_FRAME_MODE:
                _require(g2["sourceConfig"] is None,
                         "Legacy recording has transient source metadata")
            else:
                _require(type(g2["sourceConfig"]) is dict
                         and set(g2["sourceConfig"]) == {
                             "limits", "retainUntilMs"},
                         "Transient recording source metadata differs")
            if g2["active"]:
                reconciled["skipped"] += 1
                continue
            lock_path = locks / (hashlib.sha256(
                recording_id.encode("ascii")).hexdigest() + ".lock")
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT |
                                 getattr(os, "O_NOFOLLOW", 0), 0o600)
            recover_recording_id = None
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise VideoProtocolError("Live video recording cannot be reconciled") from None
                source_mode = g2["sourceMode"]
                if source_mode == ORIGINAL_FRAME_MODE:
                    self._reconcile_v1_prebinding(row)
                    reconciled["aborted"] += 1
                elif source_mode == VIDEO_SOURCE_MODE:
                    self._validate_reconciled_v2(row, g2)
                    self.journal.transaction(lambda connection, rid=recording_id:
                        connection.execute(
                            "UPDATE sessions SET binding_state='bound' WHERE recording_id=? "
                            "AND binding_state IN ('preparing','aborting')", (rid,)))
                    recover_recording_id = recording_id
                else:
                    raise VideoProtocolError("Unknown G2 video source mode")
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            if recover_recording_id is not None:
                recovery_factory = getattr(store, "recovery_session", None)
                _require(callable(recovery_factory),
                         "Transient recording recovery metadata is unavailable")
                self.recover_transient(
                    recovery_factory(recover_recording_id))
                reconciled["bound"] += 1
        return reconciled

    def _reconcile_v1_prebinding(self, row):
        recording_id = row["recording_id"]
        self.journal.transaction(lambda connection: connection.execute(
            "UPDATE sessions SET binding_state='aborting' WHERE recording_id=? "
            "AND binding_state='preparing'", (recording_id,)))
        with self.journal._lock:
            row = self.journal.connection.execute(
                "SELECT * FROM sessions WHERE recording_id=?",
                (recording_id,)).fetchone()
            _require(row is not None
                     and row["binding_state"] == "aborting",
                     "Legacy prebinding state changed")
            source_config = _validate_prebinding_source_config(
                self.journal.root, row)
            for table in ("frames", "segments", "losses"):
                _require(self.journal.connection.execute(
                    f"SELECT 1 FROM {table} WHERE recording_id=? LIMIT 1",
                    (recording_id,)).fetchone() is None,
                    "Legacy prebinding contains video data")
            _require(row["manifest_json"] is None and row["manifest_digest"] is None,
                     "Legacy prebinding contains a manifest")
        try:
            source_limits = VideoLimits(**source_config["limits"])
        except (TypeError, ValueError, ContractError):
            raise VideoProtocolError(
                "Historical prebinding source bounds are invalid") from None
        source_max_frames, source_max_bytes = _source_spool_bounds(source_limits)
        _retire_empty_prebinding_source(
            self.journal.root, self.evidence, recording_id,
            max_frames=source_max_frames, max_bytes=source_max_bytes)
        _remove_empty_prebinding_work(self.journal.root, recording_id)
        for reservation_id in (row["spool_reservation_id"],
                               row["encoding_reservation_id"]):
            _require(self.evidence.release_unused_reservation(reservation_id),
                     "Legacy prebinding reservation contains evidence")
        self.evidence.budget.commit_id(
            row["journal_reservation_id"], 256 * 1024)
        self.journal.transaction(lambda connection: connection.execute(
            "UPDATE sessions SET state='aborted', binding_state='aborted', "
            "failure_reason='prebinding_failed' WHERE recording_id=?",
            (recording_id,)))

    def _validate_reconciled_v2(self, row, g2):
        source_config = _validate_prebinding_source_config(
            self.journal.root, row)
        expected_g2 = {
            "limits": source_config["limits"],
            "retainUntilMs": source_config["retainUntilMs"],
        }
        _require(g2["sourceConfig"] == expected_g2,
                 "G2 transient source configuration changed")

    def recover_interrupted(self, recording_id):
        """Freeze an interrupted video journal without resuming acquisition.

        Already published segments are retained after digest validation.  Open
        or merely accepted frames become explicit loss evidence.  This method
        requires the per-recording writer lock, so it cannot recover a live
        producer in another process.
        """
        recording_id = _identifier(recording_id, "recording identity")
        locks = _owned_directory(self.journal.root / "locks")
        lock_path = locks / (
            hashlib.sha256(recording_id.encode("ascii")).hexdigest() + ".lock"
        )
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise VideoProtocolError(
                    "Live video recording cannot be recovered"
                ) from None
            with self.journal._lock:
                row = self.journal.connection.execute(
                    "SELECT * FROM sessions WHERE recording_id = ?",
                    (recording_id,),
                ).fetchone()
                _require(row is not None, "Video recording is unavailable")
                _require(row["source_mode"] == ORIGINAL_FRAME_MODE,
                         "Transient video recovery requires its recording ledger")
                try:
                    limits = VideoLimits(**json.loads(row["limits_json"]))
                except (TypeError, ValueError, VideoProtocolError):
                    raise VideoProtocolError("Stored video limits are invalid") from None
                if row["manifest_digest"] is not None:
                    self._clean_recording_work(recording_id, limits)
                    self._release_recovered_resources(recording_id, row)
                    return self.load(recording_id)
                _require(row["state"] in {"recording", "finalizing"},
                         "Video recording cannot be recovered")
                frame_rows = self.journal.connection.execute(
                    """SELECT * FROM frames WHERE recording_id = ?
                         ORDER BY acquisition_sequence""",
                    (recording_id,),
                ).fetchall()
                segment_rows = self.journal.connection.execute(
                    """SELECT * FROM segments WHERE recording_id = ?
                         ORDER BY segment_index""",
                    (recording_id,),
                ).fetchall()
                loss_rows = self.journal.connection.execute(
                    """SELECT loss_json FROM losses WHERE recording_id = ?
                         ORDER BY loss_sequence""",
                    (recording_id,),
                ).fetchall()
            frames_by_segment = {}
            decoded_frames = {}
            for frame_row in frame_rows:
                try:
                    frame = _frame_from_json(json.loads(frame_row["frame_json"]))
                except (TypeError, ValueError, VideoProtocolError):
                    raise VideoProtocolError("Stored frame lineage is invalid") from None
                _validate_encoder_frame(frame, limits)
                decoded_frames[frame.acquisition_sequence] = frame
                if frame_row["segment_index"] is not None:
                    frames_by_segment.setdefault(
                        frame_row["segment_index"], []
                    ).append(frame)
            segments = []
            retained_reservations = set()
            for segment_row in segment_rows:
                manifest = None
                if segment_row["state"] == "durable":
                    try:
                        manifest = json.loads(segment_row["manifest_json"])
                    except (TypeError, ValueError, UnicodeError):
                        raise VideoProtocolError(
                            "Stored segment manifest is invalid"
                        ) from None
                elif (segment_row["state"] == "sealing"
                      and segment_row["expected_digest"] is not None
                      and segment_row["expected_bytes"] is not None):
                    reference = self.evidence.lookup(
                        segment_row["expected_digest"]
                    )
                    if (reference is not None
                            and reference.bytes == segment_row["expected_bytes"]
                            and self.evidence.uses_reservation(
                                reference.digest, segment_row["reservation_id"]
                            )):
                        body = self.evidence.read(reference.digest)
                        _require(hashlib.sha256(body).hexdigest() == reference.digest,
                                 "Interrupted segment digest differs")
                        frames = tuple(frames_by_segment.get(
                            segment_row["segment_index"], []
                        ))
                        _require(bool(frames), "Interrupted segment lost lineage")
                        manifest = _segment_manifest_value(
                            frames, reference, segment_row["rotation_reason"],
                            segment_row["segment_index"],
                        )
                        self.journal.transaction(lambda connection, m=manifest,
                                                 index=segment_row["segment_index"],
                                                 ref=reference: connection.execute(
                            """UPDATE segments SET state = 'durable',
                                      reference_json = ?, manifest_json = ?
                                 WHERE recording_id = ? AND segment_index = ?
                                   AND state = 'sealing'""",
                            (_json_bytes({
                                "digest": ref.digest, "bytes": ref.bytes,
                                "path": ref.path,
                            }, MAX_FRAME_METADATA_BYTES,
                                "Segment reference is too large").decode("utf-8"),
                             _json_bytes(m, 512 * 1024,
                                         "Segment manifest is too large").decode("utf-8"),
                             recording_id, index),
                        ))
                if manifest is not None:
                    body = self.evidence.read(manifest["digest"])
                    _require(len(body) == manifest["bytes"]
                             and hashlib.sha256(body).hexdigest()
                             == manifest["digest"],
                             "Durable interrupted segment differs")
                    segments.append(manifest)
                    if segment_row["reservation_id"] is not None:
                        retained_reservations.add(segment_row["reservation_id"])
            losses = []
            for loss_row in loss_rows:
                try:
                    item = json.loads(loss_row["loss_json"])
                except (TypeError, ValueError, UnicodeError):
                    raise VideoProtocolError("Stored video loss is invalid") from None
                losses.append(item)
            known_losses = {
                item.get("acquisitionSequence") for item in losses
                if item.get("acquisitionSequence") is not None
            }
            durable_sequences = {
                frame["acquisitionSequence"]
                for segment in segments for frame in segment["frames"]
            }
            for frame_row in frame_rows:
                sequence = frame_row["acquisition_sequence"]
                if sequence in known_losses or sequence in durable_sequences:
                    continue
                frame = decoded_frames[sequence]
                item = {
                    "lossClass": (
                        "encoder-accepted-not-durable"
                        if frame_row["state"] == "accepted"
                        else "captured-dropped"
                    ),
                    "stage": "encoder",
                    "reason": "process-interruption",
                    "recordingInterval": {
                        "startOffsetMs": frame.offset_ms,
                        "endOffsetMs": frame.offset_ms,
                    },
                    "acquisitionSequence": sequence,
                }
                if frame_row["segment_index"] is not None:
                    item["segmentIndex"] = frame_row["segment_index"]
                losses.append(item)
            if not frame_rows and not losses:
                losses.append({
                    "lossClass": "not-acquired",
                    "stage": "native",
                    "reason": "process-interruption",
                    "recordingInterval": {
                        "startOffsetMs": 0, "endOffsetMs": 0,
                    },
                })
            manifest = validate_video_manifest({
                "schemaVersion": 1,
                "kind": "reproloop-avfoundation-video",
                "recordingId": recording_id,
                "status": "incomplete",
                "failureReason": "process-interruption",
                "codec": "h264",
                "container": "mp4",
                "samplingMode": (
                    "irregular-source-capture" if frame_rows
                    else "no-acquired-frames"
                ),
                "segmentDurationBoundMs": limits.segment_duration_ms,
                "segments": segments,
                "losses": losses,
                "eventMappings": [],
                "limits": _limits_manifest(limits),
            })
            self._clean_recording_work(recording_id, limits)
            encoded = _json_bytes(manifest, 4 * 1024 * 1024,
                                  "Recovered video manifest is too large")
            reservation = self.evidence.budget.reserve(
                recording_id, "finalization",
                len(encoded) + OBJECT_METADATA_BYTES,
            )
            reference = self.evidence.put_bytes(
                encoded, owner=recording_id, retention_class="original",
                retain_until_ms=row["retain_until_ms"], reservation=reservation,
            )
            self.journal.transaction(lambda connection: connection.execute(
                """UPDATE sessions SET state = 'frozen-incomplete',
                          manifest_json = ?, manifest_digest = ?,
                          manifest_reference_json = ?,
                          failure_reason = 'process-interruption'
                     WHERE recording_id = ? AND manifest_digest IS NULL""",
                (_json_bytes(manifest, 4 * 1024 * 1024,
                             "Recovered video manifest is too large").decode("utf-8"),
                 reference.digest,
                 _json_bytes({
                     "digest": reference.digest, "bytes": reference.bytes,
                     "path": reference.path,
                 }, MAX_FRAME_METADATA_BYTES,
                     "Video manifest reference is too large").decode("utf-8"),
                 recording_id),
            ))
            self._release_recovered_resources(recording_id, row)
            for segment_row in segment_rows:
                reservation_id = segment_row["reservation_id"]
                if (reservation_id is None
                        or reservation_id in retained_reservations):
                    continue
                try:
                    self.evidence.abandon_reservation(
                        reservation_id, release=True,
                    )
                except EvidenceStoreError:
                    pass
                self.evidence.release_unused_reservation(reservation_id)
            return self.load(recording_id)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _release_recovered_resources(self, recording_id, row):
        pins = self.journal.connection.execute(
            "SELECT DISTINCT pin_id FROM frames WHERE recording_id = ?",
            (recording_id,),
        ).fetchall()
        for pin in pins:
            self.evidence.unpin_id(pin["pin_id"])
        self.evidence.unpin_id("video_media_" + hashlib.sha256(
            recording_id.encode("ascii")
        ).hexdigest()[:40])
        for reservation_id in (row["spool_reservation_id"], row["encoding_reservation_id"]):
            self.evidence.release_unused_reservation(reservation_id)
        self.evidence.budget.commit_id(
            row["journal_reservation_id"], (32 * 1024 * 1024
                if row["source_mode"] == VIDEO_SOURCE_MODE else VIDEO_JOURNAL_RESERVATION_BYTES),
        )

    def recover_transient(self, recovery):
        from .video_recovery import recover_transient
        return recover_transient(self, recovery)

    def _clean_recording_work(self, recording_id, limits):
        work_root = self.journal.root / "work"
        recording_work = work_root / hashlib.sha256(
            recording_id.encode("ascii")
        ).hexdigest()
        if not recording_work.exists():
            _fsync_directory(work_root if work_root.exists() else self.journal.root)
            return
        _require(recording_work.is_dir() and not recording_work.is_symlink(),
                 "Interrupted video work path is invalid")
        children = list(recording_work.iterdir())
        _require(len(children) <= limits.max_segments,
                 "Interrupted video work limit exceeded")
        expected_directories = {
            f"segment_{index:03d}"
            for index in range(1, limits.max_segments + 1)
        }
        for directory in children:
            _require(directory.is_dir() and not directory.is_symlink(),
                     "Interrupted segment work path is invalid")
            _require(directory.name in expected_directories,
                     "Interrupted segment work path is unexpected")
            files = list(directory.iterdir())
            _require(len(files) <= 2, "Interrupted segment file limit exceeded")
            for path in files:
                _require(path.is_file() and not path.is_symlink(),
                         "Interrupted segment file is invalid")
                _require(path.name in {"segment.partial.mp4", "segment.mp4"}
                         and path.stat().st_size <= limits.max_segment_bytes,
                         "Interrupted segment file exceeds its bound")
                path.unlink()
            directory.rmdir()
        recording_work.rmdir()
        _fsync_directory(work_root)

    def close(self):
        self.journal.close()


__all__ = [
    "AVFoundationSegmentEncoder",
    "EncodedSegment",
    "EncoderFrame",
    "HelperResult",
    "PROTOCOL_MAGIC",
    "VideoCatalog",
    "VideoEncodingError",
    "VideoFrameSink",
    "VideoLimits",
    "VideoProtocolError",
    "encode_configuration",
    "encode_finish",
    "encode_frame",
    "parse_helper_output",
    "validate_video_manifest",
]
