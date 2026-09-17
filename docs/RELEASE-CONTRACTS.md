# Repro Loop release contracts — schema version 1

These G0 contracts are the frozen data boundary for later delivery goals. They are strict JSON-compatible value validators implemented only with the Python standard library. Validation proves shape and cross-reference consistency; it does not execute a recipe, grant authority, prove an oracle, qualify reproduction, or mark a repair verified.

The examples in `tests/fixtures/release/` are synthetic and runnable. `project.json`, `evidence.json`, `scenario.json`, `observation.json`, `qualification.json`, and `package.json` form one internally linked example. They are not company acceptance evidence.

## Public Python API

```python
from reproloop import contracts

project = contracts.validate_project_revision(project_wire)
recording = contracts.validate_original_evidence(recording_wire)
specification = contracts.validate_specification(specification_wire)
qualification = contracts.validate_qualification(qualification_wire)
contracts.validate_qualification_bindings(
    qualification_wire, project_wire, recording_wire, specification_wire
)

observation = contracts.validate_observation(observation_wire)
requirement = contracts.validate_coverage_requirement(requirement_wire)
bound_requirement = contracts.bind_coverage_requirement(
    relative_requirement_wire, anchor_ms=trusted_oracle_start_ms
)
coverage = contracts.observation_result(
    observation_wire, bound_requirement, evaluatedAtMs=trusted_now_ms
)  # "covered" or "unknown"

match = contracts.classify_predicates(defect, expected, phase="candidate")
# "match", "mismatch", or "unknown"; never "verified"

contracts.validate_attempt_budget(budget_wire)
contracts.validate_run_sequence(budget_wire, all_run_wires)
contracts.validate_execution_policy(policy_wire)
contracts.validate_lifecycle_receipt(receipt_wire)
contracts.validate_package_manifest(package_wire)
```

Validators return detached data or raise `contracts.ContractError` with a static, non-sensitive message. `digest(value)` is canonical JSON SHA-256 (`sort_keys`, compact separators, UTF-8, and no NaN/Infinity). IDs are lowercase ASCII identifiers up to 64 characters. Digests are 64 lowercase hexadecimal characters. Epoch milliseconds are strict integers from 0 through year 3000; booleans and floats are never integers. Artifact sizes are at most 10 GiB, observations at most 50 MiB, waits at most 60 seconds, aggregate authored delays at most 10 minutes, and individual gestures at most 5 seconds. Predicate scalar text is bounded to 4096 UTF-8 bytes; numbers are finite and within the exact JSON integer range. Other collection limits are enforced in the validators.

## Trusted project and fixture boundary

A project revision registers application/build identities, typed variables, fixture and validation recipe IDs, observation IDs, evidence policy, editable product paths, and execution classes. Paths are canonical POSIX-relative repository paths; absolute paths, empty/dot/traversal segments, backslashes, NUL/newlines, and known secret filenames (`.env*`, `auth.json`, credential files, and private-key names) are rejected. Real paths such as `src/main/kotlin/com/example/Checkout.kt` are valid.

Fixture recipes declare `remoteFencing`, `terminalStatus`, and bounded `idempotencyRetentionMs`. These capability facts do not imply safety: a later allocation must quarantine uncertain work when remote fencing or terminal evidence is unavailable. Imported data can only reference already registered recipe, endpoint, variable, build, application, and observation IDs. No contract field contains a shell command, URL, plugin, executable, or dynamic endpoint registration.

The evidence policy distinguishes permission to collect pixels, text, accessibility data, logs, and fixture receipts, plus fail-closed unknown-sensitive behavior and AI eligibility. Those flags are project data to be enforced by later trusted services, not proof that enforcement occurred.

G2 keeps test-data capture mode and retention outside this frozen G0 wire. A
trusted local service validates the unchanged project, then issues a
process-local recording registration for a strict collection policy. Imported
project JSON and `pixels: true` alone cannot issue that capability or authorize
an unknown-sensitive sample. This preserves every existing project byte and
digest. See [Durable recording and evidence storage](DURABLE-RECORDING.md) for
the registration, sample classification, retention and recovery APIs.

## Immutable evidence and executable specification

An original recording binds project/application/build identity before input and stores each admitted event's typed input, operation ID, ownership generation, sequence, relative admission time (`offsetMs`), dispatch result, receipt, and provenance. A receipt binds the same operation/generation/result to the provider incarnation and observed time. A missing receipt requires `unknown`; rejected/unknown intents have `observed` provenance, and an acknowledged injection has `injected` provenance. Preparation receipts name a registered recipe and must match the recording's project/application and precede recording. Observation and media entries are bounded digest/path/size/MIME references.

`sealed: true` and `endSequence` freeze exactly the contiguous admitted events. A later acknowledgement, stop, cleanup, quarantine, or reconciliation is a separate append-only lifecycle receipt bound to the recording digest, operation identity, generation, and receipt sequence. It cannot be added to or rewrite the original object. A package manifest is another versioned selection object and must include its recording and specification digests in `objects`.

The approved specification maps recorded event IDs, in original order, to a closed IR: `tap`, `long-press`, `swipe`, `pointer`, `text`, `back`, `home`, `rotate`, `launch`, and `terminate`. Locators use exact accessibility/resource IDs with optional exact role and bounded ancestor locators. Coordinate input uses normalized x/y values from 0 to 1 and requires width, height, rotation and version in `geometry`; geometry may additionally bind a frame digest. Locator and coordinate targets cannot conflict. Swipe requires both endpoints and a duration. Pointer IDs range from 0 to 4; the runner owns transition/timeout checks. Launch/terminate name a registered application. Text contains only a registered string or secret-reference variable, never raw text. A later adapter explicitly maps these operations to supported legacy/native operations and refuses unsupported capabilities.

