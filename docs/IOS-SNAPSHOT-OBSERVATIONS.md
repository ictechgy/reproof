# Local snapshot UIKit observations (v3)

This opt-in path connects explicit local source snapshots to UIKit observation
preparation and an unsigned Simulator build. It does **not** change v2 limits,
G9, execution wire limits, external-AI consent, fixture readiness, `knownStart`,
or replay verdicts. It is not company-app runtime qualification.

## Explicit inputs and reference

First use `reproof source-snapshot capture` with an explicit selection JSON.
No repository discovery, Git execution, archive extraction or source upload is
performed. Review the selected build code before executing it: a local Xcode
build executes trusted build scripts and is not a sandbox or network firewall.

The v3 profile has `schemaVersion: 3`, `kind: uikit-observation-v3`, and the same
application/project/target/build/tap/screen fields as v2. Replace `sourceInputs`
with this compact reference to the **original** captured snapshot:

```json
"sourceSnapshot": {
  "schemaVersion": 1,
  "kind": "reproof-local-source-snapshot",
  "digest": "<64 lowercase hexadecimal characters>",
  "files": 22000,
  "bytes": 90112000
}
```

The digest/count/byte values must match the actual verified snapshot. No local
path belongs in that reference. Optional `workspace` and `inputExtensions`
retain their existing restrictions. The tool checks required metadata paths
against the actual manifest, not an inferred source tree.

A digest identifies bytes; it does not authenticate their origin or authorize
execution. Locally rewritten, internally consistent manifests and receipts are
not third-party provenance, immutable storage or proof of historical actions.

## Prepare, then build

Example paths below are placeholders; select and review your own inputs. Budgets
are explicit at each operation, not adopted from a stored profile or receipt.
Leave room for the generated runtime files within the same final budget.

```sh
reproof ios-instrument --source original-snapshot --output prepared \
  --profile observation-v3.json \
  --snapshot-max-files 23000 --snapshot-max-bytes 157286400

reproof ios-app-build --source prepared/snapshot --output build \
  --profile prepared/auto-profile.json \
  --snapshot-max-files 23000 --snapshot-max-bytes 157286400
```

Both CLI operations require the explicit v3 `--profile`; the legacy build path
does not infer v3 from a materialized source tree.

Preparation emits a new `snapshot/`, `auto-profile.json`,
`original-manifest.json`, `changes.json` and `instrumentation.json`.
`changes.json` is a bounded path/size/hash list, not an unbounded textual diff.
The profile embedded in generated code continues to reference the original
snapshot. The sidecar preparation receipt binds both original and prepared
snapshots, the profile and changed-file metadata, avoiding a self-digest cycle.
Only the selected project and fixed generated instrumentation files may change;
other selected product inputs must retain their bytes.

The build materializes the selected snapshot into a fresh `source/`, invokes
Xcode for generic iOS Simulator with signing disabled, and checks selected input
contents afterward. Build receipts distinguish compilation from behavior:
`buildCompleted` can be true while `behaviorVerified` remains false. No Simulator
is booted and no app is installed or executed by these two commands.

## Bounds and failure behavior

- Default source budget remains 900 files / 60 MiB unless explicitly selected.
- Hard source envelope: 32,768 files / 256 MiB, at most 8 MiB per file.
- Project/plist/workspace metadata: at most 128 files / 16 MiB aggregate, with
  additional parsed-structure bounds.
- Derivation changes: at most 16 files / 16 MiB aggregate, subject also to the
  selected per-file and final source budgets.
- Ordinary source payloads are processed file by file, not kept in one byte
  dictionary. Manifest/hash metadata is proportional to the bounded file count.
- Fresh output only. Symlinks, hard-linked inputs, unsafe paths, NFC/case/prefix
  collisions and unexpected reserved instrumentation inputs are rejected.
- Failure or cancellation may leave private output. Uncertain ownership never
  authorizes deleting replacements. An executed build directory is never
  recursively removed automatically. Inspect and clean retained data yourself.

Source budgets do not cap the compiler's generated files, package caches or all
process memory. Build code must be trusted independently. No protection against
a malicious same-UID process racing every filesystem operation is claimed.
See [local snapshot semantics](LOCAL-SOURCE-SNAPSHOTS.md) for lifetime ownership,
FD limits, `cleanupIncomplete`, content identity and non-atomic capture caveats.

## Dependency-cache boundary

The repository's local [ReproofSDK Swift package](../ios/SDK/README.md) is a
separate, dependency-free integration. It does not enable SPM imports in this
snapshot path. When a `.xcworkspace` is declared, the closed-workspace validator
rejects both local and remote package references and package-product
dependencies. A pre-resolved cache or `Package.resolved` file does not override
that rejection. Without a declared workspace, that validator returns without
applying this workspace-specific rejection; this is not an approved SPM-cache
or import contract for single-project builds. Neither path implicitly admits
private caches or credentials. See [Xcode evidence scope](XCODE-SUPPORT.md).

## Deliberate unsupported cases

- v3 device/signing builds and native runtime qualification;
- SPM package references in a declared `.xcworkspace`, and workspace
  structures rejected by the existing closed-workspace validator; this includes
  portions of the reported company workspace architecture;
- broader SwiftUI observation coverage or Android snapshot instrumentation;
- implicit dependency downloads, credentials or private package-cache import;
- executable mode, ACL or xattr preservation;
- large-app G9 repair/transfer: the independent 1,024-file / 64 MiB execution
  transfer boundary is unchanged;
- replay equivalence, server determinism or defect verification from a build.

The existing local iOS artifact path has a separate 1 GiB tree limit. This is
not an increase to G9 artifact budgets. Source size success alone does not prove
that a particular company's 610 MiB app or complete QA loop is supported.
