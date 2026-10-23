# Company pilot preparation

This runbook prepares a bounded, reviewable pilot. It is not an enterprise-ready
claim or permission to deploy, access devices, enroll identities, or send company
data to an AI service. Complete the software/package checks first; stop at the
explicit approval boundary before real environment work.

## 1. Agree on the pilot boundary

Name an operator and reviewer, one app revision, an owned test environment, a
small set of reproducible cases, and the approved data/retention boundary. Define
the initial fixture, expected behavior, independent regression checks, and cleanup
acceptance before running an app. Use synthetic data for this preparation lane.
Keep test credentials, signing material, TLS private keys, and company source out
of public configuration examples, reports, commits and chat transcripts.

These are different evidence levels:

- Portable software/package checks: synthetic files, temporary stores, owned
  processes, and explicitly declared loopback services. No native tool/device/VM
  or external AI acceptance.
- Historical app/device work: README and HANDOFF describe prior sample and
  one-product-app iPhone runs. Those records are not reproduced by these checks;
  their local `artifacts/` paths are not public download links.
- Company acceptance: the selected app, fixtures, permissions, hosts and devices
  must be qualified in the actual approved environment. A historical `verified`
  label does not authorize a new run or establish equivalent coverage.

## 2. Identify and check the source

Use a clean checkout of the intended commit. Record its commit SHA, Python
version, OS, and the exact gate results. If there are local changes, do not label
the result as acceptance of the unchanged commit.

```sh
git rev-parse HEAD
git status --short
python3 --version
for goal in G0 RUNTIME CLI PREFLIGHT; do
  python3 scripts/release-check.py --goal "$goal" --effects filesystem,process || exit 1
done
for goal in LOCATOR PACKAGE; do
  python3 scripts/release-check.py --goal "$goal" --effects filesystem,process,loopback || exit 1
done
```

PACKAGE requires pip/venv and a prepared setuptools wheel builder in the same
Python interpreter; CI pins `setuptools==84.0.0`. Package construction and
installation run offline without dependency downloads. Reproof itself has no
third-party Python runtime dependencies. The CI platform matrix is Ubuntu with
Python 3.11–3.13, not a native/mobile support matrix.

Retain each JSON report and exit status in a new run-specific location. Every
required check must pass; blocked, skipped, missing, or failed checks are not
acceptance. Effects are test-selection declarations, not a sandbox. Do not
replace these selections with discovery: other suites invoke native tooling.
See [release contracts](RELEASE-CONTRACTS.md) for exact gate boundaries.

## 3. Verify the wheel that will actually be used

Follow [installation](INSTALLATION.md) to build or receive a wheel for the chosen
source. The handoff must identify the source commit SHA, original wheel filename,
wheel SHA-256, and gate evidence. Check the expected digest through a trusted
delivery channel; a checksum supplied only beside untrusted bytes does not prove
their origin. Do not infer current checkout features from a package version or a
`pip install reproof-qa` result. This runbook makes no PyPI publication claim.

After setting `RELEASE_WHEEL` to its absolute path, calculate its SHA-256 locally:

```sh
: "${RELEASE_WHEEL:?Set RELEASE_WHEEL to the supplied wheel's absolute path}"
python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$RELEASE_WHEEL"
```

Compare the complete digest, then install offline into a fresh virtualenv outside
the checkout as described in the installation guide. Preserve the import-origin
and `installation-check` output for that exact wheel. Require `ready`,
`distribution: "installed"`, `actualMobile: false`, and `actualVM: false`.
The PACKAGE gate's temporary wheel is not automatically the supplied wheel.

Optionally run the explicitly synthetic loopback demo, using a new output path,
and stop it after inspection. Do not add real provider, worker, repair-agent, or
shared-deployment options to turn this smoke check into a different operation.

## 4. Validate public configuration without starting services

Prepare public shared configuration and its referenced project/collection policy
files; include the issue runtime document if the pilot uses that workflow. The
checked-in [shared example](examples/shared-coordinator-v2.json) and
[issue example](examples/issue-runtime.json) are schema illustrations with
placeholder paths/digests, not a ready deployment. Use explicitly selected,
non-secret files and absolute paths, including referenced paths. Relative paths
resolve against the process working directory, not the configuration directory.

Run the installed CLI from the fresh installation working directory:

