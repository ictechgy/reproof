"""Project-authorized browser issue workflow over the G2–G6 services."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .. import contracts
from ..contracts.scenario import predicate_observations
from ..contracts.versions import exact
from ..issue_package import IssuePackageStore, PackageError, PackageReader, canonical, _json
from ..qualification import QualificationEngine
from .issue_sessions import FixturePreparation, IssueSessionService
from .model import Lab, LiveError, check, public_id
from .recording_session import TrustedProjectRegistration


@dataclass(frozen=True, slots=True)
class ProjectIssueRuntime:
    registration: TrustedProjectRegistration
    service: IssueSessionService
    preparations: tuple[FixturePreparation, ...]
    runtime_policy: dict
    validation_recipe_ids: tuple[str, ...]
    frame_sink_factory: object = None

    def __post_init__(self):
        check(type(self.registration) is TrustedProjectRegistration
              and type(self.service) is IssueSessionService
              and self.service.registry is not None and self.service.runner is not None,
              "issue_configuration", "Issue runtime is incomplete", 400)
        check(self.service.runner.registry is self.service.registry
              and self.service.runner.variables.project_digest==self.registration.project_digest
              and self.service.runner.observations.project_digest==self.registration.project_digest,
              'issue_configuration','Issue adapters belong to another project revision',400)
        contracts.validate_execution_policy(self.runtime_policy)
        check(type(self.preparations) is tuple and len(self.preparations) <= 8,
              "issue_configuration", "Too many registered preparation plans", 400)
        seen = set()
        for preparation in self.preparations:
            check(type(preparation) is FixturePreparation
                  and preparation.plan.project_digest == self.registration.project_digest
                  and preparation.plan.fixture_id not in seen,
                  "issue_configuration", "Preparation registration changed", 400)
            self.service.fixtures.require_plan(preparation.plan)
            seen.add(preparation.plan.fixture_id)
        recipes = {item["id"] for item in self.registration.project["recipes"]
                   if item["kind"] == "regression"}
        check(type(self.validation_recipe_ids) is tuple and self.validation_recipe_ids
              and set(self.validation_recipe_ids) <= recipes
              and len(set(self.validation_recipe_ids)) == len(self.validation_recipe_ids),
              "issue_configuration", "Trusted regression recipes are required", 400)
        check(self.frame_sink_factory is None or callable(self.frame_sink_factory),
              "issue_configuration", "Invalid recording encoder", 400)


class RecordingDurationReached(RuntimeError):
    """The bound G2 recording reached its natural duration boundary."""
    code = "recording_duration_reached"


class IssueWorkflow:
    """Durable selections/revisions and bounded asynchronous preparation/replay.

    The SQLite index has a fixed 8 MiB page ceiling and an upfront G2 charge
    covering that ceiling plus journal space. Original bytes stay in the G2
    recording store; package bytes have their own G2 namespace. Each saved
    specification is immutable. A local approval binds its exact digest.
    """
    def __init__(self, root, lab, access, runtimes, *, media_helper=None, max_jobs=2):
        check(type(lab) is Lab and lab._evidence_store is not None,
              "issue_configuration", "Registered recording storage is required", 400)
        check(type(max_jobs) is int and 1 <= max_jobs <= 4,
              "issue_configuration", "Invalid issue concurrency", 400)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        check(self.root.is_dir() and not self.root.is_symlink()
              and self.root.stat().st_uid == os.getuid(), "issue_configuration", "Invalid issue storage", 400)
        self.lab = lab
        self.access = access
        self.runtimes = {}
        for runtime in runtimes:
            check(type(runtime) is ProjectIssueRuntime and runtime.service.lab is lab,
                  "issue_configuration", "Invalid issue runtime", 400)
            key = runtime.registration.project["id"]
            check(key not in self.runtimes, "issue_configuration", "Duplicate issue project", 400)
            check(access.registration(key).project_digest == runtime.registration.project_digest,
                  "issue_configuration", "Issue project revision changed", 400)
            self.runtimes[key] = runtime
        check(1 <= len(self.runtimes) <= 128, "issue_configuration", "Issue projects are required", 400)
        self._lock = threading.RLock()
        self._media_lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(max_jobs)
        self._threads = set()
        self._handles = {}
        self._cancellations = {}
        self._recording_guards = {}
        self._forced_finalizations = set()
        self._natural_finalizations = set()
        self._finalizing = set()
        self._stop_snapshots = set()
        self._monitor_stop = threading.Event()
        self._closed = False
        self._db = None
        self.packages = None
        self.engines = {}
        self.repairs = None
        self._owner = "issues_" + hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()
        self._media_pin_id='issue_media_'+hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:48]
        self._fd = os.open(self.root / ".owner.lock",
                           os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LiveError("issue_store_busy", "Issue workflow is already open", 409) from None
            self._metadata_budget = lab._recording_budget.reserve(self._owner, "journal", 12 * 1024 * 1024,
                                                                 idempotency_key=self._owner + "_index")
            self._metadata_budget.commit()
            database = self.root / "issues.sqlite3"
            for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
                check(not path.is_symlink() and (not path.exists() or path.is_file()),
                      "issue_configuration", "Invalid issue index", 400)
            self._db = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA journal_size_limit=2097152")
            self._db.execute("PRAGMA wal_autocheckpoint=32")
            check(self._db.execute("PRAGMA max_page_count=2048").fetchone()[0] <= 2048,
                  "issue_store_limit", "Issue index is too large", 409)
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS documents(
                    kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(kind,id)
                );
            """)
            row = self._db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()
            if row is None:
                self._db.execute("INSERT INTO metadata VALUES('version',1)")
            else:
                check(row[0] == 1, "issue_store_version", "Unsupported issue index", 409)
            self.packages = IssuePackageStore(self.root / "packages", lab._evidence_store,
                                              media_helper=media_helper)
            self.lab._evidence_store.unpin_id(self._media_pin_id)
            for key, runtime in self.runtimes.items():
                self.engines[key] = QualificationEngine(self.root / "campaigns" / key, runtime.service.registry)
            for document in self._all("issue"):
                if document["state"] in {"preparing", "recording", "finalizing", "replaying", "cancelling"}:
                    document.update(state="quarantined", reason="process_restarted")
                    self._put("issue", document["id"], document)
            self._monitor_thread=threading.Thread(target=self._monitor,name='reproof-issue-monitor',daemon=False)
            self._monitor_thread.start()
            self._maintenance_thread = threading.Thread(target=self._maintain,
                name='reproof-issue-retention', daemon=False)
            self._maintenance_thread.start()
        except Exception:
            for engine in self.engines.values():
                engine.close()
            if self.packages is not None:
                self.packages.close()
            if self._db is not None:
                self._db.close()
            os.close(self._fd)
            self._fd = None
            raise

    def _put(self, kind, identifier, document, *, immutable=False):
        body = canonical(document).decode()
        check(len(body.encode()) <= (1024 * 1024 if kind == "specification" else 128 * 1024),
              "issue_document_limit", "Issue document is too large", 413)
        with self._lock:
            row = self._db.execute("SELECT body FROM documents WHERE kind=? AND id=?", (kind, identifier)).fetchone()
            if immutable and row is not None:
                check(row[0] == body, "specification_changed", "Saved revision changed", 409)
                return
            if row is None:
                check(self._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] < 8192,
                      "issue_document_limit", "Issue index is full", 409)
            self._db.execute("INSERT INTO documents VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
                             (kind, identifier, body))

    def _get(self, kind, identifier):
        with self._lock:
            row = self._db.execute("SELECT body FROM documents WHERE kind=? AND id=?", (kind, identifier)).fetchone()
            check(row is not None, "issue_not_found", "Issue document was not found", 404)
            return _json(row[0])

    def _all(self, kind):
        with self._lock:
            return [_json(row[0]) for row in self._db.execute("SELECT body FROM documents WHERE kind=?", (kind,))]

    def _runtime(self, project_id):
        runtime = self.runtimes.get(project_id)
        check(runtime is not None, "issue_not_configured", "This project has no issue runtime", 409)
        registration = self.access.registration(project_id)
        check(registration.project_digest == runtime.registration.project_digest,
              "stale_project", "Issue project revision changed", 409)
        return runtime

    def _authorize(self, principal, project_id, capability):
        self.access.store.authorize(principal, project_id, capability)
        return self._runtime(project_id)

    def _issue(self, principal, issue_id, capability="issue.read"):
        self.access.authorize_resource(principal, "issue", issue_id, capability)
        document = self._get("issue", issue_id)
        self._authorize(principal, document["projectId"], capability)
        return document

    def _guard(self, principal, project_id, capability):
        credential = principal.credential_id
        browser = principal.authorization_id
        identity = principal.principal_id
        project_digest = self._runtime(project_id).registration.project_digest
        def guard():
            check(not self._closed,'issue_closed','Issue workflow is closing',409)
            current = self.access.operation_principal(credential, browser)
            check(current.principal_id == identity, "authorization_revoked", "Authorization changed", 403)
            runtime = self._authorize(current, project_id, capability)
            check(runtime.registration.project_digest == project_digest
                  == self.access.store.project(project_id)["projectDigest"],
                  "stale_project", "Project revision changed", 409)
            return True
        return guard

    def _effect_guard(self, principal, document, cancel, *, seconds, recording=False):
        runtime = self._runtime(document["projectId"])
        device_guard = self.access.effect_authorizer(principal, project_id=document["projectId"],
            device_id=document["deviceId"], project_digest=runtime.registration.project_digest)
        fixtures = self._guard(principal, document["projectId"], "fixture.execute")
        active_recording = [None]
        deadline = None if recording else time.monotonic() + seconds
        def guard(kind):
            check(not cancel.is_set(), "cancelled", "Issue operation was cancelled", 409)
            if deadline is not None:
                check(time.monotonic() < deadline, "issue_timeout", "Issue operation timed out", 408)
            if kind.startswith("fixture_"):
                fixtures()
            result = device_guard(kind)
            if recording and kind == "session_check":
                bound_recording = active_recording[0]
                check(bound_recording is not None, "issue_configuration",
                      "Recording duration boundary is unavailable", 409)
                check(callable(getattr(bound_recording, "duration_reached", None)),
                      "issue_configuration",
                      "Recording duration boundary is unavailable", 409)
                if bound_recording.duration_reached():
                    raise RecordingDurationReached()
            return result
        def bind_recording(bound):
            check(recording and bound is not None and callable(
                getattr(bound, "duration_reached", None)),
                "issue_configuration", "Recording duration boundary is unavailable", 409)
            active_recording[0] = bound
        guard.bind_recording = bind_recording
        return guard

    def _submit(self, issue_id, task, *, before=None):
        with self._lock:
            check(not self._closed and self._slots.acquire(blocking=False),
                  "issue_busy", "All issue workers are busy", 409)
            try:
                if before is not None:
                    before()
                def run():
                    try:
                        task()
                    except Exception as error:
                        document = self._get("issue", issue_id)
                        document.update(state="quarantined" if issue_id in self._handles else "failed",
                            reason=getattr(error, "code", "issue_failed") if getattr(error, "code", "") in {
                                "cancelled", "issue_timeout", "authorization_revoked", "stale_project",
                                "preparation_failed", "package_expired", "recording_incomplete"
                            } else "issue_failed")
                        self._put("issue", issue_id, document)
                    finally:
                        with self._lock:
                            self._threads.discard(threading.current_thread())
                        self._slots.release()
                thread = threading.Thread(target=run, name="reproof-issue-work", daemon=False)
                self._threads.add(thread)
                thread.start()
            except Exception:
                self._slots.release()
                raise

    def _monitor(self):
        while not self._monitor_stop.wait(.25):
            with self._lock:active=list(self._recording_guards.items())
            for issue_id,guard in active:
                try:
                    guard('session_check')
                    with self._lock:
                        document=self._get('issue',issue_id)
                        if (document['state']=='recording' and issue_id in self._handles
                                and issue_id not in self._stop_snapshots):
                            session=self.lab.peek_session(document['sessionId'],document['ownerId'])
                            if session['state']=='closed':
                                runtime=self.runtimes[document['projectId']]
                                runtime.service.begin_stop(self._handles[issue_id][0])
                                document.update(state='finalizing',reason=None)
                                self._put('issue',issue_id,document)
                                self._natural_finalizations.add(issue_id)
                    continue
                except RecordingDurationReached:
                    collecting = False
                    try:
                        with self._lock:
                            document=self._get('issue',issue_id)
                            if (document['state']=='recording' and issue_id in self._handles
                                    and issue_id not in self._stop_snapshots):
                                runtime=self.runtimes[document['projectId']]
                                self._stop_snapshots.add(issue_id)
                                collecting = True
                        if not collecting:
                            continue
                        self._final_log_snapshot(runtime, document)
                        with self._lock:
                            document=self._get('issue',issue_id)
                            if document['state']=='recording' and issue_id in self._handles:
                                runtime.service.begin_stop(self._handles[issue_id][0])
                                document.update(state='finalizing',reason=None)
                                self._put('issue',issue_id,document)
                                self._natural_finalizations.add(issue_id)
                    except Exception:
                        # Keep the active handle and fixture until a later
                        # bounded pass confirms the durable stop.
                        continue
                    finally:
                        if collecting:
                            with self._lock:self._stop_snapshots.discard(issue_id)
                except Exception:
                    try:
                        with self._lock:
                            document=self._get('issue',issue_id)
                            if document['state']=='recording' and issue_id in self._handles:
                                runtime=self.runtimes[document['projectId']]
                                runtime.service.begin_stop(self._handles[issue_id][0])
                                document.update(state='finalizing',reason='authorization_revoked')
                                self._put('issue',issue_id,document)
                                self._forced_finalizations.add(issue_id)
                    except Exception:
                        # Preserve the active handle and held fixture when a
                        # durable stop cannot yet be confirmed; retry cleanup.
                        continue
            with self._lock:
                pending=list((self._forced_finalizations | self._natural_finalizations)-self._finalizing)
            for issue_id in pending:
                try:
                    document=self._get('issue',issue_id)
                    runtime=self.runtimes[document['projectId']]
                    cancelled=issue_id in self._forced_finalizations
                    self._submit(issue_id,lambda runtime=runtime,document=document,cancelled=cancelled:
                        self._finish(runtime,document,cancelled=cancelled),
                        before=lambda issue_id=issue_id:self._finalizing.add(issue_id))
                except LiveError:
                    continue

    def _maintain(self):
        while not self._monitor_stop.wait(5):
            try:
                self.packages.apply_retention()
                self.lab._evidence_store.apply_retention(now_ms=int(time.time() * 1000), limit=64)
            except (contracts.ContractError, sqlite3.Error, OSError):
                # Failed deletion keeps its G2 charge. Expiry guards continue
                # denying reads, and the next bounded pass retries cleanup.
                continue

    def projects(self, principal):
        result = []
        for project_id in self.access.store.authorized_project_ids(principal, "project.read"):
            if project_id not in self.runtimes:
                continue
            runtime = self._runtime(project_id)
            project = runtime.registration.project
            capabilities = []
            for capability in ("session.create", "fixture.execute", "specification.maintain",
                               "replay.execute", "recording.import", "export.read", "resource.bind", "project.maintain"):
                try:
                    self.access.store.authorize(principal, project_id, capability)
                    capabilities.append(capability)
                except contracts.ContractError:
                    pass
            devices = []
            for device in self.lab.list_devices():
                try:
                    self.access.authorize_device(principal, device["id"], project_id, "device.read")
                    devices.append(device)
                except contracts.ContractError:
                    pass
            result.append({"id": project_id, "revision": project["revision"],
                "projectDigest": runtime.registration.project_digest,
                "applications": project["applications"], "builds": project["builds"],
                "preparations": [{"id": item.plan.fixture_id, "applicationId": item.plan.application_id}
                                 for item in runtime.preparations],
                "observations": [{"id": key, "coverage": sorted(adapter.coverage_classes)}
                    for key, adapter in runtime.service.runner.observations._adapters.items()],
                "variables": [{"id": item["id"], "type": item["type"], "secret": item["secret"]}
                              for item in project["variables"]],
                "evidencePolicy": project["evidencePolicy"], "capabilities": capabilities,
                "videoAvailable": runtime.frame_sink_factory is not None, "devices": devices,
                "repair": self.repairs.availability(project_id, principal) if self.repairs else {
                    "proposalAvailable": False, "verificationAvailable": False, "reason": "repair_not_configured"}})
        return {"projects": result}

    def _preparations(self, runtime, application_id, identifiers):
        check(type(identifiers) is list and len(identifiers) <= 8
              and all(type(item) is str for item in identifiers)
              and len(set(identifiers)) == len(identifiers),
              "invalid_argument", "Invalid preparation selection", 400)
        plans = {item.plan.fixture_id: item for item in runtime.preparations
                 if item.plan.application_id == application_id}
        check(set(identifiers) <= set(plans), "fixture_binding", "Preparation is unavailable", 409)
        return [plans[key] for key in identifiers]

    def start(self, principal, body):
        exact(body, ("projectId", "applicationId", "buildId", "deviceId", "clientId", "preparationIds", "unprepared"))
        for key in ("projectId", "applicationId", "buildId", "deviceId", "clientId"):
            public_id(body[key])
        runtime = self._authorize(principal, body["projectId"], "session.create")
        self.access.authorize_device(principal, body["deviceId"], body["projectId"], "device.operate")
        self.lab._validate_release_selection(body["deviceId"], runtime.registration, body["applicationId"], body["buildId"])
        preparations = self._preparations(runtime, body["applicationId"], body["preparationIds"])
        check(type(body["unprepared"]) is bool and (bool(preparations) != body["unprepared"]),
              "preparation_required", "Select preparations or explicitly record with unknown initial state", 400)
        if preparations:
            self._authorize(principal, body["projectId"], "fixture.execute")
        document = {"id": "issue_" + uuid.uuid4().hex, "projectId": body["projectId"],
            "ownerId": principal.principal_id, "deviceId": body["deviceId"],
            "applicationId": body["applicationId"], "buildId": body["buildId"],
            "controllerId": body["clientId"], "sessionId": uuid.uuid4().hex,
            "recordingId": "recording_" + uuid.uuid4().hex, "packageId": None,
            "preparationIds": body["preparationIds"], "unprepared": body["unprepared"],
            "state": "preparing", "reason": None, "specificationKey": None,
            "approvalKey": None, "campaignId": None, "imported": False,
            "createdAtMs": int(time.time() * 1000)}
        cancel = threading.Event()
        effect_guard=self._effect_guard(principal,document,cancel,seconds=600,recording=True)
        def work():
            sink = runtime.frame_sink_factory() if runtime.frame_sink_factory is not None else None
            try:
                handle = runtime.service.start_prepared_recording(
                    device_id=document["deviceId"], owner=principal.principal_id, controller_id=body["clientId"],
                    registration=runtime.registration, application_id=body["applicationId"], build_id=body["buildId"],
                    preparations=preparations, frame_sink=sink, cancellation=cancel, timeout_seconds=10,
                    effect_authorizer=effect_guard,
                    issue_id=document["id"], session_id=document["sessionId"], recording_id=document["recordingId"])
                active_session = self.lab._session(handle.session_id, principal.principal_id)
                effect_guard.bind_recording(active_session["releaseRecorder"])
                with self._lock:
                    self._handles[document["id"]] = (handle, sink)
                    self._recording_guards[document['id']]=effect_guard
                document["state"] = "recording"
                self._put("issue", document["id"], document)
                if cancel.is_set():
                    self._finish(runtime, document, cancelled=True)
            except Exception:
                if sink is not None and document["id"] not in self._handles:
                    sink.close()
                raise
        def admitted():
            for kind, identifier in (("issue", document["id"]), ("session", document["sessionId"]),
                                     ("recording", document["recordingId"])):
                self.access.bind_resource(kind, identifier, body["projectId"], principal.principal_id, meaning="release")
            self._put("issue", document["id"], document)
            self._cancellations[document["id"]] = cancel
        self._submit(document["id"], work, before=admitted)
        return {"issue": document}

    def _finish(self, runtime, document, *, cancelled=False):
        handle, sink = self._handles[document["id"]]
        finished=False
        try:
            stopped = runtime.service.stop(handle, cancelled=cancelled)
            finished=True
            document.update(state=stopped["issue"]["state"], reason=(document.get('reason')
                if document.get('reason')=='authorization_revoked' else stopped["issue"].get("reason")))
            self._put("issue", document["id"], document)
        finally:
            if sink is not None and finished:
                sink.close()
                with self.lab.lock:
                    if sink in self.lab._video_sinks:
                        self.lab._video_sinks.remove(sink)
            if finished:
                with self._lock:
                    self._handles.pop(document["id"], None)
                    self._cancellations.pop(document["id"], None)
                    self._recording_guards.pop(document['id'],None)
                    self._forced_finalizations.discard(document['id'])
                    self._natural_finalizations.discard(document['id'])
                    self._finalizing.discard(document['id'])

    def input(self, principal, issue_id, body):
        exact(body, ("input", "operationId", "sequence", "controllerId", "epoch"))
        public_id(body["operationId"])
        public_id(body["controllerId"])
        check(type(body["epoch"]) is int and body["epoch"] > 0
              and type(body["sequence"]) is int and 0 < body["sequence"] <= 2147483647,
              "invalid_argument", "Invalid input order", 400)
        document=self._issue(principal,issue_id,"session.operate")
        check(document['ownerId']==principal.principal_id and document['state']=='recording',
              'issue_inactive','An owned recording is required',409)
        runtime=self._runtime(document['projectId'])
        self.access.authorize_device(principal,document['deviceId'],document['projectId'],'device.operate')
        receipt=runtime.service.runner.record_manual_input(body['input'],document['sessionId'],
            principal.principal_id,body['controllerId'],body['epoch'],
            operation_id=body['operationId'],sequence=body['sequence'])
        return {'receipt':receipt,'session':self.lab.peek_session(document['sessionId'],principal.principal_id)}

    def _final_log_snapshot(self, runtime, document):
        if (runtime.registration.project['evidencePolicy']['logs'] is not True
                or runtime.registration.collection_policy['captureMode'] != 'test-data'):
            return
        session = self.lab._session(document['sessionId'], document['ownerId'])
        if session.get('automaticAppLogs') is not True:
            return
        try:
            self.lab.app_logs(document['sessionId'], document['ownerId'], _allow_stopping=True)
        except Exception:
            try:
                session['releaseRecorder'].declare_gap('automatic_logs_unavailable')
            except Exception:
                self.lab.fail(document['sessionId'], 'Automatic log failure could not be recorded')

    def stop(self, principal, issue_id, *, cancel=False):
        document = self._issue(principal, issue_id)
        stop_owner=document.get('replayOwnerId') if document['state']=='replaying' else document['ownerId']
        check(stop_owner == principal.principal_id, "forbidden", "Only the operation owner may stop it", 403)
        runtime = self._runtime(document["projectId"])
        if document["state"] == "preparing" or (cancel and document["state"] == "replaying"):
            event = self._cancellations.get(issue_id)
            check(event is not None, "issue_inactive", "Issue operation is no longer active", 409)
            event.set()
            return {"issue": document, "cancellationRequested": True}
        if document["state"] in {"finalizing", "complete", "failed", "cancelled", "quarantined"}:
            return {"issue": document}
        check(document["state"] == "recording" and issue_id in self._handles,
              "issue_inactive", "Issue is not recording", 409)
        with self._lock:
            check(issue_id not in self._stop_snapshots, 'issue_busy', 'The issue stop is already being prepared', 409)
            self._stop_snapshots.add(issue_id)
        try:
            # Read the final automatic snapshot before committing the original
            # barrier. No workflow lock is held across native collection. The
            # stop response is sent only after the barrier closes admission.
            if not cancel:
                self._final_log_snapshot(runtime, document)
            with self._lock:
                current = self._get('issue', issue_id)
                if current['state'] != 'recording':
                    return {'issue': current}
            def before():
                runtime.service.begin_stop(self._handles[issue_id][0])
                document["state"] = "finalizing"
                self._put("issue", issue_id, document)
                self._finalizing.add(issue_id)
            self._submit(issue_id, lambda: self._finish(runtime, document, cancelled=cancel), before=before)
            return {"issue": document}
        finally:
            with self._lock: self._stop_snapshots.discard(issue_id)

    def list(self, principal):
        ids = {binding.resource_id for binding in self.access.visible_bindings(principal, "issue", "issue.read")}
        return {"issues": [document for document in self._all("issue") if document["id"] in ids]}

    def get(self, principal, issue_id):
        document = self._issue(principal, issue_id)
        runtime = self._runtime(document["projectId"])
        source = None
        video = None
        if document["packageId"] is not None:
            package = self.packages.get(document["packageId"], document["projectId"],
                authorize=self._guard(principal, document["projectId"], "issue.read"))
            source, video = package["recording"], package["video"]
        elif document["recordingId"] is not None:
            try:
                source = self.lab.release_recording(document["recordingId"], document["ownerId"])
                if source.get('original') is not None:
                    reference=self.lab._evidence_store.lookup(source['recordingDigest'])
                    check(reference is not None and reference.retain_until_ms>int(time.time()*1000),
                          'package_expired','Original evidence retention has expired',410)
                    for item in source['original']['media']:
                        from .video import VIDEO_MANIFEST_MIME, validate_video_manifest
                        if item['mimeType'] in {VIDEO_MANIFEST_MIME, 'application/json'}:
                            with self._source_pin(item['digest']):
                                video=validate_video_manifest(_json(self.lab._evidence_store.read(item['digest'])))
            except LiveError:
                if document["state"] not in {"preparing", "failed", "quarantined"}:
                    raise
        specification = self._get("specification", document["specificationKey"]) if document["specificationKey"] else None
        approval = self._get("approval", document["approvalKey"]) if document["approvalKey"] else None
        receipts = None
        if not document["imported"]:
            try:
                receipts = runtime.service.get(issue_id)
            except Exception:
                pass
        campaign = None
        if document["campaignId"] is not None and approval is not None:
            campaign = self.engines[document["projectId"]].lookup(document["campaignId"])
        return {"issue": document, "recording": source, "specification": specification,
                "specificationDigest": contracts.digest(specification) if specification else None,
                "specificationCanonical": canonical(specification).decode() if specification else None,
                "approval": approval, "lifecycle": receipts, "campaign": campaign, "video": video}

    @contextmanager
    def _source_pin(self,digest):
        check(self._media_lock.acquire(timeout=10),'media_busy','Another media read is still active',409)
        try:
            check(not self._closed,'issue_closed','Issue workflow is closing',409)
            with self.lab._evidence_store.pin(digest,self._media_pin_id,'export'):
                yield
        finally:self._media_lock.release()

    @contextmanager
    def open_media(self, principal, issue_id, digest):
        contracts.validate_digest(digest)
        document=self._issue(principal,issue_id,'media.read')
        view=self.get(principal,issue_id)
        source=view['recording']
        check(source is not None and source.get('original') is not None,
              'media_unavailable','Original media is unavailable',404)
        references=source['original']['media']+((view['video'] or {}).get('segments',[]))
        reference=next((item for item in references if item['digest']==digest
                        and item['mimeType'] in {'image/png','image/jpeg','video/mp4'}),None)
        check(reference is not None,'media_unavailable','Media is not part of this issue',404)
        authorization=self._guard(principal,document['projectId'],'media.read')
        def guard():
            authorization()
            if document['packageId'] is not None:
                self.packages._row(document['packageId'],document['projectId'],authorization)
            else:
                for item in (source['recordingDigest'],digest):
                    ref=self.lab._evidence_store.lookup(item)
                    check(ref is not None and ref.retain_until_ms>int(time.time()*1000),
                          'media_unavailable','Media retention has expired',410)
            return True
        if document['packageId'] is not None:
            with self.packages.open_archive(document['packageId'],document['projectId'],authorize=authorization) as archive:
                media=archive.object(digest,limits=self.packages.limits)
                guard()
                reader=PackageReader(media['body'],digest,guard)
                try:yield reader,reference['mimeType']
                finally:reader.close()
        else:
            with self._source_pin(digest):
                guard()
                reader=PackageReader(self.lab._evidence_store.read(digest),digest,guard)
                try:yield reader,reference['mimeType']
                finally:reader.close()

    def save_specification(self, principal, issue_id, body):
        exact(body, ("baseRevision", "actions", "waits", "bindings", "fixtures", "assertions"))
        document = self._issue(principal, issue_id, "specification.maintain")
        source = self.get(principal, issue_id)["recording"]
        check(source is not None and source.get("original") is not None,
              "recording_incomplete", "Freeze the original before authoring a specification", 409)
        with self._lock:
            current = self._get("issue", issue_id)
            previous = self._get("specification", current["specificationKey"]) if current["specificationKey"] else None
            revision = previous["revision"] if previous is not None else 0
            check(type(body["baseRevision"]) is int and body["baseRevision"] == revision
                  and revision < 64 and current["state"] != "replaying",
                  "specification_changed", "The specification revision changed", 409)
            specification = {"schemaVersion": 1, "id": "scenario_" + issue_id[6:], "revision": revision + 1,
                "originalRecordingDigest": source["recordingDigest"],
                **{key: copy.deepcopy(body[key]) for key in ("actions", "waits", "bindings", "fixtures", "assertions")},
                "provenance": {"kind": "authored", "source": "local-review",
                    "author": "author_" + hashlib.sha256(principal.principal_id.encode()).hexdigest()[:24],
                    "revision": str(revision + 1)}}
            contracts.validate_specification(specification)
            self._guard(principal,document['projectId'],'specification.maintain')()
            key = "spec_" + contracts.digest(specification)[:48]
            self._put("specification", key, specification, immutable=True)
            current.update(specificationKey=key, approvalKey=None, campaignId=None)
            self._put("issue", issue_id, current)
        return {"specification": specification, "specificationDigest": contracts.digest(specification)}

    def _qualification(self, runtime, recording, specification):
        plans = self._preparations(runtime, recording["original"]["applicationId"], specification["fixtures"])
        return {"schemaVersion": 1, "projectDigest": runtime.registration.project_digest,
            "projectRevision": runtime.registration.project["revision"], "recordingDigest": recording["recordingDigest"],
            "specificationDigest": contracts.digest(specification), "originalBuildId": recording["original"]["buildId"],
            "fixtureRules": [{"fixtureId": item.plan.fixture_id, "equivalenceDigest": item.plan.equivalence_digest} for item in plans],
            "observationRequirements": [{"assertionId": assertion["id"], "observationId": observation_id,
                "coverage": copy.deepcopy(assertion["coverage"])} for assertion in specification["assertions"]
                for observation_id in sorted(predicate_observations(assertion["predicate"]))],
            "runtimePolicyDigest": contracts.digest(runtime.runtime_policy),
            "validationRecipeIds": list(runtime.validation_recipe_ids),
            "attemptBudget": {"original": 3, "candidate": 3, "total": 6}}

    def _approved(self, runtime, recording, specification, approval):
        check(approval["specificationDigest"] == contracts.digest(specification)
              and approval["recordingDigest"] == recording["recordingDigest"]
              and approval["projectDigest"] == runtime.registration.project_digest,
              "approval_changed", "Local approval no longer matches this revision", 409)
        plans = self._preparations(runtime, recording["original"]["applicationId"], specification["fixtures"])
        return runtime.service.registry.register(runtime.registration, recording, specification,
            approval["qualification"], runtime.runtime_policy, fixture_plans=tuple(item.plan for item in plans))

    def approve(self, principal, issue_id, body):
        exact(body, ("specificationDigest", "revision", "bindImported"))
        document = self._issue(principal, issue_id, "specification.maintain")
        runtime = self._runtime(document["projectId"])
        check(type(body["bindImported"]) is bool and (not document["imported"] or body["bindImported"]),
              "local_binding_required", "Imported content requires explicit local binding", 409)
        if document["imported"]:
            self._authorize(principal, document["projectId"], "resource.bind")
        view = self.get(principal, issue_id)
        specification, recording = view["specification"], view["recording"]
        check(specification is not None and type(body["revision"]) is int
              and body["revision"] == specification["revision"]
              and body["specificationDigest"] == contracts.digest(specification),
              "specification_changed", "Approve the exact displayed specification", 409)
        approval = {"specificationDigest": body["specificationDigest"],
            "recordingDigest": recording["recordingDigest"], "projectDigest": runtime.registration.project_digest,
            "qualification": self._qualification(runtime, recording, specification),
            "approvedBy": principal.principal_id, "approvedAtMs": int(time.time() * 1000), "localBinding": True}
        self._approved(runtime, recording, specification, approval)
        key = "approval_" + hashlib.sha256((issue_id + body["specificationDigest"]).encode()).hexdigest()[:48]
        with self._lock:
            latest = self._get("issue", issue_id)
            check(latest["specificationKey"] == document["specificationKey"] and latest["state"] != "replaying",
                  "specification_changed", "Specification changed during approval", 409)
            self._guard(principal,document['projectId'],'specification.maintain')()
            if latest["approvalKey"] is None:
                self._put("approval", key, approval, immutable=True)
                latest["approvalKey"] = key
                self._put("issue", issue_id, latest)
            else:
                approval = self._get("approval", latest["approvalKey"])
        return {"approval": approval}

    def export(self, principal, issue_id):
        document = self._issue(principal, issue_id, "export.read")
        view = self.get(principal, issue_id)
        check(view["specification"] is not None, "specification_required", "Save a specification before export", 409)
        if document["packageId"] is not None:
            return {"package": self.packages.revise(document["packageId"], document["projectId"],
                view["specification"], authorize=self._guard(principal, document["projectId"], "export.read"),
                qualification=view["approval"]["qualification"] if view["approval"] else None)}
        runtime = self._runtime(document["projectId"])
        expires = int(time.time() * 1000) + runtime.registration.collection_policy["retentionSeconds"]["export"] * 1000
        package = self.packages.create(view["recording"], view["specification"],
            project_id=document["projectId"], project_digest=runtime.registration.project_digest,
            expires_at_ms=expires, authorize=self._guard(principal, document["projectId"], "export.read"),
            qualification=view["approval"]["qualification"] if view["approval"] else None)
        return {"package": package}

    def import_archive(self, principal, project_id, source, *, size, digest):
        runtime = self._authorize(principal, project_id, "recording.import")
        expires = int(time.time() * 1000) + runtime.registration.collection_policy["retentionSeconds"]["export"] * 1000
        package = self.packages.import_archive(source, size=size, digest=digest, project_id=project_id,
            project_digest=runtime.registration.project_digest, expires_at_ms=expires,
            authorize=self._guard(principal, project_id, "recording.import"))
        try:
            return self._publish_import(principal, project_id, package)
        except Exception:
            self.packages.withdraw_created(package)
            raise

    def _publish_import(self, principal, project_id, package):
        view = self.packages.get(package["id"], project_id,
            authorize=self._guard(principal, project_id, "recording.import"))
        issue_id = "issue_" + uuid.uuid4().hex
        key = "spec_" + contracts.digest(view["specification"])[:48]
        original = view["recording"]["original"]
        document = {"id": issue_id, "projectId": project_id, "ownerId": principal.principal_id,
            "deviceId": None, "applicationId": original["applicationId"], "buildId": original["buildId"],
            "controllerId": None, "sessionId": None, "recordingId": None, "packageId": package["id"],
            "preparationIds": view["specification"]["fixtures"], "unprepared": True,
            "state": "imported", "reason": "local_binding_required", "specificationKey": key,
            "approvalKey": None, "campaignId": None, "imported": True, "createdAtMs": int(time.time() * 1000)}
        self._guard(principal, project_id, "recording.import")()
        self.access.bind_resource("issue", issue_id, project_id, principal.principal_id, meaning="release")
        self._put("specification", key, view["specification"], immutable=True)
        self._put("issue", issue_id, document)
        return {"issue": document}

    def _wait_replay_inventory(self, device_id, cancel, guard, *, timeout_seconds=15):
        """Wait for an enrolled worker's post-cleanup report between attempts."""
        check(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
              and 0 < timeout_seconds <= 15, 'issue_configuration', 'Invalid inventory wait', 400)
        deadline = time.monotonic() + timeout_seconds
        while True:
            check(not cancel.is_set(), 'cancelled', 'Issue operation was cancelled', 409)
            guard('replay')
            with self.lab.lock:
                device = self.lab.devices.get(device_id)
                check(device is not None, 'device_unavailable', 'Selected device is unavailable', 409)
                if device.get('_inventoryBinding') is None:
                    return
                check(device.get('state') == 'available', 'device_busy', 'Selected device is allocated', 409)
                try:
                    self.lab._check_device_admission(device)
                    return
                except LiveError as error:
                    if error.code != 'inventory_unavailable':
                        raise
            remaining = deadline - time.monotonic()
            check(remaining > 0, 'inventory_unavailable', 'A current worker ownership report is required', 409)
            cancel.wait(min(.05, remaining))

    def replay(self, principal, issue_id, body):
        exact(body, ("deviceId", "clientId", "specificationDigest"))
        public_id(body["deviceId"])
        public_id(body["clientId"])
        contracts.validate_digest(body["specificationDigest"])
        document = self._issue(principal, issue_id, "replay.execute")
        runtime = self._runtime(document["projectId"])
        view = self.get(principal, issue_id)
        check(view["approval"] is not None and document["state"] not in {"preparing", "recording", "finalizing", "replaying"},
              "approval_required", "An idle, locally approved issue is required", 409)
        check(body["specificationDigest"] == contracts.digest(view["specification"]),
              "specification_changed", "Replay specification changed", 409)
        self.access.authorize_device(principal, body["deviceId"], document["projectId"], "device.operate")
        self.lab._validate_release_selection(body["deviceId"], runtime.registration, document["applicationId"], document["buildId"])
        self._authorize(principal, document["projectId"], "fixture.execute")
        approved = self._approved(runtime, view["recording"], view["specification"], view["approval"])
        campaign_id = "campaign_" + hashlib.sha256((issue_id + approved.qualification_digest).encode()).hexdigest()[:48]
        engine = self.engines[document["projectId"]]
        campaign = engine.begin_original(approved, campaign_id=campaign_id)
        existing = engine.get(campaign)
        if existing["state"] != "running":
            return {"campaign": existing, "issue": document}
        cancel = threading.Event()
        selected = dict(document, deviceId=body["deviceId"])
        guard = self._effect_guard(principal, selected, cancel, seconds=900)
        preparations = self._preparations(runtime, document["applicationId"], view["specification"]["fixtures"])
        def attempt(_number):
            guard("replay")
            if _number > 1:
                # The engine only reaches another attempt after confirmed
                # cleanup. An older periodic inventory report can still show
                # the previous reservation; await its authoritative update.
                self._wait_replay_inventory(body['deviceId'], cancel, guard)
            session_id = uuid.uuid4().hex
            recording_id = "recording_" + uuid.uuid4().hex
            attempt_issue = "issue_" + uuid.uuid4().hex
            for kind, identifier in (("session", session_id), ("recording", recording_id)):
                self.access.bind_resource(kind, identifier, document["projectId"], principal.principal_id, meaning="release")
            sink = runtime.frame_sink_factory() if runtime.frame_sink_factory else None
            try:
                return runtime.service.replay(runtime.service.registry.original_execution(approved),
                    registration=runtime.registration,
                    device_id=body["deviceId"], owner=principal.principal_id, controller_id=body["clientId"],
                    preparations=preparations, frame_sink=sink, cancellation=cancel, effect_authorizer=guard,
                    timeout_seconds=120, session_id=session_id, recording_id=recording_id, issue_id=attempt_issue)
            finally:
                if sink is not None:
                    sink.close()
                    with self.lab.lock:
                        if sink in self.lab._video_sinks:
                            self.lab._video_sinks.remove(sink)
        def run():
            result = engine.run_original(approved, attempt, campaign_id=campaign_id)
            current = self._get("issue", issue_id)
            current.update(state=result["verdict"], reason=None)
            self._put("issue", issue_id, current)
            with self._lock:
                self._cancellations.pop(issue_id, None)
        def before():
            current = self._get("issue", issue_id)
            check(current["approvalKey"] == document["approvalKey"]
                  and current["specificationKey"] == document["specificationKey"]
                  and current["state"] != "replaying", "specification_changed", "Issue changed before replay", 409)
            current.update(state="replaying", campaignId=campaign_id,replayOwnerId=principal.principal_id)
            self._put("issue", issue_id, current)
            self._cancellations[issue_id] = cancel
        self._submit(issue_id, run, before=before)
        return {"issue": self._get("issue", issue_id)}

    def close(self):
        with self._lock:
            if self._closed and self._db is None:
                return
            self._closed = True
            self._monitor_stop.set()
            for event in self._cancellations.values():
                event.set()
            threads = list(self._threads)
        self._monitor_thread.join(2)
        if self.repairs is not None:
            self.repairs.close()
        self._maintenance_thread.join(10)
        check(not self._maintenance_thread.is_alive(), "issue_busy", "Retention cleanup is still running", 409)
        deadline = time.monotonic() + 50
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        check(not any(thread.is_alive() for thread in threads), "issue_busy", "Issue cleanup is still running", 409)
        for issue_id in list(self._handles):
            document = self._get("issue", issue_id)
            self._finish(self.runtimes[document["projectId"]], document, cancelled=True)
        check(self._media_lock.acquire(timeout=10),'media_busy','Media cleanup is still running',409)
        self._media_lock.release()
        self.packages.close()
        for engine in self.engines.values():
            engine.close()
        with self._lock:
            self._db.close()
            self._db = None
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
