# UIKit automatic recorder templates

The debug-only sources in this directory are copied into the application
target by the iOS build preparer. `RLAutomaticRecorder.swift` is compiled
under `DEBUG` with the generated `RLAutoConfig.swift`; `RLAutoBootstrap.m` is
compiled under `REPRO_AUTO_DEBUG` and finds the recorder through its fixed
Objective-C runtime name, so no generated Swift header or product-module name
is required.

The runtime accepts the fixed `uikit-runtime-v1` profile used by the sample:
the `counter.name` text field, `counter.count` numeric label, the configured
counter taps/back action, and the `main`/`details` screen identifiers. It
records only the allowlisted text values `""`, `"QA"`, and `"Test"`. Scroll
semantic tracing is intentionally unsupported in this first scope; no
delegate replacement or scroll callback is installed.

All files are excluded from Release by the build preparation step. The Swift
source also has a `#if DEBUG` guard and the Objective-C source has a
`#if REPRO_AUTO_DEBUG` guard so an accidental source-file registration cannot
add a Release runtime.
