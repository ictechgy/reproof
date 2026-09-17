# Protected repair execution route

2026-09-12 · G8a authority, G8b VM execution and G9 integration

## Current result and scope

The software now includes one Apple Virtualization backend, a fixed native host
bridge, a guest-only launcher/agent, authenticated bounded transfers, explicit
resource packaging, durable cancellation, and termination-gated recovery.
The host and guest native programs compile against the installed Apple SDK.
The real native host rejects invalid image resources before VM start; the guest
launchers reject this host. These checks do **not** prove a working macOS guest.

The parent ran the current doctor on the host; `artifacts/qa-delivery/g8a-doctor-host.json`
records both frameworks available and virtualization runtime supported. A sandboxed
worker measured the runtime as unsupported. These are execution-context facts,
and neither grants authority. No guest image, offline toolchain, environment
descriptor, signing policy, company application, or owned mobile environment has
been supplied.

Actual VM acceptance is a separate G8b environment gate. It remains blocked
until an explicitly supplied owned environment is booted and independently
tested for network, filesystem, process termination, and cleanup containment.
No candidate has run in an actual VM in this delivery. Protocol tests use
explicit VM/executor doubles and do not replace that environment gate.
The [G9 project repair integration](PROJECT-REPAIR.md) now composes this backend
with fixed signing and signature inspection, independent validation and exclusive
mobile supervision. Their control flow is tested using explicit protocol doubles.
Actual signing tools, native mobile containment, company regression observers and
real AI acceptance still require operator implementations, policies and environments.

The G8b software gate passed 56 tests again after G9 integration in
`artifacts/qa-delivery/g9-regression-g8b-r1.json`. The unchanged G8a gate retains
25 passing tests from `g8a-current-parent-r1.json`. The actual environment command returned
`environment-not-supplied` with no VM boot or qualification in
`artifacts/qa-delivery/g8b-environment-r1/result.json`.

## Execution route contract

The protected route is deliberately fixed:

1. A trusted host service freezes the approved source manifest. Candidate code
   cannot write the original or the project, scenario, fixture, oracle, build,
   artifact, validation, signing, or cleanup policies.
2. A registered `build-guest` backend receives the sealed source by digest and
   runs one registered build recipe in a disposable macOS guest. The guest uses
   an explicitly supplied immutable image and offline toolchain. Guest network
   is denied; there is no host-process fallback.
3. A bounded host artifact validator, outside candidate control, validates the
   extracted artifact and records its digest. Candidate-produced JSON, XML, and
   test reports remain supplemental.
4. If the target requires signing, a dedicated host stage resolves a registered
   identity reference and approved entitlements outside the guest. It invokes a
   fixed host signer, never a candidate command or hook, and records the input
   artifact digest, output artifact digest, identity-policy ID, and entitlements
   digest. Credentials, signing identities, and provisioning material are never
   sent to the guest, AI adapter, source tree, issue package, or doctor output.
5. The approved device provider checks the signed artifact/application identity
   and installs it only under a separately qualified `mobile-device`
   environment. A `build-guest` qualification cannot authorize this step.
6. The host trusted scenario interpreter injects the frozen scenario. Required
   UI/backend predicates are measured by a read-only process or registered
   trusted runner outside candidate control. Every required validation check is
   fixed by the trusted project revision before candidate execution.
7. The supervisor confirms candidate termination, fixture cleanup, device
   cleanup/sanitation, and guest shutdown/overlay disposal. Missing or unknown
   confirmation blocks any later `verified` verdict and retains quarantine.

Desktop candidate execution, when a project needs it, uses a distinct
`desktop-guest` qualification and remains inside a disposable guest. Guest
qualification never contains a mobile application after extraction; mobile
network, account, backend, keychain/shared storage, background activity, and
sanitation controls therefore require their own evidence.

## Preparing an owned guest

The concrete backend supports an **arm64 macOS guest on Apple Silicon**. It
accepts a preinstalled raw disk, its matching hardware model, machine identifier
and auxiliary storage, and an offline toolchain disk. It does not download an
IPSW, install macOS, read signing credentials, or discover an existing personal VM.
Use a dedicated guest image without personal accounts or host credentials.

Build the fixed native programs in a new directory:

```bash
python3 scripts/build-macos-execution.py --output-new artifacts/macos-execution-tools
```

The script compiles the repository's Swift/C sources and ad-hoc signs only its
host helper with the Virtualization entitlement. It reads no signing identity.
Its `compiled` result still reports `actualVM: false` and `qualified: false`.

