"""Durable, owner-fenced replay jobs for the local Live control plane.

The queue deliberately keeps replay variables in memory only.  A persisted job
is enough to explain what happened and to recover safely after a process crash,
but never enough to re-run an uncertain injection automatically.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time
import uuid

from .model import LiveError
from ..core import ContractError
from ..storage import read_json, write_json


_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
_STATES = frozenset({"queued", "starting", "running", "cleaning"}) | _TERMINAL
_REQUEST_KEYS = {"recordingId", "variables", "requestId", "timeoutSeconds", "repeats"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SAFE_CODES = frozenset({
    "invalid_request", "not_found", "owner_forbidden", "request_conflict",
    "request_unverifiable", "recording_changed", "recording_not_replayable",
    "provider_mismatch", "device_busy", "startup_failed", "startup_timeout",
    "replay_failed", "replay_cancelled", "injection_unknown", "timeout",
    "cancelled", "cleanup_failed", "quarantined", "interrupted", "closed",
    "queue_full", "controller_lost", "shutdown_timeout", "authorization_revoked",
})


def _error(code: str, message: str = "Invalid replay job request", status: int = 409):
    raise LiveError(code, message, status)


def _check(condition, code="invalid_request", message="Invalid replay job request", status=409):
    if not condition:
        _error(code, message, status)


def _safe_code(exc, fallback="replay_failed"):
    code = getattr(exc, "code", None)
    return code if code in _SAFE_CODES else fallback


class JobQueue:
    """A small durable replay queue backed by one JSON file per job.

    ``Lab.create_session`` remains the device availability authority.  The
    queue only claims sessions it created and always closes those sessions in
    the worker that owns them.
    """

    def __init__(self, lab, output=None, *, max_running=2, poll_interval=.1, startup_timeout=95,
                 max_pending=100, effect_authorizer=None, resource_binder=None):
        _check(type(max_running) is int and 1 <= max_running <= 64)
        _check(type(poll_interval) in (int, float) and poll_interval > 0)
        _check(type(startup_timeout) in (int, float) and startup_timeout > 0)
        _check(type(max_pending) is int and 1 <= max_pending <= 10000)
        _check(effect_authorizer is None or callable(effect_authorizer))
        _check(resource_binder is None or callable(resource_binder))
        self.lab = lab
        self.output = Path(output) if output is not None else Path(lab.output) / "jobs"
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_running = max_running
        self.poll_interval = float(poll_interval)
        self.startup_timeout = float(startup_timeout)
        self.max_pending = max_pending
        self.effect_authorizer = effect_authorizer
        self.resource_binder = resource_binder
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._jobs = {}
        self._request_index = {}
        self._assigned = {}
        self._retry_after = {}
        self._workers = {}
        self._cancel = {}
        self._dispatcher = None
        self._stop = threading.Event()
        self._closed = False
        self._secret = secrets.token_bytes(32)
        self._next_order = 0
        self._load()

    def _load(self):
        for path in sorted(self.output.glob("*.json")):
            try:
                value = read_json(path)
                job = value.get("job", value)
                if not isinstance(job, dict) or path.stem != job.get("id"):
                    continue
                if not _ID.fullmatch(job.get("id", "")) or not isinstance(job.get("owner"), str):
                    continue
                if job.get("state") not in _STATES or not isinstance(job.get("requestId"), str):
                    continue
                record = self._normalise_loaded(job)
            except (ContractError, LiveError, ValueError, TypeError, KeyError, OSError):
                continue
            self._jobs[record["id"]] = record
            record["_order"] = self._next_order
            self._next_order += 1
            key = (record["owner"], record["requestId"])
            # Fingerprints intentionally cannot survive a restart.  Reusing an
            # id is therefore rejected safely instead of guessing variables.
            self._request_index[key] = {"job": record, "fingerprint": None}
            if record["state"] not in _TERMINAL:
                record["state"] = "interrupted"
                record["endedAt"] = _now_ms()
                record["errorCode"] = "interrupted"
                self._persist(record)

    @staticmethod
    def _normalise_loaded(value):
        fields = {
            "id": value.get("id"), "requestId": value.get("requestId"),
            "recordingId": value.get("recordingId"), "recordingDigest": value.get("recordingDigest"),
            "deviceId": value.get("deviceId"), "state": value.get("state"),
            "createdAt": value.get("createdAt"), "startedAt": value.get("startedAt"),
            "endedAt": value.get("endedAt"), "sessionId": value.get("sessionId"),
            "repeats": value.get("repeats", 1), "completedRuns": value.get("completedRuns", 0),
            "results": value.get("results", []), "errorCode": value.get("errorCode"),
            "owner": value.get("owner"), "timeoutSeconds": value.get("timeoutSeconds", 900),
            "cancelRequested": value.get("cancelRequested", False),
            "projectId": value.get("projectId"), "principalId": value.get("principalId"),
            "credentialId": value.get("credentialId"),
            "authorizationId": value.get("authorizationId"),
        }
        _check(_ID.fullmatch(fields["id"] or "") is not None and
               _ID.fullmatch(fields["requestId"] or "") is not None and
               _ID.fullmatch(fields["recordingId"] or "") is not None and
               isinstance(fields["recordingDigest"], str) and re.fullmatch(r"[0-9a-f]{64}", fields["recordingDigest"]) and
               isinstance(fields["deviceId"], str) and isinstance(fields["owner"], str), "invalid_request")
        _check(type(fields["createdAt"]) is int and
               all(value is None or type(value) is int for value in
                   (fields["startedAt"], fields["endedAt"])) and
               (fields["sessionId"] is None or _ID.fullmatch(fields["sessionId"]) is not None), "invalid_request")
        _check(isinstance(fields["results"], list) and len(fields["results"]) <= 10, "invalid_request")
        _check(type(fields["repeats"]) is int and 1 <= fields["repeats"] <= 10, "invalid_request")
        _check(type(fields["completedRuns"]) is int and 0 <= fields["completedRuns"] <= fields["repeats"], "invalid_request")
        _check(type(fields['cancelRequested']) is bool, 'invalid_request')
        shared = (fields["projectId"], fields["principalId"], fields["credentialId"])
        _check(all(item is None for item in shared)
               or all(isinstance(item, str) and _ID.fullmatch(item) for item in shared),
               "invalid_request")
        _check(fields["authorizationId"] is None
               or (all(item is not None for item in shared)
                   and isinstance(fields["authorizationId"], str)
                   and _ID.fullmatch(fields["authorizationId"])), "invalid_request")
        _check(type(fields["timeoutSeconds"]) is int and 1 <= fields["timeoutSeconds"] <= 3600, "invalid_request")
        _check(fields["errorCode"] is None or fields["errorCode"] in _SAFE_CODES, "invalid_request")
        for result in fields["results"]:
            _check(isinstance(result, dict) and set(result) == {"run", "replayId", "state", "receipts"}, "invalid_request")
            _check(type(result["run"]) is int and 1 <= result["run"] <= fields["repeats"] and
                   _ID.fullmatch(result["replayId"] or "") is not None and result["state"] == "succeeded" and
                   isinstance(result["receipts"], list), "invalid_request")
            for receipt in result["receipts"]:
                _check(isinstance(receipt, dict) and set(receipt) <=
                       {"id", "sequence", "action", "status", "epoch", "frameId", "durationMs", "timing"},
                       "invalid_request")
                _check(isinstance(receipt.get("id", ""), str) and _ID.fullmatch(receipt.get("id", "")) is not None and
                       type(receipt.get("sequence", 0)) is int and type(receipt.get("epoch", 0)) is int and
                       type(receipt.get("frameId", 0)) is int and type(receipt.get("durationMs", 0)) is int and
                       receipt.get("action") in {"tap", "long_press", "swipe", "text", "home", "reset", "pointer"} and
                       receipt.get("status") in {"injected", "unknown"} and
                       receipt.get("timing") in {None, "best-effort"}, "invalid_request")
        return fields

    def _persist(self, job):
        # ``_variables`` and ``_fingerprint`` are never part of this record.
        stored = {key: copy.deepcopy(value) for key, value in job.items() if not key.startswith("_")}
        write_json(self.output / f"{job['id']}.json", {"job": stored})

    @staticmethod
    def _public(job):
        fields = ("id", "requestId", "recordingId", "recordingDigest", "deviceId", "state",
                  "createdAt", "startedAt", "endedAt", "sessionId", "repeats", "completedRuns",
                  "results", "errorCode", "timeoutSeconds", "cancelRequested")
        result = {key: copy.deepcopy(job.get(key)) for key in fields}
        if job.get("projectId") is not None:
            result["projectId"] = job["projectId"]
        return result

    def _fingerprint(self, request):
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return hmac.new(self._secret, encoded, hashlib.sha256).digest()

    def _set(self, job, **changes):
        with self._lock:
            job.update(changes)
            self._persist(job)
            self._wake.notify_all()

    def _terminal(self, job, state, error_code=None):
        with self._lock:
            if job["state"] in _TERMINAL:
                return
            job.pop("_variables", None)
            job.pop("_deadline", None)
            job.pop("_controller", None)
            job["state"] = state
            job["endedAt"] = _now_ms()
            job["errorCode"] = error_code
            self._persist(job)
            self._wake.notify_all()

    def start(self):
        with self._lock:
            _check(not self._closed, "closed", "Job queue is closed")
            if self._dispatcher is None or not self._dispatcher.is_alive():
                self._stop.clear()
                self._dispatcher = threading.Thread(target=self._dispatch, name="reproloop-job-queue", daemon=True)
                self._dispatcher.start()
        return self

    def submit(self, owner, request, *, project_id=None, principal_id=None,
               credential_id=None, authorization_id=None):
        _check(isinstance(owner, str) and 0 < len(owner) <= 256)
        _check(isinstance(request, dict) and set(request) <= _REQUEST_KEYS and
               {"recordingId", "variables", "requestId"} <= set(request), "invalid_request")
        recording_id = request["recordingId"]
        request_id = request["requestId"]
        _check(isinstance(recording_id, str) and _ID.fullmatch(recording_id) is not None)
        _check(isinstance(request_id, str) and _ID.fullmatch(request_id) is not None)
        variables = request["variables"]
        _check(isinstance(variables, dict) and len(variables) <= 500, "invalid_request")
        timeout = request.get("timeoutSeconds", 900)
        repeats = request.get("repeats", 1)
        _check(type(timeout) is int and 1 <= timeout <= 3600, "invalid_request")
        _check(type(repeats) is int and 1 <= repeats <= 10, "invalid_request")
        shared = (project_id, principal_id, credential_id)
        _check(all(item is None for item in shared)
               or (all(isinstance(item, str) and _ID.fullmatch(item) for item in shared)
                   and self.effect_authorizer is not None
                   and self.resource_binder is not None), "invalid_request")
        _check(authorization_id is None
               or (all(item is not None for item in shared)
                   and isinstance(authorization_id, str)
                   and _ID.fullmatch(authorization_id)), "invalid_request")

        request_copy = {"recordingId": recording_id, "variables": copy.deepcopy(variables),
                        "requestId": request_id, "timeoutSeconds": timeout, "repeats": repeats}
        fingerprint = self._fingerprint(request_copy)
        # Resolve idempotency before touching the recording again.  A completed
        # job remains retryable even if its source is later unavailable; after a
        # restart the absent in-memory HMAC still makes reuse fail closed.
        with self._lock:
            _check(not self._closed, "closed", "Job queue is closed")
            existing = self._request_index.get((owner, request_id))
            if existing is not None:
                if existing["fingerprint"] is None:
                    _error("request_unverifiable", "Request id cannot be safely reused after restart")
                _check(hmac.compare_digest(existing["fingerprint"], fingerprint), "request_conflict")
                return self._public(existing["job"])

        # Fetching at submission pins both ownership and the immutable digest.
        try:
            recording = self.lab.recording(recording_id, owner)
        except LiveError as exc:
            if getattr(exc, "code", None) == "forbidden":
                _error("owner_forbidden", status=403)
            source_code = getattr(exc, "code", None)
            _error(source_code if source_code in {"not_found", "recording_changed"} else "recording_not_replayable")
        _check(recording.get("status") == "complete" and recording.get("replayable") is True,
               "recording_not_replayable")
        names = recording.get("variables", [])
        _check(isinstance(names, list) and set(variables) == set(names), "invalid_request")
        for value in variables.values():
            _check(isinstance(value, str) and len(value) <= 256, "invalid_request")
            try:
                value.encode("utf-8")
            except UnicodeError:
                _error("invalid_request")
        with self._lock:
            _check(not self._closed, "closed", "Job queue is closed")
            existing = self._request_index.get((owner, request_id))
            if existing is not None:
                if existing["fingerprint"] is None:
                    _error("request_unverifiable", "Request id cannot be safely reused after restart")
                _check(hmac.compare_digest(existing["fingerprint"], fingerprint), "request_conflict")
                return self._public(existing["job"])
            pending = sum(job["state"] not in _TERMINAL for job in self._jobs.values())
            _check(pending < self.max_pending, "queue_full", "Replay queue is full", 429)
            now = _now_ms()
            order = self._next_order
            self._next_order += 1
            job = {
                "id": uuid.uuid4().hex, "requestId": request_id, "recordingId": recording_id,
                "recordingDigest": recording["digest"], "deviceId": recording["deviceId"],
                "state": "queued", "createdAt": now, "startedAt": None, "endedAt": None,
                "sessionId": None, "repeats": repeats, "completedRuns": 0, "results": [],
                "errorCode": None, "owner": owner, "timeoutSeconds": timeout, "cancelRequested": False,
                "projectId": project_id, "principalId": principal_id,
                "credentialId": credential_id, "authorizationId": authorization_id,
                "_variables": copy.deepcopy(variables), "_fingerprint": fingerprint,
                "_deadline": time.monotonic() + timeout, "_order": order,
            }
            if project_id is not None:
                self.resource_binder(job["id"], project_id, principal_id)
            self._jobs[job["id"]] = job
            self._request_index[(owner, request_id)] = {"job": job, "fingerprint": fingerprint}
            self._persist(job)
            self._wake.notify_all()
            return self._public(job)

    def list(self, owner):
        _check(isinstance(owner, str) and 0 < len(owner) <= 256)
        with self._lock:
            return [self._public(job) for job in sorted(self._jobs.values(), key=lambda item: (item["createdAt"], item.get("_order", 0)), reverse=True)
                    if job["owner"] == owner]

    def get(self, job_id, owner):
        job = self._owned(job_id, owner)
        with self._lock:
            return self._public(job)

    def _owned(self, job_id, owner):
        _check(isinstance(job_id, str) and _ID.fullmatch(job_id) is not None, "not_found", status=404)
        with self._lock:
            job = self._jobs.get(job_id)
            _check(job is not None, "not_found", status=404)
            _check(job["owner"] == owner, "owner_forbidden", status=403)
            return job

    def cancel(self, job_id, owner):
        job = self._owned(job_id, owner)
        with self._lock:
            if job["state"] in _TERMINAL:
                return self._public(job)
            if job["state"] == "queued":
                self._terminal(job, "cancelled", "cancelled")
                return self._public(job)
            event = self._cancel.setdefault(job_id, threading.Event())
            event.set()
            job['cancelRequested'] = True
            self._persist(job)
        with self._lock:
            return self._public(job)

    def _dispatch(self):
        while not self._stop.is_set():
            candidate = None
            try:
                available = {device["id"] for device in self.lab.list_devices()
                             if device.get("state") == "available"}
            except Exception:
                available = set()
            with self._lock:
                now = time.monotonic()
                for job in self._jobs.values():
                    if job["state"] == "queued" and now >= job["_deadline"]:
                        self._terminal(job, "failed", "timeout")
                active = len(self._assigned)
                if active < self.max_running:
                    queued = sorted((job for job in self._jobs.values() if job["state"] == "queued"),
                                    key=lambda item: (item.get("_order", 0), item["id"]))
                    assigned_devices = set(self._assigned)
                    for job in queued:
                        if job["deviceId"] in assigned_devices or job["deviceId"] not in available:
                            continue
                        # FIFO is enforced by only considering the oldest queued
                        # job for each original device.
                        if any(other["deviceId"] == job["deviceId"] and
                               other["state"] == "queued" and
                               (other.get("_order", 0), other["id"]) < (job.get("_order", 0), job["id"])
                               for other in queued):
                            continue
                        if now < self._retry_after.get(job["deviceId"], 0):
                            continue
                        if job.get("projectId") is not None:
                            try:
                                self._authorize_effect(job, "dispatch")
                            except Exception:
                                self._terminal(job, "failed", "authorization_revoked")
                                continue
                        self._assigned[job["deviceId"]] = job["id"]
                        job["state"] = "starting"
                        job["startedAt"] = _now_ms()
                        self._cancel[job["id"]] = threading.Event()
                        self._persist(job)
                        candidate = job
                        worker = threading.Thread(target=self._run, args=(job,), name="reproloop-replay-job", daemon=True)
                        self._workers[job["id"]] = worker
                        worker.start()
                        break
            if candidate is not None:
                continue
            with self._wake:
                self._wake.wait(timeout=self.poll_interval)

    def _requeue_busy(self, job):
        # The owning worker releases its reservation and publishes queued state
        # together in finally; publishing early lets a successor race its cleanup.
        with self._lock:
            job['_retry_busy'] = True

    def _run(self, job):
        cancel = self._cancel[job["id"]]
        session_id = None
        outcome = ("succeeded", None)
        try:
            if cancel.is_set():
                outcome = ("cancelled", "cancelled")
            else:
                try:
                    self._authorize_effect(job, "startup")
                    source = self.lab.recording(job['recordingId'], job['owner'])
                    _check(source.get('digest') == job['recordingDigest'], 'recording_changed')
                    authorizer = ((lambda kind: self._authorize_effect(job, kind))
                                  if job.get("projectId") is not None else None)
                    session = self.lab.create_session(
                        job["deviceId"], job["owner"], "job-" + job["id"],
                        _effect_authorizer=authorizer)
                except LiveError as exc:
                    if exc.code == "device_busy":
                        self._requeue_busy(job)
                        return
                    outcome = ("failed", _safe_code(exc, "startup_failed"))
                except Exception:
                    outcome = ("failed", "startup_failed")
                else:
                    session_id = session["id"]
                    with self._lock:
                        job["sessionId"] = session_id
                        self._persist(job)
            deadline = min(job["_deadline"], time.monotonic() + self.startup_timeout)
            while session_id is not None and outcome[0] == "succeeded":
                if cancel.is_set():
                    outcome = ("cancelled", "cancelled"); break
                if time.monotonic() >= deadline:
                    outcome = ("failed", "timeout" if time.monotonic() >= job["_deadline"] else "startup_timeout"); break
                try:
                    state = self.lab.get_session(session_id, job["owner"])
                except Exception:
                    outcome = ("failed", "startup_failed"); break
                if state["state"] == "active":
                    try:
                        claimed = self.lab.claim(session_id, job["owner"], "job-" + job["id"],
                                                 state["epoch"], mode="automation")
                    except Exception as exc:
                        outcome = ("failed", "controller_lost" if cancel.is_set() is False else "cancelled"); break
                    with self._lock:
                        job["_controller"] = {"id": claimed["controllerId"], "epoch": claimed["epoch"]}
                    self._set(job, state="running")
                    break
                if state["state"] in {"failed", "closed", "draining"}:
                    outcome = ("failed", "quarantined" if state["state"] == "failed" else "startup_failed"); break
                cancel.wait(self.poll_interval)

            if outcome[0] == "succeeded":
                for run in range(1, job["repeats"] + 1):
                    if cancel.is_set():
                        outcome = ("cancelled", "cancelled"); break
                    if time.monotonic() >= job["_deadline"]:
                        outcome = ("failed", "timeout"); break
                    try:
                        recording = self.lab.recording(job["recordingId"], job["owner"])
                        if recording.get("digest") != job["recordingDigest"]:
                            outcome = ("failed", "recording_changed"); break
                        current = self.lab.get_session(session_id, job["owner"])
                        controller = job.get("_controller")
                        if not controller or current["controllerId"] != controller["id"]:
                            outcome = ("failed", "controller_lost"); break
                        replay = self.lab.start_replay(session_id, job["owner"], controller["id"],
                                                       current["epoch"], job["recordingId"],
                                                       copy.deepcopy(job["_variables"]))
                        replay_id = replay["id"]
                    except Exception as exc:
                        outcome = ("cancelled", "cancelled") if cancel.is_set() else ("failed", _safe_code(exc))
                        break
                    replay_result = self._wait_replay(job, session_id, replay_id, cancel, len(recording.get("events", [])))
                    if replay_result.get("timedOut"):
                        outcome = ("failed", "timeout"); break
                    if replay_result["state"] == "actions_replayed":
                        result = {"run": run, "replayId": replay_id, "state": "succeeded",
                                  "receipts": replay_result.get("receipts", [])}
                        with self._lock:
                            job["results"].append(result)
                            job["completedRuns"] = run
                            self._persist(job)
                    elif replay_result["state"] == "cancelled":
                        if replay_result.get("timedOut"):
                            outcome = ("failed", "timeout")
                        else:
                            outcome = ("cancelled", "cancelled" if cancel.is_set() else "replay_cancelled")
                        break
                    else:
                        outcome = ("failed", "timeout" if replay_result.get("timedOut") else
                                   ("injection_unknown" if replay_result.get("unknown") else "replay_failed")); break
        finally:
            retry_busy = job.pop('_retry_busy', False)
            if session_id is not None:
                self._cleanup(job, session_id, outcome)
            elif not retry_busy and job['state'] not in _TERMINAL:
                self._terminal(job, outcome[0], outcome[1])
            with self._lock:
                self._assigned.pop(job['deviceId'], None)
                self._workers.pop(job['id'], None)
                self._cancel.pop(job['id'], None)
                if retry_busy:
                    self._retry_after[job['deviceId']] = time.monotonic() + self.poll_interval
                    if cancel.is_set() or self._closed:
                        self._terminal(job, 'cancelled', 'cancelled')
                    else:
                        job.update(state='queued', startedAt=None)
                        self._persist(job)
                if job['state'] != 'queued':
                    job.pop('_variables', None)
                self._wake.notify_all()

    def _wait_replay(self, job, session_id, replay_id, cancel, expected_receipts):
        cancel_sent = False
        cancel_deadline = None
        timed_out = False
        while True:
            if cancel.is_set() and not cancel_sent:
                cancel_sent = True
                cancel_deadline = time.monotonic() + 5
                try:
                    self.lab.cancel_replay(session_id, job["owner"])
                except Exception:
                    pass
            if time.monotonic() >= job["_deadline"] and not cancel_sent:
                cancel_sent = True
                timed_out = True
                cancel_deadline = time.monotonic() + 5
                try:
                    self.lab.cancel_replay(session_id, job["owner"])
                except Exception:
                    pass
            try:
                state = self.lab.get_session(session_id, job["owner"])
                replay = state.get("replay") or {}
                if replay.get("id") == replay_id and replay.get("state") in {"actions_replayed", "cancelled", "failed"}:
                    path = Path(self.lab.output) / "replays" / f"{replay_id}.json"
                    if path.exists():
                        result = read_json(path)
                        if timed_out:
                            result["state"] = "cancelled"
                            result["timedOut"] = True
                            return result
                        valid_success = (
                            result.get("id") == replay_id and
                            result.get("recordingId") == job["recordingId"] and
                            result.get("recordingDigest") == job["recordingDigest"] and
                            result.get("state") == "actions_replayed" and
                            isinstance(result.get("receipts"), list) and
                            len(result["receipts"]) == expected_receipts
                        )
                        if valid_success:
                            return result
                        if result.get("state") == "actions_replayed":
                            return {"state": "failed", "unknown": True}
                        result["timedOut"] = timed_out
                        result["unknown"] = result.get("state") == "failed" and cancel_sent is False
                        return result
                    # Lab writes the receipt artifact before publishing the
                    # terminal replay state.  If a provider violates that
                    # ordering, wait briefly and fail closed rather than
                    # declaring a partial replay successful.
                    if replay.get("state") != "actions_replayed":
                        return {"state": replay.get("state"), "timedOut": timed_out,
                                "unknown": replay.get("state") == "failed"}
            except Exception:
                return {"state": "failed", "unknown": True}
            if cancel_sent and time.monotonic() >= cancel_deadline:
                return {"state": "failed", "timedOut": timed_out, "unknown": True}
            cancel.wait(self.poll_interval)

    def _authorize_effect(self, job, kind):
        if job.get("projectId") is None:
            return True
        try:
            return self.effect_authorizer(
                job["principalId"], job["credentialId"], job["projectId"],
                job["deviceId"], job["id"], kind, job.get("authorizationId"))
        except LiveError:
            raise
        except Exception:
            raise LiveError("authorization_revoked",
                            "Replay job authorization was revoked", 403) from None

    def _cleanup(self, job, session_id, outcome):
        state, error_code = outcome
        try:
            self._set(job, state="cleaning")
            try:
                self.lab.cancel_replay(session_id, job["owner"])
            except Exception:
                pass
            closed = self.lab.close_session(session_id, job["owner"])
            cleanup_ok = closed.get("state") == "closed"
        except Exception:
            cleanup_ok = False
        if not cleanup_ok:
            self._terminal(job, "failed", "cleanup_failed")
        else:
            self._terminal(job, state, error_code)

    def close(self):
        shutdown_deadline = time.monotonic() + 60
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            for job in self._jobs.values():
                if job["state"] == "queued":
                    self._terminal(job, "cancelled", "cancelled")
                elif job["state"] in {"starting", "running", "cleaning"}:
                    self._cancel.setdefault(job["id"], threading.Event()).set()
            dispatcher = self._dispatcher
            self._wake.notify_all()
        if dispatcher is not None:
            dispatcher.join(timeout=max(0, shutdown_deadline - time.monotonic()))
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            remaining = max(0, shutdown_deadline - time.monotonic())
            worker.join(timeout=remaining)
        with self._lock:
            for job in self._jobs.values():
                if job["state"] not in _TERMINAL:
                    alive = any(worker.is_alive() for worker in workers if self._workers.get(job["id"]) is worker)
                    if alive:
                        job["_shutdown_uncertain"] = True
                        job["state"] = "cleaning"
                        job["errorCode"] = "shutdown_timeout"
                        self._persist(job)


def _now_ms():
    return int(time.time() * 1000)


class IssueSessionJobs:
    """Trusted G4 job facade kept separate from the legacy replay queue.

    Capability objects are deliberately required on every call and are never
    written to the legacy job journal.  G5 may add authenticated submission;
    G4 remains a single-owner local composition API.
    """

    def __init__(self, service):
        from .issue_sessions import IssueSessionService
        if type(service) is not IssueSessionService:
            _error("invalid_request", "Trusted issue session service is required", 400)
        self.service = service

    def start_prepared_recording(self, **trusted_arguments):
        return self.service.start_prepared_recording(**trusted_arguments)

    def run_approved_replay(self, execution, **trusted_arguments):
        return self.service.replay(execution, **trusted_arguments)

    def run_original_qualification(self, engine, approved, execute_attempt, *,
                                   campaign_id=None):
        from ..qualification import QualificationEngine
        if type(engine) is not QualificationEngine or not callable(execute_attempt):
            _error("invalid_request", "Trusted qualification job is required", 400)
        return engine.run_original(
            approved, execute_attempt, campaign_id=campaign_id)


__all__ = ["IssueSessionJobs", "JobQueue"]
