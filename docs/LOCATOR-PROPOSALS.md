# Review-only locator change proposals

This is a pilot preparation feature, not autonomous self-healing. It suggests no
candidate by itself, calls no AI, never clicks a fallback, and never relabels a
failed original replay as a healed pass. App-code repair remains a separate
workflow with separate evidence and approval.

## Authenticated API

Use the existing authenticated shared coordinator issue API. No new permission
scope or credential is needed. Only existing project maintainers can propose or
decide; issue readers can inspect the journal for their authorized issues.

- `POST /api/release/issues/{issueId}/locator-proposals`: exactly `baseRevision`,
  `baseSpecificationDigest`, `eventId`, and `target`.
- `GET /api/release/issues/{issueId}/locator-proposals`: proposal and decision history.
- `POST /api/release/issues/{issueId}/locator-decisions`: exactly `proposalId` and
  `decision`, either `apply` or `reject`.

`target` has exactly `kind` and `value`. Both old and proposed locators must have
one of the same supported kinds (`accessibility-id` or `resource-id`). Only an
existing locator action can change. Values are limited to 160 ASCII identifier
characters: first a letter, then letters, digits, underscore, dot, colon, slash,
or hyphen. Role, ancestry, display text, coordinate conversion, action changes,
raw screenshots/logs, explanations and arbitrary audit fields are not accepted.
This syntax check is **not a secret detector**: supply only non-secret stable
application IDs. The service does not resolve secrets to inspect proposals.
Identifiers are retained and shown to authorized issue readers. Reviewer IDs are
hashed; no credential or user-provided free-text reason enters this journal.

A successful proposal is an inert suggestion, not proof that the candidate
exists, is unique, is semantically equivalent, or has the right next screen.
Reviewers must establish those facts using authorized evidence. Unsupported
provider capabilities continue to fail closed at replay. Android's registered
resource-ID restrictions and iOS/remote locator limitations are unchanged.

## Review and acceptance

1. Inspect the original failure and identify a stable non-secret replacement.
   Do not propose a new assertion, expected result, fixture or action to make a
   failure disappear. The API cannot make those edits.
2. Submit against the exact current specification digest and revision. Review
   the old/new target, original recording binding, project binding and event ID.
3. Explicitly apply or reject. Rejection changes neither spec nor approval.
   Application creates a **new, unapproved specification revision**, preserving
   all actions except the selected target, assertions, waits, fixtures, bindings
   and original recording. The previous revision is retained.
4. Perform the normal explicit approval and fresh original qualification before
   candidate verification. Old approvals/campaigns do not authorize the revision.
   A failed postcondition must remain failed; do not weaken the oracle.
5. To revert, propose the old target against the new current digest/revision and
   apply it explicitly. This creates another unapproved revision, not restoration
   of old authority. The immutable proposal/decision history remains visible.

Stale proposals cannot apply. A proposal can be decided once; duplicate decisions
return a conflict, without creating an extra revision. Spec publication, approval
invalidation and the decision are one SQLite transaction. Storage or authorization
failure rolls them back together. History reads recheck authorization before
returning. The workflow’s issue index has an existing global 8 MiB ceiling; each issue
accepts at most 64 lifetime proposals (including rejected proposals), and existing specification revision limits apply.
Operations use bounded documents/scans and the existing HTTP request deadline;
there are no model calls or candidate-retry loops.

## Verification boundary

Portable regressions use temporary stores, a loopback fixture service and a
simulated provider. They cover no-dispatch, authorization/revocation, immutable
original/oracle, CAS, concurrent/double apply, bounded input, transaction rollback
and revert. They do not establish actual-app selector quality, physical-device
coverage, operational load capacity, or enterprise security acceptance. Automatic
candidate generation, visual matching, approval UI and runtime fallback remain
unimplemented and must not be advertised as available.
