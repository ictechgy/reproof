# ReproofSDK: local Foundation annotation recorder

A self-contained Swift package for opt-in local recording, with no external
packages, UIKit hooks, network, logging, automatic collection or replay engine.
Minimum declared platforms are iOS 16 and macOS 13, Swift tools 5.9. The macOS
platform exists for Foundation tests, not a macOS UI recorder.

Add this directory as a local Swift package and link the `ReproofSDK` product.
The whole package lives inside `ios/`, so the repository's iOS-only staging and
installed-resource export preserve the dependency. `Consumer/` is a separate,
compile-only package that uses the public module without sample sources or
`@testable` access.

```swift
import Foundation
import ReproofSDK

let policy = CapturePolicy(
    screens: ["main"],
    startNodeValues: ["inventory.count": ["0"]],
    tapTargets: ["inventory.save"]
)
let recorder = ReproofRecorder(configuration: RecorderConfiguration(
    rootDirectory: applicationOwnedDirectory.appendingPathComponent("Reproof"),
    application: ApplicationMetadata(id: "example.inventory"),
    fixture: FixtureMetadata(id: "inventory-synthetic", version: 1),
    startState: StartState(screen: "main", nodes: ["inventory.count": "0"]),
    policy: policy
))
guard recorder.isEnabled else { /* configuration rejected without I/O */ return }
recorder.startIfNeeded()
recorder.recordTap(target: "inventory.save")
recorder.finish { result in
    if case let .finished(files) = result {
        // Collect these three same-session files together, not a root/latest path.
        useLocalFiles(files.capture, files.metadata, files.marker)
    }
}
```

The two application-specific names in the example, `applicationOwnedDirectory`
and `useLocalFiles`, are placeholders for your app's local storage/collection.
This is local annotation output, not an authorized upload or trusted replay.

## Explicit, bounded policy

All configuration fields are immutable value types. Construct policy from
reviewed synthetic/public constants, never from text entered by an end user.
An allowlist deliberately grants permission for its exact contents; it cannot
recognize secrets mistakenly placed into that allowlist. There is no generic
redaction or production-data safety promise.

- Screens and per-action targets are denied unless explicitly listed.
- Starting node keys must exactly equal the policy's node keys, and each value
  must be a member of that node's declared set. Fixture inputs are always empty
  in this initial version. App/fixture identity is caller-declared, not attested.
- Replace values are checked per target. Scroll permission binds the exact
  target, container and typed direction. Back and tap are independent policies.
- Semantic identifiers use ASCII letters, digits, dot, underscore and hyphen,
  1–128 bytes. Declared values use printable ASCII, 0–256 bytes. Comparisons are
  exact, case-sensitive membership, not patterns or Unicode normalization.
- Each identifier set, rule set and value map has at most 32 entries, and each
  target's value set at most 32 values. Empty value sets are invalid.
- Storage must be a local file URL with an absolute printable-ASCII path of at
  most 4096 bytes and no URL credentials, query or fragment. This conservative
  initial restriction means a Unicode directory path disables recording.
- Limits default to 256 events, 600000 ms and 20 MiB; configurable ranges are
  1–256 events, 1–600000 ms, and 512 KiB–20 MiB. Byte accounting reserves stream,
  metadata, both capture copies and atomic-write overhead. It is a per-session
  bound, not a total retention quota for the root directory.

Invalid configuration produces `isEnabled == false` and `.disabled` on start,
retains no configuration in the recorder, and performs no queue creation,
filesystem work, UUID generation or clock reads. Admission of runtime values
precedes clock reads and retained events/writer commands. A rejected event while
recording marks the session lost/truncated; later events are denied and finish
fails. No raw rejected value or platform error is returned or logged.

## Lifetime, ordering and finalization

