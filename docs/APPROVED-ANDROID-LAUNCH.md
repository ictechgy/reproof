# Explicit approved Android launch parameters

This is an **Android-only, opt-in launch configuration**, not fixture preparation,
reset, deterministic replay, or a known-start proof. General runtime profiles
still have no reset contract. Existing shared-authority requirements are unchanged.
No iOS launch-parameter integration is provided by this option.

The operator supplies a private local file separately from the runtime profile:

```json
{
  "schemaVersion": 1,
  "kind": "reproof-approved-android-launch",
  "referenceId": "launch_checkout",
  "profileDigest": "<exact full runtime profile SHA-256>",
  "extras": [
    {"key": "QA_SERVER", "type": "string", "value": "https://qa.example.invalid/base"},
    {"key": "QA_MOCK_MODE", "type": "boolean", "value": true},
    {"key": "QA_FIXTURE_VERSION", "type": "integer", "value": 1}
  ]
}
```

Use `live-serve --android-profile <profile> --android-launch-config <private-file>`
with the existing Android device/helper/application options. A trusted worker's
Android device entry may name `launchConfig` alongside `profile`, `helper`, and
`application`. Paths resolve relative to the operator's worker configuration.
The iOS worker shape deliberately does not accept `launchConfig`.

`referenceId` must equal `approvedReferences.launch`; `profileDigest` binds the
entire selected profile, including build identity. Changing the profile requires
a separately selected matching configuration. Imported profiles/recordings do not
contain or select parameter values and cannot mutate the frozen selection. The
host descriptor exposes only the reference and digest, not the extras.

## Initial supported parameter subset

- One to sixteen unique `QA_*` uppercase keys; framework/reserved names and
  secret-related names are rejected.
- Typed string, boolean, and signed 32-bit integer extras only. Booleans cannot
  stand in for integers. This is not an arbitrary shell command or environment.
- Bounded ASCII strings, at most 512 characters. No shell metacharacters, quotes,
  control characters, or line breaks. The native helper additionally quotes each
  fixed typed `am start` argument.
- URL-shaped strings require HTTPS, no embedded credentials/query/fragment,
  no percent-encoding, and the default HTTPS port. A QA base URL is configuration,
  **not** permission for the assistant to contact that service.
- At most 8 KiB canonical launch JSON; the complete private helper configuration
  must also fit its existing 16 KiB limit. Symlinked local configuration inputs,
  duplicate JSON fields, unknown fields and malformed values are rejected.

Only explicitly approved **non-secret QA configuration** belongs here. The tool
cannot recognize every secret by its value. Do not use passwords, tokens, session
cookies, production credentials, or real private data. These values are sent to
the selected local device/application when a separately authorized native run is
started; they are not automatically uploaded to a remote service. Keep local
configuration files private and out of source control or result attachments.

## Helper negotiation and evidence limits

A configured launch requires a rebuilt helper with
`launchConfigurationVersion: 1` and an exact `launchConfigurationDigest` echo.
The helper independently validates the canonical configuration and its profile
binding. The digest is an integrity/binding value, not a signature or permission
from imported data: authorization comes from the operator-selected local file
and the existing authenticated helper/host authority. Missing capability, changed digest, or invalid values reject before a
target launch. Every subsequent launch checks the same binding again; ordinary
input payloads cannot supply replacement extras. Non-configured sessions retain
legacy helper compatibility.

The app must explicitly implement these QA keys. Accepted extras and an
acknowledged launch do **not** prove the app used them, selected a QA server,
prepared a fixture, or reached a reproducible screen. A future app-owned readiness
contract must provide fresh session/build/profile/fixture-bound completion and
preserve device/fixture fencing before it can establish known start.

Host validation, fake transport tests and source wiring tests do not qualify a
native Android launch. Actual helper compilation and device execution must be
reported separately. No company application, external endpoint or real device
is exercised by the portable tests.
