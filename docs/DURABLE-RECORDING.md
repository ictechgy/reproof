# Durable recording and evidence storage — G2

G2 adds a trusted local recording path beside the unchanged legacy gesture
recording API.  It does not encode video, prepare fixtures, authenticate remote
administrators, register execution endpoints, or run repair candidates.

## Trusted local session API

Imported JSON remains inert.  Trusted composition code validates a frozen G0
project and separately registers the collection/retention policy:

```python
registration = lab.register_recording_project(
    project_wire,
    {
        "schemaVersion": 1,
        "captureMode": "sample-bound",  # sample-bound | test-data | suppressed
        "retentionSeconds": {
            "original": 2_592_000,
            "intermediate": 86_400,
            "derivative": 86_400,
            "export": 604_800,
        },
    },
    capacity_bytes=512 * 1024 * 1024,
    journal_headroom_bytes=16 * 1024 * 1024,
)

session = lab.create_release_session(
    device_id,
    owner,
    controller_id,
    registration,
    application_id="ios_app",
    build_id="original",
    preparation_receipts=actual_completed_receipts,
    frame_sink=optional_g3_sink,
)
```

The application bundle and artifact digest in the current trusted device
descriptor, and the application platform, must match the selected registered
application/build before the provider is constructed.  A shared native session
additionally requires the same project ID in its current G1 parent grant.
Preparation receipts must be complete receipts for a recipe and operation in
that revision and must predate the recording anchor.  The trusted composition
root is responsible for supplying actual receipts; G4 will own fixture
execution.  No caller boolean can substitute for the process-local
`registration` object.  Re-registering the same project ID/revision with
different project or collection-policy bytes is rejected, and returned project
and policy mappings are defensive copies. Project and collection-policy JSON
together are limited to 64 KiB. Registration reserves four times their UTF-8
size plus 16 KiB for stored metadata before its database commit. A stable local
store/project/revision key preserves that charge across restart without
charging an unchanged registration again.

Each release input supplies the actual provider command and its already
parameterized G0 input independently:

```python
lab.input(session_id, owner, live_command, recording_input=typed_g0_input)
frozen = lab.stop_release_recording(
    session_id, owner, controller_id, controller_epoch
)
```

For coordinate actions, the journal derives coordinates, duration and geometry
from the validated command and current durable frame; a caller locator or
different coordinates cannot replace the operation actually dispatched.  Text
in `live_command` may be delivered to the provider, but the durable journal
receives only the registered `variableId` and a trusted device descriptor's
`recordingTextTarget`.  Providers without that exact text-target contract reject
release text before authority admission or dispatch.  The authority payload
digest remains parameterized.
The recording journal preserves the G1 operation ID, ownership generation and
provider incarnation, while its own event sequence is contiguous from one.

`stop_release_recording` excludes new Lab admission while it commits the stop
barrier.  The frozen original contains exactly the receipt knowledge committed
at that barrier.  An acknowledgement committed afterward is an append-only G0
lifecycle receipt referencing the original digest; it cannot update the
original event.  `RecordingStore.load()`, `list_recordings()` and
`media_timeline()` are the trusted local recovery/read APIs for later G4/G7
composition.

## Sample-bound collection

The G0 `evidencePolicy` flags still describe which classes may be collected;
they do not authorize a sample.  The separate trusted collection policy is
strict and does not alter the G0 project bytes or digest:

- `sample-bound` requires a process-local classification bound to kind, sample
  digest, provider incarnation, native incarnation and acquisition sequence.
- `test-data` is an explicit trusted registration selected before the provider
  is constructed.  It is intended for approved synthetic/test data providers
  that cannot classify each sample.
- `suppressed` publishes no sample.

After acquiring a frame, a trusted local provider can classify it before the
bytes enter Lab streaming, persistence or the future encoder sink:

```python
classification = lab.classify_recording_sample(
    session_id,
    kind="pixels",
    body=acquired_bytes,
    native_incarnation="native_incarnation",
    acquisition_sequence=native_sequence,
    sample_id="sample_id",
    decision="approved",  # approved | sensitive
)
lab.publish_frame(
    session_id,
    acquired_bytes,
    mime,
    width,
    height,
    orientation,
    acquisition_sequence=native_sequence,
    classification=classification,
)
```

A classification cannot be reused for later bytes or a later sequence.
Suppression creates an explicit interruption and leaves the prior safe frame
unchanged.  Accessibility/text observations and logs follow the same policy;
disabled classes are rejected before provider collection, and an unclassified
sample is not streamed or persisted.  Preparation evidence contains only the
registered receipt envelope and payload digest.

The optional `frame_sink.accept_frame(publication, body)` integration point is
attached before acquisition.  It receives only policy-approved bytes after the
frame object is durable.  Encoding, segment rotation and playable media belong
to G3. The implemented bounded AVFoundation sink, barrier integration, loss
classes, reservations and recovery API are documented in
[Bounded AVFoundation video](AVFOUNDATION-VIDEO.md).

