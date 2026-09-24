"""Strict, side-effect-free contracts for Live gesture recordings.

The Live model owns sessions and persistence.  This module only validates an
already frozen document and derives a new document from one; it never opens a
recording file or talks to a provider.
"""
from __future__ import annotations

import copy
import math
import re
import uuid
from typing import Any

from ..core import ContractError, digest
from .model import validate_gesture


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_VARIABLE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BUNDLE = re.compile(r"[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)+\Z")
_PRIVATE = re.compile(r"_[^_]*")

_RECORDING_FIELDS = {
    "schemaVersion", "kind", "id", "sessionId", "deviceId", "applicationIdentity",
    "providerKind", "status", "replayable", "reason", "startingState", "geometry",
    "events", "variables", "media", "startedAt", "endedAt", "provenance", "digest",
}
_EVENT_FIELDS = {
    "id", "action", "payload", "status", "offsetMs", "sourceCommandId", "frameId",
    "geometryVersion", "controllerMode", "timing",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _identifier(value: Any, *, variable: bool = False) -> str:
    pattern = _VARIABLE if variable else _ID
    _require(isinstance(value, str) and pattern.fullmatch(value) is not None, "Invalid identifier")
    return value


def _nonnegative_int(value: Any, message: str) -> int:
    _require(type(value) is int and value >= 0, message)
    return value


def _validate_application_identity(value: Any) -> None:
    if value is None:
        return
    _require(isinstance(value, dict) and all(isinstance(k, str) for k in value),
             "Invalid application identity")
    _require(not any(_PRIVATE.fullmatch(k) for k in value), "Invalid application identity")
    _require(set(value) <= {"bundle", "bundleId", "artifactDigest"}, "Unknown application identity field")
    _require("bundle" in value or "bundleId" in value, "Application identity has no bundle")
    for key, item in value.items():
        _require(isinstance(item, str) and 0 < len(item) <= 255, f"Invalid application identity {key}")
        if key in {"bundle", "bundleId"}:
            _require(_BUNDLE.fullmatch(item) is not None, f"Invalid application identity {key}")
    if "artifactDigest" in value:
        _require(_DIGEST.fullmatch(value["artifactDigest"]) is not None, "Invalid application artifact digest")


def _validate_geometry(value: Any) -> None:
    _require(isinstance(value, dict) and set(value) == {"width", "height", "orientation"},
             "Invalid recording geometry")
    _require(type(value["width"]) is int and 0 < value["width"] <= 8192, "Invalid recording width")
    _require(type(value["height"]) is int and 0 < value["height"] <= 8192, "Invalid recording height")
    _require(value["orientation"] in {"portrait", "landscape"}, "Invalid recording orientation")


def _validate_event(event: Any, previous_offset: int | None, variables: set[str]) -> int:
    _require(isinstance(event, dict) and set(event) == _EVENT_FIELDS, "Unknown or incomplete recording event")
    for key in event:
        _require(not key.startswith("_"), "Private recording event field")
    event_id = _identifier(event["id"])
    action = event["action"]
    _require(isinstance(action, str), "Invalid recording action")
    payload = event["payload"]
    _require(isinstance(payload, dict), "Invalid recording payload")
    if action == "text":
        _require(set(payload) == {"variable"}, "Raw text is not allowed in recordings")
        variable = _identifier(payload["variable"], variable=True)
        _require(variable in variables, "Recording event references an undeclared variable")
    else:
        try:
            validate_gesture(action, payload)
        except (ContractError, TypeError, ValueError) as error:
            raise ContractError("Invalid recording gesture") from error
    _require(event["status"] == "injected", "Only confirmed inputs can be recorded")
    offset = event["offsetMs"]
    _require(type(offset) is int and 0 <= offset <= 600000, "Invalid recording event offset")
    _require(previous_offset is None or offset >= previous_offset, "Recording event offsets are out of order")
    _identifier(event["sourceCommandId"])
    _require(type(event["frameId"]) is int and event["frameId"] > 0, "Invalid recording frame id")
    _require(type(event["geometryVersion"]) is int and event["geometryVersion"] > 0,
             "Invalid recording geometry version")
    _require(event["controllerMode"] in {"manual", "automation"}, "Invalid recording controller mode")
    _require(event["timing"] == "best-effort", "Invalid recording timing")
    return offset


def _validate_pointer_trace(events: list[dict[str, Any]], replayable: bool) -> None:
    active: dict[int, int] = {}
    for event in events:
        action = event["action"]
        if active and action != "pointer":
            raise ContractError("Other gestures cannot interleave an active pointer trace")
        if action != "pointer":
            continue
        payload = event["payload"]
        phase = payload["phase"]
        pointer_id = payload["pointerId"]
        if phase == "down":
            _require(pointer_id not in active, "Duplicate pointer down")
            _require(len(active) < 5, "Too many active pointers")
            active[pointer_id] = event["geometryVersion"]
        elif phase in {"move", "up"}:
            if pointer_id not in active:
                # A derived recording may intentionally start in the middle
                # of a gesture; it remains non-replayable until revalidated.
                _require(not replayable, "Pointer move/up has no active down")
                continue
            _require(event["geometryVersion"] == active[pointer_id],
                     "Pointer geometry changed during a gesture")
            if phase == "up":
                del active[pointer_id]
        else:  # cancel is a session-wide fence and is idempotent.
            if active:
                _require(all(event["geometryVersion"] == geometry for geometry in active.values()),
                         "Pointer geometry changed during a gesture")
            active.clear()
    _require(not active or not replayable, "Replayable recording ends with active pointers")


def _validate_provenance(value: Any, event_ids: list[str], recording_id: str) -> None:
    _require(isinstance(value, dict) and set(value) == {"kind", "sourceRecordingId", "sourceDigest", "transform"},
             "Invalid recording provenance")
    _require(value["kind"] == "derived", "Invalid recording provenance kind")
    source_id = _identifier(value["sourceRecordingId"])
    _require(source_id != recording_id, "Derived recording cannot reference itself")
    _require(isinstance(value["sourceDigest"], str) and _DIGEST.fullmatch(value["sourceDigest"]) is not None,
             "Invalid source recording digest")
    transform = value["transform"]
    _require(isinstance(transform, dict) and set(transform) == {"eventIds", "speed"},
             "Invalid recording transform")
    selected = transform["eventIds"]
    _require(isinstance(selected, list) and len(selected) <= 500, "Invalid transformed event list")
    _require(all(isinstance(item, str) for item in selected), "Invalid transformed event identity")
    _require(len(selected) == len(set(selected)) and selected == event_ids,
             "Transformed event list does not match recording events")
    speed = transform["speed"]
    _require(type(speed) in (int, float) and math.isfinite(speed) and 0.25 <= speed <= 4,
             "Replay speed must be between 0.25 and 4")


def validate_recording(document: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a detached, immutable-in-contract recording copy."""
    _require(isinstance(document, dict), "Recording must be an object")
    _require(not any(not isinstance(key, str) or key.startswith("_") for key in document),
             "Unknown or private recording field")
    _require(set(document) <= _RECORDING_FIELDS and "digest" in document, "Unknown recording field")
    checked = copy.deepcopy(document)
    supplied_digest = checked.pop("digest")
    _require(isinstance(supplied_digest, str) and _DIGEST.fullmatch(supplied_digest) is not None,
             "Invalid recording digest")
    try:
        expected_digest = digest(checked)
    except (TypeError, ValueError):
        raise ContractError("Recording cannot be canonically digested") from None
    _require(supplied_digest == expected_digest, "Recording integrity mismatch")

    _require(checked.get("schemaVersion") == 1 and type(checked.get("schemaVersion")) is int,
             "Unsupported recording schema")
    _require(checked.get("kind") == "live-gesture-recording", "Unsupported recording kind")
    recording_id = _identifier(checked.get("id"))
    _identifier(checked.get("sessionId"))
    _identifier(checked.get("deviceId"))
    _identifier(checked.get("providerKind"))
    _require(checked.get("status") in {"complete", "invalid"}, "Recording is not frozen")
    _require(type(checked.get("replayable")) is bool, "Invalid replayability flag")
    if "applicationIdentity" in checked:
        _validate_application_identity(checked["applicationIdentity"])
    if "reason" in checked:
        _require(isinstance(checked["reason"], str) and _VARIABLE.fullmatch(checked["reason"]) is not None,
                 "Invalid recording reason")
    starting = checked.get("startingState")
    _require(isinstance(starting, dict) and set(starting) == {"kind"} and starting["kind"] in {"provider-reset", "unknown"},
             "Invalid recording starting state")
    _validate_geometry(checked.get("geometry"))
    _require(checked.get("media") == "frame-references-only", "Recording media must be frame references only")
    variables = checked.get("variables")
    _require(isinstance(variables, list) and len(variables) <= 500, "Invalid recording variables")
    declared = {_identifier(item, variable=True) for item in variables}
    _require(len(declared) == len(variables), "Duplicate recording variable")
    events = checked.get("events")
    _require(isinstance(events, list) and 0 <= len(events) <= 500, "Empty or oversized recording")
    event_ids: list[str] = []
    previous_offset: int | None = None
    for event in events:
        event_id = _identifier(event.get("id") if isinstance(event, dict) else None)
        _require(event_id not in event_ids, "Duplicate recording event identity")
        event_ids.append(event_id)
        previous_offset = _validate_event(event, previous_offset, declared)
    used = {event["payload"]["variable"] for event in events if event["action"] == "text"}
    _require(used == declared, "Recording variables do not exactly match text events")
    _validate_pointer_trace(events, checked["replayable"])
    _nonnegative_int(checked.get("startedAt"), "Invalid recording start timestamp")
    _nonnegative_int(checked.get("endedAt"), "Invalid recording end timestamp")
    _require(checked["endedAt"] >= checked["startedAt"], "Recording timestamps are out of order")

    provenance = checked.get("provenance")
    if provenance is not None:
        _validate_provenance(provenance, event_ids, recording_id)
    basic_replayable = (
        checked["status"] == "complete" and bool(events)
        and starting["kind"] == "provider-reset" and checked["media"] == "frame-references-only"
    )
    edited = provenance is not None and checked.get("reason") == "edited_sequence_requires_validation"
    _require(not checked["replayable"] or (basic_replayable and not edited),
             "Replayability does not match recording state")
    if checked["replayable"]:
        _require("reason" not in checked, "Replayable recording cannot have a reason")
    if checked["status"] == "invalid":
        _require(not checked["replayable"], "Invalid recording cannot be replayable")
    checked["digest"] = supplied_digest
    return checked


def derive_recording(recording: dict[str, Any], *, event_ids: list[str] | None = None,
                     speed: float = 1.0, new_id: str | None = None) -> dict[str, Any]:
    """Create a detached derived recording without touching the source document."""
    source = validate_recording(recording)
    _require(source["status"] == "complete", "Only complete recordings can be derived")
    _require(type(speed) in (int, float) and not isinstance(speed, bool)
             and math.isfinite(speed) and 0.25 <= speed <= 4, "Replay speed must be between 0.25 and 4")
    source_events = source["events"]
    source_ids = [event["id"] for event in source_events]
    selected_ids = source_ids[:] if event_ids is None else event_ids
    _require(isinstance(selected_ids, list), "event_ids must be a list")
    _require(all(isinstance(item, str) for item in selected_ids), "Invalid derived event identity")
    _require(len(selected_ids) == len(set(selected_ids)), "Duplicate derived event identity")
    _require(all(item in source_ids for item in selected_ids),
             "Unknown derived event identity")
    positions = [source_ids.index(item) for item in selected_ids]
    _require(positions == sorted(positions), "Derived events must preserve source order")
    identifier = uuid.uuid4().hex if new_id is None else _identifier(new_id)
    _require(identifier != source["id"], "Derived recording must have a new identity")

    selected = [copy.deepcopy(source_events[index]) for index in positions]
    for event in selected:
        offset = round(event["offsetMs"] / speed)
        _require(0 <= offset <= 600000, "Derived event offset exceeds recording limit")
        event["offsetMs"] = offset
    result = copy.deepcopy(source)
    result["id"] = identifier
    result["events"] = selected
    result["variables"] = []
    for event in selected:
        if event["action"] == "text":
            variable = event["payload"]["variable"]
            if variable not in result["variables"]:
                result["variables"].append(variable)
    result["provenance"] = {
        "kind": "derived",
        "sourceRecordingId": source["id"],
        "sourceDigest": source["digest"],
        "transform": {"eventIds": selected_ids[:], "speed": speed},
    }
    if selected_ids != source_ids:
        result["replayable"] = False
        result["reason"] = "edited_sequence_requires_validation"
    result.pop("digest", None)
    result["digest"] = digest(result)
    return validate_recording(result)