A trusted administrator supplies a JSON recipe catalog. Each entry has exactly
`id`, `executionClass`, `argv`, `artifactPolicyId`, `cleanupPolicyId`,
`outputPaths`, `maxOutputBytes`, and `timeoutMs`. `argv[0]` is an absolute **guest**
tool path; commands and policies never arrive from a candidate/package RPC.
Build wrappers must use supplied offline dependencies and write the declared
regular output files under `$REPROLOOP_OUTPUT_DIR`. Candidate-influenced build
scripts execute inside the VM. The input source is the working directory.

```bash
python3 scripts/package-repair-guest.py \
  --catalog APPROVED_GUEST_CATALOG_JSON \
  --native-build artifacts/macos-execution-tools \
  --uid DEDICATED_GUEST_UID --gid DEDICATED_GUEST_GID \
  --output-new artifacts/guest-agent-package
```

The package contains only fixed agent code, its native launchers, the selected
catalog and a digest-bound policy. It does not install itself. Inside the owned
guest, arrange the following before sealing the VM:

- Install package contents at `/Library/ReproLoopGuest`, owned by root and not
  writable by the candidate UID/group. The installation directory must permit
  traversal to the offline Python runtime; agent policy/code remain root-only.
- Supply a working offline Python 3.10+ runtime at
  `/Library/ReproLoopGuest/python/bin/python3`, with its library files readable
  by the candidate. The executable must be protected against candidate writes.
- Use a dedicated non-root UID, with no unrelated processes, login account state
  or credential access. The agent removes all processes of that UID after a job,
  including detached children. The final boundary is independently observed VM
  shutdown, even if a guest child changes process group.
- Install `io.reproloop.guest.plist` as a root launch daemon. It invokes the fixed
  `guest-connect` program, which checks the kernel's guest marker and connects
  only to host CID 2, vsock port 4050. No TCP endpoint is configurable.
- Confirm that the toolchain volume is mounted and the agent starts after a cold
  boot, then shut the guest down before supplying the base resources.

The package's `agentDigest` covers its actual fixed files, UID/GID and catalog.
The guest recomputes it before readiness. A manifest alone cannot attest that the
package was installed in a supplied disk; actual boot qualification checks that.

The provisioning metadata is a JSON object with `schemaVersion: 1`,
`environment` (G8a guest descriptor), `guestImage` (G8a image manifest),
`toolchain` (G8a offline-toolchain manifest), `agentDigest`, and `catalog`.
The declared image/toolchain sizes and SHA-256 digests must match supplied bytes.
The catalog must match the installed agent policy. Only the five supported guest
controls are accepted by this concrete implementation.

```bash
python3 scripts/provision-repair-guest.py \
  --metadata APPROVED_VM_METADATA_JSON \
  --disk OWNED_PREINSTALLED_RAW_DISK --auxiliary MATCHING_AUXILIARY_STORAGE \
  --hardware MATCHING_HARDWARE_MODEL --machine MATCHING_MACHINE_IDENTIFIER \
  --toolchain OFFLINE_TOOLCHAIN_RAW_DISK \
  --helper artifacts/macos-execution-tools/vm-helper \
  --output-new artifacts/sealed-guest-bundle
```

Select canonical paths without symlink components. Provisioning hashes every
resource, creates a new private bundle and makes base files read-only. The
composite environment digest includes the image, toolchain, hardware/machine
identity, auxiliary storage, helper, agent identity, catalog and resource limits.
Per-run disk/auxiliary clones are writable; the toolchain attachment is read-only.
There are no network devices or host directory shares. Provisioning never boots
or qualifies a VM. Partial failed provisioning remains in its new output directory.

## Qualification and execution

An owned-environment JSON file contains exactly `schemaVersion: 1`, `backendId`,
absolute `bundlePath`, absolute `statePath`, and `diskBudgetBytes`. The state
directory is private to its owner and remains canonical for that VM identity.
Budget at least the complete logical raw-disk plus auxiliary-storage size for
one writable clone, even when the filesystem supports copy-on-write.
One machine identity is tied to one immutable environment and canonical state
root. The journal retains at most 512 operation identities and refuses new work
when full; it does not silently evict replay/cleanup history or reset an identity.
Changing the image, installed catalog or toolchain requires an explicitly
provisioned and qualified environment with its own machine identity.

```bash
python3 scripts/qa-execution-backend.py \
  --environment APPROVED_OWNED_ENVIRONMENT_JSON --output-new artifacts/guest-qa-run
```

This command may boot **four fresh owned VM instances sequentially**. The fixed
preinstalled probe checks denied IPv4/IPv6 connection attempts, protected agent
and toolchain writes, a detached child, cancellation while that child is running,
an oversized output, and a forged passing report. Host observations must also
confirm native configuration, boot, connection, actual VM stop and overlay cleanup.
Only then does the local supervisor issue a G8a capability for the exact class
and environment. Requalification revokes the old capability set before probing.

