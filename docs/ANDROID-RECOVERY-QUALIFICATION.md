# Android recovery qualification design (UNQUALIFIED)

This is a proposed, private qualification harness, not a production recovery
feature. General-device `recovery` remains false and `Lab.recover_device` is
unchanged. No CLI, environment flag, imported receipt or persisted harness
output can release ordinary Live quarantine. Actual dedicated disposable-AVD
qualification and independent review are required before a separate production
integration decision. No device execution is part of this source implementation.

## Scope and trust boundary

Only a new harness-owned Android general-runtime-profile shared-v2 session is
eligible. The same process must retain its original provider, authenticated
transport, authority handle and exclusive device lease. Legacy, iOS, app-owned
fixtures, protected operations, changed app/helper/profile identity, old sessions
and loss of that live ownership are unsupported. Restart reads diagnostics only;
it cannot reconstruct capabilities or resume native effects.

The harness has its own fresh private authority/Lab state. It does not mutate an
ordinary service's availability. Existing normal dispatch revocation is retained.
Recovery authorization is a distinct process-owned capability issued only while
holding the exact NativeRecoveryLease under a fresh trusted parent grant. It is
not a reusable DispatchPermit or a new credential. Authenticated transport binds
receipts to the selected helper; JSON digests alone are not origin proof.

## Finite actions

1. **old-retire:** close normal command, inspect and media admission atomically;
   detach queued/unstarted work; wait for inflight command completion; cancel
   pointers; stop and join capture/encoder/watchdog producers; drain accepted
   inspection/media work and prohibit any later publication.
2. **old-exit:** accepted only after the terminal retirement receipt. Collect
   the owned instrumentation client and establish remote helper termination
   under the retained lease. Neither local adb exit nor remote process absence
   alone is sufficient. No stop/restart is allowed to substitute for a missing
   terminal drain receipt.
3. **fresh-probe:** start a distinct helper incarnation against the unchanged
   already-installed helper/app artifacts. Its recovery-only mode refuses normal
   commands. It may relaunch only the exact configured target and acquire one
   fresh foreground-target frame. No install, data clear, reset contract, fixture,
   arbitrary command, target, path, launch argument or environment is accepted.
4. **probe-retire / probe-exit:** apply the same terminal barrier and exit checks
   to the fresh probe. A fresh frame alone is not restoration proof.
5. **reconcile / release:** after all native owners have been collected, record
   bounded evidence and exact dispositions in the private authority journal,
   reconcile under the fresh grant and release that private lease last.

Old-retirement grants cannot launch/capture a probe. Probe grants cannot issue
ordinary input or control a different incarnation. Every grant is bound to its
finite purpose, attempt, session, device, app/artifact/profile/helper identities,
prior ownership generation, host/provider/helper/native incarnations, native
clock and equal-or-earlier deadline. Reusing ordinary expired permits or the
pointer-only autonomous cleanup exception is forbidden.

## Durable state machine

`session-bound -> producer-running -> quarantined -> old-terminal -> old-exited
-> probe-running -> probe-frame -> probe-terminal -> probe-exited -> reconciled
-> released`

Each dispatch has a durable intent before the effect and a compare-and-swap
completion bound to the same attempt and prior generation. Failure, cancellation,
missing evidence or interrupted publication retains quarantine. Late completion
cannot revive a failed attempt. A restart retains the interrupted state and
reports unsupported live-owner loss; there is no recovery-journal token persistence or resume.
Concurrent attempts, changed duplicate requests and cross-session receipts fail.

A terminal retirement receipt is produced only after the actual barriers finish.
It binds the frozen admission watermark, queued-work disposition and measured
command/pointer/capture/inspection/publication termination flags. The polling
endpoint is control-only and performs no Android observation. Its response is
not process-exit proof. Interrupted or timed-out worker termination stays unknown.

Original uncertain input outcomes remain unknown. A `recovered` disposition
means only controlled-environment restoration; it does not mean that the old
input succeeded or failed. Neither recovery nor its receipt changes `knownStart`,
recording replayability, fixture state, defect verdicts or backend determinism.

## Validation plan

