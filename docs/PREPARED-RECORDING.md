# Prepared recording and frozen scenario replay — G4

G4 adds a trusted local, single-owner composition path on top of the accepted
G0–G3 contracts. It does not authenticate shared administrators, operate a
company backend, validate a repair candidate, or emit a protected repair
verdict. Imported JSON remains inert.

## Public Python composition API

Trusted local setup registers the unchanged G0 project with `Lab`, then binds a
fixed loopback adapter to the project's fixture/check/cleanup recipes:

```python
registration = lab.register_recording_project(project, collection_policy)
fixtures = FixtureCoordinator(state_root / "fixtures")
adapter = LoopbackFixtureAdapter(
    "fixture_service",
    "http://127.0.0.1:8765",
    capabilities=AdapterCapabilities(
        remote_fencing=True,
        terminal_status=True,
        idempotency_retention_ms=60_000,
    ),
)
plan = fixtures.register_plan(
    registration,
    application_id="ios_app",
    fixture_id="seed_account",
    check_recipe_ids=("check_account",),
    cleanup_recipe_id="cleanup_account",
    adapter=adapter,
)
issues = lab.create_issue_session_service(fixtures)
```

`IssueSessionService.start_prepared_recording(...)` validates the registered
project/application/build and capture policy, reserves the device and fixture,
executes the registered preparation and start-state checks, and passes only the
actual complete receipts into `Lab.create_release_session`. The returned
`IssueSessionHandle` is a process-local capability for `issues.input(...)` and
`issues.stop(...)`; callers do not bypass the service to attach preparation or
change authority.

All selected plans, unique fixture IDs and payloads are validated before any
remote effect. Replay also requires the approved project and fixture-equivalence
digests, plus the historical prepare/check payload digests when preparation is
known. Duplicate, unknown, missing or wrong-operation historical receipts cannot
establish known preparation.

Native preparation reserves the canonical `HostAuthority` device lock before
fixtures run and transfers that handle into startup. Grant expiry is checked
before reservation and again before provider construction. Prepared native
sessions require shared authority. Legacy native mode and coordinator-only
remote reservations are rejected; G6 must implement reservation on the actual
physical worker.

`stop` first commits the G2 input/receipt barrier and bounded G3 finalization.
Fixture and device cleanup then append lifecycle receipts referencing that
frozen digest. The service reloads and compares the original after cleanup;
cleanup never changes its bytes. The device producer is closed before fixture
cleanup can release its slot. A timed-out close or unreturned input leaves
device cleanup unknown and retains fixture exclusion. Confirming device cleanup
does not release an uncertain fixture. Every domain keeps its own outcome.

## Fixture durability and recovery

`FixtureCoordinator` journals an operation before dispatch. Its idempotency key
binds allocation generation, operation ID, and payload digest. Payload bytes are
not journaled. Reusing an operation ID with changed bytes or a stale generation
fails before transport.
The durable allocation also binds the project revision, application and complete
registered plan. A fresh prepare cannot overtake a pending or unknown prepare
or cleanup. Late terminal receipts cannot restore a quarantined allocation to
ready state.

The adapter accepts only a fixed `http` loopback address and fixed
`/operations` and `/status` paths. It follows no redirects. Timeouts cause one
bounded status inspection, never a repeated mutation. Missing remote fencing,
missing terminal status, expired retention, transport ambiguity, and failed
cleanup quarantine the allocation. A cleanup sent while an earlier prepare is
nonterminal cannot release the slot; after the late prepare becomes terminal, a
new cleanup operation is required.
The timeout covers the entire HTTP response, including a continuously dripping
body. Duplicate JSON fields and oversized responses are rejected.

The coordinator is protected by a process lock. On restart, admitted/dispatched
operations become unknown and unfinished allocations become quarantined. Local
process disappearance or remote-service restart is not evidence that an effect
ended. Trusted adapters must be re-registered before reconciliation because
their executable authority is never restored from SQLite.
Use `recover_allocation(plan, allocation_id=..., owner=...)` to recover a
quarantined capability with the exact same plan, then inspect status through
`reconcile`. A related producer must be confirmed stopped before a new cleanup
may release its fixture. The unreleased prototype allocation/qualification
schemas without these binding/admission columns fail closed rather than being
silently adopted; G5 owns explicit versioned adoption.

