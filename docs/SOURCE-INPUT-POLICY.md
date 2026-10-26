# Inert observation input paths and extra assets

Configured UIKit and Views observation profiles accept NFC-normalized UTF-8 source paths. This is filename support, not permission to rename an existing file, execute an asset, or transfer source to an AI provider. Application identifiers, Kotlin class names, target identifiers and build task/scheme grammar remain separately constrained.

Paths must be relative, within 512 UTF-8 bytes, with components within 255 bytes. Unicode letters/marks/numbers plus conservative spaces, slash, dot, underscore and hyphen are admitted. Non-NFC paths, hidden components, controls/format characters (including bidi controls), shell punctuation, Windows device aliases, trailing-space/dot aliases, credential names, casefold collisions and file/directory prefix collisions are rejected. Symlink/hardlink and mutation checks at capture remain in place. No names are silently normalized or renamed.

Existing extension defaults remain unchanged. An operator-selected observation profile may add `inputExtensions`, a nonempty unique list drawn from:

`.metal`, `.glsl`, `.tiff`, `.nib`, `.a`, `.js`, `.css`, `.woff`, `.woff2`, `.aar`, `.jar`, `.so`, `.html`, `.map`.

For example, `"inputExtensions": [".metal", ".tiff"]` allows explicitly listed matching files in the UIKit `sourceInputs`; `"inputExtensions": [".js", ".css", ".jar"]` does the same for Views input assets. It does not discover additional files or recursively admit an archive/framework/cache. Each path must still be explicitly selected and pass all other boundaries. An extension cannot override protected names or output/credential exclusions. New extensions become part of the profile digest and preparation/build receipts.

Binary assets are copied as inert bytes. Their presence does not validate their contents, authorize code execution, supply a package manager cache, or establish dependency trust. Actual compilation/linking remains a separately selected build operation. A framework directory whose executable has no allowed suffix is not fully supported by adding its container name; this is not blanket `.xcframework` support.

The fixed UIKit observation runtime template recognizes the optional field and normalized Unicode paths. Rebuild instrumented applications to use that template. Portable tests check profile/preparation behavior; native compiler/filesystem behavior remains a separate qualification, especially normalization differences on macOS volumes. A decomposed selected path string is rejected rather than implicitly repaired. Capture does not compare the filesystem's stored directory-entry spelling; native normalization-insensitive lookup behavior is not qualified by the portable tests.

## Limits deliberately unchanged

- At most 900 explicitly selected observation inputs, 8 MiB per file, and 60 MiB aggregate.
- Existing 256 KiB profile limits and BlobSet/transport limits remain unchanged.
- Existing protected build/fixture recipes, small editable set, and external transfer approvals remain unchanged.

This addresses filename and asset exclusions only. It does not make a large repository fit the current observation or protected execution budgets. Larger immutable build snapshots require a separate reviewed resource policy and versioned streaming protocol, not a global limit increase.
