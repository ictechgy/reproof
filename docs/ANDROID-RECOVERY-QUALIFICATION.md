# Android recovery qualification design (UNQUALIFIED)

This is a proposed, private qualification harness, not a production recovery
feature. General-device `recovery` remains false and `Lab.recover_device` is
unchanged. No CLI, environment flag, imported receipt or persisted harness
output can release ordinary Live quarantine. Actual dedicated disposable-AVD
qualification and independent review are required before a separate production
integration decision. No device execution is part of this source implementation.

## Explicit qualification launcher, not production activation

The private recovery transport requires an `IsolatedAdbContext`. Direct
construction, imported PIDs/sockets, JSON declarations and
`IsolatedAdbContext.birth()` cannot create one. The separate, explicitly
effectful `launch_qualification_birth` entry is the only source-level issuer:
it must create and retain its own new foreground ADB server and disposable AVD.
It is not called by the normal CLI, server, device inventory or recovery API.
A `BirthPlan` describes the selected public tools, image and two APKs before
execution. Constructing or inspecting a plan does not start a process.

**Do not invoke that launcher without separate runtime authorization.** Its
future effects include creating a private AVD/userdata and homes, opening owned
local sockets, booting an emulator and ADB server, possible temporary ADB
authentication/emulator-console keys, and first-installing exactly the selected
public sample and helper APKs. It accepts no existing AVD, user seed/snapshot,
imported credentials, arbitrary shell command or optional extra APK. First-time
provisioning is a separate birth phase: recovery still cannot install, reinstall,
clear app data or reset the device. Owned processes can be collected, but private
AVD data and key residue are explicitly retained; there is no recursive deletion
of executed output directories or claim of secret erasure.

The initial resource policy requests 2 GiB guest RAM and a 6 GiB data partition.
The emulator may impose its own minimum or allocate additional overlays; this
request is not a data-size hard cap.
Before creating output it requires free space of at least 8 GiB plus twice the
selected image bytes. This is a conservative admission floor, not a filesystem
quota or a promise that the emulator cannot consume additional space. Tool and
image selections are bounded closed file lists. Use of the owner expires after
a finite selected lifetime; process collection is explicit, not an automatic
background cleanup guarantee.

### Pinned ADB and discovery boundary

The supported Darwin ADB pin is Google's versioned **35.0.2 (12147458)** release:

