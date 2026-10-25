# SDK export → review bridge (local, sample policy only)

This bridge connects finalized SDK exports to an inert human-review surface. It
is **not** an automatic recording-to-replay or recording-to-repair adapter.
`recordTap` and related callbacks are annotations, not proof of dispatched input.
No callback is translated to `dispatch=injected`, app logs, an authored oracle,
build identity, approval, or execution authority.

## Actual SDK integration surface

- Android: `android/sdk` is a Gradle Android library (`com.android.library`,
  namespace `io.reproof.sdk`, minSdk 26). The existing sample demonstrates an
  application dependency and `ReproRecorder` calls. The `freezeAndExport` callback
  receives `files/repro/<session-id>/capture.json` after its sibling `session.json`
  is finalized; collect that exact pair, including when consuming it later.
  The return value only accepts the asynchronous request. The legacy
  `capturePath()` and root `files/repro/capture.json` publication remain latest-only
  conveniences that later sessions can replace. A callback or
  published path alone is insufficient: metadata must say finalized, with
  incomplete/lostEvents/unsupported all false. Android metadata has no sequence
  boundary, so capture's own endSequence is checked against its event stream.
  Publications now use uniquely owned temporary files rather than a shared
  latest-file temporary path. This preserves the fail-closed host and bridge
  checks; it does not make capture/metadata publication a cross-file transaction
  or establish native/device export correctness.
- iOS: [`ios/SDK`](../ios/SDK/README.md) is a public, local Foundation Swift
  package with explicit bounded annotation policies. `ios/Recorder/Recorder.swift`
  is the thin sample-specific `ReproCase` adapter. Collect the session-specific
  capture JSON, `metadata.json`, and `finalized.json` returned by successful
  finalization. All session, fixture, start-state, timestamp and sequence boundaries
  must agree. The root `capture.json` is only a compatibility convenience; custom
  SDK policies are **not** admitted by this sample-only bridge.

The Python review bridge itself requires no native execution, device operation,
upload, service or credentials. The separate Swift package has its own bounded
Foundation-test and compile-only workflow. Integrating an arbitrary company app still
requires separately authored instrumentation/policies and real-device validation.
The bridge currently accepts only the repository Android default sample policy
and iOS counter sample policy, not arbitrary target IDs or user-authored schemas.

## Pure API and privacy boundary

### Local read-only CLI

```sh
reproof sdk-review --platform android --session-directory /path/to/exported/session
reproof sdk-review --platform ios --session-directory /path/to/exported/session
# From a repository checkout, use python3 -m reproof sdk-review with the same options.
```

The selected directory must contain `capture.json` and `session.json` for
Android, or `capture.json`, `metadata.json` and `finalized.json` for iOS.
Use the session-specific directory, not the root/latest convenience capture.
Extra siblings, including native `events.jsonl`, are ignored without being
opened or traversed. The command does not search or recurse, join a ticket,
register a collector binding, or accept service, credentials, AI, approval,
upload or output-file options. It accepts only the repository Android default
sample policy and iOS counter policy, not arbitrary company/custom app exports.

On supported POSIX hosts, every directory component is opened without following
symlinks. Required siblings must be regular files, each no larger than 256 KiB;
they are opened with descriptor-relative, no-follow, nonblocking reads before
any content is consumed. The directory identities and each required file's
opened/named identity, size, modification time and change time are checked
through the read. Missing files, symlinks (including ancestors), special files,
replacement and observed mutation fail closed. These checks detect observed
races, but are not a filesystem snapshot, native attestation or cross-file
publication transaction. Collect a finalized session without concurrent writers.

Exact file bytes reach the existing normalizer unchanged. Success prints only
`envelope.summary()` as JSON and exits 0. Invalid exports or unsafe/unavailable
file reads print only `{"status":"sdk_export_invalid"}` and exit 2. Invalid
arguments exit 2 with a fixed non-echoing message; paths, tokens and parser
details are never included. The command does not modify or persist the export,
contact services, inspect credentials, invoke devices or transfer data to AI.
All existing unresolved provenance and negative authority flags remain intact.

### Normalization API

`reproof.sdk_review.normalize_export(platform=..., capture=bytes,
metadata=bytes, marker=bytes_or_None)` returns `ReviewEnvelope` or raises the
fixed `ReviewInvalid('sdk_export_invalid')`. Read `envelope.summary()` for a
fresh JSON-safe review view. It contains safe allowlisted semantic action/target
pairs, platform, unresolved categories and negative authority flags only.
Android has no application ID in its export: the application bundle used for
matching comes only from the selected repository policy. iOS reports its own
application ID, which is not installed-build attestation. The summary labels
application identity policy-declared and keeps installed application identity
explicitly unresolved on both platforms; only the separately trusted collector
can assert a mapping, not the normalizer.

