# Android debug instrumentation templates

The host copies the existing `android/sdk/src/main/java/io/reproloop/sdk/ReproRecorder.kt`
into the target module's `src/debug/java/io/reproloop/sdk/`, then copies the files under
`android/debug/java/` into `src/debug/java/`. The release source set receives only
`android/release/java/io/reproloop/autotrace/ReproAuto.kt`; the debug manifest overlay
receives `android/debug/AndroidManifest.xml`. The release manifest is intentionally empty.
Before copying, the host must reject a target that already defines any of
`io.reproloop.sdk.ReproRecorder`, `io.reproloop.autotrace.ReproAuto`,
`io.reproloop.autotrace.AutoExportReceiver`, or `io.reproloop.autotrace.ReproConfig`;
overwriting prior instrumentation would make the source proof ambiguous.

The host must generate `io.reproloop.autotrace.ReproConfig` in the target debug source
set. `ReproConfig.kt.template` shows the package and fields. Values are compact JSON
strings with no source interpolation beyond a Kotlin triple-quoted literal:

```kotlin
internal object ReproConfig {
    const val PROFILE_JSON = """<full native profile JSON>"""
    const val PROFILE_DIGEST = "<full post-instrument profile SHA-256>"
    const val SITES_JSON = """[{"id":"s123abc","path":"...","line":1,"target":"...","kind":"tap"}]"""
}
```

`PROFILE_JSON` contains the package, fixture, start state, and target policy. The
profile's `targets.report` may be `null` for debug receiver mode; the runtime does not
insert or require a Report view. `SITES_JSON` is the PSI output pinned into the full
profile and every object has exactly `id`, `path`, `line`, `target`, and `kind: "tap"`.
The generated site id is a stable lower-case `s` followed by hexadecimal characters;
it must be passed unchanged to `beforeTap` and must map to the same configured target.

The Kotlin transformer wraps a tap handler without changing its exception semantics:

```kotlin
val reproToken = ReproAuto.beforeTap(this@MainActivity, "add", "s123abc")
try {
    addItem()
} catch (error: Throwable) {
    ReproAuto.threw(reproToken)
    throw error
} finally {
    ReproAuto.afterTap(reproToken)
}
```

Call `ReproAuto.start(this)` after the initial view hierarchy is rendered and
`ReproAuto.stop(this)` from the matching activity lifecycle cleanup. The runtime only
activates when the application is debuggable and the activity intent has
`repro_mode=record`. All hooks are no-throw and use a weak activity reference.

`AutoExportReceiver` is a debug-only exported receiver protected by
`android.permission.DUMP`. The explicit action is `io.reproloop.EXPORT_CAPTURE`.
Result code `0` means the asynchronous SDK freeze was accepted; result code `1` means
no active valid recording or a rejected request. Once the SDK callback has published
the finalized capture, the runtime atomically writes `files/repro/diagnostics.json`:

```json
{
  "schemaVersion": 1,
  "sessionId": "<SDK capture sessionId>",
  "appProfileDigest": "<ReproConfig.PROFILE_DIGEST>",
  "endSequence": 2,
  "actions": [{
    "eventId": "e2",
    "target": "add",
    "siteId": "s123abc",
    "before": {"count": "0"},
    "after": {"count": "1"},
    "outcome": "returned"
  }]
}
```

Only allowlisted text values (``, `QA`, `Test`) and decimal numeric values of one to
nine digits are retained. Invalid or unsafe observations invalidate the SDK capture
without affecting the application. Sidecar output is bounded to 500 actions and 1 MiB;
no exception text, arbitrary arguments, or logcat data is collected.
