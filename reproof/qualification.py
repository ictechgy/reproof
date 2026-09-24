"""Approved G4 specifications and fixed-budget original qualification."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from . import contracts
from .contracts import ContractError
from .fixtures import TrustedFixturePlan
from .live.recording_session import TrustedProjectRegistration


_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


class QualificationError(RuntimeError):
    def __init__(self, code, message="Qualification is invalid"):
        super().__init__(message)
        self.code = code


def _require(condition, code="qualification_invalid", message="Qualification is invalid"):
    if not condition:
        raise QualificationError(code, message)


def _identifier(value):
    _require(type(value) is str and _ID.fullmatch(value) is not None)
    return value


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeError):
        raise QualificationError("qualification_invalid") from None


@dataclass(frozen=True, slots=True)
class ApprovedSpecification:
    project: dict
    original: dict
    specification: dict
    qualification: dict
    runtime_policy: dict
    project_digest: str
    recording_digest: str
    specification_digest: str
    qualification_digest: str
    runtime_policy_digest: str
    preparation_known: bool
    fixture_equivalence: tuple[tuple[str, str], ...]
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ApprovedExecution:
    approved: ApprovedSpecification
    phase: str
    build_id: str
    build_digest: str
    _issuer: object = field(repr=False, compare=False)
    candidate_binding: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CandidateBuildBinding:
    qualification_digest: str
    project_digest: str
    build_id: str
    _build_json: str = field(repr=False)
    _approved: ApprovedSpecification = field(repr=False, compare=False)
    _registry: object = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


def require_candidate_binding(binding, *, project_digest, application_id, build_id):
    """Resolve an in-process candidate capability, never a build from JSON."""
    _require(type(binding) is CandidateBuildBinding
        and type(binding._registry) is ScenarioRegistry, 'candidate_unauthorized')
    return binding._registry.require_candidate_build(binding, project_digest=project_digest,
        application_id=application_id, build_id=build_id)


@dataclass(frozen=True, slots=True)
class QualificationCampaign:
    campaign_id: str
    qualification_digest: str
    original_attempts: int
    _issuer: object = field(repr=False, compare=False)


class ScenarioRegistry:
    """Persist inert approved bytes and issue process-local execution authority."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._issuer = object()
        self._lock = threading.RLock()
        self._candidate_builds = {}
        self._candidate_used = set()
        self._db = sqlite3.connect(self.root / "specifications.sqlite3",
                                   isolation_level=None,
                                   check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS specifications(
                specification_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                specification_digest TEXT NOT NULL,
                qualification_digest TEXT NOT NULL,
                project_digest TEXT NOT NULL,
                recording_digest TEXT NOT NULL,
                specification_json TEXT NOT NULL,
                qualification_json TEXT NOT NULL,
                PRIMARY KEY(specification_id,revision)
            );
            """)
        row = self._db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()
        if row is None:
            self._db.execute("INSERT INTO metadata VALUES('version',1)")
        else:
            _require(row[0] == 1, "qualification_store",
                     "Unsupported specification store")

    def register(self, registration, recording, specification, qualification,
                 runtime_policy, *, fixture_plans=()):
        _require(type(registration) is TrustedProjectRegistration,
                 "trusted_registration", "Trusted project registration is required")
        project = registration.project
        original = recording.get("original") if type(recording) is dict else None
        recording_digest = (recording.get("recordingDigest")
                            if type(recording) is dict else None)
        _require(type(original) is dict and recording_digest is not None
                 and contracts.digest(original) == recording_digest,
                 "recording_changed", "Frozen original is unavailable")
        try:
            specification = contracts.validate_specification(specification)
            qualification = contracts.validate_qualification(qualification)
            runtime_policy = contracts.validate_execution_policy(runtime_policy)
            contracts.validate_qualification_bindings(
                qualification, project, original, specification)
        except (ContractError, TypeError, ValueError):
            raise QualificationError("qualification_invalid") from None
        _require(registration.project_digest == contracts.digest(project)
                 == qualification["projectDigest"]
                 and recording_digest == qualification["recordingDigest"]
                 and contracts.digest(specification)
                 == qualification["specificationDigest"]
                 and contracts.digest(runtime_policy)
                 == qualification["runtimePolicyDigest"],
                 "qualification_binding", "Qualification bindings changed")
        _require(type(fixture_plans) in (list, tuple)
                 and len(fixture_plans) <= 128, "qualification_invalid")
        plans = {}
        for plan in fixture_plans:
            _require(type(plan) is TrustedFixturePlan
                     and plan.project_id == project["id"]
                     and plan.project_revision == project["revision"]
                     and plan.project_digest == registration.project_digest
                     and plan.application_id == original["applicationId"],
                     "fixture_binding", "Fixture binding changed")
            plans[plan.fixture_id] = plan
        rules = {item["fixtureId"]: item["equivalenceDigest"]
                 for item in qualification["fixtureRules"]}
        _require(set(plans) == set(specification["fixtures"]) == set(rules)
                 and all(plans[key].equivalence_digest == rules[key]
                         for key in rules), "fixture_binding",
                 "Fixture equivalence changed")
        completed = {}
        for item in original["preparation"]:
            completed.setdefault(item["recipeId"], []).append(item)
        required_preparation = {}
        for fixture_id in specification["fixtures"]:
            required_preparation[fixture_id] = "prepare"
            required_preparation.update((recipe_id, "check")
                                        for recipe_id in plans[fixture_id].check_recipe_ids)
        preparation_known = (bool(required_preparation) and all(
            len(completed.get(key, [])) == 1
            and completed[key][0]["status"] == "complete"
            and completed[key][0]["operation"] == operation
            for key, operation in required_preparation.items())
                             and not any(item["kind"] == "preparation_unknown"
                                         for item in original["unknowns"]))
        specification_digest = contracts.digest(specification)
        qualification_digest = contracts.digest(qualification)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT * FROM specifications WHERE specification_id=? AND revision=?",
                    (specification["id"], specification["revision"])).fetchone()
                values = (specification_digest, qualification_digest,
                          registration.project_digest, recording_digest,
                          _json(specification), _json(qualification))
                if row is None:
                    self._db.execute(
                        "INSERT INTO specifications VALUES(?,?,?,?,?,?,?,?)",
                        (specification["id"], specification["revision"], *values))
                else:
                    _require(tuple(row[2:]) == values,
                             "specification_changed",
                             "Approved specification revision changed")
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return ApprovedSpecification(
            copy.deepcopy(project), copy.deepcopy(original),
            copy.deepcopy(specification), copy.deepcopy(qualification),
            copy.deepcopy(runtime_policy), registration.project_digest,
            recording_digest, specification_digest, qualification_digest,
            contracts.digest(runtime_policy), preparation_known,
            tuple(sorted(rules.items())), self._issuer)

    def require(self, approved):
        _require(type(approved) is ApprovedSpecification
                 and approved._issuer is self._issuer,
                 "trusted_specification",
                 "Trusted approved specification is required")
        # Copy before validating so a caller cannot change the bytes between
        # the approval check and a later action in the same execution.
        try:
            snapshot = replace(
                approved, project=copy.deepcopy(approved.project),
                original=copy.deepcopy(approved.original),
                specification=copy.deepcopy(approved.specification),
                qualification=copy.deepcopy(approved.qualification),
                runtime_policy=copy.deepcopy(approved.runtime_policy))
            actual = tuple(contracts.digest(value) for value in (
                snapshot.project, snapshot.original, snapshot.specification,
                snapshot.qualification, snapshot.runtime_policy))
            expected = (snapshot.project_digest, snapshot.recording_digest,
                        snapshot.specification_digest, snapshot.qualification_digest,
                        snapshot.runtime_policy_digest)
            _require(actual == expected, "specification_changed", "Approved bytes changed")
            contracts.validate_qualification_bindings(
                snapshot.qualification, snapshot.project, snapshot.original, snapshot.specification)
            _require(snapshot.qualification["runtimePolicyDigest"] == snapshot.runtime_policy_digest,
                     "specification_changed", "Approved policy changed")
        except (ContractError, TypeError, ValueError, KeyError, RecursionError):
            raise QualificationError("specification_changed", "Approved bytes changed") from None
        # Cancellation watchers and the replay interpreter share this SQLite
        # connection. Keep execute/fetch under the same registry lock as writes;
        # concurrent use can otherwise observe an empty cached statement result.
        with self._lock:
            row = self._db.execute(
                "SELECT specification_digest,qualification_digest,project_digest,recording_digest "
                "FROM specifications WHERE specification_id=? AND revision=?",
                (snapshot.specification["id"], snapshot.specification["revision"])).fetchone()
        _require(row is not None and tuple(row) == (
            approved.specification_digest, approved.qualification_digest,
            approved.project_digest, approved.recording_digest),
            "specification_changed", "Approved specification changed")
        return snapshot

    def original_execution(self, approved):
        approved = self.require(approved)
        build = next(item for item in approved.project["builds"]
                     if item["id"] == approved.qualification["originalBuildId"])
        return ApprovedExecution(approved, "original", build["id"],
                                 build["artifactDigest"], self._issuer)

    def candidate_execution(self, approved, candidate_wire, approval):
        approved = self.require(approved)
        try:
            candidate = contracts.check_candidate_substitution(candidate_wire, approval)
        except (ContractError, TypeError, ValueError):
            raise QualificationError("candidate_unauthorized",
                                     "Trusted candidate approval is required") from None
        _require(candidate["qualificationDigest"] == approved.qualification_digest
                 and candidate["originalRecordingDigest"] == approved.recording_digest
                 and candidate["specificationDigest"] == approved.specification_digest,
                 "candidate_unauthorized", "Candidate binding changed")
        build = next((item for item in approved.project["builds"]
                      if item["id"] == candidate["sourceBuildId"]), None)
        _require(build is not None
                 and build["applicationId"] == approved.original["applicationId"]
                 and contracts.digest(build) == candidate["candidateBuildDigest"],
                 "candidate_unauthorized", "Candidate build is not registered")
        return ApprovedExecution(approved, "candidate", build["id"],
                                 build["artifactDigest"], self._issuer)

    def require_execution(self, execution):
        _require(type(execution) is ApprovedExecution
                 and execution._issuer is self._issuer,
                 "trusted_specification", "Trusted execution is required")
        approved = self.require(execution.approved)
        if execution.candidate_binding is not None:
            _require(execution.phase == 'candidate', 'candidate_unauthorized')
            build = self.require_candidate_build(execution.candidate_binding,
                project_digest=approved.project_digest, application_id=approved.original['applicationId'],
                build_id=execution.build_id)
            _require(execution.candidate_binding.qualification_digest == approved.qualification_digest,
                     'candidate_unauthorized')
        else:
            build = next((item for item in approved.project["builds"]
                          if item["id"] == execution.build_id), None)
        _require(execution.phase in {"original", "candidate"} and build is not None
                 and build["applicationId"] == approved.original["applicationId"]
                 and execution.build_digest == build["artifactDigest"]
                 and (execution.phase != "original"
                      or execution.build_id == approved.qualification["originalBuildId"]),
                 "trusted_specification", "Execution identity changed")
        return replace(execution, approved=approved)

    def authorize_candidate_build(self, approved, build_identity, approval):
        """Trusted supervisor substitution after build/signing/validation gates.

        This local API has no RPC/import equivalent. It binds a new measured
        build to the existing project/specification without rewriting either.
        General repair must separately authorize its mobile execution route.
        """
        approved = self.require(approved)
        try:
            build = contracts.validate_build_identity(build_identity)
            candidate = {'schemaVersion': 1, 'qualificationDigest': approved.qualification_digest,
                'sourceBuildId': build['id'], 'candidateBuildDigest': contracts.digest(build),
                'originalRecordingDigest': approved.recording_digest,
                'specificationDigest': approved.specification_digest}
            contracts.check_candidate_substitution(candidate, approval)
        except (contracts.ContractError, TypeError, ValueError):
            raise QualificationError('candidate_unauthorized') from None
        _require(build['applicationId'] == approved.original['applicationId']
            and build['provenance'] == 'trusted-build' and 'sourceDigest' in build
            and build['id'] not in {item['id'] for item in approved.project['builds']}, 'candidate_unauthorized')
        key = (approved.qualification_digest, build['id'])
        raw = _json(build)
        with self._lock:
            binding = self._candidate_builds.get(key)
            if binding is None:
                _require(key not in self._candidate_used and len(self._candidate_used) < 4096, 'candidate_unauthorized')
                binding = CandidateBuildBinding(approved.qualification_digest, approved.project_digest,
                    build['id'], raw, approved, self, self._issuer)
                self._candidate_used.add(key)
                self._candidate_builds[key] = binding
            _require(binding._build_json == raw, 'candidate_unauthorized')
        return ApprovedExecution(approved, 'candidate', build['id'], build['artifactDigest'], self._issuer, binding)

    def require_candidate_build(self, binding, *, project_digest, application_id, build_id):
        with self._lock:
            _require(type(binding) is CandidateBuildBinding and binding._issuer is self._issuer
                and binding._registry is self and binding.project_digest == project_digest
                and binding.build_id == build_id
                and self._candidate_builds.get((binding.qualification_digest, build_id)) is binding,
                'candidate_unauthorized')
            approved = self.require(binding._approved)
            build = json.loads(binding._build_json)
            _require(approved.qualification_digest == binding.qualification_digest
                and approved.original['applicationId'] == application_id
                and build['applicationId'] == application_id, 'candidate_unauthorized')
            return build

    def revoke_candidate_build(self, binding):
        with self._lock:
            _require(type(binding) is CandidateBuildBinding and binding._issuer is self._issuer,
                     'candidate_unauthorized')
            self._candidate_builds.pop((binding.qualification_digest, binding.build_id), None)

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close();self._db = None


class QualificationEngine:
    """Retain every original attempt in its frozen slot; never replace runs."""

    def __init__(self, root: str | Path, registry: ScenarioRegistry):
        _require(type(registry) is ScenarioRegistry, "qualification_invalid")
        self.registry = registry
        self.root = Path(root);self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._issuer = object();self._lock = threading.RLock()
        self._db = None
        self._store_lock = (self.root / "writer.lock").open("a+b")
        try:
            fcntl.flock(self._store_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._store_lock.close();self._store_lock = None
            raise QualificationError("qualification_busy", "Qualification store is active") from None
        try:
            self._initialize()
            self._recover_unfinished()
        except Exception:
            self.close()
            raise

    def _initialize(self):
        self._db = sqlite3.connect(self.root / "qualification.sqlite3",
                                   isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(attempts)")}
        _require(not columns or columns == {
            "campaign_id", "attempt", "state", "started_monotonic_ns", "run_id", "result_json"},
            "qualification_store", "Unsupported qualification store")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaigns(
                campaign_id TEXT PRIMARY KEY,
                qualification_digest TEXT NOT NULL,
                recording_digest TEXT NOT NULL,
                specification_digest TEXT NOT NULL,
                project_digest TEXT NOT NULL,
                original_build_id TEXT NOT NULL,
                attempt_budget INTEGER NOT NULL,
                state TEXT NOT NULL,
                verdict TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                ended_at_ms INTEGER
            );
            CREATE TABLE IF NOT EXISTS attempts(
                campaign_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                state TEXT NOT NULL,
                started_monotonic_ns INTEGER NOT NULL,
                run_id TEXT NOT NULL UNIQUE,
                result_json TEXT NOT NULL,
                PRIMARY KEY(campaign_id,attempt)
            );
            """)

    def _recover_unfinished(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for row in self._db.execute("SELECT * FROM attempts WHERE state='running'").fetchall():
                public = json.loads(row["result_json"])
                public["failureCode"] = "process_interrupted"
                self._db.execute(
                    "UPDATE attempts SET state='complete',result_json=? WHERE campaign_id=? AND attempt=?",
                    (_json(public), row["campaign_id"], row["attempt"]))
                self._db.execute(
                    "UPDATE campaigns SET state='complete',verdict='quarantined',ended_at_ms=? "
                    "WHERE campaign_id=?", (int(time.time() * 1000), row["campaign_id"]))
            self._db.commit()
        except Exception:
            self._db.rollback();raise

    def begin_original(self, approved, *, campaign_id=None):
        approved = self.registry.require(approved)
        campaign_id = _identifier(campaign_id or ("campaign_" + uuid.uuid4().hex))
        budget = approved.qualification["attemptBudget"]["original"]
        now = int(time.time() * 1000)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
                if existing is None:
                    self._db.execute(
                        "INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                        (campaign_id, approved.qualification_digest,
                         approved.recording_digest, approved.specification_digest,
                         approved.project_digest,
                         approved.qualification["originalBuildId"], budget,
                         "running", "unknown", now))
                else:
                    _require(tuple(existing[key] for key in (
                        "qualification_digest", "recording_digest",
                        "specification_digest", "project_digest",
                        "original_build_id", "attempt_budget")) == (
                        approved.qualification_digest, approved.recording_digest,
                        approved.specification_digest, approved.project_digest,
                        approved.qualification["originalBuildId"], budget),
                        "campaign_conflict", "Qualification campaign changed")
                self._db.commit()
            except Exception:
                self._db.rollback();raise
        return QualificationCampaign(campaign_id, approved.qualification_digest,
                                     budget, self._issuer)

    def _campaign(self, campaign):
        _require(type(campaign) is QualificationCampaign
                 and campaign._issuer is self._issuer,
                 "trusted_campaign", "Trusted qualification campaign is required")
        with self._lock:
            row = self._db.execute("SELECT * FROM campaigns WHERE campaign_id=?",
                                   (campaign.campaign_id,)).fetchone()
        _require(row is not None
                 and row["qualification_digest"] == campaign.qualification_digest
                 and row["attempt_budget"] == campaign.original_attempts,
                 "trusted_campaign", "Qualification campaign is unavailable")
        return row

    def record_attempt(self, campaign, result):
        from .scenario_runner import ScenarioRunResult
        row = self._campaign(campaign)
        _require(row["state"] == "running", "budget_exhausted",
                 "Qualification attempt budget is closed")
        _require(type(result) is ScenarioRunResult and result.trusted,
                 "untrusted_attempt", "Trusted scenario result is required")
        public = result.public()
        _require(public["phase"] == "original"
                 and public["qualificationDigest"] == row["qualification_digest"]
                 and public["recordingDigest"] == row["recording_digest"]
                 and public["specificationDigest"] == row["specification_digest"]
                 and public["projectDigest"] == row["project_digest"]
                 and public["buildId"] == row["original_build_id"],
                 "attempt_binding", "Scenario attempt binding changed")
        return self._persist_attempt(campaign, public, result.started_monotonic_ns)

    def _persist_attempt(self, campaign, public, started_monotonic_ns=None):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT * FROM campaigns WHERE campaign_id=?",
                    (campaign.campaign_id,)).fetchone()
                _require(row is not None and row["state"] == "running",
                         "budget_exhausted",
                         "Qualification attempt budget is closed")
                pending = self._db.execute(
                    "SELECT * FROM attempts WHERE campaign_id=? AND state='running'",
                    (campaign.campaign_id,)).fetchone()
                _require(pending is not None, "attempt_not_admitted", "Attempt was not admitted")
                _require(started_monotonic_ns is None or
                         started_monotonic_ns >= pending["started_monotonic_ns"],
                         "attempt_binding", "Run predates the admitted attempt")
                prior = self._db.execute("SELECT campaign_id,attempt FROM attempts WHERE run_id=?",
                                         (public["runId"],)).fetchone()
                _require(prior is None or (prior[0], prior[1]) ==
                         (campaign.campaign_id, pending["attempt"]),
                         "attempt_reused", "Run already belongs to an attempt")
                attempt = pending["attempt"]
                stored = dict(public, attempt=attempt)
                self._db.execute(
                    "UPDATE attempts SET state='complete',run_id=?,result_json=? "
                    "WHERE campaign_id=? AND attempt=?",
                    (public["runId"], _json(stored), campaign.campaign_id, attempt))
                terminal = (attempt == row["attempt_budget"]
                            or public["verdict"] == "cancelled"
                            or public["cleanup"] != "complete")
                if terminal:
                    attempts = [json.loads(item[0]) for item in self._db.execute(
                        "SELECT result_json FROM attempts WHERE campaign_id=? ORDER BY attempt",
                        (campaign.campaign_id,))]
                    verdict = self._verdict(attempts, row["attempt_budget"])
                    self._db.execute(
                        "UPDATE campaigns SET state='complete',verdict=?,ended_at_ms=? "
                        "WHERE campaign_id=?",
                        (verdict, int(time.time() * 1000), campaign.campaign_id))
                self._db.commit()
            except Exception:
                self._db.rollback();raise
        return self.get(campaign)

    @staticmethod
    def _unknown_attempt(row, approved):
        return {
            "runId": "run_" + uuid.uuid4().hex,
            "phase": "original", "verdict": "unknown",
            "projectDigest": row["project_digest"],
            "injection": "unknown", "observation": "unknown",
            "recordingDigest": row["recording_digest"],
            "specificationDigest": row["specification_digest"],
            "qualificationDigest": row["qualification_digest"],
            "buildId": row["original_build_id"],
            "preparationKnown": approved.preparation_known,
            "fixtureEquivalence": dict(approved.fixture_equivalence),
            "receipts": [], "observations": [],
            "defect": None, "expected": None, "coverage": "unknown",
            "valid": False, "cleanup": "unknown",
            "attemptRecordingDigest": None,
            "failureCode": "executor_incomplete",
        }

    def begin_attempt(self, campaign, approved):
        approved = self.registry.require(approved)
        _require(approved.qualification_digest == campaign.qualification_digest,
                 "attempt_binding", "Qualification binding changed")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._campaign(campaign)
                _require(row["state"] == "running", "budget_exhausted",
                         "Qualification attempt budget is closed")
                _require(self._db.execute(
                    "SELECT 1 FROM attempts WHERE campaign_id=? AND state='running'",
                    (campaign.campaign_id,)).fetchone() is None,
                    "attempt_active", "An attempt is already active")
                count = self._db.execute(
                    "SELECT COUNT(*) FROM attempts WHERE campaign_id=?",
                    (campaign.campaign_id,)).fetchone()[0]
                _require(count < row["attempt_budget"], "budget_exhausted")
                number = count + 1
                public = dict(self._unknown_attempt(row, approved), attempt=number)
                self._db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?)",
                                 (campaign.campaign_id, number, "running", time.monotonic_ns(),
                                  public["runId"], _json(public)))
                self._db.commit()
            except Exception:
                self._db.rollback();raise
        return number

    def _record_executor_failure(self, campaign):
        with self._lock:
            pending = self._db.execute(
                "SELECT result_json FROM attempts WHERE campaign_id=? AND state='running'",
                (campaign.campaign_id,)).fetchone()
            _require(pending is not None, "attempt_not_admitted")
            public = json.loads(pending[0])
            public["failureCode"] = "executor_failed"
        return self._persist_attempt(campaign, public)

    def run_attempt(self, campaign, approved, execute_attempt):
        _require(callable(execute_attempt), "qualification_invalid")
        number = self.begin_attempt(campaign, approved)
        try:
            result = execute_attempt(number)
            return self.record_attempt(campaign, result)
        except Exception:
            return self._record_executor_failure(campaign)

    @staticmethod
    def _verdict(attempts, budget):
        if any(item["cleanup"] in {"failed", "unknown"} for item in attempts):
            return "quarantined"
        if any(item["verdict"] == "cancelled" for item in attempts):
            return "cancelled"
        if len(attempts) != budget:
            return "unknown"
        if any(item["coverage"] != "complete"
               or item["defect"] is None or item["expected"] is None
               or not item["preparationKnown"] for item in attempts):
            return "unknown"
        if all(item["valid"] is True
               and contracts.classify_predicates(
                   item["defect"], item["expected"], phase="original") == "match"
               for item in attempts):
            return "reproduced"
        return "failed"

    def get(self, campaign):
        with self._lock:
            row = self._campaign(campaign)
            attempts = [json.loads(item[0]) for item in self._db.execute(
                "SELECT result_json FROM attempts WHERE campaign_id=? ORDER BY attempt",
                (campaign.campaign_id,))]
        return {"campaignId": row["campaign_id"], "state": row["state"],
                "verdict": row["verdict"], "attemptBudget": row["attempt_budget"],
                "attempts": attempts}

    def lookup(self, campaign_id):
        """Read an existing campaign without issuing or recreating authority."""
        campaign_id=_identifier(campaign_id)
        with self._lock:
            row=self._db.execute('SELECT * FROM campaigns WHERE campaign_id=?',(campaign_id,)).fetchone()
            _require(row is not None,'campaign_unavailable','Qualification campaign is unavailable')
            attempts=[json.loads(item[0]) for item in self._db.execute(
                'SELECT result_json FROM attempts WHERE campaign_id=? ORDER BY attempt',(campaign_id,))]
            return {'campaignId':row['campaign_id'],'state':row['state'],
                    'verdict':row['verdict'],'attemptBudget':row['attempt_budget'],'attempts':attempts}

    def require_reproduced(self, approved, campaign_id):
        """Read only this engine's completed, exactly bound original campaign.

        Uploaded JSON cannot supply a baseline. Repair also remeasures the
        registered original source and artifact before using this evidence.
        """
        approved = self.registry.require(approved)
        campaign_id = _identifier(campaign_id)
        with self._lock:
            row = self._db.execute('SELECT * FROM campaigns WHERE campaign_id=?',
                                   (campaign_id,)).fetchone()
            budget = approved.qualification['attemptBudget']['original']
            _require(row is not None and tuple(row[key] for key in (
                'qualification_digest', 'recording_digest', 'specification_digest',
                'project_digest', 'original_build_id', 'attempt_budget', 'state', 'verdict')) == (
                approved.qualification_digest, approved.recording_digest, approved.specification_digest,
                approved.project_digest, approved.qualification['originalBuildId'], budget,
                'complete', 'reproduced'), 'baseline_unqualified', 'Original reproduction is not qualified')
            result = self.lookup(campaign_id)
            attempts = result['attempts']
            expected = {'phase': 'original', 'qualificationDigest': approved.qualification_digest,
                'recordingDigest': approved.recording_digest, 'specificationDigest': approved.specification_digest,
                'projectDigest': approved.project_digest, 'buildId': approved.qualification['originalBuildId'],
                'fixtureEquivalence': dict(approved.fixture_equivalence), 'cleanup': 'complete'}
            _require(len(attempts) == budget
                and len({item.get('runId') for item in attempts}) == budget
                and all(all(item.get(key) == value for key, value in expected.items()) for item in attempts)
                and self._verdict(attempts, budget) == 'reproduced',
                'baseline_unqualified', 'Original reproduction evidence is incomplete')
            return result

    def run_original(self, approved, execute_attempt, *, campaign_id=None):
        _require(callable(execute_attempt), "qualification_invalid",
                 "Trusted attempt executor is required")
        campaign = self.begin_original(approved, campaign_id=campaign_id)
        for _ in range(campaign.original_attempts):
            current = self.get(campaign)
            if current["state"] != "running":
                break
            self.run_attempt(campaign, approved, execute_attempt)
        return self.get(campaign)

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close();self._db = None
            if self._store_lock is not None:
                self._store_lock.close();self._store_lock = None


__all__ = [
    "ApprovedExecution", "ApprovedSpecification", "QualificationCampaign",
    "QualificationEngine", "QualificationError", "ScenarioRegistry",
]
