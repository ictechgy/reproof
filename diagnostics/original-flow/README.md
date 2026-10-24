# One-shot original UIKit flow diagnostic

This is a narrowly scoped diagnostic based on PR #13 commit
`6eb207897f5819bf74b949c062a88e67df810abd`, not broad native acceptance.
Only a new, same-repository public PR **opened** against
`codex/owned-native-runtime-acceptance` can run the new workflow. It rejects
run attempts other than 1. There is no push, synchronize, dispatch, rerun,
offline replay, archived-image comparison, or second app-flow entry point.
Only this directory and its dedicated workflow are added. Existing workflow
path filters do not match these additions; portable checks target main only.

## Scope and invariants

- Standard GitHub-hosted macOS 15, preinstalled Xcode 16.4 / build 16F6,
  preinstalled iOS 18.5, one exactly owned disposable iPhone 16 Simulator.
- Read-only repository token, no persisted checkout credentials, secrets,
  signing identities, installation of tools/runtimes, security changes,
  paid runner, license acceptance, or external service calls.
- Original unsigned offline UIKit fixture only. App, observer, harness,
  production provider/model and helper are SHA-256 pinned before Apple tools.
- The original action/oracle block is unchanged and checked by AST regression:
  empty → editor → invalid → created → detail → back → cancel-editor → cancelled.
  There are nine gesture/text injections and at most eight OCR invocations.
- Exact original `state_matches`, `normalized_words`, `observe_frame`, confidence
  threshold, required/forbidden phrases and negative-oracle check are reused.
  No OCR normalization, image preprocessing, warmup, oracle relaxation or retries.
- Each state accepts at most its first OCR result. Mismatch, timeout, malformed
  result, health failure, or other failure stops the flow before later inputs.
  Pending/stale frame polling remains inside the original 25-second absolute
  state budget; it is not an OCR retry. Child timeout is min(15 seconds,
  remaining state budget), never renewed. Deadline checks follow slow phases.
- Original frame selection is retained: send records the current frame id before
  input. The pinned native helper serializes execute → wait → published frame →
  injected ACK. No additional post-ACK frame barrier or readiness delay is added.

## Intentional diagnostic-only differences

Only the original app is built, unlike the full PR #13 harness which also built
instrumented and Release variants before running the original flow. Thus this
replays the same app/action/oracle source, **not an identical pre-flow workload**.
A pass establishes this original fixture flow in this run; it cannot isolate the
historical timeout's cause or establish repeatable reliability.

Read-only metadata sampling occurs every 250 ms, capped at 1,200 samples / 300
seconds. It records provider/session health, frame ids, frame age/size, publication
history endpoints and timings. It never creates inputs, invokes OCR, transforms
frames or delays for readiness. Initial missing frames are reported without
changing baseline pending-frame behavior. Other unknown/inactive health latches
fail-closed. Health checks before inputs/frame selection and around OCR share the
existing state deadline; stopped providers cannot yield accepted states.

The source-pinned baseline observer implementation is reused with only diagnostic
one-shot selection, bounded evidence and timing added. Results retain all reached
state outcomes, each attempted frame's exact JPEG and digest-bound validated OCR
JSON when available. A timeout has a JPEG and structured stages/outcome, not a
fabricated OCR result. Unreached states have no observations. `observerCalls`
counts actual observer invocations, including launch failures, not merely frames.

Cleanup uses the prior bounded exact-owned cleanup pattern, not baseline
`run_flow`'s generic `close_all` or temporary-directory auto-deletion: close the
one session once while the bridge is live, verify provider exit/temp/lease and
session/device status, then stop server/store. Failed authority/quarantine state
is retained privately on the disposable host and never uploaded. The shell trap
separately verifies exact Simulator ownership, shuts it down, deletes only that
UUID and confirms absence. Simulator deletion never upgrades failed native cleanup.

## Evidence and local validation

The sole artifact directory is `$RUNNER_TEMP/original-flow-evidence/`: result JSON,
up to eight synthetic JPEGs and up to eight validated OCR JSON files. Native build
logs, process output, authority/lease/journal files and helper artifacts remain
outside it. Result JSON is bounded to 512 KiB; oversized sampled metadata is omitted
with a failed result instead of losing state/cleanup findings. No broad acceptance,
company-app, physical-device, semantic-locator or target-bound typing claim is made.

Run **only inert regressions** locally:

    python3 diagnostics/original-flow/run_tests.py

The runner installs an independent audit guard denying all real process/socket
operations and an independent Popen denial. Tests mock observer subprocesses,
frames, health and cleanup. Do not execute `run-ci.sh` or `run.py` locally.
The root task owns any approved remote publication and the one native CI run.