```sh
.venv/bin/reproof live-check-config \
  --shared-config /absolute/path/shared.json \
  --issue-config /absolute/path/issue-runtime.json \
  --output /absolute/path/proposed-runtime-output
```

`--output` checks namespace separation only; it does not write a report or create
the runtime directory. `--issue-config` is optional. Require exit 0 and
`status: "configuration-validated"`, retaining the scope flags and `notChecked`
list. `executionAuthority` must remain `none`. On exit 2, fix the rejected public
inputs; on interruption, do not record a pass.

This reads public configuration references only. It does not create/open an
access DB, read credentials or TLS keys, resolve secret values, bind a listener,
contact workers, or launch helpers, devices, fixtures or AI. It cannot prove
durable authorization, worker reachability, device readiness, TLS-key validity,
listener availability, qualification, secret resolution, native tools, or repair
execution. Repeat it before any separately authorized startup; it is not a signed
snapshot or protection against subsequent file changes. See
[preflight details](SHARED-PREFLIGHT.md).

## 5. Stop for real-environment acceptance

The preparation result is “portable checks and selected public configuration
validated,” not “company pilot accepted.” Before proceeding, the responsible
operator must approve the exact environment work and data handling, including:

- Service hosts/listeners, TLS, identities and least-privilege project roles,
  credential provisioning, worker enrollment, retention and access to evidence.
- Owned devices, permitted installation/reset actions, app build/signing inputs,
  fixture and observer services, and cleanup/recovery responsibilities.
- Whether builds use the explicitly non-isolated host-build lane or the separate
  VM lane, and which controls are technically enforced versus operator attestations.
- Any external AI recipient and exact source/evidence transfer scope. No external
  AI call is needed for preparation; an approved local patch is a different lane.

The operational guides are [shared coordination](SHARED-COORDINATOR.md),
[workers](WORKER-RUNTIME.md), [issue workflow](ISSUE-WORKFLOW.md), and
[protected repair](PROJECT-REPAIR.md). Their initialization and execution commands
are not part of this preflight sequence. In particular, `doctor`, native builds,
`live-admin`, real-provider `live-serve`, signing, recovery, and repair must not be
used as harmless installation probes.

For authorized company acceptance, retain the immutable original recording and
oracle, exact app/build/configuration bindings, all original/candidate attempts,
independent regression results, and confirmed cleanup. Include negative cases
and interruption/restart/cleanup failures. Inconclusive results, incomplete
cleanup, or quarantine are not a pass. Preserve journals and evidence; do not
delete state to clear a failure. Follow the platform-specific recovery guide.

## Locator changes are review-only

[Locator proposals](LOCATOR-PROPOSALS.md) are an authenticated API, not autonomous
self-healing. They do not generate candidates, call an AI, click a fallback, or
prove that a replacement exists, is unique or means the same thing. Reviewers
must establish those facts using authorized evidence.

An explicit apply creates a new unapproved specification revision while
preserving the original recording, assertions, actions other than the selected
target, waits and fixtures. Obtain fresh approval and original qualification
before candidate verification. Do not weaken an expected result to erase a
failure, reuse old approval, or relabel the failed run as healed. Reverting also
creates a new unapproved revision. Automatic candidate generation, visual
matching, an approval UI and runtime fallback are not implemented.

## Limits to retain in the pilot decision

- A green portable/package job is not physical-device, company-app, two-Mac TLS,
  VM-isolation, operational load, or enterprise security acceptance. The isolated
  VM build lane and two-Mac acceptance remain open in the documented status.
- General app support depends on declared profiles, supported UI IDs, fixtures
  and independent observations. Historical sample/one-app results do not imply
  arbitrary framework, device, OS or selector coverage.
- On an unsupervised iPhone, the documented egress lane measures device-interface
  counters and rejects excess traffic; it does not prevent transmission. Noise
  thresholds and optional packet capture have limits. Do not use this as a
  guarantee that confidential data cannot leave the device. See
  [egress limitations](IOS-EGRESS-ISOLATION.md).
- Resources are checked against an unsigned manifest for consistency, not package
  provenance. Supply-chain review, access/security review and company evidence
  retention decisions are separate obligations.

Record unperformed checks as unperformed, with an owner and the approval or
environment input needed next. Keep prior results historical rather than
claiming they were rerun for this pilot.
