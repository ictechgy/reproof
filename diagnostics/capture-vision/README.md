# Four-observation capture / Vision comparison

This is a narrowly scoped diagnostic, not native acceptance or a reliability gate.
It is intended for one public same-repository pull request opened against
`codex/owned-native-runtime-acceptance`. The new workflow handles `opened` only,
refuses run attempts other than 1, has `contents: read` only, and has no secrets,
manual dispatch, pushes, KVM, external AI, signing, SDK installs or publication.
Do not rerun this workflow without new authorization.

## Exact experiment

The seven files under `diagnostics/offline-vision` are copied byte-for-byte from
`cf1a43d3c15e0152f405f226ec0efc9bd9c264e6`. Its workflow is deliberately absent.
The original `ObserveFrame.swift` and replay parser/oracle are unchanged. Input
bytes and SHA-256 hashes are checked before Apple commands. Both archived JPEGs
are copied unchanged into a private directory. The observer is compiled once.

On GitHub-hosted `macos-15`, using preinstalled Xcode 16.4 / build 16F6:

1. Create and boot exactly one uniquely named owned iPhone 16 / iOS 18.5
   Simulator. Compile the existing helper and the original, unsigned offline
   UIKit fixture. Do not prepare an instrumented app or run the original QA flow.
2. Start one real Reproof Live session through its existing install-identity,
   authority, protocol-v2 native-handshake and authenticated bridge APIs. Capture
   the fixture's initial screen. Do not tap/type or attempt to recreate either
   archived screenshot's UI state.
3. Analyze archived `03-invalid` once, then archived `04-created` once while that
   same capture session is demonstrably active. Both original observer process
   timeouts remain exactly 15 seconds, including their original failure/oracle
   rules. Timeouts do not trigger retries or block the other planned observation
   if capture evidence remains valid.
4. Keep the bridge alive for one exact-session cleanup. Require session `closed`,
   device `available`, provider stop set, runner exit 0, and provider temporary
   state / borrowed lease cleared. Then shut down and verify the exact owned
   Simulator UUID, name, runtime and device type in `Shutdown` state.
5. Analyze the same two archived JPEGs once each, unchanged, after those checks.
   There are exactly four observer process calls on a valid completed comparison.

A failed prerequisite may produce fewer than four calls. There is no fallback
that relabels inactive/stale/unknown capture as “active,” no compensating extra
call, and no stopped baseline after unconfirmed cleanup or shutdown. Every actual
observer call is counted, including a timeout or launch failure. There is no
warmup OCR, live-frame OCR, crop, resize, alternate backend or timeout extension.

## Active-capture evidence

A separate bounded metadata-only monitor reads accepted `frameHistory`,
`frameSequence`, frame byte count, dimensions and host receipt timestamp under
the session frame lock. It checks the same session/provider/handshake, owned
runner liveness, stop flag and installation digest. It does not request a new
screenshot or invoke OCR. A cached `Lab.frame()` response alone is not evidence.

Polls run every 0.25 seconds, with a 1-second maximum sampling gap, a 2-second
maximum frame age/publication gap, a 45-second window and at most 200 samples.
Each observer process-call interval must contain at least two strictly interior,
increasing accepted frame publications. Boundary-only, pre-call, post-call,
single-frame or frozen-cache evidence fails closed. An observer returning too
quickly for this evidence makes that condition inconclusive; the diagnostic does
not delay its return, retry, add observations or inflate its budget.

This is bounded sampled activity evidence, not mathematical proof of uninterrupted
capture between every sample. An inactive or unknown state latches failure and
cannot become valid again during the run.

## Cleanup and data boundary

Exact-session cleanup is attempted once; there is no `close_all`, recover, global
shutdown/delete, or forced release. If cleanup is unconfirmed, authority and
quarantine state remain private on the disposable runner. Best-effort shutdown
and deletion of only the owned Simulator are separate and do not turn failed
session cleanup into a successful baseline. The Simulator identity is rechecked
before mutations. No user devices, accounts or production apps are involved.

Only `capture-vision-report.json` is uploaded (seven-day retention). It contains
bounded metadata, hashes, host versions, attempt counts, original stage timings,
oracle outcomes, sampled capture cadence and cleanup/teardown states. Raw OCR
text, live JPEGs, raw native/compiler logs, tokens, private config, authority
journals, databases and environment dumps are not published. Setup/build logs
and native state remain outside that artifact. `nativeAcceptance` is always
`false`, including successful comparisons.

## Inference limits

The active workload is initial-screen capture, not the original created-state
screen or input flow. Active runs first and stopped runs second, so cache, process
ordering and warming effects are not controlled. One four-call run can indicate
condition sensitivity but cannot establish causal CPU/GPU/capture defects or
repeatable reliability. Neither success nor a different failure reclassifies any
previous native acceptance result.

## Inert tests

Run from the repository root:

```sh
python3 diagnostics/capture-vision/run_tests.py
bash -n diagnostics/capture-vision/run-ci.sh
```

The runner denies real `Popen` calls by default and installs an independent Python
process/socket audit hook. Tests use explicit fakes, never `mock.stopall`, native tools,
a real provider, a Simulator, a socket or a network service. It runs all 82
applicable original archived tests unchanged. The three assertions for the old
workflow are deliberately replaced by tests for this new workflow; copying that
workflow would accidentally authorize two additional observations. Comparison
regressions cover cached/early-ended capture, strict intervals, source pins,
15-second timeout accounting, exact call order, cleanup/quarantine preservation,
identity mismatches, bounded output and redaction.

No native experiment has been executed by these tests. Actual hosted results must
be reviewed separately; an inert pass cannot establish Apple Vision behavior.