## Approved interpreter and qualification

`ScenarioRegistry.register(...)` validates exact project, original, authored
specification, qualification, fixture-equivalence, observation, runtime-policy,
and original-build bindings. It persists inert bytes and returns an
`ApprovedSpecification` capability. Re-registering the same specification ID
and revision with changed bytes is rejected. Restarted processes must perform a
new trusted registration; stored JSON cannot grant execution.
Every execution rehashes the actual approved documents and uses an independent
validated snapshot, so mutating a capability's nested dictionaries cannot change
approved behavior.

`VariableResolverRegistry` and `ObservationRegistry` accept explicit local
Python capabilities. `ScenarioRunner` supports the G0 tap, long-press, swipe,
pointer, text, back, home, rotate, launch, and terminate IR. It checks current
provider actions, current geometry, and replay-time locator evidence. Locator
evidence is durably admitted to the replay-attempt recording and its digest is
bound to the action receipt. Locator authorship remains separate from what the
original recording actually observed. The runner supplies one operation
identity to `Lab.input`, so the G1
authority permit, provider dispatch, G2 event, and replay receipt remain bound.

Secret variables are resolved only in memory. A value may reach the selected
provider, but it is absent from scenario results and fixture/issue/specification
journals. Text recording retains only its registered variable ID. Registered
observations are rejected before persistence if they contain a resolved secret.
The check covers decoded strings in values and metadata, including strings
containing JSON escapes. Observation bodies must fit their declared byte limit.

Observation adapters declare snapshot, sampled, and/or continuous support.
Each returned envelope is checked with the frozen G0 coverage requirement and a
trusted oracle anchor/current time. Unsupported, stale, truncated, partial,
future, or wrong-scope evidence is unknown. Sampled evidence cannot satisfy a
continuous requirement. Predicate evaluation is three-valued.
Each sampled property must include the actual values at every declared sample
time. Continuous properties are complete change streams with an initial value
at capture start and a closing value at capture end; the value in force at the
beginning of a stability window is included in evaluation. A missing property
is unknown. An adapter can explicitly attest absence across its complete
capture interval through `ObservationEvidence.absent_properties`.

Variable, locator, input and observation calls are bounded by the replay
deadline. Cancellation and expiry are checked after callbacks and before new
input. A late callback cannot produce a successful verdict. Locator collection
checks policy, current device ownership, session state and acquisition time.

`QualificationEngine` freezes the required original-attempt count before the
first run (three by default), appends every result to its fixed slot, and never
replaces an invalid or unfavorable attempt. Only all required original-build
runs with known historical preparation, exact current preparation payload
digests, complete coverage, `(defect=true, expected=false)`, and completed
cleanup produce `reproduced`. Injection and observation remain separate result
fields. Candidate build substitution requires the existing process-local
`TrustedSubstitutionApproval`, preserves every other binding, and remains only
an observed G4 replay. Protected candidate/regression validation belongs to G9.
An attempt-executor exception consumes a retained unknown slot using a static
failure code; exception text is neither persisted nor returned.
`run_original(approved, execute_attempt)` admits and commits each numbered slot
before invoking the callback. For stepwise execution use
`run_attempt(campaign, approved, execute_attempt)`, or call
`begin_attempt(campaign, approved)` before execution and then `record_attempt`.
An old result cannot fill a newly admitted slot, and one run ID cannot fill
multiple slots. The engine holds an exclusive writer lock; reopening after a
process death retains the admitted unknown attempt and quarantines the campaign.

## Local gate and limitations

```bash
python3 scripts/release-check.py --goal G4 --effects filesystem,process,loopback
```

The gate uses owned temporary directories, child processes, and synthetic
127.0.0.1 services. It does not access a company fixture/backend, a physical
device, a second Mac, credentials/signing material, an external endpoint, or an
AI transfer policy. Those acceptance scopes remain unavailable.
