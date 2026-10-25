"""Project-scoped ticket identifiers linked to existing immutable issue packages.

These references are not ticket intake, provider authentication, evidence,
local approval, or execution authority. No ticket prose is accepted or stored.
"""
from __future__ import annotations

import copy
import hashlib
import re
import time
from contextlib import contextmanager

from .. import contracts
from ..contracts.versions import bounded_int, exact, require, validate_digest, validate_id
from ..issue_package import PackageError
from .model import check


MAX_TICKET_REFERENCES = 64
_UNAVAILABLE = {"package_expired", "package_removed", "package_unavailable", "package_not_found"}


class TicketReferenceMixin:
    @contextmanager
    def _ticket_transaction(self):
        # Match the workflow's existing documents/SAVEPOINT ownership. Keep the
        # package lock through COMMIT so local tombstones cannot race the write.
        # All paths acquire workflow before package, never the reverse.
        with self._lock, self.packages._operation():
            self._db.execute("SAVEPOINT ticket_reference")
            try:
                yield
                self._db.execute("RELEASE ticket_reference")
            except BaseException:
                self._db.execute("ROLLBACK TO ticket_reference")
                self._db.execute("RELEASE ticket_reference")
                raise

    def _ticket_guard(self, principal, issue_id):
        document = self._issue(principal, issue_id, "specification.maintain")
        project_id = document["projectId"]
        project_digest = self._runtime(project_id).registration.project_digest
        guards = [self._guard(principal, project_id, capability)
                  for capability in ("specification.maintain", "export.read")]

        def guard():
            for capability, authorize in zip(("specification.maintain", "export.read"), guards):
                authorize()
                binding = self.access.authorize_resource(principal, "issue", issue_id, capability)
                check(binding.project_id == project_id and binding.project_digest == project_digest,
                      "ticket_reference_changed", "Issue project binding changed", 409)
            check(self._get("issue", issue_id)["projectId"] == project_id,
                  "ticket_reference_changed", "Issue project binding changed", 409)
            return True

        guard()
        return document, guard

    def _ticket_snapshot(self, principal, issue_id):
        view = self.get(principal, issue_id)
        spec, recording, issue = view["specification"], view["recording"], view["issue"]
        check(spec is not None, "specification_required", "Save a specification before linking a ticket", 409)
        check(recording is not None and recording.get("original") is not None,
              "recording_incomplete", "Original evidence is required for a ticket reference", 409)
        original = recording["original"]
        runtime = self._runtime(issue["projectId"])
        project = runtime.registration.project
        build = next((item for item in project["builds"] if item["id"] == issue["buildId"]), None)
        check(original["projectId"] == project["id"]
              and original["projectRevision"] == project["revision"]
              and original["applicationId"] == issue["applicationId"]
              and original["buildId"] == issue["buildId"]
              and contracts.digest(original) == recording["recordingDigest"] == spec["originalRecordingDigest"]
              and build is not None and build["applicationId"] == original["applicationId"]
              and build.get("sourceDigest") is not None,
              "ticket_reference_mismatch", "Issue evidence does not match the registered project and source", 409)
        return {"projectId": project["id"], "projectDigest": runtime.registration.project_digest,
                "applicationId": original["applicationId"], "buildId": original["buildId"],
                "sourceDigest": build["sourceDigest"], "artifactDigest": build["artifactDigest"],
                "recordingId": original["recordingId"], "originalRecordingDigest": recording["recordingDigest"],
                "baseRevision": spec["revision"], "baseSpecificationDigest": contracts.digest(spec)}

    def link_ticket_reference(self, principal, issue_id, body):
        exact(body, ("ticketId", "packageId", "baseRevision", "baseSpecificationDigest"))
        require(type(body["ticketId"]) is str
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", body["ticketId"]) is not None,
                "Ticket reference must be a bounded identifier, not text or a URL")
        validate_id(body["packageId"])
        bounded_int(body["baseRevision"], "base revision", 1, 64)
        validate_digest(body["baseSpecificationDigest"])
        request = copy.deepcopy(body)
        document, guard = self._ticket_guard(principal, issue_id)
        with self._ticket_transaction():
            snapshot = self._ticket_snapshot(principal, issue_id)
            check(all(snapshot[key] == request[key] for key in ("baseRevision", "baseSpecificationDigest")),
                  "specification_changed", "The specification revision changed", 409)
            packaged = self.packages.get(request["packageId"], document["projectId"], authorize=guard)
            package, recording, spec = packaged["package"], packaged["recording"], packaged["specification"]
            check(package["projectId"] == snapshot["projectId"]
                  and package["projectDigest"] == snapshot["projectDigest"]
                  and recording["recordingDigest"] == snapshot["originalRecordingDigest"]
                  and contracts.digest(recording["original"]) == snapshot["originalRecordingDigest"]
                  and contracts.digest(spec) == snapshot["baseSpecificationDigest"]
                  and spec["revision"] == snapshot["baseRevision"],
                  "ticket_reference_mismatch", "Package does not match this exact issue revision", 409)
            identity = {"issueId": issue_id, "ticketId": request["ticketId"], "packageId": request["packageId"]}
            identifier = "ticketref_" + contracts.digest(identity)[:48]
            immutable = {**identity, **snapshot, "capsuleDigest": package["archiveDigest"]}
            existing = next((item for item in self._all("ticket-reference") if item["id"] == identifier), None)
            if existing is not None:
                check(all(existing[key] == value for key, value in immutable.items()),
                      "ticket_reference_changed", "A saved ticket reference cannot be retargeted", 409)
                reference = existing
            else:
                check(sum(item["issueId"] == issue_id for item in self._all("ticket-reference"))
                      < MAX_TICKET_REFERENCES, "ticket_reference_limit", "This issue has reached its ticket reference limit", 409)
                reference = {"schemaVersion": 1, "id": identifier, **immutable, "mode": "reference-only",
                             "linkedBy": "author_" + hashlib.sha256(principal.principal_id.encode()).hexdigest()[:24],
                             "linkedAtMs": int(time.time() * 1000)}
                self._put("ticket-reference", identifier, reference, immutable=True)
            # A failure here rolls back the reference, including post-write
            # revocation, expiry, changed issue revision or changed package.
            check(self._ticket_snapshot(principal, issue_id) == snapshot,
                  "ticket_reference_changed", "Issue changed while linking the ticket", 409)
            current = self.packages._public(self.packages._row(request["packageId"], document["projectId"], guard))
            check(current == package, "ticket_reference_changed", "Package changed while linking the ticket", 409)
            guard()
        return {"reference": reference, "status": "matches-current-revision"}

    def ticket_references(self, principal, issue_id):
        document, guard = self._ticket_guard(principal, issue_id)
        with self._lock, self.packages._operation():
            snapshot = self._ticket_snapshot(principal, issue_id)
            references = []
            unavailable = 0
            for reference in self._all("ticket-reference"):
                if reference["issueId"] != issue_id:
                    continue
                try:
                    row = self.packages._row(reference["packageId"], document["projectId"], guard)
                except PackageError as error:
                    if error.code not in _UNAVAILABLE:
                        raise
                    unavailable += 1
                    continue
                matches = (all(reference[key] == value for key, value in snapshot.items())
                           and row["archive_digest"] == reference["capsuleDigest"]
                           and row["project_digest"] == reference["projectDigest"])
                references.append({**reference, "status": "matches-current-revision" if matches else "stale"})
            check(self._ticket_snapshot(principal, issue_id) == snapshot,
                  "ticket_reference_changed", "Issue changed while reading ticket references", 409)
            # Expiry/revocation may occur during the scan. Return no expired
            # identifiers, audit, digests or historical evidence references.
            visible = []
            for reference in references:
                try:
                    row = self.packages._row(reference["packageId"], document["projectId"], guard)
                except PackageError as error:
                    if error.code not in _UNAVAILABLE:
                        raise
                    unavailable += 1
                    continue
                if (row["archive_digest"] != reference["capsuleDigest"]
                        or row["project_digest"] != reference["projectDigest"]):
                    reference["status"] = "stale"
                visible.append(reference)
            guard()
            return {"references": visible, "unavailableCount": unavailable}
