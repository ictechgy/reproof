# Project-scoped ticket references

The authenticated issue workflow can attach a bounded ticket identifier to an
already published issue package. This is a durable **reference-only link**, not
an authenticated ticket-provider connector, tenant-scoped intake, a ticket
inbox, or evidence that a ticket's claims are true. Existing AccessController
project membership is the only authorization scope; there is no new tenant or
collector authority.

## Explicit reviewer workflow

1. Record and stop the issue using the existing issue workflow.
2. Author and save a specification. Review its exact revision and digest.
3. Explicitly export that revision through the existing `export` action. Retain
   `package.id` from the returned JSON (the sibling `exported` field is the local
   file path, not a package ID). Linking never exports or modifies a package.
4. Link the ticket ID to that package and exact displayed specification.
5. Review references with `ticket-references`. Approval, replay, locator review
   and repair remain separate explicit actions with their existing prerequisites.

```sh
python3 -m reproof live-issues export ISSUE_ID --output /new/output/issue.zip \
  --server https://coordinator.example.internal:9443
python3 -m reproof live-issues ticket-link ISSUE_ID \
  --ticket-id QA-42 --package-id PACKAGE_ID \
  --revision 1 --specification-digest SPECIFICATION_SHA256 \
  --server https://coordinator.example.internal:9443
python3 -m reproof live-issues ticket-references ISSUE_ID \
  --server https://coordinator.example.internal:9443
```

Use the actual returned IDs and digest in place of the placeholders. Credentials
are prompted without echo, or read with the existing `--credential-stdin` flag.
They are never command-line arguments. The CLI uses the same authenticated HTTP
routes as other issue operations, including browser-session revocation and CSRF.
No external ticket service is contacted. The ticket ID's namespace and meaning
are the reviewer's responsibility; storing `QA-42` does not authenticate a
provider or verify a ticket exists.

## Browser workbench

In the authenticated workbench, select an issue with a saved specification and
use **Export package**. The **Ticket references** panel displays the returned
package ID with the displayed revision and specification SHA-256. Enter a ticket
ID and select **Link exported package**. This sends only the four fields below;
it does not export implicitly or contact a tracker. **Refresh references** is a
read-only retry, including when a link may have completed but its response failed.
Matching/stale status is reference provenance only, never approval or verification.

The package candidate exists only in browser memory. A fresh explicit export is
required after navigation or reload, sign-out, capability/project/revision changes,
expiry or a failed reference request. Navigation and sign-out cancel pending reads
and links; late responses cannot restore the old issue's package or references.
An interrupted link may have committed on the server, so refresh before retrying.
The server remains authoritative for retention and current revision checks.

## API and immutable binding

`POST /api/release/issues/{issueId}/ticket-references` accepts exactly:

```json
{
  "ticketId": "QA-42",
  "packageId": "package_existing_publication",
  "baseRevision": 1,
  "baseSpecificationDigest": "<64 lowercase hexadecimal characters>"
}
```

`ticketId` is 1–64 ASCII alphanumeric, underscore or hyphen characters and must
start with an alphanumeric character. Case is preserved and significant; the
[intake adapter](TICKET-INTAKE-DEMO.md) uses the same rule. Package IDs retain
the existing internal contract ID syntax. Revision is an integer from 1 to 64,
not a boolean. Unknown keys, URLs,
free text, tracker credentials, requester-supplied project/source identities,
custom audit fields, or approval flags are rejected. Do not put confidential
information in ticket identifiers.

Both POST and `GET .../ticket-references` require `specification.maintain` and
`export.read` on the authorized issue and its current registered project. Viewer
and operator roles alone are insufficient. Cross-project package IDs are not
resolved. The server derives and compares:

- the issue's resource binding, runtime project digest and original project revision;
- application/build IDs and registered artifact/source digests;
- original recording identity/digest and exact specification revision/digest;
- the published package's archive SHA-256 (`capsuleDigest`).

Missing registered source identity or mismatched historical project evidence
causes refusal, not an inferred source association. Imported package content
remains provenance only, including any qualification it contains. An imported
issue still needs the existing explicit local-binding approval before replay.

Each reference stores only these immutable identifiers/digests, a hash of the
linking principal and its creation timestamp. It does not copy logs, recordings,
ticket text, oracle values, usernames, credentials or URLs into its audit record.
Original evidence, specification assertions, local approval, qualification and
repair state are not changed. SDK app-log events remain observations, not
coordinator dispatch receipts.

## Concurrency, retention and status

The reference identity is derived from issue ID, ticket ID and package ID.
Repeating that request against the same current specification returns the
original reference, including the original actor and time. Concurrent duplicates
create one record. The same identity cannot silently retarget changed immutable
references. A new package/revision requires an explicit new link. There are at
most 64 references per issue, within the existing 8,192-document global quota
and bounded workflow SQLite index; duplicate retries do not consume quota.