- [Official archive](https://dl.google.com/android/repository/platform-tools_r35.0.2-darwin.zip),
  13,335,867 bytes, SHA-256
  `1820078db90bf21628d257ff052528af1c61bb48f754b3555648f5652fa35d78`.
- Its `platform-tools/adb` entry is 14,046,464 bytes, SHA-256
  `9e78ca23f20ce357d156704d0eb706f90056d15310524881a1aec532be3acec3`.
- The publisher's [35.0.2 tag](https://android.googlesource.com/platform/packages/modules/adb/+/refs/tags/platform-tools-35.0.2)
  associates that release/build with source commit
  `ce9ea51f30f69f7560db692f7cb4d5d5502c3653`.

These hashes were computed from the official HTTPS distribution bytes; they are
not claimed to be a vendor signature or a reproducible-build attestation. The
source implementation reads and validates an already selected binary; it does
not automatically download or install SDK tools. The matching tag's
`client/main.cpp` contains the `ADB_USB=0`, `ADB_MDNS=0` and `ADB_EMU=0` startup
branches. The fixed environment omits `ADBHOST` and all ambient ADB selections.
Foreground `server nodaemon` uses only the owned endpoint. Explicit registration
is limited to the newly owned emulator's port; no general discovery/connect or
default-port fallback is allowed. A registration message has no success reply
and alone is not identity or boot evidence.

An explicit ADB host/port alone is insufficient: the client's version-mismatch
branch can kill/restart a daemon. The finite gateway rejects daemon management,
checks protocol version 41 and limits the selected transport. Private client
sandboxing denies child creation and network access outside that gateway.
The launcher's additional per-process policies are restrictions, not changes to
system-wide security settings. Their actual Darwin behavior, discovery-negative
checks and the eventual lost-ACK recovery trial still require separately
approved runtime qualification. Source checks and synthetic mocks do not prove
those runtime properties; normal recovery remains disabled.

### Socket cleanup operating assumptions

Each gateway uses a fresh owned 0700 namespace outside the sandbox-writable
client work directory, with retained parent/root descriptors. Creation has one
trusted writer. This does not promise isolation from an actively malicious
same-UID process. The listener retains the created socket while workers drain;
cleanup checks the bound namespace and exact socket, unlinks only that socket,
and removes only the empty owned directory. Foreign entries, replacements or
unconfirmed workers are preserved and reported as `cleanupIncomplete`.
A bounded namespace scan cannot allocate from an arbitrarily large directory.

Reported Mac metadata includes platforms/build-tools 34/35/36, JDK 17 and API36
images. The first launcher contract accepts only a pinned offline AOSP API36
arm64 `default` revision-2 image, not Google Play or `google_apis` images.
The supported official archive is
[`arm64-v8a-36_r02.zip`](https://dl.google.com/android/repository/sys-img/android/arm64-v8a-36_r02.zip):
810,736,863 compressed bytes; the official repository metadata lists SHA-1
`62ad6714df790f89c8a8ad32552ffe20bb16fe87`. Its central directory declares
20 regular files totaling 2,039,419,809 expanded bytes. The launcher checks that
exact relative-path/size layout plus API/ABI/tag/revision properties and each
operator-selected file digest. Central-directory inspection alone is not full
archive integrity verification. The archive has `data/empty_data_disk`, not
`userdata.img`; fresh userdata is created by the newly owned emulator. Existing
userdata, snapshots, arbitrary seeds, or SDK-manager-added files are not accepted. The reported
installed Google images therefore do not establish this prerequisite; acquiring
an additional image is not authorized or performed by this change. Exact
minor/tool hashes, signed fixture artifacts and actual launcher behavior remain
to be checked for an authorized trial. Nothing in these source
changes authorizes touching an existing AVD, generating keys, installing APKs or
changing the user's computer.

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

## One-shot deliberate ACK-delivery omission trial

The separate `reproof.live.android_recovery_trial.run_one_lost_ack` function is
an effectful **qualification-only test driver**, not a normal recovery command.
It is not imported by the ordinary CLI, server, worker, provider or Lab API.
It accepts a reviewed `BirthPlan`, the matching public sample runtime profile,
and a cancellation `threading.Event`. The plan must select at most 900 seconds;
its existing RAM/data/disk-admission bounds still apply. It creates only its own
new environment and first provisions the two selected public APKs. It uses the
existing bounded project grant issuer with real host clocks, not test clocks or
fabricated native receipts.

After startup and a fresh frame, the driver sends one center tap. A private
wrapper around the actual qualification bridge checks that the terminal ACK
matches the exact operation ID, complete authority mapping, expected shape,
`ok:true` and timing. It then deliberately omits delivery of that single response
by raising an input transport error. Pending, malformed, rejected or mismatched
ACKs are not counted as the intended omission. The ordinary `Lab.input` error
path must produce an `unknown` receipt and quarantine; the driver never calls
`Lab.fail` or writes a substitute input receipt. The wrapper is removed before
recovery. The observer's knowledge of the ACK **never upgrades the original
unknown input outcome**.

This tests *intentional omission of an observed response in the actual transport
path*. It does not prove naturally occurring packet loss, an application defect,
reproducibility or general recovery qualification. A passing report requires:

1. Exactly one matched terminal response was deliberately withheld.
2. The original receipt remains unknown, `knownStart` remains false, and the
   private Lab remains quarantined throughout and after reconciliation.
3. Existing authenticated old-producer retirement/exit, fresh probe frame,
   probe retirement/exit, and durable private authority release all complete.
4. Local pollers join and all retained client/server/emulator processes collect.

`TrialIncomplete` retains its bounded report and live owner/harness references
when any step fails. Cancellation is shared with the birth context and recovery
issuer. Local pollers are stopped/joined before abort collection. `OwnedBirth.abort`
can collect only its retained local process handles after cancellation, expiry
or failure; it sends no recovery/helper command, releases no authority, changes
no device availability and never claims recovery success. Reaped numeric PIDs
are not signalled; uncertain descriptor closes are never retried by bare FD
number. Uncollected resources and private data remain explicit residues. There
is no automatic recursive deletion or secret-erasure claim. Report cleanup fields
cover only local processes and pollers; `authorityDisposition`,
`liveDiagnosticOwnerRetained` and `diagnosticStateRetained` separately disclose
unconfirmed authority release/open diagnostic ownership and retained state. Local abort
success must not be interpreted as complete resource or authority disposal.

Invocation, **only after a separately approved runtime plan**, is deliberately
small; input selection/build/signing is not hidden in this call:

```python
import threading
from reproof.live.android_recovery_trial import run_one_lost_ack

# plan: reviewed BirthPlan for fixed public tools/image/APKs and a fresh output.
# profile: validated runtime profile bound to that selected public sample APK.
# The caller owns cancellation and must preserve TrialIncomplete.report/owners.
# probe: pinned trusted build of the bundled public C sandbox probe.
result = run_one_lost_ack(plan, profile, sandbox_probe=probe,
                          cancellation=threading.Event())
```

The future permission bundle must explicitly cover the exact selected emulator,
AOSP image, aapt/sandbox binaries, pinned ADB, public sample/helper source and
signed APK hashes, the fresh private output/ports, temporary ADB/console auth
material, boot, first installation, one tap, deliberate ACK omission, recovery,
and owned-process termination. If signing keys or the AOSP image must first be
created/downloaded, those are separate preparation effects, not implicit trial
steps. No such runtime/build/signing/download permission is granted by these
source changes. Darwin sandbox/discovery-negative checks and actual one-shot
execution remain pending; passing synthetic host tests does not satisfy them.


### Bounded preparation proposal (not runtime authorization)

For a separately approved single trial, proposed preparation limits are 1 GiB
of total downloaded archives and 2.5 GiB of expanded AOSP image files. Before
extraction, inspect the bounded central directory, reject links/unsafe paths,
and enforce both declared totals and actual streaming output limits. Do not
start an unlimited SDK installation to discover its size. The official ADB
archive was read and hashed in the cloud only; downloading or executing it on
the Mac is a separate future effect.

Start with at least 16 GiB free disk. Monitor the newly owned runtime output
against an 8 GiB planning budget and stop only its owned processes if that
budget is exceeded or free space drops below 8 GiB. Runtime monitoring can
observe transient overshoot; it is not an OS quota or a hard allocation bound.
No disk image, system quota change, or existing AVD cleanup is required.
Retain private output and uncertain cleanup evidence rather than deleting
executed trees. One trial has a 15-minute owner deadline; preparation/download
and any public probe/APK signing steps require their own bounded approved plan.

The Android compile workflow separately produces two **unsigned** public APKs
(sample buggy variant and helper), with source commit, package, size and SHA-256
manifest. It verifies unsigned output and absence of generated key stores in
its fresh build homes. These are not installable signed runtime qualification
artifacts: signing with a new temporary key, ADB authentication material,
emulator-console authentication material and all Mac execution remain separate
future approval items. No company source or credentials are inputs.


### Process-policy preflight before a trial

`run_one_lost_ack` requires `sandbox_probe`, a pinned trusted build of the
bundled public `scripts/android-recovery-sandbox-probe.c`. Compiling that tiny
probe is a separate, future approved preparation action. The trial directly
calls `run_sandbox_preflight`; it does not accept an imported pass receipt or
caller boolean. Failure occurs before any ADB server, AVD, or authority session
is created and retains the preflight's owned cleanup handles/evidence.

The preflight opens only fresh owned sentinel endpoints and files. For server,
emulator and client policy generators it compares an unrestricted probe control
with a restricted probe. Allowed owned file/network access must succeed, while
outside owned sentinel file/network access and unlisted executable launch must
fail. Only the probe's exact read/execute admission is added to the generated
policy; network/write restrictions are unchanged. These are parameterized
**derivative probe policies**, not execution of the real ADB/emulator binaries
and not proof that ADB discovery is disabled. The latter still depends on the
fixed binary/env constraints and the separately approved actual trial.

Probe processes have bounded deadlines/output and are collected through owned
handles. Its private namespace is retained, not recursively deleted. A failed
or uncertain collection is not a pass. Cloud synthetic tests validate the
control flow only; actual Darwin enforcement and the real recovery trial have
not been run by this source change. No system-wide security setting is changed.

### Bounded preflight failure diagnostics

The public probe writes fixed stage markers to its existing stderr pipe before
each sentinel action. A marker establishes only that this probe reached that
stage, not the cause of a later failure. No observed marker means entry was not
observed; it does not by itself prove that main was never entered.

PreflightIncomplete.report contains at most six fixed-role records with host
phase, failure category, return code/signal, bounded stream byte counts/digests,
collector completion/truncation and the last valid ordered probe marker. It
contains no raw stdout/stderr, argv, environment, filesystem path or policy
text. Raw bounded collectors remain private on the retained owner. Diagnostic
evidence does not bypass any success check or authorize a retry. A SIGABRT
during loader initialization is not an identified denied path or a recovery
result.

### Shared Darwin loader reads

The private producer policies and existing scoped ADB client now use one
read-only expression for the same fixed OS library/Cryptex aliases and ancestor
directory literals. All four corresponding system Keychain directory paths
remain explicitly denied for data/xattr reads. No network, writable directory,
executable, discovery or recovery authorization is added by that helper.

This aligns an observed policy mismatch: the producer previously omitted paths
already present in the client policy. A separately authorized first trial
stopped at the server's restricted public probe with SIGABRT; the retained
stack entered dyld CacheFinder/ignite/boot_boot/ignition_halt. That supports a
loader-read hypothesis, but no exact denied path was observed. Commonizing the
allowlist is not proof of the crash cause or a passing runtime test. The original
attempt remains failed before ADB/AVD startup, and another Mac execution still
requires separate authorization.


### One local public CA input is not a credential-transfer exception

The operator-recorded emulator 36.1.9/build 13823996 selection includes a public
`lib/ca-bundle.pem`. The original generic file opener intentionally rejected all
PEM paths as potentially private credentials; reusing it for this local SDK
input therefore prevented preparation before any trial/native action.

The private tool-tree verifier recognizes only this source-fixed declaration:

| Selected member | Bytes | SHA-256 |
|---|---:|---|
| `lib/ca-bundle.pem` | 258424 | `0f119da204025da7808273fab42ed8e030cafb5c7ea4e1deda4e75f066f528fb` |
| `emulator` | 1694800 | `7e8559aac63105502a6a4fec03dcc0cc7aaaf6f8e1e046bcad29095d46bd26eb` |
| `source.properties` | 102 | `1b29166b5f4c5e690859fed6b03c6e37fbfe77e7f103d52ae21796844a91d492` |

All three identities must match the immutable selection, the emulator must be
an explicitly selected executable, and the CA must not be an executable. These
recorded pins identify an approved local selection; they are not independent
vendor provenance or a vendor signature. A different tool version/bundle needs
separate explicit pin review, not an operator-supplied generic PEM exemption.

Before opening any selected content, every selected path is checked against the
unchanged general path policy, with this single context-bound exception. The
local opener rejects symlinks, multiple hardlinks and non-regular files, checks
content hash/size/stamps, and reopens the exact path to check identity. The tree
holds its root descriptor throughout verification; the CA descriptor remains
open through parsing and rebound checks. Cancellation/deadline checks remain.

The bundle is bounded to 1 MiB, 16,384 lines and 512 certificate blocks (64 KiB
each). Only CERTIFICATE PEM blocks are accepted, PRIVATE KEY markers are
rejected, and a fresh in-memory SSLContext parses the certificate blocks.
Bounded printable UTF-8 comments/titles outside PEM blocks are allowed; PEM
labels and base64 bodies remain strictly ASCII. No default
trust store is loaded, no global trust is changed, and no network connection is
made. Certificate parsing alone can never authorize another path/hash.

Ordinary PinnedFile verification, source/BlobSet/wire transfer, external AI and
upload policies still reject PEM/key/credential paths. The public CA bytes are
not packaged, transferred or returned by this verifier. The recorded 324-file
emulator plus 20-file r2 image metadata audit found this as the only path-policy
conflict; synthetic tests additionally reject other credential suffixes before
any selected file read. Actual Mac retry and full tool-tree/runtime verification
remain separate qualifications. Reuse of an older sandbox probe binary is not
implied: callers must still select the newly compiled stage-marker probe.

### Explicit private SDK anchor and image resolution

A subsequent approved trial passed the complete selected tool/image verification
and all six sandbox probes, but the emulator launcher stopped before boot:
`ANDROID_SDK_ROOT` was absent and its inferred host SDK path was not readable.
No install, tap or recovery ran. This is a configuration failure, not evidence
that recovery succeeded or that a sandbox boundary should be removed.

Birth now creates a fresh private `sdk` directory containing only two empty
folders, `platforms` and `platform-tools`. Both `ANDROID_HOME` and
`ANDROID_SDK_ROOT` are set to that same owned anchor **only for the emulator**.
No host SDK is exposed, imported, symlinked or searched by this configuration;
no SDK tools, especially `platform-tools/adb`, are put in the anchor. The
launcher receives `-sysdir` equal to the already hash-verified selected image
root, also retained as the absolute `image.sysdir.1` in the fresh AVD config.

The source basis is `ConfigDirs::isValidSdkRoot` (readable root plus those two
folders), launcher `getAvdSystemPath`, and backend `_avdInfo_getSearchPaths`
(explicit sysdir is the sole SDK image search directory). See the
[AOSP source mirror snapshot](https://github.com/mirror/qemu-android/blob/e111ba9b1d03f295d5a208242cf9698cfaa29c4e/android/emu/files/src/android/emulation/ConfigDirs.cpp),
[backend image search](https://github.com/mirror/qemu-android/blob/e111ba9b1d03f295d5a208242cf9698cfaa29c4e/android/emu/avd/src/android/avd/info.c),
and [official launcher source](https://android.googlesource.com/platform/external/qemu/+/refs/heads/emu-master-dev/android/emulator/main-emulator.cpp).
These source checks are not a reproducible-build attestation for the selected
36.1.9 binary and do not replace a separately approved runtime trial.

Before any birth listener or process starts, and again before the emulator
starts, a local static check compares the generated AVD config and index bytes,
checks all three SDK directories against retained descriptors, and rejects
missing, replaced, symlinked or non-empty SDK children. Native write/exec/network
policies are unchanged: the anchor is under the already readable owned root
and outside every native writable subtree. The image remains read-only; fresh
userdata stays in the new AVD with the existing `6144M` size setting. Kernel,
ramdisk, system, vendor and `data/empty_data_disk` remain mandatory members of
the closed revision-2 image manifest; no existing AVD or userdata is imported.
Static checks and synthetic lifecycle tests establish configuration wiring,
not successful image initialization, boot, hypervisor or recovery execution.

### Incremental warnings and private startup diagnostics

One SDK-fixed trial reported emulator boot after 19.3 seconds, but the private
host inventory/boot confirmation reached its 180-second limit. It did not
install the fixtures or run input/recovery. A buffered `read(8192)` delayed
short stderr output until exit; the old trial's missing inventory observations
cannot be reconstructed as device/boot evidence. A private ad hoc raw-log
excerpt also failed to hide a temporary **public** ADB key. Do not publish or
paste retained raw stdout/stderr, even after pathname or keyword redaction.

The private birth/preflight child collector now uses `read1(8192)`: available
pipe bytes are consumed without waiting for a full buffer or EOF. Retention is
still limited to 65,537 bytes per stream; over-limit/read-failure and fixed
warning/error/fatal/permission-denied classifications latch an alert. The
shared signing collector is unchanged. Birth checks the alert before starting
another process, during long verification, in live-owner checks and through a
composite cancellation token for bounded shell queries. Receive polling checks
at most every 100 ms while waiting for response bytes; connect/send and the
callback EOF check can still wait up to two seconds. This is fail-fast at
bounded host cancellation points, **not** a hard real-time guarantee or proof
of when the native program originally emitted a diagnostic. Detection never
changes the caller's cancellation event, original uncertain input, authority
release or recovery verdict. Cleanup still collects only owned processes.

Public failure summaries expose only fixed stages/classifications, return codes,
byte counts/hashes, collector completeness and selected-device inventory enums.
The last inventory response is reduced to an enum, byte count and SHA-256 with
an attempt counter; raw serials and lines are not copied. Boot property outcomes
are separate from inventory state. No response means `not-observed`, not an
inferred empty inventory. These observations are diagnostics, not authenticated
recovery receipts. `BirthIncomplete.report` and the trial's `birthDiagnostics`
are the supported inspection interface; raw collectors remain private evidence.

Console ports are now restricted to even 5554–5584 (paired ADB 5555–5585), inside
the emulator's recommended range. The private fixture disables only optional
`BluetoothEmulation`, `Uwb` and `WiFiPacketStream` features with a fixed `-feature`
argument. It does not grant netsimd execution or broaden any sandbox network,
write or executable permission. AOSP's
[device feature gates](https://android.googlesource.com/platform/external/qemu/+/emu-master-dev/android-qemu2-glue/main.cpp)
and [feature defaults](https://android.googlesource.com/platform/external/qemu/+/emu-master-dev/android/data/advancedFeatures.ini)
provide source-level support; they do not prove that the selected binary never
starts another optional service. Unsupported-option warnings or another denied
execution still stop the trial rather than triggering a permission fallback.
Actual compatibility and recovery remain subject to a separately approved trial.

Absence of a warning classification is not proof of the absence of denied operations: a native
process can suppress or buffer its own diagnostics before the host receives them.
