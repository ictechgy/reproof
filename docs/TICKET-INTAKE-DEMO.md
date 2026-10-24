# Local QA ticket intake and causal repair demonstration

This is an **offline, repository-checkout demonstration**, not company-app
acceptance, an installed ticket connector, an AI integration, or a production
repair executor. It does not provision devices, VMs, credentials, services, or
network access. No external source transfer occurs.

## Reference-only intake

`reproof.ticket_intake.intake_ticket` consumes bounded JSON bytes containing:

- `schemaVersion: 1`, `ticketId`
- `tenantId`, `projectId`, `applicationId`, `buildId`, `sourceDigest`
- `sessionId`, `recordingDigest`
- Optional `text`, which is discarded completely, including from ID hashes.

The service supplies the authenticated tenant and a tuple of locally authorized
`RegisteredCapsule` snapshots. **Do not construct this registry from ticket
attachments or let a ticket register its own project.** The host remains
responsible for authorization, retention, and pinning the snapshot throughout
the call. Registry dataclasses are data, not authentication capabilities.

The adapter reuses project, original/specification, app-log, and closed-graph
issue-package validation. It checks the selected build's source identity,
project log-collection policy, revision, bundle/platform, collector run/session/profile, and the exact
log object sealed in the capsule. There is no timestamp-based or fuzzy match. This correlates references to an
already authored specification, not SDK action extraction. Original evidence
does not independently prove the installed artifact or source hash: build
identity here relies on the trusted registry and collector, not attestation.
Multiple matches, missing evidence, missing expected assertion, interrupted or
unknown recording, and lost/truncated/empty logs cause abstention. Multiple log
snapshots require upstream consolidation rather than selecting an arbitrary
latest snapshot. This first version supports log-only capsules: media requiring
a decoder is rejected rather than bypassing normal package validation.

The result contains only digests, a deterministic draft ID, and fixed status
codes. Unknown fields (including raw payloads, secrets, recipes and approvals)
are rejected; ticket prose, exception text, logs and oracle values are never
returned. This is default-deny data minimization, not a general secret detector.
The registry itself can contain sensitive evidence and must remain in the
existing authorized storage boundary.

A `review-required` result is **not an authored specification or approval**.
Reviewers resolve its digest references through their existing authorized issue
service. Saving a specification still uses `IssueWorkflow.save_specification`;
approval and replay remain separate existing operations. Intake does not call
any of them and does not invent provenance, fixtures, recipes, expected behavior,
or user approval from ticket prose. Capsule qualification remains inert
provenance. There is no persistent ticket inbox or automatic connector yet.

## Causal local repair

From a repository checkout:

```sh
python scripts/qa-ticket-repair.py
python scripts/qa-ticket-repair.py --candidate wrong
python scripts/qa-ticket-repair.py --candidate noop
python -m unittest tests.test_ticket_intake tests.test_offline_repair_demo -v
```

The repair demo first constructs a repository-owned synthetic sealed capsule,
typed log, authored specification and structured ticket for the same source.
It calls the real intake adapter and requires exact draft project/source/specification/
oracle bindings before evaluating any function. The synthetic mobile-shaped
recording is clearly labeled fixture data, not actual SDK or device capture.
The frozen oracle digest is included in the synthetic specification provenance.
This is a connected synthetic correlation-to-function proof, not full scenario
replay: the schema-compatible launch action is not dispatched. The executable
behavior under test is the checkout function, evaluated against frozen oracle cases.

It then uses the repository-owned checkout fixture and an independently
frozen oracle, not the synthetic mobile execution helper that reports success
independent of source. It applies a scripted patch using `RepairSource`'s
original/source binding and protected-path checks, executes baseline and
candidate behavior, and compares actual outputs with the same oracle.

Only a narrowly declared, bounded function shape is accepted before execution;
this is **not a hardened sandbox** for arbitrary source code. The CLI accepts
named repository-owned candidate variants, not user-supplied code or patches.
Oracle and recipe inputs are protected. The wrong change still fails and a
no-op cannot count as a successful repair.

The report is local software evidence only. It grants no
`ProtectedRepairExecutor` authority or native verified proof. Actual AI, mobile,
VM, and company acceptance remain false. The explicit script invocation selects this bounded local exercise; its
review draft still says approved=false and executionAuthority=none. The report
says approval=not-granted-demo-only. An inert draft never authorizes production
execution, save, approval, replay, or repair.

## Remaining integration work

A production deployment still needs a real authenticated ticket connector,
retention-aware SDK collection and capsule registration, operator-reviewed
expected behavior, separately approved AI/data-transfer configuration, and the
existing qualified protected executor for the company's actual app. This
change is not evidence that any of those systems have been deployed or accepted.