A SAVEPOINT in the existing documents database covers reference publication.
The workflow serializes revision changes and holds the package operation lock
through commit. It rechecks exact revision/identity, both capabilities, browser
session/credential validity and package/source retention after writing and
before commit. Failure rolls back publication without generating a package or
changing the original, specification, approval or campaign.

GET returns `references` plus `unavailableCount`. A visible reference is marked:

- `matches-current-revision`: the retained package matches the current immutable
  project/source/recording/specification references;
- `stale`: a saved reference no longer matches those current references.

Neither status means replay-ready, approved, reproduced, repaired or verified.
Unavailable, expired or removed packages are omitted without returning their
reference IDs, ticket identifiers, audit metadata or digests. Their count is
reported without details. If the issue source itself has expired, project
registration is stale, or permission has been revoked, the operation fails
closed instead. The bounded historical metadata remains in the issue index;
linking does not extend evidence retention. A read checks retention and
permissions again before returning. As with other APIs, previously downloaded
information cannot be recalled.

## Find an existing reference by ticket ID

When the Reproof issue ID is unknown, use an exact project-scoped lookup:

```sh
python3 -m reproof live-issues ticket-lookup \
  --project checkout --ticket-id QA-42 --credential-stdin
```

The authenticated API is `POST /api/release/projects/{projectId}/ticket-lookup`
with exactly `{"ticketId":"QA-42"}`. POST keeps the external identifier out of
URL paths/query strings and requires the existing browser CSRF protection. This
is a read-only operation: it does not create a reference, contact a provider,
export evidence, approve a specification, or start replay/repair.

The response has `references` (sorted by reference ID) and `unavailableCount`,
with the same retention and stale/current semantics as issue-scoped history.
Each reference includes its `issueId`; multiple issues and package revisions
can legitimately share one ticket ID. No single result is silently selected.
Matching is case-sensitive and exact (`QA-42`, `qa-42`, and `QA-420` differ),
and restricted to the explicitly selected project. There is no substring,
wildcard, cross-project search, provider namespace inference, or ticket prose.
No matches returns an empty list only after authorization succeeds.

Both `specification.maintain` and `export.read` remain required, including for
empty results. Every matching issue's resource/project binding, current source
retention, project registration and package retention are rechecked. A stale
registration, expired source or revoked permission fails the whole operation
closed; unavailable linked packages are omitted and counted without metadata.
Other projects and other ticket IDs do not contribute to results or counts.

Lookup scans the existing bounded local index (8192 total documents / 8 MiB),
with at most 64 matching stored references, including unavailable references.
More matches returns `ticket_lookup_limit` (409), never a partial/truncated
result; use issue-scoped history instead. A cooperative 30-second lookup budget
is checked between issue validations and before return (`ticket_lookup_timeout`,
408); it does not interrupt an individual storage read. There is no pagination
or new database migration. The browser's existing issue-scoped history is
unchanged; project lookup is available through API/CLI only.

## Portable verification boundary

```sh
python3 scripts/release-check.py --goal TICKET_REFERENCES \
  --effects filesystem,process,loopback
```

The gate runs actual temporary workflow/access/package SQLite stores, the
loopback coordinator, real authentication/CSRF and CLI subprocesses. Tests cover
role and project boundaries, package/specification/source binding, expiry,
revocation after writing, rollback, revision races, duplicate concurrency,
idempotency, quotas, restart, stale listing and privacy rejection.

The end-to-end synthetic path records typed app logs and two dispatched actions,
saves/exports/links a package, demonstrates that replay still requires separate
approval, performs the unchanged three-attempt replay, and creates a review-only
locator proposal. An unconfigured repair executor remains blocked. The generated
PNG fixture uses an exact-byte test validator, not a native media decoder. This
is not device, native-decoder, company-app, external-ticket, AI-provider or
protected-repair qualification. No company data or external integrations are
required or exercised.

The UI state and request checks are included in the existing Node browser-logic
wrapper (`python3 -m unittest tests.test_g7_browser_logic`). The separate optional
`python3 -m unittest tests.test_ticket_reference_browser -v` smoke uses installed
Chromium and Python Playwright with disposable profiles, synthetic issue/package
fixtures and the authenticated loopback server. It checks mobile layout, repeated
link identity, unchanged approval/execution, delayed navigation/sign-out responses,
logout retry, revision changes, package expiry and permission revocation. Missing
browser prerequisites fail explicitly; they are not a skipped pass or part of the
stdlib portable gate. Browser coverage does not qualify native devices or trackers.
