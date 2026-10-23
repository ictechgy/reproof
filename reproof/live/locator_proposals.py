"""Review-only locator suggestions. Never resolve, dispatch, or grant approval."""
from __future__ import annotations

import copy
import hashlib
import re
import time
import uuid
from contextlib import contextmanager

from .. import contracts
from ..contracts.versions import exact, bounded_int, validate_digest, validate_id, require
from .model import check


FIELDS = ("actions", "waits", "bindings", "fixtures", "assertions")


def _target(value):
    # Deliberately narrower than replay locators: stable identifiers only, no
    # display text, ancestry, arbitrary explanations, raw UI or log payloads.
    exact(value, ("kind", "value"))
    require(value["kind"] in ("accessibility-id", "resource-id"), "Unsupported proposal locator")
    require(type(value["value"]) is str and len(value["value"]) <= 160
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:/-]*", value["value"]) is not None,
            "Proposal requires stable identifier syntax")


def _actor(principal):
    return "author_" + hashlib.sha256(principal.principal_id.encode()).hexdigest()[:24]


class LocatorProposalMixin:
    @contextmanager
    def _proposal_transaction(self):
        # The workflow owns the DB exclusively; serialize against all writers.
        with self._lock:
            self._db.execute("SAVEPOINT locator_proposal")
            try:
                yield
                self._db.execute("RELEASE locator_proposal")
            except BaseException:
                self._db.execute("ROLLBACK TO locator_proposal")
                self._db.execute("RELEASE locator_proposal")
                raise

    def locator_proposals(self, principal, issue_id):
        document = self._issue(principal, issue_id, "issue.read")
        with self._lock:
            proposals = [item for item in self._all("locator-proposal") if item["issueId"] == issue_id]
            decisions = {item["proposalId"]: item for item in self._all("locator-decision")
                         if item["issueId"] == issue_id}
            self._guard(principal, document["projectId"], "issue.read")()
        return {"proposals": [{**item, "decision": decisions.get(item["id"])} for item in proposals]}

    def propose_locator(self, principal, issue_id, body):
        exact(body, ("baseRevision", "baseSpecificationDigest", "eventId", "target"))
        bounded_int(body["baseRevision"], "base revision", 1, 63)
        validate_digest(body["baseSpecificationDigest"])
        validate_id(body["eventId"])
        _target(body["target"])
        document = self._issue(principal, issue_id, "specification.maintain")
        with self._proposal_transaction():
            view = self.get(principal, issue_id)
            spec = view["specification"]
            check(spec is not None and spec["revision"] == body["baseRevision"]
                  and contracts.digest(spec) == body["baseSpecificationDigest"],
                  "specification_changed", "The specification revision changed", 409)
            actions = [item for item in spec["actions"] if item["eventId"] == body["eventId"]]
            check(len(actions) == 1 and "target" in actions[0],
                  "locator_proposal_invalid", "A single existing locator action is required", 400)
            _target(actions[0]["target"])
            check(actions[0]["target"] != body["target"]
                  and actions[0]["target"]["kind"] == body["target"]["kind"],
                  "locator_proposal_invalid", "The locator must change without changing its kind", 400)
            check(len([p for p in self._all("locator-proposal") if p["issueId"] == issue_id]) < 64,
                  "locator_proposal_limit", "This issue has reached its proposal limit", 409)
            proposal = {"schemaVersion": 1, "id": "locator_" + uuid.uuid4().hex,
                "issueId": issue_id, "projectId": document["projectId"],
                "projectDigest": self._runtime(document["projectId"]).registration.project_digest,
                "originalRecordingDigest": spec["originalRecordingDigest"],
                **copy.deepcopy(body), "previousTarget": copy.deepcopy(actions[0]["target"]),
                "proposedBy": _actor(principal), "proposedAtMs": int(time.time() * 1000),
                "mode": "review-only"}
            self._guard(principal, document["projectId"], "specification.maintain")()
            self._put("locator-proposal", proposal["id"], proposal, immutable=True)
        return {"proposal": proposal}

    def decide_locator(self, principal, issue_id, body):
        exact(body, ("proposalId", "decision"))
        validate_id(body["proposalId"])
        require(body["decision"] in ("apply", "reject"), "Invalid locator decision")
        document = self._issue(principal, issue_id, "specification.maintain")
        with self._proposal_transaction():
            proposal = self._get("locator-proposal", body["proposalId"])
            check(proposal["issueId"] == issue_id and proposal["projectId"] == document["projectId"],
                  "locator_proposal_invalid", "Proposal belongs to another issue", 404)
            check(not self._db.execute("SELECT 1 FROM documents WHERE kind='locator-decision' AND id=?",
                                      (proposal["id"],)).fetchone(),
                  "locator_proposal_decided", "Proposal already has a decision", 409)
            decision = {"proposalId": proposal["id"], "issueId": issue_id,
                "decision": body["decision"], "reviewedBy": _actor(principal),
                "reviewedAtMs": int(time.time() * 1000)}
            if body["decision"] == "apply":
                view = self.get(principal, issue_id)
                spec = view["specification"]
                check(spec is not None and contracts.digest(spec) == proposal["baseSpecificationDigest"]
                      and spec["revision"] == proposal["baseRevision"]
                      and spec["originalRecordingDigest"] == proposal["originalRecordingDigest"]
                      and self._runtime(document["projectId"]).registration.project_digest == proposal["projectDigest"],
                      "specification_changed", "Proposal no longer matches the trusted specification", 409)
                updated = {key: copy.deepcopy(spec[key]) for key in FIELDS}
                action = next(item for item in updated["actions"] if item["eventId"] == proposal["eventId"])
                action["target"] = copy.deepcopy(proposal["target"])
                result = self.save_specification(principal, issue_id,
                                                {"baseRevision": spec["revision"], **updated})
                decision.update(resultingRevision=result["specification"]["revision"],
                                resultingSpecificationDigest=result["specificationDigest"])
            self._guard(principal, document["projectId"], "specification.maintain")()
            self._put("locator-decision", proposal["id"], decision, immutable=True)
        return {"decision": decision}
