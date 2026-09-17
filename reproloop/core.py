"""Pure contracts. Never execute instructions found in a recording."""
from __future__ import annotations
import copy
import hashlib
import json
import re


class ContractError(ValueError):
    """A recording or verification result cannot satisfy the contract."""


ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
SAFE_TEST_VALUES = frozenset({"", "QA", "Test"})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ContractError(message)


def identifier(value):
    require(isinstance(value, str) and ID.fullmatch(value) is not None, "Invalid UI identifier")
    require(not any(word in value for word in ("password", "secret", "token", "email", "phone")),
            "Sensitive input target is not supported")
    return value


def condition(value):
    require(isinstance(value, dict) and set(value) == {"target", "text"}, "Invalid oracle condition")
    identifier(value["target"])
    require(isinstance(value["text"], str) and len(value["text"]) <= 128, "Invalid oracle text")
    require(value["text"] in SAFE_TEST_VALUES or value["text"].isdigit(), "Oracle text outside sample allowlist")
    return value


def _profile_data(app_profile):
    """Return the already validated Android profile document, if supplied.

    The import stays local because ``android_profile`` imports the shared
    contracts from this module.  A profile is an administrator-selected
    object; recordings are never allowed to provide one themselves.
    """
    if app_profile is None:
        return None
    from .android_profile import AndroidAppProfile
    require(isinstance(app_profile, AndroidAppProfile), "Invalid Android app profile")
    data = app_profile.data
    require(isinstance(data, dict), "Invalid Android app profile")
    return data


