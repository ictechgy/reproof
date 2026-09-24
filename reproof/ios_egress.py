"""iOS target-app egress policy and byte-counter measurement.

The unsupervised device cannot hard-block target traffic, so egress is
enforced as fail-closed measurement: the XCTest helper samples getifaddrs
interface counters at the candidate window boundaries and the host compares
the aggregated delta against a calibrated noise floor. Malformed policy,
unreadable counters, or a delta above the floor never produce a pass.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from .core import digest, require


_KIND = "ios-egress-policy"
_COUNTERS_SCHEMA = "ios-network-counters"
_INTERFACE = re.compile(r"[a-z][a-z0-9_]{0,15}\Z")
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9_.-]{0,251}[A-Za-z0-9])?\Z")
_MAX_POLICY_BYTES = 32 * 1024
_MAX_INTERFACES = 64
_MAX_ALLOWLIST = 16
_UINT63_MAX = 2**63 - 1


@dataclass(frozen=True)
class IOSEgressPolicy:
    """Canonical immutable view of a validated egress policy."""

    _json: str

    @property
    def data(self):
        return json.loads(self._json)

    @property
    def digest(self):
        return digest(self.data)

    @property
    def noise_floor_bytes(self):
        return self.data["noiseFloorBytes"]

    @property
    def interfaces(self):
        return tuple(self.data["interfaces"])

    @property
    def allowlist(self):
        return tuple(self.data["allowlist"])

    @property
    def capture_mode(self):
        return self.data["capture"]["rvictl"]


def load_egress_policy(raw):
    require(type(raw) is bytes and 0 < len(raw) <= _MAX_POLICY_BYTES,
        "Invalid iOS egress policy encoding")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        require(False, "Invalid iOS egress policy JSON")
    return egress_policy(document)


def egress_policy(document):
    require(type(document) is dict and document.get("kind") == _KIND
        and document.get("version") == 1 and set(document)
        == {"kind", "version", "mode", "interfaces", "noiseFloorBytes", "allowlist", "capture"},
        "Invalid iOS egress policy shape")
    require(document["mode"] == "deny-all", "Invalid iOS egress policy mode")
    interfaces = document["interfaces"]
    require(type(interfaces) is list and 1 <= len(interfaces) <= 16
        and all(type(name) is str and _INTERFACE.fullmatch(name) for name in interfaces)
        and len(set(interfaces)) == len(interfaces)
        and all(not name.startswith(prefix) for name in interfaces
                for prefix in ("lo", "utun", "ipsec", "awdl", "llw", "ap", "anpi", "bridge", "gif", "stf")),
        "Invalid iOS egress interface scope")
    require(type(document["noiseFloorBytes"]) is int
        and 0 <= document["noiseFloorBytes"] <= 64 * 1024 * 1024,
        "Invalid iOS egress noise floor")
    allowlist = document["allowlist"]
    require(type(allowlist) is list and len(allowlist) <= _MAX_ALLOWLIST
        and all(type(entry) is dict and set(entry) == {"host", "port"}
            and type(entry["host"]) is str and _HOST.fullmatch(entry["host"])
            and type(entry["port"]) is int and 1 <= entry["port"] <= 65535
            for entry in allowlist), "Invalid iOS egress allowlist")
    capture = document["capture"]
    require(type(capture) is dict and set(capture) == {"rvictl"}
        and capture["rvictl"] in ("off", "optional"), "Invalid iOS egress capture mode")
    try:
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        require(False, "Invalid iOS egress policy JSON")
    require(len(canonical.encode("utf-8")) <= _MAX_POLICY_BYTES,
        "iOS egress policy size limit exceeded")
    return IOSEgressPolicy(canonical)


def _counter_value(value):
    require(type(value) is int and 0 <= value <= _UINT63_MAX,
        "Invalid iOS network counter value")
    return value


def network_counters(document):
    """Validate a device-reported counter snapshot into a canonical tuple."""
    require(type(document) is dict and document.get("schema") == _COUNTERS_SCHEMA
        and document.get("version") == 1 and set(document)
        == {"schema", "version", "sampledAtMs", "interfaces"},
        "Invalid iOS network counter evidence")
    require(type(document["sampledAtMs"]) is int and document["sampledAtMs"] >= 0,
        "Invalid iOS network counter timestamp")
    interfaces = document["interfaces"]
    require(type(interfaces) is dict and len(interfaces) <= _MAX_INTERFACES
        and all(type(name) is str and _INTERFACE.fullmatch(name) for name in interfaces)
        and all(type(row) is dict and set(row) == {"rxBytes", "txBytes"}
            and _counter_value(row["rxBytes"]) is not None
            and _counter_value(row["txBytes"]) is not None
            for row in interfaces.values()),
        "Invalid iOS network counter set")
    return {name: {"rxBytes": row["rxBytes"], "txBytes": row["txBytes"]}
            for name, row in sorted(interfaces.items())}


def _matched(counters, prefixes):
    return {name: counters[name] for name in counters
            if any(name == prefix or name.startswith(prefix) for prefix in prefixes)}


def counter_delta(start, end, prefixes):
    """Aggregate byte delta over egress-scoped interfaces.

    An interface counted at the start but absent at the end contributes its
    full start value (fail-closed direction); new interfaces appearing only
    in the end snapshot contribute their full end value.
    """
    begin, finish = _matched(start, prefixes), _matched(end, prefixes)
    names = set(begin) | set(finish)
    delta = 0
    for name in names:
        opened = begin.get(name, {"rxBytes": 0, "txBytes": 0})
        closed = finish.get(name, {"rxBytes": 0, "txBytes": 0})
        rx = closed["rxBytes"] - opened["rxBytes"]
        tx = closed["txBytes"] - opened["txBytes"]
        # 카운터 리셋/역전·윈도우 중 인터페이스 소멸은 관측 불가 구간이므로
        # 양쪽 스냅샷의 큰 값을 계상한다(fail-closed 방향).
        delta += (rx if rx >= 0 else max(opened["rxBytes"], closed["rxBytes"]))
        delta += (tx if tx >= 0 else max(opened["txBytes"], closed["txBytes"]))
    return delta, sorted(names)


def measurement_evidence(policy, start, end, *, capture):
    """Build the journaled measurement record; verdict is evaluated by callers."""
    begin = network_counters(start)
    finish = network_counters(end)
    delta, names = counter_delta(begin, finish, policy.interfaces)
    require(type(capture) is dict and set(capture) == {"mode"}
        and capture["mode"] in ("off", "unavailable", "captured"),
        "Invalid iOS egress capture status")
    evidence = {
        "schema": "ios-egress-measurement",
        "version": 1,
        "policyDigest": policy.digest,
        "startCountersDigest": digest(begin),
        "endCountersDigest": digest(finish),
        "deltaBytes": delta,
        "noiseFloorBytes": policy.noise_floor_bytes,
        "scope": {"interfaces": list(policy.interfaces), "matched": names},
        "capture": {"mode": capture["mode"]},
        "verdict": "pass" if delta <= policy.noise_floor_bytes else "violation",
    }
    return evidence