The recorder is one-shot. Call `startIfNeeded()` explicitly. A single lock orders
metadata initialization, accepted event numbering and writer submissions; one
serial queue performs all writes. Accepted asynchronous commands retain the
recorder until that work completes, including when the caller releases it.
Events received after finish begins are `.inactive` and cannot enter the export.

The first finish callback is retained and called exactly once, outside the state
lock, on an unspecified thread. Additional finish calls while it is pending get
`.inactive` immediately and are not retained or queued. Calls after completion
receive the cached terminal result. Callbacks may reenter. UI clients must
schedule their own main-thread work. Releasing without finish still completes
accepted writes but does not finalize a capture. Lifecycle interruption, write
failure, time/event/byte overflow, or rejected content prevents successful export.

Wire keys remain iOS schema v2. Files are:

- `<root>/<uuid>/events.jsonl`: accepted events, ordered by sequence.
- `<root>/<uuid>/metadata.json`: initially unfinalized, committed last.
- `<root>/<uuid>/capture.json`: final capture, including loss/truncation flags.
- `<root>/<uuid>/finalized.json`: sequence marker, only for complete sessions.
- `<root>/capture.json`: sample compatibility convenience copy only.

`SessionExport` returns session-specific paths. The compatibility root capture
can be replaced by later recorders and is not a session-identity guarantee.
There is no coordination between instances/processes sharing a root, no
hostile-filesystem defense, retention cleanup or encrypted storage. Give each
active recorder its own application-owned root and collect the returned paths.
Do not race the SDK with external modifications of that directory.

A successful callback requires both captures, marker and final metadata writes
to succeed. Failures return only `.failed`, with best-effort marker removal and
unfinalized metadata. Atomic file writes are not a cross-file transaction or
power-loss durability guarantee; collectors must independently validate the
complete same-session trio and its content, never trust one marker or callback.

## Sample and trust boundaries

`../Recorder/Recorder.swift` is only the sample adapter. It snapshots `ReproCase`
and supplies the original application ID, fixture ID, start state, allowlists
and `Application Support/Reproof` root. The counter, duplicate-submit and reset
cases remain distinct, with reset permission only for the reset case.

Custom policies do not make the Python host accept arbitrary apps. Its replay
compiler still supports repository sample policies, and the SDK review bridge
still supports its narrower counter sample. An annotation is not an injected
input receipt, typed app log, authored oracle, session-correlation proof,
installed-build identity, approval or execution authority. No such evidence is
invented by this package. Company-app deployment/acceptance, UI behavior,
signing, devices, Simulator execution, security qualification and AI repair are
outside this extraction.

## Verification scope

On an approved Mac with preinstalled Xcode, `swift test --package-path ios/SDK`
executes the synthetic Foundation tests. Tests cover public import, exact wire
keys, policy/private canaries, bounds, action-specific denial, concurrency,
record/finish races, repeated/reentrant completion, caller-release lifetime and
injected initialization/append/finalization failures. `swift build --package-path
ios/SDK/Consumer` compiles the independent consumer without running it.

The checked-in macOS workflow checks preinstalled Xcode 16.4 build 16F6 and
Python 3.12 from the runner tool cache (fails if either is absent), records
nonzero passing XCTest execution, passes one synthetic counter export's exact
writer bytes to the unchanged sample-only Python host normalizer, compiles that
consumer, and compiles unsigned
Debug/Release sample builds for a generic Simulator destination. It installs no
tools, resolves no remote dependencies, boots no Simulator, and does not run an
iOS test/app/device/signing/release/security workflow. Its logs distinguish tests
from builds; raw test exports are not uploaded. A green workflow would establish
only these bounded checks, not trusted replay or company-app acceptance.

Portable `tests.test_ios_sdk_package` checks resource/wiring/staging and unchanged
host rejection. It does not compile or execute Swift. At authoring time in the
Linux workspace, Swift/Xcode execution was unavailable and no native checks or
workflow dispatch were performed; source tests and a prepared workflow are not
native acceptance evidence.
