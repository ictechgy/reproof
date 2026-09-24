"""Crash recovery for transient video source journals.

Transient video has two durable authorities: the G2 recording ledger and the
video journal.  This module joins them only after acquiring the same
per-recording writer lock used by capture.  It never admits a new sample or
asks a recovery capability for an input, clock, or provider operation.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .. import contracts
from . import video as _video
from .disk_budget import DiskBudgetError
from .evidence_store import EvidenceStoreError, OBJECT_METADATA_BYTES
from .frame_spool import FrameSpool, FrameSpoolError
from .recording_session import VIDEO_SOURCE_MODE


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TRANSIENT_JOURNAL_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_FRAMES = 10_000
_MAX_LOSSES = 512
_SOURCE_KEYS = {
    "recordingFrameSequence", "acquisitionSequence", "digest", "bytes",
    "mimeType", "width", "height", "orientation", "providerIncarnation",
    "timing",
}
_SOURCE_GAP_KEYS = {"nativeSequenceGap", "nativeGapInterval"}
_TIMING_KEYS = {
    "offsetMs", "earliestOffsetMs", "latestOffsetMs", "uncertaintyNs",
    "acquisitionSequence", "nativeIncarnation", "timingSource",
}
_TIMING_OPTIONAL_KEYS = {
    "providerClockId", "providerBootDigest", "providerMonotonicNs",
    "providerCaptureStartNs",
}
_REFERENCE_KEYS = {"digest", "bytes", "path"}


def _require(value: bool, message: str) -> None:
    if not value:
        raise _video.VideoProtocolError(message)


def _integer(value: object, name: str, low: int = 0,
             high: int = 2 ** 63 - 1) -> int:
    _require(type(value) is int and low <= value <= high, f"Invalid {name}")
    return value


def _digest(value: object, name: str = "digest") -> str:
    _require(type(value) is str and _DIGEST.fullmatch(value) is not None,
             f"Invalid {name}")
    return value


def _identifier(value: object, name: str) -> str:
    _require(type(value) is str and _video._ID.fullmatch(value) is not None,
             f"Invalid {name}")
    return value


def _canonical(value: object, maximum: int, message: str) -> bytes:
    return _video._json_bytes(value, maximum, message)


def _load_json(value: object, message: str):
    try:
        return json.loads(value)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _video.VideoProtocolError(message) from None


@contextmanager
def _recording_lock(catalog, recording_id: str):
    locks = Path(catalog.journal.root) / "locks"
    descriptor = None
    try:
        locks.mkdir(exist_ok=True, mode=0o700)
        info = locks.lstat()
        _require(locks.is_dir() and not locks.is_symlink()
                 and info.st_uid == os.getuid(),
                 "Video recovery lock directory is invalid")
        path = locks / (hashlib.sha256(recording_id.encode("ascii")).hexdigest() + ".lock")
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(),
                 "Video recovery lock is invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise _video.VideoProtocolError(
                "Live video recording cannot be recovered"
            ) from None
        yield
    except OSError:
        raise _video.VideoProtocolError(
            "Live video recording cannot be recovered"
        ) from None
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _validate_metadata(recovery):
    # Import lazily so importing video_recovery while video.py is importing it
    # cannot create a cycle.  The exact-type check is the trust boundary.
    from .recording_session import RecordingRecoverySession

    _require(type(recovery) is RecordingRecoverySession,
             "Video recovery authority is unavailable")
    value = recovery.video_recovery_metadata()
    _require(type(value) is dict and set(value) == {
        "limits", "retainUntilMs", "sourceManifestDigest",
        "cleanupComplete", "state",
    }, "Historical video recovery metadata is invalid")
    limits_value = value["limits"]
    fields = set(_video.VideoLimits.__dataclass_fields__)
    _require(type(limits_value) is dict and set(limits_value) == fields,
             "Historical video limits are incomplete")
    try:
        limits = _video.VideoLimits(**copy.deepcopy(limits_value))
    except (TypeError, ValueError, _video.VideoProtocolError):
        raise _video.VideoProtocolError("Historical video limits are invalid") from None
    retain_until = _integer(value["retainUntilMs"], "source retention deadline", 1)
    source_digest = value["sourceManifestDigest"]
    _require(source_digest is None
             or (type(source_digest) is str and _DIGEST.fullmatch(source_digest) is not None),
             "Historical source manifest identity is invalid")
    _require(type(value["cleanupComplete"]) is bool,
             "Historical source cleanup state is invalid")
    _require(type(value["state"]) is str and value["state"] in {
        "finalizing", "frozen-complete", "frozen-incomplete",
    }, "Recording is not recoverable")
    _require(value["state"] == "finalizing" or source_digest is not None,
             "Frozen transient recording has no attached source manifest")
    return value, limits, retain_until, source_digest


def _validate_barrier(recovery):
    value = recovery.video_barrier_snapshot()
    _require(type(value) is dict and {
        "sequence", "mediaSequence", "offsetMs", "sourceSequence",
    } <= set(value), "Video recovery barrier is invalid")
    _integer(value["sequence"], "recording barrier", 0, 100_000)
    for key in ("mediaSequence", "sourceSequence"):
        _integer(value[key], f"{key} barrier", 0, _MAX_SOURCE_FRAMES)
    _integer(value["offsetMs"], "recording barrier offset", 0, 600_000)
    return value


def _validate_gap(value):
    _require(type(value) is dict and set(value) == {"first", "last"},
             "Invalid native acquisition gap")
    first = _integer(value["first"], "native gap start", 1)
    last = _integer(value["last"], "native gap end", first)
    return first, last


def _validate_interval(value, start=0):
    _require(type(value) is dict and set(value) == {
        "startOffsetMs", "endOffsetMs",
    }, "Invalid native gap interval")
    first = _integer(value["startOffsetMs"], "native gap interval start", start,
                     600_000)
    last = _integer(value["endOffsetMs"], "native gap interval end", first,
                    600_000)
    return first, last


def _validate_sources(recovery, barrier):
    sources = recovery.video_sources()
    _require(type(sources) is list and len(sources) <= _MAX_SOURCE_FRAMES,
             "Video source ledger is invalid")
    _require(len(sources) <= barrier["sourceSequence"],
             "Video source ledger exceeds its stop barrier")
    result = []
    previous_acquisition = 0
    for expected_sequence, source in enumerate(sources, 1):
        _require(type(source) is dict and set(source) <= _SOURCE_KEYS | _SOURCE_GAP_KEYS
                 and _SOURCE_KEYS <= set(source),
                 "Video source identity is invalid")
        _integer(source["recordingFrameSequence"], "recording frame sequence", 1)
        _require(source["recordingFrameSequence"] == expected_sequence,
                 "Video source sequence changed")
        acquisition = _integer(source["acquisitionSequence"],
                               "source acquisition sequence", 1)
        _require(acquisition > previous_acquisition,
                 "Video source acquisition order changed")
        previous_acquisition = acquisition
        _digest(source["digest"], "source digest")
        _integer(source["bytes"], "source bytes", 1, 3 * 1024 * 1024)
        _require(type(source["mimeType"]) is str
                 and source["mimeType"] in {"image/png", "image/jpeg", "image/svg+xml"},
                 "Video source MIME type is invalid")
        width = _integer(source["width"], "source width", 1, 8192)
        height = _integer(source["height"], "source height", 1, 8192)
        _require(type(source["orientation"]) is str
                 and source["orientation"] in {"portrait", "landscape"},
                 "Video source orientation is invalid")
        _identifier(source["providerIncarnation"], "source provider incarnation")
        timing = source["timing"]
        _require(type(timing) is dict and set(timing) <= (_TIMING_KEYS | _TIMING_OPTIONAL_KEYS)
                 and _TIMING_KEYS <= set(timing), "Video source timing is invalid")
        _integer(timing["offsetMs"], "source offset", 0, 600_000)
        earliest = _integer(timing["earliestOffsetMs"], "source interval start", 0, 600_000)
        _integer(timing["latestOffsetMs"], "source interval end", earliest, 600_000)
        _integer(timing["uncertaintyNs"], "source uncertainty", 0, 60_000_000_000)
        _require(timing["acquisitionSequence"] == acquisition
                 and type(timing["timingSource"]) is str
                 and timing["timingSource"] in {
                     "host-acquired", "provider-mapped", "native-unmapped",
                 },
                 "Video source timing identity is invalid")
        _identifier(timing["nativeIncarnation"], "source native incarnation")
        for key in ("providerClockId", "providerBootDigest"):
            if key in timing:
                _require(type(timing[key]) is str and timing[key],
                         f"Source {key} is invalid")
        if "providerMonotonicNs" in timing:
            _integer(timing["providerMonotonicNs"], "provider timestamp", 0)
        if "providerCaptureStartNs" in timing:
            _integer(timing["providerCaptureStartNs"], "provider capture timestamp", 0)
        if ("nativeSequenceGap" in source) != ("nativeGapInterval" in source):
            raise _video.VideoProtocolError("Native gap identity is incomplete")
        if "nativeSequenceGap" in source:
            _validate_gap(source["nativeSequenceGap"])
            _validate_interval(source["nativeGapInterval"])
        # These checks intentionally keep geometry/timing available for the
        # later journal lineage comparison, but do not reject a source that
        # the encoder would have dropped (odd geometry or unsupported MIME).
        _require(width * height <= 8192 * 8192, "Video source geometry is invalid")
        result.append(copy.deepcopy(source))
    _require(len(sources) == barrier["sourceSequence"],
             "Video source ledger is shorter than its stop barrier")
    return result


def _limits_json(limits):
    return _canonical(
        {name: getattr(limits, name) for name in limits.__dataclass_fields__},
        _video.MAX_CONFIG_BYTES,
        "Video limits are too large",
    ).decode("utf-8")


def _session_row(catalog, recording_id):
    with catalog.journal._lock:
        return catalog.journal.connection.execute(
            "SELECT * FROM sessions WHERE recording_id = ?", (recording_id,)
        ).fetchone()


def _validate_session_row(row, metadata_limits, retain_until):
    _require(row is not None, "Video recording session is unavailable")
    _require(row["source_mode"] == VIDEO_SOURCE_MODE,
             "Transient video recovery requires a v2 source journal")
    limits_value = _load_json(row["limits_json"], "Stored video limits are invalid")
    fields = set(_video.VideoLimits.__dataclass_fields__)
    _require(type(limits_value) is dict and set(limits_value) == fields
             and limits_value == {
                 name: getattr(metadata_limits, name)
                 for name in metadata_limits.__dataclass_fields__
             }, "Stored video limits differ from G2 history")
    _require(type(row["retain_until_ms"]) is int
             and row["retain_until_ms"] == retain_until,
             "Stored video retention differs from G2 history")
    _require(row["state"] in {
        "recording", "finalizing", "frozen-complete", "frozen-incomplete",
    }, "Video recording session is not recoverable")
    if row["manifest_digest"] is not None:
        _digest(row["manifest_digest"], "video manifest digest")
        _require(row["manifest_json"] is not None
                 and row["manifest_reference_json"] is not None,
                 "Stored video manifest is incomplete")
    return row


def _budget_key(catalog, recording_id, suffix):
    return "video_" + hashlib.sha256(
        f"{Path(catalog.journal.root).resolve()}:{recording_id}".encode("utf-8")
    ).hexdigest()[:40] + suffix


def _reconstruct_session(catalog, recording_id, limits, retain_until,
                         source_manifest_digest, metadata_state):
    """Recreate only the bounded video bookkeeping lost with its journal row."""
    budget = catalog.evidence.budget
    specifications = (
        ("journal", max(_TRANSIENT_JOURNAL_BYTES,
                        getattr(_video, "VIDEO_JOURNAL_RESERVATION_BYTES", 0)),
         "_journal"),
        ("spool", limits.max_queue_bytes, "_spool"),
        ("encoding", limits.max_segment_bytes, "_encoding"),
    )
    reservation_ids = {}
    for category, amount, suffix in specifications:
        try:
            reservation = budget.reserve(
                recording_id, category, amount,
                idempotency_key=_budget_key(catalog, recording_id, suffix),
            )
        except DiskBudgetError:
            raise _video.VideoProtocolError(
                "Video recovery bookkeeping reservation is unavailable"
            ) from None
        reservation_ids[category] = reservation.reservation_id

    manifest_json = None
    manifest_reference_json = None
    state = "finalizing"
    failure_reason = None
    if source_manifest_digest is not None:
        reference = catalog.evidence.lookup(source_manifest_digest)
        _require(reference is not None, "Attached source manifest is unavailable")
        manifest_json = _canonical(
            _load_json(catalog.evidence.read(source_manifest_digest),
                       "Attached source manifest is invalid"),
            _video.MAX_VIDEO_MANIFEST_BYTES,
            "Attached source manifest is too large",
        ).decode("utf-8")
        manifest_reference_json = _canonical({
            "digest": reference.digest, "bytes": reference.bytes,
            "path": reference.path,
        }, _video.MAX_FRAME_METADATA_BYTES,
            "Video manifest reference is too large").decode("utf-8")
        state = "frozen-incomplete" if metadata_state != "finalizing" else "finalizing"
        failure_reason = "process-interruption"

    values = {
        "recording_id": recording_id,
        "state": state,
        "limits_json": _limits_json(limits),
        "journal_reservation_id": reservation_ids["journal"],
        "spool_reservation_id": reservation_ids["spool"],
        "encoding_reservation_id": reservation_ids["encoding"],
        "retain_until_ms": retain_until,
        "manifest_json": manifest_json,
        "manifest_digest": source_manifest_digest,
        "manifest_reference_json": manifest_reference_json,
        "failure_reason": failure_reason,
        "source_mode": VIDEO_SOURCE_MODE,
    }
    with catalog.journal._lock:
        columns = {item[1] for item in catalog.journal.connection.execute(
            "PRAGMA table_info(sessions)"
        )}
        selected = [key for key in values if key in columns]
        if "cleanup_complete" in columns:
            selected.append("cleanup_complete")
            values["cleanup_complete"] = 0
        placeholders = ",".join("?" for _ in selected)
        catalog.journal.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = catalog.journal.connection.execute(
                "SELECT * FROM sessions WHERE recording_id = ?", (recording_id,)
            ).fetchone()
            if existing is None:
                catalog.journal.connection.execute(
                    f"INSERT INTO sessions ({','.join(selected)}) VALUES ({placeholders})",
                    tuple(values[key] for key in selected),
                )
            catalog.journal.connection.commit()
        except Exception:
            catalog.journal.connection.rollback()
            raise
    return _session_row(catalog, recording_id)


def _reference(catalog, digest, expected_bytes, reservation_id):
    _digest(digest, "video object digest")
    _integer(expected_bytes, "video object bytes", 1, 64 * 1024 * 1024)
    _require(type(reservation_id) is str and reservation_id.startswith("reservation_"),
             "Video object reservation is invalid")
    reference = catalog.evidence.lookup(digest)
    _require(reference is not None and reference.bytes == expected_bytes
             and catalog.evidence.uses_reservation(digest, reservation_id),
             "Video object reservation or identity is unavailable")
    body = catalog.evidence.read(digest)
    _require(len(body) == expected_bytes and hashlib.sha256(body).hexdigest() == digest,
             "Video object bytes differ from its journal")
    return reference


def _validate_source_frame(frame, source, limits):
    _require(frame.recording_frame_sequence == source["recordingFrameSequence"]
             and frame.acquisition_sequence == source["acquisitionSequence"]
             and frame.digest == source["digest"]
             and frame.bytes == source["bytes"]
             and frame.path == f"transient/{source['recordingFrameSequence']}"
             and frame.mime_type == source["mimeType"]
             and [frame.width, frame.height, frame.orientation]
             == [source["width"], source["height"], source["orientation"]]
             and frame.provider_incarnation == source["providerIncarnation"],
             "Video frame lineage differs from G2 source")
    timing = source["timing"]
    _require(frame.offset_ms == timing["offsetMs"]
             and frame.earliest_offset_ms == timing["earliestOffsetMs"]
             and frame.latest_offset_ms == timing["latestOffsetMs"]
             and frame.uncertainty_ns == timing["uncertaintyNs"]
             and frame.timing_source == timing["timingSource"]
             and frame.native_incarnation == timing["nativeIncarnation"]
             and frame.provider_clock_id == timing.get("providerClockId")
             and frame.provider_boot_digest == timing.get("providerBootDigest"),
             "Video frame timing differs from G2 source")
    _video._validate_encoder_frame(frame, limits)


def _source_frame_rows(catalog, recording_id, sources, limits):
    with catalog.journal._lock:
        rows = catalog.journal.connection.execute(
            "SELECT * FROM frames WHERE recording_id=? ORDER BY acquisition_sequence LIMIT ?",
            (recording_id, _MAX_SOURCE_FRAMES + 1),
        ).fetchall()
    _require(len(rows) <= _MAX_SOURCE_FRAMES, "Video frame journal exceeds its bound")
    by_recording_sequence = {}
    by_acquisition = {}
    for row in rows:
        try:
            value = _load_json(row["frame_json"], "Stored video frame lineage is invalid")
            frame = _video._frame_from_json(value)
            _video._validate_encoder_frame(frame, limits)
        except (_video.VideoProtocolError, KeyError, TypeError, ValueError):
            raise _video.VideoProtocolError("Stored video frame lineage is invalid") from None
        sequence = frame.recording_frame_sequence
        _require(sequence is not None and 1 <= sequence <= len(sources),
                 "Video frame is outside the G2 source barrier")
        source = sources[sequence - 1]
        _validate_source_frame(frame, source, limits)
        _require(row["acquisition_sequence"] == frame.acquisition_sequence
                 and row["recording_id"] == recording_id,
                 "Video frame journal identity differs")
        _require(sequence not in by_recording_sequence
                 and frame.acquisition_sequence not in by_acquisition,
                 "Video frame lineage is duplicated")
        _require(type(row["state"]) is str and row["state"] in {
            "queued", "assigned", "durable", "accepted", "dropped",
        }, "Video frame state is invalid")
        by_recording_sequence[sequence] = {"row": row, "frame": frame}
        by_acquisition[frame.acquisition_sequence] = by_recording_sequence[sequence]
    return rows, by_recording_sequence, by_acquisition


def _lineage_for(segment_row, frames):
    lineage = _load_json(segment_row["lineage_json"], "Stored segment lineage is invalid")
    _require(type(lineage) is list and bool(lineage), "Stored segment lineage is empty")
    expected = [frame.manifest_value(frames[0].offset_ms) for frame in frames]
    _require(lineage == expected, "Stored segment lineage differs from frames")
    return lineage


def _segments(catalog, recording_id, limits, sources):
    rows, frames_by_sequence, frames_by_acquisition = _source_frame_rows(
        catalog, recording_id, sources, limits)
    by_segment = {}
    for item in frames_by_sequence.values():
        segment_index = item["row"]["segment_index"]
        if segment_index is not None:
            _integer(segment_index, "segment index", 1, limits.max_segments)
            by_segment.setdefault(segment_index, []).append(item)
    with catalog.journal._lock:
        segment_rows = catalog.journal.connection.execute(
            "SELECT * FROM segments WHERE recording_id=? ORDER BY segment_index LIMIT ?",
            (recording_id, limits.max_segments + 1),
        ).fetchall()
    _require(len(segment_rows) <= limits.max_segments,
             "Video segment journal exceeds its bound")
    segment_states = {row["segment_index"]: row["state"] for row in segment_rows}
    expected_segment_state = {"assigned": "sealing", "accepted": "sealing",
                              "durable": "durable", "dropped": "failed"}
    for item in frames_by_sequence.values():
        frame_row = item["row"]
        if frame_row["state"] == "queued":
            _require(frame_row["segment_index"] is None,
                     "Queued video frame already names a segment")
        else:
            _require(frame_row["segment_index"] in segment_states
                     and segment_states[frame_row["segment_index"]]
                     == expected_segment_state[frame_row["state"]],
                     "Video frame and segment states contradict their transaction")
    outcomes = []
    retained_reservations = set()
    durable_sequences = set()
    for expected_index, row in enumerate(segment_rows, 1):
        index = _integer(row["segment_index"], "segment index", 1,
                         limits.max_segments)
        _require(index == expected_index, "Video segment sequence is not contiguous")
        state = row["state"]
        _require(type(state) is str and state in {"durable", "sealing", "failed"},
                 "Stored video segment state is invalid")
        _require(type(row["rotation_reason"]) is str,
                 "Stored video segment rotation is invalid")
        entries = sorted(by_segment.get(index, ()),
                         key=lambda item: item["frame"].recording_frame_sequence)
        frames = tuple(item["frame"] for item in entries)
        _require(bool(frames), "Stored video segment lineage is unavailable")
        _lineage_for(row, frames)
        if state == "failed":
            continue
        if state == "durable":
            _require(all(item["row"]["state"] == "durable" for item in entries),
                     "Durable video segment frame state differs")
            _require(row["reference_json"] is not None and row["manifest_json"] is not None,
                     "Durable video segment metadata is incomplete")
            ref_value = _load_json(row["reference_json"], "Stored video reference is invalid")
            _require(type(ref_value) is dict and set(ref_value) == _REFERENCE_KEYS,
                     "Stored video reference is invalid")
            reference = _reference(catalog, ref_value["digest"], ref_value["bytes"],
                                   row["reservation_id"])
            manifest = _load_json(row["manifest_json"], "Stored video segment is invalid")
            expected = _video._segment_manifest_value(
                frames, reference, row["rotation_reason"], index,
            )
            _require(manifest == expected, "Stored durable video segment differs")
            segment = manifest
        else:
            expected_digest = row["expected_digest"]
            expected_bytes = row["expected_bytes"]
            valid = (expected_digest is not None and expected_bytes is not None)
            if valid:
                try:
                    reference = _reference(catalog, expected_digest, expected_bytes,
                                           row["reservation_id"])
                except (_video.VideoProtocolError, EvidenceStoreError):
                    valid = False
            if not valid:
                _mark_segment_failed(catalog, recording_id, index, entries)
                continue
            segment = _video._segment_manifest_value(
                frames, reference, row["rotation_reason"], index,
            )
            _promote_segment(catalog, recording_id, index, reference, segment,
                             entries)
        for frame in segment["frames"]:
            sequence = frame["recordingFrameSequence"]
            _require(sequence not in durable_sequences,
                     "Durable video source frame is duplicated")
            durable_sequences.add(sequence)
        if row["reservation_id"] is not None:
            retained_reservations.add(row["reservation_id"])
        outcomes.append({"manifest": segment, "reservationId": row["reservation_id"]})
    _require(len(outcomes) <= limits.max_segments, "Video segment limit exceeded")
    return (
        rows, frames_by_sequence, frames_by_acquisition, segment_rows,
        outcomes, retained_reservations, durable_sequences,
    )


def _promote_segment(catalog, recording_id, index, reference, segment, entries):
    reference_json = _canonical({
        "digest": reference.digest, "bytes": reference.bytes,
        "path": reference.path,
    }, _video.MAX_FRAME_METADATA_BYTES,
        "Segment reference is too large").decode("utf-8")
    manifest_json = _canonical(segment, 512 * 1024,
                                "Segment manifest is too large").decode("utf-8")
    with catalog.journal._lock:
        connection = catalog.journal.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """UPDATE segments SET state='durable', reference_json=?, manifest_json=?
                     WHERE recording_id=? AND segment_index=? AND state='sealing'""",
                (reference_json, manifest_json, recording_id, index),
            )
            connection.executemany(
                """UPDATE frames SET state='durable'
                     WHERE recording_id=? AND acquisition_sequence=?""",
                [(recording_id, item["frame"].acquisition_sequence) for item in entries],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _mark_segment_failed(catalog, recording_id, index, entries):
    with catalog.journal._lock:
        connection = catalog.journal.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "UPDATE segments SET state='failed' WHERE recording_id=? AND segment_index=?",
                (recording_id, index),
            )
            connection.executemany(
                "UPDATE frames SET state='dropped' WHERE recording_id=? AND acquisition_sequence=?",
                [(recording_id, item["frame"].acquisition_sequence) for item in entries],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _load_losses(catalog, recording_id):
    with catalog.journal._lock:
        rows = catalog.journal.connection.execute(
            "SELECT loss_json FROM losses WHERE recording_id=? ORDER BY loss_sequence LIMIT ?",
            (recording_id, _MAX_LOSSES + 1),
        ).fetchall()
    _require(len(rows) <= _MAX_LOSSES, "Video loss journal exceeds its bound")
    result = []
    for row in rows:
        value = _load_json(row["loss_json"], "Stored video loss is invalid")
        _require(type(value) is dict, "Stored video loss is invalid")
        result.append(copy.deepcopy(value))
    return result


def _covered_sources(losses, sources):
    covered = set()
    native_ranges = set()
    by_acquisition = {item["acquisitionSequence"]: item["recordingFrameSequence"]
                      for item in sources}
    for loss in losses:
        source_range = loss.get("recordingFrameRange")
        if source_range is not None:
            _require(type(source_range) is dict and set(source_range) == {"first", "last"},
                     "Stored video source loss range is invalid")
            first = _integer(source_range["first"], "source loss start", 1, len(sources))
            last = _integer(source_range["last"], "source loss end", first, len(sources))
            covered.update(range(first, last + 1))
        elif "acquisitionSequence" in loss:
            sequence = loss["acquisitionSequence"]
            _require(sequence in by_acquisition,
                     "Stored video loss names an unknown source")
            covered.add(by_acquisition[sequence])
        native_range = loss.get("nativeSequenceRange")
        if native_range is not None:
            native_ranges.add(tuple(_validate_gap(native_range)))
    return covered, native_ranges


def _source_loss(source, state, segment_index=None):
    sequence = source["recordingFrameSequence"]
    offset = source["timing"]["offsetMs"]
    value = {
        "lossClass": ("encoder-accepted-not-durable"
                       if state == "accepted" else "captured-dropped"),
        "stage": "encoder",
        "reason": "process-interruption",
        "recordingInterval": {"startOffsetMs": offset, "endOffsetMs": offset},
        "recordingFrameRange": {"first": sequence, "last": sequence},
        "acquisitionSequence": source["acquisitionSequence"],
    }
    if segment_index is not None:
        value["segmentIndex"] = segment_index
    return value


def _native_gap_loss(source):
    first, last = _validate_gap(source["nativeSequenceGap"])
    start, end = _validate_interval(source["nativeGapInterval"])
    return {
        "lossClass": "captured-dropped",
        "stage": "transport",
        "reason": "native-frame-gap",
        "recordingInterval": {"startOffsetMs": start, "endOffsetMs": end},
        "nativeSequenceRange": {"first": first, "last": last},
    }


def _can_merge(previous, current):
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    if (previous.get("lossClass"), previous.get("stage"), previous.get("reason")) != (
            current.get("lossClass"), current.get("stage"), current.get("reason")):
        return False
    previous_range = previous.get("recordingFrameRange")
    current_range = current.get("recordingFrameRange")
    previous_native = previous.get("nativeSequenceRange")
    current_native = current.get("nativeSequenceRange")
    if (previous_range is None) != (current_range is None):
        return False
    if (previous_native is None) != (current_native is None):
        return False
    if previous_range is None and previous_native is None:
        return False
    previous_interval = previous.get("recordingInterval")
    current_interval = current.get("recordingInterval")
    if (not isinstance(previous_interval, dict)
            or not isinstance(current_interval, dict)
            or set(previous_interval) != {"startOffsetMs", "endOffsetMs"}
            or set(current_interval) != {"startOffsetMs", "endOffsetMs"}):
        return False
    if previous_range is not None:
        if previous_range["last"] + 1 != current_range["first"]:
            return False
    else:
        if previous_native["last"] + 1 != current_native["first"]:
            return False
    return previous_interval["endOffsetMs"] <= current_interval["startOffsetMs"]


def _merge_losses(previous, current):
    result = copy.deepcopy(previous)
    if "recordingFrameRange" in result:
        result["recordingFrameRange"]["last"] = current["recordingFrameRange"]["last"]
        result.pop("acquisitionSequence", None)
    else:
        result["nativeSequenceRange"]["last"] = current["nativeSequenceRange"]["last"]
    result["recordingInterval"]["endOffsetMs"] = current["recordingInterval"]["endOffsetMs"]
    if result.get("segmentIndex") != current.get("segmentIndex"):
        result.pop("segmentIndex", None)
    return result


def _coalesce_losses(losses):
    result = []
    for loss in losses:
        if result and _can_merge(result[-1], loss):
            result[-1] = _merge_losses(result[-1], loss)
        else:
            result.append(copy.deepcopy(loss))
    _require(len(result) <= _MAX_LOSSES, "Video loss limit reached")
    return result


def _ensure_source_sampling_marker(losses, sources, segments):
    """Keep one admitted-source identity visible for the v2 sampling rule.

    A coalesced source range is sufficient for source coverage, but the video
    contract also distinguishes an admitted-but-lost source stream from a
    genuinely empty acquisition.  Split the first covered source out of a
    range when every source outcome was otherwise represented without an
    ``acquisitionSequence`` field.
    """
    if not sources or segments or any("acquisitionSequence" in item for item in losses):
        return losses
    for offset, loss in enumerate(losses):
        source_range = loss.get("recordingFrameRange")
        if not isinstance(source_range, dict):
            continue
        first, last = source_range.get("first"), source_range.get("last")
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(sources):
            continue
        source = sources[first - 1]
        marker = copy.deepcopy(loss)
        marker["recordingFrameRange"] = {"first": first, "last": first}
        marker["acquisitionSequence"] = source["acquisitionSequence"]
        marker["recordingInterval"] = {
            "startOffsetMs": source["timing"]["offsetMs"],
            "endOffsetMs": source["timing"]["offsetMs"],
        }
        remainder = None
        if first < last:
            remainder = copy.deepcopy(loss)
            remainder["recordingFrameRange"] = {"first": first + 1, "last": last}
            remainder.pop("acquisitionSequence", None)
        replacement = [marker] + ([] if remainder is None else [remainder])
        return losses[:offset] + replacement + losses[offset + 1:]
    return losses


def _manifest_from_existing(catalog, source_digest, recording_id, sources):
    reference = catalog.evidence.lookup(source_digest)
    _require(reference is not None, "Attached source manifest is unavailable")
    body = catalog.evidence.read(source_digest)
    _require(len(body) == reference.bytes
             and hashlib.sha256(body).hexdigest() == source_digest,
             "Attached source manifest bytes differ")
    value = _load_json(body, "Attached source manifest is invalid")
    try:
        manifest = _video.validate_video_manifest(value)
    except (_video.VideoProtocolError, TypeError, ValueError):
        raise _video.VideoProtocolError("Attached source manifest is invalid") from None
    _require(manifest["schemaVersion"] == 2
             and manifest["recordingId"] == recording_id,
             "Attached source manifest identity differs")
    expected_sources = [{
        "sequence": source["recordingFrameSequence"],
        "acquisitionSequence": source["acquisitionSequence"],
        "digest": source["digest"],
    } for source in sources]
    _require(manifest["sourceFrames"] == expected_sources,
             "Attached source manifest ledger differs")
    encoded = _canonical(manifest, _video.MAX_VIDEO_MANIFEST_BYTES,
                         "Attached source manifest is too large")
    _require(encoded == body, "Attached source manifest encoding differs")
    for segment in manifest["segments"]:
        object_reference = catalog.evidence.lookup(segment["digest"])
        _require(object_reference is not None
                 and object_reference.path == segment["path"]
                 and object_reference.bytes == segment["bytes"],
                 "Attached video segment is unavailable")
        segment_body = catalog.evidence.read(segment["digest"])
        _require(hashlib.sha256(segment_body).hexdigest() == segment["digest"],
                 "Attached video segment bytes differ")
    return manifest, reference


def _update_video_session(catalog, recording_id, manifest, reference):
    body = _canonical(manifest, _video.MAX_VIDEO_MANIFEST_BYTES,
                      "Recovered video manifest is too large")
    state = "frozen-incomplete" if manifest["status"] == "incomplete" else "frozen-complete"
    failure_reason = manifest["failureReason"]
    reference_json = _canonical({
        "digest": reference.digest, "bytes": reference.bytes,
        "path": reference.path,
    }, _video.MAX_FRAME_METADATA_BYTES,
        "Video manifest reference is too large").decode("utf-8")
    with catalog.journal._lock:
        connection = catalog.journal.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT manifest_digest FROM sessions WHERE recording_id=?",
                (recording_id,),
            ).fetchone()
            _require(row is not None, "Video recording session is unavailable")
            _require(row["manifest_digest"] is None
                     or row["manifest_digest"] == reference.digest,
                     "Video manifest identity changed during recovery")
            connection.execute(
                """UPDATE sessions SET state=?, manifest_json=?,
                          manifest_digest=?, manifest_reference_json=?,
                          failure_reason=?
                     WHERE recording_id=? AND (manifest_digest IS NULL OR manifest_digest=?)""",
                (state, body.decode("utf-8"), reference.digest, reference_json,
                 failure_reason, recording_id, reference.digest),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _source_proof(catalog, recovery, manifest, manifest_reference, outcomes):
    recording_id = recovery.recording_id
    ledger = {item["recordingFrameSequence"]: item
              for item in recovery.video_sources()}

    def proof(tokens):
        identities = tuple((token.frame_sequence, token.acquisition_sequence, token.digest)
                           for token in tokens)
        for outcome in outcomes:
            expected = tuple((frame["recordingFrameSequence"],
                              frame["acquisitionSequence"],
                              frame["sourceDigest"])
                             for frame in outcome["manifest"]["frames"])
            if identities != expected:
                continue
            segment = outcome["manifest"]
            with catalog.journal._lock:
                row = catalog.journal.connection.execute(
                    "SELECT state,manifest_json FROM segments WHERE recording_id=? AND segment_index=?",
                    (recording_id, segment["segmentIndex"]),
                ).fetchone()
            _require(row is not None and row["state"] == "durable"
                     and _load_json(row["manifest_json"], "Stored segment is invalid") == segment,
                     "Source segment journal is not durable")
            body = catalog.evidence.read(segment["digest"])
            _require(len(body) == segment["bytes"], "Source segment is unavailable")
            return {
                "kind": "video-segment", "recordingId": recording_id,
                "digest": segment["digest"],
                "frames": [item[0] for item in identities],
            }
        _require(manifest is not None and manifest_reference is not None,
                 "Source outcome manifest has not been attached")
        body = catalog.evidence.read(manifest_reference.digest)
        _require(body == _canonical(manifest, _video.MAX_VIDEO_MANIFEST_BYTES,
                                    "Video manifest is too large"),
                 "Source outcome manifest is unavailable")
        for token in tokens:
            source = ledger.get(token.frame_sequence)
            if source is not None:
                _require(source["acquisitionSequence"] == token.acquisition_sequence
                         and source["digest"] == token.digest,
                         "Source outcome identity differs")
            else:
                # A token beyond the frozen ledger is only an orphaned spool
                # intent from the crash between spool.stage and G2 commit.
                _require(token.frame_sequence > len(ledger)
                         and recovery.video_source(token.acquisition_sequence) is None,
                         "Source was admitted outside the frozen outcome ledger")
        return {
            "kind": "video-manifest", "recordingId": recording_id,
            "digest": manifest_reference.digest,
            "frames": [item[0] for item in identities],
        }

    return proof


def _release_nondurable_segment_reservations(catalog, segment_rows, retained):
    for row in segment_rows:
        reservation_id = row["reservation_id"]
        if reservation_id is None or reservation_id in retained:
            continue
        try:
            catalog.evidence.abandon_reservation(reservation_id, release=True)
        except EvidenceStoreError:
            pass
        try:
            catalog.evidence.release_unused_reservation(reservation_id)
        except (EvidenceStoreError, DiskBudgetError):
            pass


def _cleanup_sources(catalog, recovery, recording_id, manifest,
                     manifest_reference, outcomes, limits):
    spool = _open_spool(catalog, recording_id, limits)
    try:
        proof = _source_proof(catalog, recovery, manifest, manifest_reference, outcomes)
        spool.recover(proof)
        active = spool.get_active_tokens()
        for offset in range(0, len(active), 256):
            spool.release(active[offset:offset + 256], proof)
        spool.finalcleanup(proof)
    finally:
        if spool is not None:
            spool.close()
    recovery.complete_source_cleanup()


def _open_spool(catalog, recording_id, limits):
    spool_root = Path(catalog.journal.root) / "sources" / hashlib.sha256(
        recording_id.encode("ascii")
    ).hexdigest()
    try:
        spool = FrameSpool.open_existing(
            spool_root, catalog.evidence.budget, recording_id,
        )
    except (FrameSpoolError, OSError):
        raise _video.VideoProtocolError("Video source spool is unavailable") from None
    maximum_input = min(
        limits.max_compressed_segment_bytes,
        limits.max_frames_per_segment * limits.max_frame_bytes,
    )
    if (spool.max_frames != 3 * limits.max_frames_per_segment + limits.max_queue_frames
            or spool.max_bytes != 3 * maximum_input + limits.max_queue_bytes):
        spool.close()
        raise _video.VideoProtocolError(
            "Frame spool bounds differ from historical video limits"
        )
    return spool


def _source_spool_retired_empty(catalog, recording_id, limits):
    spool = _open_spool(catalog, recording_id, limits)
    try:
        _require(spool._retired, "Video source spool has not retired")
        snapshot = spool.snapshot()
        _require(snapshot["activeCount"] == 0
                 and snapshot["stagingCount"] == 0
                 and snapshot["releasableCount"] == 0
                 and not any(spool.root.joinpath("frames").iterdir()),
                 "Video source spool still contains cleanup residue")
    finally:
        spool.close()


def _finish_unavailable_cleanup(catalog, recovery, recording_id, limits, state):
    # The original already passed G2 freeze; only the source cleanup marker is
    # safe to advance when its pinned manifest has since expired.  In
    # particular, do not synthesize a replacement manifest or mutate G2 media.
    _require(state in {
        "frozen-complete", "frozen-incomplete",
    }, "Unavailable source outcome is not frozen")
    _source_spool_retired_empty(catalog, recording_id, limits)
    recovery.complete_source_cleanup()
    return {
        "recordingId": recording_id,
        "status": "unavailable",
        "reason": "source-manifest-unavailable",
        "cleanupComplete": True,
    }


def _build_manifest(catalog, recovery, recording_id, limits, barrier, sources):
    (
        frame_rows, frames_by_sequence, frames_by_acquisition, segment_rows,
        outcomes, retained_reservations, durable_sequences,
    ) = _segments(catalog, recording_id, limits, sources)
    losses = _load_losses(catalog, recording_id)
    covered, native_ranges = _covered_sources(losses, sources)
    generated = []
    for source in sources:
        sequence = source["recordingFrameSequence"]
        if sequence in durable_sequences or sequence in covered:
            continue
        entry = frames_by_sequence.get(sequence)
        state = None if entry is None else entry["row"]["state"]
        generated.append(_source_loss(
            source, "accepted" if state == "accepted" else "dropped",
            None if entry is None else entry["row"]["segment_index"],
        ))
    for source in sources:
        if "nativeSequenceGap" not in source:
            continue
        native_loss = _native_gap_loss(source)
        native_range = tuple(_validate_gap(source["nativeSequenceGap"]))
        if native_range not in native_ranges:
            generated.append(native_loss)
    if not sources and not any(loss.get("lossClass") == "not-acquired" for loss in losses):
        generated.append({
            "lossClass": "not-acquired", "stage": "native",
            "reason": "process-interruption",
            "recordingInterval": {
                "startOffsetMs": 0, "endOffsetMs": barrier["offsetMs"],
            },
        })
    losses = _coalesce_losses([*losses, *generated])
    segments = [outcome["manifest"] for outcome in outcomes]
    losses = _ensure_source_sampling_marker(losses, sources, segments)
    _require(len(segments) <= limits.max_segments, "Video segment limit exceeded")
    body = {
        "schemaVersion": 2,
        "kind": "reproloop-avfoundation-video",
        "recordingId": recording_id,
        "status": "incomplete",
        "failureReason": "process-interruption",
        "codec": "h264",
        "container": "mp4",
        "samplingMode": (
            "irregular-source-capture" if sources
            else "no-acquired-frames"
        ),
        "segmentDurationBoundMs": limits.segment_duration_ms,
        "segments": segments,
        "losses": losses,
        "eventMappings": _video._event_mappings(
            recovery.video_event_snapshot(), segments, losses,
        ),
        "limits": _video._limits_manifest(limits, source_mode=VIDEO_SOURCE_MODE),
        "sourceDisposition": "transient-after-durable-outcome",
        "sourceFrames": [{
            "sequence": source["recordingFrameSequence"],
            "acquisitionSequence": source["acquisitionSequence"],
            "digest": source["digest"],
        } for source in sources],
    }
    try:
        body = _video.validate_video_manifest(body)
    except (_video.VideoProtocolError, TypeError, ValueError):
        raise _video.VideoProtocolError("Recovered video manifest is invalid") from None
    return body, segment_rows, outcomes, retained_reservations


def _publish_manifest(catalog, recording_id, manifest, retain_until):
    body = _canonical(manifest, _video.MAX_VIDEO_MANIFEST_BYTES,
                      "Recovered video manifest is too large")
    reservation = catalog.evidence.budget.reserve(
        recording_id, "finalization", len(body) + OBJECT_METADATA_BYTES,
    )
    try:
        reference = catalog.evidence.put_bytes(
            body, owner=recording_id, retention_class="original",
            retain_until_ms=retain_until, reservation=reservation,
        )
    except Exception:
        reservation.close()
        raise
    _require(reference.digest == hashlib.sha256(body).hexdigest(),
             "Recovered video manifest digest differs")
    return reference


def _finish_recovery(catalog, recovery, row, manifest, reference,
                     segment_rows, retained_reservations, outcomes, limits):
    _update_video_session(catalog, recovery.recording_id, manifest, reference)
    recovery.freeze_recovered()
    _cleanup_sources(catalog, recovery, recovery.recording_id, manifest,
                     reference, outcomes, limits)
    # The source cleanup proof is complete before any video queue/encoding
    # charge or non-durable segment reservation is released.
    catalog._clean_recording_work(recovery.recording_id, limits)
    current = _session_row(catalog, recovery.recording_id)
    catalog._release_recovered_resources(recovery.recording_id, current)
    _release_nondurable_segment_reservations(
        catalog, segment_rows, retained_reservations,
    )
    return catalog.load(recovery.recording_id)


def recover_transient(catalog, recovery):
    """Recover one stopped transient video recording without new authority."""
    from .recording_session import RecordingRecoverySession
    _require(type(recovery) is RecordingRecoverySession,
             "Video recovery authority is unavailable")
    recording_id = recovery.recording_id
    _identifier(recording_id, "recording identity")
    with _recording_lock(catalog, recording_id):
        metadata, limits, retain_until, source_digest = _validate_metadata(recovery)
        _require(getattr(recovery.store, "evidence", None) is catalog.evidence,
                 "Video recovery authority belongs to another evidence store")
        barrier = _validate_barrier(recovery)
        sources = _validate_sources(recovery, barrier)
        if not metadata["cleanupComplete"]:
            probe = _open_spool(catalog, recording_id, limits)
            probe.close()
        row = _session_row(catalog, recording_id)
        if row is None:
            row = _reconstruct_session(
                catalog, recording_id, limits, retain_until,
                source_digest, metadata["state"],
            )
        _validate_session_row(row, limits, retain_until)

        # A completed source cleanup is terminal.  Re-read the exact attached
        # manifest and leave the frozen G2 original untouched.
        if metadata["cleanupComplete"]:
            _require(source_digest is not None,
                     "Completed source cleanup has no attached manifest")
            manifest, reference = _manifest_from_existing(
                catalog, source_digest, recording_id, sources,
            )
            _require(row["manifest_digest"] in {None, source_digest},
                     "Video and G2 manifest identities differ")
            if row["manifest_digest"] is None:
                _update_video_session(catalog, recording_id, manifest, reference)
            _source_spool_retired_empty(catalog, recording_id, limits)
            catalog._clean_recording_work(recording_id, limits)
            catalog._release_recovered_resources(recording_id, _session_row(catalog, recording_id))
            return catalog.load(recording_id)

        if source_digest is not None:
            try:
                manifest, reference = _manifest_from_existing(
                    catalog, source_digest, recording_id, sources,
                )
            except _video.VideoProtocolError:
                # A source outcome may have been unpinned immediately after
                # spool retirement and before G2's cleanup marker committed.
                # Only that already-frozen, already-retired case can advance
                # the marker without reading or rewriting the original.
                if (catalog.evidence.lookup(source_digest) is None
                        and metadata["state"] in {
                            "frozen-complete", "frozen-incomplete",
                        }):
                    return _finish_unavailable_cleanup(
                        catalog, recovery, recording_id, limits, metadata["state"],
                    )
                raise
            if row["manifest_digest"] is not None:
                _require(row["manifest_digest"] == source_digest,
                         "Video and G2 manifest identities differ")
            # Existing G2 attachment is already authoritative; only rebuild
            # the video journal row if the crash preceded its journal commit.
            if row["manifest_digest"] is None:
                _update_video_session(catalog, recording_id, manifest, reference)
            current = _session_row(catalog, recording_id)
            segment_rows = _segment_rows(catalog, recording_id)
            outcomes = _outcomes_from_existing(
                catalog, recording_id, manifest, segment_rows,
            )
            return _finish_recovery(
                catalog, recovery, current, manifest, reference,
                segment_rows, _retained_from_rows(segment_rows), outcomes, limits,
            )

        manifest, segment_rows, outcomes, retained = _build_manifest(
            catalog, recovery, recording_id, limits, barrier, sources,
        )
        reference = _publish_manifest(catalog, recording_id, manifest, retain_until)
        artifacts, timings = _video._video_artifacts(
            manifest, reference, barrier["offsetMs"],
        )
        recovery.attach_finalized_video(
            artifacts, timings, incomplete_reason="process-interruption",
            source_manifest=manifest,
        )
        current = _session_row(catalog, recording_id)
        return _finish_recovery(
            catalog, recovery, current, manifest, reference,
            segment_rows, retained, outcomes, limits,
        )


def _segment_rows(catalog, recording_id):
    with catalog.journal._lock:
        rows = catalog.journal.connection.execute(
            "SELECT * FROM segments WHERE recording_id=? ORDER BY segment_index LIMIT 129",
            (recording_id,),
        ).fetchall()
    _require(len(rows) <= 128, "Video segment journal exceeds its bound")
    return rows


def _retained_from_rows(rows):
    return {row["reservation_id"] for row in rows
            if row["state"] == "durable" and row["reservation_id"] is not None}


def _outcomes_from_existing(catalog, recording_id, manifest, rows):
    by_index = {row["segment_index"]: row for row in rows}
    outcomes = []
    for segment in manifest["segments"]:
        row = by_index.get(segment["segmentIndex"])
        if row is None:
            continue
        _require(row["state"] == "durable" and row["manifest_json"] is not None,
                 "Attached video segment journal is incomplete")
        _require(_load_json(row["manifest_json"], "Stored video segment is invalid") == segment,
                 "Attached video segment journal differs")
        _require(row["reference_json"] is not None,
                 "Attached video segment reference is incomplete")
        value = _load_json(row["reference_json"], "Stored video reference is invalid")
        _require(type(value) is dict and set(value) == _REFERENCE_KEYS,
                 "Stored video reference is invalid")
        reference = _reference(catalog, value["digest"], value["bytes"], row["reservation_id"])
        _require(reference.digest == segment["digest"]
                 and reference.bytes == segment["bytes"]
                 and reference.path == segment["path"],
                 "Attached video segment reference differs")
        outcomes.append({"manifest": segment, "reservationId": row["reservation_id"]})
    return outcomes


__all__ = ["recover_transient"]