Without `--environment`, the command saves/prints `blocked-unqualified` and exits
`2`. Malformed inputs also remain blocked. A successful CLI report is historical
evidence; it exports no live capability. A long-lived trusted service instead
calls `qualify_backend` and retains the returned process-local capability.

That same trusted composition root constructs `MacOSVirtualizationBackend` with
its `QualificationAuthority`, `GuestBundle`, and canonical `RunStore`. It registers
the project route and independent validation plan, authorizes an exact request,
then calls `execute(request, authorization, inputs)`. `build-guest` accepts an
immutable `BlobSet`. `desktop-guest` requires a project/class/digest-bound
`ValidatedArtifactInput` issued by the root's `ArtifactValidationAuthority` after
a fixed host format/identity checker accepts bounded inert bytes. A dictionary or
candidate report cannot replace either capability.

The native owner enforces an execution deadline even if Python stops making
progress. The protocol additionally checks absolute deadlines, cancellation,
session identity, direction, sequence, MAC, file order and every digest. Transfers
are limited to 64 MiB, 1,024 regular files, 32 KiB chunks and 256 KiB frames.
No archive is extracted or candidate command executed on the host. Guest logs are
bounded and represented by a digest; candidate reports contain only bounded exit
metadata. A successful result is `candidate-output`, with `verified: false`.
Independent G9 validation is still required.

## Cancellation and recovery

`cancel(operation_id, request_digest)` persists cancellation before interrupting
the channel. Duplicate operation IDs never resume work. Missing VM-stop evidence
or incomplete overlay deletion keeps the run quarantined and its disk reservation
charged. A different state directory cannot bypass the machine's canonical
authority marker.

The native helper writes and fsyncs a private, run-bound `termination.json` only
after actual VM stop, or after rejecting configuration before calling VM start.
The guest has no access to that file descriptor or host directory. Following a
parent restart, `reconcile` requires that record; helper death, a caller's boolean
or an imported report cannot clear uncertainty. Cleanup removes only the three
known owned files and fsyncs the directory before releasing the reservation.
Unexpected files or missing/invalid stop records retain quarantine.

```bash
python3 scripts/qa-execution-backend.py \
  --environment APPROVED_OWNED_ENVIRONMENT_JSON \
  --recover-operation OPERATION_ID --request-digest REQUEST_SHA256 \
  --output-new artifacts/guest-recovery-run
```

Recovery never replays a candidate, issues qualification, or publishes a verified
result. A durable cancelled run remains cancelled after confirmed cleanup.

## Public integration interfaces

`reproloop.execution.protocol` validates JSON-compatible registration data:

- `validate_guest_image_manifest` and `validate_toolchain_manifest` accept only
  IDs, architecture, bounded size, digests, and bounded tool versions. They do
  not accept a path, URL, command, plugin, or executable registration.
- `validate_environment_descriptor` keeps `build-guest`, `desktop-guest`, and
  `mobile-device` shapes and required controls distinct. Guest network is
  `none`; mobile networking is either `none` or a registered policy reference.
- `validate_signing_policy` permits only fixed host signing tools, opaque
  identity/provisioning references, approved entitlements digest, forbidden
  candidate hooks, and pre/post artifact digest recording.
- `validate_external_validation_plan` accepts only `trusted-runner` or
  `external-observation` checks. Its candidate-report policy is fixed to
  `supplemental-only`.
- `validate_execution_route` binds the trusted project/backend/class,
  environment, input type, recipe, artifact policy, validation plan, cleanup
  policy, and class-specific signing identity. A request cannot choose a new
  recipe or weaken cleanup after this route is registered.
- `validate_execution_request` binds operation/backend/class/project,
  environment, sealed input, registered recipe, artifact policy, every required
  validation ID, and cleanup policy. Mobile requests additionally bind platform,
  application, and signing policy.
- `validate_backend_qualification_record` validates a bounded, expiring record.
  The returned dictionary is inert and cannot authorize execution.

`reproloop.execution.backend.QualificationAuthority` is an object capability
owned by the trusted supervisor process. Its public integration sequence is:

1. The G8b supervisor performs a real independent probe and calls
   `record_probe` with the resulting evidence digest. This method is not an RPC
   and must never be exposed to candidate or imported-package code.
2. `issue_backend_qualification(record, receipts, evaluated_at_ms=...)` requires
   current, passing, backend/class/environment-bound local receipts for all class-specific
   probes. A wire record or boolean report cannot replace those objects.
