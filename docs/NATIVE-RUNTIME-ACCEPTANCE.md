# Owned native runtime acceptance

This lane exercises newly authored, offline inventory fixtures under the repository's
MIT license. It does not download third-party apps, use customer data or accounts,
or operate a developer's phone or Mac. It is not company-app acceptance and does not
establish Compose, SwiftUI, or automatic self-healing support.

## Current acceptance state

The Android runtime job is explicitly paused after software-emulator helper
handshake failures. Its skipped status is not acceptance. Hardware acceleration
permission has not been granted; no KVM ACL or host security setting is changed.
The iOS lane continues independently. Neither lane has yet established the full
UI flow at the time this status was added; retain the draft until evidence supports
a narrower verified claim.

## iOS fixture and pixel-oracle revisions

Run `39983909592` compiled the helper and passed all three native startup-budget
unit tests. The original fixture reached authenticated startup, verified its
installed digest, and produced an actual provider JPEG. It did not pass the first
empty-state assertion: Apple Vision read `NO ITEMS` exactly but assigned confidence
`0.5`, below the required `0.9`. The text was visibly unclipped and high contrast;
this is not evidence of a layout defect. The failed result and quarantine outcome
remain failures, not UI-flow or cleanup acceptance.

Fixture revision `inventory-empty-statement-v2` changes only that displayed
statement to `INVENTORY IS EMPTY` and its corresponding exact required/forbidden
strings. The stable ID `inventory.empty`, font, layout, zero-item condition, and
`BLUE MUG` absence assertion are unchanged. The wording adds context, but any OCR
benefit remains unproven until a new native run. Results, per-flow proofs, and
accepted state records identify the fixture revision; build receipts bind the new
source and artifact digests. The older failure is not relabeled or reinterpreted.

Pixel-oracle revision `exact-required-observed-forbidden-v2` retains the `0.9`
threshold for required exact text. An exact forbidden reading at any confidence
now prevents acceptance, rather than disappearing below the confidence threshold.
Malformed or non-finite confidence values also fail closed. Portable tests
reproduce the former low-confidence-forbidden false pass and reject it. These
checks do not establish general OCR accuracy, detect every OCR misreading, or
provide a semantic locator. No fuzzy matching, alternate accepted wording,
threshold reduction, or dropped assertion is introduced.

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
