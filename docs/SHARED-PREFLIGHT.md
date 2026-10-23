# Read-only shared deployment preflight

Before initializing access state or starting a coordinator, validate the selected
**public configuration files**:

```sh
reproof live-check-config --shared-config /path/shared.json \
  --issue-config /path/issue-runtime.json --output /path/proposed-output
```

`--issue-config` is optional. `--output` checks the proposed runtime output
namespace; it never creates that directory and is not a report destination.
Relative paths keep the existing shared configuration convention: they resolve
against the process working directory, not the configuration file directory.
Use explicit absolute paths for repeatable deployment checks.

The command reads the shared document and its referenced project/collection
policy documents, and optionally the issue runtime document. It checks schemas,
listener/origin/TLS declarations, duplicate project revisions, selected issue
project digests and adapter/fixture/recipe references. With `--output`, it rejects
overlap with the access-state namespace. Inputs are limited to 1 MiB each and an
8 MiB declared total. Use only public configuration inputs, never credential
files. The report contains counts and check-scope flags, not paths, configuration
contents, variable names, private-key data or exception details.

Exit status:
- `0`: `configuration-validated`, within the explicitly reported scope.
- `2`: `rejected`, with `public_configuration_invalid` or
  `public_configuration_io`; normal argument syntax errors also exit 2.
- `130`: interrupted, without a successful validation claim.

The JSON always states `executionAuthority: "none"`. No state directory or DB is
created/opened, credential is read, listener is bound, secret environment value
is resolved, helper is launched, or device/fixture/AI request is sent. HTTPS
requires both TLS path declarations, but the files themselves are not loaded.
The command is not a startup, repair, qualification, or device readiness probe.

## What still needs separate acceptance

The report's `notChecked` list explicitly includes durable authorization and
trust state, worker reachability, device readiness, TLS-key validity, listener
availability, qualification, secret resolution, native tools and repair
execution. A syntactically valid fixture endpoint can still be unavailable or
serve the wrong environment. A TLS declaration can still name an expired or
invalid certificate. Public schema checks cannot establish runtime trust.

Before a company pilot, an authorized operator must separately review existing
access/project enrollment, provision credentials and TLS as appropriate, verify
workers and owned devices, establish test-data handling/retention, and qualify
the actual app/fixture/oracle. Those activities are not performed by preflight.
Run preflight again immediately before authorized startup; it is not a signed
snapshot or protection against files changing after validation.

Portable regression selection:

```sh
python scripts/release-check.py --goal PREFLIGHT --effects filesystem,process
```

Tests use temporary public files and forbidden-effect mocks. They do not start a
coordinator or establish actual deployment or security acceptance.
