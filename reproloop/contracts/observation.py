"""Observation coverage with explicit trusted timing requirements."""
from __future__ import annotations

import copy

from .versions import (
    bounded_int, bounded_list, epoch_ms, exact, require, validate_id,
    validate_version,
)


def validate_observation(value):
    exact(value, ("schemaVersion", "id", "providerIncarnation", "applicationId",
                  "intervalMs", "clockUncertaintyMs", "scope", "targets", "properties",
                  "limits", "truncated", "errors", "completeness", "coverage"),
          ("samplesMs",))
    validate_version(value["schemaVersion"])
    for field in ("id", "providerIncarnation", "applicationId"):
        validate_id(value[field], field)
    interval = value["intervalMs"]
    exact(interval, ("start", "end"))
    epoch_ms(interval["start"], "capture start")
    epoch_ms(interval["end"], "capture end")
    require(0 <= interval["end"] - interval["start"] <= 600000, "Invalid capture interval")
    bounded_int(value["clockUncertaintyMs"], "clock uncertainty", 0, 60000)
    require(value["scope"] in ("root", "window"), "Invalid observation scope")
    for field, maximum in (("targets", 256), ("properties", 128), ("errors", 64)):
        items = bounded_list(value[field], field, maximum)
        for item in items:
            validate_id(item, field)
        require(len(items) == len(set(items)), "Duplicate observation metadata")
    limits = value["limits"]
    exact(limits, ("nodes", "bytes", "depth"))
    bounded_int(limits["nodes"], "node limit", 1, 100000)
    bounded_int(limits["bytes"], "byte limit", 1, 50 * 1024 * 1024)
    bounded_int(limits["depth"], "depth limit", 1, 512)
    require(type(value["truncated"]) is bool, "Invalid truncation flag")
    require(value["completeness"] in ("complete", "partial", "unsupported"),
            "Invalid observation completeness")
    require(not value["truncated"] or value["completeness"] != "complete",
            "Truncated observation cannot be complete")
    require(value["coverage"] in ("snapshot", "sampled", "continuous"), "Invalid coverage")
    samples = bounded_list(value.get("samplesMs", []), "sample times", 100000)
    for sample in samples:
        epoch_ms(sample, "sample time")
        require(interval["start"] <= sample <= interval["end"], "Sample outside capture interval")
    require(samples == sorted(set(samples)), "Invalid sample order")
    require(value["coverage"] == "sampled" or not samples, "Unexpected sample times")
    return copy.deepcopy(value)


def validate_coverage_requirement(value, *, relative=False):
    exact(value, ("class", "windowMs", "maxUncertaintyMs", "maxAgeMs", "scope", "properties"),
          ("samplingIntervalMs",))
    require(value["class"] in ("snapshot", "sampled", "continuous"), "Invalid predicate class")
    window = value["windowMs"]
    exact(window, ("start", "end"))
    for field in ("start", "end"):
        if relative:
            bounded_int(window[field], "relative observation window", 0, 600000)
        else:
            epoch_ms(window[field], "observation window")
    require(0 <= window["end"] - window["start"] <= 60000, "Invalid observation window")
    bounded_int(value["maxUncertaintyMs"], "maximum uncertainty", 0, 60000)
    bounded_int(value["maxAgeMs"], "maximum age", 0, 60000)
    require(value["scope"] in ("root", "window"), "Invalid required scope")
    properties = bounded_list(value["properties"], "required properties", 128, minimum=1)
    for item in properties:
        validate_id(item, "required property")
    require(len(properties) == len(set(properties)), "Duplicate required property")
    if value["class"] == "sampled":
        bounded_int(value.get("samplingIntervalMs"), "sampling interval", 100, 60000)
    else:
        require("samplingIntervalMs" not in value, "Unexpected sampling interval")
    return copy.deepcopy(value)


def bind_coverage_requirement(requirement, *, anchor_ms):
    """Bind a frozen relative window to the trusted oracle-start clock of one run."""
    result = validate_coverage_requirement(requirement, relative=True)
    epoch_ms(anchor_ms, "trusted observation anchor")
    result["windowMs"] = {key: epoch_ms(anchor_ms + value, "bound observation time")
                          for key, value in result["windowMs"].items()}
    return result


def observation_result(observation, requirement, *, evaluatedAtMs=None):
    """Check coverage metadata; the runner separately checks values and source trust."""
    obs = validate_observation(observation)
    req = validate_coverage_requirement(requirement)
    now = epoch_ms(evaluatedAtMs, "trusted evaluation time")
    if obs["truncated"] or obs["errors"] or obs["completeness"] != "complete":
        return "unknown"
    if (obs["coverage"] != req["class"] or obs["scope"] != req["scope"]
            or not set(req["properties"]) <= set(obs["properties"])):
        return "unknown"
    uncertainty = obs["clockUncertaintyMs"]
    start, end = obs["intervalMs"]["start"], obs["intervalMs"]["end"]
    low, high = req["windowMs"]["start"], req["windowMs"]["end"]
    if uncertainty > req["maxUncertaintyMs"] or now < end + uncertainty:
        return "unknown"
    if now - (end - uncertainty) > req["maxAgeMs"]:
        return "unknown"
    if req["class"] == "snapshot":
        # The approved uncertainty is the explicit tolerance of a snapshot window.
        tolerance = req["maxUncertaintyMs"]
        if start - uncertainty < low - tolerance or end + uncertainty > high + tolerance:
            return "unknown"
    elif start + uncertainty > low or end - uncertainty < high:
        return "unknown"
    if req["class"] == "sampled":
        samples = obs.get("samplesMs", [])
        if not samples:
            return "unknown"
        # Bracket only the required window; unrelated samples cannot hide its gaps.
        before = [sample for sample in samples if sample <= low]
        after = [sample for sample in samples if sample >= high]
        inside = [sample for sample in samples if low < sample < high]
        points = ([before[-1]] if before else [low]) + inside + ([after[0]] if after else [high])
        if not any(low - req["samplingIntervalMs"] <= sample <= high + req["samplingIntervalMs"]
                   for sample in samples):
            return "unknown"
        if any(b - a + 2 * uncertainty > req["samplingIntervalMs"]
               for a, b in zip(points, points[1:])):
            return "unknown"
    return "covered"
