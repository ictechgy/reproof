# Bounded Android sample logic repair

This repository-owned exercise uses the **actual Android sample's Kotlin source
and existing JUnit oracle** with the existing Gradle
`:sample:testBuggyDebugUnitTest` task on a host JVM. It is not Android device or
UI execution, SDK capture, scenario replay, AI-generated repair, a security
sandbox, or company-app acceptance. This work does not claim a successful native
run until the explicit host goal has produced and validated fresh evidence.

## The causal check

`android/sample/src/main/java/io/reproof/sample/MainActivity.kt` adds
`CounterLogic.increment()` to the count. The checked-in implementation returns
`if (BuildConfig.BUGGY) 2 else 1`. The unchanged
`android/sample/src/test/java/io/reproof/sample/CounterLogicTest.kt` asserts that
one increment equals `1`. The exercise keeps the **buggy flavor** throughout:
passing by switching to the fixed flavor is not a repair.

| Candidate | Required result |
| --- | --- |
| Baseline (buggy branch returns `2`) | Exactly one testcase fails with expected `1`, actual `2`. |
| Wrong patch (buggy branch returns `3`) | The same testcase fails with expected `1`, actual `3`. |
| Fixed patch (buggy branch returns `1`) | The same testcase passes, exactly once, with no skips. |
| No-op | Rejected before any compilation or baseline execution. |

Only the finite source edit is permitted in temporary frozen source copies. The
checkout remains intentionally buggy. Source, app callsite, full oracle, and
build recipe are pinned. Baseline and candidate use fresh isolated directories,
so an old result, build cache, or another candidate's outputs cannot supply the
required evidence. Successful compilation and the exact fresh JUnit testcase
identity, count, failure kind/value and outcome are required. Build errors,
unrelated assertion failures, malformed results, zero tests and skipped tests
are not accepted as reproductions or fixes.

Ticket/log/specification correlation is explicitly **synthetic repository
metadata**, not a capture from an installed application. It exercises review
binding without granting execution or approval authority. Passing this host
logic exercise does not approve a ticket, accept a mobile application, or claim
actual SDK/UI/AI behavior.

## Separate portable and host goals

Portable Python contracts need no Java, Gradle, Android SDK, device or network:

```sh
python3 scripts/release-check.py --goal ANDROID-LOGIC-CONTRACTS --effects filesystem,process
```

Fabricated parser fixtures and any mocked orchestration in these tests are
contract checks, **not runtime evidence**. The portable workflow runs this goal
separately from native execution.

On a suitably provisioned host, explicitly invoke real Kotlin/JUnit execution:

```sh
export JAVA_HOME=/path/to/existing/jdk17
export ANDROID_HOME=/path/to/existing/android-sdk
export REPRO_ANDROID_LOGIC_OUTPUT=/new/local/android-logic-evidence
python3 scripts/release-check.py --goal ANDROID-LOGIC --effects filesystem,process,native-compile
```

This requires JDK 17, Android SDK platform 35 and build tools 35.0.0. The existing
checksum-pinned Gradle 8.14.5 wrapper uses the repository's Kotlin 2.2.10 and
Android Gradle Plugin 8.13.2 recipe. Gradle dependency/distribution resolution may
require network access on the separately authorized host. No native tools are
installed by the local implementation/portable checks. There is no emulator,
KVM, APK installation, device, signing, or deployment step.

The fixed, wrong, and no-op cases are separate release checks. Each has a
600-second outer budget; the two compiled observations are bounded to 240
seconds each, including cold wrapper/dependency downloads, plus bounded
toolchain checks and cleanup. A slow cold download can exceed this budget; that
is a failed/incomplete host attempt, never a reproduction or repair verdict. The explicit runtime
tests have no mocks or skip-to-green behavior: missing tools or incomplete
evidence fail the goal. `REPRO_ANDROID_LOGIC_OUTPUT` retains reports and raw
logs/results for inspection. Each candidate's destination must be new; an
existing candidate directory is not reused. Direct compiled `run_demo` calls
also require an explicit new `output` directory so raw evidence is not silently
deleted; only a no-op can use a disposable directory.

The dedicated `android-logic.yml` workflow uses a preinstalled JDK/SDK and
retains raw logs, JUnit XML, and JSON reports on success or failure. Its path
filters cover the sample, build recipe, evaluator, contract dependencies,
runtime tests and release harness. Adding this workflow does not establish that
it has run. This implementation was prepared locally without the native
Android toolchain; an authorized published hosted run remains necessary before
claiming actual runtime compatibility or causal success.

## Process cleanup limits

The shared native command runner uses Linux `waitid`/`WNOWAIT` when available to
observe natural leader exit without reaping it. The reserved leader PID allows
bounded TERM/KILL cleanup of remaining same-group children before collecting the
leader's original exit status. It requires a controlled host with default
SIGCHLD handling and no competing child reaper, including other threads.
Custom/ignored SIGCHLD is rejected before launch; detected wait-ownership loss
rejects the command without further termination signals. These checks do not
make competing reapers safe.

Unsupported platforms/interpreters retain polling (including macOS Python 3.12
used by the Swift CI). A naturally exited leader may already be reaped, so its
numeric PGID is not sent further termination signals. Bounded absence checks
fail closed with `cleanup_unconfirmed` if the group remains. **Same-group
descendants can remain alive on this fallback**; rejection is not proof of
termination. Group-escaping descendants remain out of scope on either path.
This is not a sandbox, and no interpreter or security boundary is changed.
