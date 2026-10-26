# Native test inputs are explicit prerequisites

Portable parser tests construct synthetic app bundles and APK metadata in their
temporary directories. They do not prove real signatures, native SDK inspection,
or platform process isolation. Those assertions remain in the native suites.

Native suites no longer assume an earlier, untracked `artifacts/product-delivery`
run exists in a particular checkout. Before selecting a native gate, an operator
must independently prepare repository-owned test inputs and explicitly select:

| Environment variable | Required input / existing assertions |
| --- | --- |
| `REPROOF_TEST_ANDROID_APK` | An owned `.apk` for actual AAPT inspection; expected package `com.example.reproinventory`, version code 1. |
| `REPROOF_TEST_ANDROID_UNSIGNED_APK` | Owned unsigned `.apk` for the existing Android signing-owner tests; expected package `com.example.reproinventory`. |
| `REPROOF_TEST_IOS_APP` | Owned universal Simulator `.app`, bundle `com.example.reproinventory`, suitable for the existing real native signing and independent inspection assertions. The architecture test still requires x86_64 and arm64. |
| `REPROOF_TEST_IOS_DEVICE_RUNNER` | Owned generated device XCTest runner `.app` with its matching dSYM sidecar. |
| `REPROOF_TEST_IOS_DEVICE_RUNNER_SECOND` | A second independently generated device runner for the same dSYM qualification test. |

Use absolute paths without symlink ancestors. On macOS resolve `/var` and `/tmp`
to their canonical `/private/...` locations first. APK inputs remain bounded to
64 MiB by this qualification fixture selector. These variables are local test
configuration, not permission to access company applications, personal devices,
credentials or remote services. Do not commit values, application bundles or raw
native evidence. Selecting a path does not validate its contents or prove its
provenance; the original native parsing, identity, signature and lifecycle
assertions still run unchanged.

An absent variable is reported as a precise prerequisite skip. An explicitly
configured malformed or missing input is an error, not a skipped assertion. The
release checker rejects skipped/incomplete test evidence, so missing native
inputs can never qualify a release. Portable `NATIVE_CONTRACTS` includes only the
selector's synthetic tests, not the real native suites.

This change does not make the complete discovery suite platform-independent.
Actual native suites retain their compiler, SDK, signing and sandbox requirements,
including cached tool paths in existing suites. No native build, device or
company-app run is implied by portable tests. Existing `artifacts/qa-delivery`
paths used to *write* acceptance evidence are not input fixtures and are unchanged.