3. After authenticated trusted project registration,
   `register_validation_plan`, `register_execution_route`, and
   `register_signing_policy` produce capabilities bound to the same authority
   instance. Re-registering identical metadata is idempotent; changing a recipe,
   cleanup rule, signing identity, or other content under an existing ID is
   rejected. Definition comparison and insertion are serialized across supervisor
   threads, so concurrent registrations cannot approve conflicting definitions.
   Imported scenario/candidate data cannot register these objects.
4. `authorize(..., evaluated_at_ms=...)` requires the exact backend/class/
   environment, complete independent validation set, and, for mobile, matching
   application/platform/signing policy. It returns an
   `ExecutionAuthorization`, not a verdict.
5. Immediately before backend dispatch, G8b calls
   `check_authorization` with the unchanged request and a current evaluation
   time. A backend must accept only the authority instance owned by its trusted
   composition root; creating another authority does not authorize that backend.

`DisabledExecutionBackend` remains available for unconfigured integrations and
always raises `ExecutionDenied`. The concrete implementation is in
`reproloop.execution.runtime`; registration does not enable a host subprocess
fallback for candidate code.

## Read-only doctor

Run the current host inspection without descriptors:

```bash
python3 scripts/repair-backend-doctor.py --read-only
```

The current expected exit is `2`: support facts and missing inputs are useful,
but the backend is not qualified. The JSON report uses static status codes and
never includes supplied paths, descriptor contents, compiler errors, device
identities, signing references, or credentials.

The fixed Swift prerequisite probe uses bounded host processes with group cleanup,
including compiler descendants on timeout. This probe accepts no candidate source
or executable command from a descriptor.

Explicit non-secret inputs may be selected with:

```bash
python3 scripts/repair-backend-doctor.py --read-only \
  --guest-image-manifest GUEST_IMAGE_METADATA_JSON \
  --guest-image GUEST_IMAGE_RESOURCE \
  --offline-toolchain-manifest OFFLINE_TOOLCHAIN_METADATA_JSON \
  --offline-toolchain OFFLINE_TOOLCHAIN_RESOURCE \
  --environment-descriptor BUILD_ENVIRONMENT_JSON \
  --environment-descriptor MOBILE_ENVIRONMENT_JSON \
  --signing-policy-descriptor SIGNING_POLICY_METADATA_JSON
```

The doctor reads only the explicitly selected bounded regular descriptor files.
Known credential, key, provisioning-profile, auth, and `.env` names are refused
before opening. Resource files are only checked for regular-file presence and
declared size; `resource-present-unverified` does not mean their digest, boot,
toolchain, or containment behavior passed. Symlinks are refused. The doctor does
not search the Mac, download, provision, boot, mount, sign, contact a device, or
read any credential.

Statuses have these meanings:

- `prerequisites-missing`: current framework/runtime check did not fail, but one
  or more required build inputs are absent.
- `prerequisite-inputs-present`: descriptor shape, cross-references, resource
  presence/size, and host support are suitable to attempt G8b. Authority remains
  `none` and the environment gate remains `blocked-unqualified`.
- `backend-unqualified`: current host runtime support failed or an explicitly
  selected input was invalid. No route is enabled.

Exit `0` means only that prerequisite inputs are present for a later authorized
qualification attempt. It is never VM acceptance and never execution authority.

## Required later evidence and rollback

G8b software checks use protocol doubles to prove denials and bindings, plus
actual native compilation, host/invalid-resource denial and a native-written
pre-start termination record. Run the current software gates with:

```bash
python3 scripts/release-check.py --goal G8A --effects filesystem,process,native-compile
python3 scripts/release-check.py --goal G8B --effects filesystem,process,native-compile
```

The environment gate additionally requires evidence from an owned guest for an
actual boot/readiness lifecycle, denied network attempts, immutable source,
bounded extraction, child-process termination after cancellation, confirmed
shutdown, and overlay cleanup. Mobile qualification separately requires an owned
registered device, approved application/signing inputs, backend/account scope,
termination, fixture cleanup, and sanitation evidence.

Rollback disables backend registration and new authorizations. It preserves
qualification/probe evidence as history but restores no live authorization,
does not resume a guest or device operation, and does not clear unknown cleanup.
No G8a state needs migration because it creates no durable authority or runtime.

Software behavior is covered by the G8a gate:

```bash
python3 scripts/release-check.py --goal G8A --effects filesystem,process,native-compile
```

This gate is not company acceptance. Company source/build, real QA records,
fixture/backend contracts, approved AI-transfer policy, physical-device
authorization, signing inputs, and a second Mac remain external inputs.
