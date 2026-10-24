# Offline Vision diagnostic (not native acceptance)

This is a narrowly scoped reconstruction of an approved diagnostic, not recovery
of the previous draft's source. It replays **only two exact, synthetic inventory
fixture JPEGs** from [run 40068410548](https://github.com/ictechgy/reproof/actions/runs/40068410548).
It does not repair, rerun, or replace the failed native inventory evidence. The
original third image passed; the fourth image's observer timed out after 15
seconds. A successful offline replay would narrow the investigation, not prove
native reliability, application behavior, or a fix.

`nativeAcceptance` is unconditionally `false`. `diagnosticPassed` independently
means both pinned offline images returned valid OCR and satisfied the original
oracle **within their own 15-second deadlines**. No production code, inventory
contract, native acceptance state, or release inventory is changed.

## Inputs and provenance

`provenance.json` records the source archive, source commit, exact bytes and
SHA256 hashes. The archive hash was verified before extraction. The JPEGs are
original bytes, not recompressed screenshots. `ObserveFrame.swift` is unchanged
from commit `6eb207897f5819bf74b949c062a88e67df810abd`.

- `03-invalid-attempt-001.jpg`: 93,904 bytes; SHA256
  `3248da8f3e908bf19bbbbecadeb147f876ce6fe26f36dca38440f5668eb98f50`.
- `04-created-attempt-001.jpg`: 81,847 bytes; SHA256
  `ac74d667cc9aa523790993c790dbeadf72f450b012a6d8feb32dbbe0abbeec23`.
- `ObserveFrame.swift`: 1,819 bytes; SHA256
  `18ad97694079c779f39111b0f31d0ab6336e5644d61860263c604503c28573c9`.

Both image paths, byte counts/hashes, and the observer path, size/hash are
validated before **any subprocess**, including host metadata checks. Symlinks
and non-regular inputs are rejected. The captured bytes are copied exclusively
into an owned temporary directory with read-only input files. Only these copies
are passed to the compiler and observers, preventing subsequent changes to the
checkout from changing the approved input bytes.

## One-shot execution boundary

The workflow runs only on `pull_request: opened` events affecting this directory
or its workflow file, only for public, same-repository PRs. It has no push,
`synchronize`, `workflow_dispatch`, schedule, or privileged target trigger. It
uses `macos-15`, a five-minute job cap, `contents: read`, and a pinned checkout
commit with persisted credentials disabled. There are no package installs,
caches, uploads, secrets, app launches, simulator/device operations, Live
sessions, or diagnostic network calls. Checkout is the only external action;
results are emitted as sanitized JSON to workflow stdout, not uploaded artifacts.

The runner also requires the first attempt of a GitHub-hosted macOS PR run for
`ictechgy/reproof`. It requires the preinstalled
`DEVELOPER_DIR=/Applications/Xcode_16.4.app/Contents/Developer` and verifies
`Xcode 16.4` / `Build version 16F6` and macOS major version 15. It does not switch the system Xcode selection,
install tools, or fetch components. Child processes preserve the ordinary hosted
environment and cache locations, overriding only `DEVELOPER_DIR`, to avoid
introducing a fresh-HOME/cache confound relative to the original observer.
Environment values are never logged. It records only bounded, allowlisted
`sw_vers` fields and `arm64`/`x86_64` architecture. Host metadata calls each have
a ten-second timeout and monotonic deadline. Invalid input, host, or compiler
preconditions produce **zero observer calls**.

The unchanged Swift observer is compiled once with the original flags:

```text
/usr/bin/xcrun swiftc -O -framework Vision -framework Foundation ObserveFrame.swift -o observe-frame
```

Compilation has a 90-second timeout and monotonic return deadline. Only after
successful compilation, the runner launches exactly two fresh processes in this
order: third image, fourth image. Each has its own 15-second subprocess timeout
and monotonic deadline, checked after return and after parsing/oracle evaluation.
Reaching the deadline, including equality, fails closed. The second image is
still attempted if the first times out, fails, or has invalid OCR. No process is
retried; partial stdout from a timed-out process is not treated as evidence.

A workflow rerun is blocked by the run-attempt guard. Repeating this diagnostic,
changing its pinned inputs, widening the host/toolchain scope, or running an app
or device requires separate review and authorization. Do not bypass the guards
to obtain a passing run.

## Original exact OCR oracle

Revision: `exact-required-observed-forbidden-v2`.
Text is normalized by uppercasing and collapsing whitespace, then compared for
exact equality, not substring matching. Required phrases need confidence at
least `0.9`. Any exact forbidden phrase in **all** observed text, including a
zero-confidence reading, blocks the oracle.

- Third image requires `CREATE ITEM`, `ITEM TOTAL ZERO`, and `NAME REQUIRED`;
  forbids `ITEM TOTAL ONE`.
- Fourth image requires `INVENTORY LIST`, `ITEM TOTAL ONE`, and `BLUE MUG`;
  forbids `INVENTORY IS EMPTY` and `NAME REQUIRED`.

OCR JSON must be a bounded observations object with string text and finite,
non-boolean numeric confidence in `[0, 1]`. Duplicate JSON keys and unexpected
fields are rejected. Only validated synthetic OCR rows can be retained. A valid
but late result may be shown for diagnosis; it can never pass the diagnostic.
No raw stderr, subprocess exception text, host paths, or environment variables
are published. Stderr retains only up to 16 exact stage/timing records from the
first 16 KiB: `started`, `image_read`, `vision_started`, `vision_completed`,
`output_written`, `failed`, or `invalid_arguments`.

## Portable verification

These tests use only the Python standard library and mock every subprocess.
They do not run Apple tools or the observer and may be run on Linux:

```sh
python3 -m unittest discover -s diagnostics/offline-vision -p test_replay.py -v
```

The workflow runs those inert tests first, then the diagnostic using preinstalled
Python. The diagnostic requires a **new** output filename, reserved with
exclusive creation before any subprocess. Existing reports are never replaced.
A write, flush, or close failure does not rerun anything: the completed,
sanitized report is still printed to stdout, including the file-output failure
and all completed observations. The process exits nonzero for any failed
precondition, diagnostic result, or report-file write/close failure. Stdout itself
must remain available for this fallback; closed output streams cannot be made
reliable by repeating the diagnostic.
