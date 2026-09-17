# Bounded AVFoundation video — G3

The general service now uses transient source storage and the recording profile
described in [Issue recording](ISSUE-RECORDING.md). The G3 results and v1 defaults
below describe the original API and remain compatibility evidence. Direct
`VideoFrameSink` construction still defaults to v1; `Lab.create_video_sink()`
defaults to v2. D3's actual capture and final acceptance are tracked separately.

G3 adds real H.264 MP4 segment production to the trusted G2 recording path.
It does not change the legacy `frame-references-only` recording format, claim
native-device capture cadence, or provide browser playback (G7).

The parent host passed the complete G3 gate on 2026-09-11 UTC: 80 tests,
including actual H.264 encoding and independent decoding of all 24 source
frames, 21 malformed-input cases, and seven process/storage failure cases.
The playable files and result are retained under
`artifacts/qa-delivery/g3-native-gate/20260911T185228Z-45747/`.
G2's 98 checks and the 214 affected legacy identities also passed; these suites
contain 355 distinct tests after removing their shared G2 boundary checks.

## Trusted composition API

The helper path and limits are local administrator/composition inputs. They are
never accepted from a recording, imported package, command, URL, or media path.
The sink does not accept a caller-provided recording ID. `attach_frame_sink`
binds it to the actual `RecordingSession`, registration policy, `EvidenceStore`,
and `DiskBudget` before the first frame can be acquired.

```python
limits = VideoLimits(
    segment_duration_ms=10_000,
    max_queue_frames=4,
    max_active_finalizers=1,
    finalization_timeout_seconds=10,
)
sink = lab.create_video_sink(
    helper=trusted_repro_video_binary,
    limits=limits,
)
session = lab.create_release_session(
    device_id,
    owner,
    controller_id,
    registration,
    application_id="ios_app",
    build_id="original",
    preparation_receipts=receipts,
    frame_sink=sink,
)
```

`RecordingSession.record_frame` still authorizes the sample, publishes and pins
the source object, and only then calls `accept_frame`. The video worker checks
that `FramePublication` metadata, supplied bytes, and a fresh content-store read
all have the same digest and size. It adds a `finalizer` pin so retention or a
tombstone cannot race a delayed encoder read. There is no second capture or
filesystem-import path.

The admitted G2 reference and timing must match the publication. Repeated
identical screens share one source-consumer pin per digest, while each
acquisition keeps its own sequence and timing. Durable segments stay pinned
until their references have been attached and encoding has stopped.

`stop_release_recording` commits the existing G2 barrier while Lab admission is
excluded and sets `stopAdmission` immediately. Only then may the video sink
finish. Event sequences and receipt knowledge remain fixed at the barrier.
Successful MP4 and manifest references are added only for pre-barrier frames,
then the G2 original is frozen. A video timeout/failure adds an incomplete
reason without admitting another original input or changing any receipt.

## Protocol and segment contract

`ReproVideo` is built only from `native/macos-video/` with the installed Apple
toolchain. Protocol 1 is a binary framed stdin stream:

- `RLVID001`, a four-byte big-endian configuration length, and strict JSON;
- `F`, four-byte metadata/body lengths, strict JSON, and PNG/JPEG bytes;
- `E` with zero lengths, followed by EOF.

JSON uses sorted keys, compact separators, and unescaped forward slashes.
Duplicate or escaped-equivalent keys and floating representations of integer
fields are rejected. Segment PTS starts at zero and is bounded by the recording's
ten-minute maximum. Normal EOF after `E` is valid.

The helper accepts no commands, URLs, input paths, output paths, environment
policy, or recording identity. It writes only `segment.partial.mp4` and then
`segment.mp4` in the trusted per-segment working directory. Unknown JSON fields,
truncation, trailing data, MIME/decode disagreement, digest mismatch, geometry
change, pixel excess, non-increasing acquisition sequence/PTS, compressed byte
excess, and file size excess fail closed. stdout contains bounded per-frame
`accepted` receipts and one `finished` receipt; stderr is bounded and must be
empty on success. Python owns the helper process group and terminates it on its
fixed deadline or stream excess.

The helper inherits the recording's file-lock descriptor. If its parent exits,
recovery remains excluded while that helper still holds the lock.

