# Reproof

[한국어](README.ko.md)

Reproof is a self-hosted mobile QA platform with no STF dependency. It
records QA issues as video, user actions, and initial conditions; replays them
deterministically on Android and iOS; applies AI-generated repairs; and
re-verifies a repaired candidate against the same approved original recording.

For wheel installation and running outside a development checkout, see the
[installation guide](docs/INSTALLATION.md). Progress on the general-app product
path is tracked in the [current execution plan](docs/PRODUCT-DELIVERY-PLAN.md)
(the plan document is at r51; see [HANDOFF.md](HANDOFF.md) for physical-device
progress since then).

## Status

What is proven and what is still open:

- The sample path (record, reproduce, repair, re-verify) runs end to end on
  Android devices, iOS Simulator, and a physical iPhone. In the counter defect
  (one tap on `Add` raises the count by 2), the original reproduced 3/3 and the
  repaired candidate passed 3/3; these results include real Claude-generated
  patches.
- The shared QA path (recording, video, issue packages, approved reproduction
  specifications) is verified with two real worker processes driving a
  synthetic app on one Mac, plus real MP4 and browser capture
  ([guide](docs/ISSUE-WORKFLOW.md)).
- Protected repair of declared product files is implemented
  ([G9 path](docs/PROJECT-REPAIR.md)); the 92 G9 software checks pass.
- On a physical iPhone the protected lifecycle has completed end to end:
  device qualification, service composition, issue recording, specification
  approval, 3/3 replay reproduction, and a `verified` AI repair — checked by
  an independent observer process against fixture-prepared device state.
- The same lifecycle completed against a real product app (not the bundled
  sample): the fix candidate verified, and a candidate that also performed
  network egress was rejected `egress_violation` — fail-closed on device
  byte counters with packet-level capture evidence, under both Wi-Fi and
  cellular-only connectivity.
- Still open: the isolated VM build lane (the verified lane is host-build —
  builds run directly on the host and are not reported as isolated), and
  two-Mac acceptance.

## Product direction

The goal is a self-hosted mobile test platform: remote manual control,
automation, and farm operation on one common session layer, connected to
recording and AI reproduce/repair. See the
[platform direction and design](docs/PLATFORM-ARCHITECTURE.md), the
[comparison with official documentation](docs/PLATFORM-REFERENCE-COMPARISON.md),
and [record/replay features](docs/DEMONSTRATION-REPLAY.md).

Automatic observation of general UIKit and Android Views apps is prepared by
declaring public build inputs and UI IDs
([UIKit](docs/IOS-APP-OBSERVATIONS.md),
[Android Views](docs/ANDROID-APP-OBSERVATIONS.md)). The observation profile
preserves the original, stays separate from fixtures and repair policy, and is
attached to general-app sessions on the shared service.

## Scope of the sample commands

The commands below target only the bundled Android sample app and the iOS
Simulator sample app. They record a defect where `Add` increments the count by
2, then re-verify a candidate built from the same buggy variant with only the
business logic patched. The Python host runs with no external packages.

## iOS Simulator

The Swift recording SDK, a UIKit sample, and an XCUITest batch runner form one
pipeline. Across three cases — counter, duplicate submit, initialization
failure — real Claude patches passed the existing regression tests, with the
original reproducing 3/3 and the repair passing 3/3. The same three cases were
verified on a physical iPhone with real Claude repairs, original 3/3
reproduction, and candidate 3/3 verification. The counter case is also
connected end to end from live recording to AI repair
([live repair](docs/LIVE-REPAIR.md)).

```bash
bash scripts/ios-demo.sh <BOOTED_SIMULATOR_UUID> artifacts/my-ios-demo
```

See the [iOS runbook and results](docs/IOS-RUNBOOK.md), the
[three real-AI bug cases](docs/IOS-CASES.md), and the
[QA report for failure paths](docs/QA-REPORT.md). The older Android bundle
format (v1) and iOS bundle format (v2) are verified separately.

## Android quick start

Requirements: Python 3.11+, JDK 17, Android SDK 35 with build-tools and
platform-tools, Gradle 8.14.5 (wrapper included), and an Android device running
API 26+ with USB debugging enabled. The build below assumes a prepared offline
cache; a fresh environment needs the Android plugin, Kotlin, and JUnit
dependencies fetched once.

```bash
cd <reproof clone path>
python3 -m reproof doctor
python3 -m reproof build --receipt artifacts/build.json
```

`build` builds the sample app and the ID-based input driver together and stores
source/APK hashes in the receipt. Default tool paths are discovered
automatically in standard macOS install locations; elsewhere pass `--gradle`,
`--java-home`, `--sdk-home`, or set `JAVA_HOME` and `ANDROID_HOME`.

Running `record` with `--scripted` performs synthetic QA actions and produces
the first bundle automatically:

