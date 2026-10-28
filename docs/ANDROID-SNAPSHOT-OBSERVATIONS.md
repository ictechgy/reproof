# Local snapshot Views observations (v3)

This opt-in path connects an explicit local source snapshot to Android Views
observation preparation and an unsigned Debug build. It does not change v2,
G9, execution-wire limits, external-AI consent, fixture readiness, `knownStart`,
or replay verdicts. It is not company-app or device-runtime qualification.

## Select inputs and one Activity

Capture a reviewed explicit selection with `reproof source-snapshot capture`.
There is no repository discovery, Git execution, archive extraction, upload or
external-AI call. Android's existing path/credential exclusions and finite
`inputExtensions` policy apply to every selected file, not just analyzed code.

The profile uses `schemaVersion: 3`, `kind: views-observation-v3`, and the same
`package`, `activity`, `build`, `tapTargets`, `screenTargets` and optional
`inputExtensions` as v2. Replace `sourceInputs` with a compact reference to the
**original** snapshot and explicitly select one Activity source:

```json
"sourceSnapshot": {
  "schemaVersion": 1,
  "kind": "reproof-local-source-snapshot",
  "digest": "<64 lowercase hexadecimal characters>",
  "files": 9500,
  "bytes": 73400320
},
"activitySource": "app/src/main/java/example/MainActivity.kt"
```

The reference's digest, file count and byte count must match actual verified
contents. The snapshot directory is a separate CLI argument, never part of the
portable reference. The Activity must be a selected `.kt` file in the configured
module's `src/main/java` or `src/main/kotlin`, at most 4 MiB.

Only that file is passed to Kotlin PSI. This is syntax-based selected-Activity
analysis, **not** whole-module type resolution, duplicate-class detection or
coverage of arbitrary variant source mappings. Other Kotlin files stream
unchanged into the build instead of being collected in a PSI input dictionary.
The real build must independently produce the configured Activity, ASM site and
lifecycle evidence and matching embedded observation profile. These checks are
not proof of UI behavior; `behaviorVerified` remains false.

## Explicit budgets and local build

```sh
reproof android-instrument --source original-snapshot --output prepared \
  --observation-profile views-v3.json \
  --snapshot-max-files 10000 --snapshot-max-bytes 100000000

reproof android-app-build --source prepared/snapshot --output build \
  --observation-profile prepared/app-profile.json \
  --snapshot-max-files 10000 --snapshot-max-bytes 100000000 \
  --gradle /approved/gradle --java-home /approved/jdk --sdk-home /approved/android-sdk
```

Defaults remain 900 files / 60 MiB. Each operation requires its own explicit
larger budget; profiles and receipts cannot elevate it. Hard limits remain
32,768 files / 256 MiB, 8 MiB per ordinary file and 4 MiB per analyzed Activity.
Preparation uses bounded metadata and at most 16 changed files / 16 MiB. It
changes only the selected settings/module Gradle files and fixed generated
instrumentation files; product source bytes remain unchanged. Changes are
recorded as bounded hashes, not potentially enormous textual diffs.

The prepared profile retains the original snapshot reference; the external
preparation receipt separately binds original/prepared snapshots, profile and
changes. A locally stored digest or receipt identifies contents, not trusted
provenance, immutable storage, authorization or proof of historical actions.

The new build path materializes the prepared snapshot into a fresh directory,
runs the approved Gradle executable offline, then revalidates the explicit
protected inputs. It checks ASM class/archive/site/lifecycle evidence and the
APK's embedded observation declaration before reporting success. The legacy
source scanner is not a fallback for v3. APKs remain capped at 150 MiB and the
transformed JAR at 256 MiB; source budgets do not enlarge artifact limits.

The generated module integration clears default-configuration, product-flavor
and Debug-build-type signing selections. V3 deliberately produces unsigned
Debug variants regardless of inherited signing settings; output must be
unsigned. Only the derived workspace is changed, never original source or keys.
That workspace is for the selected Debug build, not an unchanged Release build. This does not sandbox arbitrary user-supplied
Gradle code: trusted build scripts can run processes, access the network or
perform other effects. Review the build inputs before execution. Dependencies
and approved toolchains must already be cached; an offline build is not an
assurance that untrusted scripts cannot access the network.

## Failure and coverage boundaries

Outputs must be fresh and separate from inputs. Parent/root identity fences and
snapshot live-descriptor ownership checks remain in force. On failure or
cancellation, output may be retained with `snapshot_cleanup_incomplete`, even
for small inputs. Never assume failure removed private material. Executed build
directories are not recursively deleted; review retained output locally before
manual disposal. Do not upload source snapshots, compiler output or captures
without separate authorization.

Supported build integration is Kotlin DSL or the existing restricted Groovy
insertion grammar (`build.dsl: groovy`). This is not general Groovy evaluation:
script application, composite builds and unsupported metaprogramming fail
closed. One Kotlin Activity with supported explicit click registrations remains
the observation scope; Fragment, Java, XML onClick, DataBinding and Compose
coverage are not added. Dependency caches, build-generated disk usage and
compiler memory are separate from the snapshot's bounded source budgets.
The operator may select an existing dependency cache with an absolute
`GRADLE_USER_HOME`; the profile cannot choose a cache path or raise budgets.
Cancellation is checked before build dispatch and success publication; the
existing bounded PSI subprocess may finish or reach its timeout before the
next cancellation check. Cancellation is not proof that arbitrary build-script
background work has quiesced.

CI distinguishes synthetic large-input tests with compiler doubles from real
small public-fixture PSI/unsigned builds. Real fixtures explicitly warm public
dependencies before the offline consumer build; this is not an offline first
build with no downloads. Neither lane runs apps or devices, verifies a defect,
or qualifies a company application.