Current built-in native providers do not expose a pre-acquisition classifier or
capture-disable handshake.  A release session using those providers is
therefore accepted only when the registered revision permits pixels and the
separate capture mode is `test-data`; `sample-bound`, `suppressed`, and
pixels-disabled configurations fail before provider construction.  This is a
deliberate capability boundary, not a native-redaction claim.

## Storage, retention and recovery

`DiskBudget` uses a SQLite `BEGIN IMMEDIATE` reservation journal shared by
processes.  Journal reservations can use the aggregate capacity, while spool,
encoding, finalization and transfer reservations cannot consume the protected
journal headroom. A release recording reserves 8 MiB for its bounded journal and
lifecycle rows, plus 4 MiB and object metadata for frozen-original publication.
Input JSON has a separate 256 KiB allowance measured in UTF-8 bytes. The journal
charge remains after freeze because those rows still occupy storage. The real
filesystem free-space check is an additional fail-closed bound; tests inject
bounded faults and do not fill the user disk.

Evidence storage requires at least a 1 MiB configured budget; recording budgets
must also fit the reservations above. The default is 512 MiB. At least 512 KiB,
or one eighth of a larger budget, is held for shared metadata/WAL overhead.
Each object reserves another 8 KiB for its metadata and up to eight simultaneous
consumer pins. Digest slots include retained tombstones and are bounded by the
metadata allowance; expiration does not silently recycle a tombstoned identity.
These are managed admission bounds and conservative byte reservations, not
filesystem block quotas against unrelated disk writers.

`EvidenceStore` writes bounded staging files, flushes and fsyncs them, verifies
declared SHA-256 and size, atomically installs the digest path, fsyncs its
directories and only then commits the published object row.  Readers see only
published rows whose files still match.  A crash can leave an inert staged or
orphan file, never a complete reference.  Recovery retains its reservation
until the owning interrupted recording abandons or republishes it.

Objects have `original`, `intermediate`, `derivative` or `export` retention
classes.  Process-shared `recording`, `replay`, `export` and `finalizer` pins
block retention tombstones.  Tombstones are durable even when no object was
published, so a delayed producer cannot republish that digest after another
process or restart revoked it.  Revocation prevents future reads; it cannot
recall bytes already downloaded by a prior authorized consumer.

A failed file deletion or directory flush leaves the physical bytes charged and
the tombstoned object unreadable.  Reopening the store retries that durable
deletion obligation and releases the reservation only after deletion and flush
are confirmed.  A tombstoned frozen-original object also purges the journal's
cached original/events/media/observation rows under SQLite secure deletion; the
digest and lifecycle receipts remain as non-content audit metadata.

On startup, `preparing` and `recording` journals acquire an interrupted stop
barrier. An already `finalizing` journal retains its existing barrier and
original interruption reason. Recovery produces `frozen-incomplete` status and
a failed reconciliation lifecycle receipt. Input, fixture work and uncertain
operations are never resumed. A freeze fully committed before the crash remains
immutable and is not reissued.
The recording journal holds an exclusive process lock for its lifetime, so a
second `RecordingStore` cannot mistake a live writer for a crashed one.

## Clocks and limits

`RecordingTimeAnchor` samples wall time once for `startedAtMs`; later ordering
and display offsets use only the G1 suspend-inclusive host clock.  Wall-clock
corrections therefore cannot move an event backward.  Provider monotonic time
is accepted only through a live conservative `ClockMapping` bound with
`bind_recording_provider_clock`; the media timeline stores earliest/latest
offsets and uncertainty instead of treating native monotonic time as a wall
epoch.  Boot change, native restart, sleep/discontinuity evidence or invalidated
mapping creates truthful incompleteness.

Each recorded frame also declares `host-acquired`, `provider-mapped`, or
`native-unmapped` timing.  Current native bridges without an approved monotonic
mapping use `native-unmapped`, record a `native_timing_unknown` interruption,
and may use host publication time only as display time—not as a precise native
capture timestamp.

The implementation bounds recordings, inputs, frames, staging files, object
size, SQLite pages, metadata size and aggregate charged bytes.  Static public
errors contain no provider payload, raw text, physical device identity or
secret value.

## Local verification boundary

Run the focused software gate with:

```bash
python3 scripts/release-check.py --goal G2 --effects filesystem,process
```

The gate uses owned temporary directories and child processes.  It does not
operate native devices, contact another Mac, encode playable video, exercise a
company app/fixture, transfer evidence to an AI service, or qualify protected
repair execution.

The accepted G2 gate runs 98 cases, including 37 independent boundary cases and
the existing host-clock contracts. An additional 214 existing Live, provider,
HTTP, authority, app-log and harness cases passed. See
`artifacts/qa-delivery/g2-parent-review.md` for source-bound results and the
distinction between software checks and unavailable environment acceptance.

G4 composes this API without changing its barrier or capacity reservations.
Post-stop fixture/device outcomes use `RecordingStore.append_lifecycle` through
the owning `Lab`; the method accepts only cleanup/quarantine/reconcile evidence
for an already frozen digest. See
[Prepared recording and frozen scenario replay](PREPARED-RECORDING.md).
