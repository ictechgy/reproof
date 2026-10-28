# Android Live capture width

`live-serve` and Android workers accept the operator setting
`--android-capture-max-width` with one of **480, 720, 960, 1280, 1440, 1920**.
The default remains **960**. This limits encoded image width; it does not change
raw device input coordinates, app state, reset contracts or reproduction verdicts.
It does not widen the runtime profile geometry: actual encoded frames must still
satisfy that profile's maximum width/height and allowed orientations.

Nondefault values require `shared-v2`, an Android Live helper/app, and an explicit
**general runtime profile** (`--android-profile`). Sample/v1 profiles and
`legacy-offline-v1` are unsupported for this opt-in and are rejected before
provider/device effects. Their default-960 path remains available as before.

The host sends a version-1 capture policy with the selected width and fixed
resource limits. The helper independently checks exact fields, integer types,
allowed widths, profile and authority. Host startup requires its exact policy
acknowledgement and capability version before installing or launching the target.
There is no silent fallback to 960. Old helpers reject the unknown policy envelope
before acquiring UiAutomation; missing or mismatched acknowledgements fail closed.
General-target capture still waits for its existing target readiness gate and
foreground checks. No additional capture-authority command is introduced.

## Fixed resource ceilings

Higher resolution is opt-in, not a global budget increase:

- Maximum encoded frame: **3 MiB**; native buffer remains **12 MiB / 16 frames**.
- Maximum scaled output: **8,294,400 pixels** (1920 × 4320; about 31.6 MiB ARGB).
- Raw display/bitmap preflight ceiling: **16,777,216 pixels** (about 64 MiB ARGB).
- Output axes also remain bounded to the existing host limit of **8192**.
- Checked dimension arithmetic and actual bitmap checks precede scaling.
- JPEG staging storage is fixed at 3 MiB; the completed frame copy is at most
  another 3 MiB. Encoding overflow is rejected before staging can grow.

Display metrics are checked before requesting a screenshot and actual dimensions
are checked afterward. Rotation may race those observations, and UiAutomation
allocates its original screenshot internally: these checks are **not a total
process-memory guarantee or OS sandbox**. Platform/codec internal allocations
are not measured by the output byte limit. Oversized or pathological captures
fail closed; normal default-960 geometry is unchanged. Larger frames may hit the
unchanged byte ceiling, reduce throughput or fail capture rather than being
silently accepted with raised limits.

A larger width does not establish OCR accuracy, visual equivalence or network
fixture determinism. Host tests and pure Kotlin policy/geometry/encoding-bound
tests can validate contracts; ordinary CI compiles the helper without launching
an emulator or app. Actual device performance and company-app behavior require
separate authorized runtime qualification.