Parameters (including synthetic replace text), raw session/event IDs, timestamps,
start state, raw text, parser errors and input digests are not emitted.

Each file is limited to 256 KiB; events/arrays to 256; objects to 32 keys; nesting
to eight levels; strings to 256 characters. Unknown fields, duplicate JSON keys,
non-finite numbers, unexpected nested data, unsupported targets/parameters,
loss/truncation, reordered events and finalization mismatches are rejected.
Current native UUID session IDs and present, integer, nondecreasing elapsedMs
within 0..600000 are required; older loose synthetic captures are not admitted.
No generic redaction promise is made: unsupported content is denied, not retained
or summarized. There is no disk persistence, telemetry, AI transfer or network.

Existing `compile_capture` / `compile_ios_capture` validators enforce their
sample action policies; their required repository policy oracle is used only
for validation. The compiled scenario and oracle are discarded. The policy is
not a QA outcome and cannot become an authored ticket oracle. iOS finalization
also uses `validate_finalization`; Android metadata mirrors its existing SDK
collector checks without constructing a device provider.

The envelope privately retains exact-byte SHA-256 identity and SDK session ID.
`export_digest` gives a trusted collector the same length-framed digest over
platform/capture/metadata/marker. Whitespace changes alter identity. These
private fields are not authorization tokens and must not be logged or serialized.
Python dataclasses are not a security sandbox; callers are trusted code, and
must not construct an envelope from a client-provided summary.

## Explicit collector join, not SDK promotion

An authenticated service/operator must separately supply a `CollectorBinding`,
authorized `RegisteredCapsule`, and ticket to `linked_review`. Registration is
not authentication. Never deserialize registration or binding from the SDK,
ticket text, model output, or another untrusted payload.

The binding pins the export digest, SDK session, SDK platform/application bundle,
exact sealed capsule digest, and complete ticket identity tuple in `IDENTITY`
order. The capsule's registered application must match the SDK platform/bundle.
SDK session and app-log/recording session are distinct namespaces: the collector
must explicitly attest their mapping through that exact binding; no time-based,
latest-session or fuzzy matching is performed. A provided mismatch abstains.
A missing binding stays unresolved and abstains before creating a draft.

Only after this link succeeds does `intake_ticket` validate the
separate frozen complete recording, genuine collector-supplied dispatch receipts,
authored expected oracle, typed app logs and all project/build/source/session
identities. It retains its strict loss/truncation, identity and completeness
requirements. The SDK export does not satisfy any of those missing evidence
requirements. The result remains a review draft, never a saved/approved issue.
Approval and protected execution must follow their existing separate workflows.

Repeated sealed app-log observations must form the bounded, same-identity
[validated prefix chain](TICKET-INTAKE-DEMO.md#reference-only-intake), in original
observation order. Every snapshot is validated, and the final reference must
match the trusted collector's registered app-log digest. This does not fabricate
a collector mapping, select an arbitrary latest log, or rewrite frozen evidence.

## Runnable repository-owned joined example

From a repository checkout:

```sh
python3 scripts/qa-sdk-review.py
python3 -m unittest tests.test_sdk_review tests.test_sdk_review_cli tests.test_ticket_intake
```

The script reads only repository fixtures and prints both platforms' results:
review-only normalization; exact-bound review draft; missing-binding abstention;
mismatched-binding abstention; incomplete export rejected before any draft.

`sample_export` creates clearly synthetic exports in the actual native file
formats, not captures recorded on a device. The joined sample uses the PR21
synthetic sealed-capsule generator with explicitly synthetic platform/bundle
variants. Its preexisting fixture receipts, authored fixture oracle and typed
fixture logs stay separate from SDK annotations. They demonstrate contracts,
**not production injected receipts or company-app acceptance**. The SDK and
capsule session names deliberately differ to exercise explicit mapping.

`actualAI`, `nativeExecution`, `companyAcceptance` and `repairReady` remain false.
The existing [offline causal demonstration](TICKET-INTAKE-DEMO.md) can be invoked
separately through `scripts/qa-ticket-repair.py`; this bridge never invokes it or
claims its result as SDK validation. Its local Python exercise is not native or
AI execution either.

The existing `TICKET_INTAKE` portable inventory check includes these bounded
inert tests and local CLI filesystem/race/privacy regressions. Workflow triggers
and permissions are unchanged. Passing portable
checks cannot establish SDK installation, native behavior, company deployment,
or real AI repair acceptance.
