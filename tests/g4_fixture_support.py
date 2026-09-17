"""Owned loopback fixture service with real generation fences and state effects.

The parent launches this in a separate Python process. The only dataset is one
synthetic boolean account state; request payload values are never persisted.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def serve(pipe):
    lock = threading.RLock()
    state = {"generation": 0, "allocationId": None, "dirty": False,
             "effects": [], "calls": {}, "fenceRejections": 0}
    operations = {}
    cleanup_release = threading.Event()
    cleanup_release.set()
    hold_cleanup = False

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, document):
            body = canonical(document)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ConnectionError):
                pass

        def do_GET(self):
            if self.path != "/state":
                return self.reply(404, {})
            with lock:
                return self.reply(200, state)

        def do_POST(self):
            nonlocal hold_cleanup
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 128 * 1024:
                    raise ValueError()
                request = json.loads(self.rfile.read(size))
                if type(request) is not dict:
                    raise ValueError()
            except (ValueError, TypeError, OSError):
                return self.reply(400, {})
            if self.path == "/control":
                with lock:
                    if "holdCleanupResponse" in request:
                        hold_cleanup = request["holdCleanupResponse"] is True
                        if hold_cleanup:
                            cleanup_release.clear()
                        else:
                            cleanup_release.set()
                return self.reply(200, {"ok": True})
            if self.path not in {"/operations", "/status"}:
                return self.reply(404, {})
            try:
                identity = tuple(request[key] for key in (
                    "allocationId", "generation", "operationId", "operation",
                    "recipeId", "payloadDigest", "idempotencyKey"))
                operation_id = request["operationId"]
                generation = request["generation"]
                if type(generation) is not int or generation < 1:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                return self.reply(400, {})

            def response(status, *, completed=None):
                return {"operationId": operation_id, "generation": generation,
                        "status": status, "completedAtMs": completed,
                        "retentionExpiresAtMs": None if completed is None else completed + 60000,
                        "fence": state["generation"] or generation}

            with lock:
                existing = operations.get(operation_id)
                if existing is not None and existing["identity"] != identity:
                    return self.reply(409, {})
                if self.path == "/status":
                    return self.reply(200, dict(existing["response"], fence=state["generation"])
                                      if existing else response("unknown"))
                if existing is not None:
                    return self.reply(200, dict(existing["response"], fence=state["generation"]))
                try:
                    digest = hashlib.sha256(canonical(request["payload"])).hexdigest()
                    key = hashlib.sha256(canonical({"allocationGeneration": generation,
                                                    "operationId": operation_id,
                                                    "payloadDigest": digest})).hexdigest()
                    if digest != request["payloadDigest"] or key != request["idempotencyKey"]:
                        raise ValueError()
                except (KeyError, TypeError, ValueError):
                    return self.reply(409, {})
                state["calls"][operation_id] = state["calls"].get(operation_id, 0) + 1
                if (generation < state["generation"] or
                        (generation == state["generation"] and
                         request["allocationId"] != state["allocationId"])):
                    state["fenceRejections"] += 1
                    return self.reply(200, response("failed", completed=int(time.time() * 1000)))
                if generation > state["generation"]:
                    state["generation"] = generation
                    state["allocationId"] = request["allocationId"]
                operations[operation_id] = {"identity": identity, "response": response("running")}
                delay = request["payload"].get("delayMs", 0)
            if delay:
                time.sleep(min(float(delay) / 1000, 2))
            with lock:
                if (generation != state["generation"] or
                        request["allocationId"] != state["allocationId"]):
                    state["fenceRejections"] += 1
                    result = response("failed", completed=int(time.time() * 1000))
                else:
                    operation = request["operation"]
                    status = "complete"
                    if operation == "prepare":
                        state["dirty"] = True
                    elif operation == "cleanup":
                        state["dirty"] = False
                    elif operation == "check":
                        status = "complete" if state["dirty"] else "failed"
                    else:
                        status = "failed"
                    state["effects"].append({"operationId": operation_id, "operation": operation,
                                             "generation": generation, "dirty": state["dirty"]})
                    result = response(status, completed=int(time.time() * 1000))
                operations[operation_id]["response"] = result
                hold = hold_cleanup and request["operation"] == "cleanup"
            if hold:
                cleanup_release.wait(4)
            self.reply(200, result)

    server = Server(("127.0.0.1", 0), Handler)
    server.timeout = .05
    pipe.send({"port": server.server_port})
    try:
        while not pipe.poll():
            server.handle_request()
    finally:
        cleanup_release.set()
        server.server_close()
        pipe.close()
