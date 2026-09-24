"""Usable G4 prepared-recording and approved-replay orchestration API."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from .. import contracts
from ..fixtures import (FixtureAllocation, FixtureCoordinator,
                        TrustedFixturePlan)
from ..qualification import ApprovedExecution, ScenarioRegistry
from ..scenario_runner import ScenarioRunResult, ScenarioRunner
from ..storage import read_json, write_json
from .model import Lab, LiveError, TrustedDeviceReservation
from .recording_session import TrustedProjectRegistration


class IssueSessionError(RuntimeError):
    def __init__(self, code, message="Issue session failed", *, issue_id=None):
        super().__init__(message);self.code=code;self.issue_id=issue_id


def _require(condition, code="issue_invalid", message="Issue session is invalid"):
    if not condition:
        raise IssueSessionError(code, message)


def _operation(*parts):
    return "operation_" + hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:40]


FIXTURE_RESERVATION_VERSION = 1


def fixture_reservation_id(issue_id,fixture_id):
    contracts.validate_id(issue_id);contracts.validate_id(fixture_id)
    return 'allocation_'+hashlib.sha256(('issue-fixture-v1\0'+issue_id+'\0'+fixture_id).encode()).hexdigest()[:40]


@dataclass(frozen=True, slots=True)
class FixturePreparation:
    plan: TrustedFixturePlan
    payload: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class IssueSessionHandle:
    issue_id: str
    session_id: str
    recording_id: str
    owner: str = field(repr=False, compare=False)
    controller_id: str
    _issuer: object = field(repr=False, compare=False)


class IssueSessionService:
    """Own preparation, recording authority, stop, and both cleanup domains."""

    def __init__(self, lab: Lab, fixtures: FixtureCoordinator, *, root=None,
                 scenario_registry: ScenarioRegistry | None = None,
                 scenario_runner: ScenarioRunner | None = None):
        _require(type(lab) is Lab and type(fixtures) is FixtureCoordinator)
        self.lab=lab;self.fixtures=fixtures
        self.root=Path(root or (Path(lab.output)/"issue-sessions-v1"))
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.registry=scenario_registry;self.runner=scenario_runner
        if scenario_registry is not None:
            _require(type(scenario_registry) is ScenarioRegistry)
        if scenario_runner is not None:
            _require(type(scenario_runner) is ScenarioRunner
                     and scenario_runner.registry is scenario_registry)
        self._issuer=object();self._lock=threading.RLock()
        self._active={};self._records={}
        self._recover()

    def _recover(self):
        for path in sorted(self.root.glob("*.json")):
            try:
                contracts.validate_id(path.stem)
                value=read_json(path)
                _require(type(value) is dict and value.get("issueId")==path.stem)
                if value.get("state") in {"preparing","recording","finalizing","cleaning"}:
                    value.update(state="quarantined",reason="process_restarted",
                                 updatedAtMs=int(time.time()*1000))
                    write_json(path,value)
                self._records[value["issueId"]]=value
            except Exception:
                continue

    def _persist(self, record):
        public=copy.deepcopy(record)
        write_json(self.root/f"{record['issueId']}.json",public)
        self._records[record["issueId"]]=public

    def _handle(self, handle):
        _require(type(handle) is IssueSessionHandle and handle._issuer is self._issuer,
                 "trusted_issue", "Trusted issue session is required")
        active=self._active.get(handle.issue_id)
        _require(active is not None and active["handle"] is handle,
                 "trusted_issue", "Issue session is unavailable")
        return active

    def _checked_preparations(self, preparations, registration, application_id):
        _require(type(registration) is TrustedProjectRegistration,
                 "trusted_registration", "Trusted project registration is required")
        _require(type(preparations) in (list, tuple) and len(preparations) <= 128)
        result = []
        seen = set()
        for item in preparations:
            _require(type(item) is FixturePreparation, "trusted_fixture")
            try:
                plan = self.fixtures.require_plan(item.plan)
                payload = copy.deepcopy(item.payload)
                self.fixtures.payload_digest(payload)
            except Exception:
                raise IssueSessionError("fixture_binding", "Fixture registration changed") from None
            _require(plan.project_digest == registration.project_digest
                     and plan.application_id == application_id and plan.fixture_id not in seen,
                     "fixture_binding", "Fixture selection changed")
            seen.add(plan.fixture_id)
            result.append(FixturePreparation(plan, payload))
        return tuple(result)

    def start_prepared_recording(self, *, device_id, owner, controller_id,
                                 registration, application_id, build_id,
                                 preparations, frame_sink=None,
                                 authority_grant=None, cancellation=None,
                                 timeout_seconds=30, effect_authorizer=None,
                                 issue_id=None, session_id=None, recording_id=None,candidate_binding=None,
                                 device_scope=None,_candidate_identity=None,
                                 _candidate_profile=None,_provider_factory=None,
                                 _startup_binding=None):
        preparations = self._checked_preparations(preparations, registration, application_id)
        _require(type(timeout_seconds) in (int,float)
                 and not isinstance(timeout_seconds,bool)
                 and 0<timeout_seconds<=60,"issue_invalid")
        cancel=cancellation or threading.Event()
        _require(callable(getattr(cancel,"is_set",None)),"issue_invalid")
        if device_scope is not None:
            self.lab.validate_retained_device_scope(
                device_scope,owner=owner,device_id=device_id,
                registration=registration,application_id=application_id,
                build_id=build_id,candidate_binding=candidate_binding,
                _candidate_identity=_candidate_identity,
                _candidate_profile=_candidate_profile)
        issue_id=issue_id or "issue_"+uuid.uuid4().hex
        contracts.validate_id(issue_id)
        def authorize(kind):
            if effect_authorizer is not None:
                _require(callable(effect_authorizer) and effect_authorizer(kind) is True,
                         "authorization_revoked", "Issue authorization was revoked")
        record={"schemaVersion":1,"issueId":issue_id,"state":"preparing",
                "deviceId":device_id,"applicationId":application_id,
                "buildId":build_id,"projectDigest":registration.project_digest,
                "sessionId":None,"recordingId":None,"preparation":[],
                "fixtures":[],"cleanup":[],"createdAtMs":int(time.time()*1000),
                "updatedAtMs":int(time.time()*1000),"reason":None}
        record['fixtureReservationVersion']=FIXTURE_RESERVATION_VERSION
        record['fixtureReservations']=[{'fixtureId':item.plan.fixture_id,
            'allocationId':fixture_reservation_id(issue_id,item.plan.fixture_id)} for item in preparations]
        with self._lock:
            _require(issue_id not in self._records,"issue_conflict")
            self._persist(record)
        device_reservation=None;allocated=[];session=None
        try:
            authorize("reservation")
            if device_scope is None:
                device_reservation=self.lab.reserve_release_device(
                    device_id,owner,"reservation_"+uuid.uuid4().hex,registration,
                    application_id=application_id,build_id=build_id,
                    authority_grant=authority_grant,candidate_binding=candidate_binding)
            for index,item in enumerate(preparations,1):
                if cancel.is_set():
                    raise IssueSessionError("cancelled","Issue session was cancelled")
                plan=item.plan
                authorize("fixture_reserve")
                _require(plan.project_digest==registration.project_digest
                         and plan.application_id==application_id,
                         "fixture_binding","Fixture selection changed")
                allocation=self.fixtures.reserve(
                    plan,owner=owner,device_id=device_id,
                    allocation_id=fixture_reservation_id(issue_id,plan.fixture_id))
                allocated.append((plan,allocation))
                # Recovery must know the original allocation before any remote
                # preparation can start or its response can be interrupted.
                record["fixtures"].append(self.fixtures.status(allocation))
                record["updatedAtMs"]=int(time.time()*1000);self._persist(record)
                outcome=self.fixtures.prepare(
                    plan,allocation,payload=item.payload,
                    operation_id=_operation(issue_id,plan.fixture_id,"prepare"),
                    timeout_seconds=timeout_seconds,effect_authorizer=effect_authorizer)
                record["fixtures"][-1]=self.fixtures.status(allocation)
                record["preparation"].extend(copy.deepcopy(list(outcome.receipts)))
                record["updatedAtMs"]=int(time.time()*1000);self._persist(record)
                _require(outcome.status=="complete","preparation_unknown",
                         "Fixture preparation was not confirmed")
            if cancel.is_set():
                raise IssueSessionError("cancelled","Issue session was cancelled")
            authorize("session_create")
            startup_deadline = time.monotonic() + timeout_seconds
            session=self.lab.create_release_session(
                device_id,owner,controller_id,registration,
                application_id=application_id,build_id=build_id,
                preparation_receipts=copy.deepcopy(record["preparation"]),
                preparation_unknown=not bool(preparations),
                authority_grant=authority_grant,frame_sink=frame_sink,
                device_reservation=device_reservation,effect_authorizer=effect_authorizer,
                session_id=session_id,recording_id=recording_id,candidate_binding=candidate_binding,
                device_scope=device_scope,_candidate_identity=_candidate_identity,
                _candidate_profile=_candidate_profile,_provider_factory=_provider_factory,
                _startup_binding=_startup_binding)
            device_reservation=None
            # Remote and native providers can acknowledge startup before their
            # first frame. Keep preparation closed to QA input until the actual
            # session becomes active, with cancellation and authority checked
            # throughout the bounded wait.
            while session["state"] == "connecting":
                if cancel.is_set():
                    raise IssueSessionError("cancelled", "Issue session was cancelled")
                authorize("session_startup")
                remaining = startup_deadline - time.monotonic()
                _require(remaining > 0, "startup_failed", "Prepared provider did not become active")
                time.sleep(min(.025, remaining))
                session = self.lab.get_session(session["id"], owner)
            if cancel.is_set():
                raise IssueSessionError("cancelled", "Issue session was cancelled")
            authorize("session_startup")
            _require(session["state"]=="active","startup_failed",
                     "Prepared provider did not become active")
            recording_id=session["releaseRecordingId"]
            handle=IssueSessionHandle(issue_id,session["id"],recording_id,
                                      owner,session["controllerId"],self._issuer)
            record.update(state="recording",sessionId=session["id"],
                          recordingId=recording_id,
                          updatedAtMs=int(time.time()*1000))
            active={"handle":handle,"record":record,"registration":registration,
                    "allocations":allocated,"cancel":cancel,"deviceScope":device_scope}
            with self._lock:
                self._active[issue_id]=active;self._persist(record)
            return handle
        except Exception as error:
            device_ok=True
            if device_reservation is not None:
                try:
                    released=self.lab.release_device_reservation(device_reservation)
                    device_ok=released.get("state")=="available"
                except Exception:
                    device_ok=False
            if session is not None:
                device_ok=self._close_device(session["id"],owner,timeout_seconds) and device_ok
            cleanup=self._cleanup_fixtures(issue_id,allocated,timeout_seconds,
                                           producer_stopped=device_ok)
            unsafe=(any(item["status"]!="complete" for item in cleanup)
                    or not device_ok)
            record.update(state="quarantined" if unsafe else "failed",
                          cleanup=cleanup,
                          deviceCleanup="complete" if device_ok else "unknown",
                          reason=(getattr(error,"code",None)
                          if getattr(error,"code",None) in {
                              "cancelled","preparation_unknown","startup_failed"}
                          else "cleanup_incomplete" if unsafe
                          else "preparation_failed"),
                          updatedAtMs=int(time.time()*1000))
            with self._lock:self._persist(record)
            if isinstance(error,IssueSessionError):
                error.issue_id=issue_id;raise
            raise IssueSessionError("preparation_failed",
                                    "Prepared recording could not start",
                                    issue_id=issue_id) from None

    def input(self, handle, command, *, recording_input):
        active=self._handle(handle)
        _require(active["record"]["state"]=="recording","issue_inactive",
                 "Issue session is not recording")
        return self.lab.input(handle.session_id,handle.owner,command,
                              recording_input=recording_input)

    def begin_stop(self, handle, *, controller=None, epoch=None):
        """Commit the admission barrier before scheduling bounded finalization."""
        with self._lock:
            active=self._handle(handle);record=active["record"]
            _require(record["state"]=="recording","issue_inactive",
                     "Issue session is not recording")
            current=self.lab.peek_session(handle.session_id,handle.owner)
            controller=controller or current["controllerId"]
            epoch=current["epoch"] if epoch is None else epoch
            self.lab.begin_release_stop(handle.session_id,handle.owner,controller,epoch,cleanup_only=True)
            active["stop_context"]=(controller,epoch)
            record.update(state="finalizing",updatedAtMs=int(time.time()*1000))
            self._persist(record)
            return copy.deepcopy(record)

    def stop(self, handle, *, controller=None, epoch=None, cancelled=False,
             cleanup_timeout_seconds=10):
        _require(type(cleanup_timeout_seconds) in (int,float)
                 and 0<cleanup_timeout_seconds<=60,"issue_invalid")
        with self._lock:
            active=self._handle(handle);record=active["record"]
            if record["state"]=="recording":
                self.begin_stop(handle,controller=controller,epoch=epoch)
            _require(record["state"]=="finalizing" and not active.get("finalizer_started"),
                     "issue_inactive","Issue finalization is already running")
            stopped_controller,stopped_epoch=active["stop_context"]
            _require(controller in (None,stopped_controller) and epoch in (None,stopped_epoch),
                     "issue_inactive","Stop controller changed")
            controller,epoch=stopped_controller,stopped_epoch
            active["finalizer_started"]=True
        frozen=None;stop_error=None
        try:
            frozen=self.lab.finalize_release_recording(
                handle.session_id,handle.owner,controller,epoch)
        except Exception as error:
            stop_error=error
            try:frozen=self.lab.release_recording(handle.recording_id,handle.owner)
            except Exception:pass
        original_digest=(frozen or {}).get("recordingDigest")
        original_before=copy.deepcopy((frozen or {}).get("original"))
        record.update(state="cleaning",recordingDigest=original_digest,
                      updatedAtMs=int(time.time()*1000));self._persist(record)
        device_ok=self._close_device(handle.session_id,handle.owner,
                                     cleanup_timeout_seconds,controller,epoch)
        cleanup=self._cleanup_fixtures(
            handle.issue_id,active["allocations"],cleanup_timeout_seconds,
            producer_stopped=device_ok)
        fixture_ok=all(item["status"]=="complete" for item in cleanup)
        if original_digest is not None:
            for item in cleanup:
                try:
                    self.lab.append_release_lifecycle(
                        handle.recording_id,handle.owner,
                        operation_id=item["operationId"],
                        generation=item["generation"],kind="cleanup",
                        status=item["status"])
                except Exception:
                    fixture_ok=False
            device_operation=_operation(handle.issue_id,"device","cleanup")
            try:
                self.lab.append_release_lifecycle(
                    handle.recording_id,handle.owner,
                    operation_id=device_operation,generation=1,kind="cleanup",
                    status="complete" if device_ok else "unknown")
            except Exception:
                device_ok=False
        immutable=False
        if original_digest is not None:
            try:
                after=self.lab.release_recording(handle.recording_id,handle.owner)
                immutable=(after.get("recordingDigest")==original_digest
                           and after.get("original")==original_before
                           and contracts.digest(after["original"])==original_digest)
                frozen=after
            except Exception:
                immutable=False
        successful=(stop_error is None and fixture_ok and device_ok and immutable
                    and (frozen or {}).get("status")=="frozen-complete"
                    and not cancelled)
        state=("complete" if successful else
               "cancelled" if cancelled and fixture_ok and device_ok else
               "quarantined" if not fixture_ok or not device_ok else "failed")
        record.update(state=state,cleanup=cleanup,
                      deviceCleanup="complete" if device_ok else "unknown",
                      originalImmutable=immutable,
                      reason=(None if successful else "cancelled" if cancelled
                              else "cleanup_incomplete" if not fixture_ok or not device_ok
                              else "recording_incomplete"),
                      updatedAtMs=int(time.time()*1000))
        with self._lock:
            self._persist(record);self._active.pop(handle.issue_id,None)
        return {"issue":copy.deepcopy(record),"recording":copy.deepcopy(frozen)}

    def _close_device(self, session_id, owner, timeout_seconds, controller=None, epoch=None):
        completed=threading.Event();result={}
        def close():
            try:
                result["closed"]=self.lab.close_session(
                    session_id,owner,controller,epoch,require_finished_inputs=True)
            except Exception:
                pass
            finally:
                completed.set()
        threading.Thread(target=close,name="reproof-issue-cleanup",daemon=True).start()
        return (completed.wait(timeout_seconds)
                and result.get("closed",{}).get("state")=="closed")

    def _cleanup_fixtures(self, issue_id, allocations, timeout_seconds, *, producer_stopped=True):
        results=[]
        for plan,allocation in reversed(allocations):
            operation_id=_operation(issue_id,plan.fixture_id,"cleanup",
                                    str(allocation.generation))
            try:
                if not producer_stopped:
                    self.fixtures.retain_for_cleanup(allocation)
                    status="unknown"
                else:
                    result=self.fixtures.cleanup(
                        plan,allocation,operation_id=operation_id,
                        timeout_seconds=timeout_seconds)
                    status=("complete" if result["status"]=="complete"
                            and self.fixtures.status(allocation)["state"]=="available"
                            else "failed" if result["status"]=="failed" else "unknown")
            except Exception:
                status="unknown"
            results.append({"fixtureId":plan.fixture_id,
                            "allocationId":allocation.allocation_id,
                            "generation":allocation.generation,
                            "operationId":operation_id,"status":status})
        return results

    def replay(self, execution: ApprovedExecution, *, registration,
               device_id, owner, controller_id, preparations,
               frame_sink=None, authority_grant=None, cancellation=None,
               timeout_seconds=120, effect_authorizer=None, issue_id=None,
               session_id=None, recording_id=None,device_scope=None,
               _candidate_identity=None,_candidate_profile=None,
               _provider_factory=None,_startup_binding=None):
        _require(self.registry is not None and self.runner is not None,
                 "scenario_unavailable", "Scenario runner is not configured")
        execution=self.registry.require_execution(execution)
        _require(type(registration) is TrustedProjectRegistration
                 and registration.project_digest == execution.approved.project_digest,
                 "fixture_binding", "Replay project changed")
        preparations=self._checked_preparations(
            preparations,registration,execution.approved.original["applicationId"])
        expected=dict(execution.approved.fixture_equivalence)
        actual={item.plan.fixture_id:item.plan.equivalence_digest for item in preparations}
        _require(actual==expected,"fixture_binding","Fixture selection changed")
        original_receipts={item["recipeId"]:item["payloadDigest"]
                           for item in execution.approved.original["preparation"]
                           if item["status"]=="complete"}
        if execution.approved.preparation_known:
            for item in preparations:
                payload_digest=self.fixtures.payload_digest(item.payload)
                _require(all(original_receipts.get(key)==payload_digest for key in
                             (item.plan.fixture_id,*item.plan.check_recipe_ids)),
                         "fixture_binding","Replay preparation differs from the original")
        handle=self.start_prepared_recording(
            device_id=device_id,owner=owner,controller_id=controller_id,
            registration=registration,
            application_id=execution.approved.original["applicationId"],
            build_id=execution.build_id,preparations=preparations,
            frame_sink=frame_sink,authority_grant=authority_grant,
            cancellation=cancellation,
            timeout_seconds=min(float(timeout_seconds),60),effect_authorizer=effect_authorizer,
            issue_id=issue_id,session_id=session_id,recording_id=recording_id,
            candidate_binding=execution.candidate_binding,device_scope=device_scope,
            _candidate_identity=_candidate_identity,_candidate_profile=_candidate_profile,
            _provider_factory=_provider_factory,_startup_binding=_startup_binding)
        current_issue=self.get(handle.issue_id)
        actual_receipts={item["recipeId"]:item["payloadDigest"]
                         for item in current_issue["preparation"]
                         if item["status"]=="complete"}
        required=set()
        for item in preparations:
            required.add(item.plan.fixture_id)
            required.update(item.plan.check_recipe_ids)
        if (execution.approved.preparation_known
                and (not required <= set(original_receipts)
                     or any(actual_receipts.get(key)!=original_receipts.get(key)
                            for key in required))):
            self.stop(handle)
            raise IssueSessionError("fixture_binding",
                                    "Replay preparation differs from the original")
        current=self.lab.get_session(handle.session_id,owner)
        try:
            result=self.runner.run(
                execution,handle.session_id,owner,current["controllerId"],
                current["epoch"],cancellation=cancellation,
                timeout_seconds=timeout_seconds)
        except Exception:
            self.stop(handle)
            raise IssueSessionError("scenario_failed",
                                    "Approved scenario execution failed") from None
        stopped=self.stop(handle,cancelled=result.verdict=="cancelled")
        recording=stopped.get("recording") or {}
        digest=recording.get("recordingDigest")
        _require(type(digest) is str,"recording_incomplete",
                 "Replay evidence was not frozen")
        cleanup=("complete" if stopped["issue"].get("deviceCleanup")=="complete"
                 and all(item["status"]=="complete" for item in stopped["issue"]["cleanup"])
                 else "failed" if stopped["issue"]["state"]=="quarantined"
                 else "unknown")
        return self.runner.finalize(
            result,cleanup=cleanup,attempt_recording_digest=digest,
            fixture_equivalence=actual)

    def get(self, issue_id):
        with self._lock:
            value=self._records.get(issue_id)
            _require(value is not None,"not_found","Issue session was not found")
            return copy.deepcopy(value)

    def list(self):
        with self._lock:
            return [copy.deepcopy(value) for value in sorted(
                self._records.values(),key=lambda item:item["createdAtMs"],reverse=True)]

    def release_retained_device_scope(self, scope, *, owner):
        """Release an enclosing device owner after its last replay cleanup."""
        _require(getattr(scope,"owner",None)==owner,"stale_controller",
                 "Device scope owner changed")
        with self._lock:
            active=[item for item in self._active.values()
                    if item.get("deviceScope") is scope]
            _require(not active,"issue_inactive",
                     "Retained device scope still has an active issue")
        return self.lab.release_retained_device_scope(scope)


__all__ = ["FixturePreparation","IssueSessionError","IssueSessionHandle",
           "IssueSessionService"]
