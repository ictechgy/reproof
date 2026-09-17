# Repro Loop Android MVP

This directory contains the Android side of the Repro Loop MVP: a small
semantic capture SDK, a deterministic sample app with buggy and fixed flavors,
and a dependency-free accessibility driver.

## Offline build

Use JDK 17 and the cached Android SDK. From this directory:

```sh
export JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
export ANDROID_HOME=/Users/repro/Library/Android/sdk
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
Events are appended as ordered JSONL as they happen, so a process killed before
Report still leaves a crash-recovery trail. Password-like targets are not in the
SDK allowlist, and the recorder stops when it reaches ten minutes or 20 MiB.

Replay mode is selected with `repro_mode=replay`; it does not create a recorder.

## Driver commands

Install `driver-debug.apk` and invoke it with Android instrumentation. The
driver requires a unique visible resource ID and uses accessibility actions only:

```sh
adb shell am instrument -w \
  -e op observe -e package io.reproloop.sample \
  io.reproloop.driver/.DriverInstrumentation
adb shell am instrument -w \
  -e op replace -e package io.reproloop.sample \
  -e target name -e value QA io.reproloop.driver/.DriverInstrumentation
adb shell am instrument -w \
  -e op tap -e package io.reproloop.sample \
  -e target add io.reproloop.driver/.DriverInstrumentation
```

Each invocation finishes with a compact JSON object in Bundle key `result`.
`observe` reports only nodes from the requested package and only exposes text
for the `name` and `count` allowlist entries. `scroll_to` is bounded to twenty
scroll actions and `back` uses the platform global back action.

The sample's observable fixture starts with `name=""` and `count="0"`.
Adding once displays `2` in `buggyDebug` and `1` in `fixedDebug`. The intentional
source condition is in `CounterLogic.kt`, making it straightforward for the
host agent to patch and rebuild.