Waits, bindings, fixture references, and assertions have closed shapes. Exactly one defect assertion and one expected assertion are required. Predicates support bounded Boolean composition and exact property presence/equality/numeric comparisons. Each assertion freezes its observation class, window, stability, freshness, scope, properties, and sampling requirement. Snapshot assertions cannot claim stability. Fixture rules must cover precisely the used fixtures; qualification observation requirements bind each `(assertionId, observationId)` to the exact approved coverage requirement.

## Coverage and qualification semantics

Observation envelopes describe actual interval, clock uncertainty, scope, supported properties, traversal limits, errors/truncation/completeness, and snapshot/sampled/continuous coverage. Specification/qualification windows are relative to the oracle phase of each replay; `bind_coverage_requirement(..., anchor_ms=...)` creates a runtime window without changing the frozen object. The runner supplies that trusted anchor and an explicit current time in the same translated clock domain. The convenience of deriving acceptance bounds or current time from provider data is intentionally unavailable.

Coverage checks account for uncertainty on interval edges, freshness, and sampling gaps. A snapshot's explicitly approved uncertainty is its timestamp tolerance. Truncated, erroneous, unsupported, stale, future-dated, incomplete-window, missing-sample, or wrong-class evidence evaluates to `unknown`. Samples never prove continuous absence. Slow providers can use a larger explicitly approved sampling/freshness bound. Coverage metadata is not an assertion-value evaluator: later trusted providers/runners must bind it to actual measured values and independently check those values.

The predicate-pair truth table is fixed:

| phase | defect | expected | result |
|---|---:|---:|---|
| original | true | false | match |
| candidate | false | true | match |
| either | true | true | mismatch |
| either | false | false | mismatch |
| either | unknown/non-boolean | any | unknown |

`match` is only an oracle result. Qualified reproduction additionally needs exact project/original-build/specification/fixture/observation/runtime/validation bindings and all frozen original attempts. Verified repair is reserved for a later protected supervisor after candidate substitution authority, every candidate attempt, independent regressions, and cleanup evidence pass.

The default/frozen budget is three original and three candidate attempts, total six. The total is fixed before execution. `validate_run_sequence` requires attempts 1–6 with no gaps, duplicates, replacements, or extras; phases must occupy their fixed slots. An invalid/unknown-coverage run, an invalid predicate pair (including a transient invalid-between-successes), regression failure, or incomplete cleanup fails the entire sequence and cannot be discarded.

## Local authority and candidate substitution

Wire data never grants execution authority. `validate_candidate_run` rejects historical `substitutionAuthorized`, `regressionEvidence`, or similar booleans. Trusted server code first performs its admin/maintainer authorization, then may call:

```python
approval = contracts.issue_substitution_approval(
    qualification_digest=qualification_digest,
    recording_digest=recording_digest,
    specification_digest=specification_digest,
    candidate_build_id="candidate_build",
    candidate_build_digest=validated_candidate_build_manifest_digest,
)
contracts.check_candidate_substitution(candidate_wire, approval)
```

The frozen, process-local `TrustedSubstitutionApproval` cannot be instantiated from JSON into a valid issuer capability. The candidate wire includes `candidateBuildDigest`, which binds the exact validated build manifest containing the source/artifact digests. Reusing a build name with different bytes cannot reuse that approval. Recording, project revision, specification, fixture equivalence, observation requirements, runtime policy, and validation recipes stay frozen. Trusted registries/runners own issuance after authorization and must not expose it to imported packages or candidate processes.

Execution policies list technical `enforcedControls` separately from operator `attestations`; the same control cannot appear in both. Effects recorded by `release-check` are selection declarations, not enforcement or sandbox receipts.

## Versions, migration, legacy, and rollback

Schema version 1 readers accept exactly version 1. A future reader does not automatically reject version 1: compatibility exists only when that reader retains this validator or supplies an explicit, tested migration producing a new object and digest. Unknown fields and unsupported versions fail closed. Changing a specification creates a new revision/digest and invalidates its qualification. Migration never rewrites original bytes in place.

Legacy capture and bundle APIs remain unchanged and continue through their original validators. G0 does not reinterpret legacy files as executable specifications or qualified evidence. Rollback means disabling consumers of these new schema-version-1 objects; sealed originals and lifecycle receipts remain immutable artifacts.

The trusted G4 registries, durable fixture allocation state, prepared session
sequence, bounded interpreter, and original qualification API are documented in
[Prepared recording and frozen scenario replay](PREPARED-RECORDING.md). These
services consume the unchanged schema-version-1 objects; they do not add wire
fields that confer execution authority.

## Release gate

Run the complete local G0 software gate with:

```bash
python3 scripts/release-check.py --goal G0 --effects filesystem,process
```

The checked-in inventory selects whole contract/boundary suites and the runner's own fault checks; the separate test that invokes the full gate is excluded from that gate to avoid recursion. Every entry declares its command, environment, effects, timeout, evidence type, and software/environment gate. The entire registry is validated before any command runs. Unknown/unavailable goals, malformed/empty registries, unregistered commands, and missing effects block execution. Timeouts, excess output, test failures, and zero/missing test evidence fail a check. Child output is bounded while reading, and owned process groups are terminated at completion or failure. The report retains actual exit code, elapsed time, command, test count, and bounded summary. Issue packages cannot register executable commands.
