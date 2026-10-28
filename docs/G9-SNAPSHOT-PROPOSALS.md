# Local snapshot proposals (opt-in, Android artifact identity only)

This route removes the legacy 1,024-file / 64 MiB **proposal-input** bottleneck.
It does not migrate protected G9 verification, remote build transport, execution,
signing, replay, or AI transfer. A completed job is `proposal-ready`,
`verified: false`, with no candidate execution evidence. Existing repair
configurations keep their existing behavior and limits.

## Operator configuration

Capture an explicit file selection with `source-snapshot capture` first (see
[local snapshots](LOCAL-SOURCE-SNAPSHOTS.md)). No directory discovery, Git command,
archive extraction, secret collection, or upload is performed. Snapshot digests
identify bytes, not trusted provenance or an atomic source-tree point in time.

An issue-runtime project's `repair` registration may opt into this exact shape:

```json
{
  "schemaVersion": 2,
  "kind": "local-snapshot-proposal",
  "sourceRoot": "/private/operator-selected/source-snapshot",
  "sourceSnapshot": {
    "schemaVersion": 1,
    "kind": "reproof-local-source-snapshot",
    "digest": "<snapshot digest>",
    "files": 10000,
    "bytes": 73400320
  },
  "snapshotBudget": {"maxFiles": 12000, "maxBytes": 83886080},
  "protectedPaths": ["tests/Regression.kt"],
  "originalArtifact": "/private/operator-selected/original.apk",
  "buildRecipeId": "build_app",
  "agent": {
    "kind": "local-unified-diff",
    "patchFile": "/private/operator-selected/proposal.diff",
    "baseSourceDigest": "<original full-source G0 digest>"
  }
}
```

The snapshot reference's digest is **not** the G0 build `sourceDigest`.
`legacy_source_digest(verified_manifest)` in `reproof.snapshot_repair_source`
computes the existing canonical sorted `{path,digest,size}` manifest digest;
small legacy BlobSet inputs have exactly the same digest. Verify actual snapshot
contents before using metadata to declare a build. The G0 project still supplies
editable paths, protected build/fixture/regression recipes, approved original
build identity, and evidence policy. Only an Android application's selected
original build is accepted. Artifact SHA-256 is streamed from one regular APK
file, at most 150 MiB, and repeatedly rechecked. This is inert byte identity,
**not APK validity, installation, signature, or behavior verification**.

Startup validates declarations only. Execution still requires the same approved
issue revision and reproduced baseline campaign, project authorization,
cancellation/revocation checks and retention. Use the existing repair job API/CLI
with mode `propose`. `verify`, external AI agents, transfer policies, diagnostic
collection, and protected executors are rejected for this route before provider
invocation. No snapshot configuration can opt into them.

## Bounds and output

The operator must explicitly select budgets, within 32,768 files / 256 MiB and
existing snapshot per-file/path rules. Editable inputs are separately bounded:
128 selected text files, 1 MiB each, 8 MiB aggregate, and the existing 512 KiB
serialized proposal packet ceiling. Only editable text and authored QA fields
reach the local adapter; protected files and APK bytes do not. The patch remains
at most 256 KiB. Candidate derivation changes at most 16 files with bounded exact
edits and streams all other bytes unchanged. A version-2 proposal stores exact
edits, compact candidate reference and input patch receipt instead of regenerating
a potentially expensive textual diff. The original snapshot is never edited.
The candidate is stored under the private repair journal at `<job-id>/candidate`;
the proposal sidecar is `<job-id>/proposal/proposal.json`. These are local paths,
not remotely executable or automatically transferable artifacts.

Before any candidate output is written, the journal reserves the selected maximum
content bytes plus bounded manifest capacity and 2 MiB proposal capacity. The APK
is not copied. Set `repairStorageBytes` large enough for all concurrent/retained
reservations; no silent cap increase occurs. The shared service's separate 8 MiB
journal allowance remains unchanged. Records contain compact references, not
thousands of source paths. Snapshot contents are revalidated on derivation and
publication; a later consumer must verify again because local files are mutable.

### Conservative retention is intentional

Published snapshot trees do not retain process-lifetime ownership pins for every
file. Failure, cancellation, expiry, and restart therefore **preserve** candidate
and proposal output rather than recursively delete it using a path or digest as
ownership proof. Retention marks cleanup blocked and retains its quota reservation;
expired proposals cannot be fetched. Even a small late failure can leave private
residue. There is no automatic reclamation or operator deletion API in this slice.
Do not expect a restart to recover quota. Plan private storage retention and stop
accepting jobs when full. Filesystem ownership protections do not isolate a
malicious process running as the same OS user.

## Remaining limits

* No protected candidate build/replay/verification or large remote-wire migration.
* No iOS artifact-tree support, multi-artifact support, artifact cap increase, or
  company-app qualification.
* Existing source filename/content-selection protections remain; a snapshot is
  not permission to include credentials or private data in an external provider.
* Synthetic large-source/inert-APK checks demonstrate the host proposal flow,
  not real app compilation or correctness.
