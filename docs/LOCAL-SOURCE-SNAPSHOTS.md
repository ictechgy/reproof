# Local bounded source snapshots (T8/T9 foundation)

`reproof source-snapshot` captures explicitly selected regular-file bytes into a
new local directory, verifies them, and materializes them into another new local
directory. It does **not** execute a build, inspect a repository automatically,
run Git, extract an archive, contact a server, upload source, or invoke AI.

This is a usable large-source staging primitive, **not large-repository support
in existing instrumentation or G9 repair**. Those consumers still use their
existing 900/1,024-file and 60/64 MiB contracts. Large application artifacts are
also outside this source-only feature. No existing execution/transfer, artifact,
editable-path, external-AI consent, or prepared-replay policy is changed.

## Explicit local selection

Create a JSON file listing exactly the paths you intend to copy:

```json
{
  "schemaVersion": 1,
  "kind": "reproof-source-selection",
  "paths": ["App/App.swift", "App/효과.metal", "App.xcodeproj/project.pbxproj"]
}
```

All paths are relative to the selected source root. The tool does not expand
wildcards, discover dependencies, read Git configuration, follow submodules or
resolve LFS pointers. Omitted files stay omitted. A selection file can itself
contain sensitive project structure: keep it local and private.

The existing conservative inert path rules remain: NFC, case-consistent names,
no traversal/absolute paths, hidden components, credential filenames/extensions,
reserved aliases, control characters, shell syntax, prefix collisions, symbolic
links or hard links. Files must be regular files, with at most 32 path components
and 512 UTF-8 path bytes. Selected contents are opaque bytes: **a filename check
cannot detect secrets embedded in an ordinary source file**. Explicit selection
is not authorization to transmit them. No content is executed or interpreted.

## Commands

The default budgets are 900 files and 60 MiB of source bytes. A larger local
capture requires explicit limits, bounded by 32,768 files and 256 MiB:

```sh
reproof source-snapshot capture --source <local-source> \
  --selection <selection.json> --output <new-snapshot> \
  --max-files 32768 --max-bytes 268435456

reproof source-snapshot verify --snapshot <snapshot> \
  --max-files 32768 --max-bytes 268435456

reproof source-snapshot materialize --snapshot <snapshot> --output <new-source> \
  --max-files 32768 --max-bytes 268435456
```

Budgets must be explicitly supplied again when verifying/materializing a larger
snapshot: a stored document cannot increase an operator's resource allowance.
`--max-file-bytes` may reduce the 8 MiB per-file ceiling;
`--max-manifest-bytes` may reduce the 24 MiB metadata ceiling. The explicit
selection JSON also has a 24 MiB input ceiling. Metadata nesting, token counts,
string lengths, directory counts (65,536), and path depths are bounded before or
during processing. Data is streamed in 64 KiB chunks, not held as one large
`BlobSet`.

A capture occupies at most its selected source-byte budget plus its manifest
budget, excluding filesystem metadata/allocation overhead. Materialization
occupies at most the selected source-byte budget, also excluding that overhead.
These are **per-operation logical byte limits**, not an OS disk quota or a global
concurrent-operation quota. Leave filesystem headroom. Large sparse files are
charged at their logical size; no sparse-file optimization is promised.

## Format and trust

A snapshot contains `manifest.json` and a `data/` tree. The canonical manifest
has `schemaVersion: 1`, `kind: reproof-local-source-snapshot`, a sorted `files`
list (`path`, `size`, `sha256`), and a digest of its canonical metadata body.
The manifest is strictly parsed and actual file bytes are rehashed on verify and
while materializing. Extra files, changed links/identities and malformed or
changed metadata are rejected. Success reports only operation, digest, file
count, byte count and `provenance: unverified-local-content`; it does not print
source names or contents.

**A digest is not a signature, trusted provenance, a readiness receipt or an
execution capability.** Someone who can change both files and manifest can
construct another internally consistent snapshot. The tool does not claim the
snapshot came from a particular Git commit or company build. Capturing multiple
files is not an atomic filesystem-wide point-in-time transaction. Same-UID
hostile writers are not sandboxed by this feature; keep source and destinations
under exclusive operator control. Reverify before use; a successful check does
not stop later edits.

Output directories must not exist and must not overlap inputs. Files are created
with private non-executable permissions (0600), directories with 0700. Executable
bits, ownership, ACLs, xattrs, symlinks and other filesystem metadata are not
preserved. This is an inert byte snapshot, not an arbitrary filesystem backup.

## Failure and cancellation

Rollback is **best effort**, including ordinary failure, byte-limit overflow and
cancellation. Preexisting output is never overwritten. Cleanup requires live
file-descriptor ownership pins for the entry and its directory ancestors, plus
unchanged completed-file metadata. A saved device/inode number is not ownership:
filesystems can reuse inode numbers after a file is removed.

The tool retains at most 32 recent file pins and the active directory prefix
(up to 32 directories, in addition to the output root/parent). After leaving a
subtree, it never reenters that unpinned prefix for writes. Evicted pins are not
reacquired as cleanup authority. These bounds allow large successful operations
without retaining tens of thousands of descriptors, but **even ordinary failures
can leave newly created output behind**. Verification of completed/unpinned files
is content verification only, not authority to delete them. Capture leaves the
`data/` prefix to write the root manifest, so a failure at that late stage may
retain source data even for a one-file snapshot.

Changed files—including same-inode edits—foreign entries and entries lacking live
ownership proof are preserved. The error reports `cleanupIncomplete: true` when
safe cleanup cannot finish. Do not mistake this for successful rollback: inspect
the destination and residual local source data before retrying with a new name.
No unknown path is automatically removed to make a retry work. There is no
recursive deletion of a caller-supplied path based on stored inode numbers.

The pin bound is not the total process-descriptor bound: bounded directory scans,
source reads and runtime descriptors add overhead. Deep trees can safely fail
under very low process limits (for example 96 descriptors); the tool does not
raise limits or close unrelated descriptors to force success. Synthetic child
tests cover success at 256, controlled failure at 96, and descriptor counts
returning to baseline in both cases.

The Python API also accepts a cancellation event (`is_set()`); CLI interruption
is reported as cancelled after safe cleanup, or cleanup-incomplete when that is
the actual outcome. Publication is not promised to survive power failure: no
filesystem durability/transaction attestation is made.

## Validation and remaining work

Portable tests cover budgets, malformed metadata allocation bounds, path/link
and replacement races, actual-byte tampering, cancellation, safe rollback and
installed-package availability. Scale tests capture/verify/materialize real
synthetic 22,000- and 32,768-file trees with 4 KiB files, both exceeding 64 MiB,
and measure peak Python allocations. This is not measurement of a company repo,
OS cache/RSS, native build performance, or application reproducibility.

Later integration must explicitly migrate observation profiles away from embedded
large path lists, connect immutable build inputs to the existing small edit
surface, add bounded disk-backed host/guest transfers with paged manifests, and
separately address large app-artifact budgets. Those capabilities are not enabled
by creating or importing one of these local snapshots.
