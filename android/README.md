# Reproof Android MVP

This directory contains the Android side of the Reproof MVP: a small
semantic capture SDK, a deterministic sample app with buggy and fixed flavors,
and a dependency-free accessibility driver.

## Offline build

Use JDK 17 and the cached Android SDK. From this directory:

```sh
export JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew --offline assembleBuggyDebug assembleFixedDebug :driver:assembleDebug
```

The APKs are written under `sample/build/outputs/apk/` and
`driver/build/outputs/apk/`.

The focused behavior test is available for both flavors:

```sh
./gradlew --offline :sample:testFixedDebugUnitTest
```

`testBuggyDebugUnitTest` intentionally fails before the repair agent patches
`CounterLogic.kt`; after that patch it is the required regression check.

## Recording

Start the sample with `repro_mode=record` and `fixture_id=default`. The sample
annotates semantic actions (`replace`, `tap`, and `back`) with stable resource
entry names. The Report button freezes the session and writes an atomic
`capture.json` under the app-private `files/repro/<session-id>/` directory and
publishes the latest completed capture at `files/repro/capture.json` for the
host collector.
`freezeAndExport { capture -> ... }` now passes the session-specific file, not
that replaceable latest slot. Its sibling `session.json` has been finalized
before the callback runs on the recorder executor. A later session's export
does not replace this callback file or its sibling metadata. Both writers must
succeed before the callback is invoked; a `true` return means the asynchronous
request was accepted, not that export succeeded or that the session is complete.
Consumers must still check the capture and metadata's matching session identity
and completeness flags.

The callback's `(File) -> Unit` API and default callback are unchanged, but code
that assumed its file was the root latest slot must use `capturePath()` instead.
That legacy accessor still returns `files/repro/capture.json`, which another
session may replace; do not use it for delayed same-session consumption. The
wire format and latest-file publication are unchanged. Each atomic write now
uses an exclusively created temporary file beside its destination, so overlapping
recorders cannot publish or remove each other's temporary bytes. The file is
synced and closed before rename; a failed write, sync or rename leaves the prior
destination in place and attempts to remove only that call's temporary file.
Cleanup is best effort, so a crash or denied deletion can leave a temporary file.
Different recorder instances are not serialized: the last successful rename wins,
not necessarily the newest session. `current-session`, latest capture and session
metadata are independent writes, not a cross-file transaction. This does not add
directory syncing or a crash-durability guarantee beyond the existing file sync.

Events are appended as ordered JSONL as they happen, so a process killed before
Report still leaves a crash-recovery trail. Password-like targets are not in the
SDK allowlist, and the recorder stops when it reaches ten minutes or 20 MiB.

Replay mode is selected with `repro_mode=replay`; it does not create a recorder.

## Recorder privacy admission

`ReproPrivacyConfig` and `ReproStartState` provide an additive typed API:

```kotlin
val policy = ReproPrivacyConfig(
    safeTextTargets = setOf("name"),
    numericTargets = setOf("count"),
    allowedStartScreens = setOf("main"),
    tapTargets = setOf("add", "next"),
)
val start = ReproStartState("main", mapOf("name" to "", "count" to "0"))
val recorder = ReproRecorder(context, policy, start)
if (recorder.isEnabled) recorder.recordTap("add")
```

The integration owner must supply trusted semantic IDs, synthetic safe text
values and start-screen allowlists. Do not derive a policy from arbitrary user
input, raw JSON values or captured screen text. This is admission, not generic
redaction: non-allowlisted values, nested objects, extra/missing observation
nodes, wrong types and oversized input are rejected, not sanitized or echoed.
Default text values remain `""`, `"QA"` and `"Test"`; numeric start nodes allow
only one to nine ASCII digits. Numeric targets are not automatically text or tap
targets. Tap, replace, scroll and back have separate policies; a scroll endpoint
is tappable only if also explicitly in `tapTargets`.

