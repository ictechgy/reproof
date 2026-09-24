"""Bounded trusted interpreter for approved G4 executable specifications."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import threading
import time

from . import contracts
from .contracts.scenario import predicate_observations
from .live.model import Lab, LiveError
from .live.recording_session import TrustedProjectRegistration
from .qualification import ApprovedExecution, ScenarioRegistry


class ScenarioError(RuntimeError):
    def __init__(self, code, message="Scenario execution failed"):
        super().__init__(message)
        self.code = code


def _require(condition, code="scenario_invalid", message="Scenario execution is invalid"):
    if not condition:
        raise ScenarioError(code, message)


def _scalar(value):
    _require(value is None or type(value) in (str, int, float, bool),
             "observation_invalid", "Observation value is invalid")
    if type(value) is float:
        _require(math.isfinite(value), "observation_invalid",
                 "Observation value is invalid")
    if type(value) is str:
        _require(len(value.encode("utf-8")) <= 4096, "observation_invalid",
                 "Observation value is invalid")
    return copy.deepcopy(value)


def _reject_protected_values(value, secrets):
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            pending.extend(item);pending.extend(item.values())
        elif type(item) in (list, tuple):
            pending.extend(item)
        elif type(item) is str:
            _require(not any(secret and secret in item for secret in secrets),
                     "secret_observation", "Observation contains a protected value")


@dataclass(frozen=True, slots=True)
class StaticVariableResolver:
    value: object = field(repr=False)

    def resolve(self, context):
        return copy.deepcopy(self.value)


class VariableResolverRegistry:
    """Process-local variable capabilities; resolved values are never serialized."""

    def __init__(self, registration):
        _require(type(registration) is TrustedProjectRegistration,
                 "trusted_registration", "Trusted project registration is required")
        self.project = registration.project
        self.project_digest = registration.project_digest
        self._resolvers = {}

    def register(self, variable_id, resolver):
        variables = {item["id"]: item for item in self.project["variables"]}
        _require(variable_id in variables and callable(getattr(resolver, "resolve", None)),
                 "variable_invalid", "Variable resolver is invalid")
        _require(variable_id not in self._resolvers, "variable_conflict",
                 "Variable resolver is already registered")
        self._resolvers[variable_id] = resolver
        return variable_id

    def resolve(self, variable_id, context):
        declaration = next((item for item in self.project["variables"]
                            if item["id"] == variable_id), None)
        _require(declaration is not None, "variable_invalid",
                 "Variable is not registered")
        resolver = self._resolvers.get(variable_id)
        if resolver is None:
            _require("default" in declaration, "variable_unavailable",
                     "Variable value is unavailable")
            value = copy.deepcopy(declaration["default"])
        else:
            try:
                value = resolver.resolve(copy.deepcopy(context))
            except Exception:
                raise ScenarioError("variable_unavailable",
                                    "Variable value is unavailable") from None
        expected = {"string": str, "integer": int, "boolean": bool,
                    "secret-reference": str}[declaration["type"]]
        _require(type(value) is expected, "variable_invalid",
                 "Variable value has the wrong type")
        if type(value) is str:
            _require(len(value.encode("utf-8")) <= 256, "variable_invalid",
                     "Variable value exceeds the provider bound")
        if type(value) is int:
            _require(-(2 ** 31) <= value <= 2 ** 31 - 1, "variable_invalid")
        return value, declaration["secret"] is True


@dataclass(frozen=True, slots=True)
class ObservationRequest:
    observation_id: str
    assertion_id: str
    application_id: str
    requirement: dict
    anchor_ms: int
    deadline_monotonic: float
    session_id: str
    owner: str = field(repr=False)
    lab: Lab = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ObservationEvidence:
    envelope: dict
    # property -> scalar or [(epoch_ms, scalar), ...]
    values: dict
    # Explicitly absent for the complete declared capture interval. An omitted
    # value alone is unknown and cannot establish absence.
    absent_properties: tuple[str, ...] = ()


class ObservationRegistry:
    """Register trusted provider/backend observation capabilities explicitly."""

    def __init__(self, registration):
        _require(type(registration) is TrustedProjectRegistration,
                 "trusted_registration", "Trusted project registration is required")
        self.project = registration.project
        self.project_digest = registration.project_digest
        self._adapters = {}

    def register(self, observation_id, adapter):
        _require(observation_id in self.project["observations"]
                 and callable(getattr(adapter, "observe", None))
                 and type(getattr(adapter, "provider_incarnation", None)) is str
                 and isinstance(getattr(adapter, "coverage_classes", None),
                                (set, frozenset, tuple, list))
                 and set(adapter.coverage_classes)
                 <= {"snapshot", "sampled", "continuous"},
                 "observation_invalid", "Observation adapter is invalid")
        _require(observation_id not in self._adapters,
                 "observation_conflict", "Observation adapter is already registered")
        contracts.validate_id(adapter.provider_incarnation,
                              "observation provider incarnation")
        self._adapters[observation_id] = adapter
        return observation_id

    def adapter(self, observation_id, required_class):
        adapter = self._adapters.get(observation_id)
        _require(adapter is not None and required_class in adapter.coverage_classes,
                 "observation_unsupported", "Required observation is unsupported")
        return adapter


_RESULT_ISSUER = object()


@dataclass(frozen=True, slots=True)
class ScenarioRunResult:
    run_id: str
    phase: str
    verdict: str
    project_digest: str
    recording_digest: str
    specification_digest: str
    qualification_digest: str
    build_id: str
    preparation_known: bool
    fixture_equivalence: tuple[tuple[str, str], ...]
    receipts: tuple[dict, ...]
    observations: tuple[dict, ...]
    defect: bool | None
    expected: bool | None
    coverage: str
    valid: bool
    cleanup: str
    attempt_recording_digest: str | None
    started_monotonic_ns: int
    _issuer: object = field(repr=False, compare=False)

    @property
    def trusted(self):
        return self._issuer is _RESULT_ISSUER

    def public(self):
        _require(self.trusted, "untrusted_attempt")
        injection=("injected" if self.receipts
                   and all(item.get("status")=="injected" for item in self.receipts)
                   else "cancelled" if self.verdict=="cancelled"
                   else "failed" if self.verdict=="failed" else "unknown")
        observation="observed" if self.coverage=="complete" else "unknown"
        return {
            "runId": self.run_id, "phase": self.phase,
            "verdict": self.verdict, "projectDigest": self.project_digest,
            "injection": injection, "observation": observation,
            "recordingDigest": self.recording_digest,
            "specificationDigest": self.specification_digest,
            "qualificationDigest": self.qualification_digest,
            "buildId": self.build_id,
            "preparationKnown": self.preparation_known,
            "fixtureEquivalence": dict(self.fixture_equivalence),
            "receipts": copy.deepcopy(list(self.receipts)),
            "observations": copy.deepcopy(list(self.observations)),
            "defect": self.defect, "expected": self.expected,
            "coverage": self.coverage, "valid": self.valid,
            "cleanup": self.cleanup,
            "attemptRecordingDigest": self.attempt_recording_digest,
        }


class ScenarioRunner:
    """Execute only registry-issued specifications through an active Lab session."""

    def __init__(self, lab: Lab, registry: ScenarioRegistry,
                 variables: VariableResolverRegistry,
                 observations: ObservationRegistry, *,
                 monotonic=time.monotonic, wall_clock_ms=None):
        _require(type(lab) is Lab and type(registry) is ScenarioRegistry
                 and type(variables) is VariableResolverRegistry
                 and type(observations) is ObservationRegistry,
                 "scenario_invalid")
        self.lab = lab;self.registry = registry
        self.variables = variables;self.observations = observations
        self.monotonic = monotonic
        self.wall_clock_ms = wall_clock_ms or (lambda: int(time.time() * 1000))
        self._result_lock = threading.RLock()
        self._issued_results = {}
        self._final_results = {}

    def _retain_result(self, target, result):
        # These are short-lived process capabilities, not retained evidence.
        # Eviction denies reuse; it never restores an expired capability.
        with self._result_lock:
            if len(target) >= 4096:
                target.pop(next(iter(target)))
            target[result.run_id] = result
        return result

    def record_manual_input(self, action, session_id, owner, controller, epoch, *,
                            operation_id, sequence):
        """Record one operator action using only registered variable resolvers.

        This is an input operation, not a scenario approval or replay verdict.
        The Lab enforces the current device/session authorization and barrier.
        """
        action=contracts.validate_input(action)
        contracts.validate_id(operation_id)
        _require(type(sequence) is int and 1<=sequence<=100000,"scenario_invalid")
        session = self.lab._session(session_id, owner)
        with session['lock']:
            self.lab._control(session, controller, epoch)
        cancel=threading.Event();deadline=self.monotonic()+10
        binding=self.lab.release_binding(session_id,owner)
        _require(binding['projectDigest']==self.variables.project_digest,"scenario_binding")
        resolved={}
        try:
            if action['action']=='text':
                variable=action['parameters']['variableId']
                context={'projectId':self.variables.project['id'],
                         'applicationId':binding['applicationId'],'buildId':binding['buildId'],
                         'phase':'recording','runId':operation_id}
                value,_secret=self._bounded_call(
                    lambda:self.variables.resolve(variable,context),cancel,deadline)
                resolved[variable]=value
            command,_locator=self._bounded_call(
                lambda:self._command(dict(action,eventId=operation_id),session_id,owner,
                    controller,epoch,sequence,session_id,resolved),cancel,deadline)
            return self.lab.input(session_id,owner,command,recording_input=action)
        finally:
            resolved.clear()

    def run(self, execution: ApprovedExecution, session_id: str, owner: str,
            controller: str, epoch: int, *, cancellation=None,
            timeout_seconds: float = 120):
        started_monotonic_ns = time.monotonic_ns()
        execution = self.registry.require_execution(execution)
        approved = execution.approved
        _require(self.variables.project_digest == approved.project_digest
                 and self.observations.project_digest == approved.project_digest,
                 "scenario_binding", "Scenario registry binding changed")
        _require(type(timeout_seconds) in (int, float)
                 and not isinstance(timeout_seconds, bool)
                 and 0 < timeout_seconds <= 900, "scenario_invalid")
        cancel = cancellation or threading.Event()
        _require(callable(getattr(cancel, "is_set", None))
                 and callable(getattr(cancel, "wait", None)),
                 "scenario_invalid")
        binding = self.lab.release_binding(session_id, owner)
        _require((binding["projectDigest"], binding["applicationId"],
                  binding["buildId"],binding['artifactDigest']) ==
                 (approved.project_digest, approved.original["applicationId"],
                  execution.build_id,execution.build_digest), "scenario_binding",
                 "Release session selection changed")
        deadline = self.monotonic() + float(timeout_seconds)
        run_id = "run_" + hashlib.sha256(
            (session_id + "\0" + approved.specification_digest + "\0"
             + str(self.wall_clock_ms())).encode("utf-8")).hexdigest()[:40]
        context = {"projectId": approved.project["id"],
                   "applicationId": approved.original["applicationId"],
                   "buildId": execution.build_id, "phase": execution.phase,
                   "runId": run_id}
        resolved = {};secret_values = []
        receipts = []
        observation_summaries = []
        defect = expected = None
        coverage = "unknown";valid = True;verdict = "unknown"
        try:
            needed = {action["parameters"]["variableId"]
                      for action in approved.specification["actions"]
                      if action["action"] == "text"}
            needed.update(binding_["variableId"]
                          for binding_ in approved.specification["bindings"])
            for variable_id in sorted(needed):
                value, secret = self._bounded_call(
                    lambda variable_id=variable_id: self.variables.resolve(variable_id, context),
                    cancel, deadline)
                resolved[variable_id] = value
                if secret and type(value) is str:
                    secret_values.append(value)
            waits = {item["afterEventId"]: item["durationMs"]
                     for item in approved.specification["waits"]}
            sequence = 0
            for action in approved.specification["actions"]:
                self._active(cancel, deadline)
                sequence += 1
                try:
                    command, locator_evidence = self._bounded_call(
                        lambda: self._command(action, session_id, owner, controller,
                                              epoch, sequence, run_id, resolved),
                        cancel, deadline)
                    self._active(cancel, deadline)
                except LiveError as error:
                    verdict = ("failed" if error.code in {
                        "invalid_argument", "unsupported_operation"
                    } else "unknown")
                    valid = False
                    break
                locator_artifact = None
                if locator_evidence is not None:
                    try:
                        _reject_protected_values(locator_evidence, secret_values)
                        locator_artifact = self.lab.record_registered_observation(
                            session_id, owner,
                            {"kind": "locator", "eventId": action["eventId"],
                             "evidence": copy.deepcopy(locator_evidence)})
                    except LiveError:
                        verdict = "unknown"
                        valid = False
                        break
                recording_input = {key: copy.deepcopy(value)
                                   for key, value in action.items()
                                   if key != "eventId"}
                try:
                    self._active(cancel, deadline)
                    receipt = self._bounded_call(
                        lambda command=command, recording_input=recording_input:
                            self.lab.input(session_id, owner, command, recording_input=recording_input),
                        cancel, deadline)
                except ScenarioError:
                    receipts.append({"eventId": action["eventId"],
                                     "operationId": command["commandId"], "status": "unknown"})
                    raise
                except LiveError as error:
                    code = getattr(error, "code", "injection_unknown")
                    verdict = ("failed" if code in {"input_rejected",
                                                     "unsupported_operation"}
                               else "unknown")
                    valid = False
                    break
                action_receipt={"eventId": action["eventId"],
                                "operationId": command["commandId"],
                                "status": receipt.get("status", "unknown")}
                if locator_artifact is not None:
                    action_receipt["locatorEvidenceDigest"]=(
                        locator_artifact["digest"])
                receipts.append(action_receipt)
                self._active(cancel, deadline)
                if receipt.get("status") != "injected":
                    valid = False;verdict = "unknown";break
                duration = waits.get(action["eventId"], 0) / 1000
                if duration:
                    self._wait(cancel, deadline, duration)
            else:
                anchor = self.wall_clock_ms()
                assertion_values = {}
                observation_cache = {}
                all_covered = True
                for assertion in approved.specification["assertions"]:
                    for observation_id in sorted(
                            predicate_observations(assertion["predicate"])):
                        key = (assertion["id"], observation_id)
                        cache_key=(observation_id,
                                   contracts.digest(assertion["coverage"]))
                        if cache_key not in observation_cache:
                            try:
                                observation_cache[cache_key]=self._observe(
                                    approved, assertion, observation_id, anchor,
                                    session_id, owner, deadline, cancel,
                                    secret_values)
                            except ScenarioError as error:
                                if error.code in {"cancelled", "expired"}:
                                    raise
                                observation_cache[cache_key]=(None,False,None)
                        evidence,covered,artifact=observation_cache[cache_key]
                        assertion_values[key] = evidence
                        all_covered = all_covered and covered
                        observation_summaries.append({
                            "assertionId": assertion["id"],
                            "observationId": observation_id,
                            "coverage": "complete" if covered else "unknown",
                            "digest": (artifact.get("digest")
                                       if artifact is not None else None),
                        })
                by_role = {}
                for assertion in approved.specification["assertions"]:
                    by_role[assertion["role"]] = self._predicate(
                        assertion["predicate"], assertion, anchor,
                        assertion_values)
                defect = by_role.get("defect");expected = by_role.get("expected")
                coverage = "complete" if all_covered else "unknown"
                if defect is None or expected is None or not all_covered:
                    verdict = "unknown"
                else:
                    verdict = "observed"
                self._active(cancel, deadline)
        except ScenarioError as error:
            valid = False
            verdict = "cancelled" if error.code == "cancelled" else "unknown"
        finally:
            for key in list(resolved):
                resolved[key] = None
            resolved.clear();secret_values.clear()
        result = ScenarioRunResult(
            run_id, execution.phase, verdict, approved.project_digest,
            approved.recording_digest, approved.specification_digest,
            approved.qualification_digest, execution.build_id,
            approved.preparation_known, approved.fixture_equivalence,
            tuple(copy.deepcopy(receipts)),
            tuple(copy.deepcopy(observation_summaries)), defect, expected,
            coverage, valid, "unknown", None, started_monotonic_ns, _RESULT_ISSUER)
        return self._retain_result(self._issued_results, result)

    def finalize(self, result, *, cleanup, attempt_recording_digest,
                 fixture_equivalence=None):
        _require(type(result) is ScenarioRunResult and result.trusted,
                 "untrusted_attempt", "Trusted scenario result is required")
        with self._result_lock:
            _require(self._issued_results.get(result.run_id) is result, 'untrusted_attempt')
        _require(cleanup in {"complete", "failed", "unknown"},
                 "scenario_invalid")
        contracts.validate_digest(attempt_recording_digest,
                                  "attempt recording digest")
        if fixture_equivalence is not None:
            _require(tuple(sorted(fixture_equivalence.items()))
                     == result.fixture_equivalence,
                     "fixture_binding", "Fixture equivalence changed")
        verdict = result.verdict
        if cleanup != "complete":
            verdict = "quarantined"
        # Candidate replay can be observed, but G4 never promotes it to a
        # protected repair verdict.
        finalized = replace(result, verdict=verdict, cleanup=cleanup,
                            attempt_recording_digest=attempt_recording_digest)
        with self._result_lock:
            _require(self._issued_results.pop(result.run_id, None) is result, 'untrusted_attempt')
            return self._retain_result(self._final_results, finalized)

    def require_finalized(self, result, execution, *, started_after_ns):
        """G9 accepts only this runner's exact completed result for this dispatch."""
        execution = self.registry.require_execution(execution)
        approved = execution.approved
        with self._result_lock:
            _require(type(result) is ScenarioRunResult and result.trusted
                and self._final_results.get(result.run_id) is result
                and type(started_after_ns) is int and result.started_monotonic_ns >= started_after_ns,
                'untrusted_attempt')
            _require((result.phase, result.project_digest, result.recording_digest,
                result.specification_digest, result.qualification_digest, result.build_id,
                result.fixture_equivalence) == (execution.phase, approved.project_digest,
                approved.recording_digest, approved.specification_digest, approved.qualification_digest,
                execution.build_id, approved.fixture_equivalence), 'scenario_binding')
            contracts.validate_digest(result.attempt_recording_digest)
        return result

    def _active(self, cancel, deadline):
        if cancel.is_set():
            raise ScenarioError("cancelled", "Scenario was cancelled")
        if self.monotonic() >= deadline:
            raise ScenarioError("expired", "Scenario deadline expired")

    def _bounded_call(self, callback, cancel, deadline):
        self._active(cancel, deadline)
        completed = threading.Event()
        holder = {}
        def invoke():
            try:
                holder["value"] = callback()
            except Exception as error:
                holder["error"] = error
            finally:
                completed.set()
        threading.Thread(target=invoke, name="reproof-scenario-read", daemon=True).start()
        while not completed.wait(min(.05, max(0, deadline - self.monotonic()))):
            self._active(cancel, deadline)
        self._active(cancel, deadline)
        if "error" in holder:
            raise holder["error"]
        return holder["value"]

    def _wait(self, cancel, deadline, duration):
        end = min(deadline, self.monotonic() + duration)
        while self.monotonic() < end:
            if cancel.wait(min(0.05, max(0, end - self.monotonic()))):
                raise ScenarioError("cancelled", "Scenario was cancelled")
        self._active(cancel, deadline)

    def _command(self, action, session_id, owner, controller, epoch, sequence,
                 run_id, resolved):
        frame = self.lab.frame(session_id, owner)
        name = action["action"]
        provider_action = {"long-press": "long_press"}.get(name, name)
        payload = {};locator_evidence = None
        if "target" in action:
            evidence = self.lab.resolve_locator(session_id, owner,
                                                action["target"])
            locator_evidence = copy.deepcopy(evidence)
            if name in {"tap", "long-press"}:
                payload.update(x=evidence["x"], y=evidence["y"])
            elif name == "text":
                payload["value"] = resolved[action["parameters"]["variableId"]]
        if "geometry" in action:
            rotation = 0 if frame["orientation"] == "portrait" else 90
            actual = {"width": frame["width"], "height": frame["height"],
                      "rotation": rotation, "version": frame["geometryVersion"]}
            if "frameDigest" in action["geometry"]:
                actual["frameDigest"] = frame.get("objectDigest")
            _require(actual == action["geometry"], "geometry_mismatch",
                     "Scenario geometry changed")
        parameters = action["parameters"]
        if name == "tap" and "target" not in action:
            payload = {"x": parameters["x"], "y": parameters["y"]}
        elif name == "long-press":
            if "target" not in action:
                payload.update(x=parameters["x"], y=parameters["y"])
            payload["durationMs"] = parameters["durationMs"]
        elif name == "swipe":
            payload = {"fromX": parameters["x"], "fromY": parameters["y"],
                       "toX": parameters["x2"], "toY": parameters["y2"],
                       "durationMs": parameters["durationMs"]}
        elif name == "pointer":
            payload = copy.deepcopy(parameters)
        elif name in {"rotate", "launch", "terminate"}:
            payload = copy.deepcopy(parameters)
        elif name in {"back", "home"}:
            payload = {}
        command_id = "operation_" + hashlib.sha256(
            (run_id + "\0" + action["eventId"]).encode("utf-8")).hexdigest()[:40]
        return ({"controllerId": controller, "epoch": epoch,
                 "sequence": sequence, "commandId": command_id,
                 "frameId": frame["id"],
                 "geometryVersion": frame["geometryVersion"],
                 "action": provider_action, "payload": payload}, locator_evidence)

    def _observe(self, approved, assertion, observation_id, anchor,
                 session_id, owner, deadline, cancel, secret_values):
        self._active(cancel, deadline)
        relative = assertion["coverage"]
        bound = contracts.bind_coverage_requirement(relative, anchor_ms=anchor)
        adapter = self.observations.adapter(observation_id, relative["class"])
        request = ObservationRequest(
            observation_id, assertion["id"], approved.original["applicationId"],
            copy.deepcopy(bound), anchor, deadline, session_id, owner, self.lab)
        try:
            evidence = self._bounded_call(lambda: adapter.observe(request), cancel, deadline)
        except ScenarioError:
            raise
        except Exception:
            raise ScenarioError("observation_unknown",
                                "Observation is unavailable") from None
        _require(type(evidence) is ObservationEvidence,
                 "observation_invalid", "Observation evidence is invalid")
        envelope = contracts.validate_observation(evidence.envelope)
        _require(envelope["id"] == observation_id
                 and envelope["applicationId"] == approved.original["applicationId"]
                 and envelope["providerIncarnation"] == adapter.provider_incarnation,
                 "observation_invalid", "Observation binding changed")
        normalized = self._values(evidence.values, envelope)
        absent = evidence.absent_properties
        _require(type(absent) in (list, tuple) and len(absent) <= 128
                 and all(type(key) is str for key in absent)
                 and len(set(absent)) == len(absent)
                 and set(absent) <= set(envelope["properties"])
                 and not set(absent).intersection(normalized),
                 "observation_invalid")
        document = {"envelope": copy.deepcopy(envelope),
                    "values": copy.deepcopy(normalized), "absentProperties": list(absent)}
        _reject_protected_values(document, secret_values)
        encoded = json.dumps(document, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        _require(len(encoded) <= envelope["limits"]["bytes"],
                 "observation_incomplete", "Observation exceeds its capture bound")
        checked = ObservationEvidence(envelope, normalized, tuple(absent))
        artifact = self.lab.record_registered_observation(session_id, owner, document)
        try:
            covered = contracts.observation_result(
                envelope, bound, evaluatedAtMs=self.wall_clock_ms()) == "covered"
        except Exception:
            covered = False
        return checked, covered, artifact

    @staticmethod
    def _values(values, envelope):
        _require(type(values) is dict and len(values) <= 128,
                 "observation_invalid")
        result = {}
        total_points = 0
        for key, value in values.items():
            contracts.validate_id(key, "observation property")
            _require(key in envelope["properties"], "observation_invalid")
            total_points += len(value) if type(value) is list else 1
            _require(total_points <= 100_000, "observation_invalid")
            if type(value) is list:
                _require(len(value) <= 100_000, "observation_invalid")
                points = []
                for point in value:
                    _require(type(point) in (list, tuple) and len(point) == 2
                             and type(point[0]) is int
                             and envelope["intervalMs"]["start"] <= point[0]
                             <= envelope["intervalMs"]["end"],
                             "observation_invalid")
                    points.append((point[0], _scalar(point[1])))
                _require([item[0] for item in points]
                         == sorted(set(item[0] for item in points)),
                         "observation_invalid")
                result[key] = points
            else:
                result[key] = [(envelope["intervalMs"]["end"], _scalar(value))]
            times = [point[0] for point in result[key]]
            if envelope["coverage"] == "sampled":
                _require(times == envelope.get("samplesMs", []) and bool(times),
                         "observation_incomplete", "Property samples are incomplete")
            elif envelope["coverage"] == "continuous":
                _require(bool(times) and times[0] == envelope["intervalMs"]["start"]
                         and times[-1] == envelope["intervalMs"]["end"],
                         "observation_incomplete", "Property interval is incomplete")
        return result

    def _predicate(self, predicate, assertion, anchor, evidence):
        kind = predicate["kind"]
        if kind == "property":
            item = evidence.get((assertion["id"], predicate["observationId"]))
            if item is None:
                return None
            bound = contracts.bind_coverage_requirement(assertion["coverage"],
                                                        anchor_ms=anchor)
            operator = predicate["operator"]
            if predicate["property"] in item.absent_properties:
                return True if operator == "absent" else False if operator == "exists" else None
            points = item.values.get(predicate["property"], [])
            low, high = bound["windowMs"]["start"], bound["windowMs"]["end"]
            if assertion["stabilityMs"]:
                low = high - assertion["stabilityMs"]
            if assertion["coverage"]["class"] == "snapshot":
                # observation_result uses this explicitly approved tolerance
                # for snapshot capture. Evaluate the same window without
                # rewriting the measured timestamp to the oracle anchor.
                low -= bound["maxUncertaintyMs"]
                high += bound["maxUncertaintyMs"]
            if assertion["coverage"]["class"] == "continuous":
                before = [point for point in points if point[0] <= low]
                points = before[-1:] + [point for point in points if low < point[0] <= high]
            else:
                points = [point for point in points if low <= point[0] <= high]
            if not points:
                return None
            if operator == "exists":
                return True
            if operator == "absent":
                return False
            if assertion["coverage"]["class"] == "snapshot":
                points = [points[-1]]
            wanted = predicate.get("value")
            def compare(value):
                if operator == "equals":return type(value) is type(wanted) and value == wanted
                if operator == "not-equals":return not (type(value) is type(wanted) and value == wanted)
                if (type(value) not in (int, float) or isinstance(value, bool)
                        or type(wanted) not in (int, float) or isinstance(wanted, bool)):
                    return None
                return {"lt": value < wanted, "lte": value <= wanted,
                        "gt": value > wanted, "gte": value >= wanted}[operator]
            results = [compare(value) for _, value in points]
            return None if any(value is None for value in results) else all(results)
        children = [self._predicate(child, assertion, anchor, evidence)
                    for child in predicate["children"]]
        if kind == "not":
            return None if children[0] is None else not children[0]
        if kind == "all":
            return False if False in children else (None if None in children else True)
        return True if True in children else (None if None in children else False)


__all__ = [
    "ObservationEvidence", "ObservationRegistry", "ObservationRequest",
    "ScenarioError", "ScenarioRunResult", "ScenarioRunner",
    "StaticVariableResolver", "VariableResolverRegistry",
]
