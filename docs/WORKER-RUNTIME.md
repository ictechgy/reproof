# General application workers and artifact transport

G6 adds version-2 application profiles, enrolled device inventory, physical
reservations, and durable artifact HTTP APIs. These compose the
[host authority](HOST-AUTHORITY.md), [collection policy](DURABLE-RECORDING.md),
and [shared coordinator](SHARED-COORDINATOR.md) contracts.

## Application selection

A runtime profile binds the exact project digest, application/build IDs,
package or bundle, selected artifact digest and size, provenance digest,
launch target, helper versions, observation capabilities, geometry limits,
and approved launch/preparation references. It has `schemaVersion: 2` and
`kind: "reproof-runtime-application"`. Unknown fields, unsupported adapters,
duplicate identifiers, and malformed nested values are rejected.

Use `validate_android_runtime_profile` / `load_android_runtime_profile` or
`validate_ios_profile` / `load_ios_profile` from the corresponding profile
module. Public complete profile examples are the `android_document` and
`physical_ios_document` builders in `tests/test_worker_profiles.py`; replace
their synthetic project/build hashes with the trusted registered values.

General profiles do not require a numeric oracle, sample reset contract,
sample SDK, or `ReproBuildID`. A launch acknowledges target launch; it does
not prove fixture reset or equivalent starting state. Both live providers
require their declared `native-frame` pixel adapter. Every declared observation
must also be allowed by the registered project policy. Actual frames must fit
the declared dimensions and orientations.

Android supports a selected single APK and checks the installed APK digest.
Split APK qualification is unavailable. Its optional resource-ID locator binds
a fresh native acquisition to the current frame and geometry. iOS currently
supports the declared gesture/launch actions and pixels. General accessibility
locators remain unavailable. Its optional `repro-app-log` adapter requires the
selected app to embed a matching [configured UIKit observation profile](IOS-APP-OBSERVATIONS.md).
This adapter records declared clicks, screens and lifecycle without a sample
fixture. Legacy application logging retains its original scope.

Android's optional `repro-app-log` adapter requires a matching
[Views observation profile](ANDROID-APP-OBSERVATIONS.md) embedded in the selected APK.
The APK is verified through a regular-file boundary and frozen before installation;
the installed hash must equal the selected hash. Logs use the embedded observation
profile digest and a fresh launch ID. General Android launch restarts the process
without clearing app data, with the same behavior when logs are disabled. The helper
must advertise observation launch version 2. Control readiness is independent of
the first frame; launch acknowledgement requires a fresh target frame. Pixel capture
remains disabled until the authorized target launch succeeds.

Simulator apps are checked again and copied into a private installation snapshot
before startup. The installed file tree must equal the selected artifact. A
same-sized older source executable cannot silently retain the previous code.
Installation does not delete app data; leftover files from a different build
shape still cause rejection and require a separately approved clean fixture.

Physical iOS verifies selected signed products and their bundle/version/tree
digest before installation. After installation it queries the selected bundle
with `devicectl device info apps --bundle-id`, compares the returned version and
build, and requires the helper's bound target-launch acknowledgement. Missing,
ambiguous, or mismatched installed metadata fails. Installed-binary SHA-256
remains unavailable and is recorded as null; the selected artifact's digest is
never substituted. The device query adapter still needs validation against the
authorized physical device/Xcode deployment.

## Worker configuration

For one application, the explicit flags remain available:
`--android-profile` with Android helper/application APKs, or `--ios-profile`
with physical iPhone products/application. For several projects on one worker,
use one profile per configured device:

```json
{
  "schemaVersion": 1,
  "devices": [
    {
      "platform": "android",
      "deviceId": "authorized-local-adb-identity",
      "profile": "checkout-android.json",
      "application": "products/checkout.apk",
      "helper": "products/live-helper.apk"
    },
    {
      "platform": "ios-physical",
      "deviceId": "iphone-public-identifier",
      "profile": "catalog-ios.json",
      "application": "products/Catalog.app",
      "products": "signed-test-products"
    }
  ]
}
```

Paths are trusted local operator configuration, relative to this document or
absolute. They are not accepted in artifact requests or imported project
recipes. The file permits 1–16 devices and only the listed platform fields.
Duplicate aliases or physical identities are rejected. Do not combine
`--devices-config` with individual device/product flags or legacy authority mode.

```sh
python3 -m reproof live-worker \
  --output /var/lib/reproof/worker-output \
  --authority-root /var/lib/reproof/worker-authority \
  --devices-config /etc/reproof/devices.json \
  --project-registration /etc/reproof/checkout-registration.json \
  --project-registration /etc/reproof/catalog-registration.json \
  --coordinator https://coordinator.example:8443 \
  --host-id mac-a --host-incarnation startup-unique-id \
  --enrollment-stdin \
  --host-credential-output /run/reproof/worker-host-credential.json
```

Each registration file contains exactly `project` and `collectionPolicy`.
Enrollment and transport credentials go through private stdin, as described in
the shared coordinator guide. Non-loopback worker listeners additionally need
their TLS certificate, key, and advertised host; none are read by the example's
default loopback worker listener. Actual network/device use requires the
operator's authorized deployment inputs.

The shared coordinator issues a fresh bounded grant for the selected current
project at session/reservation admission. A reservation carries that same grant
into startup. It does not select a default project or replace an explicit
wrong-project grant. Prepared remote sessions reserve on the physical worker
before fixture effects. A remote close remains available after ordinary grant
expiry or host revocation, but must confirm the exact owned session before the
coordinator releases its reservation.

