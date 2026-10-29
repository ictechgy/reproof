# Xcode support and evidence scope

This is an evidence map, not a blanket Xcode compatibility guarantee. A compiler
version alone does not qualify an application, signing setup, Simulator
lifecycle or physical device. This documentation change runs no Apple tools
and establishes no new native result.

## Separate configuration, reports and executed evidence

| Evidence | Version / scope | Limit |
| --- | --- | --- |
| Reported compatibility | Live helper build and Simulator Live operation reported with Xcode 26.6 in user feedback | Not independently reproduced by this documentation change. A report is not a repository CI pass or support for operations outside the reported scope. |
| Checked-in CI configuration | `macos-15`, preinstalled Xcode 16.4 build 16F6, Python 3.12; exact version check fails if absent | Configuration is not proof that a particular commit's run passed, nor a multi-version support matrix. |
| Historical sample Simulator record | Xcode 27.0 build 27A5228h and iOS 27.0 arm64 Simulator in the [MVP runbook](IOS-RUNBOOK.md) | Not a current-checkout rerun, stable-Xcode guarantee or company-app qualification. |
| Physical-device qualification | Separately selected toolchain, helper/artifact, device and operation evidence under the [protected-service contract](IOS-PROTECTED-SERVICE.md) | Compilation and sample Simulator results do not qualify a new physical-device deployment. |

## What the checked-in CI is configured to check

The source of truth is [ios-sdk-compile.yml](../.github/workflows/ios-sdk-compile.yml).
It selects `/Applications/Xcode_16.4.app/Contents/Developer` for the job, checks
`Xcode 16.4` / `Build version 16F6`, and neither installs Xcode nor changes the
global selection. Its configured checks include:

- Synthetic Foundation/XCTest SDK execution with a nonzero-test check and a
  bounded sample Swift-logic repair oracle. These are host tests, not app UI
  execution.
- Independent local SDK consumer compilation and synthetic export compatibility
  with the unchanged sample-only host normalizer.
- Unsigned Debug/Release sample and snapshot-backed app compilation for a
  generic iOS Simulator destination. Snapshot proof keeps `deviceExecution`
  and `behaviorVerified` false.
- Live helper `build-for-testing` and product checks, not a running Simulator
  or helper session. Synthetic parser/observation contracts are not app behavior.
- Compilation of the public recovery sandbox probe without executing it. This
  does not validate actual sandbox enforcement or qualify Android recovery.

This workflow does not boot a Simulator, install or launch an app on a device,
or establish physical-device signing or end-to-end recovery. A passing-result
claim must identify the exact commit/run and completed checks, not merely the
workflow configuration or a green portable Python job. Missing tools and skipped
native tests are not passes. Documentation-only changes need not trigger this
path-filtered workflow. The [private Android recovery qualification
harness](ANDROID-RECOVERY-QUALIFICATION.md) remains a separate, explicitly
unqualified runtime path; its source merge is not runtime acceptance.

## Dependency support is a separate boundary

The repository ships a local, dependency-free [ReproofSDK](../ios/SDK/README.md)
Swift package used by the sample. This is not remote package distribution or
support for arbitrary SPM workspaces or pre-resolved package-cache imports.

- [iOS snapshot builds](IOS-SNAPSHOT-OBSERVATIONS.md#dependency-cache-boundary)
  reject package references, including local SPM references, in the
  closed-workspace validator when a `.xcworkspace` is declared. A pre-resolved
  cache does not override that rejection. Without a declared workspace, the
  validator returns without this check; single-project builds still provide no
  approved SPM-cache/import contract.
- [Android snapshot builds](ANDROID-SNAPSHOT-OBSERVATIONS.md) can receive an
  operator-selected `GRADLE_USER_HOME` and run Gradle offline. Path selection
  is not dependency authentication or an approved cache manifest/receipt.

A future approved-cache interface needs a separately reviewed input and trust
contract. This documentation adds no cache ingestion, dependency resolution,
credentials, network permission, signing permission or runtime qualification.
