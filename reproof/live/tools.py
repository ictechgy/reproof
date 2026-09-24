"""Bounded JSON-lines automation bridge for the local Live HTTP client.

This is a small, explicit tool surface for local agents.  It is not an MCP
server and deliberately exposes no shell, filesystem, or arbitrary URL
operation.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from typing import Any, TextIO
from http.client import HTTPException
from urllib.error import URLError
from urllib.parse import quote

from ..core import ContractError
from .client import Client
from .model import LiveError


MAX_LINE = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TOOLS = frozenset({
    "devices.list", "sessions.list", "sessions.create", "sessions.get", "sessions.close",
    "sessions.heartbeat", "control.claim", "frame.observe", "input.send", "recordings.list", "recordings.get",
    "recordings.import", "recordings.derive", "recordings.start", "recordings.stop", "replay.start",
    "replay.cancel", "jobs.list", "jobs.get", "jobs.submit", "jobs.cancel",
    "repairs.list", "repairs.get", "repairs.submit", "repairs.cancel", "repairs.resume",
})


def _fail(message: str = "Invalid request arguments") -> None:
    raise ContractError(message)


def _safe_id(value: Any) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail()
    return value


def _args(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= set(value) or not set(value) <= required | optional:
        _fail()
    return value


def _int(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail()
    return value


def _path(identifier: Any) -> str:
    return quote(_safe_id(identifier), safe="")


def _variables(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail()
    for key, item in value.items():
        _safe_id(key)
        if not isinstance(item, str) or len(item) > 256:
            _fail()
    return value


def _command(value: Any) -> dict[str, Any]:
    fields = {"controllerId", "epoch", "sequence", "commandId", "frameId", "geometryVersion", "action", "payload"}
    if not isinstance(value, dict) or set(value) != fields:
        _fail()
    _safe_id(value["controllerId"])
    _int(value["epoch"], minimum=1)
    _int(value["sequence"], minimum=1)
    _safe_id(value["commandId"])
    _int(value["frameId"], minimum=1)
    _int(value["geometryVersion"], minimum=1)
    if not isinstance(value["action"], str) or value["action"] not in {"tap", "long_press", "swipe", "text", "home", "reset", "pointer"}:
        _fail()
    if not isinstance(value["payload"], dict):
        _fail()
    return value


def _call(client: Any, path: str, body: Any = None) -> dict[str, Any]:
    result = client.call(path, body)
    if not isinstance(result, dict):
        _fail("Local API returned an invalid result")
    return result


def dispatch(client: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one already parsed and structurally valid tool request."""
    if tool not in _TOOLS:
        _fail("Unknown tool")

    if tool == "devices.list":
        _args(arguments, set())
        return _call(client, "/api/devices")
    if tool == "repairs.list":
        _args(arguments, set())
        return _call(client, "/api/repairs")
    if tool in {"repairs.get", "repairs.cancel", "repairs.resume"}:
        value = _args(arguments, {"jobId", "clientId"} if tool == "repairs.resume" else {"jobId"})
        path = f"/api/repairs/{_path(value['jobId'])}"
        if tool == "repairs.get":
            return _call(client, path)
        if tool == "repairs.cancel":
            return _call(client, path + "/cancel", {})
        return _call(client, path + "/resume", {"clientId": _safe_id(value["clientId"])})
    if tool == "repairs.submit":
        value = _args(arguments, {"sessionId", "controllerId", "epoch", "recordingId", "requestId"})
        return _call(client, f"/api/sessions/{_path(value['sessionId'])}/repair", {
            "controllerId": _safe_id(value["controllerId"]), "epoch": _int(value["epoch"], minimum=1),
            "recordingId": _safe_id(value["recordingId"]), "requestId": _safe_id(value["requestId"])})
    if tool == "sessions.list":
        _args(arguments, set())
        return _call(client, "/api/sessions")
    if tool == "sessions.create":
        value = _args(arguments, {"deviceId", "clientId"})
        body = {"deviceId": _safe_id(value["deviceId"]), "clientId": _safe_id(value["clientId"])}
        return _call(client, "/api/sessions", body)
    if tool == "sessions.get":
        value = _args(arguments, {"sessionId"})
        return _call(client, f"/api/sessions/{_path(value['sessionId'])}")
    if tool == "sessions.close":
        value = _args(arguments, {"sessionId", "controllerId", "epoch"})
        body = {"controllerId": _safe_id(value["controllerId"]), "epoch": _int(value["epoch"], minimum=1)}
        return _call(client, f"/api/sessions/{_path(value['sessionId'])}/close", body)
    if tool == "sessions.heartbeat":
        value = _args(arguments, {"sessionId", "clientId"})
        return _call(client, f"/api/sessions/{_path(value['sessionId'])}/heartbeat",
                     {"clientId": _safe_id(value["clientId"])})
    if tool == "control.claim":
        value = _args(arguments, {"sessionId", "clientId", "expectedEpoch", "mode"})
        if value["mode"] not in {"manual", "automation"}:
            _fail()
        body = {"clientId": _safe_id(value["clientId"]), "expectedEpoch": _int(value["expectedEpoch"], minimum=1), "mode": value["mode"]}
        return _call(client, f"/api/sessions/{_path(value['sessionId'])}/control", body)
    if tool == "frame.observe":
        value = _args(arguments, {"sessionId"})
        return _call(client, f"/api/sessions/{_path(value['sessionId'])}/frame")
    if tool == "input.send":
        value = _args(arguments, {"sessionId", "command"})
        return _call(client, f"/api/sessions/{_path(value['sessionId'])}/input", _command(value["command"]))

    if tool == "recordings.list":
        _args(arguments, set())
        return _call(client, "/api/recordings")
    if tool == "recordings.get":
        value = _args(arguments, {"recordingId"})
        return _call(client, f"/api/recordings/{_path(value['recordingId'])}")
    if tool == "recordings.import":
        value = _args(arguments, {"recording"})
        if not isinstance(value["recording"], dict):
            _fail()
        return _call(client, "/api/recordings/import", {"recording": value["recording"]})
    if tool == "recordings.derive":
        value = _args(arguments, {"recordingId"}, {"eventIds", "speed"})
        body: dict[str, Any] = {}
        if "eventIds" in value:
            if (not isinstance(value["eventIds"], list) or len(value["eventIds"]) > 500
                    or any(not isinstance(item, str) for item in value["eventIds"])):
                _fail()
            body["eventIds"] = [_safe_id(item) for item in value["eventIds"]]
        if "speed" in value:
            speed = value["speed"]
            if type(speed) not in (int, float) or not math.isfinite(speed) or not 0.25 <= speed <= 4:
                _fail()
            body["speed"] = speed
        return _call(client, f"/api/recordings/{_path(value['recordingId'])}/derive", body)
    if tool in {"recordings.start", "recordings.stop"}:
        required = {"sessionId", "controllerId", "epoch"}
        optional = {"reset"} if tool == "recordings.start" else set()
        value = _args(arguments, required, optional)
        body = {"controllerId": _safe_id(value["controllerId"]), "epoch": _int(value["epoch"], minimum=1)}
        if tool == "recordings.start" and "reset" in value:
            if type(value["reset"]) is not bool:
                _fail()
            body["reset"] = value["reset"]
        action = "start" if tool == "recordings.start" else "stop"
        return _call(client, f"/api/sessions/{_path(value['sessionId'])}/recordings/{action}", body)

    if tool == "replay.start":
        value = _args(arguments, {"sessionId", "controllerId", "epoch", "recordingId"}, {"variables"})
        body = {"controllerId": _safe_id(value["controllerId"]), "epoch": _int(value["epoch"], minimum=1),
                "recordingId": _safe_id(value["recordingId"])}
        if "variables" in value:
            body["variables"] = _variables(value["variables"])
        return _call(client, f"/api/sessions/{_path(value['sessionId'])}/replay", body)
    if tool == "replay.cancel":
        value = _args(arguments, {"sessionId"}, {"clientId"})
        body = {} if "clientId" not in value else {"clientId": _safe_id(value["clientId"])}
        return _call(client, f"/api/sessions/{_path(value['sessionId'])}/replay/cancel", body)

    if tool == "jobs.list":
        _args(arguments, set())
        return _call(client, "/api/jobs")
    if tool == "jobs.get":
        value = _args(arguments, {"jobId"})
        return _call(client, f"/api/jobs/{_path(value['jobId'])}")
    if tool == "jobs.submit":
        value = _args(arguments, {"request"})
        request = _args(value["request"], {"recordingId", "variables", "requestId"}, {"repeats", "timeoutSeconds"})
        body = {"recordingId": _safe_id(request["recordingId"]), "variables": _variables(request["variables"]),
                "requestId": _safe_id(request["requestId"])}
        if "repeats" in request:
            body["repeats"] = _int(request["repeats"], minimum=1)
            if body["repeats"] > 10:
                _fail()
        if "timeoutSeconds" in request:
            body["timeoutSeconds"] = _int(request["timeoutSeconds"], minimum=1)
            if body["timeoutSeconds"] > 3600:
                _fail()
        return _call(client, "/api/jobs", body)
    if tool == "jobs.cancel":
        value = _args(arguments, {"jobId"})
        return _call(client, f"/api/jobs/{_path(value['jobId'])}/cancel", {})
    _fail("Unknown tool")


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _readline(stream: TextIO) -> str | None:
    raw = stream.readline(MAX_LINE + 1)
    if raw == "" or raw == b"":
        return None
    is_bytes = isinstance(raw, bytes)
    if is_bytes:
        newline = b"\n"
        content = raw.split(newline, 1)[0]
    else:
        newline = "\n"
        content = raw.split(newline, 1)[0]
    if newline not in raw and len(raw) > MAX_LINE:
        while True:
            # ``readline`` is bounded but stops at a newline, so it cannot
            # consume the first bytes of the following request.
            chunk = stream.readline(8192)
            if chunk == "" or chunk == b"":
                break
            if (b"\n" if isinstance(chunk, bytes) else "\n") in chunk:
                break
        return "__OVERSIZE__"
    if content.endswith(b"\r" if is_bytes else "\r"):
        content = content[:-1]
    try:
        text = content.decode("utf-8") if is_bytes else content
        if len(text.encode("utf-8")) > MAX_LINE:
            return "__OVERSIZE__"
        return text
    except (UnicodeDecodeError, UnicodeEncodeError):
        return "__INVALID_ENCODING__"


