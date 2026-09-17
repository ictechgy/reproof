"""Exact source coverage for video manifests with transient capture bytes.

Source digests are commitments. They are not paths or downloadable CAS
references. The recording store separately compares this ledger and every
frame's geometry/timing against its own admission journal before freezing.
"""
from bisect import bisect_left
import re

from ..core import ContractError


MAX_SOURCE_FRAMES = 10_000
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BYTE_LOSSES = frozenset({"captured-dropped", "encoder-accepted-not-durable"})


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _range(value, maximum):
    _require(type(value) is dict and set(value) == {"first", "last"},
             "Invalid video source range")
    first, last = value["first"], value["last"]
    _require(type(first) is int and type(last) is int
             and 1 <= first <= last <= maximum, "Invalid video source range")
    return first, last


def validate_source_coverage(sources, segments, losses):
    """Require exactly one durable byte outcome for every admitted source.

    Common segment/loss fields are validated by the video manifest parser.
    This check binds their additional v2 sequence fields to a bounded source
    ledger. Timing uncertainty does not account for missing frame bytes, and
    native losses cannot overlap any source actually admitted by G2.
    """
    _require(type(sources) is list and len(sources) <= MAX_SOURCE_FRAMES,
             "Invalid video source ledger")
    _require(type(segments) is list and len(segments) <= 128,
             "Invalid video source segments")
    _require(type(losses) is list and len(losses) <= 2 * MAX_SOURCE_FRAMES,
             "Invalid video source losses")
    native_sequences = []
    previous_native = 0
    for sequence, item in enumerate(sources, 1):
        _require(type(item) is dict and set(item) == {
            "sequence", "acquisitionSequence", "digest"
        }, "Invalid video source ledger entry")
        native = item["acquisitionSequence"]
        _require(type(item["sequence"]) is int and item["sequence"] == sequence
                 and type(native) is int and previous_native < native < 2 ** 63
                 and type(item["digest"]) is str
                 and _DIGEST.fullmatch(item["digest"]) is not None,
                 "Invalid video source identity")
        previous_native = native
        native_sequences.append(native)

    covered = bytearray(len(sources))
    previous_durable_sequence = 0
    for segment in segments:
        _require(type(segment) is dict and type(segment.get("frames")) is list
                 and len(segment["frames"]) <= 256,
                 "Invalid video source segment")
        for frame in segment["frames"]:
            _require(type(frame) is dict, "Invalid video source frame")
            sequence = frame.get("recordingFrameSequence")
            _require(type(sequence) is int and 1 <= sequence <= len(sources),
                     "Invalid video source frame sequence")
            _require(sequence > previous_durable_sequence,
                     "Durable video source order changed")
            previous_durable_sequence = sequence
            item = sources[sequence - 1]
            _require(frame.get("acquisitionSequence") == item["acquisitionSequence"]
                     and type(frame.get("acquisitionSequence")) is int
                     and frame.get("sourceDigest") == item["digest"]
                     and not covered[sequence - 1],
                     "Video source outcome differs or is duplicated")
            covered[sequence - 1] = 1

    native_ranges = []
    for loss in losses:
        _require(type(loss) is dict, "Invalid video source loss")
        source_range = loss.get("recordingFrameRange")
        native_range = loss.get("nativeSequenceRange")
        has_source_range = "recordingFrameRange" in loss
        has_native_range = "nativeSequenceRange" in loss
        _require(not (has_source_range and has_native_range),
                 "Video loss has conflicting sequence domains")
        kind = loss.get("lossClass")
        if has_source_range:
            first, last = _range(source_range, len(sources))
            _require(kind in _BYTE_LOSSES or kind == "unknown-acquisition-interval",
                     "Video source was admitted before its loss")
            if "acquisitionSequence" in loss:
                _require(first == last
                         and type(loss["acquisitionSequence"]) is int
                         and loss["acquisitionSequence"]
                         == sources[first - 1]["acquisitionSequence"],
                         "Video source loss identity differs")
            if kind in _BYTE_LOSSES:
                for index in range(first - 1, last):
                    _require(not covered[index], "Video source outcome is duplicated")
                    covered[index] = 1
        elif has_native_range:
            first, last = _range(native_range, 2 ** 63 - 1)
            _require(kind in {"not-acquired", "captured-dropped"}
                     and loss.get("stage") in {"native", "transport"}
                     and "acquisitionSequence" not in loss,
                     "Invalid native acquisition loss")
            index = bisect_left(native_sequences, first)
            _require(index == len(native_sequences) or native_sequences[index] > last,
                     "Native loss overlaps an admitted video source")
            native_ranges.append((first, last))
        else:
            _require(kind in {"not-acquired", "unknown-acquisition-interval"}
                     and "acquisitionSequence" not in loss,
                     "Video source loss has no sequence domain")

    previous_end = 0
    for first, last in sorted(native_ranges):
        _require(first > previous_end, "Native acquisition loss is duplicated")
        previous_end = last
    _require(all(covered), "Video source outcome is missing")