```bash
python3 -m reproof record \
  --apk android/sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk \
  --driver-apk android/driver/build/outputs/apk/debug/driver-debug.apk \
  --receipt artifacts/build.json \
  --scripted --output artifacts/qa-bundle

python3 -m reproof validate artifacts/qa-bundle
python3 -m reproof replay artifacts/qa-bundle --output artifacts/original-runs
```

For manual QA, drop `--scripted`. In the sample app, type `QA`, tap `Add` once,
then press Enter in the terminal — the session freezes on the `Report` action.
Record-only buttons never enter replay events. With several devices attached,
pick one with `--serial`.

The dedicated sample package `io.reproof.sample` is reset before each run.
Run with the device screen on and unlocked.

## Repair loop

Run the full pipeline offline with the prepared reference patch; this run is
not recorded as an AI execution in the results:

```bash
python3 -m reproof repair artifacts/qa-bundle \
  --patch-file scripts/sample-fix.json \
  --output artifacts/offline-repair
```

With a signed-in Claude CLI, send the sample source and synthetic QA recording
to generate a real patch:

```bash
python3 -m reproof repair artifacts/qa-bundle \
  --agent claude --output artifacts/claude-repair
```

The pipeline confirms the same defect across 3 original runs, then applies the
patch in a separate work directory. A run is `verified` only when the installed
repaired APK's hash matches the build result, the protected JUnit regression
tests actually pass, and the candidate passes 3/3 runs. Mixed results are
`inconclusive`; successful runs are never cherry-picked.

Repair scope is limited to the integer increment expression in
`CounterLogic.kt`. If the agent touches other code, instrumentation, fixtures,
tests, or build settings, the build is blocked beforehand. Repair of declared
product files in general projects uses the separate
[G9 path](docs/PROJECT-REPAIR.md).

To run the offline example in one shot: `bash scripts/demo.sh
artifacts/my-demo`. The output directory must be a new path; existing evidence
is never overwritten.

## Reading results

Check `report.html` and `job.json`/`result.json` in the output directory.
Every repair attempt leaves a source copy, `edits.json`, `patch.diff`, a build
receipt, and repeated-run evidence. The original sample source is preserved as
the baseline.

Exit codes: `0` for successful reproduce/repair, `2` for unmet conditions or
environment blocks, `130` for CLI cancel.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v

# Regression tests on the fixed build
cd android
./gradlew --offline :sample:testFixedDebugUnitTest :sample:assembleFixedDebug :driver:assembleDebug
```

The original's `:sample:testBuggyDebugUnitTest` intentionally fails because of
the defect. In a copy where the repair loop patched CounterLogic, the same test
must pass. `NO-SOURCE` or skipped tests never count as verification success.

For extra scripts that check input, scroll, navigation, back, and
sensitive-input refusal on a physical device, see `scripts/device_smoke.py
--help`.

## Layout and scope

- `android/sdk`: opt-in recording SDK — ordered JSONL, session freeze, and
  incomplete-record markers
- `android/sample`: buggy and fixed builds plus protected regression tests
- `android/driver`: observe, tap, input, scroll, and back through UiAutomation
  resource IDs
- `reproof`: bundle validation, compilation, ADB execution, repeat verdicts,
  repair orchestrator, HTML reports
- `ios`: Swift Recorder, UIKit sample, protected XCUITest and logic regression
- `reproof/ios_*`: Simulator builds, v2 bundles, batch runs, Swift repair
  verification
- `schemas`: public formats for recording and verdict data
- `tests`: failure, tampering, repeat-result, and patch-boundary checks

See the [iOS support design](docs/IOS-DESIGN.md),
[execution contracts and current limits](docs/CONTRACTS.md),
[implementation and verification status](docs/IMPLEMENTATION.md), and the
[full plan](PLAN.md). Per-app observation, fixtures, and independent
verification have been exercised on a physical iPhone against a real product
app; two-Mac acceptance and the isolated VM build lane are still open.
Check support scope separately for the sample path and the shared QA path.

## Live console

`python3 -m reproof live-serve --demo` starts the local console. Real iOS
Simulator connection, record/replay, and Python-script export are covered in
the [live runbook](docs/LIVE-RUNBOOK.md); results are in
[Live QA](docs/LIVE-QA.md).

To inspect the work queue and recording library without a device, run
`python3 -m reproof live-serve --demo --demo-count 2`. See the
[operations, CLI, and agent tools documentation](docs/LIVE-OPERATIONS.md).

For continuous touch, streaming, and worker execution on real Android — and
for physical-iPhone signing, install, live record/replay results, and their
limits — see the [device live documentation](docs/DEVICE-LIVE.md).

The central coordinator for multiple users and registered hosts is a separate
mode from the local console. Roles, project permissions, one-time host
enrollment, TLS/CSRF, configuration files that contain no secrets, and the
admin CLI are covered in the
[shared coordinator operations documentation](docs/SHARED-COORDINATOR.md).