Each segment has exactly one geometry and one clock-mapping identity. Rotation
occurs before the registered duration bound is crossed, and on geometry,
mapping, or frame-count changes. Input offsets remain irregular; the first
frame is segment-local PTS zero and later PTS retain their actual elapsed
offset. No constant FPS is synthesized.

`host-acquired` and valid `provider-mapped` samples retain their earliest/latest
recording offsets and uncertainty. `native-unmapped` samples use publication
order for playable PTS only: their manifest has no capture interval and says
`display-publication-order-only`. A wall/display timestamp is never promoted to
a native capture timestamp.

## Durability, loss, and recovery

The sink reserves additional `spool` queue capacity and `encoding` output
capacity. Every sealing segment obtains its own `finalization` reservation,
including the content-store metadata charge. G2's journal and original-freeze
reservations are never reused as video capacity. The video journal separately
reserves 8 MiB, caps SQLite at 2,048 pages, and permits at most 512 loss rows.
Each catalog reserves another 256 KiB before SQLite metadata is created; that
charge remains while the catalog persists. Runtime reservation identities bind
both the catalog and recording. Reopening the same catalog reuses its charge.
Defaults are:

- 10,000 ms segment bound, 16 frames per segment, 32 segments;
- 3 MiB compressed frame, 4,194,304 decoded pixels;
- four queued frames / 12 MiB queue;
- one active finalizer per recorder, two finalizers process-wide, and at most
  32 Lab-created video sinks;
- 32 MiB segment, 256 MiB total video;
- 64 KiB each helper stdout/stderr, 10 second finalization.

An acknowledgement means only that AVAssetWriter appended a frame. A segment
becomes durable after `finishWriting`, file and directory flushes, size/digest
validation, content-store atomic publication, and the durable video journal
transition. The manifest records segment checksum, bytes, geometry, frame
count, first/last captured offsets, conservative capture interval, exact
sampling mode, per-frame lineage, and segment-local PTS.

Known loss intervals take priority over a segment when mapping an action to
video. Consecutive clock-discontinuity notifications are coalesced so control
messages cannot grow the frame queue without a bound.

Loss evidence remains distinct:

- `not-acquired` for native/no-sample intervals;
- `captured-dropped` for transport, queue, decode, or pre-accept failures;
- `encoder-accepted-not-durable` for acknowledged frames in a failed segment;
- `unknown-acquisition-interval` for playable `native-unmapped` frames.

The manifest records the actual vulnerable segment/frame interval. It does not
claim a general loss duration. `VideoCatalog.recover_interrupted(recording_id)`
requires the per-recording process lock, never resumes capture or encoding,
validates and retains already published segments, converts unfinished frames
to explicit loss evidence, releases owned active reservations/pins, removes
only derived bounded work files, and publishes an incomplete recovery manifest.
An MP4 filename or container header alone is never recovery evidence.

Source pins and encoding charges remain until an active producer finishes.
Failed work-file deletion or directory flush keeps the work reservation and
stops further encoding. Recovery confirms file removal before releasing its
charge, can retry a failed cleanup, and releases pins left by a crash immediately
after manifest publication. Published or unfinished content-store objects keep
their reservations when a later video-journal write fails.

## Verification

The registered software/native gate is:

```bash
python3 scripts/release-check.py --goal G3 \
  --effects filesystem,process,native-compile
```

The retained native acceptance route is:

```bash
python3 scripts/verify-video.py --backend avfoundation --cases all --output-new
```

It compiles current Swift source into an owned output/module cache, encodes the
parent 24-PNG irregular-cadence/geometry corpus, and invokes the preserved
independent AVAssetReader binary. That reader checks all frame IDs once in
order, moving-block colors, dimensions, and PTS against source offsets. `all`
also runs malformed/truncated protocol, zero-frame, declared native/transport/
queue loss, killed helper, finalizer stall, injected write failure, and malformed
image cases. A missing sandbox codec returns nonzero `blocked` evidence rather
than treating a mock or compilation as encoding acceptance.

The acceptance command retains byte-identical `.mp4` copies for the local
AVAssetReader and manual playback. Their digests match the durable content-store
objects. These are fixed synthetic acceptance inputs; browser playback,
physical-device cadence, and company QA remain separate acceptance scopes.