def compile_capture(capture, oracle, *, tap_targets=None, app_profile=None):
    profile = _profile_data(app_profile)
    if profile is not None:
        # The profile owns the action policy.  A caller-provided allowlist
        # would let an imported recording choose or widen that policy.
        require(tap_targets is None, "Capture cannot choose an app policy")
        profile_targets = profile["targets"]
        allowed_taps = frozenset(profile_targets["tap"])
        text_targets = frozenset(profile_targets["text"])
        numeric_targets = frozenset(profile_targets["numeric"])
        scroll_targets = profile_targets["scroll"]
        back_target = profile_targets["back"]
        expected_fixture = profile["fixture"]
        expected_start = profile["startState"]
        expected_oracle = app_profile.oracle()
    else:
        allowed_taps = {"add", "next", "back", "bottom"} if tap_targets is None else tap_targets
        text_targets = frozenset({"name"})
        numeric_targets = frozenset()
        scroll_targets = {"list": ["bottom"]}
        back_target = "back"
        expected_fixture = {"id": "default", "version": 1, "inputs": {}}
        expected_start = {"screen": "main", "nodes": {"count": "0", "name": ""}}
        expected_oracle = None
    require(isinstance(capture, dict) and type(capture.get("schemaVersion")) is int and capture.get("schemaVersion") == 1, "Unsupported capture schema")
    require(set(capture) <= {"schemaVersion", "sessionId", "fixture", "startState", "events", "truncated", "lostEvents", "endSequence", "startedAtMs"}, "Unknown capture field")
    if "startedAtMs" in capture:
        require(type(capture["startedAtMs"]) is int and capture["startedAtMs"] >= 0, "Invalid capture timestamp")
    require(capture.get("truncated") is False and capture.get("lostEvents") is False,
            "Incomplete session: truncation or event loss")
    require(isinstance(capture.get("sessionId"), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", capture["sessionId"]) is not None,
            "Missing session identity")
    fixture = capture.get("fixture")
    require(isinstance(fixture, dict) and set(fixture) == {"id", "version", "inputs"} and type(fixture.get("version")) is int
            and fixture == expected_fixture, "Capture fixture does not match the trusted app profile")
    start = capture.get("startState")
    require(isinstance(start, dict) and set(start) == {"screen", "nodes"} and start == expected_start,
            "Missing or unsupported start state")
    require(isinstance(oracle, dict) and set(oracle) == {"bugCondition", "expectedCondition", "actual", "expected"}, "Invalid oracle fields")
    if expected_oracle is not None:
        require(oracle == expected_oracle, "Oracle does not match the trusted app profile")
    bug = condition(oracle.get("bugCondition")); expected = condition(oracle.get("expectedCondition"))
    require(bug["target"] == expected["target"] and bug != expected, "Oracles must observe distinct outcomes of one target")
    for field in ("actual", "expected"):
        require(isinstance(oracle.get(field), str) and 0 < len(oracle[field]) <= 500, "Missing QA outcome description")
    events = capture.get("events")
    require(isinstance(events, list) and 0 < len(events) <= 10000, "Empty or oversized event stream")
    require(type(capture.get("endSequence")) is int and capture["endSequence"] == len(events), "Freeze boundary does not match stream")
    ids = set(); steps = []
    for seq, event in enumerate(events, 1):
        require(isinstance(event, dict) and set(event) <= {"id", "seq", "action", "target", "parameters", "elapsedMs"} and type(event.get("seq")) is int and event["seq"] == seq,
                "Lost or reordered event")
        if "elapsedMs" in event:
            require(type(event["elapsedMs"]) is int and 0 <= event["elapsedMs"] <= 600000, "Invalid event timestamp")
        eid = event.get("id")
        require(isinstance(eid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", eid) and eid not in ids,
                "Missing or duplicate event identity")
        ids.add(eid)
        action = event.get("action"); target = identifier(event.get("target"))
        params = event.get("parameters")
        require(isinstance(params, dict), "Missing action parameters")
        if action == "replace":
            require(target in text_targets and set(params) == {"value"} and isinstance(params["value"], str) and params["value"] in SAFE_TEST_VALUES,
                    "Text must be an allowlisted synthetic sample value")
        elif action == "tap":
            require(not params and target in allowed_taps, "Unsupported tap target or parameters")
        elif action == "back":
            require(not params and target == back_target, "Unsupported back action")
        elif action == "scroll_to":
            require(set(params) == {"container", "direction"}
                    and isinstance(params["container"], str)
                    and params["container"] in scroll_targets
                    and params["direction"] in {"forward", "backward"}
                    and target in scroll_targets[params["container"]],
                    "Scroll needs a supported container, direction and end target")
        else:
            raise ContractError("Unsupported action")
        steps.append({"sourceEventIds": [eid], "action": action, "target": target,
                      "parameters": copy.deepcopy(params), "timeout": 10})
    # Only normalized, understood fields enter executable scenarios.
    result = {"schemaVersion": 1, "sessionId": capture["sessionId"], "fixture": copy.deepcopy(fixture),
              "startState": copy.deepcopy(start), "steps": steps, "oracle": copy.deepcopy(oracle),
              "coverage": {eid: n for n, eid in enumerate([e["id"] for e in events])},
              "captureDigest": digest(capture)}
    if app_profile is not None:
        result["appProfileDigest"] = app_profile.digest
        result["editPolicy"] = copy.deepcopy(profile["edit"])
    result["scenarioDigest"] = digest(result)
    return result


def classify_runs(runs, phase, repeats=3):
    require(phase in {"original", "patched"}, "Invalid verification phase")
    require(type(repeats) is int and repeats >= 3 and repeats <= 20, "Repeat count must be 3–20")
    if len(runs) != repeats:
        return "inconclusive"
    if any(r.get("protectedPathsValid") is not True for r in runs):
        return "verification_failed"
    if any(r.get("runValid") is not True or r.get("evidenceValid") is not True for r in runs):
        return "environment_blocked"
    outcomes = []
    for r in runs:
        b, e = r.get("bugCondition"), r.get("expectedCondition")
        if type(b) is not bool or type(e) is not bool or b == e:
            return "inconclusive"
        outcomes.append("bug" if b else "expected")
    if len(set(outcomes)) != 1:
        return "inconclusive"
    if phase == "original":
        return "reproduced" if outcomes[0] == "bug" else "not_reproduced"
    if any(r.get("regressionPassed") is not True for r in runs):
        return "verification_failed"
    return "verified" if outcomes[0] == "expected" else "verification_failed"