Configuration IDs have at most 64 characters; each target/value set has at most
64 members, scroll maps at most 16 containers and start states at most 128 nodes.
Safe text values have at most 64 characters and cannot contain control characters.
Policy and state collections are defensively copied and exposed read-only.
The supplied start state must contain exactly the union of text and numeric
observation targets; where legacy default sets overlap, numeric validation wins.
A conservative start-state storage reserve must fit the capture byte budget.
Duration and byte budgets can be reduced from, but not exceed, the defaults.
Invalid typed policy/state construction throws `IllegalArgumentException` with
a fixed message, as a programmer configuration error.

The existing `JSONObject` constructor and named sample arguments remain source
compatible. Its raw adapter checks exact keys and types without calling
`toString` or coercive `getString` on untrusted input. Invalid legacy policy or
start state produces a disabled recorder (`isEnabled == false`) before JSON
serialization, context access, clocks, executor creation or filesystem work.
Recording and `freezeAndExport` then return false; the callback is not invoked.
`capturePath()` on a disabled recorder throws `IllegalStateException` with the
fixed message `Recorder is disabled`, rather than returning a stale capture.
Callers must inspect `isEnabled` and must not upload an older capture as a new
session. A typed, structurally valid state that does not match its policy also
disables the recorder. `isEnabled` describes configuration admission, not whether
an admitted session has subsequently stopped or successfully exported.

The version-1 capture and metadata wire keys are unchanged. Both source and
build instrumentation copy the single `ReproRecorder.kt` file, including its
support types. `ReproAuto` supplies numeric targets and the validated start
screen from the trusted host-generated profile, rather than inferring policy
from arbitrary raw state.

The existing Android compile workflow is extended to compile the SDK, both
sample variants and the live helper, and run `:sdk:testDebugUnitTest`. Its existing
pull-request and main-push path filters also cover Android instrumentation
templates. The SDK test source set compiles the actual debug runtime templates
with an inert test-only configuration; these files are not production SDK
sources and are not packaged in its AAR. These JVM
tests exercise the pure admission policy with mutation, bound and non-coercion
canaries and the actual export-orchestration helper with synthetic temporary
files. Export tests cover delayed and interleaved sessions, writer failures,
callback ordering/count/thread and exception propagation. Separate JVM tests call
the actual atomic-file helper, including its default writer, file sync and rename,
against temporary host files. A writer seam deterministically overlaps two calls
and injects partial-write and closed-descriptor sync failures; rename refusal and
temporary creation failure also preserve existing data. These tests check owned
temporary cleanup without deleting another writer's live file or an unrelated
temporary. They do not instantiate Android `Context` or `JSONObject` or run a
recorder, and host filesystem behavior does not prove Android device behavior.
Source-copy and ordering assertions are supporting Python checks, not runtime
proof. Real CI compilation/JVM results are required before merging this change. Native
Android JSON-adapter behavior, device recording and export remain unverified by
these tests; no device, installation, signing or production qualification is
claimed.

## Driver commands

Install `driver-debug.apk` and invoke it with Android instrumentation. The
driver requires a unique visible resource ID and uses accessibility actions only:

```sh
adb shell am instrument -w \
  -e op observe -e package io.reproof.sample \
  io.reproof.driver/.DriverInstrumentation
adb shell am instrument -w \
  -e op replace -e package io.reproof.sample \
  -e target name -e value QA io.reproof.driver/.DriverInstrumentation
adb shell am instrument -w \
  -e op tap -e package io.reproof.sample \
  -e target add io.reproof.driver/.DriverInstrumentation
```

Each invocation finishes with a compact JSON object in Bundle key `result`.
`observe` reports only nodes from the requested package and only exposes text
for the `name` and `count` allowlist entries. `scroll_to` is bounded to twenty
scroll actions and `back` uses the platform global back action.

The sample's observable fixture starts with `name=""` and `count="0"`.
Adding once displays `2` in `buggyDebug` and `1` in `fixedDebug`. The intentional
source condition is in `CounterLogic.kt`, making it straightforward for the
host agent to patch and rebuild.
