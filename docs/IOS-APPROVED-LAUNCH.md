# Approved iOS Simulator QA launch settings

An operator can bind a small set of QA launch settings to an exact general iOS
runtime profile and selected `.app`. This is configuration, **not app-owned
readiness, a reset contract, backend isolation, or proof of reproducibility**.
Apps that cannot consume these settings retain ordinary Live/unverified playback;
this option does not make app changes mandatory.

## Supported boundary

- General iOS runtime profile + selected Simulator `.app`, **shared-v2 only**.
- Local `live-serve` and explicit Simulator `live-worker` operator configuration.
- No sample/bundle-only/physical-iPhone selection, legacy-offline opt-in, HTTP
  session override, arbitrary environment, arbitrary launch arguments, shell
  commands, paths, credentials, or automatic production-endpoint rewriting.
- A compatible helper validates configuration and acknowledges its version/digest
  before the host permits launch. A stale helper fails closed. Legacy helpers
  can launch before their ready callback, so this option cannot safely support
  legacy-offline mode. Existing no-config legacy behavior is unchanged.

The operator must select a QA build whose app code deliberately consumes the
settings. The tool cannot prove that `QA_SERVER` is a QA server, that the app
honors it, or that earlier app initializers avoided production requests. `qa` and
`mock` are requested modes, not enforced sandbox policies. Fixture ID/version
are only parameters; they do not set `knownStart` or authorize prepared replay.
Existing observation/oracle requirements for defect verdicts are unchanged.

## Operator configuration

Create a local JSON file with exactly these fields (replace digests/reference
with the selected runtime profile's actual values):

```json
{
  "schemaVersion": 1,
  "kind": "reproof-approved-ios-launch",
  "referenceId": "launch_checkout",
  "profileDigest": "<complete runtime profile digest>",
  "artifactDigest": "<profile artifact sha256>",
  "environment": [
    {"key": "QA_SERVER", "type": "string", "value": "https://qa.example.invalid/base/"},
    {"key": "QA_MODE", "type": "string", "value": "qa"},
    {"key": "QA_FIXTURE_ID", "type": "string", "value": "checkout_7"},
    {"key": "QA_FIXTURE_VERSION", "type": "integer", "value": 7}
  ]
}
```

`referenceId` must equal `approvedReferences.launch`. The full profile digest
binds the project, build, application and capabilities; `artifactDigest` also
explicitly binds the selected app tree. Existing artifact staging/installation
identity checks remain required. Changing any binding requires a new selection.
The loader rejects duplicate JSON keys, links and files larger than 8 KiB. It
captures an immutable canonical copy; later file edits do not change sessions.

One to four unique settings are allowed:

| Key | Type | Allowed values |
| --- | --- | --- |
| `QA_SERVER` | string | HTTPS, lowercase ASCII DNS host with at least two labels; maximum 512 characters; no port, IP literal, userinfo, query, fragment, percent encoding or backslash. Optional path uses alphanumeric `._~-` segments, without `.`/`..` or empty interior segments. |
| `QA_MODE` | string | `qa` or `mock` |
| `QA_FIXTURE_ID` | string | 1–64 ASCII alphanumeric/underscore/hyphen characters, first character alphanumeric |
| `QA_FIXTURE_VERSION` | integer | 1–2147483647, not boolean or floating point |

Fixture ID and version must appear together. Do not embed secrets in URLs or
fixture IDs: these fields are not a secret transport. All other keys are denied.
Swift independently checks the same canonical schema, bindings and digest, then
assigns only the validated map to `XCUIApplication.launchEnvironment` alongside
existing reserved Reproof fields. The same captured settings are used on every
launch; no ambient host environment is copied into the target.

```sh
reproof live-serve --authority-mode shared-v2 \
  --simulator <device> --products <Live helper products> \
  --ios-app <QA.app> --ios-profile <runtime.json> \
  --ios-launch-config <launch.json>
```

Supply the usual shared coordinator/authority setup. For an explicit Simulator
worker, use `--simulator`, `--ios-products`, `--ios-app`, `--ios-profile` and
`--ios-launch-config` with the existing worker authentication options. The
multi-device JSON configuration's existing physical-iOS entry is not expanded
by this option. Mixed physical/Android targets with this selection are rejected.

Raw values are private operator inputs, not public device/session descriptors or
error messages. The temporary XCTest configuration remains private and runner
output is discarded as before. A digest handshake proves only that the selected
helper accepted the configuration; it does not prove application behavior.

## Validation and limits

Host tests cover shape/type/URL/binding rejection, immutable settings, helper
handshake, startup ordering and unchanged no-config behavior. Ordinary macOS CI
runs synthetic Foundation/CryptoKit parser vectors and compiles the unsigned
Live helper without booting or running a Simulator. This is compile/contract
coverage, not qualification of a real company's app or native device behavior.