## Inventory and recovery

Workers refresh configured device presence and ownership every five seconds.
Presence probes only filter configured identities; they do not claim, reset,
or recover a device. Android, physical iOS, and simulator probes have bounded
command timeouts. A failed probe reports that platform absent. Adding another
device/product selection requires trusted local worker configuration and the
normal enrolled assignment; a discovered serial is never auto-assigned.

Inventory protocol version 2 uses the same canonical opaque fingerprint as
G1 physical leases. Raw serials/UDIDs remain local and are absent from public
inventory replies. Reports bind host generation/incarnation, alias, profile,
authority root, and ownership generation. The host obtains release metadata
from its actual durable G1 journal. A receipt digest binds the fields;
authenticated trusted-host transport supplies provenance, not the digest alone.

The coordinator separately persists an ownership hold and a minimum release
generation. Missing reports, restart, or state strings such as
`quarantined → recovering → available` cannot erase it. Normal G1 release can
clear its held generation; unknown/quarantined ownership requires G1
reconciliation and a later release. An old release or another authority root
cannot clear a newer hold. G1 recovery does not replace project sanitation or
fixture cleanup evidence.

The browser device list, job availability selection, and session/reservation
admission consult this registry for enrolled remote devices. Reports older than
15 seconds, missing ownership evidence, or changed host/profile bindings prevent
admission. The physical worker still performs the final canonical claim.
`inventory-v1` state is not silently adopted into `inventory-v2`; incompatible
state needs reviewed offline migration.

## Artifact HTTP/API

All routes use the existing worker transport credential and current enrolled
host/project authorization. Allocation also requires a trusted project/policy
registration. Subsequent requests send `X-Repro-Project-Id`; the Python client
remembers it after allocation, or accepts explicit `project_id` after reconnect.

| Operation | Route |
| --- | --- |
| Allocate | `POST /v2/artifacts/uploads` |
| Status/resume | `GET /v2/artifacts/uploads/{objectId}` |
| Chunk | `PUT /v2/artifacts/uploads/{objectId}/chunks/{offset}` |
| Finalize | `POST /v2/artifacts/uploads/{objectId}/finalize` |
| Read/Range | `GET /v2/artifacts/{objectId}` |
| Tombstone | `DELETE /v2/artifacts/{objectId}` |
| Retention health | `GET /v2/artifacts/maintenance` |

```python
import time
from reproof.live.worker import WorkerClient

# worker_token comes from the service's private credential channel.
client = WorkerClient("https://worker.example:9876", worker_token)
published = client.upload_artifact_bytes(
    b'{"schemaVersion":1}', project_id="checkout", kind="manifest",
    metadata={"format": "json"}, retention_class="original",
    retain_until_ms=int(time.time() * 1000) + 60_000,
)
body = client.download_artifact(published["objectId"], project_id="checkout")
```

Default worker limits are 64 MiB/object, 256 MiB/project, 512 MiB/host,
1 MiB/chunk, 512 chunks/object, and 8 KiB metadata. Association and chunk
metadata remain charged after payload deduplication or deletion; terminal rows
are bounded too. The aggregate DiskBudget also includes recording storage.
A download response is at most 4 MiB; read larger objects in explicit ranges
whose `end` is exclusive in the Python API. A ranged response must have the exact
206 status, Content-Range, and length; an ignored Range is rejected.

Offsets/digests are durable. Identical repeated chunks are idempotent; changed
overlaps, gaps, wrong size/digest, or stale upload generation are rejected.
The client may query status/retry transfer operations after a lost response.
It never retries an uncertain native input. HTTP clients and worker requests
have absolute deadlines, including trickled headers/bodies.

Publication uses verified EvidenceStore staging, fsync, and atomic publication,
with authorization rechecked across I/O. Transfer objects use the separate
`artifact-v2/objects` namespace while sharing the recording DiskBudget. An
identical transferred digest cannot delete the original recording's object.
Sharing or nesting the recording and transfer EvidenceStore roots is rejected.

Retention comes from the registered collection policy; pixels/logs require
their permitted categories and explicit test-data mode. Application uploads
must match a registered build digest. The bounded background sweep removes
expired partials and published associations, retries active pins and failed
cleanup, and exposes aggregate maintenance health. It never releases a disk
charge before cleanup is confirmed. Interrupted pre-association allocation is
recovered only through this transfer root's exact budget owner; unknown files
without that ownership proof stop healthy startup for offline recovery.

Only one live process may own a transfer store. Shutdown stops retention,
drains admitted transfer operations, and preserves storage while HTTP consumers
release their pins. Prior transfer formats/configurations are rejected rather
than silently orphaned or reused.

## Verification scope

`python3 scripts/release-check.py --goal G6 --effects filesystem,process,loopback`
runs the registered software checks. G1 supplies current Android/iOS helper
compilation and executable boundary checks; G3 supplies actual MP4 encoding and
independent decoding. Two actual worker CLI processes are exercised on this Mac
with owned synthetic hardware adapters, actual host clocks, enrollment,
canonical reservations, registrations, and artifact transport.

Physical iPhone/Android operation, installed-app query compatibility, signed
product acceptance, two-Mac TLS/USB behavior, and company QA/fixtures are pending
their authorized environment inputs. Synthetic adapters and compilation do not
establish these acceptance results.