def _error_payload(exc: BaseException) -> dict[str, str]:
    if isinstance(exc, (URLError, HTTPException, TimeoutError, ConnectionError)):
        return {"code": "transport_unavailable", "message": "Local API connection unavailable"}
    if isinstance(exc, LiveError):
        code = getattr(exc, "code", "operation_failed")
        if not isinstance(code, str) or re.fullmatch(r"[a-z0-9_]{1,64}", code) is None:
            code = "operation_failed"
        messages = {
            "invalid_argument": "Invalid request arguments", "not_found": "Resource not found",
            "unauthorized": "Local API authentication failed", "forbidden": "Operation is not allowed",
            "invalid_server": "Invalid local server", "not_replayable": "Recording cannot be replayed",
        }
        return {"code": code, "message": messages.get(code, "Local operation failed")}
    if isinstance(exc, ContractError):
        return {"code": "invalid_argument", "message": "Invalid request arguments"}
    return {"code": "internal_error", "message": "Local operation failed"}


def _safe_request(value: Any) -> tuple[str | None, str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"id", "tool", "arguments"}:
        _fail()
    request_id = _safe_id(value["id"])
    tool = value["tool"]
    if not isinstance(tool, str) or tool not in _TOOLS:
        _fail("Unknown tool")
    if not isinstance(value["arguments"], dict):
        _fail()
    return request_id, tool, value["arguments"]


def serve(client: Any, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout) -> int:
    """Serve requests until EOF; malformed requests do not stop later lines."""
    while True:
        line = _readline(input_stream)
        if line is None:
            return 0
        request_id: str | None = None
        try:
            if line == "__OVERSIZE__":
                _fail("Request line exceeds 1 MiB")
            if line == "__INVALID_ENCODING__":
                _fail("Request is not valid UTF-8")
            value = json.loads(line, object_pairs_hook=_pairs, parse_constant=_reject_constant)
            request_id, tool, arguments = _safe_request(value)
            result = dispatch(client, tool, arguments)
            response = {"id": request_id, "ok": True, "result": result}
        except (LiveError, ContractError, TypeError, ValueError, KeyError,
                URLError, HTTPException, TimeoutError, ConnectionError) as exc:
            response = {"id": request_id, "ok": False, "error": _error_payload(exc)}
        output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
        output_stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reproof live-tools", description="Bounded local Live JSON-lines tools")
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    args = parser.parse_args(argv)
    return serve(Client(args.server))


if __name__ == "__main__":
    raise SystemExit(main())