Host tests use real private HostAuthority/lease/journal behavior and synthetic
native transports. Cover wrong generation/identity, expired/revoked grants,
concurrent attempts, every crash boundary, cancellation before/after dispatch,
late callbacks, duplicate receipts, restart, changed artifacts and incomplete
producer exit. Assert ordinary API/capabilities and normal journals unchanged.
Native pure-JVM tests exercise finite grant validation, admission races and
barrier completion, including interrupted-but-live workers and blocked media.
Compile the actual helper in ordinary CI. Neither host mocks nor compilation
qualifies Android runtime behavior. A later explicitly authorized disposable-AVD
run must establish real command/pointer/capture termination and remote helper
exit before production integration can be considered.

## Frozen private wire and resource envelope

The optional helper-only `qualificationRecovery` configuration contains
`configurationBase64` and its SHA-256 `configurationDigest`. Decoded fields are
exactly `schemaVersion:1`, `kind:reproof-android-recovery-qualification`,
`phase:session|probe`, attempt/session IDs, device scope digest, logical
`applicationId` (not Android package), artifact/profile/helper digests and prior
ownership generation. The already validated native runtime profile separately
binds the target package/component. The target cannot be `io.reproof.live`.
Only an explicit `emulator-<port>` endpoint with measured `ro.kernel.qemu=1` is
accepted. Existing installed single-APK identities must match; this harness does
not install the helper or app. Fixtures, SDK recording, automatic app logs and
approved-launch extras are excluded. Provisioning a public synthetic app on a
new disposable AVD is a separate, explicitly authorized runtime step.

The independent handshake requires `recoveryQualificationVersion:1`,
`recoveryQualificationConfigurationDigest`, and `nativeProcessIdentity` with
positive integer `pid` and nonzero decimal-string `startTicks` (at most 20
characters), measured from a fixed bounded `/proc/self/stat` read. Unreadable or
ambiguous identity fails; no escalation or alternate identity is attempted.

`POST /qualification-recovery` carries only a strict `grant`: version, finite
purpose, operation ID, sequence, configuration/attempt/session binding, prior
and reserved candidate generations, host/helper/provider/native incarnations,
native clock/deadline and the fixed purpose/configuration payload digest. The
candidate generation is **not** ordinary ownership; only durable reconciliation
advances it. `GET /qualification-recovery/<operationId>` returns pending, unknown,
or an exact terminal receipt. The receipt binds the exact grant, measured native
process identity, terminal native timestamp, kind and typed evidence:

- Retirement: exact true admission/queue/inflight/pointer/capture/media/publication
  barrier results, frozen admission watermark and optional detached command ID.
- Probe: foreground status, native frame/geometry IDs, bounded dimensions,
  capture timestamps, full encoded-frame byte count and SHA-256. The owned host
  adapter fetches the matching bytes and checks both digest and decoded metadata.
- Exit: `exit-accepted` with empty evidence; this is not process-exit proof.

Control polling does not invoke normal Android observations. A pending poll
cannot count as delivery of a terminal exit receipt. Remote PID/start identity
is rechecked before exit dispatch; authenticated terminal/exit evidence,
helper-only stop if still present, remote absence and owned client/poller
collection are all required. A different process fails instead of being stopped.

One recovery attempt is allowed per harness. Each phase is bounded by 30 seconds
and the current parent grant; the whole attempt by 180 seconds. The diagnostic
journal has at most 32 records, 8 KiB per record and 64 KiB total. Receipts are
bounded at 16 KiB; no raw frames, bearer tokens, inputs or app logs are journaled.
Native frame limits remain 3 MiB JPEG, existing header bounds and 8,294,400 output
pixels; APK checks retain the 150 MiB cap. Phase failure, interrupted producer
or missing evidence can leave private output and live owners retained. No
recursive cleanup or claim of successful disposal follows such a failure.

The existing helper startup mechanism writes a temporary bearer token in the
helper's private `files/live-config.json`. This is not a new signer or persistent
recovery credential. The journal never stores it and restart never reloads it.
The probe uses a fresh token; collected host transports clear theirs. The helper
configuration file can remain after exit until another startup overwrites it;
this change does not delete it or claim secret erasure.

## Current source qualification versus production support

The private harness exercises real host Lab/authority/lease/journal transitions
with synthetic transport responses in portable tests. Native tests compile and
execute the actual pure-Kotlin protocol and barrier helpers; the actual Android
helper must also compile in CI. None of these establish real Android process,
gesture, Binder, screenshot or app behavior. **T18 is not complete.** Normal
sessions, normal recovery API, advertised capabilities and ordinary persisted
availability are unchanged. Even the private Lab remains quarantined after the
private authority release; its output is evidence for review, not an importable
production unlock. Restart is diagnostic-only at every journal phase.
