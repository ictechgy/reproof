# Owned native runtime acceptance

This lane exercises newly authored, offline inventory fixtures under the repository's
MIT license. It does not download third-party apps, use customer data or accounts,
or operate a developer's phone or Mac. It is not company-app acceptance and does not
establish Compose, SwiftUI, or automatic self-healing support.

## Isolation and execution

`.github/workflows/native-runtime.yml` uses standard public GitHub-hosted
`ubuntu-24.04` and `macos-15` runners with read-only repository permissions, no
checkout credentials, and a 25-minute limit per job. There are no publishing steps.
The Android helper and fixture are built using the checked-in verified Gradle wrapper
and existing SDK 35/JDK 17. Debug signing keys are generated only in the job's
private temporary home; no key or keystore is uploaded. The emulator uses the
official Android 35 default x86_64 image. SDK license prompts are not auto-accepted:
a missing required license is a blocker, not permission to accept it.

The iOS lane requires preinstalled Xcode 16.4 and iOS 18.5 and builds unsigned
simulator products. It does not install Xcode, accept a license, select a signing
team, or access a user keychain. A missing runtime fails preflight.

Each wrapper creates a uniquely named emulator/simulator and passes its explicit
identity to the harness. The harness must verify ownership before any install or
input. It must never choose a connected device automatically. Shell traps close
only the created process/device and delete only that disposable AVD/simulator.
Interrupted creation and forced runner termination can prevent explicit teardown;
the hosted runner itself is ephemeral. Teardown is best-effort for these cases,
not a guarantee that a signal or hard timeout executes the cleanup trap.
No host security permissions are changed to enable acceleration.

## Required proof and interpretation

Both fixtures exercise an initial list, invalid form submission with no new item created,
valid creation, list/detail navigation, and cancel/back behavior. Checks must use
actual UI observations, not merely a successful input acknowledgement or a fixture
state file. Negative cases must not be reported as passes because an assertion was
relaxed, a target substituted, or an observation failed.

Android uses Reproof's general-app exact resource-ID locator/input path with the
real native helper. Missing, ambiguous, disabled, and protected targets are negative
cases. This demonstrates only the fixture's Android Views contract.

iOS uses Reproof's existing gesture path with local on-runner frame observations.
General iOS semantic locator replay and target-bound typing remain unsupported.
An expected rejection of that unsupported path is recorded separately from successful
gesture execution. Local OCR is an oracle for fixed fixture text, not a locator
healing mechanism; uncertainty is not permission to pass or approximate an assertion.

Android startup diagnostics observe original instance calls without replacing the
provider type or changing arguments, return values, exceptions, deadlines, or
authority admission. Startup observers are removed after startup succeeds; frame
and input dispatch methods are not wrapped. Only allowlisted failure enums,
bounded state metadata, and repository-relative traceback locations are retained.

Runtime artifacts are retained for seven days and must contain only fixture-owned
screenshots, redacted results, and build/runtime diagnostics. Private authority
material, tokens, signing keys, and user data must never enter artifacts. Review
failure evidence before retrying. Do not keep rerunning unchanged expensive jobs
until they happen to pass.

## Boundaries still requiring separate acceptance

Passing this lane does not prove physical-device behavior, production app builds,
company authentication/roles, locale coverage, concurrency/load, security review,
real Android OEM behavior, or iOS semantic locator support. It does not authorize
personal-device installation/reset, company access, external AI transmission,
release signing, or deployment. Preserve original specs and exact assertions when
moving from these fixtures to a real pilot app.

Runner/tool references checked when introducing this lane:
- [GitHub standard hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [macOS 15 arm64 installed software](https://github.com/actions/runner-images/blob/main/images/macos/macos-15-arm64-Readme.md)
- [Android AVD manager](https://developer.android.com/tools/avdmanager)
- [Android emulator command line](https://developer.android.com/studio/run/emulator-commandline)

Installed image contents change independently of this repository. Missing pinned
SDK/runtime requirements must remain an explicit infrastructure failure.
